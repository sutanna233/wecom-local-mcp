from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent


FAKE_PYTHON = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = Path(os.environ["FAKE_PYTHON_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"cwd": os.getcwd(), "args": args}, ensure_ascii=False) + "\n")

if args[:1] == ["-c"]:
    raise SystemExit(0)
if not args:
    raise SystemExit(2)
script = Path(args[0]).name
if script == "find_wxwork_keys.py":
    Path(os.environ["FAKE_KEY_FILE"]).write_text("{}", encoding="utf-8")
    raise SystemExit(0)
if script == "wecom_collector.py":
    if "--watch" in args:
        print("synthetic watcher stdout")
        print("synthetic watcher stderr", file=sys.stderr)
        raise SystemExit(0)
    marker = Path(os.environ["FAKE_FAILURE_MARKER"])
    if os.environ.get("FAKE_FAIL_ONCE") == "1" and not marker.exists():
        marker.write_text("failed", encoding="utf-8")
        print(os.environ["FAKE_FAILURE_TEXT"], file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
raise SystemExit(0)
"""


class StaticDeploymentTests(unittest.TestCase):
    def test_powershell_source_is_ascii_only(self):
        source = (ROOT / "deploy_wecom.ps1").read_bytes()
        self.assertFalse(source.startswith(b"\xef\xbb\xbf"))
        source.decode("ascii", errors="strict")
        text = source.decode("ascii")
        self.assertIn("ConvertFrom-CodePoints", text)
        self.assertIn("0x542F, 0x52A8, 0x6D88, 0x606F, 0x91C7, 0x96C6", text)
        self.assertIn("0x542F, 0x52A8, 0x53EA, 0x8BFB", text)

    def test_deploy_entry_changes_to_its_own_directory(self):
        raw = (ROOT / "01_一键部署.cmd").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        lines = raw.decode("ascii", errors="strict").splitlines()
        self.assertEqual(lines[0], "@echo off")
        self.assertEqual(lines[1], "setlocal")
        self.assertIn('cd /d "%~dp0"', lines)
        text = "\n".join(lines)
        self.assertIn('setup_venv.py" --offline', text)
        self.assertIn('set "PIP_NO_INDEX=1"', text)
        self.assertIn('.venv\\Scripts\\python.exe', text)
        self.assertIn('-PythonPath "%VENV_PYTHON%"', text)
        self.assertIn('exit /b %DEPLOY_EXIT%', text)
        self.assertNotIn("pip install", text)

    def test_examples_are_strict_json_without_machine_paths_or_bom(self):
        for name in ("mcp_config.example.json", "wecom_config.example.json"):
            raw = (ROOT / name).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), name)
            value = json.loads(raw.decode("utf-8", errors="strict"))
            serialized = json.dumps(value, ensure_ascii=False)
            self.assertNotIn(str(ROOT), serialized, name)
            self.assertNotIn("C:\\Python", serialized, name)
            self.assertNotIn("F:\\work", serialized, name)
        wecom = json.loads((ROOT / "wecom_config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(wecom["wxwork_keys_file"], "wxwork_keys.json")
        self.assertEqual(wecom["wxwork_decrypted_dir"], "wxwork_decrypted")
        self.assertEqual(wecom["wxwork_export_dir"], "wxwork_export")

    def test_requirements_only_cover_the_packaged_runtime(self):
        self.assertEqual(
            (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="strict"),
            "pycryptodome==4.2.0\n",
        )

    def test_package_selfcheck_uses_strict_utf8_and_data_assertions(self):
        source = (ROOT / "package_zip.py").read_text(encoding="utf-8", errors="strict")
        self.assertNotIn('errors="replace"', source)
        self.assertIn('errors="strict"', source)
        self.assertIn("SEED_SYNTHETIC_DB", source)
        self.assertIn("SYNTHETIC_CONTENT", source)
        self.assertIn('full["content"] == SYNTHETIC_CONTENT', source)
        self.assertIn("verify_offline_venv(extracted)", source)
        self.assertIn("NETWORK_GUARD", source)
        self.assertIn('"wheels/pycryptodome-4.2.0-cp39-abi3-win_amd64.whl"', source)

    def deploy_source(self) -> str:
        return (ROOT / "deploy_wecom.ps1").read_text(encoding="ascii", errors="strict")

    def test_deploy_prefers_venv_and_never_installs_from_network(self):
        source = self.deploy_source()
        self.assertIn('Test-Path $VenvPython -PathType Leaf', source)
        self.assertIn('Invoke-Python @("configure_wecom.py", "--config", $ConfigPath)', source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("https://pypi", source)
        self.assertNotIn("Invoke-WebRequest", source)

    def test_local_wheel_has_original_abi3_tag_and_native_modules(self):
        wheel = ROOT / "wheels" / "pycryptodome-4.2.0-cp39-abi3-win_amd64.whl"
        with zipfile.ZipFile(wheel) as archive:
            self.assertIsNone(archive.testzip())
            metadata = archive.read("pycryptodome-4.2.0.dist-info/WHEEL").decode("ascii")
            self.assertIn("Tag: cp39-abi3-win_amd64", metadata)
            self.assertEqual(sum(name.endswith(".pyd") for name in archive.namelist()), 53)


class ConfigureWecomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wecom_configure_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.documents = self.root / "Documents"
        self.config = self.root / "package" / "config.json"

    def make_data(self, account: str, mtime: int) -> Path:
        data = self.documents / "WXWork" / account / "Data"
        data.mkdir(parents=True)
        database = data / "message.db"
        database.write_bytes(b"synthetic encrypted database")
        os.utime(database, (mtime, mtime))
        os.utime(data, (mtime, mtime))
        return data

    def test_placeholder_config_uses_most_recent_common_data_directory(self):
        import configure_wecom

        self.make_data("older-account", 1_700_000_000)
        newest = self.make_data("newer-account", 1_800_000_000)
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({
            "wxwork_db_dir": "D:\\Path\\To\\WXWork\\ACCOUNT_ID\\Data",
            "custom_setting": "preserved",
        }), encoding="utf-8")
        selected, detected = configure_wecom.ensure_config(
            self.config, environ={}, documents=self.documents
        )
        self.assertTrue(detected)
        self.assertEqual(selected, newest.resolve())
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["wxwork_db_dir"], str(newest.resolve()))
        self.assertEqual(saved["custom_setting"], "preserved")
        self.assertEqual(saved["wxwork_keys_file"], "wxwork_keys.json")

    def test_existing_valid_config_wins_over_detection(self):
        import configure_wecom

        existing = self.make_data("configured-account", 1_700_000_000)
        self.make_data("other-account", 1_800_000_000)
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"wxwork_db_dir": str(existing)}), encoding="utf-8")
        selected, detected = configure_wecom.ensure_config(
            self.config, environ={}, documents=self.documents
        )
        self.assertFalse(detected)
        self.assertEqual(selected, existing.resolve())

    def test_explicit_environment_data_directory_is_supported(self):
        import configure_wecom

        direct = self.root / "custom" / "Data"
        direct.mkdir(parents=True)
        selected, detected = configure_wecom.ensure_config(
            self.config,
            environ={"WECOM_DATA_DIR": str(direct)},
            documents=self.documents,
        )
        self.assertTrue(detected)
        self.assertEqual(selected, direct.resolve())


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "requires Windows PowerShell 5.1")
class PowerShellDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wecom_deploy_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "部署目录"
        self.vendor = self.root / "third_party" / "wecom-decrypt"
        self.vendor.mkdir(parents=True)
        self.source_db_dir = self.root / "source-data"
        self.source_db_dir.mkdir()
        shutil.copy2(ROOT / "deploy_wecom.ps1", self.root / "deploy_wecom.ps1")
        self.key_path = self.vendor / "wxwork_keys.json"
        self.config_path = self.vendor / "config.json"
        self.config_path.write_text(json.dumps({
            "wxwork_db_dir": str(self.source_db_dir),
            "wxwork_keys_file": self.key_path.name,
        }), encoding="utf-8")
        self.log_path = self.root / "fake-python.jsonl"
        self.marker_path = self.root / "failure.marker"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        fake_script = self.fake_bin / "fake_python.py"
        fake_script.write_text(FAKE_PYTHON, encoding="utf-8")
        (self.fake_bin / "python.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake_python.py" %*\r\n',
            encoding="ascii",
        )

    def run_deploy(self, failure_text: str, start_watcher: bool = False) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": str(self.fake_bin) + os.pathsep + env.get("PATH", ""),
            "FAKE_PYTHON_LOG": str(self.log_path),
            "FAKE_KEY_FILE": str(self.key_path),
            "FAKE_FAILURE_MARKER": str(self.marker_path),
            "FAKE_FAIL_ONCE": "1",
            "FAKE_FAILURE_TEXT": failure_text,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(self.root / "deploy_wecom.ps1"),
            "-ConfigPath", str(self.config_path),
        ]
        if start_watcher:
            command.append("-StartWatcher")
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )

    def records(self, script: str, option: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["args"] and Path(row["args"][0]).name == script
                and (option is None or option in row["args"])]

    def test_workdir_key_retry_launchers_utf8_and_hidden_logs(self):
        result = self.run_deploy("wxSQLite3 AES-128 key validation failed", start_watcher=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        key_runs = self.records("find_wxwork_keys.py")
        collector_runs = self.records("wecom_collector.py", "--once")
        self.assertEqual(len(key_runs), 2, result.stdout + result.stderr)
        self.assertEqual(len(collector_runs), 2, result.stdout + result.stderr)
        for row in key_runs:
            self.assertEqual(
                os.path.normcase(os.path.realpath(row["cwd"])),
                os.path.normcase(os.path.realpath(self.vendor)),
            )
        for row in collector_runs:
            self.assertEqual(
                os.path.normcase(os.path.realpath(row["cwd"])),
                os.path.normcase(os.path.realpath(self.root)),
            )

        collector_launcher = self.root / "02_启动消息采集.cmd"
        mcp_launcher = self.root / "03_启动只读MCP.cmd"
        self.assertTrue(collector_launcher.is_file())
        self.assertTrue(mcp_launcher.is_file())
        for launcher in (collector_launcher, mcp_launcher):
            raw = launcher.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8", errors="strict")
            self.assertIn('cd /d "%~dp0"', text)
            self.assertIn('set "PYTHONUTF8=1"', text)
            self.assertIn('set "PYTHONIOENCODING=utf-8"', text)

        config_raw = (self.root / "mcp_config.generated.json").read_bytes()
        self.assertFalse(config_raw.startswith(b"\xef\xbb\xbf"))
        config = json.loads(config_raw.decode("utf-8", errors="strict"))
        server = config["mcpServers"]["wecom-local-messages"]
        self.assertEqual(server["env"]["PYTHONUTF8"], "1")
        self.assertEqual(server["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(server["env"]["WECOM_MCP_READ_ONLY"], "1")

        stdout_log = self.root / "data" / "collector.stdout.log"
        stderr_log = self.root / "data" / "collector.stderr.log"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (stdout_log.exists() and stderr_log.exists()):
            time.sleep(0.05)
        self.assertTrue(stdout_log.is_file(), result.stdout + result.stderr)
        self.assertTrue(stderr_log.is_file(), result.stdout + result.stderr)
        self.assertIn("Collector stdout log:", result.stdout)
        self.assertIn("Collector stderr log:", result.stdout)
        self.assertIn("collector.stdout.log", result.stdout)
        self.assertIn("collector.stderr.log", result.stdout)

    def test_non_key_failure_does_not_retry_key_extraction(self):
        result = self.run_deploy("synthetic collector failure")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records("find_wxwork_keys.py")), 1)
        self.assertEqual(len(self.records("wecom_collector.py", "--once")), 1)
        self.assertNotIn("refreshing the local key once", result.stdout)


FAKE_SETUP = r"""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
Path(os.environ["FAKE_SETUP_LOG"]).write_text(
    json.dumps({"args": sys.argv[1:]}), encoding="utf-8"
)
if os.environ.get("FAKE_SETUP_EXIT") != "0":
    raise SystemExit(int(os.environ["FAKE_SETUP_EXIT"]))
python = root / ".venv" / "Scripts" / "python.exe"
python.parent.mkdir(parents=True, exist_ok=True)
python.write_text("synthetic launcher", encoding="ascii")
"""

FAKE_DEPLOY = r"""param(
    [string]$PythonPath = "",
    [string]$OfflineDb = ""
)
$Payload = @{ python = $PythonPath; offline_db = $OfflineDb } | ConvertTo-Json
[IO.File]::WriteAllText($env:FAKE_DEPLOY_LOG, $Payload, [Text.UTF8Encoding]::new($false))
exit [int]$env:FAKE_DEPLOY_EXIT
"""


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "requires Windows BAT and PowerShell")
class BatchLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wecom_bat_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "离线 部署"
        self.root.mkdir()
        shutil.copy2(ROOT / "01_一键部署.cmd", self.root / "01_一键部署.cmd")
        (self.root / "setup_venv.py").write_text(FAKE_SETUP, encoding="utf-8")
        (self.root / "deploy_wecom.ps1").write_text(FAKE_DEPLOY, encoding="ascii")
        self.setup_log = self.root / "setup.json"
        self.deploy_log = self.root / "deploy.json"

    def run_bat(self, *args: str, setup_exit: int = 0, deploy_exit: int = 0):
        env = os.environ.copy()
        env.update({
            "WECOM_DEPLOY_NO_PAUSE": "1",
            "FAKE_SETUP_LOG": str(self.setup_log),
            "FAKE_DEPLOY_LOG": str(self.deploy_log),
            "FAKE_SETUP_EXIT": str(setup_exit),
            "FAKE_DEPLOY_EXIT": str(deploy_exit),
        })
        return subprocess.run(
            [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c",
             str(self.root / "01_一键部署.cmd"), *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )

    def test_bat_creates_venv_offline_and_forwards_arguments(self):
        result = self.run_bat("-OfflineDb", "checked.sqlite")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        setup = json.loads(self.setup_log.read_text(encoding="utf-8"))
        deploy = json.loads(self.deploy_log.read_text(encoding="utf-8"))
        self.assertEqual(setup["args"], ["--offline"])
        self.assertEqual(
            os.path.normcase(deploy["python"]),
            os.path.normcase(str(self.root / ".venv" / "Scripts" / "python.exe")),
        )
        self.assertEqual(deploy["offline_db"], "checked.sqlite")

    def test_bat_propagates_deploy_exit_code(self):
        result = self.run_bat(deploy_exit=23)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)

    def test_bat_stops_when_offline_setup_fails(self):
        result = self.run_bat(setup_exit=9)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.deploy_log.exists())


@unittest.skipUnless(
    os.name == "nt" and sys.maxsize > 2**32,
    "requires 64-bit Windows Python for the packaged abi3 wheel",
)
class OfflineWheelTests(unittest.TestCase):
    def test_isolated_install_with_socket_access_blocked(self):
        import package_zip

        with tempfile.TemporaryDirectory(prefix="wecom_isolated_wheel_test_") as td:
            temporary = Path(td)
            guard = temporary / "guard"
            guard.mkdir()
            (guard / "sitecustomize.py").write_text(
                package_zip.NETWORK_GUARD, encoding="utf-8"
            )
            destination = temporary / "isolated venv"
            env = os.environ.copy()
            env.update({
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": str(guard),
                "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            })
            result = subprocess.run(
                [sys.executable, str(ROOT / "setup_venv.py"),
                 "--dest", str(destination), "--offline"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PYCRYPTODOME=OK", result.stdout)
            python = destination / "Scripts" / "python.exe"
            verified = subprocess.run(
                [python, "-I", "-c",
                 "import sys, Crypto; from Crypto.Cipher import AES; "
                 "assert Crypto.__version__ == '4.2.0'; "
                 "assert sys.prefix.lower() in Crypto.__file__.lower(); "
                 "assert AES.block_size == 16"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)


class PackageRegressionTests(unittest.TestCase):
    def test_package_build_and_synthetic_mcp_selfcheck_in_temp_dir(self):
        import package_zip

        with tempfile.TemporaryDirectory(prefix="wecom_package_regression_") as td:
            output = Path(td) / "test-package.zip"
            with mock.patch.object(package_zip, "OUT", output):
                package_zip.main()
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
