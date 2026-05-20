#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "bin" / "tactile-macos-mcp"

EXPECTED_TOOLS = {
    "list_apps",
    "get_app_state",
    "click",
    "perform_secondary_action",
    "set_value",
    "scroll",
    "drag",
    "press_key",
    "type_text",
}

EXPECTED_ACTIONS = {
    "Press",
    "Raise",
    "ShowMenu",
    "Confirm",
    "Cancel",
    "Increment",
    "Decrement",
    "Focus",
    "Select",
    "Deselect",
    "ScrollUp",
    "ScrollDown",
    "ScrollLeft",
    "ScrollRight",
}

EXPECTED_OBSERVATION_MODES = {"ax", "ax_ocr", "ax_ocr_visual"}
EXPECTED_SUMMARY_MODES = {"full", "metadata", "tsv"}


class MCPClient:
    def __init__(self, server: Path, timeout: float):
        if not server.exists():
            raise RuntimeError(f"server binary not found: {server}")
        env = os.environ.copy()
        env["TACTILE_MACOS_MCP_ROOT"] = str(ROOT)
        self.timeout = timeout
        self.next_id = 1
        self.proc = subprocess.Popen(
            [str(server)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def request(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._read_response(request_id)

    def notify(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _write(self, payload):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, request_id):
        deadline = time.monotonic() + self.timeout
        assert self.proc.stdout is not None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"server exited with {self.proc.returncode}: {stderr}")
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.02)
                continue
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
            return message.get("result")
        raise TimeoutError(f"timed out waiting for {request_id}")

    def initialize(self):
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tactile-macos-mcp-test", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        return self.request("tools/list")

    def call_tool(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})


def content_text(result):
    parts = result.get("content", [])
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text")


def find_action_enum(tool):
    schema = tool.get("inputSchema", {})
    action = schema.get("properties", {}).get("action", {})
    return set(action.get("enum", []))


def find_property_enum(tool, property_name):
    schema = tool.get("inputSchema", {})
    prop = schema.get("properties", {}).get(property_name, {})
    return set(prop.get("enum", []))


def assert_contains_all(text, expected_fragments, label):
    missing = [fragment for fragment in expected_fragments if fragment not in text]
    if missing:
        raise AssertionError(f"{label} missing fragments: {missing}; text={text}")


def test_tools(client):
    result = client.list_tools()
    tools = result.get("tools", [])
    names = {tool["name"] for tool in tools}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    if missing or extra:
        raise AssertionError(f"tool mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    by_name = {tool["name"]: tool for tool in tools}
    actions = find_action_enum(by_name["perform_secondary_action"])
    if actions != EXPECTED_ACTIONS:
        raise AssertionError(
            f"perform_secondary_action enum mismatch: missing={sorted(EXPECTED_ACTIONS - actions)} "
            f"extra={sorted(actions - EXPECTED_ACTIONS)}"
        )
    modes = find_property_enum(by_name["get_app_state"], "observation_mode")
    if modes != EXPECTED_OBSERVATION_MODES:
        raise AssertionError(
            f"get_app_state observation_mode enum mismatch: missing={sorted(EXPECTED_OBSERVATION_MODES - modes)} "
            f"extra={sorted(modes - EXPECTED_OBSERVATION_MODES)}"
        )
    summary_modes = find_property_enum(by_name["get_app_state"], "summary_mode")
    if summary_modes != EXPECTED_SUMMARY_MODES:
        raise AssertionError(
            f"get_app_state summary_mode enum mismatch: missing={sorted(EXPECTED_SUMMARY_MODES - summary_modes)} "
            f"extra={sorted(summary_modes - EXPECTED_SUMMARY_MODES)}"
        )
    click_properties = by_name["click"].get("inputSchema", {}).get("properties", {})
    if "element_index" in click_properties:
        raise AssertionError("click schema should not expose element_index")
    for property_name in ("coordinate_space", "screen_x", "screen_y"):
        if property_name in click_properties:
            raise AssertionError(f"click schema should not expose {property_name}")
    click_description = by_name["click"].get("description", "")
    assert_contains_all(
        click_description,
        [
            "Click coordinate-backed targets only.",
            "x/y macOS screen points",
            "This tool does not accept AX element_index input; for AX element operations, use perform_secondary_action.",
        ],
        "click description",
    )
    secondary_action_description = by_name["perform_secondary_action"].get("description", "")
    assert_contains_all(
        secondary_action_description,
        [
            "Use this tool for AX element operations; click is coordinate-only.",
        ],
        "perform_secondary_action description",
    )
    click_result = client.call_tool("click", {"app": "TextEdit", "element_index": "1"})
    if not click_result.get("isError") or "coordinate" not in content_text(click_result).lower():
        raise AssertionError("click should reject element_index inputs")
    legacy_coords_result = client.call_tool(
        "click",
        {"app": "TextEdit", "x": 10, "y": 20, "screen_x": 0, "screen_y": 0},
    )
    legacy_coords_text = content_text(legacy_coords_result).lower()
    if not legacy_coords_result.get("isError") or "no longer accepts" not in legacy_coords_text:
        raise AssertionError("click should reject legacy alternate coordinate fields")
    result = client.call_tool("set_value")
    if not result.get("isError") or "disabled" not in content_text(result).lower():
        raise AssertionError("set_value should remain listed but return a disabled error")
    print("tools: ok")


def test_list_apps(client):
    result = client.call_tool("list_apps")
    if result.get("isError"):
        raise AssertionError(content_text(result))
    text = content_text(result)
    if "apps" not in text.lower():
        raise AssertionError(f"unexpected list_apps response: {text[:400]}")
    print("list_apps: ok")


def test_state(client, app):
    result = client.call_tool("get_app_state", {"app": app})
    if result.get("isError"):
        raise AssertionError(content_text(result))
    text = content_text(result)
    if app.lower() not in text.lower() and "state_path" not in text:
        raise AssertionError(f"unexpected get_app_state response: {text[:400]}")
    if "summary_mode: tsv" not in text:
        raise AssertionError(f"get_app_state should default to tsv summary: {text[:400]}")
    if "format: tsv" not in text:
        raise AssertionError(f"default get_app_state should advertise tsv format: {text[:400]}")
    if "index\tsource\trole\ttext\tflags\tconfidence\tcenter\tframe\tactions\tax_path" not in text:
        raise AssertionError(f"default get_app_state should include a tsv header row: {text[:400]}")
    if "full_element_dump:" not in text:
        raise AssertionError(f"get_app_state tsv summary should include full dump path: {text[:400]}")
    if "screenshotFrame:" in text or "screenshotCenter:" in text:
        raise AssertionError(f"get_app_state tsv summary should not expose screenshot coordinates: {text[:400]}")
    compact_result = client.call_tool("get_app_state", {"app": app, "summary_mode": "compact"})
    compact_text = content_text(compact_result).lower()
    if not compact_result.get("isError") or "unsupported summary_mode compact" not in compact_text:
        raise AssertionError("get_app_state should reject the removed compact summary mode")
    tsv_result = client.call_tool("get_app_state", {"app": app, "observation_mode": "ax", "summary_mode": "tsv"})
    if tsv_result.get("isError"):
        raise AssertionError(content_text(tsv_result))
    tsv_text = content_text(tsv_result)
    if "summary_mode: tsv" not in tsv_text:
        raise AssertionError(f"tsv summary should report tsv mode: {tsv_text[:400]}")
    if "format: tsv" not in tsv_text:
        raise AssertionError(f"tsv summary should advertise tabular format: {tsv_text[:400]}")
    if "index\tsource\trole\ttext\tflags\tconfidence\tcenter\tframe\tactions\tax_path" not in tsv_text:
        raise AssertionError(f"tsv summary should include a header row: {tsv_text[:400]}")
    if "AX elements (" in tsv_text or "OCR lines (" in tsv_text:
        raise AssertionError(f"tsv summary should not include prose element sections: {tsv_text[:400]}")
    metadata_result = client.call_tool("get_app_state", {"app": app, "observation_mode": "ax", "summary_mode": "metadata"})
    if metadata_result.get("isError"):
        raise AssertionError(content_text(metadata_result))
    metadata_text = content_text(metadata_result)
    if "Element listing omitted by summary_mode=metadata." not in metadata_text:
        raise AssertionError(f"metadata summary should omit element listing: {metadata_text[:400]}")
    if "AX elements (" in metadata_text or "OCR lines (" in metadata_text:
        raise AssertionError(f"metadata summary returned element sections: {metadata_text[:400]}")
    print(f"get_app_state({app}): ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=Path, default=SERVER)
    parser.add_argument("--test", choices=["tools", "list-apps", "state", "all"], default="tools")
    parser.add_argument("--app", default="TextEdit")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    client = MCPClient(args.server, args.timeout)
    try:
        client.initialize()
        if args.test in ("tools", "all"):
            test_tools(client)
        if args.test in ("list-apps", "all"):
            test_list_apps(client)
        if args.test in ("state", "all"):
            test_state(client, args.app)
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
