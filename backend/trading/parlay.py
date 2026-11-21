"""Weekly informational NFL parlay, built from the two pregame models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter
import math

from ..data import schedules
from ..forecast_feed import read_predictions, schedule_status
from ..mc_archive import get_forecasts
from ..model import PREDICTIONS
from ..monte_carlo import total_probabilities, week_cutoff
from ..totals import PREDICTIONS as TOTAL_PREDICTIONS, over_probability
from .common import iso, utc
from .parlay_odds import schedule_prices

MIN_SUPPORT = 0.55
PROFILES = (("Anchor", 2), ("Changeup", 2), ("Triple", 3), ("Fourfold", 4), ("Long shot", 5))
TICKETS_PER_WEEK = len(PROFILES)
INACTIVE = {"cancelled", "canceled", "postponed", "suspended", "live", "in progress", "final", "completed"}


def upcoming(game, now):
    return (game["game_type"] == "REG" and game.get("kickoff_confirmed") and utc(game["kickoff"]) > now
            and game.get("home_score") is None and game.get("away_score") is None
            and str(game.get("source_status") or "").lower() not in INACTIVE)


def load_models(games):
    season = games[0]["season"]
    ml, _ = read_predictions(PREDICTIONS)
    totals, _ = read_predictions(TOTAL_PREDICTIONS)
    schedule = schedules()
    if schedule_status(datetime.now(timezone.utc))["schedule_stale"]:
        return {}, {}, {}, {}, {}
    # The NFLverse reference line is used only to define the over/under leg.
    lines = dict(schedule.select("game_id", "total_line").iter_rows()) if "total_line" in schedule.columns else {}
    mc = get_forecasts(season, games)
    return ml, totals, mc, lines, schedule_prices(schedule, (game["game_id"] for game in games))


def _pregame(pred, game, now):
    return (pred.get("kind") == "live_pregame" and pred.get("generated_at") and
            week_cutoff(utc(game["kickoff"])) <= utc(pred["generated_at"]) <= now)


def eligible_legs(games, ml, totals, forecasts, lines, now, prices=None):
    candidates = []
    for game in games:
        if not upcoming(game, now):
            continue
        gid = game["game_id"]
        mc = forecasts.get(gid, {})
        if (mc.get("status") != "available" or mc.get("forecast_origin") != "archived"
                or mc.get("kickoff") != game["kickoff"]):
            continue

        def add(market, selection, gb_p, mc_p, line=None):
            if not all(isinstance(p, (int, float)) and MIN_SUPPORT <= p <= 1 for p in (gb_p, mc_p)):
                return
            leg = {"game_id": gid, "matchup": f"{game['away']} at {game['home']}",
                   "kickoff": game["kickoff"], "market": market, "selection": selection,
                   "line": line, "gradient_probability": gb_p, "mc_probability": mc_p}
            quote = (prices or {}).get((gid, market, selection, line))
            if quote:
                leg["odds"] = quote
            candidates.append(leg)

        pred = ml.get(gid, {})
        if _pregame(pred, game, now):
            home, away = mc["probability_home_win"], mc["probability_away_win"]
            # The gradient model trains on games with winners. Compare the two
            # moneyline estimates on the same conditional-on-no-tie basis.
            if home + away > 0 and pred.get("home_probability") is not None:
                gb = pred["home_probability"]
                add("moneyline", game["home"], gb, home / (home + away))
                add("moneyline", game["away"], 1 - gb, away / (home + away))
        line, total = lines.get(gid), totals.get(gid, {})
        if (isinstance(line, (int, float)) and math.isfinite(line) and line > 0 and line % 1 == .5
                and _pregame(total, game, now)):
            gb = over_probability(total.get("predicted_total"), line)
            mc_total = total_probabilities(mc, line)
            if gb is not None and mc_total:
                add("total", "Over", gb, mc_total["over"], line)
                add("total", "Under", 1 - gb, mc_total["under"], line)

    # Rank by the weaker model. Break ties deterministically across restarts.
    candidates.sort(key=lambda leg: (-min(leg["gradient_probability"], leg["mc_probability"]),
                                     leg["kickoff"], leg["game_id"], leg["market"], leg["selection"]))
    return candidates


def rate_ticket(legs, slot=1, recorded_at=None):
    """Use the lower forecast per leg. Cross-game independence is an approximation."""
    support = [min(leg["gradient_probability"], leg["mc_probability"]) for leg in legs]
    hit = math.prod(support)
    profile, target_size = PROFILES[slot - 1]
    quotes = [leg.get("odds") for leg in legs]
    decimal = math.prod(quote["decimal"] for quote in quotes) if all(quotes) else None
    american = None
    if decimal:
        american = round(100 * (decimal - 1) if decimal >= 2 else -100 / (decimal - 1))
    return {"slot": slot, "profile": profile if len(legs) == target_size else f"Original {slot}",
            "recorded_at": recorded_at, "legs": list(legs), "estimated_hit_probability": hit,
            "gradient_hit_probability": math.prod(leg["gradient_probability"] for leg in legs),
            "mc_hit_probability": math.prod(leg["mc_probability"] for leg in legs),
            "reference_decimal_odds": decimal, "reference_american_odds": american,
            "market_implied_probability": (1 / decimal if decimal else None),
            "risk_score": round(1 + 9 * (1 - hit), 1),
            # A coin-flip weakest leg scores 1, unanimous certainty scores 10.
            "overall_rating": round(1 + 18 * (min(support) - .5), 1)}


def ticket_key(ticket):
    # A quote moving from 42.5 to 43.5 does not make a new pick.
    return tuple(sorted((leg["game_id"], leg["market"], leg["selection"]) for leg in ticket["legs"]))


def choose_ticket(slot, candidates, other_tickets, recorded_at):
    """Pick one leg per game, rotating game exposure across the five profiles."""
    size = PROFILES[slot - 1][1]
    if len({leg["game_id"] for leg in candidates}) < size:
        return None
    exposure = Counter(leg["game_id"] for t in other_tickets for leg in t["legs"])
    used = {ticket_key(t) for t in other_tickets}

    def rank(leg):
        support = min(leg["gradient_probability"], leg["mc_probability"])
        price = (leg.get("odds") or {}).get("decimal", 0)
        # Anchor favours model agreement. Changeup seeks other games, then strength.
        # The longer profiles prefer fully priced tickets and a larger reference return.
        if slot <= 2:
            return (exposure[leg["game_id"]] if slot == 2 else 0, -support, -price)
        if slot == TICKETS_PER_WEEK:
            return (not bool(price), -price, exposure[leg["game_id"]], -support)
        return (not bool(price), exposure[leg["game_id"]], -price, -support)

    ranked = sorted(candidates, key=rank)
    for first in ranked:
        legs = [first]
        games = {first["game_id"]}
        for leg in ranked:
            if leg["game_id"] in games:
                continue
            if len(legs) + 1 == size:
                ticket = rate_ticket([*legs, leg], slot, recorded_at)
                if ticket_key(ticket) not in used:
                    return ticket
                continue
            legs.append(leg)
            games.add(leg["game_id"])
    return None


def select_legs(games, ml, totals, forecasts, lines, now):
    """First ticket for existing callers that consume a single pair."""
    candidates = eligible_legs(games, ml, totals, forecasts, lines, now)
    ticket = choose_ticket(1, candidates, [], iso(now))
    return ticket["legs"] if ticket else []


def pick_text(leg):
    if leg["market"] == "total":
        pick = f"{leg['selection']} {leg['line']:g}"
    else:
        pick = f"{leg['selection']} to win"
    odds = leg.get("odds")
    price = f"{odds['american']:+d} ({odds['decimal']:.2f}×)" if odds else "odds unavailable"
    return (f"{leg['matchup']} · **{pick}** · {price} · "
            f"{leg['gradient_probability']:.0%} / {leg['mc_probability']:.0%}")


def parlay_card(season, week, tickets, now, expires_at):
    fields = []
    for number, ticket in enumerate(tickets, 1):
        legs = ticket["legs"]
        kickoff = int(min(utc(leg["kickoff"]) for leg in legs).timestamp())
        profile = ticket.get("profile") or "Original"
        heading = (f"🎟️ {ticket.get('slot', number)}. {profile} · {len(legs)} legs · "
                   f"Rating {ticket['overall_rating']:g}/10 · Risk {ticket['risk_score']:g}/10")
        lines = [pick_text(leg) for leg in legs]
        multiple = ticket.get("reference_decimal_odds")
        if multiple is None:
            count = sum(bool(leg.get("odds")) for leg in legs)
            lines.append(f"**Payout unavailable** · odds available for {count}/{len(legs)} legs")
        else:
            lines.append(f"**{multiple:.2f}× return** ({ticket['reference_american_odds']:+d}) · "
                         f"$10 → ${10 * multiple:,.2f} if all hit · break-even {ticket['market_implied_probability']:.1%}")
        lines.append(f"**Model hit chance {ticket['estimated_hit_probability']:.1%}** · first kickoff <t:{kickoff}:R>")
        if ticket.get("recorded_at") and any(leg.get("odds") for leg in legs):
            snapshot = int(utc(ticket["recorded_at"]).timestamp())
            lines.append(f"Prices read <t:{snapshot}:f>")
        fields.append([heading, "\n".join(lines)])
    if not tickets:
        description = "🥩 Still on the grill. Waiting for two games both models like."
    elif len(tickets) < TICKETS_PER_WEEK:
        description = f"🥩 {len(tickets)} of 5 tickets ready. More can join as games qualify."
    else:
        description = "🥩 Five tickets off the grill. Every leg on a ticket has to hit."
    if tickets:
        description += ("\nEach pick shows American odds (decimal return) and the win chances from "
                        "Gradient / Monte Carlo. Hit chance uses the lower forecast on each leg. "
                        "Ticket returns multiply the NFLverse reference odds "
                        "and include the stake. No sportsbook promo boost is included.")
    return {"title": f"🏈 Beef Kirkey’s parlay board · Week {week}, {season}", "season": season, "week": week,
            "as_of": iso(now), "expires_at": iso(expires_at), "status": "ready" if tickets else "no_pair",
            "legs": tickets[0]["legs"] if tickets else [], "parlays": tickets, "fields": fields,
            "description": description,
            "footer": "Model chance assumes independent games and excludes moneyline ties. Higher risk = likelier to miss."}


class ParlayPublisher:
    def __init__(self, store, load=load_models, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.load, self.clock = store, load, clock
        self.retry_at = None

    def tick(self, rows, now):
        if self.retry_at and now < self.retry_at:
            return
        self.retry_at = now + timedelta(minutes=10)
        future = sorted((g for g in rows if upcoming(g, now)), key=lambda g: g["kickoff"])
        if not future or week_cutoff(utc(future[0]["kickoff"])) > now:
            return
        season, week = future[0]["season"], future[0]["week"]
        games = [g for g in future if (g["season"], g["week"]) == (season, week)]
        key = f"parlay:{season}:{week:02d}"
        previous = self.store.publication(key)
        payload = previous["payload"] if previous else {}
        stored = payload.get("parlays", [])
        if not stored and payload.get("legs"):
            stored = [{"legs": payload["legs"]}]
        by_game = {g["game_id"]: g for g in games}

        def still_pregame(ticket, at):
            return all(leg["game_id"] in by_game and by_game[leg["game_id"]]["kickoff"] == leg["kickoff"]
                       and utc(leg["kickoff"]) > at for leg in ticket["legs"])

        if (len(stored) == TICKETS_PER_WEEK and
                not any(still_pregame(t, now) and
                        (payload.get("board_version") != 2 or t.get("reference_decimal_odds") is None
                         or len(t["legs"]) != PROFILES[t["slot"] - 1][1]) for t in stored)):
            return
        ml, totals, forecasts, lines, *snapshots = self.load(games)
        prices = snapshots[0] if snapshots else {}
        # Loading may take long enough to cross kickoff. Check it again before publication.
        finished = self.clock()
        candidates = eligible_legs(games, ml, totals, forecasts, lines, finished, prices)
        superseded = list(payload.get("superseded_parlays", []))
        by_slot = {}
        for index, saved in enumerate(stored, 1):
            t = (saved if payload.get("board_version") == 2
                 else rate_ticket(saved["legs"], index, payload.get("as_of")))
            by_slot[t["slot"]] = t
        for slot, (_, size) in enumerate(PROFILES, 1):
            old = by_slot.get(slot)
            if old and not still_pregame(old, finished):
                continue
            if not old or len(old["legs"]) != size:
                others = [t for number, t in by_slot.items() if number != slot]
                new = choose_ticket(slot, candidates, others, iso(finished))
                if new:
                    if old:
                        superseded.append(old)
                    by_slot[slot] = new
                    continue
            if old and old["reference_decimal_odds"] is None:
                # Reprice only from one complete pregame snapshot of the exact selections.
                legs = [dict(leg, odds=prices.get((leg["game_id"], leg["market"], leg["selection"], leg["line"])))
                        for leg in old["legs"]]
                if all(leg["odds"] for leg in legs):
                    by_slot[slot] = rate_ticket(legs, slot, iso(finished))
        tickets = [by_slot[slot] for slot in sorted(by_slot)]
        if previous and payload.get("board_version") == 2 and tickets == payload.get("parlays"):
            return
        if tickets:
            # The board is deliverable while at least one ticket can still be followed pregame.
            expires_at = max(min(utc(leg["kickoff"]) for leg in t["legs"]) for t in tickets)
            if expires_at <= finished:
                return
        else:
            same_week = [g for g in rows if g["game_type"] == "REG" and
                         (g["season"], g["week"]) == (season, week) and g.get("kickoff_confirmed")]
            if finished < min(utc(g["kickoff"]) for g in same_week) - timedelta(minutes=30):
                return
            expires_at = max(utc(g["kickoff"]) for g in games)
            if previous or expires_at <= finished:
                return
        card = parlay_card(season, week, tickets, finished, expires_at)
        card["board_version"] = 2
        if payload.get("previous_as_of") or (stored and payload.get("board_version") != 2):
            card["previous_as_of"] = payload.get("previous_as_of") or payload.get("as_of")
        if superseded:
            card["superseded_parlays"] = superseded
            card["description"] += " Earlier two-leg tickets in the longer slots were revised before kickoff."
        self.store.enqueue([{"publication_id": key, "channel": "parlay", "kind": "weekly_parlay",
                             "revision": previous["revision"] + 1 if previous else 1, "payload": card}], finished)
