"""Azure data export: consistent SQLite snapshots, no secrets, and the full command path
(export.sh -> ssh -> export-on-vm.sh -> docker -> export_data.py) against mocks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from deploy.export_data import export, main

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


MOCK_DOCKER = r'''
import os, subprocess, sys
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
        if parts["target"] == "/tool/export_data.py":
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
sys.exit(subprocess.call([sys.executable] + [host(a) for a in command]))
'''


def write_mock(bin_dir: Path, name: str, body: str):
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.mark.skipif(not BASH, reason="Bash is required")
def test_export_command_end_to_end_with_mocks(tmp_path):
    vm_home = tmp_path / "vm"
    (vm_home / "nfl-fair-value").mkdir(parents=True)
    (vm_home / "nfl-fair-value" / "compose.yaml").write_text("services: {}\n")
    volumes = tmp_path / "volumes"
    pred, state, live = make_volumes(volumes)
    live.close()
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
    run = subprocess.run([BASH, str(ROOT / "deploy/azure/export.sh"), "sub-123", "--only", "gold", "--csv"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    saved = list((shell_home / "nfl-exports").glob("nfl-data-*.tar.gz"))
    assert len(saved) == 1 and "OK" in run.stdout and "Saved:" in run.stdout
    assert "predictions/gold/paper.sqlite3: trades 2" in run.stdout
    names, manifest = members(saved[0])
    assert "csv/predictions/gold/paper.sqlite3/trades.csv" in names
    assert not any(n.startswith("predictions/trading") for n in names)
    calls = log.read_text()
    assert "--source-address-prefixes 198.51.100.9/32" in calls and "--network none" in calls
    # Nothing left behind on the VM: the export folder and uploaded scripts are removed.
    assert not any((vm_home / "nfl-exports").iterdir()) and not any((vm_home / "nfl-upload").iterdir())
    # A bad --only value is rejected before anything runs.
    bad = subprocess.run([BASH, str(ROOT / "deploy/azure/export.sh"), "sub-123", "--only", "gold;rm -rf /"],
                         env=env, capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "comma list" in bad.stderr
