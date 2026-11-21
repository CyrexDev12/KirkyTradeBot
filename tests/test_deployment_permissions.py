"""Linux regression for extraction under the updater's private umask.

Can also run without pytest: python3 tests/test_deployment_permissions.py.
Run as root on Linux to additionally exercise imports as container UID 10001.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.package import build


@unittest.skipUnless(os.name == "posix", "Requires Linux/Unix permission semantics")
class ReleasePermissionsTest(unittest.TestCase):
    def test_private_extraction_keeps_nested_source_readable(self):
        extractor = (ROOT / "deploy/azure/update-on-vm.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="nfl-permissions-") as directory:
            work = Path(directory)
            work.chmod(0o755)
            archive = work / "release.tar.gz"
            build(archive)
            stage = work / "stage"
            stage.mkdir(mode=0o700)
            subprocess.run([sys.executable, "-c", "import os; os.umask(0o077)\n" + extractor,
                            str(archive), str(stage)], check=True, capture_output=True, text=True)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            for path in stage.rglob("*"):
                mode = path.stat().st_mode & 0o777
                if path.is_dir():
                    self.assertEqual(mode, 0o755, str(path.relative_to(stage)))
                else:
                    self.assertEqual(mode & 0o444, 0o444, str(path.relative_to(stage)))
            self.assertFalse((stage / ".secrets").exists())

            # Emulate COPY's root-owned code tree, including nested permissions.
            image = work / "image"
            image.mkdir(mode=0o755)
            shutil.copytree(stage / "backend", image / "backend")
            (image / "backend").chmod(0o755)
            identity = {"user": 10001, "group": 10001, "extra_groups": []} if os.geteuid() == 0 else {}
            check = subprocess.run([
                sys.executable, "-c",
                "import importlib.util; from pathlib import Path; "
                "spec=importlib.util.find_spec('backend.trading.api'); assert spec is not None; "
                "compile(Path(spec.origin).read_text(), spec.origin, 'exec')"
            ], cwd=image, capture_output=True, text=True, **identity)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
