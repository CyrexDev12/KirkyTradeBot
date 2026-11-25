"""Snapshot the live data volumes into one archive, without stopping any service.

Runs inside a disposable container on the VM (see ``deploy/azure/export-on-vm.sh``)
with the Docker volumes mounted at ``/source/<name>``. It can also run on any
machine against plain directories, which is how the tests use it.

* SQLite databases are copied with SQLite's online-backup API, so every copy is a
  consistent snapshot (committed data, including rows still in the WAL) even
  while the bots keep writing. ``-wal`` / ``-shm`` / journal files are not copied.
* Other files (JSON, model artifacts, parquet, health files) are copied as-is.
* Anything that looks like a secret (``.env``, ``.secrets``, token or key
  files) is skipped. The secrets live outside the volumes, but this is a guard.
* ``--csv`` also writes every SQLite table as CSV under ``csv/``.
* ``--only oil-d-paper`` writes separate 15-minute and 1-hour Oil D paper trade CSVs and a summary.
* ``--only oil-d-live-shadow`` exports live fills, shadow entry simulations, and paired comparisons.
* ``manifest.json`` lists every file with its size and SHA-256, and each
  database's tables with row counts.

Usage:
  python export_data.py --source predictions=/source/predictions --source bot-state=/source/bot-state \\
      --output /export/nfl-data-20261123T030000Z.tar.gz [--only trading,gold] [--csv] [--owner 1000:1000]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile

SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SKIP_SUFFIXES = ("-wal", "-shm", "-journal", ".tmp")
SECRET_PARTS = (".secrets", ".env")
SECRET_WORDS = ("token", "secret", "password", "private_key", "id_rsa")
OIL_D_PAPER = Path("wti/models/D/paper.sqlite3")
OIL_D_LIVE = Path("wti/live/live.sqlite3")
OIL_D_SHADOW = Path("wti/shadow/oil-d.sqlite3")
OIL_D_SCOPES = {"oil-d-paper", "oil-d-live-shadow"}
PAIR_SECONDS = 15.0
OIL_D_TRADE_FIELDS = (
    "trade_id", "horizon", "status", "side", "market_slug", "window_start_utc", "window_end_utc",
    "opened_at_utc", "closed_at_utc", "closed_day_mst", "signal_at_utc", "order_ready_at_utc",
    "quantity", "remaining_quantity", "entry_price", "entry_fee_usd", "entry_cost_usd", "net_proceeds_usd",
    "net_pnl_usd", "exit_reason", "outcome", "price_to_beat", "settlement_price", "settlement_source",
    "index_price", "fair_up", "fair_side", "edge", "book_requested_at_utc", "book_received_at_utc",
    "book_exchange_at_utc", "book_sequence", "entry_fills", "exits",
)
PAIR_FIELDS = (
    "market_slug", "side", "match", "live_signal_at", "shadow_signal_at", "signal_gap_seconds", "live_order_id",
    "live_order_state", "live_quote_source", "shadow_outcome", "shadow_reason", "live_limit_price",
    "shadow_limit_price", "live_filled_quantity", "shadow_filled_quantity", "live_fill_price", "shadow_fill_price",
    "live_minus_shadow_price_cents", "live_minus_shadow_quantity", "live_first_fill_at", "shadow_first_fill_at",
    "live_entry_fees_usd", "shadow_entry_fees_usd", "live_net_pnl_usd", "live_fees_exact",
)


def is_sqlite(path: Path) -> bool:
    if not path.name.endswith(SQLITE_SUFFIXES):
        return False
    with open(path, "rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def is_secret(relative: Path) -> bool:
    lowered = relative.as_posix().lower()
    return (any(part in SECRET_PARTS or part.startswith(".env") for part in relative.parts)
            or any(word in lowered for word in SECRET_WORDS))


def wanted(volume: str, relative: Path, only: set[str] | None) -> bool:
    """``--only`` matches a volume name (``bot-state``) or a top-level folder in it (``gold``)."""
    if not only:
        return True
    return volume in only or (relative.parts and relative.parts[0] in only) or \
        ("bot" in only and volume == "bot-state")


def sqlite_snapshot(source: Path, destination: Path) -> dict:
    """Consistent copy through the online-backup API; returns table row counts."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        tables = [r[0] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: dst.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        dst.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file
    finally:
        dst.close()
    return counts


def table_csvs(database: Path):
    """Yield (table, csv bytes) for every table."""
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                   "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            cursor = db.execute(f'SELECT * FROM "{table}"')
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows(cursor)
            yield table, buffer.getvalue().encode()
    finally:
        db.close()


def utc_field(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value, (int, float)) else value


def oil_d_paper_csvs(database: Path, directory: Path):
    """Write the requested Oil D horizons from a private SQLite snapshot."""
    summaries = {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        for horizon in ("15m", "1h"):
            market = f"wti-{horizon}"
            summary = dict(trades=0, open_trades=0, closed_trades=0, wins=0, losses=0, breakeven=0,
                           net_pnl_usd=Decimal(0))
            rows = db.execute("""
                SELECT id,window_start,status,cost,net_pnl,closed_day,payload FROM trades
                WHERE asset=? OR (asset='wti' AND
                    coalesce(nullif(json_extract(payload,'$.key'), 'wti'),
                             'wti-' || json_extract(payload,'$.horizon'))=?)
                ORDER BY window_start DESC,id DESC
            """, (market, market))
            with (directory / f"oil-d-{horizon}.csv").open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, OIL_D_TRADE_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for trade_id, start, status, cost, pnl, closed_day, payload in rows:
                    trade = json.loads(payload)
                    closed = status == "CLOSED"
                    summary["trades"] += 1
                    if closed:
                        summary["closed_trades"] += 1
                        result = Decimal(str(pnl))
                        summary["net_pnl_usd"] += result
                        summary["wins" if result > 0 else "losses" if result < 0 else "breakeven"] += 1
                    elif status == "OPEN":
                        summary["open_trades"] += 1
                    writer.writerow({
                        **trade, "trade_id": trade_id, "horizon": horizon, "status": status,
                        "window_start_utc": utc_field(start),
                        "window_end_utc": utc_field(trade.get("window_end") or trade.get("window_end_ts")),
                        "opened_at_utc": utc_field(trade.get("opened_at")),
                        "closed_at_utc": utc_field(trade.get("closed_at")),
                        "closed_day_mst": closed_day,
                        "signal_at_utc": utc_field(trade.get("signal_at")),
                        "order_ready_at_utc": utc_field(trade.get("order_ready_at")),
                        "remaining_quantity": trade.get("remaining"),
                        "entry_fee_usd": trade.get("entry_fee"), "entry_cost_usd": cost,
                        "net_proceeds_usd": trade.get("proceeds"), "net_pnl_usd": pnl if closed else None,
                        "book_requested_at_utc": utc_field(trade.get("book_requested_at")),
                        "book_received_at_utc": utc_field(trade.get("book_at")),
                        "book_exchange_at_utc": utc_field(trade.get("book_exchange_at")),
                        "entry_fills": json.dumps(trade["entry_fills"]) if "entry_fills" in trade else None,
                        "exits": json.dumps(trade["exits"]) if "exits" in trade else None,
                    })
            summary["net_pnl_usd"] = f"{summary['net_pnl_usd']:.6f}"
            summaries[horizon] = summary
    with (directory / "oil-d-summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, ("horizon", *summaries["15m"]))
        writer.writeheader()
        writer.writerows({"horizon": horizon, **summary} for horizon, summary in summaries.items())
    return summaries


def paired_oil_d_entries(live, shadow):
    by_contract = defaultdict(list)
    for candidate in shadow:
        by_contract[candidate["market_slug"]].append(candidate)
    used = set()
    pairs = []
    for real in sorted(live, key=lambda row: row.get("signal_at") or 0):
        if real.get("signal_source") != "stream":
            continue
        row = {"market_slug": real["market_slug"], "side": real["side"], "live_signal_at": real["signal_at"],
               "live_order_id": real["order_id"], "live_order_state": real["order_state"],
               "live_quote_source": real.get("quote_source"), "live_net_pnl_usd": real.get("net_pnl_usd"),
               "live_fees_exact": real.get("fees_exact")}
        price_fields = ("limit_price", "filled_quantity", "fill_price", "first_fill_at", "entry_fees_usd")
        row.update({f"live_{field}": real.get(field) for field in price_fields})
        candidates = by_contract[real["market_slug"]]
        same_side = [candidate for candidate in candidates if candidate["side"] == real["side"]]
        close = [candidate for candidate in same_side if candidate["signal_at"] is not None
                 and real["signal_at"] is not None
                 and abs(candidate["signal_at"] - real["signal_at"]) <= PAIR_SECONDS]
        available = [candidate for candidate in close if (candidate["market_slug"], candidate["signal_at"]) not in used]
        if not available:
            row["match"] = ("shadow_already_paired" if close else "outside_signal_window" if same_side else
                            "different_side" if candidates else "no_shadow_signal_for_contract")
            pairs.append(row)
            continue

        def shares_signal_book(candidate):
            fields = ("signal_book_received_at", "signal_book_exchange_at")
            return all(real.get(field) is not None and candidate.get(field) == real[field] for field in fields)

        match = min(available, key=lambda candidate: (not shares_signal_book(candidate),
                    abs(candidate["signal_at"] - real["signal_at"]), candidate["signal_at"]))
        used.add((match["market_slug"], match["signal_at"]))
        row.update(match="same_signal_book" if shares_signal_book(match) else "nearby_signal",
                   shadow_signal_at=match["signal_at"],
                   signal_gap_seconds=round(match["signal_at"] - real["signal_at"], 6),
                   shadow_outcome=match.get("outcome"), shadow_reason=match.get("reason"))
        row.update({f"shadow_{field}": match.get(field) for field in price_fields})
        if real.get("venue_order_id") and decimal_field(real.get("filled_quantity")) > 0:
            if decimal_field(match.get("filled_quantity")) > 0:
                row["live_minus_shadow_quantity"] = (decimal_field(real["filled_quantity"])
                                                     - decimal_field(match["filled_quantity"]))
                if real.get("fill_price") is not None and match.get("fill_price") is not None:
                    row["live_minus_shadow_price_cents"] = 100 * (
                        decimal_field(real["fill_price"]) - decimal_field(match["fill_price"]))
        pairs.append(row)
    return pairs


def decimal_field(value):
    return Decimal(str(value)) if value is not None else Decimal(0)


def write_oil_d_csv(path, fields, rows):
    timestamp_fields = {field for field in fields if field.endswith("_at")
                        or field in ("window_start", "window_end", "deadline")}
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(f"{field}_utc" if field in timestamp_fields else field for field in fields)
        for row in rows:
            writer.writerow(utc_field(row.get(field)) if field in timestamp_fields else row.get(field)
                            for field in fields)


def oil_d_live_shadow_csvs(live_db, shadow_db, directory):
    if __package__:
        from .oil_d_live_export import ATTEMPT_FIELDS, LIVE_FIELDS, SHADOW_FIELDS, live_attempt_rows, live_rows, shadow_rows
    else:
        from oil_d_live_export import ATTEMPT_FIELDS, LIVE_FIELDS, SHADOW_FIELDS, live_attempt_rows, live_rows, shadow_rows

    live, shadow, attempts = live_rows(live_db), shadow_rows(shadow_db), live_attempt_rows(live_db)
    pairs = paired_oil_d_entries(live, shadow)
    native = [row for row in live if row.get("signal_source") == "stream"]
    matched = Counter(row["match"] for row in pairs)
    native_confirmed = [row for row in native if row.get("venue_order_id")
                        and decimal_field(row.get("filled_quantity")) > 0]
    aligned = [row for row in pairs if row["match"] in ("same_signal_book", "nearby_signal")]
    confirmed_ids = {row["order_id"] for row in native_confirmed}
    price_deltas = [row["live_minus_shadow_price_cents"] for row in pairs
                    if "live_minus_shadow_price_cents" in row]
    summary = {
        "signal_match_window_seconds": PAIR_SECONDS,
        "all_live_entry_order_records": len(live),
        "websocket_live_entry_order_records": len(native),
        "websocket_live_orders_with_exchange_id": sum(bool(row.get("venue_order_id")) for row in native),
        "websocket_live_orders_with_confirmed_fill": len(native_confirmed),
        "websocket_live_closed_trades": sum(row.get("net_pnl_usd") is not None for row in native),
        "websocket_live_realized_net_pnl_usd": str(sum(decimal_field(row.get("net_pnl_usd")) for row in native)),
        "shadow_signals": len(shadow),
        "shadow_signals_with_simulated_fill": sum(row.get("outcome") == "filled" for row in shadow),
        "same_contract_and_side_pair_count": matched["same_signal_book"] + matched["nearby_signal"],
        "pairs_from_same_signal_book": matched["same_signal_book"],
        "pairs_from_nearby_signals": matched["nearby_signal"],
        "paired_live_confirmed_fills": sum(row["live_order_id"] in confirmed_ids for row in aligned),
        "paired_shadow_simulated_fills": sum(row["shadow_outcome"] == "filled" for row in aligned),
        "pairs_with_fill_prices_on_both_sides": len(price_deltas),
        "average_live_minus_shadow_fill_price_cents": (
            str(round(sum(price_deltas) / len(price_deltas), 4)) if price_deltas else None),
    }
    summary.update({f"unpaired_{name}": count for name, count in sorted(matched.items())
                    if name not in ("same_signal_book", "nearby_signal")})
    outputs = (
        ("oil-d-live-trades.csv", LIVE_FIELDS, live),
        ("oil-d-live-attempts.csv", ATTEMPT_FIELDS, attempts),
        ("oil-d-shadow-entries.csv", SHADOW_FIELDS, shadow),
        ("oil-d-live-shadow-comparison.csv", PAIR_FIELDS, pairs),
        ("oil-d-live-shadow-summary.csv", ("metric", "value"),
         ({"metric": key, "value": value} for key, value in summary.items())),
    )
    for name, fields, rows in outputs:
        write_oil_d_csv(directory / name, fields, rows)
    return summary, [name for name, _, _ in outputs]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def export(sources: dict[str, Path], output: Path, only: set[str] | None = None, with_csv: bool = False,
           owner: tuple[int, int] | None = None, now: datetime | None = None) -> dict:
    oil_d_scope = (only or set()) & OIL_D_SCOPES
    if oil_d_scope and len(only) != 1:
        raise SystemExit("An Oil D export selector must be used by itself.")
    if oil_d_scope and "predictions" not in sources:
        raise SystemExit("The Oil D export requires the predictions volume.")
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"created_at": now.isoformat(), "format": 1, "consistent": "sqlite online backup",
                "volumes": sorted(sources), "only": sorted(only) if only else None, "files": [],
                "skipped": []}
    with tempfile.TemporaryDirectory(prefix="nfl-export-") as scratch:
        scratch = Path(scratch)
        staged: list[tuple[Path, str]] = []
        for volume, root in sorted(sources.items()):
            if oil_d_scope and volume != "predictions":
                continue
            root = Path(root)
            if not root.is_dir():
                raise SystemExit(f"Source {volume} is not a directory: {root}")
            if oil_d_scope:
                is_paper = only == {"oil-d-paper"}
                snapshots = []
                for relative in ((OIL_D_PAPER,) if is_paper else (OIL_D_LIVE, OIL_D_SHADOW)):
                    source = root / relative
                    if (not source.is_file() or source.is_symlink() or root.resolve() not in source.resolve().parents
                            or not is_sqlite(source)):
                        raise SystemExit(f"Oil D {'paper ' if is_paper else ''}database missing or invalid: {source}")
                    snapshot = scratch / source.name
                    sqlite_snapshot(source, snapshot)
                    snapshots.append(snapshot)
                if is_paper:
                    manifest["oil_d_paper"] = oil_d_paper_csvs(snapshots[0], scratch)
                    names = ("oil-d-15m.csv", "oil-d-1h.csv", "oil-d-summary.csv")
                else:
                    manifest["oil_d_live_shadow"], names = oil_d_live_shadow_csvs(*snapshots, scratch)
                for name in names:
                    path = scratch / name
                    staged.append((path, name))
                    manifest["files"].append({"path": name, "kind": "csv", "bytes": path.stat().st_size,
                                              "sha256": sha256(path)})
                continue
            for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
                relative = path.relative_to(root)
                name = f"{volume}/{relative.as_posix()}"
                if not wanted(volume, relative, only):
                    continue
                if path.name.endswith(SKIP_SUFFIXES):
                    continue
                if is_secret(relative):
                    manifest["skipped"].append({"path": name, "reason": "looks like a secret"})
                    continue
                target = scratch / name
                target.parent.mkdir(parents=True, exist_ok=True)
                entry = {"path": name}
                if is_sqlite(path):
                    entry["tables"] = sqlite_snapshot(path, target)
                    entry["kind"] = "sqlite"
                    if with_csv:
                        for table, data in table_csvs(target):
                            csv_name = f"csv/{name}/{table}.csv"
                            csv_path = scratch / csv_name
                            csv_path.parent.mkdir(parents=True, exist_ok=True)
                            csv_path.write_bytes(data)
                            staged.append((csv_path, csv_name))
                            manifest["files"].append({"path": csv_name, "kind": "csv", "bytes": len(data),
                                                      "sha256": hashlib.sha256(data).hexdigest()})
                else:
                    target.write_bytes(path.read_bytes())
                    entry["kind"] = "file"
                entry.update(bytes=target.stat().st_size, sha256=sha256(target))
                manifest["files"].append(entry)
                staged.append((target, name))
        manifest["files"].sort(key=lambda f: f["path"])
        manifest["total_bytes"] = sum(f["bytes"] for f in manifest["files"])
        manifest_bytes = json.dumps(manifest, indent=1).encode()
        tmp = output.with_name(output.name + ".partial")
        with tarfile.open(tmp, "w:gz") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode, info.mtime = len(manifest_bytes), 0o600, int(now.timestamp())
            archive.addfile(info, io.BytesIO(manifest_bytes))
            for path, name in sorted(staged, key=lambda x: x[1]):
                info = archive.gettarinfo(str(path), arcname=name)
                info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
                with open(path, "rb") as handle:
                    archive.addfile(info, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, output)
    checksum = sha256(output)
    output.with_name(output.name + ".sha256").write_text(f"{checksum}  {output.name}\n")
    os.chmod(output.with_name(output.name + ".sha256"), 0o600)
    if owner:
        for path in (output, output.with_name(output.name + ".sha256")):
            os.chown(path, *owner)
    return {**manifest, "archive": str(output), "archive_sha256": checksum}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", action="append", required=True, metavar="NAME=PATH")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only", help="comma list: a volume (predictions, bot-state) or folders such as "
                                       "trading, crypto, updown, gold, wti, natgas; 'bot' = the Discord bot's state. "
                                       "Use 'oil-d-paper' or 'oil-d-live-shadow' by itself for targeted Oil D CSVs.")
    parser.add_argument("--csv", action="store_true", help="also export every SQLite table as CSV")
    parser.add_argument("--owner", help="uid:gid to own the archive (the VM user)")
    args = parser.parse_args(argv)
    sources = {}
    for item in args.source:
        name, _, path = item.partition("=")
        if not name or not path:
            parser.error(f"--source must be NAME=PATH, got {item!r}")
        sources[name] = Path(path)
    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    owner = tuple(int(x) for x in args.owner.split(":")) if args.owner else None
    result = export(sources, args.output, only, args.csv, owner)
    dbs = [f for f in result["files"] if f["kind"] == "sqlite"]
    print(f"Exported {len(result['files'])} files ({result['total_bytes'] / 1e6:.1f} MB before compression), "
          f"{len(dbs)} databases, to {args.output.name}")
    for f in dbs:
        print(f"  {f['path']}: " + ", ".join(f"{t} {n}" for t, n in f["tables"].items()))
    for horizon, summary in result.get("oil_d_paper", {}).items():
        print(f"  Oil D {horizon}: {summary['trades']} trades, {summary['closed_trades']} closed, "
              f"{summary['open_trades']} open, net P&L ${summary['net_pnl_usd']}")
    if comparison := result.get("oil_d_live_shadow"):
        print(f"Oil D WebSocket live: {comparison['websocket_live_entry_order_records']} entry orders, "
              f"{comparison['websocket_live_orders_with_confirmed_fill']} with a confirmed fill")
        print(f"Shadow: {comparison['shadow_signals']} signals, "
              f"{comparison['shadow_signals_with_simulated_fill']} with a simulated fill")
        print(f"Paired: {comparison['same_contract_and_side_pair_count']} close signals, "
              f"{comparison['pairs_with_fill_prices_on_both_sides']} comparable fill prices")
    if result["skipped"]:
        print(f"  skipped {len(result['skipped'])} file(s) that look like secrets")
    print(f"SHA-256 {result['archive_sha256']}")


if __name__ == "__main__":
    main()
