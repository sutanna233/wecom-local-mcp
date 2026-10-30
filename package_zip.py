"""Build and verify a clean deployment ZIP without messages or key material."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "wecom-local-mcp-v1.4.0.zip"
PREFIX = "wecom-local-mcp"

FILES = [
    "README.md",
    "TECHNICAL.md",
    "deploy_wecom.ps1",
    "01_一键部署.cmd",
    "setup_venv.py",
    "configure_wecom.py",
    "skills/wecom-local-mcp/SKILL.md",
    "skills/wecom-local-mcp/references/usage.md",
    "skills/wecom-local-mcp/references/qoder.md",
    "skills/wecom-local-mcp/references/workbuddy.md",
    "skills/wecom-local-mcp/references/qoder.mcp.json",
    "skills/wecom-local-mcp/references/workbuddy.mcp.json",
    "skills/wecom-local-mcp/scripts/verify_agent_deploy.py",
    "wheels/pycryptodome-4.2.0-cp39-abi3-win_amd64.whl",
    "wecom_mcp.py",
    "wecom_webui.py",
    "wecom_collector.py",
    "wecom_snapshot.py",
    "prepare_offline_db.py",
    "store.py",
    "test_pipeline.py",
    "test_collector_regressions.py",
    "test_snapshot_regressions.py",
    "test_snapshot_performance.py",
    "test_store_performance.py",
    "test_mcp_regressions.py",
    "test_mcp_performance.py",
    "test_webui.py",
    "test_deploy_regressions.py",
    "test_mcp.py",
    "requirements.txt",
    "mcp_config.example.json",
    "wecom_config.example.json",
    "third_party/wecom-decrypt/config.py",
    "third_party/wecom-decrypt/find_wxwork_keys.py",
    "third_party/wecom-decrypt/decrypt_wxwork_db.py",
    "third_party/wecom-decrypt/export_wxwork_messages.py",
    "third_party/wecom-decrypt/key_scan_common.py",
    "third_party/wecom-decrypt/key_utils.py",
    "third_party/wecom-decrypt/wxwork_crypto.py",
    "third_party/wecom-decrypt/tests/test_wxwork_crypto.py",
    "third_party/wecom-decrypt/README.md",
]

GENERIC_CONFIG = {
    "wxwork_db_dir": "D:\\Path\\To\\WXWork\\ACCOUNT_ID\\Data",
    "wxwork_keys_file": "wxwork_keys.json",
    "wxwork_decrypted_dir": "wxwork_decrypted",
    "wxwork_export_dir": "wxwork_export",
}

NOTICE = r"""WeCom Local MCP deployment package

No encryption keys, chat databases, decrypted snapshots, exports, logs,
screenshots, or WXWork binaries are included.

Run 01_一键部署.cmd. It creates .venv, installs pycryptodome exclusively from
wheels/ with --no-index, detects common WXWork Data paths, writes the collector
config, and performs the initial sync. If detection fails, set WECOM_DATA_DIR
or pass a configuration explicitly. For a checked offline database, run
01_一键部署.cmd -OfflineDb path\to\wecom_messages.sqlite through the same entry.

Third-party WeCom decryption/export source:
https://github.com/lyrcsjy/wechat-decrypt
"""

SYNTHETIC_CONTENT = "package-self-test-合成消息内容"
SEED_SYNTHETIC_DB = r"""
import sys
from store import Store

store = Store(sys.argv[1])
try:
    saved = store.save_message({
        "msgid": "wxwork:message_table:package-self-test",
        "seq": 1,
        "roomid": "R:package-self-test",
        "from": "package-sender",
        "msgtime": 1700000000,
        "msgtype": 1,
        "content": "package-self-test-合成消息内容",
        "raw": {
            "conversation": "package-self-test-conversation",
            "sender_id": "package-sender-id",
            "time": "2023-11-14 22:13:20",
        },
    })
    assert saved
finally:
    store.close()
"""


NETWORK_GUARD = r"""import socket

def _offline(*args, **kwargs):
    raise RuntimeError("network access is disabled during package self-test")

socket.create_connection = _offline
socket.getaddrinfo = _offline
socket.socket.connect = _offline
socket.socket.connect_ex = _offline
"""


def utf8_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(updates)
    return env


def run_strict_utf8(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        encoding="utf-8",
        errors="strict",
        **kwargs,
    )


def rpc_request(rid: int, method: str, params: dict | None = None) -> str:
    request: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        request["params"] = params
    return json.dumps(request, ensure_ascii=False)


def verify_extracted_mcp(extracted: Path) -> None:
    synthetic_db = extracted / "data" / "package_self_test.sqlite"
    run_strict_utf8(
        [sys.executable, "-c", SEED_SYNTHETIC_DB, str(synthetic_db)],
        cwd=extracted,
        env=utf8_env(),
        capture_output=True,
        timeout=30,
        check=True,
    )
    requests = "\n".join([
        rpc_request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
        rpc_request(2, "tools/list"),
        rpc_request(3, "tools/call", {
            "name": "search_messages",
            "arguments": {"query": "package-self-test", "limit": 5},
        }),
        rpc_request(4, "tools/call", {
            "name": "get_message",
            "arguments": {"msgid": "wxwork:message_table:package-self-test"},
        }),
        rpc_request(5, "tools/call", {
            "name": "list_conversations",
            "arguments": {"query": "package-self-test", "limit": 5},
        }),
    ]) + "\n"
    process = run_strict_utf8(
        [sys.executable, "wecom_mcp.py"],
        cwd=extracted,
        env=utf8_env(WECOM_MCP_DB=str(synthetic_db), WECOM_MCP_READ_ONLY="1"),
        input=requests,
        capture_output=True,
        timeout=30,
        check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(responses) == 5, responses
    by_id = {response["id"]: response for response in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "wecom-local-messages"
    tool_names = {tool["name"] for tool in by_id[2]["result"]["tools"]}
    assert {"search_messages", "get_message", "list_conversations", "recent_messages"} <= tool_names
    assert "sync_messages" not in tool_names

    def structured(rid: int) -> dict:
        result = by_id[rid]["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]

    found = structured(3)
    assert found["count"] == 1, found
    assert found["messages"][0]["content"] == SYNTHETIC_CONTENT, found
    full = structured(4)
    assert full["content"] == SYNTHETIC_CONTENT, full
    assert full["msgid"] == "wxwork:message_table:package-self-test", full
    conversations = structured(5)
    assert conversations["count"] == 1, conversations
    assert conversations["conversations"][0]["conversation"] == "package-self-test-conversation"

    generated = {
        "mcpServers": {
            "wecom-local-messages": {
                "command": sys.executable,
                "args": [str(extracted / "wecom_mcp.py")],
                "env": {
                    "WECOM_MCP_DB": str(synthetic_db),
                    "WECOM_MCP_READ_ONLY": "1",
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            }
        }
    }
    config_path = extracted / "mcp_config.generated.json"
    config_path.write_text(json.dumps(generated, ensure_ascii=False), encoding="utf-8")
    verified = run_strict_utf8(
        [sys.executable, "skills/wecom-local-mcp/scripts/verify_agent_deploy.py", "--require-data"],
        cwd=extracted, env=utf8_env(), capture_output=True, timeout=60, check=True,
    )
    assert "MCP_VERIFICATION=OK" in verified.stdout, verified.stdout + verified.stderr
    for template in ("qoder.mcp.json", "workbuddy.mcp.json"):
        value = json.loads((extracted / "skills" / "wecom-local-mcp" / "references" / template).read_text(encoding="utf-8"))
        assert "wecom-local-messages" in value["mcpServers"]


def verify_offline_venv(extracted: Path) -> None:
    """Install the packed wheel in a clean venv with socket access blocked."""
    with tempfile.TemporaryDirectory(prefix="wecom_offline_venv_test_") as td:
        temporary = Path(td)
        guard = temporary / "network_guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
        destination = temporary / "isolated venv"
        env = utf8_env(
            PYTHONNOUSERSITE="1",
            PYTHONPATH=str(guard),
            PIP_NO_INDEX="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
        )
        installed = run_strict_utf8(
            [sys.executable, "setup_venv.py", "--dest", str(destination), "--offline"],
            cwd=extracted,
            env=env,
            capture_output=True,
            timeout=180,
            check=True,
        )
        assert "PYCRYPTODOME=OK" in installed.stdout, installed.stdout + installed.stderr
        python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        verified = run_strict_utf8(
            [python, "-I", "-c",
             "import Crypto; from Crypto.Cipher import AES; "
             "assert Crypto.__version__ == '4.2.0'; assert AES.block_size == 16; "
             "print('OFFLINE_CRYPTO=OK')"],
            cwd=extracted,
            env=env,
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert "OFFLINE_CRYPTO=OK" in verified.stdout, verified.stdout + verified.stderr


def main() -> None:
    for rel in FILES:
        if not (ROOT / rel).is_file():
            raise FileNotFoundError(ROOT / rel)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in FILES:
            z.write(ROOT / rel, f"{PREFIX}/{rel}")
        z.writestr(
            f"{PREFIX}/third_party/wecom-decrypt/config.json",
            json.dumps(GENERIC_CONFIG, indent=2) + "\n",
        )
        z.writestr(f"{PREFIX}/PACKAGE_CONTENTS.txt", NOTICE)
    with zipfile.ZipFile(OUT) as z:
        assert z.testzip() is None
        names = z.namelist()
        forbidden = [n for n in names if n.endswith((".sqlite", ".db", ".log", ".png", "wxwork_keys.json"))]
        assert not forbidden, forbidden
        packed_requirements = z.read(f"{PREFIX}/requirements.txt").decode("utf-8", errors="strict")
        assert packed_requirements == (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="strict")
        packed_config = z.read(f"{PREFIX}/third_party/wecom-decrypt/config.json").decode("utf-8", errors="strict")
        assert json.loads(packed_config) == GENERIC_CONFIG
        with tempfile.TemporaryDirectory(prefix="wecom_zip_test_") as td:
            z.extractall(td)
            extracted = Path(td) / PREFIX
            run_strict_utf8(
                [sys.executable, "-m", "unittest", "-v",
                 "test_pipeline.py", "test_collector_regressions.py",
                 "test_snapshot_regressions.py", "test_snapshot_performance.py",
                 "test_store_performance.py", "test_mcp_regressions.py",
                 "test_mcp_performance.py", "test_webui.py"],
                cwd=extracted,
                env=utf8_env(),
                timeout=90,
                check=True,
            )
            verify_offline_venv(extracted)
            verify_extracted_mcp(extracted)
    print(f"ZIP={OUT}")
    print(f"FILES={len(FILES) + 2}")
    print(f"BYTES={OUT.stat().st_size}")
    print("ZIP_INTEGRITY=OK")
    print("EXTRACTED_UNIT_TESTS=OK")
    print("EXTRACTED_OFFLINE_VENV=OK")
    print("EXTRACTED_MCP_INITIALIZE=OK")
    print("EXTRACTED_MCP_SYNTHETIC_CONTENT=OK")
    print("EXTRACTED_AGENT_SKILL=OK")
    print("QODER_WORKBUDDY_TEMPLATES=OK")
    print("SENSITIVE_DATA_INCLUDED=NO")


if __name__ == "__main__":
    main()
