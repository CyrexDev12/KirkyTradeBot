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
* ``manifest.json`` lists every file with its size and SHA-256, and each
  database's tables with row counts.

Usage:
  python export_data.py --source predictions=/source/predictions --source bot-state=/source/bot-state \\
      --output /export/nfl-data-20261123T030000Z.tar.gz [--only trading,gold] [--csv] [--owner 1000:1000]
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def export(sources: dict[str, Path], output: Path, only: set[str] | None = None, with_csv: bool = False,
           owner: tuple[int, int] | None = None, now: datetime | None = None) -> dict:
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
            root = Path(root)
            if not root.is_dir():
                raise SystemExit(f"Source {volume} is not a directory: {root}")
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
                                       "trading, crypto, updown, gold; 'bot' = the Discord bot's state")
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
    if result["skipped"]:
        print(f"  skipped {len(result['skipped'])} file(s) that look like secrets")
    print(f"SHA-256 {result['archive_sha256']}")


if __name__ == "__main__":
    main()
