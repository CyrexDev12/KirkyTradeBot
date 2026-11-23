"""Match and rank full-game Polymarket US player props from public data."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import re
import time

import httpx

from .data import canonical
from .markets import _all_events, _amount, _date, _event_detail, _get
from .player_yardage import is_pregame, kickoff, name_key, valid_stat
from .trading.book import NO, OPEN, YES, is_fresh, parse_book, selected_view
from .trading.fees import execution_fee

MARKET_TYPES = {
    "football_player_rushing_yards": "rushing",
    "football_player_passing_yards": "passing",
}
SETTLEMENT = ("Full game including overtime, using the official box score. "
              "A player with no qualifying snap settles at the last fair market price.")
MIN_EDGE = 0.02
QUANTITY = Decimal("10")
_cache: dict[tuple, tuple[float, dict]] = {}
_scan_lock = asyncio.Lock()
_LOG = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,200}")


@dataclass(frozen=True)
class Prop:
    player_name: str
    team: str
    stat: str
    threshold: int
    label: str
    slug: str
    coefficient: Decimal
    reference_over: float | None
    reference_under: float | None

    @property
    def line(self):
        return self.threshold - 0.5


def event_matches(game, event):
    start = _date(event.get("startTime") or event.get("startDate"))
    if not start or not start.tzinfo or abs((start - kickoff(game)).total_seconds()) > 3600:
        return False
    if event.get("ended") or event.get("closed") or event.get("live"):
        return False
    event_teams = {canonical(team.get("abbreviation")) for team in event.get("teams") or ()}
    return event_teams == {game["home"], game["away"]}


def parse_props(game, event):
    if not event_matches(game, event):
        return []
    teams = {str(team["id"]): canonical(team["abbreviation"]) for team in event["teams"]
             if team.get("id") is not None and team.get("abbreviation")}
    props = []
    for market in event.get("markets") or ():
        market_start = _date(market.get("gameStartTime"))
        if not market_start or not market_start.tzinfo or abs((market_start - kickoff(game)).total_seconds()) > 3600:
            continue
        prop = parse_prop(market, teams)
        if prop is not None:
            props.append(prop)
    return props


def parse_prop(market, teams):
    stat = MARKET_TYPES.get(market.get("sportsMarketType"))
    slug = market.get("slug")
    if (stat is None or market.get("marketType") != "props" or not market.get("active")
            or market.get("closed") or market.get("hidden") or market.get("archived")
            or market.get("status") != "MARKET_STATUS_OPEN"
            or not isinstance(slug, str) or not _SAFE_SLUG.fullmatch(slug)):
        return None
    metadata = market.get("metadata") or {}
    player, label = metadata.get("playerName"), metadata.get("lineLabel")
    team_id = str(metadata.get("teamId"))
    team = teams.get(team_id)
    if not isinstance(player, str) or not team or not isinstance(label, str):
        return None
    sides = market.get("marketSides") or []
    if len(sides) != 2:
        return None
    long, short = (next((side for side in sides if side.get("long") is value), {}) for value in (True, False))
    names = (str(long.get("description")).casefold().strip(), str(short.get("description")).casefold().strip())
    if any(str(side.get("teamId")) != team_id or side.get("tradable") is not True for side in (long, short)):
        return None
    try:
        raw_line = float(market["line"])
        coefficient = Decimal(str(market["feeCoefficient"]))
        if not coefficient.is_finite() or not 0 <= coefficient < 1:
            return None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None

    description = str(market.get("description") or "").casefold()
    if not all(term in description for term in ("overtime is included", "official box score", "at least one snap")):
        return None
    if not re.search(r"must participate[^.;]*[;,]\s*otherwise,? the market will settle to the last fair market price", description):
        return None
    stat_phrase = f"{stat} yards"
    player_phrase = re.escape(player.strip().casefold())
    if names == ("yes", "no") and re.fullmatch(r"\d+\+", label) and label == f"{int(label[:-1])}+":
        threshold = int(label[:-1])
        rule = rf"settle to yes if\s+{player_phrase}\s+records?\s+{threshold}\s+or more\s+{stat_phrase}\b"
        if raw_line != threshold or not re.search(rule, description):
            return None
    elif names == ("over", "under") and raw_line > 0 and raw_line % 1 == 0.5:
        threshold = int(raw_line) + 1
        rule = rf"{player_phrase}\s+records?\s+over\s+{re.escape(str(raw_line))}\s+{stat_phrase}\b"
        if not re.search(rule, description):
            return None
    else:
        return None
    if threshold < 1:
        return None
    bid, ask = _amount(market.get("bestBidQuote")), _amount(market.get("bestAskQuote"))
    return Prop(player, team, stat, threshold, label, slug, coefficient, ask, 1 - bid if bid is not None else None)


def shortlist(candidates):
    """Prioritize coverage across distinct players before checking alternate lines."""
    by_player = {}
    for prop, projection, probability in candidates:
        prices = [(p, price) for p, price in ((probability, prop.reference_over),
                                             (1 - probability, prop.reference_under))
                  if price is not None and 0 < price < 1]
        if not prices:
            continue
        edge = max(p - price - float(prop.coefficient) * price * (1 - price) for p, price in prices)
        if edge <= 0:
            continue
        key = projection.payload["player_id"], prop.stat
        by_player.setdefault(key, []).append((edge, prop.slug, prop, projection, probability))
    ordered = [sorted(group, key=lambda item: (-item[0], item[1])) for group in by_player.values()]
    prioritized = []
    for level in range(3):
        group = [lines[level] for lines in ordered if len(lines) > level]
        prioritized.extend(sorted(group, key=lambda item: (-item[0], item[1])))
    return [item[2:] for item in prioritized[:36]]


def evaluate_book(prop, projection, probability, snapshot, now):
    if snapshot.state != OPEN or not is_fresh(snapshot, now, 45)[0]:
        return None
    best = None
    for side_name, selected, prob in (("over", YES, probability), ("under", NO, 1 - probability)):
        remaining = QUANTITY
        fills = []
        for price, size in selected_view(snapshot, selected).asks:
            take = min(remaining, size)
            remaining -= take
            fills.append((price, take))
            if remaining == 0:
                break
        if remaining != 0:
            continue
        price = float(sum(price * size for price, size in fills) / QUANTITY)
        fee = float(execution_fee(prop.coefficient, fills) / QUANTITY)
        edge = prob - price - fee
        if edge < MIN_EDGE or (best is not None and edge <= best["edge"]):
            continue
        record = {key: value for key, value in projection.payload.items() if key != "history"}
        best = {**record, "side": side_name, "line": prop.line, "market_line_label": prop.label,
                "over_threshold": prop.threshold, "model_probability": prob,
                "price": price, "estimated_fee_per_contract": fee, "edge": edge,
                "quantity": int(QUANTITY), "market_slug": prop.slug,
                "quote_at": snapshot.received_at.isoformat(), "settlement": SETTLEMENT}
    return best


async def scan_week(games, data, season, week):
    async with _scan_lock:
        now = datetime.now(timezone.utc)
        future_games = [game for game in games if is_pregame(game, now)]
        key = season, week, tuple(game["game_id"] for game in future_games)
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 180:
            # Ranking entries preserve quote timestamps. Quotes are never described as live after a cache hit.
            return cached[1]
        try:
            result = await _scan(future_games, data, now)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            _LOG.warning("NFL player prop listing unavailable: %s", exc)
            result = {"picks": [], "note": "Polymarket could not return player markets for this week."}
        result = {**result, "season": season, "week": week, "fetched_at": now.isoformat(), "mode": "PAPER_ONLY"}
        _cache.clear()
        _cache[key] = time.monotonic(), result
        return result


async def _partial(coroutines, timeout):
    if not coroutines:
        return []
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout)
        return [task.result() for task in tasks if task in done]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _scan(games, data, now):
    if not games:
        return {"picks": [], "note": "There are no upcoming games in this NFL week."}
    async with httpx.AsyncClient(timeout=8, limits=httpx.Limits(max_connections=8)) as client:
        events = await asyncio.wait_for(_all_events(client), timeout=12)

        async def collect(game):
            matches = [event for event in events if event_matches(game, event)]
            if len(matches) != 1:
                return []
            event_id = str(matches[0].get("id"))
            if not re.fullmatch(r"\d{1,20}", event_id):
                return []
            try:
                detail = await _event_detail(client, event_id)
                return [(game, prop) for prop in parse_props(game, detail)]
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                _LOG.info("Player props unavailable for %s: %s", game["game_id"], exc)
                return []

        grouped = await _partial([collect(game) for game in games], 18)
        props = [item for group in grouped for item in group]
        if not props:
            return {"picks": [], "note": "No verified rushing or passing yardage markets were returned for upcoming games."}
        roster = {game["game_id"]: data.active_players((game["home"], game["away"]), game["week"]) for game in games}
        candidates, projections = [], {}
        for game, prop in props:
            players = [p for p in roster[game["game_id"]] if p.team == prop.team
                       and name_key(p.name) == name_key(prop.player_name) and valid_stat(p.position, prop.stat)]
            if len(players) != 1:
                continue
            player = players[0]
            key = game["game_id"], player.id, prop.stat
            if key not in projections:
                projections[key] = data.project(player, prop.stat, game, now)
            projection = projections[key]
            if projection is None or not projection.priceable():
                continue
            probability = projection.over_probability(prop.line)
            if not 0.05 <= probability <= 0.95:
                continue
            candidates.append((prop, projection, probability))

        async def quote(prop, projection, probability):
            try:
                payload = await _get(client, f"/v1/markets/{prop.slug}/book")
                received = datetime.now(timezone.utc)
                if received >= _date(projection.payload["kickoff"]):
                    return None
                snapshot = parse_book(prop.slug, payload, received)
                return evaluate_book(prop, projection, probability, snapshot, received)
            except (httpx.HTTPError, RuntimeError, ValueError, InvalidOperation) as exc:
                _LOG.info("Player prop book unavailable for %s: %s", prop.slug, exc)
                return None

        checked = await _partial([quote(*candidate) for candidate in shortlist(candidates)], 30)
    finished_at = datetime.now(timezone.utc)
    ranked = sorted((pick for pick in checked if pick and _date(pick["kickoff"]) > finished_at),
                    key=lambda pick: (-pick["edge"], pick["market_slug"]))
    unique = {}
    for pick in ranked:
        unique.setdefault((pick["player_id"], pick["stat"]), pick)
    picks = list(unique.values())[:10]
    note = ("Ranked by estimated edge after fees at the retrieved order book. "
            "Probabilities assume the player participates.")
    if len(picks) < 10:
        note = f"{len(picks)} of 10 slots currently meet the market, workload, quote, and edge checks. {note}"
    return {"picks": picks, "note": note}
