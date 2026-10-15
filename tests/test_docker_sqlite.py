import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytz


ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import errno
import os
import sqlite3
import tempfile
import threading
from utils import db
from utils.channel_repository import start_run, finish_run
if 'IPTV_PROBE_PROCESS' in os.environ:
    from pathlib import Path
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    assert os.getpid() == int(os.environ['IPTV_PROBE_PROCESS'])
    for name in ('NoNewPrivs', 'Seccomp', 'Seccomp_filters'):
        assert status.get(name, '').strip() == os.environ['IPTV_PROBE_' + name]
    print('healthy process restrictions unchanged', flush=True)
print('SQLite', sqlite3.sqlite_version, flush=True)
completed = []
thread = threading.Thread(target=lambda: completed.append(True))
thread.start()
thread.join(timeout=5)
assert completed == [True]
print('thread creation OK', flush=True)
with tempfile.TemporaryFile() as file:
    assert os.pwrite(file.fileno(), b'probe', 0) == 5
    file.seek(0)
    assert file.read() == b'probe'
print('pwrite OK', flush=True)
try:
    os.getpriority(os.PRIO_PROCESS, 0)
except PermissionError as exc:
    assert exc.errno == errno.EPERM
else:
    raise AssertionError('Inherited getpriority restriction was lost')
print('inherited restrictions OK', flush=True)
with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, 'startup.db')
    run_id = start_run(path)
    finish_run(path, run_id, 'success')
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT status FROM runs').fetchone()[0] == 'success'
    db.ensure_result_data_schema(os.path.join(directory, 'playback.db'))
print('channel repository OK', flush=True)
for mode in ('WAL', 'DELETE'):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, 'probe.db')
        connection = db.get_db_connection(path, journal_mode=mode)
        try:
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.execute("INSERT INTO probe VALUES ('ok')")
            connection.commit()
            assert connection.execute('SELECT value FROM probe').fetchone()[0] == 'ok'
        finally:
            connection.close()
        print(mode, 'OK', flush=True)
with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, 'results.db')
    db.replace_result_data(path, [{'id': 'demo', 'url': 'http://example.test/live'}])
    connection = db.get_db_connection(path)
    try:
        assert connection.execute('SELECT id FROM result_data').fetchone()[0] == 'demo'
    finally:
        connection.close()
print('result_data OK', flush=True)
"""


@unittest.skipUnless(os.environ.get("IPTV_SQLITE_DOCKER_TEST") == "1", "opt-in Docker compatibility test")
class DockerSqliteTests(unittest.TestCase):
    def _images(self):
        requested_platform = os.environ.get("IPTV_SQLITE_DOCKER_PLATFORM")
        if requested_platform:
            server_arch = subprocess.check_output(
                ["docker", "info", "--format", "{{.Architecture}}"], text=True, timeout=15
            ).strip()
            native_arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(server_arch, server_arch)
            if requested_platform.split("/")[1] != native_arch:
                self.skipTest("Kernel seccomp tests require native execution, not CPU emulation")
        override = os.environ.get("IPTV_SQLITE_DOCKER_IMAGE")
        images = {override} if override else set(re.findall(
            r"^FROM\s+(python:\S+)", (ROOT / "Dockerfile").read_text(), re.MULTILINE
        ))
        self.assertTrue(images)
        return images

    def test_healthy_startup_keeps_process_restrictions_unchanged(self):
        launcher = """
import os
from pathlib import Path
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
os.environ['IPTV_PROBE_PROCESS'] = str(os.getpid())
for name in ('NoNewPrivs', 'Seccomp', 'Seccomp_filters'):
    os.environ['IPTV_PROBE_' + name] = status.get(name, '').strip()
os.execvp('python', ['python', '-m', 'utils.docker_compat', 'python', '-'])
"""
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "seccomp.json"
            profile.write_text(json.dumps({
                "defaultAction": "SCMP_ACT_ALLOW",
                "syscalls": [
                    {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
                    {"names": ["getpriority"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
                ],
            }))
            for image in sorted(self._images()):
                with self.subTest(image=image):
                    requested = os.environ.get("IPTV_SQLITE_DOCKER_PLATFORM")
                    platform_args = ["--platform", requested] if requested else []
                    result = subprocess.run([
                        "docker", "run", *platform_args, "--rm", "-i", "--network", "none",
                        "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                        "--security-opt", f"seccomp={profile}",
                        "-w", "/tmp", "-e", "PYTHONPATH=/app:/deps",
                        "--mount", f"type=bind,source={ROOT / 'utils'},target=/app/utils,readonly",
                        "--mount", f"type=bind,source={ROOT / 'config/config.ini'},target=/app/config/config.ini,readonly",
                        "--mount", f"type=bind,source={Path(pytz.__file__).parent},target=/deps/pytz,readonly",
                        "--entrypoint", "python", image, "-c", launcher,
                    ], input=PROBE, text=True, capture_output=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("healthy process restrictions unchanged", result.stdout)
                    self.assertIn("result_data OK", result.stdout)
                    self.assertNotIn("compatibility enabled", result.stderr)

    def test_sqlite_writes_when_pwritev2_is_denied(self):
        images = self._images()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "seccomp.json"
            profile.write_text(json.dumps({
                "defaultAction": "SCMP_ACT_ALLOW",
                "syscalls": [
                    {"names": ["pwritev2"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
                    {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
                    {"names": ["getpriority"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
                ],
            }))
            for image in sorted(images):
                with self.subTest(image=image):
                    platform = os.environ.get("IPTV_SQLITE_DOCKER_PLATFORM")
                    platform_args = ["--platform", platform] if platform else []
                    result = subprocess.run([
                        "docker", "run", *platform_args, "--rm", "-i", "--network", "none",
                        "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges",
                        "--security-opt", f"seccomp={profile}",
                        "-w", "/tmp", "-e", "PYTHONPATH=/app:/deps",
                        "--mount", f"type=bind,source={ROOT / 'utils'},target=/app/utils,readonly",
                        "--mount", f"type=bind,source={ROOT / 'config/config.ini'},target=/app/config/config.ini,readonly",
                        "--mount", f"type=bind,source={Path(pytz.__file__).parent},target=/deps/pytz,readonly",
                        "--entrypoint", "python", image, "-m", "utils.docker_compat", "python", "-",
                    ], input=PROBE, text=True, capture_output=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("thread creation OK", result.stdout)
                    self.assertIn("pwrite OK", result.stdout)
                    self.assertIn("inherited restrictions OK", result.stdout)
                    self.assertIn("channel repository OK", result.stdout)
                    self.assertIn("result_data OK", result.stdout)

    def test_legacy_write_denial_is_preserved(self):
        images = self._images()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "seccomp.json"
            profile.write_text(json.dumps({
                "defaultAction": "SCMP_ACT_ALLOW",
                "syscalls": [{
                    "names": ["pwrite64", "pwritev2"],
                    "action": "SCMP_ACT_ERRNO", "errnoRet": 1,
                }],
            }))
            for image in sorted(images):
                with self.subTest(image=image):
                    requested = os.environ.get("IPTV_SQLITE_DOCKER_PLATFORM")
                    platform_args = ["--platform", requested] if requested else []
                    result = subprocess.run([
                        "docker", "run", *platform_args, "--rm", "--network", "none",
                        "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                        "--security-opt", f"seccomp={profile}",
                        "-w", "/tmp", "-e", "PYTHONPATH=/app",
                        "--mount", f"type=bind,source={ROOT / 'utils'},target=/app/utils,readonly",
                        "--entrypoint", "python", image, "-m", "utils.docker_compat",
                        "python", "-c", "print('payload executed')",
                    ], text=True, capture_output=True, timeout=120)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("payload executed", result.stdout)
                    self.assertIn("PermissionError", result.stderr)


if __name__ == "__main__":
    unittest.main()
