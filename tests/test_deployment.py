"""Deployment packaging and failure-path checks; no Azure/Docker access required."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from deploy.package import build, source_files

ROOT = Path(__file__).resolve().parents[1]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


def shell_path(path):
    value = str(path).replace("\\", "/")
    return "/" + value[0].lower() + value[2:] if len(value) > 1 and value[1] == ":" else value


def test_release_excludes_private_files_and_has_portable_permissions(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("README.md", "requirements.txt", "pytest.ini", "Dockerfile", "compose.yaml", ".dockerignore", ".env.example"):
        (root / name).write_text("public")
    for directory in ("backend", "bot", "frontend", "deploy", "tests", "scripts", "docs"):
        (root / directory).mkdir()
    (root / "deploy/test.sh").write_bytes(b"#!/bin/bash\r\necho ready\r\n")
    (root / "bot/.env").write_text("PRIVATE")
    (root / "bot/.secrets").mkdir()
    (root / "bot/.secrets/token").write_text("PRIVATE")
    (root / "backend/__pycache__").mkdir()
    (root / "backend/__pycache__/test.pyc").write_bytes(b"PRIVATE")
    for suffix in ("zip", "tar.gz"):
        output = tmp_path / ("release." + suffix)
        build(output, root)
        if suffix == "zip":
            with zipfile.ZipFile(output) as package:
                assert package.testzip() is None
                assert package.read("deploy/test.sh") == b"#!/bin/bash\necho ready\n"
                assert package.getinfo("deploy/test.sh").external_attr >> 16 & 0o777 == 0o755
                assert package.getinfo("deploy/").external_attr >> 16 & 0o777 == 0o755
                assert all(b"PRIVATE" not in package.read(name) for name in package.namelist())
        else:
            with tarfile.open(output) as package:
                assert package.getmember("deploy").mode == 0o755
                assert package.getmember("deploy/test.sh").mode == 0o755
                assert all(b"PRIVATE" not in package.extractfile(m).read() for m in package if m.isfile())


MOCK_DOCKER = r'''
import json, os, pathlib, sys, tarfile
sys.stdout.reconfigure(newline='\n')
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as log: log.write(json.dumps(args) + '\n')
failure = os.environ.get('MOCK_FAILURE', '')
def path(value):
    if os.name == 'nt' and len(value) > 2 and value[0] == '/' and value[2] == '/':
        value = value[1].upper() + ':' + value[2:]
    return pathlib.Path(value)
if args[0] == 'inspect':
    if args[2] == '{{.Image}}': print('sha256:old-backend')
    elif '/data' in args[2]: print('nfl-fair-value_predictions')
    else: print('nfl-fair-value_bot-state')
elif args[0] == 'image': pass
elif args[0] == 'run':
    if '--entrypoint' in args:
        assert '--network' in args and args[args.index('--network')+1] == 'none'
        assert '--mount' not in args and '--read-only' in args
        assert 'os.getuid() == 10001' in args[-1]
        sys.exit(29 if failure == 'imports' else 0)
    if failure == 'backup': sys.exit(23)
    mounts = [args[i+1] for i, a in enumerate(args) if a == '--mount']
    backup = path(next(m for m in mounts if 'type=bind' in m).split('source=')[1].split(',')[0])
    volume = args[-1]
    with tarfile.open(backup / (volume + '.tar.gz'), 'w:gz') as package:
        package.add(path(os.environ['MOCK_VOLUME']), arcname='.')
elif args[0] == 'compose':
    actions = {'config', 'ps', 'build', 'stop', 'up', 'exec', 'logs'}
    index = next(i for i, value in enumerate(args) if value in actions)
    action, options = args[index], args[index+1:]
    rollback = any('rollback-images.yaml' in a for a in args)
    if action == 'config' and '--services' in options: print('backend\nbot')
    elif action == 'ps' and '-q' in options: print('old-' + options[-1])
    elif action == 'build' and failure == 'build': sys.exit(17)
    elif action == 'up' and failure == 'activation' and not rollback: sys.exit(19)
    elif action == 'exec':
        if 'raise SystemExit(not uncertainty_profiles_current())' in options[-1]:
            sys.exit(1 if failure in ('profiles', 'missing-profiles') else 0)
        if 'backend.trading.cli' in options and failure == 'profiles': sys.exit(21)
        if 'bot' in options:
            assert 'api.paper_outbox()' in options[-1], 'Operator health check must send authentication'
else: raise SystemExit('Unexpected Docker command: ' + repr(args))
'''


@pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash is required")
@pytest.mark.parametrize("failure", ["", "build", "imports", "backup", "activation", "profiles", "missing-profiles", "new-secret"])
def test_vm_update_preserves_secrets_and_recovers_on_failure(tmp_path, failure):
    home = tmp_path / "vm"
    app = home / "nfl-fair-value"
    app.mkdir(parents=True)
    (app / "backend").mkdir()
    (app / "backend/old.py").write_text("old release")
    (app / "bot").mkdir()
    (app / "compose.yaml").write_text("name: nfl-fair-value\nservices:\n  backend: {}\n  bot: {}\n")
    (app / ".env").write_text("DISCORD_GUILD_ID=123\n")
    (app / ".secrets").mkdir()
    (app / ".secrets/discord_token.txt").write_text("existing-private-discord-value")
    if failure != 'new-secret':
        (app / ".secrets/paper_operator_token.txt").write_text("existing-private-operator-value")
    volume = tmp_path / "live-volume"
    volume.mkdir()
    (volume / "preserved-state").write_text("prediction and delivery history")
    package = tmp_path / "update.tar.gz"
    build(package)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_python = tmp_path / "docker.py"
    docker_python.write_text(MOCK_DOCKER)
    # Git Bash can run the Windows Python executable with native path conversion.
    import sys
    python = shell_path(sys.executable)
    commands = {
        'python3': f'exec "{python}" "$@"',
        'sudo': 'if [[ "${1:-}" == -n ]]; then shift; fi\nexec "$@"',
        'flock': 'exit 0',
        'chown': 'exit 0',
        'openssl': 'printf generated-private-operator-value',
        'docker': f'exec "{python}" "{shell_path(docker_python)}" "$@"',
        'curl': 'printf \'{"status":"ok"}\'',
    }
    for name, command in commands.items():
        target = bin_dir / name
        target.write_text('#!/usr/bin/env bash\n' + command + '\n', newline='\n')
        target.chmod(0o755)
    log = tmp_path / "docker.log"
    env = dict(os.environ, MOCK_LOG=str(log), MOCK_FAILURE=failure, MOCK_VOLUME=str(volume))
    env['NFL_DEPLOY_HOME'] = shell_path(home)
    command = f'export PATH="{shell_path(bin_dir)}:$PATH"; bash deploy/azure/update-on-vm.sh "{shell_path(package)}"'
    result = subprocess.run([str(BASH), '-c', command], cwd=ROOT, env=env, text=True, capture_output=True)
    expected = 0 if failure in ('', 'missing-profiles', 'new-secret') else {'build': 17, 'imports': 29, 'backup': 23, 'activation': 19, 'profiles': 21}[failure]
    assert result.returncode == expected, result.stdout + result.stderr
    assert Path(str(package) + '.status').read_text().strip() == str(expected)
    assert (app / '.secrets/discord_token.txt').read_text() == 'existing-private-discord-value'
    expected_operator = 'generated-private-operator-value' if failure == 'new-secret' else 'existing-private-operator-value'
    assert (app / '.secrets/paper_operator_token.txt').read_text() == expected_operator
    assert 'private-discord-value' not in result.stdout + result.stderr
    assert 'private-operator-value' not in result.stdout + result.stderr
    assert (app / '.env').read_text() == 'DISCORD_GUILD_ID=123\n'
    assert (volume / 'preserved-state').read_text() == 'prediction and delivery history'
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any('down' in call or 'prune' in call for call in calls)
    rollback = any('up' in call and any('rollback-images.yaml' in a for a in call) for call in calls)
    assert rollback == (failure in ('backup', 'activation', 'profiles'))
    if expected:
        assert (app / 'backend/old.py').read_text() == 'old release'
    else:
        assert not (app / 'backend/old.py').exists()
        assert (app / 'backend/trading/worker.py').exists()
        assert (app / 'backend/crypto/worker.py').exists()
        assert any('up' in call and 'crypto-worker' in call and '--wait' in call for call in calls)
        assert any('/api/players/projection' in call[-1] and '/api/graphs/team-points' in call[-1]
                   for call in calls if call[0] == 'compose' and 'exec' in call)
        assert 'Update complete.' in result.stdout
    if failure in ('build', 'imports'):
        assert not any('stop' in call for call in calls)
        assert not any('up' in call for call in calls)
    else:
        import_checks = [i for i, call in enumerate(calls) if call[0] == 'run' and '--entrypoint' in call]
        assert len(import_checks) == 3
        assert max(import_checks) < next(i for i, call in enumerate(calls) if 'stop' in call)
        backend_check = calls[import_checks[0]][-1]
        assert 'Desk(None); Desk(None, commodity=NATGAS)' in backend_check
    if failure not in ('build', 'imports', 'backup'):
        backups = list((home / 'nfl-backups').glob('*/nfl-fair-value_bot-state.tar.gz'))
        assert len(backups) == 1
        with tarfile.open(backups[0]) as backup:
            assert backup.extractfile('./preserved-state').read() == b'prediction and delivery history'
    if failure != 'build':
        import_commands = [call[-1] for call in calls if call[0] == 'run' and '--entrypoint' in call]
        assert any('backend.updown.worker' in code and 'backend.gold.worker' in code
                   and 'backend.wti.worker' in code and 'backend.natgas.worker' in code
                   for code in import_commands)


@pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash is required")
@pytest.mark.parametrize("option", ["", "--skip-ssh-rule"])
def test_cloud_shell_wrapper_updates_existing_vm_without_redeployment(tmp_path, option):
    import sys
    root = tmp_path / 'project'
    (root / 'deploy/azure').mkdir(parents=True)
    for filename in ('package.py', 'azure/update.sh', 'azure/update-on-vm.sh'):
        shutil.copyfile(ROOT / 'deploy' / filename, root / 'deploy' / filename)
    for name in ('README.md', 'requirements.txt', 'pytest.ini', 'Dockerfile', 'compose.yaml', '.dockerignore', '.env.example'):
        (root / name).write_text('public')
    key = tmp_path / 'key'
    key.write_text('test-only-key')
    log = tmp_path / 'commands.log'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    commands = {
        'python3': f'exec "{shell_path(sys.executable)}" "$@"',
        'curl': 'printf 198.51.100.4',
        'az': '''printf 'az %s\\n' "$*" >> "$MOCK_COMMAND_LOG"
case "$1 $2" in
  'account set') ;;
  'vm show')
    if [[ "$*" == *osProfile.adminUsername* ]]; then printf azureuser; else printf 'nfl-fair-value mexicocentral Standard_B2s_v2'; fi ;;
  'network public-ip') printf 203.0.113.20 ;;
  'network nsg') [[ "$*" == *'rule update'* ]] ;;
  *) exit 99 ;;
esac''',
        'ssh': '''printf 'ssh %s\\n' "$*" >> "$MOCK_COMMAND_LOG"
if [[ "$*" == *nohup* ]]; then printf 12345; fi''',
        'scp': 'printf \'scp %s\\n\' "$*" >> "$MOCK_COMMAND_LOG"',
    }
    for name, command in commands.items():
        target = bin_dir / name
        target.write_text('#!/usr/bin/env bash\n' + command + '\n', newline='\n')
        target.chmod(0o755)
    env = dict(os.environ, NFL_SSH_KEY=shell_path(key), MOCK_COMMAND_LOG=shell_path(log))
    for name in ('NFL_RESOURCE_GROUP', 'NFL_VM_NAME'):
        env.pop(name, None)
    command = f'export PATH="{shell_path(bin_dir)}:$PATH"; bash deploy/azure/update.sh test-subscription {option}'
    result = subprocess.run([str(BASH), '-c', command], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert ('rule update' in calls) == (not option)
    assert 'deployment group create' not in calls and 'group create' not in calls
    assert 'nohup bash' in calls and 'tail --pid=12345' in calls
    assert 'azureuser@203.0.113.20' in calls
