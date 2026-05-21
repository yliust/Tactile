#!/usr/bin/env python3
"""LangGraph-based macOS computer-use workflow backed by tactile MCP."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

WORKFLOW_DIR = Path(__file__).resolve().parent
LEGACY_WORKFLOW_PATH = WORKFLOW_DIR / "codex_llm_workflow.py"
PROJECT_ROOT = WORKFLOW_DIR.parents[3]
MCP_ROOT = PROJECT_ROOT / "mcps" / "tactile-macos-mcp"
MCP_SERVER = MCP_ROOT / "bin" / "tactile-macos-mcp"


def load_legacy_workflow():
    spec = importlib.util.spec_from_file_location("_tactile_legacy_codex_llm_workflow", LEGACY_WORKFLOW_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load legacy workflow module from {LEGACY_WORKFLOW_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


legacy = load_legacy_workflow()


DEFAULT_LLM_MODEL = os.getenv("TACTILE_MODEL", "gpt-5.5")
LLM_TIMEOUT_SECONDS = float(os.getenv("TACTILE_LLM_TIMEOUT", "600"))
LLM_MAX_RETRIES = int(os.getenv("TACTILE_LLM_MAX_RETRIES", "3"))
MCP_TIMEOUT_SECONDS = float(os.getenv("TACTILE_MCP_TIMEOUT", "20"))


class MCPClient:
    def __init__(self, server: Path, timeout: float):
        if not server.exists():
            raise RuntimeError(f"server binary not found: {server}")
        env = os.environ.copy()
        env["TACTILE_MACOS_MCP_ROOT"] = str(MCP_ROOT)
        self.timeout = timeout
        self.next_id = 1
        self.proc = subprocess.Popen(
            [str(server)],
            cwd=str(MCP_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._read_response(request_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "computer-use-workflow", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
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
            return message.get("result") or {}
        raise TimeoutError(f"timed out waiting for MCP response {request_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and optionally execute macOS UI actions through a LangGraph computer-use workflow.")
    parser.add_argument("instruction", nargs="?", default="", help="Natural-language instruction for the target app.")
    parser.add_argument("--target", default=None, help="Optional app name, bundle id, or .app path override. By default the app is inferred from the instruction.")
    parser.add_argument("--list-apps", action="store_true", help="List discovered local apps and exit.")
    parser.add_argument("--match", help="With --list-apps, regex or literal text matched against app names, aliases, bundle IDs, paths, and running processes.")
    parser.add_argument("--compact", action="store_true", help="With --list-apps, print concise app records and merge matching running processes into installed apps.")
    parser.add_argument("--best", action="store_true", help="With --list-apps, print only the preferred matching app record. Implies compact ranking.")
    parser.add_argument("--limit", type=int, help="With --list-apps, maximum number of records to print.")
    parser.add_argument("--model", default=None, help="Override model name passed to the LangChain chat model.")
    parser.add_argument("--mode", choices=legacy.WORKFLOW_MODES, default="auto", help="Workflow mode. auto chooses from fixed app profiles or the capability selector.")
    parser.add_argument("--capability-selection", choices=legacy.CAPABILITY_SELECTION_MODES, default="auto", help="How auto mode chooses app capabilities. auto uses fixed profiles for known apps and asks the LLM for unknown apps.")
    parser.add_argument("--visual-planning", choices=legacy.VISUAL_PLANNING_MODES, default="auto", help="Attach screenshots to the planner. auto enables it for AX-poor/profile-selected apps.")
    parser.add_argument("--visual-max-width", type=int, default=1280, help="Maximum width for planner screenshot images. Use 0 to attach the original capture.")
    parser.add_argument("--max-elements", type=int, default=180, help="Maximum summarized UI elements sent to the LLM.")
    parser.add_argument("--max-ocr-lines", type=int, default=80, help="Maximum OCR lines included for AX-poor workflow observations.")
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum observe-plan-act iterations when --execute is enabled.")
    parser.add_argument("--max-actions-per-step", type=int, default=1, help="Maximum actions the LLM may return for one observation step. Defaults to 1 so every action is followed by a fresh traversal.")
    parser.add_argument("--include-menus", action="store_true", help="Include AX menu elements in the LLM observation payload.")
    parser.add_argument("--no-virtual-hints", action="store_true", help="Disable generated coordinate hints for common search/input regions when AX does not expose real text fields.")
    parser.add_argument("--ocr-languages", default="zh-Hans,en-US", help="Unused by the MCP version; retained for CLI compatibility.")
    parser.add_argument("--ocr-recognition-level", choices=["accurate", "fast"], default="accurate", help="Unused by the MCP version; retained for CLI compatibility.")
    parser.add_argument("--debug-ax-grid", action="store_true", help=f"Draw a temporary red AX element grid for the target app on every workflow observation. Can also be enabled with {legacy.DEBUG_AX_GRID_ENV}=1.")
    parser.add_argument("--debug-ax-grid-duration", type=float, help=f"Seconds to keep the red AX grid visible. Defaults to {legacy.DEFAULT_DEBUG_AX_GRID_DURATION} or {legacy.DEBUG_AX_GRID_DURATION_ENV}.")
    parser.add_argument("--debug-observation", action="store_true", help="Print summarized element ids, roles, text, and frames before planning each step.")
    parser.add_argument("--execute", action="store_true", help="Execute the planned actions. Without this flag, only prints the plan.")
    parser.add_argument("--mock-plan", action="store_true", help="Skip the LLM call and use the deterministic fallback planner.")
    parser.add_argument("--no-fallback", action="store_true", help="Fail if the LLM call or plan parsing fails.")
    parser.add_argument("--plan-output", type=Path, default=None, help="Optional path to write the full run log JSON.")
    parser.add_argument("--traversal-output", type=Path, default=None, help="Optional path to write the latest raw traversal JSON.")
    return parser


def _llm_client_config(model_name: str | None = None) -> tuple[str, str, str | None]:
    api_key = os.getenv("TACTILE_OPENAI_API_KEY")
    base_url = os.getenv("TACTILE_OPENAI_BASE_URL")
    resolved_model_name = model_name or DEFAULT_LLM_MODEL
    if not api_key:
        raise RuntimeError("missing LLM API key; set TACTILE_OPENAI_API_KEY")
    return resolved_model_name, api_key, base_url


def _message_content(prompt: str, image_base64: str | Sequence[str] | None = None) -> str | list[dict[str, Any]]:
    images = [image_base64] if isinstance(image_base64, str) else list(image_base64 or [])
    if not images:
        return prompt
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}})
    return content


def _extract_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return "" if value is None else str(value)


def invoke_chat_model(
    prompt: str,
    *,
    model_name: str | None = None,
    image_base64: str | Sequence[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    resolved_model_name, api_key, base_url = _llm_client_config(model_name)
    kwargs: dict[str, Any] = {
        "model": resolved_model_name,
        "api_key": api_key,
        "temperature": 0,
        "max_retries": LLM_MAX_RETRIES,
        "request_timeout": LLM_TIMEOUT_SECONDS,
    }
    if base_url:
        kwargs["base_url"] = base_url
    model = ChatOpenAI(**kwargs)
    message = HumanMessage(content=_message_content(prompt, image_base64))
    started_at = time.time()
    response = model.invoke([message])
    finished_at = time.time()
    response_text = _extract_text_content(getattr(response, "content", ""))
    metadata = {
        "resolved_model_name": resolved_model_name,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_ms": round((finished_at - started_at) * 1000, 2),
        "usage_metadata": getattr(response, "usage_metadata", None),
        "response_metadata": getattr(response, "response_metadata", None),
        "additional_kwargs": getattr(response, "additional_kwargs", None),
        "id": getattr(response, "id", None),
    }
    return response_text, metadata


def _content_text(result: dict[str, Any]) -> str:
    parts = result.get("content", [])
    return "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("type") == "text")


def _extract_marker_path(text: str, marker: str) -> Path | None:
    prefix = f"{marker}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                return Path(candidate)
    return None


def _normalize_target_identifier(value: str) -> str:
    if value.endswith(".app") or "/" in value:
        return Path(value).stem or value
    return value


def _mcp_call_succeeded(result: dict[str, Any], text: str) -> bool:
    if result.get("isError"):
        return False
    return not text.lstrip().startswith("Refused.")


def _normalize_keypress(key: str) -> str:
    aliases = {
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "space": "Space",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
    }
    cleaned = key.strip()
    if not cleaned:
        return cleaned
    if "+" not in cleaned:
        return aliases.get(cleaned.casefold(), cleaned)
    parts = cleaned.split("+")
    parts[-1] = aliases.get(parts[-1].casefold(), parts[-1])
    return "+".join(parts)


def _scroll_direction_and_pages(action: dict[str, Any]) -> tuple[str, float]:
    delta_y = float(action.get("deltaY", action.get("delta_y", 0)) or 0)
    delta_x = float(action.get("deltaX", action.get("delta_x", 0)) or 0)
    if abs(delta_x) > abs(delta_y):
        return ("right" if delta_x > 0 else "left"), max(abs(delta_x) / 5.0, 1.0)
    if delta_y == 0 and delta_x == 0:
        return "down", 1.0
    return ("down" if delta_y > 0 else "up"), max(abs(delta_y) / 5.0, 1.0)


def _estimate_tokens(text: str, model_name: str | None) -> int | None:
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model_name or DEFAULT_LLM_MODEL)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return None


def _estimate_value_tokens(value: Any, model_name: str | None) -> int | None:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except TypeError:
        serialized = str(value)
    return _estimate_tokens(serialized, model_name)


def _split_prompt_payload(prompt: str) -> tuple[str | None, str | None]:
    for marker in ("Current state JSON:\n", "Current capability evidence JSON:\n"):
        if marker in prompt:
            prefix, payload = prompt.split(marker, 1)
            return prefix + marker, payload
    return None, None


def _nested_breakdown(value: Any, model_name: str | None, *, depth: int = 1) -> Any:
    if depth < 0:
        return None
    if isinstance(value, dict):
        return {
            key: {
                "estimated_tokens": _estimate_value_tokens(item, model_name),
                "characters": len(json.dumps(item, ensure_ascii=False)),
                "items": _nested_breakdown(item, model_name, depth=depth - 1),
            }
            for key, item in value.items()
        }
    if isinstance(value, list):
        return {
            "count": len(value),
            "first_items": [
                {
                    "estimated_tokens": _estimate_value_tokens(item, model_name),
                    "characters": len(json.dumps(item, ensure_ascii=False)),
                }
                for item in value[:3]
            ],
        }
    return None


def _build_prompt_breakdown(prompt: str, *, model_name: str | None) -> dict[str, Any]:
    prefix, payload_text = _split_prompt_payload(prompt)
    breakdown: dict[str, Any] = {
        "total_characters": len(prompt),
        "estimated_total_tokens": _estimate_tokens(prompt, model_name),
        "instruction_prefix_characters": len(prefix or prompt),
        "instruction_prefix_estimated_tokens": _estimate_tokens(prefix or prompt, model_name),
    }
    if payload_text is None:
        return breakdown
    payload_text = payload_text.strip()
    breakdown["payload_json_characters"] = len(payload_text)
    breakdown["payload_json_estimated_tokens"] = _estimate_tokens(payload_text, model_name)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        breakdown["payload_parse_error"] = str(exc)
        return breakdown
    breakdown["payload_keys"] = list(payload.keys()) if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        breakdown["payload_fields"] = {
            key: {
                "estimated_tokens": _estimate_value_tokens(value, model_name),
                "characters": len(json.dumps(value, ensure_ascii=False)),
                "items": _nested_breakdown(value, model_name),
            }
            for key, value in payload.items()
        }
    return breakdown


def _normalize_usage_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    usage = metadata.get("usage_metadata")
    response_metadata = metadata.get("response_metadata")
    token_usage = response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None
    normalized = {
        "usage_metadata": usage,
        "response_metadata": response_metadata,
        "token_usage": token_usage,
    }
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None
    if isinstance(usage, dict):
        prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
    if prompt_tokens is None and isinstance(token_usage, dict):
        prompt_tokens = token_usage.get("prompt_tokens") or token_usage.get("input_tokens")
        completion_tokens = token_usage.get("completion_tokens") or token_usage.get("output_tokens")
        total_tokens = token_usage.get("total_tokens")
    normalized["prompt_tokens"] = prompt_tokens
    normalized["completion_tokens"] = completion_tokens
    normalized["total_tokens"] = total_tokens
    return normalized


def _compact_frame(frame: Any) -> dict[str, int] | None:
    if not isinstance(frame, dict):
        return None
    try:
        return {
            "x": int(round(float(frame["x"]))),
            "y": int(round(float(frame["y"]))),
            "width": int(round(float(frame["width"]))),
            "height": int(round(float(frame["height"]))),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _compact_elements_for_prompt(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for element in elements:
        compact: dict[str, Any] = {
            "id": element.get("id"),
            "source": element.get("source"),
            "role": element.get("role"),
            "text": element.get("text"),
            "direct_ax": element.get("direct_ax"),
        }
        if "ocr_confidence" in element and element.get("ocr_confidence") is not None:
            compact["ocr_confidence"] = element.get("ocr_confidence")
        if "name" in element and element.get("name") is not None:
            compact["name"] = element.get("name")
        frame = _compact_frame(element.get("frame"))
        if frame is not None:
            compact["frame"] = frame
        compacted.append({key: value for key, value in compact.items() if value is not None})
    return compacted


def _compact_observation_for_prompt(observation: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    ax_count = sum(1 for element in elements if element.get("source") == "ax")
    ocr_count = sum(1 for element in elements if element.get("source") == "ocr")
    profile_region_count = sum(1 for element in elements if element.get("source") == "profile_region")
    virtual_region_count = sum(1 for element in elements if element.get("source") == "virtual_region")
    compact: dict[str, Any] = {
        "workflow_mode": observation.get("workflow_mode"),
        "app_profile": observation.get("app_profile"),
        "app_guide_path": observation.get("app_guide_path"),
        "stats": observation.get("stats"),
        "screenshot_path": observation.get("screenshot_path"),
        "element_summary": {
            "total": len(elements),
            "ax": ax_count,
            "ocr": ocr_count,
            "profile_regions": profile_region_count,
            "virtual_regions": virtual_region_count,
        },
        "ax_elements": {
            "count": len(observation.get("ax_elements") or []),
            "note": "Detailed AX elements are provided only in the top-level elements list to avoid prompt duplication.",
        },
        "ocr_lines": {
            "count": len(observation.get("ocr_lines") or []),
            "note": "Detailed OCR lines are provided only in the top-level elements list to avoid prompt duplication.",
        },
        "profile_regions": {
            "count": len(observation.get("profile_regions") or []),
            "note": "Detailed profile regions are provided only in the top-level elements list to avoid prompt duplication.",
        },
        "ocr_error": observation.get("ocr_error"),
        "visual_observation": observation.get("visual_observation"),
        "ocr_payload": {
            "included": False,
            "note": "Raw OCR payload is omitted from the planner prompt; use top-level OCR elements instead.",
        },
    }
    return {key: value for key, value in compact.items() if value is not None}


def _compact_result_for_prompt(result: dict[str, Any]) -> dict[str, Any]:
    action = result.get("action") if isinstance(result.get("action"), dict) else {}
    compact: dict[str, Any] = {
        "ok": result.get("ok"),
        "mode": result.get("mode"),
        "action_type": action.get("type"),
        "element_id": action.get("element_id"),
        "text_length": action.get("text_length"),
        "point": result.get("point"),
        "input_method": result.get("input_method"),
        "skipped": result.get("skipped"),
        "fallback_from": result.get("fallback_from"),
        "fallback_reason": result.get("fallback_reason"),
        "fallback_skipped": result.get("fallback_skipped"),
        "error": result.get("error"),
    }
    diagnostics = result.get("input_diagnostics")
    if isinstance(diagnostics, dict):
        compact["input_diagnostics"] = {
            key: diagnostics.get(key)
            for key in (
                "skip_reason",
                "existing_text_match",
                "preferred_input_method",
                "text_length",
                "focus",
                "post_input_verification",
            )
            if diagnostics.get(key) is not None
        }
    post_input_verification = result.get("post_input_verification")
    if post_input_verification is not None:
        compact["post_input_verification"] = post_input_verification
    return {key: value for key, value in compact.items() if value is not None}


def _compact_history_for_prompt(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in history[-3:]:
        compacted.append(
            {
                "step": item.get("step"),
                "status": item.get("status"),
                "summary": item.get("summary"),
                "actions": item.get("actions"),
                "execution_results": [
                    _compact_result_for_prompt(result)
                    for result in (item.get("execution_results") or [])
                    if isinstance(result, dict)
                ],
            }
        )
    return compacted


def _unique_preserve_order(values: list[str], *, limit: int = 8) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _build_progress_summary_from_steps(step_records: list[dict[str, Any]], app_profile_name: str) -> dict[str, Any]:
    clicked_targets: list[str] = []
    typed_texts: list[str] = []
    shortcuts: list[str] = []
    coordinate_click_attempts = 0
    direct_ax_failures = 0
    last_non_wait_action: dict[str, Any] | None = None

    for step in step_records:
        plan = step.get("plan") or {}
        actions = plan.get("actions") or []
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "").lower()
            if action_type == "keypress" and action.get("key"):
                shortcuts.append(str(action.get("key")))
            elif action_type == "writetext" and action.get("text"):
                typed_texts.append(str(action.get("text")))
            elif action_type == "click" and action.get("x") is not None and action.get("y") is not None:
                coordinate_click_attempts += 1
            if action_type and action_type not in {"wait", "finish"}:
                last_non_wait_action = action

        for element in step.get("action_elements") or []:
            if not isinstance(element, dict):
                continue
            text = legacy.clean_text(element.get("text"), limit=120)
            if text:
                clicked_targets.append(text)

        for result in step.get("execution_results") or []:
            if not isinstance(result, dict):
                continue
            if result.get("mode") == "direct_ax" and not result.get("ok"):
                direct_ax_failures += 1
            action = result.get("action")
            if isinstance(action, dict):
                action_type = str(action.get("type") or "").lower()
                if action_type and action_type not in {"wait", "finish"}:
                    last_non_wait_action = action

    org_like_targets = [
        text
        for text in clicked_targets
        if any(marker in text for marker in ("团队", "公司", "组织", "Lab", "账号"))
    ]
    contact_like_targets = [
        text
        for text in clicked_targets
        if text not in org_like_targets and len(text) <= 20
    ]

    summary: dict[str, Any] = {
        "completed_steps": sum(1 for step in step_records if step.get("execution_results")),
        "clicked_targets": _unique_preserve_order(clicked_targets, limit=10),
        "typed_texts": _unique_preserve_order(typed_texts, limit=5),
        "used_shortcuts": _unique_preserve_order(shortcuts, limit=5),
        "coordinate_click_attempts": coordinate_click_attempts,
        "direct_ax_failures": direct_ax_failures,
        "last_non_wait_action": last_non_wait_action,
    }
    if org_like_targets:
        summary["org_targets_attempted"] = _unique_preserve_order(org_like_targets, limit=5)
    if contact_like_targets:
        summary["contact_targets_attempted"] = _unique_preserve_order(contact_like_targets, limit=8)

    repeat_guard: list[str] = [
        "Treat completed later-stage milestones as durable. Do not restart from the beginning unless the current UI directly contradicts them.",
    ]
    if typed_texts:
        repeat_guard.append(
            "A search/contact query was already entered. Do not go back to prerequisite navigation unless the current UI clearly lost the query or target path."
        )
    if app_profile_name == "feishu-lark":
        if org_like_targets:
            repeat_guard.append(
                "Organization/account switching was already attempted earlier. Do not repeat it after contact-search steps unless the current UI explicitly shows the wrong organization."
            )
        if contact_like_targets:
            repeat_guard.append(
                "If opening the searched contact failed once, prefer the next fallback path such as 通讯录 or a different visible result. Do not bounce back to organization switching."
            )
    summary["repeat_guard"] = repeat_guard
    return summary


def _inject_progress_summary_into_prompt(prompt: str, progress_summary: dict[str, Any]) -> str:
    marker = "Current state JSON:\n"
    progress_block = (
        "Long-horizon progress JSON:\n"
        f"{json.dumps(progress_summary, ensure_ascii=False)}\n\n"
    )
    if marker in prompt:
        return prompt.replace(marker, progress_block + marker, 1)
    return prompt + "\n\n" + progress_block


def _element_role_is_text_input(role: Any) -> bool:
    role_text = str(role or "").lower()
    return any(
        marker in role_text
        for marker in (
            "axtextarea",
            "axtextfield",
            "text area",
            "text field",
            "文本输入区",
            "文本字段",
            "搜索文本栏",
        )
    )


def _elements_for_completion_verifier(elements: list[dict[str, Any]], *, limit: int = 90) -> list[dict[str, Any]]:
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        score = 0
        if element.get("text"):
            score += 3
        if _element_role_is_text_input(element.get("role")):
            score += 2
        if element.get("source") == "ax":
            score += 1
        if element.get("direct_ax"):
            score += 1
        ranked.append((score, -index, element))
    ranked.sort(reverse=True)
    selected = [element for _, _, element in ranked[:limit]]
    return _compact_elements_for_prompt(selected)


def _normalize_completion_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "unsatisfied").strip().lower()
    if status not in {"satisfied", "unsatisfied", "blocked"}:
        status = "unsatisfied"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    summary = legacy.clean_text(payload.get("summary"), limit=800)
    evidence_raw = payload.get("evidence")
    evidence: list[str] = []
    if isinstance(evidence_raw, list):
        for item in evidence_raw:
            cleaned = legacy.clean_text(item, limit=240)
            if cleaned:
                evidence.append(cleaned)
            if len(evidence) >= 5:
                break
    return {
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
    }


def _build_completion_verifier_prompt(
    *,
    user_instruction: str,
    target_identifier: str,
    workflow_mode: str,
    app_profile_name: str,
    step_number: int,
    max_steps: int,
    progress_summary: dict[str, Any],
    observation: dict[str, Any],
    elements: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
) -> str:
    payload = {
        "user_instruction": user_instruction,
        "target_identifier": target_identifier,
        "workflow_mode": workflow_mode,
        "app_profile": app_profile_name,
        "step_number": step_number,
        "max_steps": max_steps,
        "progress_summary": progress_summary,
        "observation": observation,
        "elements": elements,
        "recent_history": recent_history,
    }
    return (
        "You are a completion verifier for a UI automation agent.\n"
        "Decide whether the user's requested task is already complete based on the current UI and recent executed actions.\n"
        "Return JSON only. Do not use markdown.\n"
        "Allowed statuses: satisfied, unsatisfied, blocked.\n"
        "Rules:\n"
        "1. Use the current UI as the primary source of truth.\n"
        "2. Distinguish editable inputs from completed results, history items, or opened targets.\n"
        "3. If the requested end state is already visible, prefer satisfied so the agent does not repeat the action.\n"
        "4. Use blocked only when the task clearly cannot proceed without human-only intervention or missing credentials.\n"
        "5. Be conservative with confidence. Only use confidence >= 0.8 when the completion evidence is clear.\n"
        "Return exactly this JSON shape:\n"
        "{\"status\":\"satisfied|unsatisfied|blocked\",\"confidence\":0.0,\"summary\":\"...\",\"evidence\":[\"...\"]}\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def discover_apps_via_mcp(client: MCPClient) -> list[legacy.AppCandidate]:
    result = client.call_tool("list_apps")
    text = _content_text(result)
    if result.get("isError"):
        raise RuntimeError(text or "list_apps failed")
    json_path = _extract_marker_path(text, "json")
    if json_path is None or not json_path.exists():
        raise RuntimeError(f"list_apps did not return a readable json artifact: {text[:500]}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    candidates: list[legacy.AppCandidate] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip()
        bundle_id = str(record.get("bundleIdentifier") or "").strip() or None
        path = str(record.get("path") or "").strip() or None
        pid = record.get("pid")
        running = bool(record.get("running"))
        identifier = bundle_id or name or (Path(path).stem if path else "")
        aliases = tuple(
            value
            for value in dict.fromkeys(
                [
                    name,
                    bundle_id,
                    path,
                    Path(path).stem if path else "",
                ]
            )
            if value
        )
        if path:
            candidates.append(
                legacy.AppCandidate(
                    display_name=name or Path(path).stem,
                    identifier=identifier,
                    aliases=aliases,
                    path=path,
                    bundle_id=bundle_id,
                    source="filesystem",
                )
            )
        if running and pid is not None:
            candidates.append(
                legacy.AppCandidate(
                    display_name=name or bundle_id or f"PID {pid}",
                    identifier=identifier,
                    aliases=aliases,
                    path=None,
                    bundle_id=bundle_id,
                    source=f"running:{int(pid)}",
                )
            )
    return candidates


def resolve_app_identifier_via_mcp(
    client: MCPClient,
    user_instruction: str,
    explicit_target: str | None = None,
) -> tuple[str, dict[str, Any]]:
    apps = discover_apps_via_mcp(client)
    if explicit_target:
        target_norm = legacy.normalize_name(explicit_target)
        stem_norm = legacy.normalize_name(Path(explicit_target).stem) if explicit_target.endswith(".app") else ""
        exact_matches = [
            app
            for app in apps
            if any(legacy.normalize_name(alias) in {target_norm, stem_norm} for alias in app.aliases)
            or (app.bundle_id and legacy.normalize_name(app.bundle_id) in {target_norm, stem_norm})
            or (app.path and legacy.normalize_name(Path(app.path).stem) in {target_norm, stem_norm})
        ]
        if exact_matches:
            chosen = sorted(exact_matches, key=lambda app: (app.path is None, len(app.display_name)))[0]
            return chosen.identifier, {
                "mode": "explicit_target",
                "input": explicit_target,
                "display_name": chosen.display_name,
                "matched_alias": explicit_target,
                "identifier": chosen.identifier,
                "bundle_id": chosen.bundle_id,
                "source": chosen.source,
                "aliases": list(chosen.aliases),
            }
        identifier = _normalize_target_identifier(explicit_target)
        return identifier, {"mode": "explicit_target_unresolved", "input": explicit_target, "identifier": identifier}

    scored: list[tuple[int, int, str, legacy.AppCandidate]] = []
    for app in apps:
        score, alias_length, alias = legacy.app_match_score(user_instruction, app)
        if score > 0:
            scored.append((score, alias_length, alias, app))
    if not scored:
        suggestions = sorted(
            [
                {
                    "display_name": app.display_name,
                    "identifier": app.identifier,
                    "aliases": list(app.aliases[:5]),
                    "source": app.source,
                }
                for app in apps
                if app.path is not None
            ],
            key=lambda item: item["display_name"].casefold(),
        )[:30]
        raise RuntimeError(
            "could not infer target app from instruction. "
            "Mention an installed app name in the instruction or pass --target.\n"
            f"Sample discovered apps: {json.dumps(suggestions, ensure_ascii=False)}"
        )
    scored.sort(key=lambda item: (item[0], item[1], item[3].path is not None), reverse=True)
    top_score = scored[0][0]
    best = [item for item in scored if item[0] == top_score]
    chosen_score, _, matched_alias, chosen = best[0]
    return chosen.identifier, {
        "mode": "inferred_from_instruction",
        "display_name": chosen.display_name,
        "matched_alias": matched_alias,
        "score": chosen_score,
        "identifier": chosen.identifier,
        "bundle_id": chosen.bundle_id,
        "source": chosen.source,
        "aliases": list(chosen.aliases),
    }


def list_apps(args: argparse.Namespace) -> int:
    client = MCPClient(MCP_SERVER, MCP_TIMEOUT_SECONDS)
    try:
        client.initialize()
        records = legacy.app_candidate_records(
            discover_apps_via_mcp(client),
            match=args.match,
            compact=args.compact,
            best=args.best,
            limit=args.limit,
        )
    finally:
        client.close()
    payload: Any = records[0] if args.best and records else (None if args.best else records)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


class LangGraphComputerUseWorkflow:
    def __init__(self, args: argparse.Namespace, *, mcp_client: MCPClient | Any | None = None):
        self.args = args
        self.max_steps = max(1, args.max_steps if args.execute else 1)
        self.debug_ax_grid_enabled = args.debug_ax_grid or legacy.env_flag_enabled(legacy.DEBUG_AX_GRID_ENV)
        self.debug_ax_grid_duration = legacy.resolve_debug_ax_grid_duration(args.debug_ax_grid_duration)
        self.mcp = mcp_client if mcp_client is not None else MCPClient(MCP_SERVER, MCP_TIMEOUT_SECONDS)
        self._owns_mcp_client = mcp_client is None
        self.llm_call_counter = 0
        if self._owns_mcp_client:
            self.mcp.initialize()

    def close(self) -> None:
        if not self._owns_mcp_client:
            return
        close = getattr(self.mcp, "close", None)
        if callable(close):
            close()

    def build_graph(self):
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(dict)
        graph.add_node("bootstrap", self.bootstrap)
        graph.add_node("observe", self.observe)
        graph.add_node("plan", self.plan)
        graph.add_node("execute", self.execute)
        graph.add_node("finalize", self.finalize)
        graph.add_edge(START, "bootstrap")
        graph.add_edge("bootstrap", "observe")
        graph.add_edge("observe", "plan")
        graph.add_conditional_edges(
            "plan",
            self.route_after_plan,
            {"execute": "execute", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "execute",
            self.route_after_execute,
            {"observe": "observe", "finalize": "finalize"},
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    def bootstrap(self, state: dict[str, Any]) -> dict[str, Any]:
        target_identifier, target_resolution = resolve_app_identifier_via_mcp(self.mcp, self.args.instruction, self.args.target)
        print(
            "target app: "
            f"{target_resolution.get('display_name', target_identifier)} "
            f"(matched: {target_resolution.get('matched_alias', target_resolution.get('input', ''))}, "
            f"identifier: {target_identifier})",
            file=sys.stderr,
        )
        initial_app_state = self.fetch_app_state(target_identifier)
        traversal = self.traversal_from_app_state(initial_app_state)
        pid = int(initial_app_state["pid"])
        app_profile = legacy.resolve_app_profile(target_identifier, target_resolution, traversal)
        workflow_mode, visual_planning = legacy.apply_capability_decision(
            requested_mode=self.args.mode,
            requested_visual_planning=self.args.visual_planning,
            profile=app_profile,
            decision=None,
        )
        artifact_dir = legacy.workflow_run_artifact_dir(self.args.plan_output, cwd=Path.cwd())
        llm_call_dir = artifact_dir / "llm-calls"
        llm_call_dir.mkdir(parents=True, exist_ok=True)
        print(f"llm-calls dir: {llm_call_dir}", file=sys.stderr)
        capability_decision = legacy.profile_capability_decision(app_profile, workflow_mode, visual_planning)
        run_log: dict[str, Any] = {
            "target": {"identifier": target_identifier, "pid": pid, "resolution": target_resolution},
            "instruction": self.args.instruction,
            "execute": self.args.execute,
            "requested_mode": self.args.mode,
            "requested_capability_selection": self.args.capability_selection,
            "workflow_mode": workflow_mode,
            "app_profile": app_profile.name,
            "app_guide_path": app_profile.guide_path,
            "app_guide_warnings": list(legacy.app_guide_warnings()),
            "requested_visual_planning": self.args.visual_planning,
            "visual_planning": visual_planning,
            "capability_selection": capability_decision,
            "artifact_root": os.fspath(self.args.plan_output.parent if self.args.plan_output else legacy.session_artifact_dir(cwd=Path.cwd(), create=False)),
            "artifact_dir": os.fspath(artifact_dir),
            "llm_call_dir": os.fspath(llm_call_dir),
            "plan_output": os.fspath(self.args.plan_output) if self.args.plan_output else None,
            "started_at": time.time(),
            "steps": [],
            "llm_calls": [],
            "final_status": "running",
            "debug_ax_grid": {
                "enabled": self.debug_ax_grid_enabled,
                "duration": self.debug_ax_grid_duration if self.debug_ax_grid_enabled else None,
            },
        }
        state.update(
            {
                "target_identifier": target_identifier,
                "target_resolution": target_resolution,
                "pid": pid,
                "app_profile": app_profile,
                "workflow_mode": workflow_mode,
                "visual_planning": visual_planning,
                "capability_decision": capability_decision,
                "artifact_dir": artifact_dir,
                "run_log": run_log,
                "history": [],
                "previous_step_record": None,
                "llm_capability_selection_done": False,
                "step_number": 1,
                "done": False,
                "auto_finish_summary": None,
                "app_state": initial_app_state,
                "traversal": traversal,
            }
        )
        return state

    def invoke_llm_with_trace(
        self,
        *,
        prompt: str,
        artifact_dir: Path,
        kind: str,
        step_number: int,
        model_name: str | None = None,
        image_base64: str | Sequence[str] | None = None,
        extra: dict[str, Any] | None = None,
        run_log: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        self.llm_call_counter += 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        call_slug = f"{self.llm_call_counter:03d}-{legacy.safe_path_component(kind)}-step-{step_number:02d}-{stamp}"
        call_dir = artifact_dir / "llm-calls" / call_slug
        call_dir.mkdir(parents=True, exist_ok=True)

        prompt_breakdown = _build_prompt_breakdown(prompt, model_name=model_name or DEFAULT_LLM_MODEL)
        prompt_record = {
            "kind": kind,
            "step_number": step_number,
            "model": model_name or DEFAULT_LLM_MODEL,
            "image_count": len([image_base64] if isinstance(image_base64, str) else list(image_base64 or [])),
            "prompt_characters": len(prompt),
            "prompt_estimated_tokens": prompt_breakdown.get("estimated_total_tokens"),
            "usage": None,
            "timing": None,
            "extra": extra or {},
            "call_dir": os.fspath(call_dir),
            "status": "pending",
        }
        prompt_record["prompt_breakdown"] = prompt_breakdown

        (call_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        legacy.write_json(
            call_dir / "request_content.json",
            {
                "prompt": prompt,
                "image_lengths": [len(image) for image in ([image_base64] if isinstance(image_base64, str) else list(image_base64 or []))],
            },
        )
        legacy.write_json(call_dir / "metadata.json", prompt_record)

        try:
            response_text, metadata = invoke_chat_model(
                prompt,
                model_name=model_name,
                image_base64=image_base64,
            )
        except Exception as exc:
            prompt_record["status"] = "error"
            prompt_record["error"] = repr(exc)
            legacy.write_json(call_dir / "metadata.json", prompt_record)
            raise

        usage = _normalize_usage_metadata(metadata)
        prompt_record["model"] = metadata.get("resolved_model_name") or prompt_record["model"]
        prompt_record["usage"] = usage
        prompt_record["timing"] = {
            "started_at": metadata.get("started_at"),
            "finished_at": metadata.get("finished_at"),
            "elapsed_ms": metadata.get("elapsed_ms"),
        }
        prompt_record["status"] = "completed"
        prompt_record["prompt_breakdown"] = _build_prompt_breakdown(prompt, model_name=prompt_record["model"])
        (call_dir / "response.txt").write_text(response_text, encoding="utf-8")
        legacy.write_json(call_dir / "metadata.json", prompt_record)
        if run_log is not None:
            run_log.setdefault("llm_calls", []).append(prompt_record)
        return response_text, prompt_record

    def observe(self, state: dict[str, Any]) -> dict[str, Any]:
        app_state = self.fetch_app_state(state["target_identifier"])
        traversal = self.traversal_from_app_state(app_state)
        step_number = state["step_number"]
        artifact_dir = state["artifact_dir"]
        if self.debug_ax_grid_enabled:
            legacy.launch_debug_ax_grid(
                state["pid"],
                self.debug_ax_grid_duration,
                label=f"workflow step {step_number}",
                traversal=traversal,
                artifact_dir=artifact_dir,
            )
        app_profile = legacy.resolve_app_profile(state["target_identifier"], state["target_resolution"], traversal)
        capability_decision = self.resolve_capability_decision(state, traversal, app_profile)
        workflow_mode, visual_planning = legacy.apply_capability_decision(
            requested_mode=self.args.mode,
            requested_visual_planning=self.args.visual_planning,
            profile=app_profile,
            decision=capability_decision,
        )
        observation, elements, element_index, planner_images, planner_ax_index = self.build_step_observation_from_app_state(
            app_state,
            traversal=traversal,
            workflow_mode=workflow_mode,
            app_profile=app_profile,
            step_number=step_number,
            artifact_dir=artifact_dir,
            max_elements=self.args.max_elements,
            max_ocr_lines=self.args.max_ocr_lines,
            include_menus=self.args.include_menus,
            include_virtual_hints=not self.args.no_virtual_hints,
            visual_planning_enabled=visual_planning,
            visual_max_width=self.args.visual_max_width,
        )
        current_signature = legacy.observation_signature(elements)
        previous_step_record = state.get("previous_step_record")
        if previous_step_record is not None:
            legacy.verify_previous_text_input(previous_step_record, elements)
        if previous_step_record is not None and current_signature == previous_step_record.get("observation_signature_before"):
            self.maybe_apply_direct_ax_noop_fallback(
                state,
                previous_step_record,
                traversal,
                observation,
                elements,
                element_index,
                planner_images,
                current_signature,
                workflow_mode,
                app_profile,
                planner_ax_index,
            )
            app_state = state["app_state"]
            traversal = state["traversal"]
            observation = state["observation"]
            elements = state["elements"]
            element_index = state["element_index"]
            planner_images = state["planner_images"]
            planner_ax_index = state["planner_ax_index"]
            current_signature = state["current_signature"]
        completion_verification: dict[str, Any] | None = None
        if state["history"] and not state.get("done"):
            try:
                completion_verification = self.verify_task_completion(
                    state,
                    observation=observation,
                    elements=elements,
                )
            except Exception as exc:
                print(f"warning: completion verification failed: {exc}", file=sys.stderr)
        if completion_verification is not None:
            if previous_step_record is not None:
                previous_step_record["completion_verification"] = completion_verification
            if state["history"]:
                state["history"][-1]["completion_verification"] = completion_verification
            if completion_verification["status"] == "satisfied" and completion_verification["confidence"] >= 0.8:
                state["done"] = True
                state["auto_finish_summary"] = (
                    completion_verification.get("summary")
                    or "已确认当前界面已经满足用户请求，停止继续操作以避免重复执行。"
                )
                state["run_log"]["final_status"] = "finished"
            elif completion_verification["status"] == "blocked" and completion_verification["confidence"] >= 0.9:
                state["done"] = True
                state["auto_finish_summary"] = (
                    completion_verification.get("summary")
                    or "已确认当前任务被阻塞，需要人工继续。"
                )
                state["run_log"]["final_status"] = "blocked"
        if self.args.traversal_output:
            legacy.write_json(self.args.traversal_output, traversal)
        if self.args.debug_observation:
            legacy.print_observation_debug(step_number, elements)
            legacy.write_json(artifact_dir / f"step-{step_number:02d}-observation.json", observation)
        state.update(
            {
                "app_state": app_state,
                "traversal": traversal,
                "app_profile": app_profile,
                "capability_decision": capability_decision,
                "workflow_mode": workflow_mode,
                "visual_planning": visual_planning,
                "observation": observation,
                "elements": elements,
                "element_index": element_index,
                "planner_images": planner_images,
                "planner_ax_index": planner_ax_index,
                "current_signature": current_signature,
            }
        )
        state["run_log"]["workflow_mode"] = workflow_mode
        state["run_log"]["app_profile"] = app_profile.name
        state["run_log"]["app_guide_path"] = app_profile.guide_path
        state["run_log"]["app_guide_warnings"] = list(legacy.app_guide_warnings())
        state["run_log"]["capability_selection"] = capability_decision
        state["run_log"]["visual_planning"] = visual_planning
        return state

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("done") and state.get("auto_finish_summary"):
            final_status = state["run_log"].get("final_status")
            plan = {
                "status": "blocked" if final_status == "blocked" else "finished",
                "summary": state["auto_finish_summary"],
                "actions": [{"type": "finish"}],
            }
        else:
            plan = self.make_plan(state)
        actions = legacy.validate_plan(plan, state["element_index"], max_actions_per_step=self.args.max_actions_per_step)
        plan["actions"] = actions
        step_record: dict[str, Any] = {
            "step": state["step_number"],
            "target": {"app": state["traversal"].get("app_name", state["target_identifier"]), "pid": state["pid"]},
            "workflow_mode": state["workflow_mode"],
            "app_profile": state["app_profile"].name,
            "app_guide_path": state["app_profile"].guide_path,
            "capability_selection": state["capability_decision"],
            "visual_planning": state["visual_planning"],
            "element_count_sent_to_llm": len(state["elements"]),
            "traversal_stats": state["traversal"].get("stats", {}),
            "observation_signature_before": state["current_signature"],
            "observation_sources": {
                "ax_elements": len(state["observation"].get("ax_elements") or []),
                "ocr_lines": len(state["observation"].get("ocr_lines") or []),
                "profile_regions": len(state["observation"].get("profile_regions") or []),
                "screenshot_path": state["observation"].get("screenshot_path"),
                "visual_observation": state["observation"].get("visual_observation"),
            },
            "plan": plan,
        }
        step_record["action_elements"] = legacy.action_element_snapshots(actions, state["element_index"])
        if self.args.debug_observation:
            step_record["observation"] = state["observation"]
        state["run_log"]["steps"].append(step_record)
        state["plan"] = plan
        state["actions"] = actions
        state["plan_status"] = str(plan.get("status", "continue")).lower()
        state["step_record"] = step_record
        return state

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if not self.args.execute:
            state["run_log"]["final_status"] = "dry_run"
            state["done"] = True
            return state
        execution_results = self.execute_plan_via_mcp(
            state["actions"],
            state["element_index"],
            state["planner_ax_index"],
            target_identifier=state["target_identifier"],
            target_pid=state["pid"],
            app_profile=state["app_profile"],
        )
        state["step_record"]["execution_results"] = execution_results
        state["history"].append(
            {
                "step": state["step_number"],
                "status": state["plan_status"],
                "summary": state["plan"].get("summary"),
                "actions": state["actions"],
                "execution_results": execution_results,
            }
        )
        if state["plan_status"] in {"finished", "blocked"} or any(action.get("type") == "finish" for action in state["actions"]):
            state["run_log"]["final_status"] = state["plan_status"] if state["plan_status"] in {"finished", "blocked"} else "finished"
            state["done"] = True
            return state
        state["previous_step_record"] = state["step_record"]
        state["step_number"] += 1
        if state["step_number"] > self.max_steps:
            state["run_log"]["final_status"] = "max_steps_reached"
            state["done"] = True
            return state
        if self.args.plan_output:
            legacy.refresh_trace(state["run_log"])
            legacy.write_json(self.args.plan_output, state["run_log"])
        return state

    def finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        state["run_log"]["completed_at"] = time.time()
        legacy.refresh_trace(state["run_log"])
        return state

    def route_after_plan(self, state: dict[str, Any]) -> str:
        return "execute"

    def route_after_execute(self, state: dict[str, Any]) -> str:
        return "finalize" if state.get("done") else "observe"

    def resolve_capability_decision(
        self,
        state: dict[str, Any],
        traversal: dict[str, Any],
        app_profile: legacy.AppProfile,
    ) -> dict[str, Any]:
        should_select_with_llm = legacy.should_use_llm_capability_selection(
            self.args.capability_selection,
            app_profile,
            mock_plan=self.args.mock_plan,
        )
        if should_select_with_llm and not state["llm_capability_selection_done"]:
            state["llm_capability_selection_done"] = True
            try:
                return self.choose_app_capabilities(state, traversal, app_profile)
            except Exception as exc:
                fallback_mode = legacy.resolve_workflow_mode("auto", app_profile)
                fallback_visual = legacy.resolve_visual_planning("auto", fallback_mode, app_profile)
                decision = legacy.normalize_capability_decision(
                    {
                        "workflow_mode": fallback_mode,
                        "visual_planning": fallback_visual,
                        "reason": f"LLM capability selection failed; using profile fallback: {exc}",
                    },
                    fallback_workflow_mode=fallback_mode,
                    fallback_visual_planning=fallback_visual,
                    source="profile-fallback",
                )
                decision.update(
                    {
                        "profile": app_profile.name,
                        "profile_fixed_strategy": app_profile.fixed_strategy,
                        "error": str(exc),
                    }
                )
                print(f"warning: LLM capability selection failed, using profile fallback: {exc}", file=sys.stderr)
                return decision
        if not should_select_with_llm:
            profile_mode, profile_visual = legacy.apply_capability_decision(
                requested_mode=self.args.mode,
                requested_visual_planning=self.args.visual_planning,
                profile=app_profile,
                decision=None,
            )
            return legacy.profile_capability_decision(app_profile, profile_mode, profile_visual)
        return state["capability_decision"]

    def choose_app_capabilities(
        self,
        state: dict[str, Any],
        traversal: dict[str, Any],
        app_profile: legacy.AppProfile,
    ) -> dict[str, Any]:
        fallback_workflow_mode = legacy.resolve_workflow_mode("auto", app_profile)
        fallback_visual_planning = legacy.resolve_visual_planning("auto", fallback_workflow_mode, app_profile)
        elements, _ = legacy.summarize_elements(
            traversal,
            max_elements=120,
            include_menus=self.args.include_menus,
            include_virtual_hints=False,
        )
        ax_summary = legacy.capability_ax_summary(traversal, elements)
        screenshot_path: Path | None = None
        planner_image_path: Path | None = None
        planner_images: list[str] = []
        screenshot_error: str | None = None
        screenshot_payload = (state.get("app_state") or {}).get("screenshot") or {}
        screenshot_candidate = screenshot_payload.get("path")
        if isinstance(screenshot_candidate, str) and screenshot_candidate:
            screenshot_path = Path(screenshot_candidate)
        if screenshot_path is not None and screenshot_path.exists():
            try:
                planner_image_path = legacy.prepare_visual_planner_image(
                    screenshot_path,
                    artifact_dir=state["artifact_dir"],
                    step_number=0,
                    max_width=self.args.visual_max_width,
                )
                planner_images.append(legacy.image_file_base64(planner_image_path))
            except Exception as exc:
                screenshot_error = str(exc)
        prompt = legacy.build_capability_selection_prompt(
            user_instruction=self.args.instruction,
            target_identifier=state["target_identifier"],
            target_resolution=state["target_resolution"],
            traversal=traversal,
            app_profile=app_profile,
            fallback_workflow_mode=fallback_workflow_mode,
            fallback_visual_planning=fallback_visual_planning,
            elements=elements,
            ax_summary=ax_summary,
            screenshot_attached=bool(planner_images),
        )
        raw, _ = self.invoke_llm_with_trace(
            prompt=prompt,
            artifact_dir=state["artifact_dir"],
            kind="capability_selection",
            step_number=0,
            model_name=self.args.model,
            image_base64=planner_images or None,
            extra={
                "workflow_mode_fallback": fallback_workflow_mode,
                "visual_planning_fallback": fallback_visual_planning,
                "profile": app_profile.name,
            },
            run_log=state.get("run_log"),
        )
        decision = legacy.normalize_capability_decision(
            legacy.parse_llm_json_object(raw),
            fallback_workflow_mode=fallback_workflow_mode,
            fallback_visual_planning=fallback_visual_planning,
            source="llm",
        )
        decision.update(
            {
                "profile": app_profile.name,
                "profile_fixed_strategy": app_profile.fixed_strategy,
                "app_guide_path": app_profile.guide_path,
                "ax_summary": ax_summary,
                "screenshot_path": os.fspath(screenshot_path) if screenshot_path else None,
                "planner_image_path": os.fspath(planner_image_path) if planner_image_path else None,
                "image_attached_to_selector": bool(planner_images),
                "screenshot_error": screenshot_error,
            }
        )
        return decision

    def make_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.args.mock_plan:
            return legacy.fallback_plan(self.args.instruction, state["element_index"], state["history"])
        planner_elements = _compact_elements_for_prompt(state["elements"])
        planner_observation = _compact_observation_for_prompt(state["observation"], state["elements"])
        planner_history = _compact_history_for_prompt(state["history"])
        progress_summary = _build_progress_summary_from_steps(
            state.get("run_log", {}).get("steps", []),
            state["app_profile"].name,
        )
        prompt = legacy.build_planner_prompt(
            self.args.instruction,
            state["target_identifier"],
            state["traversal"],
            planner_elements,
            planner_observation,
            planner_history,
            step_number=state["step_number"],
            max_steps=self.max_steps,
            max_actions_per_step=self.args.max_actions_per_step,
            workflow_mode=state["workflow_mode"],
            app_profile=state["app_profile"],
        )
        prompt = _inject_progress_summary_into_prompt(prompt, progress_summary)
        try:
            raw, _ = self.invoke_llm_with_trace(
                prompt=prompt,
                artifact_dir=state["artifact_dir"],
                kind="planner",
                step_number=state["step_number"],
                model_name=self.args.model,
                image_base64=state["planner_images"] or None,
                extra={
                    "workflow_mode": state["workflow_mode"],
                    "app_profile": state["app_profile"].name,
                    "history_length": len(state["history"]),
                    "element_count": len(state["elements"]),
                    "planner_element_count": len(planner_elements),
                    "planner_history_length": len(planner_history),
                    "progress_summary": progress_summary,
                },
                run_log=state.get("run_log"),
            )
            return legacy.parse_llm_plan(raw)
        except Exception as exc:
            allow_fallback = not self.args.no_fallback and (not self.args.execute or self.args.mock_plan)
            if not allow_fallback:
                raise
            print(f"warning: LLM planning failed, using fallback plan: {exc}", file=sys.stderr)
            return legacy.fallback_plan(self.args.instruction, state["element_index"], state["history"])

    def verify_task_completion(
        self,
        state: dict[str, Any],
        *,
        observation: dict[str, Any],
        elements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        verifier_elements = _elements_for_completion_verifier(elements)
        verifier_observation = _compact_observation_for_prompt(observation, verifier_elements)
        verifier_history = _compact_history_for_prompt(state["history"])
        progress_summary = _build_progress_summary_from_steps(
            state.get("run_log", {}).get("steps", []),
            state["app_profile"].name,
        )
        prompt = _build_completion_verifier_prompt(
            user_instruction=self.args.instruction,
            target_identifier=state["target_identifier"],
            workflow_mode=state["workflow_mode"],
            app_profile_name=state["app_profile"].name,
            step_number=state["step_number"],
            max_steps=self.max_steps,
            progress_summary=progress_summary,
            observation=verifier_observation,
            elements=verifier_elements,
            recent_history=verifier_history,
        )
        raw, _ = self.invoke_llm_with_trace(
            prompt=prompt,
            artifact_dir=state["artifact_dir"],
            kind="completion_verifier",
            step_number=state["step_number"],
            model_name=self.args.model,
            extra={
                "workflow_mode": state["workflow_mode"],
                "app_profile": state["app_profile"].name,
                "history_length": len(state["history"]),
                "verifier_element_count": len(verifier_elements),
                "verifier_history_length": len(verifier_history),
                "progress_summary": progress_summary,
            },
            run_log=state.get("run_log"),
        )
        return _normalize_completion_verdict(legacy.parse_llm_json_object(raw))

    def maybe_apply_direct_ax_noop_fallback(
        self,
        state: dict[str, Any],
        previous_step_record: dict[str, Any],
        traversal: dict[str, Any],
        observation: dict[str, Any],
        elements: list[dict[str, Any]],
        element_index: dict[str, legacy.UiElement],
        planner_images: list[str],
        current_signature: str,
        workflow_mode: str,
        app_profile: legacy.AppProfile,
        planner_ax_index: dict[str, str],
    ) -> None:
        execution_results = previous_step_record.get("execution_results") or []
        direct_ax_result = next((item for item in execution_results if isinstance(item, dict) and legacy.direct_ax_click_candidate(item)), None)
        if direct_ax_result is not None and legacy.should_auto_coordinate_fallback_from_direct_ax(app_profile):
            action = direct_ax_result.get("action") or {}
            snapshots = previous_step_record.get("action_elements") or []
            snapshot = next(
                (
                    item
                    for item in snapshots
                    if isinstance(item, dict) and item.get("element_id") == action.get("element_id")
                ),
                snapshots[0] if snapshots else None,
            )
            center = snapshot.get("center") if isinstance(snapshot, dict) else None
            if isinstance(center, dict):
                x = center.get("x")
                y = center.get("y")
                if x is not None and y is not None:
                    result = self.mcp.call_tool(
                        "click",
                        {"app": state["target_identifier"], "x": float(x), "y": float(y)},
                    )
                    text = _content_text(result)
                    fallback_result = {
                        "index": 1,
                        "action": action,
                        "ok": _mcp_call_succeeded(result, text),
                        "activated_pid": state["pid"],
                        "activation": "mcp_session",
                        "mode": "coordinate",
                        "fallback_from": "direct_ax",
                        "fallback_reason": "direct_ax_no_observation_change",
                        "point": {"x": float(x), "y": float(y)},
                        "tool_output": text[-1000:],
                    }
                    execution_results.append(fallback_result)
                    previous_step_record["execution_results"] = execution_results
                    previous_step_record["direct_ax_noop_fallback"] = True
                    if state["history"]:
                        state["history"][-1]["execution_results"] = execution_results
                    app_state = self.fetch_app_state(state["target_identifier"])
                    traversal = self.traversal_from_app_state(app_state)
                    observation, elements, element_index, planner_images, planner_ax_index = self.build_step_observation_from_app_state(
                        app_state,
                        traversal=traversal,
                        workflow_mode=workflow_mode,
                        app_profile=app_profile,
                        step_number=state["step_number"],
                        artifact_dir=state["artifact_dir"],
                        max_elements=self.args.max_elements,
                        max_ocr_lines=self.args.max_ocr_lines,
                        include_menus=self.args.include_menus,
                        include_virtual_hints=not self.args.no_virtual_hints,
                        visual_planning_enabled=state["visual_planning"],
                        visual_max_width=self.args.visual_max_width,
                    )
                    current_signature = legacy.observation_signature(elements)
                    state["app_state"] = app_state
        elif direct_ax_result is not None:
            previous_step_record["direct_ax_noop_fallback"] = False
            previous_step_record["direct_ax_noop_fallback_skipped"] = "disabled_for_app_profile"
            if state["history"]:
                state["history"][-1]["direct_ax_noop_fallback"] = False
                state["history"][-1]["direct_ax_noop_fallback_skipped"] = "disabled_for_app_profile"
        state.update(
            {
                "traversal": traversal,
                "observation": observation,
                "elements": elements,
                "element_index": element_index,
                "planner_images": planner_images,
                "planner_ax_index": planner_ax_index,
                "current_signature": current_signature,
            }
        )

    def fetch_app_state(self, app: str) -> dict[str, Any]:
        result = self.mcp.call_tool(
            "get_app_state",
            {
                "app": app,
                "observation_mode": "ax_ocr",
                "summary_mode": "metadata",
            },
        )
        text = _content_text(result)
        if result.get("isError"):
            raise RuntimeError(text or f"get_app_state failed for {app}")
        state_path = _extract_marker_path(text, "state")
        if state_path is None or not state_path.exists():
            raise RuntimeError(f"get_app_state did not return a readable state artifact: {text[:500]}")
        return json.loads(state_path.read_text(encoding="utf-8"))

    def traversal_from_app_state(self, app_state: dict[str, Any]) -> dict[str, Any]:
        traversal = dict(app_state.get("traversal") or {})
        traversal.setdefault("app_name", app_state.get("appName") or app_state.get("bundleIdentifier") or "")
        traversal.setdefault("stats", {})
        return traversal

    def build_step_observation_from_app_state(
        self,
        app_state: dict[str, Any],
        *,
        traversal: dict[str, Any],
        workflow_mode: str,
        app_profile: legacy.AppProfile,
        step_number: int,
        artifact_dir: Path,
        max_elements: int,
        max_ocr_lines: int,
        include_menus: bool,
        include_virtual_hints: bool,
        visual_planning_enabled: bool,
        visual_max_width: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, legacy.UiElement], list[str], dict[str, str]]:
        ax_elements, element_index = legacy.summarize_elements(
            traversal,
            max_elements=max_elements,
            include_menus=include_menus,
            include_virtual_hints=include_virtual_hints,
        )
        planner_ax_index = self.map_planner_ax_indices(ax_elements, element_index, app_state.get("elements") or [])
        combined_elements = list(ax_elements)
        screenshot_payload = app_state.get("screenshot") or {}
        screenshot_path = screenshot_payload.get("path") if isinstance(screenshot_payload.get("path"), str) else None
        window_frame = legacy.window_region_from_traversal(traversal)
        if window_frame is None and isinstance(screenshot_payload.get("windowFrame"), dict):
            frame = screenshot_payload["windowFrame"]
            try:
                window_frame = (
                    float(frame["x"]),
                    float(frame["y"]),
                    float(frame["width"]),
                    float(frame["height"]),
                )
            except (KeyError, TypeError, ValueError):
                window_frame = None
        ocr_payload = app_state.get("ocrPayload") if isinstance(app_state.get("ocrPayload"), dict) else None
        ocr_error = app_state.get("ocrError")
        ocr_elements: list[dict[str, Any]] = []
        if ocr_payload is not None:
            ocr_elements = legacy.summarize_ocr_lines(ocr_payload, element_index, max_lines=max_ocr_lines)
            combined_elements.extend(ocr_elements)
        profile_region_elements: list[dict[str, Any]] = []
        if workflow_mode == "ax-poor" and window_frame is not None:
            profile_region_elements = legacy.add_profile_regions(
                legacy.profile_regions_for_window(app_profile, window_frame),
                element_index,
            )
            combined_elements.extend(profile_region_elements)
        planner_images: list[str] = []
        visual_observation: dict[str, Any] = {
            "enabled": visual_planning_enabled,
            "image_attached_to_planner": False,
            "screenshot_path": screenshot_path,
            "planner_image_path": None,
            "error": None,
        }
        if visual_planning_enabled and screenshot_path and Path(screenshot_path).exists():
            try:
                planner_image_path = legacy.prepare_visual_planner_image(
                    Path(screenshot_path),
                    artifact_dir=artifact_dir,
                    step_number=step_number,
                    max_width=visual_max_width,
                )
                planner_images.append(legacy.image_file_base64(planner_image_path))
                visual_observation = {
                    "enabled": True,
                    "image_attached_to_planner": True,
                    "screenshot_path": screenshot_path,
                    "planner_image_path": os.fspath(planner_image_path),
                    "coordinate_space": {
                        "frame": "screen_points_top_left",
                        "screenshot_region": {
                            "x": window_frame[0],
                            "y": window_frame[1],
                            "width": window_frame[2],
                            "height": window_frame[3],
                        } if window_frame is not None else None,
                        "planner_rule": (
                            "The attached image is the captured app window. "
                            "Raw coordinate actions must use top-left screen points, not image pixels."
                        ),
                    },
                    "error": None,
                }
            except Exception as exc:
                visual_observation["error"] = str(exc)
                print(f"warning: visual planner image attachment failed: {str(exc)[-800:]}", file=sys.stderr)
        observation = {
            "workflow_mode": workflow_mode,
            "app_profile": app_profile.name,
            "app_guide_path": app_profile.guide_path,
            "stats": traversal.get("stats", {}),
            "screenshot_path": screenshot_path,
            "ax_elements": ax_elements,
            "ocr_lines": ocr_elements,
            "profile_regions": profile_region_elements,
            "ocr_error": ocr_error,
            "visual_observation": visual_observation,
        }
        if ocr_payload is not None:
            observation["ocr_payload"] = ocr_payload
        return observation, combined_elements, element_index, planner_images, planner_ax_index

    def map_planner_ax_indices(
        self,
        ax_elements: list[dict[str, Any]],
        element_index: dict[str, legacy.UiElement],
        mcp_elements: list[dict[str, Any]],
    ) -> dict[str, str]:
        by_path: dict[str, str] = {}
        for item in mcp_elements:
            if not isinstance(item, dict) or item.get("source") != "ax":
                continue
            ax_path = legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000)
            index = item.get("index")
            if ax_path and index is not None:
                by_path[ax_path] = str(index)
        mapping: dict[str, str] = {}
        for item in ax_elements:
            element_id = str(item.get("id") or "")
            ui_element = element_index.get(element_id)
            if ui_element is None or ui_element.ax_path is None:
                continue
            mcp_index = by_path.get(ui_element.ax_path)
            if mcp_index is not None:
                mapping[element_id] = mcp_index
        return mapping

    def execute_plan_via_mcp(
        self,
        actions: list[dict[str, Any]],
        element_index: dict[str, legacy.UiElement],
        planner_ax_index: dict[str, str],
        *,
        target_identifier: str,
        target_pid: int,
        app_profile: legacy.AppProfile | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i, action in enumerate(actions, start=1):
            action_type = action["type"]
            print(f"executing action {i}: {json.dumps(action, ensure_ascii=False)}", file=sys.stderr)

            if action_type == "finish":
                results.append({"index": i, "action": action, "ok": True})
                return results
            if action_type == "wait":
                time.sleep(float(action.get("seconds", 1.0)))
                results.append({"index": i, "action": action, "ok": True})
                continue

            if action_type in {"click", "doubleclick", "rightclick"}:
                element = legacy.action_element(action, element_index)
                point = legacy.action_point(action, element_index)
                direct_ax_failure: str | None = None
                if action_type == "click" and element is not None and element.ax_path is not None:
                    mcp_element_index = planner_ax_index.get(element.element_id)
                    if mcp_element_index is not None:
                        result = self.mcp.call_tool(
                            "perform_secondary_action",
                            {"app": target_identifier, "element_index": mcp_element_index, "action": "Press"},
                        )
                        text = _content_text(result)
                        if _mcp_call_succeeded(result, text):
                            results.append(
                                {
                                    "index": i,
                                    "action": action,
                                    "ok": True,
                                    "activated_pid": target_pid,
                                    "activation": "mcp_session",
                                    "target_pid": target_pid,
                                    "mode": "direct_ax",
                                    "ax_path": element.ax_path,
                                    "tool_output": text[-1000:],
                                }
                            )
                            time.sleep(0.2)
                            continue
                        direct_ax_failure = text[-1000:] or "perform_secondary_action returned an unsuccessful result"
                        if legacy.is_feishu_lark_context(target_identifier, app_profile):
                            results.append(
                                {
                                    "index": i,
                                    "action": action,
                                    "ok": False,
                                    "activated_pid": target_pid,
                                    "activation": "mcp_session",
                                    "target_pid": target_pid,
                                    "mode": "direct_ax",
                                    "ax_path": element.ax_path,
                                    "fallback_skipped": "coordinate_fallback_disabled_for_feishu_lark",
                                    "error": direct_ax_failure,
                                }
                            )
                            time.sleep(0.2)
                            continue
                        print(
                            "warning: direct AX activation failed; falling back to coordinate input: "
                            f"{direct_ax_failure[-800:]}",
                            file=sys.stderr,
                        )
                arguments: dict[str, Any] = {"app": target_identifier, "x": point[0], "y": point[1]}
                if action_type == "doubleclick":
                    arguments["click_count"] = 2
                elif action_type == "rightclick":
                    arguments["mouse_button"] = "right"
                result = self.mcp.call_tool("click", arguments)
                text = _content_text(result)
                results.append(
                    {
                        "index": i,
                        "action": action,
                        "ok": _mcp_call_succeeded(result, text),
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "mode": "coordinate",
                        "fallback_from": "direct_ax" if direct_ax_failure else None,
                        "direct_ax_error": direct_ax_failure,
                        "point": {"x": point[0], "y": point[1]},
                        "tool_output": text[-1000:],
                    }
                )
                time.sleep(0.2)
                continue

            if action_type == "mousemove":
                point = legacy.action_point(action, element_index)
                results.append(
                    {
                        "index": i,
                        "action": action,
                        "ok": False,
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "mode": "unsupported",
                        "point": {"x": point[0], "y": point[1]},
                        "error": "mousemove is not exposed by tactile-macos-mcp",
                    }
                )
                time.sleep(0.2)
                continue

            if action_type == "scroll":
                element = legacy.action_element(action, element_index)
                direction, pages = _scroll_direction_and_pages(action)
                arguments: dict[str, Any] = {
                    "app": target_identifier,
                    "direction": direction,
                    "pages": pages,
                }
                mcp_element_index = planner_ax_index.get(element.element_id) if element is not None else None
                if mcp_element_index is not None:
                    arguments["element_index"] = mcp_element_index
                else:
                    point = legacy.action_point(action, element_index)
                    arguments["x"] = point[0]
                    arguments["y"] = point[1]
                result = self.mcp.call_tool("scroll", arguments)
                text = _content_text(result)
                results.append(
                    {
                        "index": i,
                        "action": action,
                        "ok": _mcp_call_succeeded(result, text),
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "tool_output": text[-1000:],
                    }
                )
                time.sleep(0.2)
                continue

            if action_type == "writetext":
                text = str(action.get("text", ""))
                element = legacy.action_element(action, element_index)
                existing_text_match = legacy.text_already_present_in_text_target(text, element, element_index)
                if existing_text_match is not None:
                    results.append(
                        {
                            "index": i,
                            "action": {
                                "type": "writetext",
                                "element_id": existing_text_match.get("element_id"),
                                "text_length": len(text),
                            },
                            "ok": True,
                            "activated_pid": target_pid,
                            "activation": "mcp_session",
                            "skipped": "text_already_present_in_text_target",
                            "input_diagnostics": {
                                "skip_reason": "text_already_present_in_text_target",
                                "existing_text_match": existing_text_match,
                            },
                        }
                    )
                    time.sleep(0.2)
                    continue
                input_diagnostics: dict[str, Any] = {
                    "text_length": len(text),
                    "element_id": element.element_id if element is not None else None,
                    "app_profile": legacy.app_profile_name(app_profile) or None,
                }
                if element is not None:
                    mcp_element_index = planner_ax_index.get(element.element_id)
                    if mcp_element_index is not None and element.ax_path is not None:
                        focus_result = self.mcp.call_tool(
                            "perform_secondary_action",
                            {"app": target_identifier, "element_index": mcp_element_index, "action": "Focus"},
                        )
                        focus_text = _content_text(focus_result)
                        input_diagnostics["focus"] = {
                            "method": "perform_secondary_action",
                            "ok": _mcp_call_succeeded(focus_result, focus_text),
                            "tool_output": focus_text[-500:],
                        }
                        if not _mcp_call_succeeded(focus_result, focus_text):
                            results.append(
                                {
                                    "index": i,
                                    "action": {
                                        "type": "writetext",
                                        "element_id": element.element_id,
                                        "text_length": len(text),
                                    },
                                    "ok": False,
                                    "activated_pid": target_pid,
                                    "activation": "mcp_session",
                                    "mode": "focus_failed",
                                    "input_diagnostics": input_diagnostics,
                                    "error": focus_text[-1000:] or "failed to focus target element",
                                }
                            )
                            time.sleep(0.2)
                            continue
                    else:
                        point = element.center
                        focus_result = self.mcp.call_tool("click", {"app": target_identifier, "x": point[0], "y": point[1]})
                        focus_text = _content_text(focus_result)
                        input_diagnostics["focus"] = {
                            "method": "coordinate_click",
                            "ok": _mcp_call_succeeded(focus_result, focus_text),
                            "tool_output": focus_text[-500:],
                        }
                        if not _mcp_call_succeeded(focus_result, focus_text):
                            results.append(
                                {
                                    "index": i,
                                    "action": {
                                        "type": "writetext",
                                        "element_id": element.element_id,
                                        "text_length": len(text),
                                    },
                                    "ok": False,
                                    "activated_pid": target_pid,
                                    "activation": "mcp_session",
                                    "mode": "focus_failed",
                                    "input_diagnostics": input_diagnostics,
                                    "error": focus_text[-1000:] or "failed to focus target coordinates",
                                }
                            )
                            time.sleep(0.2)
                            continue
                result = self.mcp.call_tool("type_text", {"app": target_identifier, "text": text})
                tool_text = _content_text(result)
                results.append(
                    {
                        "index": i,
                        "action": {
                            "type": "writetext",
                            "element_id": element.element_id if element is not None else None,
                            "text_length": len(text),
                        },
                        "ok": _mcp_call_succeeded(result, tool_text),
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "mode": "keyboard",
                        "input_method": "mcp_type_text",
                        "input_diagnostics": input_diagnostics,
                        "tool_output": tool_text[-1000:],
                    }
                )
                time.sleep(0.2)
                continue

            if action_type == "keypress":
                key = _normalize_keypress(str(action.get("key") or action.get("keys") or ""))
                if not key:
                    raise ValueError(f"keypress action needs key: {action!r}")
                result = self.mcp.call_tool("press_key", {"app": target_identifier, "key": key})
                tool_text = _content_text(result)
                results.append(
                    {
                        "index": i,
                        "action": action,
                        "ok": _mcp_call_succeeded(result, tool_text),
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "tool_output": tool_text[-1000:],
                    }
                )
                time.sleep(0.2)
                continue

            raise ValueError(f"unhandled action type: {action_type}")
        return results


def build_summary(run_log: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_status": run_log.get("final_status"),
        "workflow_mode": run_log.get("workflow_mode"),
        "app_profile": run_log.get("app_profile"),
        "app_guide_path": run_log.get("app_guide_path"),
        "capability_selection": run_log.get("capability_selection"),
        "visual_planning": run_log.get("visual_planning"),
        "target": run_log.get("target"),
        "instruction": run_log.get("instruction"),
        "steps": len(run_log.get("steps", [])),
        "artifact_dir": run_log.get("artifact_dir"),
        "llm_call_dir": run_log.get("llm_call_dir"),
        "llm_calls": len(run_log.get("llm_calls", [])),
        "plan_output": run_log.get("plan_output"),
    }


def run_workflow(args: argparse.Namespace, *, mcp_client: MCPClient | Any | None = None) -> dict[str, Any]:
    runner = LangGraphComputerUseWorkflow(args, mcp_client=mcp_client)
    try:
        recursion_limit = max(50, runner.max_steps * 4 + 12)
        final_state = runner.build_graph().invoke({}, config={"recursion_limit": recursion_limit})
    finally:
        runner.close()
    run_log = final_state["run_log"]
    if args.plan_output:
        legacy.write_json(args.plan_output, run_log)
    return run_log


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_apps:
        return list_apps(args)
    if not args.instruction.strip():
        parser.error("instruction is required unless --list-apps is used")
    if args.execute and args.plan_output is None:
        args.plan_output = legacy.default_artifact_path("workflow-run", ".json", cwd=Path.cwd())
    args.plan_output = legacy.session_scoped_output_path(args.plan_output)
    args.traversal_output = legacy.session_scoped_output_path(args.traversal_output)
    run_log = run_workflow(args)
    print(json.dumps(build_summary(run_log), ensure_ascii=False, indent=2))
    if not args.execute:
        print("dry-run only; pass --execute to operate the UI", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
