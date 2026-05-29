#!/usr/bin/env python3
"""LangGraph-based macOS computer-use workflow backed by tactile MCP."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

WORKFLOW_DIR = Path(__file__).resolve().parent
LEGACY_WORKFLOW_PATH = WORKFLOW_DIR / "codex_llm_workflow.py"
PROJECT_ROOT = WORKFLOW_DIR.parents[3]
MCP_ROOT = PROJECT_ROOT / "mcps" / "tactile-macos-mcp"
MCP_SERVER = MCP_ROOT / "bin" / "tactile-macos-mcp"


@dataclass(frozen=True)
class WorkflowStage:
    name: str
    instruction: str
    target: str | None = None
    expects_share_text: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)




def load_legacy_workflow():
    spec = importlib.util.spec_from_file_location("_tactile_legacy_codex_llm_workflow", LEGACY_WORKFLOW_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load legacy workflow module from {LEGACY_WORKFLOW_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


legacy = load_legacy_workflow()

MCP_SECONDARY_ACTIONS = {
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
EXTENDED_ACTION_TYPES = set(legacy.ALLOWED_ACTION_TYPES) | {"secondary_action", "drag"}


DEFAULT_LLM_MODEL = os.getenv("TACTILE_MODEL", "gpt-5.5")
LLM_TIMEOUT_SECONDS = float(os.getenv("TACTILE_LLM_TIMEOUT", "600"))
LLM_MAX_RETRIES = int(os.getenv("TACTILE_LLM_MAX_RETRIES", "3"))
MCP_TIMEOUT_SECONDS = float(os.getenv("TACTILE_MCP_TIMEOUT", "20"))
PRECISE_SCROLL_PAGES = float(os.getenv("TACTILE_PRECISE_SCROLL_PAGES", "0.2"))
COLLECTION_ALIGNMENT_MAX_ATTEMPTS = int(os.getenv("TACTILE_COLLECTION_ALIGNMENT_MAX_ATTEMPTS", "12"))
COLLECTION_DRAG_MIN_POINTS = float(os.getenv("TACTILE_COLLECTION_DRAG_MIN_POINTS", "18"))
COLLECTION_DRAG_PROBE_RATIO = float(os.getenv("TACTILE_COLLECTION_DRAG_PROBE_RATIO", "0.32"))
COLLECTION_VALUE_PROBE_RATIO = float(os.getenv("TACTILE_COLLECTION_VALUE_PROBE_RATIO", "0.6"))
COLLECTION_DRAG_MAX_RATIO = float(os.getenv("TACTILE_COLLECTION_DRAG_MAX_RATIO", "0.9"))
COLLECTION_DRAG_MAX_POINTS = float(os.getenv("TACTILE_COLLECTION_DRAG_MAX_POINTS", "280"))
COLLECTION_DRAG_OVERSHOOT_RATIO = float(os.getenv("TACTILE_COLLECTION_DRAG_OVERSHOOT_RATIO", "1.1"))
OPEN_COLLECTION_CONTAINER_ROLES = {
    "AXList",
    "AXMenu",
    "AXTable",
    "AXOutline",
    "AXBrowser",
    "AXScrollArea",
    "AXScrollBar",
}
OPEN_COLLECTION_TRIGGER_ROLES = {
    "AXMenuButton",
    "AXPopUpButton",
    "AXComboBox",
}
OPEN_COLLECTION_VALUE_SOURCE_ROLES = OPEN_COLLECTION_TRIGGER_ROLES | {
    "AXTextField",
}
OPEN_COLLECTION_OPTION_ROLES = {
    "AXMenuItem",
    "AXRow",
    "AXCell",
    "AXButton",
    "AXRadioButton",
    "AXCheckBox",
    "AXStaticText",
}
OPEN_COLLECTION_AUGMENT_LIMIT = 96
VISIBLE_OPTION_SELECTOR_CANDIDATE_LIMIT = 72


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


def _requested_visual_planning_for_profile(requested_mode: str, profile: legacy.AppProfile) -> str:
    if requested_mode == "off" and profile.fixed_strategy and profile.workflow_mode == "ax-poor":
        return "auto"
    return requested_mode


def _normalize_secondary_action_name(name: str | None) -> str:
    cleaned = str(name or "").strip()
    if cleaned.startswith("AX") and len(cleaned) > 2:
        return cleaned[2:]
    return cleaned


def _frame_tuple_from_mapping(frame: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(frame, dict):
        return None
    try:
        x = float(frame["x"])
        y = float(frame["y"])
        width = float(frame["width"])
        height = float(frame["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (x, y, width, height)


def _frame_tuple_from_summary_element(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    return _frame_tuple_from_mapping(element.get("frame"))


def _frame_tuple_from_raw_ax_item(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    return _frame_tuple_from_mapping(item.get("screenFrame") or item.get("screen_frame"))


def _frame_center(frame: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, width, height = frame
    return (x + width / 2.0, y + height / 2.0)


def _frame_distance(frame_a: tuple[float, float, float, float], frame_b: tuple[float, float, float, float]) -> float:
    ax, ay = _frame_center(frame_a)
    bx, by = _frame_center(frame_b)
    return abs(ax - bx) + abs(ay - by)


def _frame_contains_with_margin(
    container: tuple[float, float, float, float],
    frame: tuple[float, float, float, float],
    *,
    margin_x: float = 56.0,
    margin_y: float = 56.0,
) -> bool:
    px, py = _frame_center(frame)
    x, y, width, height = container
    return x - margin_x <= px <= x + width + margin_x and y - margin_y <= py <= y + height + margin_y


def _element_base_role_from_summary(element: dict[str, Any]) -> str:
    return legacy.base_role(str(element.get("role") or ""))


def _element_base_role_from_raw_ax(item: dict[str, Any]) -> str:
    return legacy.base_role(str(item.get("role") or ""))


def _is_open_collection_container_role(role_base: str, frame: tuple[float, float, float, float] | None) -> bool:
    if role_base in OPEN_COLLECTION_CONTAINER_ROLES:
        return True
    if role_base == "AXWindow" and frame is not None:
        _, _, width, height = frame
        return width <= 360 and height <= 680
    return False


def _is_open_collection_option_role(role_base: str) -> bool:
    return role_base in OPEN_COLLECTION_OPTION_ROLES


def _next_ax_element_id(index: dict[str, legacy.UiElement]) -> str:
    next_id = max((int(key[1:]) for key in index if key.startswith("e") and key[1:].isdigit()), default=-1) + 1
    return f"e{next_id}"


def _normalized_secondary_actions(actions: Any) -> list[str]:
    if not isinstance(actions, list):
        return []
    return [
        normalized
        for action in actions
        if (normalized := _normalize_secondary_action_name(action))
    ]


def _collection_child_order(ax_path: str | None, container_ax_path: str | None) -> int | None:
    if not ax_path or not container_ax_path:
        return None
    prefix = f"{container_ax_path}.children["
    if not ax_path.startswith(prefix):
        return None
    suffix = ax_path[len(prefix) :]
    digits: list[str] = []
    for ch in suffix:
        if ch.isdigit():
            digits.append(ch)
            continue
        break
    if not digits:
        return None
    if len(suffix) <= len(digits) or suffix[len(digits)] != "]":
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


def _collection_container_for_ax_path(ax_path: str | None, containers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not ax_path:
        return None
    best: dict[str, Any] | None = None
    best_length = -1
    for container in containers:
        container_ax_path = container.get("ax_path")
        if not isinstance(container_ax_path, str) or not container_ax_path:
            continue
        if ax_path == container_ax_path:
            return container
        if ax_path.startswith(f"{container_ax_path}.children[") and len(container_ax_path) > best_length:
            best = container
            best_length = len(container_ax_path)
    return best


def _collection_membership_from_frame(
    frame: tuple[float, float, float, float] | None,
    containers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if frame is None:
        return None
    for container in containers:
        if _frame_contains_with_margin(container["frame"], frame):
            return container
    return None


def _collection_metadata_for_candidate(
    *,
    ax_path: str | None,
    frame: tuple[float, float, float, float] | None,
    containers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    container = _collection_container_for_ax_path(ax_path, containers)
    if container is None:
        container = _collection_membership_from_frame(frame, containers)
    if container is None:
        return None
    container_ax_path = container.get("ax_path")
    order = _collection_child_order(ax_path, container_ax_path) if isinstance(container_ax_path, str) else None
    visible_in_viewport = bool(frame is not None and _frame_contains_with_margin(container["frame"], frame, margin_x=8.0, margin_y=8.0))
    return {
        "container_ax_path": container_ax_path,
        "container_role": container.get("role") or container.get("role_base"),
        "container_frame": container.get("frame"),
        "order": order,
        "visible_in_viewport": visible_in_viewport,
    }


def _collection_container_records_from_raw_ax(raw_ax_elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for item in raw_ax_elements:
        if not isinstance(item, dict) or item.get("source") != "ax":
            continue
        frame = _frame_tuple_from_raw_ax_item(item)
        role_base = _element_base_role_from_raw_ax(item)
        if not _is_open_collection_container_role(role_base, frame):
            continue
        if frame is None:
            continue
        containers.append(
            {
                "role_base": role_base,
                "role": str(item.get("role") or ""),
                "text": legacy.clean_text(item.get("text"), limit=240),
                "frame": frame,
                "ax_path": legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000),
            }
        )
    return containers


def _collection_container_records_from_elements(
    elements: list[dict[str, Any]],
    *,
    preferred_element_id: str | None = None,
    element_index: dict[str, legacy.UiElement] | None = None,
) -> list[dict[str, Any]]:
    preferred: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        frame = _frame_tuple_from_summary_element(element)
        role_base = _element_base_role_from_summary(element)
        if not _is_open_collection_container_role(role_base, frame):
            continue
        if frame is None:
            continue
        record = {
            "id": element.get("id"),
            "source": element.get("source"),
            "role": element.get("role"),
            "direct_ax": element.get("direct_ax"),
            "role_base": role_base,
            "text": legacy.clean_text(element.get("text"), limit=240),
            "frame": frame,
            "ax_path": (
                element_index.get(str(element.get("id") or "")).ax_path
                if element_index and isinstance(element_index.get(str(element.get("id") or "")), legacy.UiElement)
                else None
            ),
        }
        if preferred_element_id and str(element.get("id") or "") == preferred_element_id:
            preferred.append(record)
        else:
            others.append(record)
    return preferred + others


def _is_candidate_near_any_collection(
    candidate_frame: tuple[float, float, float, float],
    containers: list[dict[str, Any]],
) -> bool:
    return any(_frame_contains_with_margin(container["frame"], candidate_frame) for container in containers)


def _collection_option_sort_key(element: dict[str, Any], containers: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    role_base = _element_base_role_from_summary(element)
    role_priority = {
        "AXMenuItem": 0.0,
        "AXRow": 1.0,
        "AXCell": 1.0,
        "AXButton": 2.0,
        "AXRadioButton": 2.0,
        "AXCheckBox": 2.0,
        "AXStaticText": 3.0,
    }.get(role_base, 4.0)
    frame = _frame_tuple_from_summary_element(element) or (0.0, 0.0, 1.0, 1.0)
    min_distance = min((_frame_distance(frame, container["frame"]) for container in containers), default=0.0)
    x, y, _, _ = frame
    visible_rank = 0.0 if element.get("collection_visible_in_viewport") else 1.0
    order_rank = float(element.get("collection_order")) if element.get("collection_order") is not None else 1_000_000.0
    return (visible_rank, role_priority, order_rank, min_distance + abs(y) + abs(x))


def _annotate_existing_ax_elements_with_collection_metadata(
    ax_elements: list[dict[str, Any]],
    element_index: dict[str, legacy.UiElement],
    containers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    container_ids_by_path = {
        ui_element.ax_path: element.get("id")
        for element in ax_elements
        if isinstance(element, dict)
        for ui_element in [element_index.get(str(element.get("id") or ""))]
        if isinstance(ui_element, legacy.UiElement) and ui_element.ax_path
    }
    annotated: list[dict[str, Any]] = []
    for element in ax_elements:
        if not isinstance(element, dict):
            continue
        updated = dict(element)
        ui_element = element_index.get(str(element.get("id") or ""))
        ax_path = ui_element.ax_path if isinstance(ui_element, legacy.UiElement) else None
        frame = _frame_tuple_from_summary_element(element)
        metadata = _collection_metadata_for_candidate(ax_path=ax_path, frame=frame, containers=containers)
        if metadata is not None:
            updated["collection_container_ax_path"] = metadata["container_ax_path"]
            updated["collection_container_role"] = metadata["container_role"]
            updated["collection_order"] = metadata["order"]
            updated["collection_visible_in_viewport"] = metadata["visible_in_viewport"]
            if metadata["container_ax_path"] in container_ids_by_path:
                updated["collection_container_element_id"] = container_ids_by_path[metadata["container_ax_path"]]
        annotated.append(updated)
    return annotated


def _augment_ax_elements_with_collection_candidates(
    ax_elements: list[dict[str, Any]],
    element_index: dict[str, legacy.UiElement],
    raw_ax_elements: list[dict[str, Any]],
    *,
    max_additional: int = OPEN_COLLECTION_AUGMENT_LIMIT,
) -> list[dict[str, Any]]:
    containers = _collection_container_records_from_raw_ax(raw_ax_elements)
    if not containers:
        return ax_elements
    ax_elements = _annotate_existing_ax_elements_with_collection_metadata(ax_elements, element_index, containers)
    container_ids_by_path = {
        ui_element.ax_path: element.get("id")
        for element in ax_elements
        if isinstance(element, dict)
        for ui_element in [element_index.get(str(element.get("id") or ""))]
        if isinstance(ui_element, legacy.UiElement) and ui_element.ax_path
    }
    existing_paths = {
        ui_element.ax_path
        for ui_element in element_index.values()
        if isinstance(ui_element, legacy.UiElement) and ui_element.ax_path
    }
    candidates: list[dict[str, Any]] = []
    for item in raw_ax_elements:
        if not isinstance(item, dict) or item.get("source") != "ax":
            continue
        ax_path = legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000)
        if not ax_path or ax_path in existing_paths:
            continue
        frame = _frame_tuple_from_raw_ax_item(item)
        role_base = _element_base_role_from_raw_ax(item)
        if not _is_open_collection_option_role(role_base):
            continue
        text = legacy.clean_text(item.get("text"), limit=240)
        if not text:
            continue
        metadata = _collection_metadata_for_candidate(ax_path=ax_path, frame=frame, containers=containers)
        if metadata is None:
            continue
        secondary_actions = _normalized_secondary_actions(item.get("secondaryActions") or item.get("secondary_actions"))
        candidates.append(
            {
                "role_base": role_base,
                "role": str(item.get("role") or ""),
                "text": text,
                "frame": frame,
                "ax_path": ax_path,
                "secondary_actions": secondary_actions,
                "collection_container_ax_path": metadata["container_ax_path"],
                "collection_container_role": metadata["container_role"],
                "collection_order": metadata["order"],
                "collection_visible_in_viewport": metadata["visible_in_viewport"],
                "collection_container_element_id": container_ids_by_path.get(metadata["container_ax_path"]),
            }
        )
    if not candidates:
        return ax_elements
    candidates.sort(
        key=lambda item: (
            0 if item.get("collection_visible_in_viewport") else 1,
            {
                "AXMenuItem": 0,
                "AXRow": 1,
                "AXCell": 1,
                "AXButton": 2,
                "AXRadioButton": 2,
                "AXCheckBox": 2,
                "AXStaticText": 3,
            }.get(item["role_base"], 4),
            item.get("collection_order") if item.get("collection_order") is not None else 1_000_000,
            min((_frame_distance(item["frame"], container["frame"]) for container in containers), default=0.0)
            if item.get("frame") is not None else 1_000_000,
        )
    )
    augmented = list(ax_elements)
    for candidate in candidates[:max_additional]:
        element_id = _next_ax_element_id(element_index)
        x, y, width, height = candidate["frame"]
        ui_element = legacy.UiElement(
            element_id=element_id,
            role=candidate["role"],
            text=candidate["text"],
            x=x,
            y=y,
            width=width,
            height=height,
            ax_path=candidate["ax_path"],
        )
        element_index[element_id] = ui_element
        summary_element = {
            "id": element_id,
            "source": "ax",
            "role": candidate["role"],
            "text": candidate["text"],
            "direct_ax": True,
            "frame": {"x": x, "y": y, "width": width, "height": height},
            "center": {"x": ui_element.center[0], "y": ui_element.center[1]},
            "collection_container_ax_path": candidate.get("collection_container_ax_path"),
            "collection_container_role": candidate.get("collection_container_role"),
            "collection_order": candidate.get("collection_order"),
            "collection_visible_in_viewport": candidate.get("collection_visible_in_viewport"),
            "collection_container_element_id": candidate.get("collection_container_element_id"),
        }
        if candidate["secondary_actions"]:
            summary_element["secondary_actions"] = candidate["secondary_actions"]
        augmented.append(summary_element)
    return augmented


def _is_collection_navigation_action(action: dict[str, Any], element_index: dict[str, legacy.UiElement]) -> bool:
    action_type = str(action.get("type") or "").lower()
    if action_type == "scroll":
        return True
    if action_type == "secondary_action":
        secondary_action = _normalize_secondary_action_name(action.get("action"))
        return secondary_action in {"ScrollUp", "ScrollDown", "ScrollLeft", "ScrollRight", "Increment", "Decrement"}
    if action_type == "click":
        element_id = action.get("element_id")
        if not element_id:
            return False
        element = element_index.get(str(element_id))
        if element is None:
            return False
        role_base = legacy.base_role(element.role)
        return role_base in OPEN_COLLECTION_CONTAINER_ROLES or role_base in OPEN_COLLECTION_TRIGGER_ROLES
    return False


def _visible_option_candidates_for_plan(
    elements: list[dict[str, Any]],
    element_index: dict[str, legacy.UiElement],
    plan: dict[str, Any],
    *,
    limit: int = VISIBLE_OPTION_SELECTOR_CANDIDATE_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions = plan.get("actions") or []
    first_action = actions[0] if actions else {}
    preferred_element_id = str(first_action.get("element_id") or "") or None
    containers = _collection_container_records_from_elements(
        elements,
        preferred_element_id=preferred_element_id,
        element_index=element_index,
    )
    preferred_collection_ax_path = None
    preferred_summary = next(
        (element for element in elements if isinstance(element, dict) and str(element.get("id") or "") == preferred_element_id),
        None,
    )
    if isinstance(preferred_summary, dict):
        preferred_collection_ax_path = preferred_summary.get("collection_container_ax_path")
    preferred_ui = element_index.get(preferred_element_id) if preferred_element_id else None
    if not preferred_collection_ax_path and isinstance(preferred_ui, legacy.UiElement):
        preferred_role_base = legacy.base_role(preferred_ui.role)
        if preferred_role_base in OPEN_COLLECTION_CONTAINER_ROLES and preferred_ui.ax_path:
            preferred_collection_ax_path = preferred_ui.ax_path
        elif (
            preferred_role_base in OPEN_COLLECTION_TRIGGER_ROLES
            and preferred_ui.x is not None
            and preferred_ui.y is not None
            and preferred_ui.width is not None
            and preferred_ui.height is not None
        ):
            trigger_frame = (preferred_ui.x, preferred_ui.y, preferred_ui.width, preferred_ui.height)
            container = _collection_membership_from_frame(trigger_frame, containers)
            if container is None and containers:
                container = min(containers, key=lambda item: _frame_distance(trigger_frame, item["frame"]))
            if isinstance(container, dict):
                preferred_collection_ax_path = container.get("ax_path")
    if not containers and preferred_element_id:
        preferred = element_index.get(preferred_element_id)
        if preferred is not None and preferred.x is not None and preferred.y is not None and preferred.width is not None and preferred.height is not None:
            containers = [
                {
                    "id": preferred_element_id,
                    "source": preferred.source,
                    "role": preferred.role,
                    "direct_ax": preferred.ax_path is not None,
                    "role_base": legacy.base_role(preferred.role),
                    "text": preferred.text,
                    "frame": (preferred.x, preferred.y, preferred.width, preferred.height),
                    "ax_path": preferred.ax_path,
                }
            ]
            preferred_collection_ax_path = preferred.ax_path
    if not containers:
        return [], []
    candidates: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_id = str(element.get("id") or "")
        if any(element_id == str(container.get("id") or "") for container in containers):
            continue
        role_base = _element_base_role_from_summary(element)
        if not _is_open_collection_option_role(role_base):
            continue
        if not legacy.clean_text(element.get("text"), limit=240):
            continue
        if preferred_collection_ax_path:
            if element.get("collection_container_ax_path") != preferred_collection_ax_path:
                continue
        else:
            frame = _frame_tuple_from_summary_element(element)
            if frame is None or not _is_candidate_near_any_collection(frame, containers):
                continue
        candidates.append(element)
    candidates.sort(key=lambda item: _collection_option_sort_key(item, containers))
    return containers, candidates[:limit]


def _preferred_option_selection_action(element: dict[str, Any]) -> dict[str, Any]:
    secondary_actions = [str(action) for action in (element.get("secondary_actions") or [])]
    for preferred in ("Select", "Press", "Confirm"):
        if preferred in secondary_actions:
            return {"type": "secondary_action", "element_id": str(element.get("id")), "action": preferred}
    return {"type": "click", "element_id": str(element.get("id"))}


def _compact_collection_records_for_prompt(collection_containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for container in collection_containers:
        frame = container.get("frame")
        frame_mapping = None
        if isinstance(frame, tuple) and len(frame) == 4:
            frame_mapping = {
                "x": int(round(float(frame[0]))),
                "y": int(round(float(frame[1]))),
                "width": int(round(float(frame[2]))),
                "height": int(round(float(frame[3]))),
            }
        compact = {
            "id": container.get("id"),
            "source": container.get("source", "ax"),
            "role": container.get("role") or container.get("role_base"),
            "text": container.get("text"),
            "direct_ax": container.get("direct_ax", True),
            "frame": frame_mapping,
        }
        compacted.append({key: value for key, value in compact.items() if value is not None})
    return compacted


def _planner_elements_by_id(elements: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {
        str(element.get("id") or ""): element
        for element in (elements or [])
        if isinstance(element, dict) and element.get("id")
    }


def _raw_collection_items(
    raw_ax_elements: list[dict[str, Any]],
    *,
    container_ax_path: str,
    container_frame: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in raw_ax_elements:
        if not isinstance(item, dict) or item.get("source") != "ax":
            continue
        ax_path = legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000)
        order = _collection_child_order(ax_path, container_ax_path)
        if order is None:
            continue
        frame = _frame_tuple_from_raw_ax_item(item)
        items.append(
            {
                "index": str(item.get("index")) if item.get("index") is not None else None,
                "role": str(item.get("role") or ""),
                "text": legacy.clean_text(item.get("text"), limit=240),
                "frame": frame,
                "ax_path": ax_path,
                "order": order,
                "secondary_actions": _normalized_secondary_actions(item.get("secondaryActions") or item.get("secondary_actions")),
                "visible_in_viewport": bool(
                    frame is not None
                    and container_frame is not None
                    and _frame_contains_with_margin(container_frame, frame, margin_x=8.0, margin_y=8.0)
                ),
            }
        )
    items.sort(key=lambda item: item["order"])
    return items


def _collection_scroll_instruction(
    *,
    target_order: int,
    visible_orders: list[int],
) -> tuple[str, float] | None:
    if not visible_orders:
        return None
    visible_orders = sorted(visible_orders)
    min_visible = visible_orders[0]
    max_visible = visible_orders[-1]
    if min_visible <= target_order <= max_visible:
        return None
    visible_span = max(1, max_visible - min_visible + 1)
    midpoint = (min_visible + max_visible) / 2.0
    delta_rows = target_order - midpoint
    direction = "down" if delta_rows > 0 else "up"
    pages = max(PRECISE_SCROLL_PAGES, min(4.0, abs(delta_rows) / visible_span))
    return direction, pages


def _collection_visible_midpoint(visible_orders: Sequence[int]) -> float | None:
    if not visible_orders:
        return None
    ordered = sorted(int(value) for value in visible_orders)
    return (ordered[0] + ordered[-1]) / 2.0


def _normalized_collection_value_text(text: str | None) -> str:
    cleaned = legacy.clean_text(text or "", limit=240) or ""
    return " ".join(cleaned.lower().split())


def _collection_value_tokens(text: str | None) -> list[str]:
    normalized = _normalized_collection_value_text(text)
    if not normalized:
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for token in normalized.split(" "):
        token = token.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _match_collection_value_to_item(
    value_text: str | None,
    collection_items: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_value = _normalized_collection_value_text(value_text)
    if not normalized_value:
        return None
    value_tokens = set(_collection_value_tokens(normalized_value))
    best_item: dict[str, Any] | None = None
    best_score: tuple[int, int, int, int, int] | None = None
    for item in collection_items:
        item_text = legacy.clean_text(item.get("text"), limit=240)
        normalized_item = _normalized_collection_value_text(item_text)
        if not normalized_item:
            continue
        item_tokens = _collection_value_tokens(normalized_item)
        exact_token_matches = [token for token in item_tokens if token in value_tokens]
        token_match_lengths = [len(token) for token in exact_token_matches]
        best_token_match = max(token_match_lengths, default=0)
        full_match = 1 if normalized_item == normalized_value else 0
        if full_match == 0 and best_token_match == 0:
            continue
        score = (
            full_match,
            best_token_match,
            len(exact_token_matches),
            len(normalized_item),
            1 if item.get("visible_in_viewport") else 0,
            -abs(int(item.get("order") or 0)),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_item = item
    return best_item


def _nearest_collection_value_source_from_raw_ax(
    raw_ax_elements: Sequence[dict[str, Any]],
    *,
    anchor_frame: tuple[float, float, float, float] | None,
    collection_items: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if anchor_frame is None:
        return None
    best_item: dict[str, Any] | None = None
    best_score: tuple[int, float, float, float] | None = None
    anchor_center_x, anchor_center_y = _frame_center(anchor_frame)
    for item in raw_ax_elements:
        if not isinstance(item, dict) or item.get("source") != "ax":
            continue
        role_base = _element_base_role_from_raw_ax(item)
        if role_base not in OPEN_COLLECTION_VALUE_SOURCE_ROLES:
            continue
        frame = _frame_tuple_from_raw_ax_item(item)
        text = legacy.clean_text(item.get("text"), limit=240)
        if frame is None or not text:
            continue
        item_center_x, item_center_y = _frame_center(frame)
        vertical_distance = abs(item_center_y - anchor_center_y)
        horizontal_distance = abs(item_center_x - anchor_center_x)
        semantic_match = (
            1
            if collection_items and _match_collection_value_to_item(text, collection_items) is not None
            else 0
        )
        score = (
            -semantic_match,
            vertical_distance,
            horizontal_distance,
            0.0 if role_base in OPEN_COLLECTION_TRIGGER_ROLES else 1.0,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_item = item
    return best_item


def _build_visible_option_selector_prompt(
    *,
    user_instruction: str,
    target_identifier: str,
    step_number: int,
    current_plan: dict[str, Any],
    recent_history: list[dict[str, Any]],
    progress_summary: dict[str, Any],
    collection_containers: list[dict[str, Any]],
    option_candidates: list[dict[str, Any]],
) -> str:
    payload = {
        "user_instruction": user_instruction,
        "target_identifier": target_identifier,
        "step_number": step_number,
        "current_plan": current_plan,
        "recent_history": recent_history,
        "progress_summary": progress_summary,
        "collection_containers": _compact_collection_records_for_prompt(collection_containers),
        "option_candidates": _compact_elements_for_prompt(option_candidates),
    }
    return (
        "You are a specialist dropdown/list/menu option selector for a desktop UI automation agent.\n"
        "The main planner is about to navigate an open collection, usually by scrolling.\n"
        "Your only job is to decide whether one of the provided collection option candidates already satisfies the user's goal.\n"
        "Candidates may include both currently visible items and offscreen items from the same open collection.\n"
        "If an exact or semantically correct candidate is present anywhere in option_candidates, select it now instead of continuing to scroll.\n"
        "An offscreen candidate is still valid to select; the executor can align the collection to it.\n"
        "Return null only when the intended value is not present among option_candidates or the choices are genuinely ambiguous.\n"
        "Choose only from the provided option_candidates element ids. Never invent ids. Never return coordinates.\n"
        "Return JSON only with this exact shape:\n"
        "{\"element_id\":\"e12\"|null,\"reason\":\"...\"}\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_stage_planner_prompt(
    *,
    user_instruction: str,
    explicit_target: str | None,
    candidate_apps: list[dict[str, Any]],
) -> str:
    payload = {
        "user_instruction": user_instruction,
        "explicit_target": explicit_target,
        "candidate_apps": candidate_apps,
        "current_date": datetime.now().strftime("%Y-%m-%d"),
    }
    return (
        "You are a semantic task decomposition planner for a desktop computer-use agent.\n"
        "Break the user's request into the smallest meaningful stages only when staging materially improves reliability.\n"
        "Do not use app-specific assumptions. Reason from task semantics only.\n"
        "Each stage controls exactly one target application.\n"
        "Choose stage targets from explicit_target or candidate_apps only.\n"
        "If the task contains structured form fields such as date, start time, end time, duration, recipient, or payload handoff, prefer focused stages for those fields instead of one large stage.\n"
        "If a later stage needs text or a link produced by an earlier stage, needs_multi_stage must be true. Set expects_share_text=true on the producing stage and use {{shared_payload}} in the downstream stage instruction.\n"
        "If the task includes both generating a shareable payload and sending or forwarding it elsewhere, you must produce at least two stages: produce/extract payload, then send payload.\n"
        "If the task includes structured temporal constraints plus a form submission goal, you should usually split the primary work into focused stages such as open/configure date/configure time/configure duration/submit, unless the UI already visibly satisfies some of those fields.\n"
        "If multi-stage decomposition is unnecessary, return needs_multi_stage=false.\n"
        "Return JSON only with this exact shape:\n"
        "{\"needs_multi_stage\":true|false,\"reason\":\"...\",\"stages\":[{\"name\":\"...\",\"target\":\"...\",\"instruction\":\"...\",\"max_steps\":8,\"expects_share_text\":false}]}\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_stage_plan_refiner_prompt(
    *,
    user_instruction: str,
    explicit_target: str | None,
    candidate_apps: list[dict[str, Any]],
    proposed_plan: dict[str, Any],
) -> str:
    payload = {
        "user_instruction": user_instruction,
        "explicit_target": explicit_target,
        "candidate_apps": candidate_apps,
        "proposed_plan": proposed_plan,
    }
    return (
        "You are a semantic stage-plan reviewer for a desktop automation agent.\n"
        "Review the proposed stage plan and rewrite it only if it is too coarse or violates the decomposition rules.\n"
        "Keep the plan generic. Do not use app-specific assumptions.\n"
        "A plan is too coarse when it mixes producing a payload and sending that payload in one stage, or when it keeps multiple structured field-setting goals inside one stage even though splitting them would clearly improve reliability.\n"
        "If the plan is already appropriately granular, return it unchanged.\n"
        "Return JSON only with the same shape as the planner output.\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _candidate_apps_for_stage_planner(client: MCPClient | Any, instruction: str, explicit_target: str | None) -> list[dict[str, Any]]:
    candidates = discover_apps_via_mcp(client)
    ranked: list[tuple[int, int, legacy.AppCandidate]] = []
    for candidate in candidates:
        score, alias_length, _ = legacy.app_match_score(instruction, candidate)
        if explicit_target and explicit_target in candidate.aliases:
            score += 2000
        ranked.append((score, alias_length, candidate))
    ranked.sort(key=lambda item: (item[0], item[1], item[2].path is not None), reverse=True)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, _, candidate in ranked[:20]:
        if candidate.identifier in seen:
            continue
        seen.add(candidate.identifier)
        result.append(
            {
                "display_name": candidate.display_name,
                "identifier": candidate.identifier,
                "bundle_id": candidate.bundle_id,
                "path": candidate.path,
                "aliases": list(candidate.aliases[:6]),
                "match_score": score,
            }
        )
    return result


def _normalize_stage_planner_payload(payload: dict[str, Any], explicit_target: str | None) -> list[WorkflowStage] | None:
    if not bool(payload.get("needs_multi_stage")):
        return None
    stages_raw = payload.get("stages")
    if not isinstance(stages_raw, list) or len(stages_raw) < 2:
        return None
    stages: list[WorkflowStage] = []
    for index, raw_stage in enumerate(stages_raw, start=1):
        if not isinstance(raw_stage, dict):
            continue
        instruction = legacy.clean_text(raw_stage.get("instruction"), limit=2000)
        if not instruction:
            continue
        target = legacy.clean_text(raw_stage.get("target"), limit=200) or (explicit_target if index == 1 else None)
        name = legacy.safe_path_component(legacy.clean_text(raw_stage.get("name"), limit=120) or f"stage-{index}")
        expects_share_text = bool(raw_stage.get("expects_share_text"))
        try:
            max_steps = int(raw_stage.get("max_steps", 12))
        except (TypeError, ValueError):
            max_steps = 12
        max_steps = min(20, max(4, max_steps))
        stages.append(
            WorkflowStage(
                name=name,
                target=target,
                instruction=instruction,
                expects_share_text=expects_share_text,
                metadata={"max_steps": max_steps},
            )
        )
    if len(stages) < 2:
        return None
    return stages


def invoke_stage_planner_with_trace(
    *,
    prompt: str,
    artifact_dir: Path | None,
    model_name: str | None,
    kind: str = "task_stage_planner",
) -> str:
    root = artifact_dir or legacy.session_artifact_dir(cwd=Path.cwd())
    call_root = root / "llm-calls"
    call_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    call_dir = call_root / f"000-{legacy.safe_path_component(kind)}-step-00-{stamp}"
    call_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "kind": kind,
        "step_number": 0,
        "model": model_name or DEFAULT_LLM_MODEL,
        "prompt_characters": len(prompt),
        "prompt_breakdown": _build_prompt_breakdown(prompt, model_name=model_name or DEFAULT_LLM_MODEL),
        "status": "pending",
        "call_dir": os.fspath(call_dir),
    }
    (call_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    legacy.write_json(call_dir / "request_content.json", {"prompt": prompt})
    legacy.write_json(call_dir / "metadata.json", metadata)
    try:
        response_text, llm_metadata = invoke_chat_model(prompt, model_name=model_name)
    except Exception as exc:
        metadata["status"] = "error"
        metadata["error"] = repr(exc)
        legacy.write_json(call_dir / "metadata.json", metadata)
        raise
    metadata["status"] = "completed"
    metadata["usage"] = _normalize_usage_metadata(llm_metadata)
    metadata["timing"] = {
        "started_at": llm_metadata.get("started_at"),
        "finished_at": llm_metadata.get("finished_at"),
        "elapsed_ms": llm_metadata.get("elapsed_ms"),
    }
    (call_dir / "response.txt").write_text(response_text, encoding="utf-8")
    legacy.write_json(call_dir / "metadata.json", metadata)
    return response_text


def plan_task_stages(args: argparse.Namespace, client: MCPClient | Any, *, artifact_dir: Path | None = None) -> list[WorkflowStage] | None:
    candidate_apps = _candidate_apps_for_stage_planner(client, args.instruction, args.target)
    matched_count = sum(1 for app in candidate_apps if int(app.get("match_score") or 0) > 0)
    if matched_count < 2:
        return None
    prompt = build_stage_planner_prompt(
        user_instruction=args.instruction,
        explicit_target=args.target,
        candidate_apps=candidate_apps,
    )
    try:
        raw = invoke_stage_planner_with_trace(
            prompt=prompt,
            artifact_dir=artifact_dir,
            model_name=args.model,
            kind="task_stage_planner",
        )
    except Exception as exc:
        print(f"warning: task stage planning failed; falling back to single-stage workflow: {exc}", file=sys.stderr)
        return None
    try:
        payload = legacy.parse_llm_json_object(raw)
    except Exception as exc:
        print(f"warning: task stage planner returned invalid JSON; falling back to single-stage workflow: {exc}", file=sys.stderr)
        return None
    normalized = _normalize_stage_planner_payload(payload, args.target)
    if normalized is None:
        return None
    refiner_prompt = build_stage_plan_refiner_prompt(
        user_instruction=args.instruction,
        explicit_target=args.target,
        candidate_apps=candidate_apps,
        proposed_plan=payload,
    )
    try:
        refined_raw = invoke_stage_planner_with_trace(
            prompt=refiner_prompt,
            artifact_dir=artifact_dir,
            model_name=args.model,
            kind="task_stage_refiner",
        )
        refined_payload = legacy.parse_llm_json_object(refined_raw)
        refined = _normalize_stage_planner_payload(refined_payload, args.target)
        return refined or normalized
    except Exception as exc:
        print(f"warning: task stage refiner failed; using initial stage plan: {exc}", file=sys.stderr)
        return normalized


def _stage_output_path(base_path: Path | None, stage_index: int, stage_name: str) -> Path | None:
    if base_path is None:
        return None
    suffix = "".join(base_path.suffixes)
    stem = base_path.name[: -len(suffix)] if suffix else base_path.name
    return base_path.with_name(f"{stem}.stage-{stage_index:02d}-{legacy.safe_path_component(stage_name)}{suffix}")


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
        if "secondary_actions" in element and element.get("secondary_actions"):
            compact["secondary_actions"] = element.get("secondary_actions")
        if "collection_visible_in_viewport" in element and element.get("collection_visible_in_viewport") is not None:
            compact["collection_visible_in_viewport"] = bool(element.get("collection_visible_in_viewport"))
        if "collection_order" in element and element.get("collection_order") is not None:
            compact["collection_order"] = element.get("collection_order")
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
        "secondary_action": action.get("action"),
        "text_length": action.get("text_length"),
        "point": result.get("point"),
        "input_method": result.get("input_method"),
        "skipped": result.get("skipped"),
        "fallback_from": result.get("fallback_from"),
        "fallback_reason": result.get("fallback_reason"),
        "fallback_skipped": result.get("fallback_skipped"),
        "supported_secondary_actions": result.get("supported_secondary_actions"),
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
        compact_item = {
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
        completion_verification = item.get("completion_verification")
        if isinstance(completion_verification, dict):
            compact_item["completion_verification"] = {
                "status": completion_verification.get("status"),
                "confidence": completion_verification.get("confidence"),
                "summary": completion_verification.get("summary"),
                "evidence": completion_verification.get("evidence"),
            }
        compacted.append(compact_item)
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
    target_counts: dict[str, int] = {}
    point_counts: dict[tuple[int, int], int] = {}
    verifier_summaries: list[str] = []

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
                point_key = _action_coordinate_key(action)
                if point_key is not None:
                    point_counts[point_key] = point_counts.get(point_key, 0) + 1
            if action_type and action_type not in {"wait", "finish"}:
                last_non_wait_action = action

        for element in step.get("action_elements") or []:
            if not isinstance(element, dict):
                continue
            text = legacy.clean_text(element.get("text"), limit=120)
            if text:
                clicked_targets.append(text)
                target_counts[text] = target_counts.get(text, 0) + 1

        for result in step.get("execution_results") or []:
            if not isinstance(result, dict):
                continue
            if (result.get("mode") == "direct_ax" and not result.get("ok")) or result.get("fallback_from") == "direct_ax":
                direct_ax_failures += 1
            action = result.get("action")
            if isinstance(action, dict):
                action_type = str(action.get("type") or "").lower()
                if action_type and action_type not in {"wait", "finish"}:
                    last_non_wait_action = action
        completion_verification = step.get("completion_verification")
        if isinstance(completion_verification, dict):
            summary = legacy.clean_text(completion_verification.get("summary"), limit=240)
            if summary:
                verifier_summaries.append(summary)

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
        "recent_verifier_summaries": _unique_preserve_order(verifier_summaries, limit=4),
    }
    repeated_points = [f"({x},{y})" for (x, y), count in point_counts.items() if count >= 2]
    if org_like_targets:
        summary["org_targets_attempted"] = _unique_preserve_order(org_like_targets, limit=5)
    if contact_like_targets:
        summary["contact_targets_attempted"] = _unique_preserve_order(contact_like_targets, limit=8)

    repeat_guard: list[str] = [
        "Treat completed later-stage milestones as durable. Do not restart from the beginning unless the current UI directly contradicts them.",
    ]
    repeated_targets = [text for text, count in sorted(target_counts.items(), key=lambda item: item[1], reverse=True) if count >= 2]
    if repeated_targets:
        repeat_guard.append(
            "If the same visible target has already been clicked multiple times without verifier-confirmed progress, change strategy instead of repeating that click."
        )
    if repeated_points:
        repeat_guard.append(
            "If the same raw coordinate has already been tried multiple times without progress, do not click that coordinate again. Choose a different anchor or action type."
        )
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
    if repeated_targets:
        summary["repeated_targets"] = _unique_preserve_order(repeated_targets, limit=6)
    if repeated_points:
        summary["repeated_points"] = _unique_preserve_order(repeated_points, limit=6)
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


def _inject_extended_action_guidance(prompt: str) -> str:
    action_schema = (
        "Extended MCP action schema:\n"
        "- {\"type\":\"secondary_action\",\"element_id\":\"e12\",\"action\":\"Press|Raise|ShowMenu|Confirm|Cancel|Increment|Decrement|Focus|Select|Deselect|ScrollUp|ScrollDown|ScrollLeft|ScrollRight\"}\n"
        "- {\"type\":\"drag\",\"from_element_id\":\"e12\",\"to_element_id\":\"e13\"}\n"
        "- {\"type\":\"drag\",\"from_x\":123,\"from_y\":456,\"to_x\":789,\"to_y\":456}\n\n"
        "Extended action rules:\n"
        "- Prefer the MCP-native actions above. Do not use mousemove; the current MCP controller exposes click, secondary_action, drag, scroll, type_text, and press_key.\n"
        "- If an element exposes secondary_actions and the needed action is listed there, prefer secondary_action over repeated coordinate clicks.\n"
        "- If recent_history shows a click failed because Press is unsupported and supported_secondary_actions are available, your next action should usually be a matching secondary_action on that same element.\n"
        "- Prefer interactive controls such as Button, MenuButton, Incrementor, CheckBox, RadioButton, or TextField over adjacent StaticText labels. Do not click a StaticText label when a nearby real control exposes the actual interaction.\n"
        "- Use secondary_action=Increment/Decrement for picker steppers or incrementors instead of clicking them repeatedly.\n"
        "- Use secondary_action=ShowMenu for menu buttons or pop-up buttons when a direct Press is unreliable.\n"
        "- When a dropdown, pop-up list, or menu is open and the desired option is already visible in the current AX/OCR candidates, directly choose that visible option instead of scrolling.\n"
        "- Only scroll an open list or menu after confirming the needed option is not currently visible among the collection candidates.\n"
        "- Use drag only when moving a slider, scrubbing a picker, selecting a range, or drag-and-drop is visibly required.\n\n"
    )
    marker = "Element notes:\n"
    if marker in prompt:
        return prompt.replace(marker, action_schema + marker, 1)
    return prompt + "\n\n" + action_schema


def _action_coordinate_key(action: dict[str, Any]) -> tuple[int, int] | None:
    try:
        if action.get("x") is None or action.get("y") is None:
            return None
        return int(round(float(action["x"]))), int(round(float(action["y"])))
    except (TypeError, ValueError):
        return None


def _stagnation_constraints_from_steps(step_records: list[dict[str, Any]]) -> dict[str, Any]:
    disallowed_points: list[tuple[int, int]] = []
    disallowed_element_ids: list[str] = []
    point_counts: dict[tuple[int, int], int] = {}
    element_counts: dict[str, int] = {}
    for step in step_records[-4:]:
        verifier = step.get("completion_verification") or {}
        if str(verifier.get("status") or "") == "satisfied":
            continue
        actions = (step.get("plan") or {}).get("actions") or []
        for action in actions:
            if not isinstance(action, dict):
                continue
            point_key = _action_coordinate_key(action)
            if point_key is not None:
                point_counts[point_key] = point_counts.get(point_key, 0) + 1
            element_id = action.get("element_id")
            if element_id:
                element_counts[str(element_id)] = element_counts.get(str(element_id), 0) + 1
    disallowed_points = [point for point, count in point_counts.items() if count >= 2]
    disallowed_element_ids = [element_id for element_id, count in element_counts.items() if count >= 2]
    return {
        "disallowed_points": [{"x": point[0], "y": point[1]} for point in disallowed_points],
        "disallowed_element_ids": disallowed_element_ids,
    }


def _inject_stagnation_constraints(prompt: str, constraints: dict[str, Any]) -> str:
    if not constraints.get("disallowed_points") and not constraints.get("disallowed_element_ids"):
        return prompt
    block = (
        "Stagnation recovery constraints JSON:\n"
        f"{json.dumps(constraints, ensure_ascii=False)}\n"
        "Do not return any coordinate or element_id listed above unless the current UI now clearly shows new evidence that the previously failed target has changed state.\n"
        "If those actions failed repeatedly, choose a materially different control, action type, or navigation path.\n\n"
    )
    marker = "Current state JSON:\n"
    if marker in prompt:
        return prompt.replace(marker, block + marker, 1)
    return prompt + "\n\n" + block


def _plan_conflicts_with_stagnation_constraints(plan: dict[str, Any], constraints: dict[str, Any]) -> bool:
    disallowed_points = {
        (int(item["x"]), int(item["y"]))
        for item in constraints.get("disallowed_points") or []
        if isinstance(item, dict) and item.get("x") is not None and item.get("y") is not None
    }
    disallowed_element_ids = {str(item) for item in constraints.get("disallowed_element_ids") or []}
    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        point_key = _action_coordinate_key(action)
        if point_key is not None and point_key in disallowed_points:
            return True
        element_id = action.get("element_id")
        if element_id is not None and str(element_id) in disallowed_element_ids:
            return True
    return False


def _element_identity_key(element: dict[str, Any]) -> tuple[Any, ...]:
    frame = _compact_frame(element.get("frame"))
    frame_key = tuple(frame.values()) if frame is not None else None
    return (
        element.get("source"),
        element.get("role"),
        legacy.clean_text(element.get("text"), limit=200),
        frame_key,
    )


def _dedupe_elements_for_prompt(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        key = _element_identity_key(element)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(element)
    return deduped


def validate_plan(plan: dict[str, Any], element_index: dict[str, legacy.UiElement], *, max_actions_per_step: int) -> list[dict[str, Any]]:
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise ValueError("plan.actions must be a list")
    if len(actions) > max_actions_per_step:
        plan["dropped_actions"] = actions[max_actions_per_step:]
        plan["actions"] = actions[:max_actions_per_step]
        actions = plan["actions"]
        print(
            f"warning: planner returned more than {max_actions_per_step} action(s); only the first action(s) will execute before re-observation",
            file=sys.stderr,
        )

    normalized: list[dict[str, Any]] = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            raise ValueError(f"action must be an object: {raw_action!r}")
        action = dict(raw_action)
        action_type = str(action.get("type", "")).lower()
        if action_type not in EXTENDED_ACTION_TYPES:
            raise ValueError(f"unsupported action type: {action_type!r}")
        action["type"] = action_type
        if action_type == "secondary_action":
            element_id = str(action.get("element_id") or "")
            secondary_action = _normalize_secondary_action_name(action.get("action"))
            if not element_id or element_id not in element_index:
                raise ValueError(f"secondary_action needs a known element_id: {raw_action!r}")
            if secondary_action not in MCP_SECONDARY_ACTIONS:
                raise ValueError(f"unsupported secondary action: {secondary_action!r}")
            action["action"] = secondary_action
        elif action_type == "drag":
            from_element_id = action.get("from_element_id")
            to_element_id = action.get("to_element_id")
            if from_element_id and str(from_element_id) not in element_index:
                raise ValueError(f"unknown drag from_element_id: {from_element_id!r}")
            if to_element_id and str(to_element_id) not in element_index:
                raise ValueError(f"unknown drag to_element_id: {to_element_id!r}")
            has_element_refs = bool(from_element_id and to_element_id)
            has_coordinates = all(key in action for key in ("from_x", "from_y", "to_x", "to_y"))
            if not has_element_refs and not has_coordinates:
                raise ValueError(f"drag action needs from/to element ids or coordinates: {raw_action!r}")
        elif "element_id" in action and action["element_id"] not in element_index:
            raise ValueError(f"unknown element_id: {action['element_id']!r}")
        normalized.append(action)
    return normalized


def _drag_point(action: dict[str, Any], element_index: dict[str, legacy.UiElement], prefix: str) -> tuple[float, float]:
    element_key = action.get(f"{prefix}_element_id")
    if element_key:
        return element_index[str(element_key)].center
    return float(action[f"{prefix}_x"]), float(action[f"{prefix}_y"])


def _should_use_clipboard_paste(text: str) -> bool:
    return bool(text and ("\n" in text or "http://" in text or "https://" in text or len(text) >= 80))


def _write_clipboard(text: str) -> None:
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "pbcopy failed")


def _parse_supported_secondary_actions(text: str) -> list[str]:
    match = re.search(r"Supported actions:\s*(.+)$", text, re.IGNORECASE)
    if match is None:
        return []
    values = re.split(r"\s*,\s*", match.group(1).strip())
    normalized = [_normalize_secondary_action_name(value) for value in values]
    return [value for value in normalized if value in MCP_SECONDARY_ACTIONS]


def _secondary_action_scroll_direction(action: str) -> str | None:
    mapping = {
        "ScrollUp": "up",
        "ScrollDown": "down",
        "ScrollLeft": "left",
        "ScrollRight": "right",
    }
    return mapping.get(action)


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


def _share_text_candidate(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    markers = ("http://", "https://", "会议号", "会议链接", "入会密码", "加入会议", "复制邀请", "邀请信息")
    return any(marker in normalized for marker in markers)


def extract_share_text_heuristic(elements: list[dict[str, Any]]) -> str | None:
    candidates: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = str(element.get("text") or "").strip()
        if not _share_text_candidate(text):
            continue
        candidates.append(text)
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def build_share_text_extractor_prompt(
    *,
    stage_instruction: str,
    observation: dict[str, Any],
    elements: list[dict[str, Any]],
) -> str:
    payload = {
        "stage_instruction": stage_instruction,
        "observation": observation,
        "elements": elements,
    }
    return (
        "You extract a shareable downstream payload from the current UI.\n"
        "Return JSON only. Do not use markdown.\n"
        "If the current UI visibly contains a meeting invitation, link, meeting number, or invite text that should be forwarded, return it exactly.\n"
        "Preserve URLs, line breaks, and meeting numbers. Do not invent missing text.\n"
        "Return exactly this JSON shape:\n"
        "{\"status\":\"found|missing\",\"share_text\":\"...\",\"summary\":\"...\"}\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


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
        initial_app_state = self.fetch_app_state(target_identifier, activation_policy="foreground")
        traversal = self.traversal_from_app_state(initial_app_state)
        pid = int(initial_app_state["pid"])
        app_profile = legacy.resolve_app_profile(target_identifier, target_resolution, traversal)
        requested_visual_planning = _requested_visual_planning_for_profile(self.args.visual_planning, app_profile)
        if requested_visual_planning != self.args.visual_planning:
            print(
                "warning: requested visual planning was overridden to auto because this fixed-strategy AX-poor profile needs screenshot assistance",
                file=sys.stderr,
            )
        workflow_mode, visual_planning = legacy.apply_capability_decision(
            requested_mode=self.args.mode,
            requested_visual_planning=requested_visual_planning,
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
            "requested_visual_planning": requested_visual_planning,
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
                "requested_visual_planning": requested_visual_planning,
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
            requested_visual_planning=state.get("requested_visual_planning", self.args.visual_planning),
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
        actions = validate_plan(plan, state["element_index"], max_actions_per_step=self.args.max_actions_per_step)
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
            planner_elements=state["elements"],
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
                requested_visual_planning=state.get("requested_visual_planning", self.args.visual_planning),
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
        stagnation_constraints = _stagnation_constraints_from_steps(state.get("run_log", {}).get("steps", []))
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
        prompt = _inject_extended_action_guidance(prompt)
        prompt = _inject_stagnation_constraints(prompt, stagnation_constraints)
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
                    "stagnation_constraints": stagnation_constraints,
                },
                run_log=state.get("run_log"),
            )
            plan = legacy.parse_llm_plan(raw)
            if _plan_conflicts_with_stagnation_constraints(plan, stagnation_constraints):
                repair_prompt = (
                    prompt
                    + "\n\nThe previous proposal reused a disallowed coordinate or element that already failed repeatedly."
                    + " Return one materially different next action."
                )
                repair_raw, _ = self.invoke_llm_with_trace(
                    prompt=repair_prompt,
                    artifact_dir=state["artifact_dir"],
                    kind="planner_repair",
                    step_number=state["step_number"],
                    model_name=self.args.model,
                    image_base64=state["planner_images"] or None,
                    extra={
                        "workflow_mode": state["workflow_mode"],
                        "app_profile": state["app_profile"].name,
                        "stagnation_constraints": stagnation_constraints,
                    },
                    run_log=state.get("run_log"),
                )
                plan = legacy.parse_llm_plan(repair_raw)
            plan = self.maybe_rewrite_plan_for_visible_option_selection(
                state,
                plan,
                progress_summary=progress_summary,
            )
            return plan
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

    def maybe_rewrite_plan_for_visible_option_selection(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        *,
        progress_summary: dict[str, Any],
    ) -> dict[str, Any]:
        actions = plan.get("actions") or []
        if len(actions) != 1:
            return plan
        first_action = actions[0]
        if not isinstance(first_action, dict) or not _is_collection_navigation_action(first_action, state["element_index"]):
            return plan
        collection_containers, option_candidates = _visible_option_candidates_for_plan(
            state["elements"],
            state["element_index"],
            plan,
        )
        if not option_candidates:
            return plan
        prompt = _build_visible_option_selector_prompt(
            user_instruction=self.args.instruction,
            target_identifier=state["target_identifier"],
            step_number=state["step_number"],
            current_plan=plan,
            recent_history=_compact_history_for_prompt(state["history"]),
            progress_summary=progress_summary,
            collection_containers=collection_containers,
            option_candidates=option_candidates,
        )
        try:
            raw, _ = self.invoke_llm_with_trace(
                prompt=prompt,
                artifact_dir=state["artifact_dir"],
                kind="visible_option_selector",
                step_number=state["step_number"],
                model_name=self.args.model,
                extra={
                    "current_plan": plan,
                    "collection_container_count": len(collection_containers),
                    "option_candidate_count": len(option_candidates),
                },
                run_log=state.get("run_log"),
            )
            payload = legacy.parse_llm_json_object(raw)
        except Exception as exc:
            print(f"warning: visible option selector failed; keeping original plan: {exc}", file=sys.stderr)
            return plan
        selected_element_id = str(payload.get("element_id") or "").strip()
        if not selected_element_id:
            return plan
        candidate = next(
            (item for item in option_candidates if str(item.get("id") or "") == selected_element_id),
            None,
        )
        if candidate is None:
            return plan
        rewritten_plan = copy.deepcopy(plan)
        rewritten_plan["actions"] = [_preferred_option_selection_action(candidate)]
        reason = legacy.clean_text(payload.get("reason"), limit=400)
        if reason:
            rewritten_plan["summary"] = reason
            rewritten_plan["visible_option_selector_reason"] = reason
        rewritten_plan["visible_option_selector_original_action"] = first_action
        rewritten_plan["visible_option_selector_chosen_element"] = selected_element_id
        return rewritten_plan

    def extract_share_text(self, state: dict[str, Any]) -> str | None:
        heuristic = extract_share_text_heuristic(state.get("elements") or [])
        if heuristic:
            return heuristic
        elements = _elements_for_completion_verifier(state.get("elements") or [], limit=120)
        observation = _compact_observation_for_prompt(state.get("observation") or {}, elements)
        prompt = build_share_text_extractor_prompt(
            stage_instruction=self.args.instruction,
            observation=observation,
            elements=elements,
        )
        raw, _ = self.invoke_llm_with_trace(
            prompt=prompt,
            artifact_dir=state["artifact_dir"],
            kind="share_text_extractor",
            step_number=state.get("step_number", 0),
            model_name=self.args.model,
            extra={
                "app_profile": state["app_profile"].name,
                "element_count": len(elements),
            },
            run_log=state.get("run_log"),
        )
        payload = legacy.parse_llm_json_object(raw)
        if str(payload.get("status") or "").lower() != "found":
            return None
        share_text = str(payload.get("share_text") or "").strip()
        return share_text or None

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

    def fetch_app_state(self, app: str, *, activation_policy: str = "preserve_session") -> dict[str, Any]:
        result = self.mcp.call_tool(
            "get_app_state",
            {
                "app": app,
                "observation_mode": "ax_ocr",
                "summary_mode": "metadata",
                "activation_policy": activation_policy,
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
        raw_ax_elements = [
            item
            for item in (app_state.get("elements") or [])
            if isinstance(item, dict) and item.get("source") == "ax"
        ]
        ax_elements = _augment_ax_elements_with_collection_candidates(
            ax_elements,
            element_index,
            raw_ax_elements,
        )
        by_path_secondary_actions: dict[str, list[str]] = {}
        for item in raw_ax_elements:
            if not isinstance(item, dict) or item.get("source") != "ax":
                continue
            ax_path = legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000)
            if not ax_path:
                continue
            secondary_actions = item.get("secondaryActions") or item.get("secondary_actions") or []
            if isinstance(secondary_actions, list):
                by_path_secondary_actions[ax_path] = [
                    normalized
                    for action in secondary_actions
                    if (normalized := _normalize_secondary_action_name(action))
                ]
        for element in ax_elements:
            ui_element = element_index.get(str(element.get("id") or ""))
            if ui_element is not None and ui_element.ax_path is not None:
                secondary_actions = by_path_secondary_actions.get(ui_element.ax_path)
                if secondary_actions:
                    element["secondary_actions"] = secondary_actions
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
        combined_elements = _dedupe_elements_for_prompt(combined_elements)
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

    def _scroll_arguments_for_collection_container(
        self,
        *,
        target_identifier: str,
        current_container_mcp_index: str | None,
        container_element_id: str | None,
        container_frame: tuple[float, float, float, float] | None,
        planner_ax_index: dict[str, str],
        direction: str,
        pages: float,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "app": target_identifier,
            "direction": direction,
            "pages": pages,
        }
        if current_container_mcp_index:
            arguments["element_index"] = current_container_mcp_index
            return arguments
        if container_element_id and container_element_id in planner_ax_index:
            arguments["element_index"] = planner_ax_index[container_element_id]
            return arguments
        if container_frame is not None:
            cx, cy = _frame_center(container_frame)
            arguments["x"] = cx
            arguments["y"] = cy
        return arguments

    def _drag_arguments_for_collection_container(
        self,
        *,
        target_identifier: str,
        container_frame: tuple[float, float, float, float],
        delta_y: float,
    ) -> dict[str, Any]:
        x, y, width, height = container_frame
        cx = float(x) + (float(width) / 2.0)
        margin = min(24.0, max(12.0, float(height) * 0.18))
        min_y = float(y) + margin
        max_y = float(y) + float(height) - margin
        if max_y <= min_y:
            min_y = float(y)
            max_y = float(y) + float(height)
        cy = (min_y + max_y) / 2.0
        max_shift = max(18.0, min(max_y - min_y - 2.0, COLLECTION_DRAG_MAX_POINTS, float(height) * COLLECTION_DRAG_MAX_RATIO))
        shift = min(max(abs(float(delta_y)), COLLECTION_DRAG_MIN_POINTS), max_shift)
        if float(delta_y) >= 0.0:
            from_y = max(min_y, cy - (shift / 2.0))
            to_y = min(max_y, cy + (shift / 2.0))
        else:
            from_y = min(max_y, cy + (shift / 2.0))
            to_y = max(min_y, cy - (shift / 2.0))
        return {
            "app": target_identifier,
            "from_x": cx,
            "from_y": from_y,
            "to_x": cx,
            "to_y": to_y,
        }

    def _drag_arguments_for_collection_item(
        self,
        *,
        target_identifier: str,
        container_frame: tuple[float, float, float, float],
        item_frame: tuple[float, float, float, float],
        delta_y: float,
    ) -> dict[str, Any]:
        x, y, width, height = container_frame
        item_center_x, item_center_y = _frame_center(item_frame)
        cx = min(max(item_center_x, float(x) + 8.0), float(x) + float(width) - 8.0)
        margin = min(24.0, max(12.0, float(height) * 0.12))
        min_y = float(y) + margin
        max_y = float(y) + float(height) - margin
        if max_y <= min_y:
            min_y = float(y)
            max_y = float(y) + float(height)
        from_y = min(max(item_center_y, min_y), max_y)
        max_shift = max(18.0, min(max_y - min_y - 2.0, COLLECTION_DRAG_MAX_POINTS, float(height) * COLLECTION_DRAG_MAX_RATIO))
        requested_shift = max(-max_shift, min(max_shift, float(delta_y)))
        overshoot = max(COLLECTION_DRAG_MIN_POINTS, float(height) * COLLECTION_DRAG_OVERSHOOT_RATIO)
        min_drag_y = float(y) - overshoot
        max_drag_y = float(y) + float(height) + overshoot
        to_y = min(max(from_y + requested_shift, min_drag_y), max_drag_y)
        actual_shift = to_y - from_y
        if 0.0 < abs(actual_shift) < COLLECTION_DRAG_MIN_POINTS:
            if requested_shift >= 0.0:
                to_y = min(max_drag_y, from_y + COLLECTION_DRAG_MIN_POINTS)
            else:
                to_y = max(min_drag_y, from_y - COLLECTION_DRAG_MIN_POINTS)
        return {
            "app": target_identifier,
            "from_x": cx,
            "from_y": from_y,
            "to_x": cx,
            "to_y": to_y,
        }

    def _maybe_execute_collection_option_selection(
        self,
        *,
        action: dict[str, Any],
        element: legacy.UiElement | None,
        planner_elements_by_id: dict[str, dict[str, Any]],
        planner_ax_index: dict[str, str],
        target_identifier: str,
        target_pid: int,
    ) -> dict[str, Any] | None:
        if element is None:
            return None
        summary = planner_elements_by_id.get(element.element_id)
        if not isinstance(summary, dict):
            return None
        container_ax_path = summary.get("collection_container_ax_path")
        target_order = summary.get("collection_order")
        if not isinstance(container_ax_path, str) or target_order is None:
            return None
        if summary.get("collection_visible_in_viewport"):
            return None
        container_element_id = summary.get("collection_container_element_id")
        container_summary = (
            planner_elements_by_id.get(str(container_element_id))
            if container_element_id is not None
            else None
        )
        desired_container_frame = (
            _frame_tuple_from_summary_element(container_summary)
            if isinstance(container_summary, dict)
            else None
        )
        target_ax_path = element.ax_path
        target_text = legacy.clean_text(summary.get("text"), limit=240) or element.text
        desired_secondary_action = _normalize_secondary_action_name(action.get("action")) if action.get("type") == "secondary_action" else None

        scroll_attempts = 0
        drag_attempts = 0
        reopen_attempts = 0
        last_scroll_args: dict[str, Any] | None = None
        last_drag_args: dict[str, Any] | None = None
        last_visible_signature: tuple[int, ...] | None = None
        last_visible_midpoint: float | None = None
        last_drag_delta_y: float | None = None
        last_alignment_method: str | None = None
        selected_order_per_point: float | None = None
        last_selected_order: int | None = None
        last_selected_distance_to_target: int | None = None
        drag_order_per_point: float | None = None
        drag_target_y_per_point: float | None = None
        last_target_center_y: float | None = None
        last_target_distance_to_center: float | None = None
        cached_collection_items: list[dict[str, Any]] = []
        cached_container_frame: tuple[float, float, float, float] | None = None
        cached_value_source_frame: tuple[float, float, float, float] | None = None
        prefer_drag_alignment = False
        probe_sign = 1.0
        for _ in range(COLLECTION_ALIGNMENT_MAX_ATTEMPTS):
            app_state = self.fetch_app_state(target_identifier)
            raw_ax_elements = [
                item
                for item in (app_state.get("elements") or [])
                if isinstance(item, dict) and item.get("source") == "ax"
            ]
            raw_collection_containers = _collection_container_records_from_raw_ax(raw_ax_elements)
            resolved_container = next(
                (
                    container
                    for container in raw_collection_containers
                    if legacy.clean_text(container.get("ax_path"), limit=1000) == container_ax_path
                ),
                None,
            )
            if resolved_container is None and desired_container_frame is not None and raw_collection_containers:
                resolved_container = min(
                    raw_collection_containers,
                    key=lambda container: _frame_distance(desired_container_frame, container["frame"]),
                )
            resolved_container_ax_path = (
                legacy.clean_text(resolved_container.get("ax_path"), limit=1000)
                if isinstance(resolved_container, dict)
                else None
            )
            container_item = next(
                (
                    item
                    for item in raw_ax_elements
                    if resolved_container_ax_path
                    and legacy.clean_text(item.get("axPath") or item.get("ax_path"), limit=1000) == resolved_container_ax_path
                ),
                None,
            )
            container_frame = (
                tuple(resolved_container["frame"])
                if isinstance(resolved_container, dict) and resolved_container.get("frame") is not None
                else _frame_tuple_from_raw_ax_item(container_item) if isinstance(container_item, dict) else None
            )
            current_container_mcp_index = str(container_item.get("index")) if isinstance(container_item, dict) and container_item.get("index") is not None else None
            if container_frame is not None:
                cached_container_frame = container_frame
            collection_items = _raw_collection_items(
                raw_ax_elements,
                container_ax_path=resolved_container_ax_path or container_ax_path,
                container_frame=container_frame,
            )
            if collection_items:
                cached_collection_items = list(collection_items)
            selection_items = collection_items or cached_collection_items
            anchor_frame = container_frame or cached_container_frame or cached_value_source_frame
            value_source_item = _nearest_collection_value_source_from_raw_ax(
                raw_ax_elements,
                anchor_frame=anchor_frame,
                collection_items=selection_items,
            )
            if isinstance(value_source_item, dict):
                value_source_frame = _frame_tuple_from_raw_ax_item(value_source_item)
                if value_source_frame is not None:
                    cached_value_source_frame = value_source_frame
            current_value_text = legacy.clean_text(value_source_item.get("text"), limit=240) if isinstance(value_source_item, dict) else None
            current_selected_item = _match_collection_value_to_item(current_value_text, selection_items)
            current_selected_order = (
                int(current_selected_item["order"])
                if isinstance(current_selected_item, dict) and current_selected_item.get("order") is not None
                else None
            )
            current_selected_distance_to_target = (
                abs(int(target_order) - current_selected_order)
                if current_selected_order is not None
                else None
            )
            current_selected_visible_item = _match_collection_value_to_item(current_value_text, collection_items)
            target_item = next(
                (
                    item
                    for item in selection_items
                    if (target_ax_path and item.get("ax_path") == target_ax_path)
                    or (item.get("order") == target_order and legacy.clean_text(item.get("text"), limit=240) == target_text)
                ),
                None,
            )
            if target_item is None:
                if not collection_items and isinstance(value_source_item, dict):
                    value_source_index = value_source_item.get("index")
                    if value_source_index is not None:
                        reopen_attempts += 1
                        reopen_result = self.mcp.call_tool(
                            "perform_secondary_action",
                            {"app": target_identifier, "element_index": str(value_source_index), "action": "ShowMenu"},
                        )
                        reopen_text = _content_text(reopen_result)
                        if _mcp_call_succeeded(reopen_result, reopen_text):
                            time.sleep(0.2)
                            continue
                break
            if current_selected_order == int(target_order):
                return {
                    "action": action,
                    "ok": True,
                    "activated_pid": target_pid,
                    "activation": "mcp_session",
                    "mode": "collection_option_value_selected",
                    "collection_target_text": target_text,
                    "collection_target_order": target_order,
                    "collection_current_value_text": current_value_text,
                    "collection_scroll_attempts": scroll_attempts,
                    "collection_drag_attempts": drag_attempts,
                    "collection_reopen_attempts": reopen_attempts,
                }
            current_target_center_y = None
            if collection_items and target_item.get("frame") is not None:
                _, current_target_center_y = _frame_center(target_item["frame"])
            current_target_distance_to_center = None
            target_far_offscreen = False
            if current_target_center_y is not None and container_frame is not None:
                _, container_center_y = _frame_center(container_frame)
                current_target_distance_to_center = abs(current_target_center_y - container_center_y)
                target_far_offscreen = (
                    not bool(target_item.get("visible_in_viewport"))
                    and current_target_distance_to_center > (float(container_frame[3]) * 0.75)
                )
            if collection_items and target_item.get("visible_in_viewport") and target_item.get("frame") is not None:
                mcp_index = target_item.get("index")
                if desired_secondary_action and desired_secondary_action in {"Select", "Press", "Confirm"} and mcp_index:
                    result = self.mcp.call_tool(
                        "perform_secondary_action",
                        {"app": target_identifier, "element_index": mcp_index, "action": desired_secondary_action},
                    )
                    text = _content_text(result)
                    if _mcp_call_succeeded(result, text):
                        return {
                            "action": action,
                            "ok": True,
                            "activated_pid": target_pid,
                            "activation": "mcp_session",
                            "mode": "collection_option_secondary_action",
                            "ax_path": target_item.get("ax_path"),
                            "collection_target_text": target_text,
                            "collection_target_order": target_order,
                            "collection_current_value_text": current_value_text,
                            "collection_scroll_attempts": scroll_attempts,
                            "collection_drag_attempts": drag_attempts,
                            "collection_reopen_attempts": reopen_attempts,
                            "tool_output": text[-1000:],
                        }
                tx, ty = _frame_center(target_item["frame"])
                result = self.mcp.call_tool("click", {"app": target_identifier, "x": tx, "y": ty})
                text = _content_text(result)
                return {
                    "action": action,
                    "ok": _mcp_call_succeeded(result, text),
                    "activated_pid": target_pid,
                    "activation": "mcp_session",
                    "mode": "collection_option_coordinate",
                    "ax_path": target_item.get("ax_path"),
                    "point": {"x": tx, "y": ty},
                    "collection_target_text": target_text,
                    "collection_target_order": target_order,
                    "collection_current_value_text": current_value_text,
                    "collection_scroll_attempts": scroll_attempts,
                    "collection_drag_attempts": drag_attempts,
                    "collection_reopen_attempts": reopen_attempts,
                    "tool_output": text[-1000:],
                }
            if not collection_items:
                if isinstance(value_source_item, dict):
                    value_source_index = value_source_item.get("index")
                    if value_source_index is not None:
                        reopen_attempts += 1
                        reopen_result = self.mcp.call_tool(
                            "perform_secondary_action",
                            {"app": target_identifier, "element_index": str(value_source_index), "action": "ShowMenu"},
                        )
                        reopen_text = _content_text(reopen_result)
                        if _mcp_call_succeeded(reopen_result, reopen_text):
                            time.sleep(0.2)
                            continue
                break
            visible_orders = [int(item["order"]) for item in collection_items if item.get("visible_in_viewport") and item.get("order") is not None]
            visible_signature = tuple(sorted(visible_orders))
            visible_midpoint = _collection_visible_midpoint(visible_orders)
            if current_selected_visible_item is not None and current_selected_visible_item.get("frame") is not None and current_selected_order is not None:
                prefer_drag_alignment = True
            if last_alignment_method == "scroll" and last_visible_signature is not None and visible_signature == last_visible_signature:
                prefer_drag_alignment = True
            if (
                last_alignment_method == "drag"
                and last_visible_midpoint is not None
                and visible_midpoint is not None
                and last_drag_delta_y not in {None, 0.0}
            ):
                observed_order_delta = visible_midpoint - last_visible_midpoint
                if abs(observed_order_delta) >= 0.5:
                    drag_order_per_point = observed_order_delta / float(last_drag_delta_y)
                else:
                    probe_sign *= -1.0
            if (
                last_alignment_method == "drag"
                and last_selected_order is not None
                and current_selected_order is not None
                and last_drag_delta_y not in {None, 0.0}
            ):
                observed_selected_delta = current_selected_order - last_selected_order
                if abs(observed_selected_delta) >= 1:
                    selected_order_per_point = observed_selected_delta / float(last_drag_delta_y)
                else:
                    probe_sign *= -1.0
                    selected_order_per_point = None
            if (
                last_alignment_method == "drag"
                and last_target_center_y is not None
                and current_target_center_y is not None
                and last_drag_delta_y not in {None, 0.0}
            ):
                observed_target_y_delta = current_target_center_y - last_target_center_y
                if abs(observed_target_y_delta) >= 1.0:
                    drag_target_y_per_point = observed_target_y_delta / float(last_drag_delta_y)
            if (
                last_alignment_method == "drag"
                and last_target_distance_to_center is not None
                and current_target_distance_to_center is not None
                and current_target_distance_to_center > last_target_distance_to_center + 8.0
            ):
                probe_sign *= -1.0
                drag_order_per_point = None
                drag_target_y_per_point = None
            if (
                last_alignment_method == "drag"
                and last_selected_distance_to_target is not None
                and current_selected_distance_to_target is not None
                and current_selected_distance_to_target > last_selected_distance_to_target
            ):
                probe_sign *= -1.0
                selected_order_per_point = None
            instruction = _collection_scroll_instruction(target_order=int(target_order), visible_orders=visible_orders)
            if instruction is None:
                break
            if not prefer_drag_alignment:
                direction, pages = instruction
                last_scroll_args = self._scroll_arguments_for_collection_container(
                    target_identifier=target_identifier,
                    current_container_mcp_index=current_container_mcp_index,
                    container_element_id=str(container_element_id) if container_element_id else None,
                    container_frame=container_frame,
                    planner_ax_index=planner_ax_index,
                    direction=direction,
                    pages=pages,
                )
                scroll_result = self.mcp.call_tool("scroll", last_scroll_args)
                scroll_text = _content_text(scroll_result)
                scroll_attempts += 1
                last_alignment_method = "scroll"
                last_visible_signature = visible_signature
                last_visible_midpoint = visible_midpoint
                last_drag_delta_y = None
                if not _mcp_call_succeeded(scroll_result, scroll_text):
                    prefer_drag_alignment = True
                    if container_frame is None:
                        return {
                            "action": action,
                            "ok": False,
                            "activated_pid": target_pid,
                            "activation": "mcp_session",
                            "mode": "collection_option_scroll_failed",
                            "collection_target_text": target_text,
                            "collection_target_order": target_order,
                            "collection_scroll_attempts": scroll_attempts,
                            "scroll_arguments": last_scroll_args,
                            "error": scroll_text[-1000:],
                        }
                else:
                    time.sleep(0.2)
                    continue
            if container_frame is None or visible_midpoint is None:
                break
            if (
                selected_order_per_point is not None
                and abs(selected_order_per_point) >= 0.01
                and current_selected_order is not None
                and not target_far_offscreen
            ):
                drag_delta_y = (float(target_order) - float(current_selected_order)) / float(selected_order_per_point)
                max_drag = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_DRAG_MAX_RATIO))
                drag_delta_y = max(-max_drag, min(max_drag, drag_delta_y))
                if 0.0 < abs(drag_delta_y) < COLLECTION_DRAG_MIN_POINTS:
                    drag_delta_y = COLLECTION_DRAG_MIN_POINTS if drag_delta_y > 0.0 else -COLLECTION_DRAG_MIN_POINTS
            elif (
                drag_target_y_per_point is not None
                and abs(drag_target_y_per_point) >= 0.01
                and current_target_center_y is not None
            ):
                _, container_center_y = _frame_center(container_frame)
                drag_delta_y = (container_center_y - current_target_center_y) / float(drag_target_y_per_point)
                max_drag = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_DRAG_MAX_RATIO))
                drag_delta_y = max(-max_drag, min(max_drag, drag_delta_y))
                if 0.0 < abs(drag_delta_y) < COLLECTION_DRAG_MIN_POINTS:
                    drag_delta_y = COLLECTION_DRAG_MIN_POINTS if drag_delta_y > 0.0 else -COLLECTION_DRAG_MIN_POINTS
            elif target_far_offscreen and current_target_center_y is not None:
                _, container_center_y = _frame_center(container_frame)
                desired_direction = 1.0 if current_target_center_y > container_center_y else -1.0
                drag_delta_y = desired_direction * min(
                    COLLECTION_DRAG_MAX_POINTS,
                    max(COLLECTION_DRAG_MIN_POINTS, abs(current_target_center_y - container_center_y) * 0.25),
                )
            elif current_selected_order is not None:
                drag_delta_y = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_VALUE_PROBE_RATIO))
                drag_delta_y *= probe_sign if int(target_order) >= int(current_selected_order) else -probe_sign
            elif current_target_center_y is not None:
                _, container_center_y = _frame_center(container_frame)
                drag_delta_y = container_center_y - current_target_center_y
                max_drag = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_DRAG_MAX_RATIO))
                drag_delta_y = max(-max_drag, min(max_drag, drag_delta_y))
                if 0.0 < abs(drag_delta_y) < COLLECTION_DRAG_MIN_POINTS:
                    drag_delta_y = COLLECTION_DRAG_MIN_POINTS if drag_delta_y > 0.0 else -COLLECTION_DRAG_MIN_POINTS
            elif drag_order_per_point is None or abs(drag_order_per_point) < 0.01:
                drag_delta_y = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_DRAG_PROBE_RATIO)) * probe_sign
            else:
                drag_delta_y = (float(target_order) - float(visible_midpoint)) / float(drag_order_per_point)
                max_drag = max(COLLECTION_DRAG_MIN_POINTS, min(COLLECTION_DRAG_MAX_POINTS, float(container_frame[3]) * COLLECTION_DRAG_MAX_RATIO))
                drag_delta_y = max(-max_drag, min(max_drag, drag_delta_y))
                if 0.0 < abs(drag_delta_y) < COLLECTION_DRAG_MIN_POINTS:
                    drag_delta_y = COLLECTION_DRAG_MIN_POINTS if drag_delta_y > 0.0 else -COLLECTION_DRAG_MIN_POINTS
            if current_selected_visible_item is not None and current_selected_visible_item.get("frame") is not None and current_selected_order is not None:
                last_drag_args = self._drag_arguments_for_collection_item(
                    target_identifier=target_identifier,
                    container_frame=container_frame,
                    item_frame=current_selected_visible_item["frame"],
                    delta_y=drag_delta_y,
                )
            else:
                last_drag_args = self._drag_arguments_for_collection_container(
                    target_identifier=target_identifier,
                    container_frame=container_frame,
                    delta_y=drag_delta_y,
                )
            drag_result = self.mcp.call_tool("drag", last_drag_args)
            drag_text = _content_text(drag_result)
            drag_attempts += 1
            last_alignment_method = "drag"
            last_visible_signature = visible_signature
            last_visible_midpoint = visible_midpoint
            last_drag_delta_y = drag_delta_y
            last_selected_order = current_selected_order
            last_selected_distance_to_target = current_selected_distance_to_target
            last_target_center_y = current_target_center_y
            last_target_distance_to_center = current_target_distance_to_center
            if not _mcp_call_succeeded(drag_result, drag_text):
                return {
                    "action": action,
                    "ok": False,
                    "activated_pid": target_pid,
                    "activation": "mcp_session",
                    "mode": "collection_option_drag_failed",
                    "collection_target_text": target_text,
                    "collection_target_order": target_order,
                    "collection_current_value_text": current_value_text,
                    "collection_scroll_attempts": scroll_attempts,
                    "collection_drag_attempts": drag_attempts,
                    "collection_reopen_attempts": reopen_attempts,
                    "drag_arguments": last_drag_args,
                    "error": drag_text[-1000:],
                }
            time.sleep(0.2)
        return {
            "action": action,
            "ok": False,
            "activated_pid": target_pid,
            "activation": "mcp_session",
            "mode": "collection_option_unresolved",
            "collection_target_text": target_text,
            "collection_target_order": target_order,
            "collection_current_value_text": current_value_text,
            "collection_scroll_attempts": scroll_attempts,
            "collection_drag_attempts": drag_attempts,
            "collection_reopen_attempts": reopen_attempts,
            "scroll_arguments": last_scroll_args,
            "drag_arguments": last_drag_args,
            "error": "collection target could not be brought into view for direct selection",
        }

    def execute_plan_via_mcp(
        self,
        actions: list[dict[str, Any]],
        element_index: dict[str, legacy.UiElement],
        planner_ax_index: dict[str, str],
        *,
        target_identifier: str,
        target_pid: int,
        app_profile: legacy.AppProfile | None = None,
        planner_elements: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        planner_elements_by_id = _planner_elements_by_id(planner_elements)
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
                if action_type == "click":
                    collection_result = self._maybe_execute_collection_option_selection(
                        action=action,
                        element=element,
                        planner_elements_by_id=planner_elements_by_id,
                        planner_ax_index=planner_ax_index,
                        target_identifier=target_identifier,
                        target_pid=target_pid,
                    )
                    if collection_result is not None:
                        collection_result["index"] = i
                        results.append(collection_result)
                        time.sleep(0.2)
                        continue
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
                        supported_secondary_actions = _parse_supported_secondary_actions(direct_ax_failure)
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
                                    "supported_secondary_actions": supported_secondary_actions,
                                    "error": direct_ax_failure,
                                }
                            )
                            time.sleep(0.2)
                            continue
                        if supported_secondary_actions and "Press" not in supported_secondary_actions:
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
                                    "fallback_skipped": "secondary_action_available",
                                    "supported_secondary_actions": supported_secondary_actions,
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

            if action_type == "secondary_action":
                element = legacy.action_element(action, element_index)
                if element is None:
                    raise ValueError(f"secondary_action needs element_id: {action!r}")
                collection_result = self._maybe_execute_collection_option_selection(
                    action=action,
                    element=element,
                    planner_elements_by_id=planner_elements_by_id,
                    planner_ax_index=planner_ax_index,
                    target_identifier=target_identifier,
                    target_pid=target_pid,
                )
                if collection_result is not None:
                    collection_result["index"] = i
                    results.append(collection_result)
                    time.sleep(0.2)
                    continue
                mcp_element_index = planner_ax_index.get(element.element_id)
                if mcp_element_index is None:
                    raise ValueError(f"secondary_action target is missing MCP element index: {action!r}")
                secondary_action = str(action.get("action") or "")
                scroll_direction = _secondary_action_scroll_direction(secondary_action)
                if scroll_direction is not None:
                    summary = planner_elements_by_id.get(element.element_id, {})
                    scroll_element_id = str(summary.get("collection_container_element_id") or element.element_id)
                    scroll_mcp_element_index = planner_ax_index.get(scroll_element_id, mcp_element_index)
                    result = self.mcp.call_tool(
                        "scroll",
                        {
                            "app": target_identifier,
                            "element_index": scroll_mcp_element_index,
                            "direction": scroll_direction,
                            "pages": PRECISE_SCROLL_PAGES,
                        },
                    )
                    text = _content_text(result)
                    results.append(
                        {
                            "index": i,
                            "action": action,
                            "ok": _mcp_call_succeeded(result, text),
                            "activated_pid": target_pid,
                            "activation": "mcp_session",
                            "mode": "secondary_action_scroll",
                            "ax_path": element.ax_path,
                            "pages": PRECISE_SCROLL_PAGES,
                            "tool_output": text[-1000:],
                        }
                    )
                else:
                    result = self.mcp.call_tool(
                        "perform_secondary_action",
                        {"app": target_identifier, "element_index": mcp_element_index, "action": secondary_action},
                    )
                    text = _content_text(result)
                    results.append(
                        {
                            "index": i,
                            "action": action,
                            "ok": _mcp_call_succeeded(result, text),
                            "activated_pid": target_pid,
                            "activation": "mcp_session",
                            "mode": "secondary_action",
                            "ax_path": element.ax_path,
                            "tool_output": text[-1000:],
                        }
                    )
                time.sleep(0.2)
                continue

            if action_type == "drag":
                from_point = _drag_point(action, element_index, "from")
                to_point = _drag_point(action, element_index, "to")
                result = self.mcp.call_tool(
                    "drag",
                    {
                        "app": target_identifier,
                        "from_x": from_point[0],
                        "from_y": from_point[1],
                        "to_x": to_point[0],
                        "to_y": to_point[1],
                    },
                )
                text = _content_text(result)
                results.append(
                    {
                        "index": i,
                        "action": action,
                        "ok": _mcp_call_succeeded(result, text),
                        "activated_pid": target_pid,
                        "activation": "mcp_session",
                        "mode": "drag",
                        "from_point": {"x": from_point[0], "y": from_point[1]},
                        "to_point": {"x": to_point[0], "y": to_point[1]},
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
                mcp_element_index = None
                if element is not None:
                    summary = planner_elements_by_id.get(element.element_id, {})
                    scroll_element_id = str(summary.get("collection_container_element_id") or element.element_id)
                    mcp_element_index = planner_ax_index.get(scroll_element_id)
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
                use_clipboard_paste = _should_use_clipboard_paste(text)
                if use_clipboard_paste:
                    _write_clipboard(text)
                    result = self.mcp.call_tool("press_key", {"app": target_identifier, "key": "cmd+v"})
                    tool_text = _content_text(result)
                    mode = "clipboard_paste"
                    input_method = "pbcopy_cmd_v"
                else:
                    result = self.mcp.call_tool("type_text", {"app": target_identifier, "text": text})
                    tool_text = _content_text(result)
                    mode = "keyboard"
                    input_method = "mcp_type_text"
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
                        "mode": mode,
                        "input_method": input_method,
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
    if isinstance(run_log.get("stages"), list):
        return {
            "final_status": run_log.get("final_status"),
            "instruction": run_log.get("instruction"),
            "workflow_type": "multi_stage",
            "stage_count": len(run_log.get("stages", [])),
            "artifact_dir": run_log.get("artifact_dir"),
            "plan_output": run_log.get("plan_output"),
            "stages": [
                {
                    "name": stage.get("name"),
                    "target": stage.get("target"),
                    "instruction": stage.get("instruction"),
                    "final_status": stage.get("final_status"),
                    "plan_output": stage.get("plan_output"),
                    "llm_call_dir": stage.get("llm_call_dir"),
                }
                for stage in run_log.get("stages", [])
            ],
        }
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


def run_single_workflow_state(
    args: argparse.Namespace,
    *,
    mcp_client: MCPClient | Any | None = None,
) -> tuple[dict[str, Any], LangGraphComputerUseWorkflow]:
    runner = LangGraphComputerUseWorkflow(args, mcp_client=mcp_client)
    recursion_limit = max(50, runner.max_steps * 4 + 12)
    try:
        final_state = runner.build_graph().invoke({}, config={"recursion_limit": recursion_limit})
    except Exception:
        runner.close()
        raise
    return final_state, runner


def run_multi_stage_workflow(args: argparse.Namespace, stages: list[WorkflowStage], *, mcp_client: MCPClient | Any | None = None) -> dict[str, Any]:
    shared_client = mcp_client
    owns_client = False
    if shared_client is None:
        shared_client = MCPClient(MCP_SERVER, MCP_TIMEOUT_SECONDS)
        shared_client.initialize()
        owns_client = True

    artifact_dir = legacy.workflow_run_artifact_dir(args.plan_output, cwd=Path.cwd()) if args.plan_output else None
    run_log: dict[str, Any] = {
        "instruction": args.instruction,
        "execute": args.execute,
        "workflow_type": "multi_stage",
        "artifact_dir": os.fspath(artifact_dir) if artifact_dir else None,
        "plan_output": os.fspath(args.plan_output) if args.plan_output else None,
        "started_at": time.time(),
        "stages": [],
        "final_status": "running",
    }
    share_text: str | None = None

    try:
        for index, stage in enumerate(stages, start=1):
            stage_args = copy.deepcopy(args)
            if share_text and "{{shared_payload}}" in stage.instruction:
                stage_args.instruction = stage.instruction.replace("{{shared_payload}}", share_text)
            elif share_text and stage.metadata.get("recipient") and index == len(stages):
                stage_args.instruction = (
                    f"打开目标发送应用，给好友“{stage.metadata.get('recipient', '')}”发送以下消息。"
                    "不要改写，不要截断，发送后再结束：\n"
                    f"{share_text}"
                )
            else:
                stage_args.instruction = stage.instruction
            stage_args.target = stage.target
            stage_args.plan_output = _stage_output_path(args.plan_output, index, stage.name)
            stage_args.traversal_output = _stage_output_path(args.traversal_output, index, stage.name)
            final_state, runner = run_single_workflow_state(stage_args, mcp_client=shared_client)
            try:
                stage_run_log = final_state["run_log"]
                stage_record = {
                    "name": stage.name,
                    "target": stage.target,
                    "instruction": stage_args.instruction,
                    "final_status": stage_run_log.get("final_status"),
                    "plan_output": stage_run_log.get("plan_output"),
                    "artifact_dir": stage_run_log.get("artifact_dir"),
                    "llm_call_dir": stage_run_log.get("llm_call_dir"),
                    "workflow_mode": stage_run_log.get("workflow_mode"),
                    "app_profile": stage_run_log.get("app_profile"),
                    "stage_max_steps_hint": stage.metadata.get("max_steps"),
                    "effective_max_steps": stage_args.max_steps,
                    "stage_log": stage_run_log,
                }
                if stage.expects_share_text:
                    share_text = runner.extract_share_text(final_state)
                    stage_record["extracted_share_text_preview"] = legacy.clean_text(share_text, limit=240) if share_text else None
                    if not share_text:
                        stage_record["final_status"] = "blocked"
                        run_log["stages"].append(stage_record)
                        run_log["final_status"] = "blocked"
                        break
                run_log["stages"].append(stage_record)
                if stage_record["final_status"] not in {"finished", "dry_run"}:
                    run_log["final_status"] = stage_record["final_status"] or "blocked"
                    break
            finally:
                runner.close()
        else:
            run_log["final_status"] = "dry_run" if not args.execute else "finished"
    finally:
        run_log["completed_at"] = time.time()
        if owns_client and shared_client is not None:
            shared_client.close()
    if args.plan_output:
        legacy.write_json(args.plan_output, run_log)
    return run_log


def run_workflow(args: argparse.Namespace, *, mcp_client: MCPClient | Any | None = None) -> dict[str, Any]:
    shared_client = mcp_client
    owns_client = False
    if shared_client is None:
        shared_client = MCPClient(MCP_SERVER, MCP_TIMEOUT_SECONDS)
        shared_client.initialize()
        owns_client = True
    artifact_dir = legacy.workflow_run_artifact_dir(args.plan_output, cwd=Path.cwd()) if args.plan_output else None
    try:
        stage_plan = plan_task_stages(args, shared_client, artifact_dir=artifact_dir)
        if stage_plan:
            return run_multi_stage_workflow(args, stage_plan, mcp_client=shared_client)
        final_state, runner = run_single_workflow_state(args, mcp_client=shared_client)
        run_log = final_state["run_log"]
        try:
            if args.plan_output:
                legacy.write_json(args.plan_output, run_log)
            return run_log
        finally:
            runner.close()
    finally:
        if owns_client and shared_client is not None:
            shared_client.close()


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
