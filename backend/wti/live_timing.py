"""Compare Oil D live-entry timing and historical quote availability without trading."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal, InvalidOperation
import json
from math import ceil, isfinite
from pathlib import Path
import sqlite3
from statistics import median
import time

from .commodity import WTI
from .live_books import EXCHANGE_MAX_AGE, EXIT_BUFFER, SIGNAL_TTL, SUBMISSION_BUFFER, usable
from .live_store import TERMINAL_STATES, live_path
from .shadow import StreamQuotes, decode_window, recorded_book
from .tape import tape_path


PHASES = {
    "signal_pickup": ("ready_at", "attempt_started_at"),
    "account_and_contract_checks": ("attempt_started_at", "checks_completed_at"),
    "book_search": ("book_search_started_at", "book_search_finished_at"),
    "quote_to_decision": ("quote_selected_at", "decision_at"),
    "decision_to_submission": ("decision_at", "submit_started_at"),
    "entry_time_left_at_submission": ("submit_started_at", "deadline"),
    "submission_to_acknowledgment": ("submit_started_at", "acknowledged_at"),
    "submission_to_finish": ("submit_started_at", "attempt_finished_at"),
    "total_attempt": ("attempt_started_at", "attempt_finished_at"),
}
PROBES = {
    "checks_completed": "checks_completed_at",
    "quote_selected": "quote_selected_at",
    "decision": "decision_at",
    "submission": "submit_started_at",
}
PAIRS = ("rest_only", "stream_only", "both", "neither")


def readonly(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1))


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        return None
    try:
        parsed = float(value)
        return parsed if isfinite(parsed) else None
    except (ValueError, OverflowError):
        return None


def as_decimal(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(ordered), "median": round(median(ordered), 4) if ordered else None,
            "p95": round(ordered[ceil(.95 * len(ordered)) - 1], 4) if ordered else None}


def elapsed(row, first, second, *, allow_negative=False):
    a, b = number(row.get(first)), number(row.get(second))
    if a is None or b is None or (not allow_negative and b < a):
        return None
    return b - a


def attempts(db_path, since, as_of):
    if not db_path.is_file():
        return []
    with readonly(db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_timings' AND type='table'").fetchone():
            return []
        rows = db.execute("SELECT payload FROM entry_timings WHERE at BETWEEN ? AND ? ORDER BY at,id",
                          (since, as_of))
        return [data for row in rows if isinstance(data := json.loads(row[0]), dict)]


def probe_times(entries):
    probes = defaultdict(list)
    for entry in entries:
        slug = entry.get("slug")
        ready, deadline = number(entry.get("ready_at")), number(entry.get("deadline"))
        if not isinstance(slug, str) or ready is None or deadline is None:
            continue
        for name, key in PROBES.items():
            at = number(entry.get(key))
            if at is not None and ready <= at <= min(deadline, ready + SIGNAL_TTL):
                probes[slug].append((at, name, ready, entry))
    return probes


def recorded_depth(book, side, price):
    return sum((Decimal(str(size)) for px, size in book.side_asks(side) if Decimal(str(px)) <= price), Decimal(0))


def classify(rest, stream):
    return ("both" if stream else "rest_only") if rest else ("stream_only" if stream else "neither")


def entry_exchange_age_limit(entry):
    age = number(entry.get("max_exchange_age_seconds"))
    return age if age is not None and age > 0 else EXCHANGE_MAX_AGE


def evaluate_probe(counts, rest, stream, at, ready, entry):
    age = entry_exchange_age_limit(entry)
    rest_ok = rest is not None and usable(rest, at, ready, exchange_max_age=age)
    stream_ok = stream is not None and usable(stream, at, ready, exchange_max_age=age)
    counts["fresh"][classify(rest_ok, stream_ok)] += 1
    side = entry.get("side")
    price = as_decimal(entry.get("limit"))
    minimum = as_decimal(entry.get("minimum_quantity"))
    share = as_decimal(entry.get("depth_share"))
    if (side in ("UP", "DOWN") and price is not None and 0 < price < 1
            and minimum is not None and minimum > 0 and share is not None and 0 < share <= 1):
        counts["fresh_with_minimum_depth"][classify(
            rest_ok and recorded_depth(rest, side, price) * share >= minimum,
            stream_ok and recorded_depth(stream, side, price) * share >= minimum,
        )] += 1


def compare_recorded_quotes(path, entries):
    coverage = {name: {metric: dict.fromkeys(PAIRS, 0)
                       for metric in ("fresh", "fresh_with_minimum_depth")} for name in PROBES}
    market_count = 0
    if not path.is_file():
        return coverage, market_count
    with readonly(path) as db:
        db.execute("BEGIN")
        for slug, probes in probe_times(entries).items():
            row = db.execute("SELECT slug,start,end,window FROM markets WHERE slug=?", (slug,)).fetchone()
            window = decode_window(row) if row else None
            if not window:
                continue
            probes = [probe for probe in probes
                      if window.start <= probe[2] <= probe[0] < window.end - EXIT_BUFFER - SUBMISSION_BUFFER]
            if not probes:
                continue
            market_count += 1
            upper = min(window.end, max(at for at, _, _, _ in probes))
            frames = iter(db.execute("""SELECT at,source,kind,payload FROM tape
                WHERE slug=? AND source IN ('rest','stream') AND at BETWEEN ? AND ? ORDER BY at,id""",
                (slug, window.start, upper)))
            following = next(frames, None)
            age_limits = {entry_exchange_age_limit(entry) for _, _, _, entry in probes}
            streams = {age: StreamQuotes(exchange_max_age=age) for age in age_limits}
            rest = None
            for at, name, ready, entry in sorted(probes, key=lambda probe: probe[0]):
                while following and following[0] <= at:
                    stamp, source, kind, raw = following
                    payload = json.loads(raw)
                    if source == "stream":
                        for stream in streams.values():
                            stream.update(stamp, kind, payload)
                    else:
                        rest = recorded_book(stamp, payload) if kind == "book" else None
                    following = next(frames, None)
                stream = streams[entry_exchange_age_limit(entry)]
                evaluate_probe(coverage[name], rest, stream.quote, at, ready, entry)
    return coverage, market_count


def quote_observations(entries):
    sources = defaultdict(lambda: {
        "results": Counter(), "reasons": Counter(), "http_status": Counter(), "stages": Counter(),
    })
    timings = defaultdict(lambda: defaultdict(list))
    ages = {
        "receipt_age": ("received_at", "checked_at"),
        "exchange_age": ("exchange_at", "checked_at"),
        "request_after_ready": ("ready_at", "requested_at"),
        "request_to_receipt": ("requested_at", "received_at"),
    }
    for entry in entries:
        for observation in entry.get("quotes") or ():
            if not isinstance(observation, dict):
                continue
            source = str(observation.get("source") or "unknown")
            for field, key in (("result", "results"), ("reason", "reasons"), ("stage", "stages"),
                               ("http_status", "http_status")):
                value = observation.get(field)
                if value is not None and not (field == "http_status" and observation.get("reason") == "direct_cooldown"):
                    sources[source][key][str(value)] += 1
            checked_at = number(observation.get("checked_at"))
            observed = {**observation, "checked_at": checked_at if checked_at is not None
                        else number(observation.get("observed_at"))}
            for name, (start, end) in ages.items():
                age = elapsed(observed, start, end, allow_negative=True)
                if age is not None:
                    timings[source][name].append(age)
            wait = number(observation.get("wait_seconds"))
            if wait is not None and wait >= 0:
                timings[source]["wait"].append(wait)
    return {source: {**counts, "seconds": {k: distribution(v) for k, v in timings[source].items()}}
            for source, counts in sources.items()}


def initial_responses(entries):
    counts = dict.fromkeys(("with_fills", "terminal_no_fill", "pending", "unconfirmed"), 0)
    for entry in entries:
        if number(entry.get("submit_started_at")) is None:
            continue
        filled = as_decimal(entry.get("filled"))
        if entry.get("acknowledged") is not True or filled is None:
            counts["unconfirmed"] += 1
        elif filled > 0:
            counts["with_fills"] += 1
        elif entry.get("order_state") in TERMINAL_STATES:
            counts["terminal_no_fill"] += 1
        else:
            counts["pending"] += 1
    return counts


def report(journal, tape, since, as_of, *, include_attempts=False):
    entries = attempts(journal, since, as_of)
    phases = {}
    for phase, (start, end) in PHASES.items():
        durations = []
        for entry in entries:
            finish = "quote_selected_at" if end == "book_search_finished_at" and end not in entry else end
            duration = elapsed(entry, start, finish)
            if duration is not None:
                durations.append(duration)
        phases[phase] = distribution(durations)
    coverage, markets = compare_recorded_quotes(tape, entries)
    return {
        "since": since,
        "as_of": as_of,
        "attempts": len(entries),
        "exchange_age_limits_seconds": Counter(f"{entry_exchange_age_limit(entry):g}" for entry in entries),
        "signals": len({(entry["slug"], signal_at) for entry in entries if isinstance(entry.get("slug"), str)
                        and (signal_at := number(entry.get("signal_at"))) is not None}),
        "outcomes": Counter(str(entry.get("outcome") or "unknown") for entry in entries),
        "reasons": Counter(str(entry["reason"]) for entry in entries if entry.get("reason")),
        "order_states": Counter(str(entry["order_state"]) for entry in entries if entry.get("order_state")),
        "submission_quote_sources": Counter(str(entry["final_quote_source"]) for entry in entries
                                          if entry.get("final_quote_source") and entry.get("submit_started_at") is not None),
        "acknowledged": sum(entry.get("acknowledged") is True for entry in entries),
        "initial_responses": initial_responses(entries),
        "phases_seconds": phases,
        "quotes": quote_observations(entries),
        "recorded_coverage": {"markets": markets, "probes": coverage},
        "coverage_note": "The tape comparison uses recorded receipt times and each attempt's exchange-age limit. "
                         "Stream writes may be buffered. "
                         "Direct live REST reads are counted separately. Minimum displayed depth measures "
                         "visible liquidity at the limit.",
        **({"attempt_details": entries} if include_attempts else {}),
    }


def format_counts(counts):
    return ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in counts.items()) or "—"


def print_report(result, hours):
    attempts, signals = result["attempts"], result["signals"]
    print(f"Oil D entry timing | {hours}h | {attempts} attempt{'s' if attempts != 1 else ''} | "
          f"{signals} signal{'s' if signals != 1 else ''}")
    if not result["attempts"]:
        print("No entry timing has been recorded for this period.")
        return
    print("Exchange timestamp limits | " + ", ".join(f"{age}s: {total} attempts" for age, total in
                                                       result["exchange_age_limits_seconds"].items()))
    print(f"Outcomes | {format_counts(result['outcomes'])}")
    for reason, total in result["reasons"].most_common():
        print(f"  {total}× {reason}")
    if result["order_states"]:
        print(f"Initial order states | {format_counts(result['order_states'])}")
    if result["submission_quote_sources"]:
        print(f"Quote source at submission | {format_counts(result['submission_quote_sources'])}")
    if sum(result["initial_responses"].values()):
        print(f"Initial responses | {result['acknowledged']} acknowledged | "
              f"{format_counts(result['initial_responses'])}")
        print("Pending or unconfirmed responses can change during reconciliation.")
    for phase, stats in result["phases_seconds"].items():
        if stats["samples"]:
            print(f"{phase.replace('_', ' ')} | median {stats['median']:.3f}s | "
                  f"p95 {stats['p95']:.3f}s | n={stats['samples']}")
    for source, counts in result["quotes"].items():
        status = ", ".join(f"HTTP {code} ×{count}" for code, count in counts["http_status"].items())
        print(f"{source} | {format_counts(counts['reasons'] or counts['results'])}"
              f"{' | ' + status if status else ''}")
        for metric, stats in counts["seconds"].items():
            print(f"  {metric.replace('_', ' ')} | median {stats['median']:.3f}s | "
                  f"p95 {stats['p95']:.3f}s | n={stats['samples']}")
    if not result["recorded_coverage"]["markets"]:
        print("No recorded market quotes to compare at the logged times.")
        return
    print(result["coverage_note"])
    for name, counts in result["recorded_coverage"]["probes"].items():
        if sum(counts["fresh"].values()):
            print(f"{name.replace('_', ' ')} fresh | {format_counts(counts['fresh'])}")
        if sum(counts["fresh_with_minimum_depth"].values()):
            print(f"  with minimum depth | {format_counts(counts['fresh_with_minimum_depth'])}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--attempts", action="store_true", help="include individual persisted attempts in JSON")
    parser.add_argument("--journal", type=Path, default=live_path(WTI.directory()))
    parser.add_argument("--tape", type=Path, default=tape_path(WTI.directory()))
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    if args.attempts and not args.json:
        parser.error("--attempts requires --json")
    now = time.time()
    result = report(args.journal, args.tape, now - args.hours * 3600, now, include_attempts=args.attempts)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result, args.hours)


if __name__ == "__main__":
    main()
