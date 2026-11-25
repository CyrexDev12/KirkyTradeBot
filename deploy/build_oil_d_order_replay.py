"""Build the standalone source-only Oil D order replay toolkit for Azure Cloud Shell."""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE = "backend.wti.paper_execution_replay"
RUNNER = "deploy/azure/replay-oil-d-orders.sh"
APP = "oil-d-order-replay.pyz"


def write_member(archive, name, data, mode=0o644):
    member = ZipInfo(name)
    member.create_system = 3
    member.external_attr = (0o100000 | mode) << 16
    member.compress_type = ZIP_DEFLATED
    archive.writestr(member, data)


def build(output, root=ROOT):
    entry = root.joinpath(*MODULE.split(".")).with_suffix(".py")
    if not entry.is_file():
        raise SystemExit(f"Replay module is missing: {entry.relative_to(root)}")
    payload = BytesIO()
    with ZipFile(payload, "w") as app:
        write_member(app, "__main__.py", f"from {MODULE} import main\nmain()\n".encode())
        backend = root / "backend"
        for path in sorted(backend.rglob("*.py")):
            relative = path.relative_to(root)
            if (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(backend.resolve())
                    and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts)):
                write_member(app, relative.as_posix(), path.read_bytes())

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as toolkit:
        write_member(toolkit, APP, payload.getvalue())
        write_member(toolkit, RUNNER, (root / RUNNER).read_bytes().replace(b"\r\n", b"\n"), 0o755)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".deploy/nfl-oil-d-order-replay.zip")
    path = build(parser.parse_args().output)
    print(f"Built {path} ({path.stat().st_size:,} bytes).")


if __name__ == "__main__":
    main()
