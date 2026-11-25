"""Compare recorded WTI 15-minute order intentions against a forward market tape."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from ..updown.depth import available_depth, spend_depth
from ..updown.strategy import Book, Settings, Window, match_entry, match_exit
from .commodity import WTI
from .models import MODEL_INFO
from .tape import tape_path


@dataclass(frozen=True)
class Scenario:
    name: str
    added_seconds: float
    depth: float


SCENARIOS = (
    Scenario("baseline", 0, .50),
    Scenario("slower", 3, .50),
    Scenario("thinner", 0, .25),
    Scenario("slower+thinner", 3, .25),
)
BOOK_MAX_AGE = 5.0


@dataclass
class Fills:
    attempts: int = 0
    unverified: int = 0
    filled: int = 0
    requested: int = 0
    quantity: int = 0
    gross: float = 0.0

    def total(self, other):
        return Fills(self.attempts + other.attempts, self.unverified + other.unverified,
                     self.filled + other.filled, self.requested + other.requested,
                     self.quantity + other.quantity, self.gross + other.gross)

    def as_report(self):
        verified = self.attempts - self.unverified
        return {
            "attempts": self.attempts, "unverified": self.unverified, "verified": verified,
            "filled": self.filled, "requested_contracts_verified": self.requested, "filled_contracts": self.quantity,
            "fill_rate": self.filled / verified if verified else None,
            "quantity_fill_rate": self.quantity / self.requested if self.requested else None,
            "average_fill_price": self.gross / self.quantity if self.quantity else None,
        }


class BookCursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.following = next(self.rows, None)
        self.spent = {"bids": {}, "offers": {}}
        self.raw = self.available = None
        self.last_exchange_at = self.last_sequence = self.last_trade_at = None
        self.state = "gap"
        self.last_at = float("-inf")

    def advance(self, arrival):
        while self.following and self.following[0] <= arrival:
            at, kind, payload = self.following
            self.following = next(self.rows, None)
            if kind == "trade":
                # A trade newer than the last book can have changed its depth.
                # Wait for the next book to confirm the level quantities.
                stamp = payload.get("exchange_at")
                if stamp is not None and (self.last_trade_at is None or stamp > self.last_trade_at):
                    self.last_trade_at = stamp
                if self.state == "book" and (stamp is None or self.raw.exchange_at is None
                                             or stamp > self.raw.exchange_at):
                    self.state = "gap"
                continue
            self.state, self.last_at = kind, at
            if kind != "book":
                continue
            book = Book(**payload)
            if ((book.exchange_at is not None and self.last_exchange_at is not None
                 and book.exchange_at < self.last_exchange_at)
                    or (book.sequence is not None and self.last_sequence is not None
                        and book.sequence < self.last_sequence)):
                self.state = "gap"
                continue
            if book.exchange_at is not None:
                self.last_exchange_at = book.exchange_at
            if book.sequence is not None:
                self.last_sequence = book.sequence
            if self.last_trade_at is not None and (book.exchange_at is None or book.exchange_at < self.last_trade_at):
                self.state = "gap"
                continue
            self.raw = book
            self.available = available_depth(book, self.spent)
        if (arrival - self.last_at > BOOK_MAX_AGE
                or (self.state == "book" and self.raw.exchange_at is not None and self.raw.exchange_at > arrival)):
            return "gap"
        return self.state

    def consume(self, side, buying, fills, fraction):
        if not spend_depth(self.raw, self.spent, side, buying, fills, fraction):
            raise RuntimeError("Replay tried to consume unavailable liquidity")
        self.available = available_depth(self.raw, self.spent)


def replay_window(window, intents, books, payout, as_of, scenario):
    buy, sell = Fills(), Fills()
    remaining = 0
    side = None
    cash = 0.0
    pending = window.end > as_of
    cursor = BookCursor(books)
    pricing = Settings(participation=scenario.depth)
    for _, intent in intents:
        buying = intent["action"] == "buy"
        if (buying and side is not None) or (not buying and (not remaining or side != intent["side"])):
            continue
        stats = buy if buying else sell
        arrival = intent["ready_at"] + scenario.added_seconds
        deadline = min(window.end, intent["deadline"])
        if min(arrival, deadline) > as_of:
            pending = True
            continue
        quantity = intent["quantity"] if buying else min(remaining, intent["quantity"])
        stats.attempts += 1
        state = "closed" if arrival >= deadline else cursor.advance(arrival)
        if state == "gap":
            stats.unverified += 1
            continue
        stats.requested += quantity
        if state == "closed":
            continue
        if buying:
            entry_settings = replace(pricing, min_edge=intent["min_edge"])
            decision = match_entry(replace(window, theta=intent["theta"]), cursor.available, intent["side"],
                                   quantity, intent["limit"], intent["fair_up"], intent["budget"], entry_settings,
                                   balance=max(intent["budget"], 1), open_stake=0)
            if not decision:
                continue
            filled = decision.quantity
            gross = sum(price * count for price, count in decision.fills)
            levels = decision.fills
            remaining, side = filled, intent["side"]
            cash -= decision.cost
        else:
            trade = {"side": side, "remaining": remaining, "theta": intent["theta"]}
            decision = match_exit(trade, cursor.available, quantity, intent["limit"], intent["exit_kind"], pricing)
            if not decision:
                continue
            filled, gross, levels = decision["quantity"], decision["gross"], decision["fills"]
            remaining -= filled
            cash += gross - decision["fee"]
        cursor.consume(intent["side"], buying, levels, scenario.depth)
        stats.filled += 1
        stats.quantity += filled
        stats.gross += gross
    if remaining:
        if payout is None:
            pending = True
        else:
            cash += remaining * (payout if side == "UP" else 1 - payout)
    verified = not (buy.unverified or sell.unverified)
    return buy, sell, cash if verified and not pending else None


@dataclass
class ModelResult:
    entry: Fills
    exit: Fills
    pnl: float = 0.0
    scored_windows: int = 0

    def as_report(self):
        return {"entry": self.entry.as_report(), "exit": self.exit.as_report(),
                "net_pnl": round(self.pnl, 6) if self.scored_windows else None,
                "scored_windows": self.scored_windows}


def compare(path: Path, since: float, as_of: float, source: str = "auto"):
    totals = {(model, s.name): ModelResult(Fills(), Fills()) for model in MODEL_INFO for s in SCENARIOS}
    markets = trade_updates = book_updates = 0
    requested = source
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")  # One view of the tape while Azure keeps recording.
            criteria = "FROM tape JOIN markets USING(slug) WHERE markets.start>=? AND tape.at<=?"
            period = (since, as_of)
            if source == "auto":
                streaming = db.execute(f"""SELECT 1 {criteria}
                    AND source='stream' AND kind='book' LIMIT 1""", period).fetchone()
                source = "stream" if streaming else "rest"
            trade_updates = db.execute(f"SELECT count(*) {criteria} AND kind='trade'", period).fetchone()[0]
            book_updates = db.execute(f"SELECT count(*) {criteria} AND kind='book' AND source=?",
                                      (*period, source)).fetchone()[0]
            for slug, raw_window, payout, settled_at in db.execute(
                    "SELECT slug,window,payout,settled_at FROM markets WHERE start>=? AND start<=? ORDER BY start", period):
                markets += 1
                window = Window(**json.loads(raw_window))
                if settled_at is None or settled_at > as_of:
                    payout = None
                intents = {model: [] for model in MODEL_INFO}
                for at, payload in db.execute("""SELECT at,payload FROM tape WHERE slug=? AND source='intent'
                                                AND at<=? ORDER BY at,id""", (slug, as_of)):
                    data = json.loads(payload)
                    if data["model"] in intents:
                        intents[data["model"]].append((at, data))
                if not any(intents.values()):
                    continue
                books = [(at, kind, json.loads(data)) for at, kind, data in db.execute(
                    """SELECT at,kind,payload FROM tape WHERE slug=? AND source=? AND at<=?
                       AND kind IN ('book','closed','gap','trade') ORDER BY at,id""", (slug, source, as_of))]
                for model, orders in intents.items():
                    if not orders:
                        continue
                    results = [replay_window(window, orders, books, payout, as_of, scenario) for scenario in SCENARIOS]
                    comparable = all(pnl is not None for _, _, pnl in results)
                    for scenario, (entry, exit_, pnl) in zip(SCENARIOS, results):
                        result = totals[model, scenario.name]
                        result.entry = result.entry.total(entry)
                        result.exit = result.exit.total(exit_)
                        # P&L uses the same completed, verified windows for all scenarios of a model.
                        if comparable:
                            result.pnl += pnl
                            result.scored_windows += 1
    return {"source": source if source != "auto" else "rest", "requested_source": requested,
            "since": since, "as_of": as_of, "markets_recorded": markets, "book_updates": book_updates,
            "public_trades_recorded": trade_updates,
            "scenarios": [{"name": s.name, "added_seconds": s.added_seconds, "depth": s.depth} for s in SCENARIOS],
            "models": {model: {"name": MODEL_INFO[model][0], "scenarios": {
                s.name: totals[model, s.name].as_report() for s in SCENARIOS}} for model in MODEL_INFO}}


def report_text(report):
    def pct(value):
        return "—" if value is None else f"{value:.1%}"

    def price(value):
        return "—" if value is None else f"{value * 100:.2f}c"

    def side(row):
        return (f"{row['filled']}/{row['verified']} ({pct(row['fill_rate'])}), "
                f"{pct(row['quantity_fill_rate'])} qty, {price(row['average_fill_price'])}")

    span = [datetime.fromtimestamp(report[k], timezone.utc).strftime("%Y-%m-%d %H:%MZ") for k in ("since", "as_of")]
    lines = [f"WTI 15m replay | {span[0]} to {span[1]}",
             f"Source: {report['source']} | {report['markets_recorded']} markets | {report['book_updates']} book updates"
             f" | {report['public_trades_recorded']} public trades captured across sources",
             "Each side shows filled/verified attempts, share of contracts filled, and average fill price.",
             "Net P&L uses windows with comparable outcomes across all four scenarios for that model.",
             "Baseline: recorded order delay, 50% depth. Slower: +3 s. Thinner: 25% depth.", ""]
    if not report["book_updates"]:
        lines.append("Waiting for book data from the selected source.")
    for model, data in report["models"].items():
        lines.append(f"{model}  {data['name']}")
        for scenario, row in data["scenarios"].items():
            pnl = f"{row['net_pnl']:+.2f} USD" if row["net_pnl"] is not None else "—"
            gaps = row["entry"]["unverified"] + row["exit"]["unverified"]
            lines.append(f"  {scenario:<16} buy {side(row['entry']):<33} sell {side(row['exit']):<33}"
                         f" P&L {pnl:>8} ({row['scored_windows']} windows) | unverified {gaps}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=168, help="Lookback in hours (default: one week)")
    parser.add_argument("--source", choices=("auto", "rest", "stream"), default="auto")
    parser.add_argument("--db", type=Path, default=tape_path(WTI.directory()))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 0 < args.hours <= 744:
        parser.error("--hours must be greater than 0 and at most 744")
    now = time.time()
    report = compare(args.db, now - args.hours * 3600, now, args.source)
    print(json.dumps(report, indent=2) if args.json else report_text(report))


if __name__ == "__main__":
    main()
