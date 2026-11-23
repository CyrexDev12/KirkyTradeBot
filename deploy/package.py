"""Build a source-only Azure ZIP or tarball, with portable Unix permissions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ("README.md", "requirements.txt", "pytest.ini", "Dockerfile", "compose.yaml",
         ".dockerignore", ".env.example")
DIRECTORIES = ("backend", "bot", "frontend", "deploy", "scripts", "tests", "docs")
EXCLUDED = {"__pycache__", ".pytest_cache", ".secrets", ".env", ".deploy", ".venv", ".git"}


def source_files(root=ROOT):
    paths = [root / name for name in FILES]
    for directory in DIRECTORIES:
        paths.extend(p for p in (root / directory).rglob("*") if p.is_file())
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo"} or (relative.parts[0] == "docs" and path.suffix != ".md"):
            continue
        data = path.read_bytes()
        if path.suffix == ".sh":
            data = data.replace(b"\r\n", b"\n")
        result[relative.as_posix()] = data
    return result


def build(destination, root=ROOT):
    destination = Path(destination)
    files = source_files(root)
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                                           stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        previous = root / "release.json"
        revision = json.loads(previous.read_text()).get("source_commit", "unavailable") if previous.exists() else "unavailable"
    manifest = {"built_at_utc": datetime.now(timezone.utc).isoformat(), "source_commit": revision,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files["release.json"] = json.dumps(manifest, indent=2).encode()
    directories = sorted({parent.as_posix() for name in files for parent in Path(name).parents
                          if str(parent) != "."})
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.name.endswith(".tar.gz"):
        with tarfile.open(destination, "w:gz") as archive:
            for name in directories:
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(entry)
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), 0o755 if name.endswith(".sh") else 0o644
                archive.addfile(entry, io.BytesIO(data))
    else:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in directories:
                entry = zipfile.ZipInfo(name + "/")
                entry.create_system, entry.external_attr = 3, (0o40755 << 16) | 0x10
                archive.writestr(entry, b"")
            for name, data in files.items():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
    print(f"Built {destination} ({len(files)} files; credentials and runtime data excluded).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".deploy/nfl-fair-value-azure.zip")
    build(parser.parse_args().output)
