"""Replay the recorded REST and stream entry opportunities for Oil D live signals."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
import json
from pathlib import Path
import time

from .commodity import WTI
from .live_books import EXIT_BUFFER, SIGNAL_TTL, SUBMISSION_BUFFER, usable
from .live_store import live_path
from .live_timing import (PAIRS, as_decimal, attempts, classify, distribution, entry_exchange_age_limit,
                          format_counts, number, readonly, recorded_depth)
from .replay_capture import StreamBookSource
from .shadow import decode_window, recorded_book
from .tape import tape_path


SOURCES = ("rest", "stream")
PRE_QUOTE_BLOCKS = frozenset(("account_has_exposure", "budget_below_minimum", "invalid_limit"))
# When several books fail for different reasons, report the closest approach.
PROGRESS = {reason: index for index, reason in enumerate((
    "no_book", "market_not_open", "stale", "price_above_limit", "insufficient_depth", "eligible",
))}


@dataclass(frozen=True)
class Entry:
    checks_at: float
    cutoff: float
    ready_at: float
    side: str
    limit: Decimal
    minimum: Decimal
    share: Decimal
    exchange_age_limit: float


@dataclass(frozen=True)
class Opportunity:
    outcome: str
    at: float | None = None
    seconds_left: float | None = None
    seconds_since_checks: float | None = None

    def __bool__(self):
        return self.at is not None


def entry_from_timing(row, window, as_of):
    if row.get("reason") in PRE_QUOTE_BLOCKS:
        return None, row["reason"]
    ready = number(row.get("ready_at"))
    checks = number(row.get("checks_completed_at"))
    deadline = number(row.get("deadline"))
    if checks is None:
        return None, "no_account_checks_completed"
    if ready is None or deadline is None or not window.start <= ready <= checks:
        return None, "invalid_timing"
    cutoff = min(deadline, ready + SIGNAL_TTL, window.end - EXIT_BUFFER - SUBMISSION_BUFFER)
    if checks >= cutoff:
        return None, "checks_completed_at_or_after_cutoff"
    limit = as_decimal(row.get("limit"))
    minimum = as_decimal(row.get("minimum_quantity"))
    share = as_decimal(row.get("depth_share"))
    side = row.get("side")
    if (side not in ("UP", "DOWN") or limit is None or not 0 < limit < 1
            or minimum is None or minimum <= 0 or share is None or not 0 < share <= 1):
        return None, "missing_or_invalid_contract_terms"
    max_quantity = as_decimal(row.get("max_order_quantity"))
    if max_quantity is not None and max_quantity < minimum:
        return None, "budget_below_minimum"
    if cutoff > as_of:
        return None, "cutoff_not_reached"
    return Entry(checks, cutoff, ready, side, limit, minimum, share, entry_exchange_age_limit(row)), None


def choose(first, second):
    if first and second:
        return first if first.at <= second.at else second
    return second if PROGRESS[second.outcome] > PROGRESS[first.outcome] else first


def inspect(entry, book, unavailable, start, stop):
    if book is None:
        return Opportunity(unavailable)
    # A timestamp initially ahead of the allowed venue-clock lead can become
    # eligible while the same REST book remains current.
    at = max(start, book.exchange_at - 2) if book.exchange_at is not None else start
    if at >= stop or not usable(book, at, entry.ready_at, exchange_max_age=entry.exchange_age_limit):
        return Opportunity("stale")
    asks = book.side_asks(entry.side)
    if asks and min(Decimal(str(price)) for price, _ in asks) > entry.limit:
        return Opportunity("price_above_limit")
    if entry.share * recorded_depth(book, entry.side, entry.limit) < entry.minimum:
        return Opportunity("insufficient_depth")
    return Opportunity("eligible", at, entry.cutoff - at, at - entry.checks_at)


def evaluate(entry, window, source, frames):
    stream = None
    at = window.start
    if source == "stream":
        stream = StreamBookSource(clock=lambda: at)
        generation = stream.begin(window)
        stream.set_connected(generation, True)
    best = Opportunity("no_book")
    book = None
    unavailable = "no_book"
    start = entry.checks_at
    for at, batch in groupby(frames, key=lambda frame: frame[0]):
        if at >= entry.cutoff:
            break
        if start < at:
            best = choose(best, inspect(entry, book, unavailable, start, at))
            if best:
                return best
            start = at
        for _, kind, payload in batch:
            non_open = kind == "closed"
            recorded = recorded_book(at, payload) if kind == "book" else None
            if stream is None:
                book = recorded
            else:
                stream.observe(generation, (window.slug, at, source, kind, payload))
                book, non_open = stream.snapshot(window.slug)
            if book is None:
                if non_open:
                    unavailable = "market_not_open"
                elif recorded is not None and not usable(recorded, at, exchange_max_age=entry.exchange_age_limit):
                    unavailable = "stale"
                else:
                    unavailable = "no_book"
    return choose(best, inspect(entry, book, unavailable, start, entry.cutoff))


def get_frames(db, window, cutoff):
    frames = {source: [] for source in SOURCES}
    rows = db.execute("""SELECT at,source,kind,payload FROM tape
        WHERE slug=? AND source IN ('rest','stream') AND at>=? AND at<? ORDER BY at,id""",
        (window.slug, window.start, cutoff))
    for at, source, kind, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        frames[source].append((at, kind, payload) if isinstance(payload, dict) else (at, "gap", {}))
    return frames


def signal_results(db, slug, signals, as_of):
    market = db.execute("SELECT slug,start,end,window FROM markets WHERE slug=?", (slug,)).fetchone() if db else None
    window = decode_window(market) if market else None
    if window is None:
        return (), Counter({"market_not_recorded": len(signals)})
    entries_by_signal = {}
    unavailable = Counter()
    for signal_at, timings in signals.items():
        entries, reasons = [], []
        for timing in timings:
            entry, reason = entry_from_timing(timing, window, as_of)
            if entry is not None:
                entries.append(entry)
            else:
                reasons.append(reason)
        if entries:
            entries_by_signal[signal_at] = entries
        else:
            unavailable[reasons[-1]] += 1
    if not entries_by_signal:
        return (), unavailable
    cutoff = max(entry.cutoff for group in entries_by_signal.values() for entry in group)
    frames = get_frames(db, window, cutoff)
    results = []
    for signal_at, entries in entries_by_signal.items():
        source_results = {}
        for source in SOURCES:
            best = Opportunity("no_book")
            for entry in entries:
                best = choose(best, evaluate(entry, window, source, frames[source]))
            source_results[source] = best
        results.append((slug, signal_at, source_results))
    return results, unavailable


def report(journal, tape, since, as_of):
    timings = attempts(journal, since, as_of)
    by_market = defaultdict(lambda: defaultdict(list))
    missing_identity = 0
    for timing in timings:
        slug, signal_at = timing.get("slug"), number(timing.get("signal_at"))
        if not isinstance(slug, str) or not slug or signal_at is None:
            missing_identity += 1
        else:
            by_market[slug][signal_at].append(timing)

    results = []
    unavailable = Counter()
    if tape.is_file():
        with readonly(tape) as db:
            db.execute("BEGIN")
            for slug, signals in by_market.items():
                replayed, skipped = signal_results(db, slug, signals, as_of)
                results.extend(replayed)
                unavailable.update(skipped)
    elif by_market:
        unavailable["market_not_recorded"] = sum(map(len, by_market.values()))

    coverage = dict.fromkeys(PAIRS, 0)
    outcomes = {source: Counter() for source in SOURCES}
    seconds_left = {source: [] for source in SOURCES}
    wait = {source: [] for source in SOURCES}
    details = []
    for slug, signal_at, opportunities in sorted(results, key=lambda result: result[1]):
        pair = classify(opportunities["rest"], opportunities["stream"])
        coverage[pair] += 1
        detail = {"slug": slug, "signal_at": signal_at, "coverage": pair}
        for source, outcome in opportunities.items():
            outcomes[source][outcome.outcome] += 1
            detail[source] = {"outcome": outcome.outcome}
            if outcome:
                seconds_left[source].append(outcome.seconds_left)
                wait[source].append(outcome.seconds_since_checks)
                detail[source].update(earliest_eligible_at=outcome.at, seconds_left=round(outcome.seconds_left, 4),
                                      seconds_since_checks=round(outcome.seconds_since_checks, 4))
        details.append(detail)
    return {
        "since": since, "as_of": as_of, "attempts": len(timings), "attempts_missing_identity": missing_identity,
        "signals": len(results) + sum(unavailable.values()), "evaluated_signals": len(results),
        "unassessable_signals": unavailable, "coverage": coverage,
        "sources": {source: {"outcomes": outcomes[source], "seconds_left_at_first_eligible": distribution(seconds_left[source]),
                             "seconds_from_checks_to_first_eligible": distribution(wait[source])} for source in SOURCES},
        "signal_details": details,
        "measurement": "Recorded quotes from account-check completion through the entry cutoff. This measures "
                       "displayed liquidity at the signal limit using each attempt's freshness and minimum size rules. "
                       "Confirmed account and pilot sizing blocks are counted separately. The replay excludes direct "
                       "live REST reads, order arrival, and actual fills. Stream receipts may be written in batches.",
    }


def print_report(result, hours):
    print(f"Oil D entry opportunity replay | {hours}h | {result['attempts']} attempts | {result['signals']} signals")
    if result["unassessable_signals"]:
        print(f"Unavailable for comparison | {format_counts(result['unassessable_signals'])}")
    if not result["evaluated_signals"]:
        print("No completed signals with account-check timing and contract terms to replay.")
        return
    print(f"{result['evaluated_signals']} comparable signals | {format_counts(result['coverage'])}")
    for source, stats in result["sources"].items():
        print(f"{source} | {format_counts(stats['outcomes'])}")
        left = stats["seconds_left_at_first_eligible"]
        if left["samples"]:
            delay = stats["seconds_from_checks_to_first_eligible"]
            print(f"  Median first opportunity {delay['median']:.3f}s after checks, with {left['median']:.3f}s left")
    print(result["measurement"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="print the summary and per-signal details as JSON")
    parser.add_argument("--journal", type=Path, default=live_path(WTI.directory()))
    parser.add_argument("--tape", type=Path, default=tape_path(WTI.directory()))
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    now = time.time()
    result = report(args.journal, args.tape, now - args.hours * 3600, now)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result, args.hours)


if __name__ == "__main__":
    main()
