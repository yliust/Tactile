#!/usr/bin/env python3
"""LLM-driven workflow for controlling a Windows app through UI Automation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SDK_ROOT = Path(__file__).resolve().parents[1]
SDK_SCRIPT = SDK_ROOT / "WindowsUseSDK.ps1"
SKILL_ROOT = SDK_ROOT.parents[1] if len(SDK_ROOT.parents) > 1 else None
ALLOWED_ACTION_TYPES = {
    "click",
    "doubleclick",
    "rightclick",
    "mousemove",
    "scroll",
    "writetext",
    "streamtext",
    "pastetext",
    "keypress",
    "wait",
    "finish",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class UiElement:
    element_id: str
    role: str
    text: str | None
    x: float | None
    y: float | None
    width: float | None
    height: float | None
    uia_path: str | None = None
    patterns: tuple[str, ...] = ()

    @property
    def center(self) -> tuple[float, float]:
        if self.x is None or self.y is None or self.width is None or self.height is None:
            raise ValueError(f"{self.element_id} has no complete frame")
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


@dataclass(frozen=True)
class AppCandidate:
    display_name: str
    identifier: str
    aliases: tuple[str, ...]
    source: str
    hwnd: int | None = None
    pid: int | None = None
    app_id: str | None = None
    path: str | None = None


def powershell_exe() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def decode_process_output(data: bytes) -> str:
    if not data:
        return ""
    encodings = ["utf-8-sig", locale.getpreferredencoding(False), "mbcs", "gbk", "utf-16-le"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def run_sdk(args: list[str], *, timeout: float | None = None) -> dict[str, Any]:
    cmd = [
        powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        os.fspath(SDK_SCRIPT),
        *args,
    ]
    proc = subprocess.run(
        cmd,
        cwd=SDK_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stdout = decode_process_output(proc.stdout)
    stderr = decode_process_output(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"WindowsUseSDK failed ({proc.returncode}) for {args!r}\n"
            f"stdout:\n{stdout[-2000:]}\n"
            f"stderr:\n{stderr[-2000:]}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"WindowsUseSDK returned non-JSON output: {stdout[:2000]!r}") from exc
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(str(payload.get("error") or payload))
    return payload


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def clean_text(value: Any, *, limit: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    if not compact:
        return None
    if len(compact) > limit:
        return compact[: limit - 1] + "..."
    return compact


def unique_preserving_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            continue
        key = normalize_name(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def discover_apps() -> list[AppCandidate]:
    payload = run_sdk(["list-apps", "--limit", "300"], timeout=20)
    candidates: list[AppCandidate] = []
    for item in payload.get("applications", []):
        if not isinstance(item, dict):
            continue
        aliases = unique_preserving_order(
            [
                str(item.get("name") or ""),
                str(item.get("title") or ""),
                str(item.get("app_id") or ""),
                str(item.get("exe_name") or ""),
                str(item.get("path") or ""),
            ]
        )
        if not aliases:
            continue
        display_name = aliases[0]
        if str(item.get("source") or "") == "running_window":
            identifier = str(item.get("name") or item.get("title") or display_name)
        else:
            identifier = (
                str(item.get("app_id") or "")
                or str(item.get("path") or "")
                or str(item.get("name") or "")
                or display_name
            )
        candidates.append(
            AppCandidate(
                display_name=display_name,
                identifier=identifier,
                aliases=aliases,
                source=str(item.get("source") or "windows"),
                hwnd=int(item["hwnd"]) if item.get("hwnd") else None,
                pid=int(item["pid"]) if item.get("pid") else None,
                app_id=str(item.get("app_id") or "") or None,
                path=str(item.get("path") or "") or None,
            )
        )
    return candidates


def app_match_score(instruction: str, candidate: AppCandidate) -> tuple[int, int, str]:
    normalized_instruction = normalize_name(instruction)
    best_score = 0
    best_alias = ""
    best_alias_length = 0
    for alias in candidate.aliases:
        normalized_alias = normalize_name(alias)
        if not normalized_alias or len(normalized_alias) < 2:
            continue
        score = 0
        if normalized_alias in normalized_instruction:
            score = 1000 + len(normalized_alias)
        elif normalized_instruction in normalized_alias:
            score = 700 + len(normalized_instruction)
        if score > best_score:
            best_score = score
            best_alias = alias
            best_alias_length = len(normalized_alias)
    return best_score, best_alias_length, best_alias


def resolve_app_identifier(user_instruction: str, explicit_target: str | None = None) -> tuple[str, dict[str, Any]]:
    apps = discover_apps()
    if explicit_target:
        target_norm = normalize_name(explicit_target)
        exact_matches = [
            app
            for app in apps
            if any(normalize_name(alias) == target_norm for alias in app.aliases)
            or (app.app_id and normalize_name(app.app_id) == target_norm)
            or (app.path and normalize_name(Path(app.path).stem) == target_norm)
        ]
        if exact_matches:
            chosen = sorted(exact_matches, key=lambda app: (app.hwnd is None, len(app.display_name)))[0]
            return chosen.identifier, {
                "mode": "explicit_target",
                "input": explicit_target,
                "display_name": chosen.display_name,
                "matched_alias": explicit_target,
                "identifier": chosen.identifier,
                "source": chosen.source,
                "hwnd": chosen.hwnd,
                "pid": chosen.pid,
            }
        return explicit_target, {"mode": "explicit_target_unresolved", "input": explicit_target, "identifier": explicit_target}

    scored: list[tuple[int, int, str, AppCandidate]] = []
    for app in apps:
        score, alias_length, alias = app_match_score(user_instruction, app)
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
            ],
            key=lambda item: item["display_name"].casefold(),
        )[:30]
        raise RuntimeError(
            "could not infer target app from instruction. Mention an app name or pass --target.\n"
            f"Sample discovered apps: {json.dumps(suggestions, ensure_ascii=False)}"
        )

    scored.sort(key=lambda item: (item[0], item[1], item[3].hwnd is not None), reverse=True)
    top_score = scored[0][0]
    best = [item for item in scored if item[0] == top_score]
    chosen_score, _, matched_alias, chosen = best[0]
    return chosen.identifier, {
        "mode": "inferred_from_instruction",
        "display_name": chosen.display_name,
        "matched_alias": matched_alias,
        "score": chosen_score,
        "identifier": chosen.identifier,
        "source": chosen.source,
        "hwnd": chosen.hwnd,
        "pid": chosen.pid,
        "ambiguous_matches": [
            {
                "display_name": item[3].display_name,
                "matched_alias": item[2],
                "identifier": item[3].identifier,
                "score": item[0],
            }
            for item in best[:5]
        ],
    }


def open_or_activate_app(app_identifier: str) -> dict[str, Any]:
    return run_sdk(["open", app_identifier], timeout=30)


def refresh_target_window(app_identifier: str, current_hwnd: int) -> tuple[int, dict[str, Any] | None]:
    try:
        refreshed = run_sdk(["open", app_identifier, "--no-activate"], timeout=20)
    except Exception as exc:
        return current_hwnd, {"status": "unavailable", "error": str(exc)}
    try:
        refreshed_hwnd = int(refreshed["hwnd"])
    except (KeyError, TypeError, ValueError):
        return current_hwnd, {"status": "unavailable", "result": refreshed}
    return refreshed_hwnd, refreshed


def traversal_signal(traversal: dict[str, Any]) -> int:
    stats = traversal.get("stats") if isinstance(traversal.get("stats"), dict) else {}
    try:
        return int(stats.get("count") or len(traversal.get("elements") or []))
    except (TypeError, ValueError):
        return len(traversal.get("elements") or [])


def should_probe_raw_view(traversal: dict[str, Any]) -> bool:
    app_name = str(traversal.get("app_name") or "").casefold()
    title = str(traversal.get("title") or "").casefold()
    electron_like = any(
        marker in f"{app_name} {title}"
        for marker in ("feishu", "lark", "electron", "chrome", "chromium", "slack", "teams")
    )
    return electron_like or traversal_signal(traversal) < 40


def summarize_view_choice(view: str, traversal: dict[str, Any]) -> dict[str, Any]:
    return {
        "view": view,
        "count": traversal_signal(traversal),
        "stats": traversal.get("stats", {}),
        "processing_time_seconds": traversal.get("processing_time_seconds"),
    }


def traverse_app(hwnd: int, *, no_activate: bool = True, view: str = "auto") -> dict[str, Any]:
    def run_view(view_name: str) -> dict[str, Any]:
        args = ["traverse", "--hwnd", str(hwnd), "--visible-only", "--view", view_name]
        if no_activate:
            args.append("--no-activate")
        return run_sdk(args, timeout=30 if view_name == "raw" else 25)

    if view != "auto":
        return run_view(view)

    control = run_view("control")
    if not should_probe_raw_view(control):
        control["view_selection"] = {"selected": "control", "reason": "control_view_sufficient"}
        return control
    try:
        raw = run_view("raw")
    except Exception as exc:
        control["view_selection"] = {
            "selected": "control",
            "reason": "raw_view_unavailable",
            "raw_error": str(exc),
        }
        return control

    control_score = traversal_signal(control)
    raw_score = traversal_signal(raw)
    if raw_score >= max(control_score + 8, int(control_score * 1.25)):
        raw["view_selection"] = {
            "selected": "raw",
            "reason": "raw_view_exposed_more_ui",
            "alternates": {"control": summarize_view_choice("control", control)},
        }
        return raw

    control["view_selection"] = {
        "selected": "control",
        "reason": "raw_view_not_better",
        "alternates": {"raw": summarize_view_choice("raw", raw)},
    }
    return control


def is_feishu_like(traversal: dict[str, Any]) -> bool:
    app_name = str(traversal.get("app_name") or "").casefold()
    title = str(traversal.get("title") or "").casefold()
    return any(marker in f"{app_name} {title}" for marker in ("feishu", "lark", "飞书"))


def has_real_text_input(index: dict[str, UiElement]) -> bool:
    return any(element.role in {"Edit", "ComboBox", "Document"} for element in index.values())


def add_hint(
    summary: list[dict[str, Any]],
    index: dict[str, UiElement],
    *,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    hint_id = f"h{len(index)}"
    ui_element = UiElement(
        element_id=hint_id,
        role="VirtualRegion",
        text=text,
        x=float(x),
        y=float(y),
        width=float(width),
        height=float(height),
    )
    index[hint_id] = ui_element
    summary.append(
        {
            "id": hint_id,
            "role": ui_element.role,
            "text": ui_element.text,
            "direct_uia": False,
            "patterns": [],
            "frame": {
                "x": ui_element.x,
                "y": ui_element.y,
                "width": ui_element.width,
                "height": ui_element.height,
            },
        }
    )


def element_frame(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    if element.get("x") is None or element.get("y") is None or element.get("width") is None or element.get("height") is None:
        return None
    try:
        x = float(element["x"])
        y = float(element["y"])
        width = float(element["width"])
        height = float(element["height"])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (x, y, width, height)


def role_priority(role: str, text: str) -> int:
    if role in {"Edit", "ComboBox", "Document"}:
        return 0
    if role in {"Button", "CheckBox", "RadioButton", "SplitButton", "Hyperlink", "MenuItem"}:
        return 1
    if role in {"ListItem", "TreeItem", "DataItem", "TabItem"}:
        return 2
    if text:
        return 3
    return 4


def element_priority(element: dict[str, Any]) -> tuple[int, float, float]:
    role = str(element.get("role") or "")
    text = clean_text(element.get("text")) or ""
    y = float(element.get("y") or 0)
    width = float(element.get("width") or 0)
    return (role_priority(role, text), -y, -width)


def summarize_elements(
    traversal: dict[str, Any],
    *,
    max_elements: int,
    include_virtual_hints: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, UiElement]]:
    raw_elements = traversal.get("elements") or []
    candidates = [
        element
        for element in raw_elements
        if isinstance(element, dict)
        and element.get("x") is not None
        and element.get("y") is not None
        and not bool(element.get("is_offscreen"))
    ]
    candidates.sort(key=element_priority)
    selected = candidates[:max_elements]

    summary: list[dict[str, Any]] = []
    index: dict[str, UiElement] = {}
    for i, element in enumerate(selected):
        element_id = f"e{i}"
        patterns = tuple(str(item) for item in (element.get("patterns") or []) if item)
        ui_element = UiElement(
            element_id=element_id,
            role=str(element.get("role") or ""),
            text=clean_text(element.get("text")),
            x=float(element["x"]) if element.get("x") is not None else None,
            y=float(element["y"]) if element.get("y") is not None else None,
            width=float(element["width"]) if element.get("width") is not None else None,
            height=float(element["height"]) if element.get("height") is not None else None,
            uia_path=clean_text(element.get("uiaPath") or element.get("uia_path"), limit=1000),
            patterns=patterns,
        )
        index[element_id] = ui_element
        summary.append(
            {
                "id": element_id,
                "role": ui_element.role,
                "text": ui_element.text,
                "direct_uia": ui_element.uia_path is not None,
                "patterns": list(ui_element.patterns),
                "frame": {
                    "x": ui_element.x,
                    "y": ui_element.y,
                    "width": ui_element.width,
                    "height": ui_element.height,
                },
            }
        )
    if include_virtual_hints:
        add_virtual_region_hints(summary, index, traversal)
    return summary, index


def add_virtual_region_hints(
    summary: list[dict[str, Any]],
    index: dict[str, UiElement],
    traversal: dict[str, Any],
) -> None:
    feishu_like = is_feishu_like(traversal)
    if has_real_text_input(index) and not feishu_like:
        return
    windows = [
        element_frame(element)
        for element in traversal.get("elements", [])
        if isinstance(element, dict) and str(element.get("role")) == "Window"
    ]
    window_frames = sorted((frame for frame in windows if frame is not None), key=lambda item: item[2] * item[3], reverse=True)
    if not window_frames:
        root_frame = element_frame(
            {
                "x": 0,
                "y": 0,
                "width": 1200,
                "height": 800,
            }
        )
        window_frames = [root_frame] if root_frame else []

    app_name = str(traversal.get("app_name", "")).casefold()
    max_hint_windows = 1 if feishu_like else 2
    for window_i, (x, y, width, height) in enumerate(window_frames[:max_hint_windows]):
        if width < 300 or height < 220:
            continue
        if feishu_like:
            dock_center_y = y + height - 52
            slot_size = 56
            specs = [
                (
                    "Feishu/Lark profile/avatar button; use only to open the profile card for verification, not as the organization switcher",
                    x + 16,
                    y + 22,
                    82,
                    64,
                ),
                (
                    "Feishu/Lark bottom organization dock; probe/click visible org icons here and verify by OCRing the profile card",
                    x + 20,
                    y + height - 104,
                    min(340, max(220, width * 0.20)),
                    92,
                ),
                *[
                    (
                        (
                            "Feishu/Lark bottom organization dock more button; open only to inspect visible existing orgs, never join/create/login"
                            if slot == 3
                            else f"Feishu/Lark bottom organization dock slot {slot + 1}; current org is usually first after switching, verify via profile card OCR"
                        ),
                        x + 56 + 60 * slot - slot_size / 2,
                        dock_center_y - slot_size / 2,
                        slot_size,
                        slot_size,
                    )
                    for slot in range(4)
                ],
                ("Feishu/Lark global search candidate; Ctrl+K is often better if no real Edit field is exposed", x + 70, y + 36, min(360, max(220, width * 0.34)), 46),
                ("Feishu/Lark first search/chat result candidate; re-observe or OCR the row before opening", x + 64, y + 88, min(420, max(260, width * 0.38)), 78),
                ("Feishu/Lark compose input candidate; use only after the chat title or input placeholder confirms the recipient", x + width * 0.28, y + height - 132, width * 0.68, 96),
            ]
        elif "wechat" in app_name or "weixin" in app_name:
            specs = [
                ("WeChat left search field candidate generated by workflow", x + 76, y + 18, min(220, width * 0.28), 34),
                ("WeChat top search result candidate generated by workflow", x + 64, y + 56, min(300, width * 0.32), 76),
                ("WeChat compose input candidate generated by workflow; use after chat title is verified", x + width * 0.36, y + height - 190, width * 0.60, 150),
            ]
        else:
            specs = [
                ("top-left search/input candidate generated by workflow; use only when real UIA edit fields are missing", x + 70, y + 36, min(300, max(160, width * 0.34)), 42),
                ("bottom compose/input candidate generated by workflow; use only after the intended context is visibly selected", x + width * 0.30, y + height - 110, width * 0.64, 74),
            ]
        for text, hx, hy, hwidth, hheight in specs:
            add_hint(summary, index, text=text, x=hx, y=hy, width=hwidth, height=hheight)


def build_planner_prompt(
    user_instruction: str,
    target_identifier: str,
    traversal: dict[str, Any],
    elements: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    step_number: int,
    max_steps: int,
    max_actions_per_step: int,
) -> str:
    payload = {
        "target_identifier": target_identifier,
        "app_name": traversal.get("app_name"),
        "hwnd": traversal.get("hwnd"),
        "pid": traversal.get("pid"),
        "title": traversal.get("title"),
        "uia_view": traversal.get("view"),
        "view_selection": traversal.get("view_selection"),
        "accessibility_hint": traversal.get("accessibility_hint"),
        "stats": traversal.get("stats", {}),
        "elements": elements,
        "history": history[-6:],
        "step": step_number,
        "max_steps": max_steps,
    }
    return (
        "You control a Windows desktop app through UI Automation observations.\n"
        "Choose at most one small action for the current state. After it runs, the workflow will observe again.\n"
        "Prefer element_id actions. Click actions on UIA elements use the UIA frame as the coordinate source.\n"
        "If accessibility_hint reports sparse Chromium UIA, use app-specific shortcuts and targeted virtual regions; do not assume hidden semantic elements exist.\n"
        "For messaging tasks, select and verify the recipient/chat before typing the message body.\n"
        "If an observed window looks like login/scan/auth but the app may already have another window, wait or finish blocked only after the workflow has refreshed the top-level window choice.\n"
        "Use finish only when the user goal is complete, blocked, or needs human confirmation.\n"
        f"Hard limit: at most {max_actions_per_step} action in this step.\n"
        "Return JSON exactly: {\"status\":\"continue|finished|blocked\",\"summary\":\"...\",\"actions\":[...]}.\n\n"
        f"User instruction: {user_instruction}\n\n"
        "Allowed action examples:\n"
        "{\"type\":\"click\",\"element_id\":\"e3\"}\n"
        "{\"type\":\"writetext\",\"element_id\":\"e5\",\"text\":\"hello\"}\n"
        "{\"type\":\"streamtext\",\"text\":\"hello\"}\n"
        "{\"type\":\"keypress\",\"key\":\"ctrl+f\"}\n"
        "{\"type\":\"scroll\",\"element_id\":\"e7\",\"deltaY\":5}\n"
        "{\"type\":\"finish\"}\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def load_llm_helpers():
    sys.path.insert(0, os.fspath(SDK_ROOT))
    from utils.llm_config import call_llm, extract_and_convert_dict  # type: ignore

    return call_llm, extract_and_convert_dict


def parse_llm_plan(raw_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        _, extract_and_convert_dict = load_llm_helpers()
        parsed = extract_and_convert_dict(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM did not return a JSON object: {raw_text[:500]!r}")
    if not isinstance(parsed.get("actions"), list):
        raise ValueError(f"LLM plan is missing an actions list: {parsed!r}")
    return parsed


def find_best_text_input(element_index: dict[str, UiElement]) -> str | None:
    candidates = [
        element
        for element in element_index.values()
        if element.role in {"Edit", "ComboBox", "Document"} or "Value" in element.patterns
    ]
    if not candidates:
        candidates = [
            element
            for element in element_index.values()
            if element.role == "VirtualRegion" and element.text and "input candidate" in element.text
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: ((item.y or 0), (item.width or 0)))
    return candidates[-1].element_id


def fallback_plan(user_instruction: str, element_index: dict[str, UiElement], history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    history = history or []
    prior_action_types = [
        action.get("type")
        for step in history
        for action in step.get("actions", [])
        if isinstance(action, dict)
    ]
    text_input_id = find_best_text_input(element_index)
    if "click" not in prior_action_types and text_input_id:
        action = {"type": "click", "element_id": text_input_id}
    elif "writetext" not in prior_action_types:
        action = {"type": "writetext", "text": user_instruction}
    elif "keypress" not in prior_action_types:
        action = {"type": "keypress", "key": "enter"}
    else:
        action = {"type": "finish"}
    return {"status": "continue", "summary": "Fallback plan: advance one UI action and observe again.", "actions": [action]}


def make_plan(
    user_instruction: str,
    target_identifier: str,
    traversal: dict[str, Any],
    elements: list[dict[str, Any]],
    element_index: dict[str, UiElement],
    history: list[dict[str, Any]],
    *,
    step_number: int,
    max_steps: int,
    max_actions_per_step: int,
    model: str | None,
    provider: str | None,
    mock_plan: bool,
    allow_fallback: bool,
) -> dict[str, Any]:
    if mock_plan:
        return fallback_plan(user_instruction, element_index, history)
    prompt = build_planner_prompt(
        user_instruction,
        target_identifier,
        traversal,
        elements,
        history,
        step_number=step_number,
        max_steps=max_steps,
        max_actions_per_step=max_actions_per_step,
    )
    try:
        call_llm, _ = load_llm_helpers()
        raw = call_llm(prompt, **{k: v for k, v in {"model_name": model, "provider": provider}.items() if v})
        return parse_llm_plan(raw)
    except Exception as exc:
        if not allow_fallback:
            raise
        print(f"warning: LLM planning failed, using fallback plan: {exc}", file=sys.stderr)
        return fallback_plan(user_instruction, element_index, history)


def validate_plan(
    plan: dict[str, Any],
    element_index: dict[str, UiElement],
    *,
    max_actions_per_step: int,
) -> list[dict[str, Any]]:
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise ValueError("plan.actions must be a list")
    if len(actions) > max_actions_per_step:
        plan["dropped_actions"] = actions[max_actions_per_step:]
        plan["actions"] = actions[:max_actions_per_step]
        actions = plan["actions"]

    normalized: list[dict[str, Any]] = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            raise ValueError(f"action must be an object: {raw_action!r}")
        action_type = str(raw_action.get("type", "")).lower()
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ValueError(f"unsupported action type: {action_type!r}")
        action = dict(raw_action)
        action["type"] = action_type
        if "element_id" in action and action["element_id"] not in element_index:
            raise ValueError(f"unknown element_id: {action['element_id']!r}")
        normalized.append(action)
    return normalized


def action_point(action: dict[str, Any], element_index: dict[str, UiElement]) -> tuple[float, float]:
    element_id = action.get("element_id")
    if element_id:
        return element_index[str(element_id)].center
    try:
        return (float(action["x"]), float(action["y"]))
    except KeyError as exc:
        raise ValueError(f"action needs element_id or x/y: {action!r}") from exc


def action_element(action: dict[str, Any], element_index: dict[str, UiElement]) -> UiElement | None:
    element_id = action.get("element_id")
    if not element_id:
        return None
    return element_index[str(element_id)]


def execute_plan(actions: list[dict[str, Any]], element_index: dict[str, UiElement], *, hwnd: int) -> list[dict[str, Any]]:
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

        element = action_element(action, element_index)
        if action_type in {"click", "doubleclick", "rightclick", "mousemove"}:
            if action_type == "click" and element is not None and element.uia_path is not None:
                try:
                    payload = run_sdk(["uia", "click", str(hwnd), element.uia_path], timeout=10)
                    results.append({"index": i, "action": action, "ok": True, "mode": "uia_coordinate_click", "result": payload})
                    time.sleep(0.25)
                    continue
                except Exception as exc:
                    print(f"warning: UIA coordinate click failed; falling back to coordinate input: {exc}", file=sys.stderr)
            x, y = action_point(action, element_index)
            payload = run_sdk(["input", "--hwnd", str(hwnd), action_type, f"{x:.1f}", f"{y:.1f}"], timeout=10)
            results.append({"index": i, "action": action, "ok": True, "mode": "coordinate", "result": payload})
            time.sleep(0.25)
            continue
        if action_type == "scroll":
            x, y = action_point(action, element_index)
            delta_y = int(action.get("deltaY", action.get("delta_y", 5)))
            payload = run_sdk(["input", "--hwnd", str(hwnd), "scroll", f"{x:.1f}", f"{y:.1f}", str(delta_y)], timeout=10)
            results.append({"index": i, "action": action, "ok": True, "mode": "coordinate", "result": payload})
            time.sleep(0.25)
            continue
        if action_type in {"writetext", "streamtext", "pastetext"}:
            text = str(action.get("text", ""))
            if not text:
                results.append({"index": i, "action": action, "ok": True, "skipped": "empty text"})
                continue
            focus_result: dict[str, Any] | None = None
            if element is not None and element.uia_path is not None:
                try:
                    focus_action = "focus" if element.role in {"Edit", "ComboBox", "Document"} else "click"
                    focus_result = run_sdk(["uia", focus_action, str(hwnd), element.uia_path], timeout=10)
                    time.sleep(0.12)
                except Exception as exc:
                    print(f"warning: could not focus text target before typing; continuing with current focus: {exc}", file=sys.stderr)
            elif element is not None:
                x, y = element.center
                focus_result = run_sdk(["input", "--hwnd", str(hwnd), "click", f"{x:.1f}", f"{y:.1f}"], timeout=10)
                time.sleep(0.12)
            input_action = "pastetext" if action_type == "pastetext" or action.get("method") == "paste" else "streamtext"
            payload = run_sdk(["input", "--hwnd", str(hwnd), input_action, text], timeout=max(10, len(text) * 0.08))
            results.append(
                {
                    "index": i,
                    "action": {"type": action_type, "element_id": action.get("element_id"), "text_length": len(text)},
                    "ok": True,
                    "mode": "clipboard_paste" if input_action == "pastetext" else "unicode_stream",
                    "focus_result": focus_result,
                    "result": payload,
                }
            )
            time.sleep(0.25)
            continue
        if action_type == "keypress":
            key = str(action.get("key") or action.get("keys") or "").strip()
            if not key:
                raise ValueError(f"keypress action needs key: {action!r}")
            payload = run_sdk(["input", "--hwnd", str(hwnd), "keypress", key], timeout=10)
            results.append({"index": i, "action": action, "ok": True, "mode": "keyboard", "result": payload})
            time.sleep(0.25)
            continue
        raise ValueError(f"unhandled action type: {action_type}")
    return results


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_TRACE_MODULE: Any | None = None
_TRACE_LOAD_ATTEMPTED = False


def load_tactile_trace_module() -> Any | None:
    global _TRACE_LOAD_ATTEMPTED, _TRACE_MODULE
    if _TRACE_LOAD_ATTEMPTED:
        return _TRACE_MODULE
    _TRACE_LOAD_ATTEMPTED = True
    if SKILL_ROOT is None:
        return None
    module_path = SKILL_ROOT / "scripts" / "utils" / "tactile_trace.py"
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_tactile_windows_trace", module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"warning: could not load Tactile trace helper: {exc}", file=sys.stderr)
        return None
    _TRACE_MODULE = module
    return module


def refresh_trace(run_log: dict[str, Any]) -> None:
    module = load_tactile_trace_module()
    if module is not None:
        run_log["trace"] = module.build_trace(run_log, platform="windows")


def print_observation_debug(step_number: int, elements: list[dict[str, Any]], *, limit: int = 80) -> None:
    print(f"observation step {step_number}: {len(elements)} summarized elements", file=sys.stderr)
    for element in elements[:limit]:
        frame = element.get("frame", {})
        print(
            f"  {element.get('id')}: role={element.get('role')!r} "
            f"text={element.get('text')!r} "
            f"frame=({frame.get('x')}, {frame.get('y')}, {frame.get('width')}, {frame.get('height')})",
            file=sys.stderr,
        )
    if len(elements) > limit:
        print(f"  ... {len(elements) - limit} more", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan and optionally execute Windows UI actions from a natural-language instruction.")
    parser.add_argument("instruction", nargs="?", default="", help="Natural-language instruction for the target app.")
    parser.add_argument("--target", default=None, help="Optional app name, app id, exe name, path, or window title.")
    parser.add_argument("--list-apps", action="store_true", help="List discovered local apps and windows.")
    parser.add_argument("--model", default=None, help="Override model_name passed to utils.llm_config.call_llm.")
    parser.add_argument("--provider", default=None, help="Override provider passed to workflow.")
    parser.add_argument("--max-elements", type=int, default=180, help="Maximum summarized UI elements sent to the LLM.")
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum observe-plan-act iterations when --execute is enabled.")
    parser.add_argument("--max-actions-per-step", type=int, default=1, help="Maximum actions returned for one observation step.")
    parser.add_argument("--no-virtual-hints", action="store_true", help="Disable generated coordinate hints.")
    parser.add_argument("--uia-view", choices=["auto", "control", "raw", "content"], default="auto", help="UI Automation view to traverse. Auto probes RawView for sparse Electron/Chromium apps.")
    parser.add_argument("--debug-observation", action="store_true", help="Print summarized UI elements before planning.")
    parser.add_argument("--execute", action="store_true", help="Execute the planned actions.")
    parser.add_argument("--mock-plan", action="store_true", help="Skip the LLM call and use the deterministic fallback planner.")
    parser.add_argument("--no-fallback", action="store_true", help="Fail if the LLM call or plan parsing fails.")
    parser.add_argument("--plan-output", type=Path, default=None, help="Optional path to write the full run log JSON.")
    parser.add_argument("--traversal-output", type=Path, default=None, help="Optional path to write the latest raw traversal JSON.")
    args = parser.parse_args(argv)

    if args.list_apps:
        print(json.dumps([candidate.__dict__ for candidate in discover_apps()], ensure_ascii=False, indent=2))
        return 0
    if not args.instruction.strip():
        parser.error("instruction is required unless --list-apps is used")

    target_identifier, target_resolution = resolve_app_identifier(args.instruction, args.target)
    print(
        "target app: "
        f"{target_resolution.get('display_name', target_identifier)} "
        f"(matched: {target_resolution.get('matched_alias', target_resolution.get('input', ''))}, "
        f"identifier: {target_identifier})",
        file=sys.stderr,
    )
    open_result = open_or_activate_app(target_identifier)
    hwnd = int(open_result["hwnd"])

    run_log: dict[str, Any] = {
        "target": {"identifier": target_identifier, "hwnd": hwnd, "open": open_result, "resolution": target_resolution},
        "instruction": args.instruction,
        "execute": args.execute,
        "steps": [],
        "final_status": "running",
    }
    history: list[dict[str, Any]] = []
    max_steps = max(1, args.max_steps if args.execute else 1)

    for step_number in range(1, max_steps + 1):
        refreshed_hwnd, window_refresh = refresh_target_window(target_identifier, hwnd)
        if refreshed_hwnd != hwnd:
            print(f"target window changed: hwnd {hwnd} -> {refreshed_hwnd}", file=sys.stderr)
            hwnd = refreshed_hwnd
        traversal = traverse_app(hwnd, no_activate=True, view=args.uia_view)
        if args.traversal_output:
            write_json(args.traversal_output, traversal)
        elements, element_index = summarize_elements(
            traversal,
            max_elements=args.max_elements,
            include_virtual_hints=not args.no_virtual_hints,
        )
        if args.debug_observation:
            print_observation_debug(step_number, elements)
        plan = make_plan(
            args.instruction,
            target_identifier,
            traversal,
            elements,
            element_index,
            history,
            step_number=step_number,
            max_steps=max_steps,
            max_actions_per_step=args.max_actions_per_step,
            model=args.model,
            provider=args.provider,
            mock_plan=args.mock_plan,
            allow_fallback=(not args.no_fallback and (not args.execute or args.mock_plan)),
        )
        actions = validate_plan(plan, element_index, max_actions_per_step=args.max_actions_per_step)
        plan["actions"] = actions

        step_record: dict[str, Any] = {
            "step": step_number,
            "target": {"app": traversal.get("app_name", target_identifier), "hwnd": hwnd, "pid": traversal.get("pid")},
            "window_refresh": window_refresh,
            "element_count_sent_to_llm": len(elements),
            "traversal_stats": traversal.get("stats", {}),
            "uia_view": traversal.get("view"),
            "view_selection": traversal.get("view_selection"),
            "accessibility_hint": traversal.get("accessibility_hint"),
            "plan": plan,
        }
        if args.debug_observation:
            step_record["observation"] = elements
        run_log["steps"].append(step_record)

        status = str(plan.get("status", "continue")).lower()
        if not args.execute:
            run_log["final_status"] = "dry_run"
            break

        execution_results = execute_plan(actions, element_index, hwnd=hwnd)
        step_record["execution_results"] = execution_results
        history.append(
            {
                "step": step_number,
                "status": status,
                "summary": plan.get("summary"),
                "actions": actions,
                "execution_results": execution_results,
            }
        )
        if status in {"finished", "blocked"} or any(action.get("type") == "finish" for action in actions):
            run_log["final_status"] = status if status in {"finished", "blocked"} else "finished"
            break
        if args.plan_output:
            refresh_trace(run_log)
            write_json(args.plan_output, run_log)
    else:
        run_log["final_status"] = "max_steps_reached"

    refresh_trace(run_log)
    print(json.dumps(run_log, ensure_ascii=False, indent=2))
    if args.plan_output:
        write_json(args.plan_output, run_log)
    if not args.execute:
        print("dry-run only; pass --execute to operate the UI", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
