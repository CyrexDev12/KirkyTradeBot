#!/usr/bin/env bash
# Run from the updated VM's application directory while Oil D is paused and flat.
set -euo pipefail

resume=false
if [[ $# -eq 1 && "$1" == --resume ]]; then
    resume=true
elif [[ $# -ne 0 ]]; then
    echo 'Usage: bash deploy/azure/enable-oil-d-websocket.sh [--resume]' >&2
    exit 2
fi

env_file=.secrets/wti-live.env
if [[ ! -f compose.yaml || ! -f "$env_file" || -L "$env_file" ]]; then
    echo 'Run from the updated VM application directory with the existing Oil D environment file.' >&2
    exit 1
fi

# Use the updater's lock when it exists, without creating a root-owned lock file.
if [[ -e .update.lock ]]; then
    exec 9<.update.lock
    flock -n 9 || { echo 'Another update or Oil D configuration change is running.' >&2; exit 1; }
fi

privileged=()
if [[ $EUID -ne 0 ]]; then privileged=(sudo); fi
docker=("${privileged[@]}" docker)
compose=("${docker[@]}" compose)
mode=stream_end_to_end

check_pause() {
    "${compose[@]}" exec -T wti-live-worker python - "$mode" "$resume" "${1:-}" <<'PY'
from datetime import datetime
import os
import sys
import time

from backend.wti.commodity import WTI
from backend.wti.live_store import LiveStore, live_path

mode, resume, started = sys.argv[1:]
path = live_path(WTI.directory())
if not path.is_file():
    raise SystemExit('The Oil D live journal is missing.')
status = LiveStore(path).status()
if not status['paused'] or not (status['disabled_reason'] or '').startswith('New entries stopped by '):
    raise SystemExit('A Discord operator pause is required. Run /oil-live off before changing the quote mode.')
if status['active_trade'] is not None:
    raise SystemExit('Oil D still has an active position or order. Keep the worker running until flat.')
if not started:
    raise SystemExit(0)
if os.getenv('WTI_LIVE_BOOK_MODE') != mode:
    raise SystemExit('The recreated worker has an unexpected quote-mode setting. Oil D remains paused.')

heartbeat = status['last_heartbeat']
heartbeat_at = datetime.fromisoformat(heartbeat).timestamp() if heartbeat else 0
started_at = datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
if (heartbeat_at < started_at or not 0 <= time.time() - heartbeat_at < 30
        or status.get('book_mode') != mode or not status.get('worker_configured')):
    raise SystemExit(75)  # The new worker may still be initializing.
if resume == 'true' and not status.get('entries_configured'):
    raise SystemExit('The worker configuration disables live entries. Oil D remains paused.')
PY
}

check_pause

"${privileged[@]}" python3 - "$env_file" "$mode" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

path = Path(sys.argv[1])
key = b'WTI_LIVE_BOOK_MODE'
setting = key + b'=' + sys.argv[2].encode('ascii')
with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit('The Oil D environment path must be a regular file.')
    original = source.read()

pattern = re.compile(rb'^[ \t]*(?:export[ \t]+)?' + key + rb'(?:[ \t]*[:=]|[ \t]*(?:#.*)?[\r\n]*$)')
newlines = b'\r\n' if b'\r\n' in original else b'\n'
updated = []
found = False
for line in original.splitlines(keepends=True):
    if pattern.match(line):
        if not found:
            ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
            updated.append(setting + ending)
            found = True
    else:
        updated.append(line)
if not found:
    if original and not original.endswith(b'\n'):
        updated.append(newlines)
    updated.append(setting + newlines)
content = b''.join(updated)
if content == original:
    raise SystemExit(0)

descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
try:
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
        output.flush()
        os.fchown(output.fileno(), info.st_uid, info.st_gid)
        os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
        os.fsync(output.fileno())
    os.replace(temporary, path)
finally:
    Path(temporary).unlink(missing_ok=True)
PY

"${compose[@]}" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 90 wti-live-worker
container="$("${compose[@]}" ps --quiet wti-live-worker)"
if [[ -z "$container" ]]; then
    echo 'The recreated Oil D worker could not be found. Oil D remains paused.' >&2
    exit 1
fi
started="$("${docker[@]}" inspect --format '{{.State.StartedAt}}' "$container")"
if [[ -z "$started" ]]; then
    echo 'The recreated Oil D worker startup time could not be checked. Oil D remains paused.' >&2
    exit 1
fi

verified=false
for ((attempt=0; attempt<15; attempt++)); do
    if check_pause "$started"; then
        verified=true
        break
    else
        result=$?
        if [[ "$result" -ne 75 ]]; then exit "$result"; fi
    fi
    sleep 1
done
if [[ "$verified" != true ]]; then
    echo 'No fresh Oil D heartbeat confirmed stream_end_to_end. Oil D remains paused.' >&2
    exit 1
fi

echo 'Oil D worker confirmed stream_end_to_end in a fresh heartbeat while paused and flat.'
if [[ "$resume" == true ]]; then
    "${compose[@]}" exec -T wti-live-worker python -m backend.wti.live_worker --resume-operator-pause
fi
