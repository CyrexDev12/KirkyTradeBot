"""Polymarket US public NFL winner-market matching and executable quotes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import time

import httpx

from .data import canonical

BASE = "https://gateway.polymarket.us"
_cache: dict[tuple, tuple[float, dict]] = {}
_book_cache: dict[str, tuple[float, dict]] = {}
_event_cache: tuple[float, list[dict]] | None = None
_detail_cache: dict[str, tuple[float, dict]] = {}
_total_cache: dict[tuple, tuple[float, dict]] = {}
_request_lock = asyncio.Lock()
_next_request = 0.0


def _amount(value):
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
        return result if 0 <= result <= 1 else None
    except (ValueError, TypeError):
        return None


def _date(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def match_event(game: dict, events: list[dict]) -> dict | None:
    """Require exact side IDs, roles, start time, market type, and settlement rules."""
    game_time = _date(game["kickoff"])
    candidates = []
    for event in events:
        start = _date(event.get("startTime") or event.get("startDate"))
        if not start or abs((start-game_time).total_seconds()) > 3600:
            continue
        teams = event.get("teams") or []
        event_teams = {canonical(t.get("abbreviation")) for t in teams}
        if event_teams != {game["home"], game["away"]}:
            continue
        for market in event.get("markets") or []:
            if market.get("sportsMarketType") != "football_team_full_game_winner":
                continue
            if market.get("marketType") != "moneyline" or market.get("closed") or not market.get("active"):
                continue
            desc = (market.get("description") or "").lower()
            if "overtime is included" not in desc or "tie" not in desc or "$0.50" not in desc:
                continue
            sides = market.get("marketSides") or []
            if len(sides) != 2 or {bool(s.get("long")) for s in sides} != {True, False}:
                continue
            by_team = {canonical((s.get("team") or {}).get("abbreviation")): s for s in sides}
            if set(by_team) != {game["home"], game["away"]}:
                continue
            if (by_team[game["home"]].get("team") or {}).get("ordering") != "home" or (by_team[game["away"]].get("team") or {}).get("ordering") != "away":
                continue
            if not market.get("slug"):
                continue
            candidates.append({"event": event, "market": market, "sides": by_team})
    return candidates[0] if len(candidates) == 1 else None


def match_total_markets(game: dict, event: dict) -> list[dict]:
    """Select combined-score full-game lines; never include team or period totals."""
    start = _date(event.get("startTime") or event.get("startDate"))
    kickoff = _date(game.get("kickoff"))
    if not start or not kickoff or abs((start-kickoff).total_seconds()) > 3600:
        return []
    if {canonical(t.get("abbreviation")) for t in event.get("teams") or []} != {game["home"], game["away"]}:
        return []
    matches = []
    seen = set()
    for market in event.get("markets") or []:
        if market.get("sportsMarketType") != "football_team_full_game_total" or market.get("marketType") != "totals":
            continue
        if not market.get("active") or market.get("closed") or not market.get("slug"):
            continue
        try:
            line = float(market.get("line"))
        except (TypeError, ValueError):
            continue
        # NFL score is integer; half-point lines settle to exactly one side.
        if line <= 0 or abs((line % 1) - 0.5) > 1e-8 or line in seen:
            continue
        desc = (market.get("description") or "").lower()
        if "combine for over" not in desc or "overtime is included" not in desc:
            continue
        sides = market.get("marketSides") or []
        if len(sides) != 2:
            continue
        by_label = {str(s.get("description", "")).strip().lower(): s for s in sides}
        if set(by_label) != {"over", "under"} or not by_label["over"].get("long") or by_label["under"].get("long"):
            continue
        seen.add(line)
        matches.append({"event": event, "market": market,
                        "sides": {"over": by_label["over"], "under": by_label["under"]}, "line": line})
    return sorted(matches, key=lambda m: m["line"])


def execution(book: dict, long: bool, quantity: float) -> dict:
    """Calculate full-depth average purchase price for a specified number of contracts."""
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    levels = (book.get("offers") or []) if long else (book.get("bids") or [])
    prices = []
    for item in levels:
        p = _amount(item.get("px"))
        try:
            qty = float(item.get("qty", 0))
        except (ValueError, TypeError):
            continue
        if p is not None and qty > 0:
            prices.append((p if long else 1-p, qty))
    prices.sort(key=lambda x: x[0])
    remain, cost, fills = quantity, 0.0, []
    for p, qty in prices:
        take = min(qty, remain)
        cost += p*take
        if take:
            fills.append({"price": p, "quantity": take})
        remain -= take
        if remain <= 1e-8:
            break
    if remain > 1e-8:
        return {"fillable": False, "vwap": None, "available": quantity-remain}
    return {"fillable": True, "vwap": cost/quantity, "available": quantity, "fills": fills}


def valuation(prob: float | None, quote: dict | None, quantity: float = 100) -> dict | None:
    if prob is None or not quote:
        return None
    ask = quote.get("ask")
    mid = quote.get("display_price")
    raw = prob-mid if mid is not None else None
    if ask is None or quote.get("state") != "MARKET_STATE_OPEN":
        return {"raw_difference": raw, "gross_edge": prob-ask if ask is not None else None,
                "net_edge": None, "category": "Quote not executable", "trade_size": quantity}
    fill = quote.get("execution") or {}
    if not fill.get("fillable"):
        return {"raw_difference": raw, "gross_edge": prob-ask, "net_edge": None,
                "category": "Insufficient liquidity", "trade_size": quantity}
    vwap = fill["vwap"]
    theta = quote.get("fee_coefficient")
    if theta is None:
        return {"raw_difference": raw, "gross_edge": prob-ask, "net_edge": None,
                "category": "Fee unavailable", "trade_size": quantity}
    fills = fill.get("fills")
    if fills:
        exact = [theta*f["quantity"]*f["price"]*(1-f["price"]) for f in fills]
        # The exchange rounds per fill and caps total fees at the rounded
        # cumulative exact fee. Book levels approximate actual future fills.
        estimated_fee = min(sum(round(x, 2) for x in exact), round(sum(exact), 2))
        fee_per_contract = estimated_fee/quantity
    else:
        fee_per_contract = theta*vwap*(1-vwap)
    net = prob-vwap-fee_per_contract
    strong = float(os.getenv("NFL_STRONG_EDGE", "0.05"))
    moderate = float(os.getenv("NFL_MODERATE_EDGE", "0.02"))
    category = "Strong potential value" if net >= strong else "Moderate potential value" if net >= moderate else "Small potential value" if net > 0 else "No positive edge"
    return {"raw_difference": raw, "gross_edge": prob-ask, "net_edge": net, "category": category,
            "trade_size": quantity, "vwap": vwap, "estimated_fee_per_contract": fee_per_contract,
            "estimated_profit_per_contract": net}


async def _get(client: httpx.AsyncClient, path: str, params=None) -> dict:
    global _next_request
    for attempt in range(2):
        # Spread reads across time; a burst of book requests can hit the
        # provider's per-IP limit even when the one-second average is low.
        async with _request_lock:
            wait = max(0, _next_request - time.monotonic())
            if wait:
                await asyncio.sleep(wait)
            _next_request = time.monotonic() + 0.6
        response = await client.get(BASE + path, params=params)
        if response.status_code == 429:
            await asyncio.sleep(1)
            continue
        response.raise_for_status()
        return response.json()
    response.raise_for_status()
    raise RuntimeError("Polymarket rate limit persisted after retries")


async def _all_events(client: httpx.AsyncClient) -> list[dict]:
    global _event_cache
    if _event_cache and time.monotonic()-_event_cache[0] < 300:
        return _event_cache[1]
    events = []
    for offset in range(0, 500, 100):
        page = (await _get(client, "/v2/leagues/nfl/events", {"limit": 100, "offset": offset})).get("events") or []
        events.extend(page)
        if len(page) < 100:
            break
    _event_cache = (time.monotonic(), events)
    return events


async def get_week_markets(games: list[dict], quantity: float = 100, refresh: bool = False) -> dict:
    key = (tuple(g["game_id"] for g in games), round(quantity, 2))
    if not refresh and key in _cache and time.monotonic()-_cache[key][0] < 45:
        return _cache[key][1]
    now = datetime.now(timezone.utc)
    eligible = [g for g in games if _date(g["kickoff"]) > now]
    if not eligible:
        return {"markets": {}, "fetched_at": now.isoformat(), "error": None}
    async with httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_connections=12)) as client:
        try:
            events = await _all_events(client)
        except Exception as exc:
            return {"markets": {}, "fetched_at": now.isoformat(), "error": f"Polymarket API unavailable: {exc}"}
        matched = {g["game_id"]: match_event(g, events) for g in eligible}
        semaphore = asyncio.Semaphore(2)
        async def load(game_id: str, match: dict):
            async with semaphore:
                slug = match["market"]["slug"]
                try:
                    if not refresh and slug in _book_cache and time.monotonic()-_book_cache[slug][0] < 45:
                        book = _book_cache[slug][1]
                    else:
                        book = await _get(client, f"/v1/markets/{slug}/book")
                        _book_cache[slug] = (time.monotonic(), book)
                    return game_id, _quote(match, book.get("marketData") or {}, quantity, now)
                except Exception as exc:
                    if slug in _book_cache:
                        cached = _quote(match, _book_cache[slug][1].get("marketData") or {}, quantity, now)
                        cached["error"] = f"Last quote retained after refresh failure: {exc}"
                        cached["status"] = "STALE"
                        for outcome in cached["outcomes"].values():
                            outcome["state"] = "STALE"
                        return game_id, cached
                    reference = _reference_quote(match, now)
                    reference["error"] = f"Order book unavailable: {exc}"
                    return game_id, reference
        pairs = await asyncio.gather(*(load(gid, m) for gid, m in matched.items() if m))
    result = {"markets": dict(pairs), "fetched_at": now.isoformat(), "error": None}
    _cache[key] = (time.monotonic(), result)
    return result


def _reference_quote(match: dict, now: datetime) -> dict:
    """Display exchange reference BBO when depth is temporarily unavailable.

    No executable edge is calculated until a current order book is fetched.
    """
    market, event = match["market"], match["event"]
    bid, ask = _amount(market.get("bestBidQuote")), _amount(market.get("bestAskQuote"))
    mid = (bid+ask)/2 if bid is not None and ask is not None else None
    outcomes = {}
    for team, side in match["sides"].items():
        long = bool(side["long"])
        outcomes[team] = {"team": team, "long": long, "bid": bid if long else (1-ask if ask is not None else None),
                          "ask": ask if long else (1-bid if bid is not None else None),
                          "display_price": mid if long else (1-mid if mid is not None else None),
                          "last_trade": None, "execution": {"fillable": False, "vwap": None, "available": 0},
                          "fee_coefficient": market.get("feeCoefficient"), "state": "REFERENCE_ONLY"}
    return {"event_id": event.get("id"), "market_id": market.get("id"), "slug": market.get("slug"),
            "status": "REFERENCE_ONLY", "volume_contracts": None,
            "updated_at": market.get("updatedAt") or now.isoformat(), "fetched_at": now.isoformat(),
            "outcomes": outcomes, "settlement": "Full game including overtime; tie settles at $0.50 per side; rescheduling rules apply."}


def _quote(match: dict, book: dict, quantity: float, now: datetime) -> dict:
    market, event = match["market"], match["event"]
    bids = [_amount(x.get("px")) for x in book.get("bids") or []]
    asks = [_amount(x.get("px")) for x in book.get("offers") or []]
    bid = max((x for x in bids if x is not None), default=None)
    ask = min((x for x in asks if x is not None), default=None)
    stats = book.get("stats") or {}
    current, last = _amount(stats.get("currentPx")), _amount(stats.get("lastTradePx"))
    theta = market.get("feeCoefficient")
    try:
        theta = float(theta)
        if not 0 < theta < 1:
            theta = None
    except (TypeError, ValueError):
        theta = None
    outcomes = {}
    for team, side in match["sides"].items():
        long = bool(side["long"])
        outcomes[team] = {"team": team, "long": long, "bid": bid if long else (1-ask if ask is not None else None),
                          "ask": ask if long else (1-bid if bid is not None else None),
                          "display_price": current if long else (1-current if current is not None else None),
                          "last_trade": last if long else (1-last if last is not None else None),
                          "execution": execution(book, long, quantity), "fee_coefficient": theta,
                          "state": book.get("state")}
    return {"event_id": event.get("id"), "market_id": market.get("id"), "slug": market.get("slug"),
            "status": book.get("state"), "volume_contracts": stats.get("sharesTraded"),
            "updated_at": book.get("transactTime") or (stats.get("lastPriceSample") or {}).get("ts") or now.isoformat(),
            "fetched_at": now.isoformat(), "outcomes": outcomes,
            "settlement": "Full game including overtime; tie settles at $0.50 per side; rescheduling rules apply."}


def _total_quote(match: dict, now: datetime, book: dict | None = None, quantity: float = 100) -> dict:
    quote = _quote(match, book, quantity, now) if book is not None else _reference_quote(match, now)
    quote["line"] = match["line"]
    quote["settlement"] = "Combined points, full game including overtime. Half-point line; no score push. Rescheduling rules apply."
    return quote


async def _event_detail(client: httpx.AsyncClient, event_id: str, refresh: bool = False) -> dict:
    if not refresh and event_id in _detail_cache and time.monotonic()-_detail_cache[event_id][0] < 45:
        return _detail_cache[event_id][1]
    detail = (await _get(client, f"/v1/events/{event_id}")).get("event") or {}
    _detail_cache[event_id] = (time.monotonic(), detail)
    return detail


async def get_week_totals(games: list[dict], quantity: float = 100, refresh: bool = False) -> dict:
    key = (tuple(g["game_id"] for g in games), round(quantity, 2))
    if not refresh and key in _total_cache and time.monotonic()-_total_cache[key][0] < 45:
        return _total_cache[key][1]
    now = datetime.now(timezone.utc)
    eligible = [g for g in games if _date(g["kickoff"]) > now]
    if not eligible:
        return {"totals": {}, "fetched_at": now.isoformat(), "error": None}
    async with httpx.AsyncClient(timeout=25, limits=httpx.Limits(max_connections=8)) as client:
        try:
            events = await _all_events(client)
        except Exception as exc:
            return {"totals": {}, "fetched_at": now.isoformat(), "error": f"Polymarket totals unavailable: {exc}"}
        semaphore = asyncio.Semaphore(3)

        async def load(game: dict):
            event = next((e for e in events if
                          _date(e.get("startTime") or e.get("startDate")) and
                          abs((_date(e.get("startTime") or e.get("startDate"))-_date(game["kickoff"])).total_seconds()) <= 3600 and
                          {canonical(t.get("abbreviation")) for t in e.get("teams") or []} == {game["home"], game["away"]}), None)
            if not event:
                return game["game_id"], None
            async with semaphore:
                try:
                    detail = await _event_detail(client, str(event["id"]), refresh)
                    matches = match_total_markets(game, detail)
                    if not matches:
                        return game["game_id"], None
                    lines = [_total_quote(m, now) for m in matches]
                    def distance(quote):
                        over = quote["outcomes"]["over"].get("display_price")
                        return abs(over-0.5) if over is not None else float("inf")
                    selected = min(lines, key=distance)
                    if distance(selected) == float("inf"):
                        selected = lines[len(lines)//2]
                    slug = selected["slug"]
                    match = next(m for m in matches if m["market"]["slug"] == slug)
                    try:
                        if not refresh and slug in _book_cache and time.monotonic()-_book_cache[slug][0] < 45:
                            book = _book_cache[slug][1]
                        else:
                            book = await _get(client, f"/v1/markets/{slug}/book")
                            _book_cache[slug] = (time.monotonic(), book)
                        selected = _total_quote(match, now, book.get("marketData") or {}, quantity)
                    except Exception as exc:
                        selected["error"] = f"Order book unavailable: {exc}"
                    lines = [selected if q["slug"] == slug else q for q in lines]
                    return game["game_id"], {"featured_line": selected["line"], "featured": selected,
                                              "lines": lines, "line_count": len(lines)}
                except Exception as exc:
                    return game["game_id"], {"featured": None, "lines": [], "line_count": 0, "error": str(exc)}

        pairs = await asyncio.gather(*(load(g) for g in eligible))
    result = {"totals": {gid: q for gid, q in pairs if q is not None}, "fetched_at": now.isoformat(), "error": None}
    _total_cache[key] = (time.monotonic(), result)
    return result
