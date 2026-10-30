"""Verify the generated WeCom stdio MCP config without printing message data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
ROOT = SKILL_DIR.parents[1]
SERVER_NAME = "wecom-local-messages"
PROTOCOL_VERSION = "2024-11-05"
READ_TOOLS = {
    "list_conversations",
    "search_messages",
    "recent_messages",
    "get_message",
    "count_messages",
}


def request(rid: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        value["params"] = params
    return value


def tool_call(rid: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return request(rid, "tools/call", {"name": name, "arguments": arguments})


def load_server(config_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    raw = config_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"generated config must be UTF-8 without BOM: {config_path}")
    config = json.loads(raw.decode("utf-8", errors="strict"))
    server = config["mcpServers"][SERVER_NAME]
    if not isinstance(server.get("command"), str) or not server["command"]:
        raise ValueError("generated MCP command is missing")
    if not isinstance(server.get("args"), list) or not all(isinstance(item, str) for item in server["args"]):
        raise ValueError("generated MCP args must be a string array")
    if server.get("env", {}).get("WECOM_MCP_READ_ONLY") != "1":
        raise ValueError("generated MCP config must keep WECOM_MCP_READ_ONLY=1")
    if not server["env"].get("WECOM_MCP_DB"):
        raise ValueError("generated MCP config is missing WECOM_MCP_DB")
    env = os.environ.copy()
    for key, value in server.get("env", {}).items():
        if not isinstance(value, str):
            raise ValueError(f"generated MCP env value must be a string: {key}")
        env[key] = value
    return server, env


def sanitized_stderr(text: str) -> str:
    # Startup diagnostics can contain local paths. Keep troubleshooting output
    # local and bounded, and never echo MCP stdout payloads or message content.
    result = text
    for path in (str(ROOT), str(Path.home())):
        if path:
            result = result.replace(path, "<LOCAL_PATH>")
    return result[-2000:].strip()


def run_rpc(
    server: dict[str, Any],
    env: dict[str, str],
    requests: list[dict[str, Any]],
    timeout: float,
) -> dict[int, dict[str, Any]]:
    wire = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    command = [server["command"], *server["args"]]
    try:
        process = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            input=wire,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("configured MCP Python executable was not found; rerun 01_一键部署.cmd") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"MCP stdio verification timed out after {timeout:g}s") from exc
    if process.returncode != 0:
        detail = sanitized_stderr(process.stderr)
        suffix = f"; stderr: {detail}" if detail else ""
        raise RuntimeError(f"MCP exited with code {process.returncode}{suffix}")
    lines = process.stdout.splitlines()
    if len(lines) != len(requests):
        raise RuntimeError(f"expected {len(requests)} JSON-RPC responses, got {len(lines)}")
    responses = [json.loads(line) for line in lines]
    by_id: dict[int, dict[str, Any]] = {}
    for response in responses:
        rid = response.get("id")
        if not isinstance(rid, int) or rid in by_id:
            raise RuntimeError("MCP returned a missing, non-integer, or duplicate response id")
        by_id[rid] = response
    if set(by_id) != {item["id"] for item in requests}:
        raise RuntimeError("MCP response ids do not match the requests")
    return by_id


def result(response: dict[str, Any]) -> dict[str, Any]:
    if "error" in response:
        error = response["error"]
        raise RuntimeError(f"JSON-RPC error {error.get('code')}: {error.get('message')}")
    value = response.get("result")
    if not isinstance(value, dict):
        raise RuntimeError("MCP result is not an object")
    return value


def structured(response: dict[str, Any]) -> dict[str, Any]:
    value = result(response)
    if value.get("isError"):
        raise RuntimeError("MCP tool returned isError=true")
    payload = value.get("structuredContent")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool result has no structuredContent object")
    return payload


def validate_page(payload: dict[str, Any], array_name: str) -> None:
    if not isinstance(payload.get(array_name), list):
        raise RuntimeError(f"MCP page is missing {array_name}")
    for key in ("count", "total", "offset", "has_more", "next_offset"):
        if key not in payload:
            raise RuntimeError(f"MCP page is missing {key}")
    if payload["count"] != len(payload[array_name]):
        raise RuntimeError("MCP page count does not match its result array")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "mcp_config.generated.json",
        help="generated config path (default: package-root mcp_config.generated.json)",
    )
    parser.add_argument("--require-data", action="store_true", help="fail instead of skipping get_message on an empty DB")
    parser.add_argument("--timeout", type=float, default=30.0, help="timeout for each stdio session, in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    config_path = args.config.resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"generated MCP config not found: {config_path}; run 01_一键部署.cmd first")
    server, env = load_server(config_path)

    initial_requests = [
        request(1, "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}),
        request(2, "tools/list"),
        tool_call(3, "list_conversations", {"limit": 1}),
        tool_call(4, "recent_messages", {"limit": 1}),
        tool_call(5, "search_messages", {"query": "__wecom_skill_verify_no_match__", "limit": 1}),
        tool_call(6, "count_messages", {"group_by": "day"}),
    ]
    responses = run_rpc(server, env, initial_requests, args.timeout)
    initialized = result(responses[1])
    if initialized.get("protocolVersion") != PROTOCOL_VERSION:
        raise RuntimeError("unexpected MCP protocolVersion")
    if initialized.get("serverInfo", {}).get("name") != SERVER_NAME:
        raise RuntimeError("unexpected MCP serverInfo.name")
    tools = result(responses[2]).get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("tools/list did not return a tools array")
    names = {item.get("name") for item in tools if isinstance(item, dict)}
    if not READ_TOOLS <= names:
        raise RuntimeError(f"tools/list is missing: {sorted(READ_TOOLS - names)}")
    if "sync_messages" in names:
        raise RuntimeError("read-only MCP unexpectedly exposes sync_messages")
    validate_page(structured(responses[3]), "conversations")
    recent = structured(responses[4])
    validate_page(recent, "messages")
    validate_page(structured(responses[5]), "messages")
    counted = structured(responses[6])
    if not isinstance(counted.get("total"), int) or not isinstance(counted.get("groups"), list):
        raise RuntimeError("count_messages returned invalid aggregate metadata")
    print("MCP_COUNT_MESSAGES=OK")
    print("MCP_INITIALIZE=OK")
    print("MCP_TOOLS_LIST=OK")
    print("MCP_READ_ONLY=OK")
    print("MCP_LIST_CONVERSATIONS=OK")
    print("MCP_RECENT_MESSAGES=OK")
    print("MCP_SEARCH_MESSAGES=OK")

    messages = recent["messages"]
    if not messages:
        if args.require_data:
            raise RuntimeError("the configured database has no messages; get_message was not verified")
        print("MCP_GET_MESSAGE=SKIP_EMPTY_DATABASE")
        print("MCP_VERIFICATION=OK_WITH_EMPTY_DATABASE")
        return 0

    msgid = messages[0].get("msgid")
    if not isinstance(msgid, str) or not msgid:
        raise RuntimeError("recent_messages returned an item without msgid")
    read_requests = [
        request(11, "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}),
        tool_call(12, "get_message", {"msgid": msgid}),
    ]
    read_responses = run_rpc(server, env, read_requests, args.timeout)
    result(read_responses[11])
    full = structured(read_responses[12])
    if full.get("msgid") != msgid:
        raise RuntimeError("get_message returned a different msgid")
    print("MCP_GET_MESSAGE=OK")
    print("MCP_VERIFICATION=OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MCP_VERIFICATION=FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
