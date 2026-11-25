"""Read-only comparison of recorded WTI stream and REST quote availability."""
from bisect import bisect_left
from contextlib import closing
import argparse
import json
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
import time

from ..updown.strategy import Book, Window
from .commodity import WTI
from .live_books import EXCHANGE_MAX_AGE, EXIT_BUFFER, SIGNAL_TTL, SUBMISSION_BUFFER, usable
from .tape import tape_path


def decode_window(row):
    """Restrict this observer to the 15-minute WTI contract from the tape."""
    slug, start, end, payload = row
    try:
        window = Window(**json.loads(payload))
    except (TypeError, ValueError):
        return None
    if (window.key == "wti-15m" and end - start == 900
            and (window.slug, window.start, window.end) == (slug, start, end)):
        return window
    return None


def recorded_book(at, payload):
    try:
        book = Book(**payload)
        if book.at == at:
            return book
    except (TypeError, ValueError):
        pass
    return None


class StreamQuotes:
    """Invalidate the quote on a gap, close, or trade newer than the displayed depth."""

    def __init__(self, *, exchange_max_age=EXCHANGE_MAX_AGE):
        self.exchange_max_age = exchange_max_age
        self.quote = None
        self.last_book = None
        self.exchange_at = self.sequence = self.trade_at = None

    def update(self, at, kind, payload):
        if kind == "trade":
            stamp = payload.get("exchange_at")
            if stamp is not None and (self.trade_at is None or stamp > self.trade_at):
                self.trade_at = stamp
            if self.quote and (stamp is None or stamp > self.quote.exchange_at):
                self.quote = None
            return None
        if kind != "book":
            self.quote = None
            return None
        book = recorded_book(at, payload)
        self.last_book = book
        self.quote = None
        if book is None or not usable(book, at, exchange_max_age=self.exchange_max_age):
            return None
        if ((self.exchange_at is not None and book.exchange_at < self.exchange_at)
                or (self.trade_at is not None and book.exchange_at < self.trade_at)
                or (self.sequence is not None and book.sequence is not None and book.sequence < self.sequence)):
            return None
        self.exchange_at = book.exchange_at
        if book.sequence is not None:
            self.sequence = book.sequence
        self.quote = book
        return book


def first_fresh(books, ready, last, deadline):
    index = bisect_left(books, ready, key=lambda book: book.at)
    for offset in range(index, len(books)):
        book = books[offset]
        if book.at > last or book.at >= deadline:
            return None
        if usable(book, book.at, ready):
            return book
    return None


def summarize_source(totals, samples, eligible):
    delays = sorted(delay for delay, _ in samples)
    return {
        **totals,
        "fresh_intents": len(delays),
        "coverage": len(delays) / eligible if eligible else None,
        "median_ready_to_quote_seconds": median(delays) if delays else None,
        "p95_ready_to_quote_seconds": delays[ceil(.95 * len(delays)) - 1] if delays else None,
        "median_venue_to_receipt_seconds": median(age for _, age in samples) if samples else None,
    }


def report(path: Path, since, as_of):
    sources = ("rest", "stream")
    totals = {source: {"book_updates": 0, "gap_events": 0, "rejected_books": 0} for source in sources}
    samples = {source: [] for source in sources}
    first = {"rest": 0, "stream": 0, "tie": 0, "neither": 0}
    eligible = markets = both = 0
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            windows = db.execute(
                "SELECT slug,start,end,window FROM markets WHERE start BETWEEN ? AND ? AND end>?",
                (since - 900, as_of, since),
            )
            for row in windows:
                window = decode_window(row)
                if window is None:
                    continue
                markets += 1
                books = {source: [] for source in sources}
                stream = StreamQuotes()
                tape = db.execute(
                    """SELECT at,source,kind,payload FROM tape WHERE slug=? AND source IN ('rest','stream')
                       AND at>=? AND at<=? AND at<? ORDER BY at,id""",
                    (window.slug, window.start, as_of, window.end),
                )
                for at, source, kind, raw in tape:
                    payload = json.loads(raw)
                    book = (stream.update(at, kind, payload) if source == "stream"
                            else recorded_book(at, payload) if kind == "book" else None)
                    if at >= since:
                        totals[source]["gap_events"] += kind == "gap"
                        totals[source]["book_updates"] += kind == "book"
                    if book and usable(book, at):
                        books[source].append(book)
                    elif kind == "book" and at >= since:
                        totals[source]["rejected_books"] += 1
                intents = db.execute(
                    """SELECT at,payload FROM tape WHERE slug=? AND source='intent' AND kind='order'
                       AND at>=? AND at<=? AND json_extract(payload,'$.model')='D'
                       AND json_extract(payload,'$.action')='buy'""",
                    (window.slug, window.start, as_of),
                )
                for at, raw in intents:
                    intent = json.loads(raw)
                    if intent.get("side") not in ("UP", "DOWN"):
                        continue
                    ready = intent["ready_at"]
                    deadline = min(intent["deadline"], window.end - EXIT_BUFFER) - SUBMISSION_BUFFER
                    last = ready + SIGNAL_TTL
                    if not (max(at, since) <= ready < deadline and min(last, deadline) <= as_of):
                        continue
                    eligible += 1
                    fresh = {source: first_fresh(books[source], ready, last, deadline) for source in sources}
                    for source, book in fresh.items():
                        if book:
                            samples[source].append((book.at - ready, book.at - book.exchange_at))
                    rest, streamed = fresh["rest"], fresh["stream"]
                    if rest and streamed:
                        both += 1
                        first["rest" if rest.at < streamed.at else "stream" if streamed.at < rest.at else "tie"] += 1
                    else:
                        first["rest" if rest else "stream" if streamed else "neither"] += 1
    return {
        "since": since, "as_of": as_of, "markets": markets, "eligible_d_buy_intents": eligible,
        "both_sources_fresh": both, "first_fresh": first,
        "sources": {source: summarize_source(totals[source], samples[source], eligible) for source in sources},
        "measurement": "Recorded receipt times for clock-eligible Oil D buy intents. Stream writes may be "
                       "buffered. Direct live REST reads are outside this comparison.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="lookback hours (default 24)")
    parser.add_argument("--db", type=Path, default=tape_path(WTI.directory()), help="override the WTI tape path")
    parser.add_argument("--json", action="store_true", help="print all metrics as JSON")
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    now = time.time()
    stats = report(args.db, now - 3600 * args.hours, now)
    if args.json:
        print(json.dumps(stats, indent=2))
        return
    print(f"WTI stream shadow | {args.hours}h | {stats['markets']} markets | "
          f"{stats['eligible_d_buy_intents']} eligible Oil D buy intents")
    print(stats["measurement"])
    for source, metrics in stats["sources"].items():
        coverage = metrics["coverage"]
        delay = metrics["median_ready_to_quote_seconds"]
        print(f"{source.upper():6} {metrics['fresh_intents']} covered "
              f"({f'{coverage:.0%}' if coverage is not None else '—'}) | "
              f"{metrics['book_updates']} books | {metrics['gap_events']} gaps | "
              f"{metrics['rejected_books']} rejected books | median wait "
              f"{f'{delay:.2f}s' if delay is not None else '—'}")
    first = stats["first_fresh"]
    print(f"First fresh: REST {first['rest']} | stream {first['stream']} | tie {first['tie']} | "
          f"neither {first['neither']}")


if __name__ == "__main__":
    main()
