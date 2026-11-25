"""Azure data export: consistent SQLite snapshots, no secrets, and the full command path
(export.sh -> ssh -> export-on-vm.sh -> docker -> export_data.py) against mocks."""
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from deploy.export_data import export, main, paired_oil_d_entries

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def make_volumes(root: Path):
    """Two fake volumes shaped like the VM's, with a database that is mid-write (rows only in the WAL)."""
    pred, state = root / "predictions", root / "bot-state"
    (pred / "trading").mkdir(parents=True)
    (pred / "gold").mkdir()
    (pred / "crypto").mkdir()
    (state).mkdir()
    (pred / "predictions.json").write_text('{"games": 16}')
    (pred / "gold" / "health.json").write_text('{"last_heartbeat": "2026-11-23T03:00:00+00:00"}')
    (pred / "gold" / "health.tmp").write_text("partial")
    (pred / ".secrets").mkdir()
    (pred / ".secrets" / "discord_token.txt").write_text("PRIVATE-TOKEN")
    (pred / "crypto" / "api_token.json").write_text("PRIVATE-TOKEN")
    gold = sqlite3.connect(pred / "gold" / "paper.sqlite3")
    gold.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, side TEXT, net_pnl REAL)")
    gold.executemany("INSERT INTO trades(side, net_pnl) VALUES (?, ?)", [("UP", 1.9), ("DOWN", -2.9)])
    gold.commit()
    gold.close()
    # The live NFL journal in WAL mode with committed rows not yet checkpointed into the main file.
    live = sqlite3.connect(pred / "trading" / "paper_trading.sqlite3")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT)")
    live.executemany("INSERT INTO events(kind) VALUES (?)", [("ENTRY_FILL",)] * 25)
    live.commit()
    bot = sqlite3.connect(state / "discord.sqlite3")
    bot.execute("CREATE TABLE paper_channels (guild_id INTEGER, kind TEXT, channel_id INTEGER)")
    bot.execute("INSERT INTO paper_channels VALUES (1, 'gold', 55)")
    bot.commit()
    bot.close()
    (state / "discord.heartbeat").write_text("1795400000")
    return pred, state, live


def members(path):
    with tarfile.open(path) as archive:
        return {m.name: m for m in archive.getmembers()}, json.load(archive.extractfile("manifest.json"))


def test_export_is_consistent_complete_and_secret_free(tmp_path):
    pred, state, live = make_volumes(tmp_path / "vol")
    assert (pred / "trading" / "paper_trading.sqlite3-wal").stat().st_size > 0  # rows are only in the WAL
    out = tmp_path / "out" / "nfl-data-test.tar.gz"
    result = export({"predictions": pred, "bot-state": state}, out, with_csv=True)
    live.close()
    names, manifest = members(out)
    assert manifest == {k: v for k, v in result.items() if k not in ("archive", "archive_sha256")}
    files = {f["path"]: f for f in manifest["files"]}
    assert files["predictions/trading/paper_trading.sqlite3"]["tables"] == {"events": 25}
    assert files["predictions/gold/paper.sqlite3"]["tables"] == {"trades": 2}
    assert files["bot-state/discord.sqlite3"]["tables"] == {"paper_channels": 1}
    assert {"predictions/predictions.json", "predictions/gold/health.json", "bot-state/discord.heartbeat",
            "csv/predictions/gold/paper.sqlite3/trades.csv"} <= set(names)
    assert not any(n.endswith(("-wal", "-shm", ".tmp")) for n in names)
    assert {s["path"] for s in manifest["skipped"]} == {"predictions/.secrets/discord_token.txt",
                                                       "predictions/crypto/api_token.json"}
    with tarfile.open(out) as archive:
        for m in archive.getmembers():
            assert m.mode == 0o600 and not m.name.startswith("/") and ".." not in m.name
            data = archive.extractfile(m).read()
            assert b"PRIVATE-TOKEN" not in data
            if m.name != "manifest.json":
                assert hashlib.sha256(data).hexdigest() == files[m.name]["sha256"]
        assert archive.extractfile("csv/predictions/gold/paper.sqlite3/trades.csv").read().decode().splitlines() == [
            "id,side,net_pnl", "1,UP,1.9", "2,DOWN,-2.9"]
        archive.extractall(tmp_path / "unpacked", filter="data")
    snap = sqlite3.connect(tmp_path / "unpacked/predictions/trading/paper_trading.sqlite3")
    assert snap.execute("SELECT count(*) FROM events").fetchone()[0] == 25
    assert snap.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # one self-contained file
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    checksum = (out.parent / (out.name + ".sha256")).read_text().split()[0]
    assert checksum == hashlib.sha256(out.read_bytes()).hexdigest() == result["archive_sha256"]
    # The live database is untouched and still writable by its owner.
    again = sqlite3.connect(pred / "trading" / "paper_trading.sqlite3")
    again.execute("INSERT INTO events(kind) VALUES ('EXIT_FILL')")
    again.commit()
    assert again.execute("SELECT count(*) FROM events").fetchone()[0] == 26


def test_only_filter_and_cli(tmp_path, capsys):
    pred, state, live = make_volumes(tmp_path / "vol")
    out = tmp_path / "gold.tar.gz"
    main(["--source", f"predictions={pred}", "--source", f"bot-state={state}", "--output", str(out),
          "--only", "gold,bot"])
    live.close()
    names, manifest = members(out)
    assert sorted(n for n in names if n != "manifest.json") == [
        "bot-state/discord.heartbeat", "bot-state/discord.sqlite3", "predictions/gold/health.json",
        "predictions/gold/paper.sqlite3"]
    printed = capsys.readouterr().out
    assert "2 databases" in printed and "predictions/gold/paper.sqlite3: trades 2" in printed
    with pytest.raises(SystemExit):
        main(["--source", "predictions", "--output", str(out)])


def make_oil_ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""PRAGMA journal_mode=WAL;
        PRAGMA wal_autocheckpoint=0;
        CREATE TABLE trades (id INTEGER PRIMARY KEY, asset TEXT NOT NULL, window_start INTEGER NOT NULL,
                       status TEXT NOT NULL, cost REAL NOT NULL, net_pnl REAL NOT NULL,
                       closed_day TEXT, payload TEXT NOT NULL, UNIQUE(asset,window_start));""")
    return db


def insert_oil_trade(db, asset, started, slug, *, status="CLOSED", net=0.0, **fields):
    timestamp = datetime.fromtimestamp(started, timezone.utc).isoformat()
    opened = datetime.fromtimestamp(started + 61, timezone.utc).isoformat()
    closed = datetime.fromtimestamp(started + 88, timezone.utc).isoformat() if status == "CLOSED" else None
    payload = {"market_slug": slug, "mode": "PAPER_ONLY", "asset": "wti", "status": status,
               "window_start_ts": started, "window_start": timestamp, "opened_at": opened, "closed_at": closed,
               "quantity": 4, "side": "DOWN", "entry_price": 0.4, "entry_fee": 0.09, "cost": 1.69,
               "signal_at": started + 57, "order_ready_at": started + 59,
               "book_at": opened, "book_requested_at": timestamp, "book_exchange_at": timestamp, "book_sequence": 17,
               "entry_fills": [[0.4, 4]], "internal_evidence": "DO-NOT-EXPORT", **fields}
    db.execute("INSERT INTO trades(asset,window_start,status,cost,net_pnl,closed_day,payload) VALUES (?,?,?,?,?,?,?)",
               (asset, started, status, 1.69, net, closed[:10] if closed else None, json.dumps(payload)))


def read_archive_csv(archive, filename):
    return list(csv.DictReader(io.StringIO(archive.extractfile(filename).read().decode("utf-8-sig"))))


def test_oil_d_paper_export_reads_committed_snapshot_and_emits_only_requested_csvs(tmp_path):
    pred = tmp_path / "predictions"
    out = tmp_path / "oil-d.tar.gz"
    start = 1795564800
    with closing(make_oil_ledger(pred / "wti/models/D/paper.sqlite3")) as db:
        insert_oil_trade(db, "wti-15m", start + 900, "d-15m-open", status="OPEN", key="wti-15m", horizon="15m")
        insert_oil_trade(db, "wti-15m", start, "d-15m-loss", net=-1.69, key="wti-15m", horizon="15m")
        insert_oil_trade(db, "wti-1h", start, "d-1h-win", net=2.28, key="wti-1h", horizon="1h")
        insert_oil_trade(db, "wti", start - 3600, "d-1h-legacy-key", net=-.45, key="wti-1h")
        insert_oil_trade(db, "wti", start - 7200, "d-1h-legacy-horizon", net=0, horizon="1h")
        insert_oil_trade(db, "wti-1d", start, "d-excluded-1d", net=8.99, key="wti-1d", horizon="1d")
        insert_oil_trade(db, "wti-150m", start, "d-excluded-other", net=8.99, key="wti-150m", horizon="150m")
        db.commit()
        assert (pred / "wti/models/D/paper.sqlite3-wal").stat().st_size > 0
        insert_oil_trade(db, "wti-15m", start + 1800, "d-not-yet-committed", status="OPEN")
        for name in ("models/A/paper.sqlite3", "live/live.sqlite3", "shadow/oil-d.sqlite3"):
            with closing(make_oil_ledger(pred / "wti" / name)) as unrelated:
                insert_oil_trade(unrelated, "wti-15m", start, "unrelated-database", net=9.8)
                unrelated.commit()

        result = export({"predictions": pred}, out, only={"oil-d-paper"})
        assert db.execute("SELECT count(*) FROM trades").fetchone() == (8,)
        db.rollback()
        assert db.execute("SELECT count(*) FROM trades").fetchone() == (7,)

    with tarfile.open(out) as archive:
        assert set(archive.getnames()) == {"oil-d-15m.csv", "oil-d-1h.csv", "oil-d-summary.csv", "manifest.json"}
        manifest = json.load(archive.extractfile("manifest.json"))
        assert manifest["only"] == ["oil-d-paper"]
        assert manifest == {k: v for k, v in result.items() if k not in ("archive", "archive_sha256")}
        for record in manifest["files"]:
            data = archive.extractfile(record["path"]).read()
            assert b"DO-NOT-EXPORT" not in data
            assert hashlib.sha256(data).hexdigest() == record["sha256"]
        quarter_hour = read_archive_csv(archive, "oil-d-15m.csv")
        hourly = read_archive_csv(archive, "oil-d-1h.csv")
        summary = {row["horizon"]: row for row in read_archive_csv(archive, "oil-d-summary.csv")}
        assert [row["market_slug"] for row in quarter_hour] == ["d-15m-open", "d-15m-loss"]
        assert [row["market_slug"] for row in hourly] == ["d-1h-win", "d-1h-legacy-key", "d-1h-legacy-horizon"]
        assert quarter_hour[0]["status"] == "OPEN" and quarter_hour[0]["net_pnl_usd"] == ""
        assert quarter_hour[1]["status"] == "CLOSED" and float(quarter_hour[1]["net_pnl_usd"]) == -1.69
        assert [float(row["net_pnl_usd"]) for row in hourly] == [2.28, -.45, 0.0]
        proof = quarter_hour[1]
        assert json.loads(proof["entry_fills"]) == [[.4, 4]] and proof["book_sequence"] == "17"
        assert datetime.fromisoformat(proof["book_received_at_utc"]).timestamp() == start + 61
        assert datetime.fromisoformat(proof["book_exchange_at_utc"]).timestamp() == start
        assert datetime.fromisoformat(proof["book_requested_at_utc"]).timestamp() == start
        assert datetime.fromisoformat(proof["order_ready_at_utc"]).timestamp() == start + 59
        assert datetime.fromisoformat(proof["signal_at_utc"]).timestamp() == start + 57
        counts = ("trades", "open_trades", "closed_trades", "wins", "losses", "breakeven")
        assert tuple(int(summary["15m"][key]) for key in counts) == (2, 1, 1, 0, 1, 0)
        assert tuple(int(summary["1h"][key]) for key in counts) == (3, 0, 3, 1, 1, 1)
        assert float(summary["15m"]["net_pnl_usd"]) == -1.69
        assert float(summary["1h"]["net_pnl_usd"]) == pytest.approx(1.83)


def test_oil_d_paper_export_reports_missing_ledger_without_creating_archive(tmp_path):
    pred = tmp_path / "predictions"
    with closing(make_oil_ledger(pred / "wti/models/A/paper.sqlite3")):
        out = tmp_path / "oil-d-missing.tar.gz"
        with pytest.raises(SystemExit, match="Oil D paper"):
            export({"predictions": pred}, out, only={"oil-d-paper"})
    assert not out.exists()
    assert not out.with_name(out.name + ".sha256").exists()


def make_oil_d_comparison_ledgers(pred):
    from backend.wti.live_store import LiveStore
    from backend.wti.oil_d_shadow_store import OilDShadowStore

    at = 1795600900
    slug = "cpwtif-wti-updown-15m-2026-11-25-1430z"
    live = LiveStore(pred / "wti/live/live.sqlite3")
    signal_id = live.record_signal(slug, at - 100, at + 800, at,
                                   {"signal_source": "stream", "signal_book_received_at": at - 1,
                                    "signal_book_exchange_at": at - 2, "side": "UP"})
    order = live.claim_entry(signal_id, "dry-run-key", at + 2, {"side": "UP", "price": "0.49", "quantity": "2"})
    live.update_order(order["id"], "ORDER_STATE_CANCELED", at + 3, venue_id="mock-exchange",
                      payload={"filled": "1", "average_price": "0.47", "fees": "0.02"})
    live.close_trade(order["trade_id"], at + 20, "CLOSED", {"net_pnl": "0.11", "fees_exact": True})
    live.record_entry_timing(at + 3, {"slug": slug, "signal_at": at, "signal_source": "stream", "side": "UP",
                                      "outcome": "submitted", "final_quote_source": "stream", "filled": "1"})
    live.record_entry_timing(at + 4, {"slug": slug, "signal_at": at + 4, "signal_source": "stream",
                                      "outcome": "skipped", "reason": "budget_below_minimum"})
    shadow = OilDShadowStore(pred / "wti/shadow/oil-d.sqlite3")
    shadow.record(slug, at + 0.5, "signal", at + 0.5, {"side": "UP", "shadow_limit": 0.49,
                  "book_received_at": at - 1, "book_exchange_at": at - 2})
    shadow.record(slug, at + 0.5, "execution", at + 3, {"outcome": "filled", "fill": {
        "quantity": "1.5", "average_price": "0.45", "fee": "0.03", "filled_at": at + 3}})


def test_pairing_prefers_shared_signal_book_and_excludes_legacy():
    live = {"market_slug": "window", "side": "DOWN", "order_id": 1, "order_state": "ORDER_STATE_CANCELED",
            "signal_at": 110, "signal_source": "stream", "signal_book_received_at": 108.5,
            "signal_book_exchange_at": 108, "venue_order_id": "123", "filled_quantity": "0.10", "fill_price": "0.43"}
    shadow = {"market_slug": "window", "side": "DOWN", "filled_quantity": "0.20", "fill_price": "0.45"}
    candidates = [{**shadow, "signal_at": 110.1, "signal_book_received_at": 109},
                  {**shadow, "signal_at": 109.1, "signal_book_received_at": 108.5, "signal_book_exchange_at": 108}]
    pair, no_match = paired_oil_d_entries([live, {**live, "market_slug": "later", "signal_at": 120},
                                         {**live, "signal_source": "paper"}], candidates)
    assert pair["match"] == "same_signal_book" and pair["shadow_signal_at"] == 109.1
    assert float(pair["live_minus_shadow_price_cents"]) == -2
    assert float(pair["live_minus_shadow_quantity"]) == -.10
    assert no_match["match"] == "no_shadow_signal_for_contract"


MOCK_DOCKER = r'''
import os, shutil, subprocess, sys, tempfile
args = sys.argv[1:]
log = open(os.environ["MOCK_LOG"], "a"); log.write(" ".join(args) + "\n"); log.close()
if args[:2] in (["volume", "inspect"], ["image", "inspect"]):
    sys.exit(0)
assert args[0] == "run", args
for flag in ("--rm", "--read-only"):
    assert flag in args, flag
assert args[args.index("--network") + 1] == "none"
mounts = {}
for i, a in enumerate(args):
    if a == "--mount":
        parts = dict(p.split("=", 1) if "=" in p else (p, True) for p in args[i + 1].split(","))
        source = parts["source"]
        if parts["type"] == "volume":
            source = os.path.join(os.environ["MOCK_VOLUMES"], source.split("_", 1)[1])
        mounts[parts["target"]] = source
        if parts["target"].startswith("/tool/"):
            assert "readonly" in parts
image_at = args.index(os.environ.get("MOCK_IMAGE", "nfl-fair-value-backend:latest"))
command = args[image_at + 1:]
def host(value):
    for target, source in sorted(mounts.items(), key=lambda m: -len(m[0])):
        if value == target or value.startswith(target + "/"):
            return source + value[len(target):]
        if "=" in value and value.split("=", 1)[1].startswith(target):
            name, path = value.split("=", 1)
            return name + "=" + source + path[len(target):]
    return value
with tempfile.TemporaryDirectory() as tool_dir:
    for target, source in list(mounts.items()):
        if target.startswith("/tool/"):
            local = os.path.join(tool_dir, os.path.basename(target))
            shutil.copyfile(source, local)
            mounts[target] = local
    sys.exit(subprocess.call([sys.executable] + [host(a) for a in command]))
'''


def write_mock(bin_dir: Path, name: str, body: str):
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.mark.skipif(not BASH, reason="Bash is required")
@pytest.mark.parametrize("scope", ("gold", "oil-d-live-shadow"))
def test_export_command_end_to_end_with_mocks(tmp_path, scope):
    vm_home = tmp_path / "vm"
    (vm_home / "nfl-fair-value").mkdir(parents=True)
    (vm_home / "nfl-fair-value" / "compose.yaml").write_text("services: {}\n")
    volumes = tmp_path / "volumes"
    pred, state, live = make_volumes(volumes)
    live.close()
    if scope == "oil-d-live-shadow":
        make_oil_d_comparison_ledgers(pred)
    shell_home = tmp_path / "cloudshell"
    (shell_home / ".ssh").mkdir(parents=True)
    (shell_home / ".ssh" / "nfl-discord").write_text("KEY")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "docker.py").write_text(MOCK_DOCKER)
    write_mock(bin_dir, "docker", f'#!/bin/bash\nexec "{sys.executable}" "{tmp_path}/docker.py" "$@"\n')
    write_mock(bin_dir, "sudo", '#!/bin/bash\n[[ "$1" == -n ]] && shift\n[[ $# -eq 0 || "$1" == true ]] && exit 0\nexec "$@"\n')
    write_mock(bin_dir, "az", '#!/bin/bash\necho "az $*" >> "$MOCK_LOG"\n'
               'case "$*" in *public-ip*) echo 203.0.113.7;; *osProfile*) echo azureuser;; esac\n')
    write_mock(bin_dir, "curl", '#!/bin/bash\necho 198.51.100.9\n')
    # ssh: run the remote command locally as the VM user (HOME = the fake VM home).
    write_mock(bin_dir, "ssh", '#!/bin/bash\nwhile [[ "$1" == -* ]]; do shift 2; done\nshift\n'
               'echo "ssh $*" >> "$MOCK_LOG"\ncd "$VM_HOME" && HOME="$VM_HOME" bash -c "$*"\n')
    # scp: "user@host:path" means a path under the fake VM home.
    write_mock(bin_dir, "scp", '#!/bin/bash\nwhile [[ "$1" == -* ]]; do shift 2; done\nargs=()\n'
               'for a in "$@"; do if [[ "$a" == *@*:* ]]; then args+=("$VM_HOME/${a#*:}"); else args+=("$a"); fi; done\n'
               'cp "${args[@]}"\n')
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(shell_home),
           "VM_HOME": str(vm_home), "MOCK_LOG": str(log), "MOCK_VOLUMES": str(volumes)}
    run = subprocess.run([BASH, str(ROOT / "deploy/azure/export.sh"), "sub-123", "--only", scope, "--csv"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    saved = list((shell_home / "nfl-exports").glob("nfl-data-*.tar.gz"))
    assert len(saved) == 1 and "OK" in run.stdout and "Saved:" in run.stdout
    names, manifest = members(saved[0])
    if scope == "gold":
        assert "predictions/gold/paper.sqlite3: trades 2" in run.stdout
        assert "csv/predictions/gold/paper.sqlite3/trades.csv" in names
        assert not any(n.startswith("predictions/trading") for n in names)
    else:
        assert "Paired: 1 close signals, 1 comparable fill prices" in run.stdout
        assert set(names) == {"manifest.json", "oil-d-live-trades.csv", "oil-d-live-attempts.csv",
                              "oil-d-shadow-entries.csv", "oil-d-live-shadow-comparison.csv",
                              "oil-d-live-shadow-summary.csv"}
        assert manifest["oil_d_live_shadow"]["websocket_live_realized_net_pnl_usd"] == "0.11"
        with tarfile.open(saved[0]) as archive:
            pair, = read_archive_csv(archive, "oil-d-live-shadow-comparison.csv")
            assert pair["match"] == "same_signal_book"
            assert float(pair["live_minus_shadow_price_cents"]) == 2
            assert float(pair["live_minus_shadow_quantity"]) == -.5
            reasons = {row["reason"] for row in read_archive_csv(archive, "oil-d-live-attempts.csv")}
            assert "budget_below_minimum" in reasons
    calls = log.read_text()
    assert "--source-address-prefixes 198.51.100.9/32" in calls and "--network none" in calls
    # Nothing left behind on the VM: the export folder and uploaded scripts are removed.
    assert not any((vm_home / "nfl-exports").iterdir()) and not any((vm_home / "nfl-upload").iterdir())
    # A bad --only value is rejected before anything runs.
    bad = subprocess.run([BASH, str(ROOT / "deploy/azure/export.sh"), "sub-123", "--only", "gold;rm -rf /"],
                         env=env, capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "comma list" in bad.stderr
