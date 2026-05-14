"""Fast, fixed-strategy Feishu/Lark commands for macOS."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from utils import tactile_trace


DEFAULT_TARGETS = ("com.electron.lark", "/Applications/Lark.app", "Lark", "Feishu", "飞书")

PRIMARY_SECTIONS = (
    "消息",
    "知识问答",
    "日历",
    "多维表格",
    "云文档",
    "视频会议",
    "飞行社",
    "工作台",
    "应用中心",
    "通讯录",
    "更多",
)

SECTION_ALIASES = {
    "message": "消息",
    "messages": "消息",
    "chat": "消息",
    "聊天": "消息",
    "消息": "消息",
    "qa": "知识问答",
    "知识问答": "知识问答",
    "calendar": "日历",
    "日历": "日历",
    "base": "多维表格",
    "bitable": "多维表格",
    "多维表格": "多维表格",
    "docs": "云文档",
    "doc": "云文档",
    "云文档": "云文档",
    "meeting": "视频会议",
    "meetings": "视频会议",
    "视频会议": "视频会议",
    "workplace": "工作台",
    "工作台": "工作台",
    "appcenter": "应用中心",
    "应用中心": "应用中心",
    "contacts": "通讯录",
    "通讯录": "通讯录",
    "more": "更多",
    "更多": "更多",
    "飞行社": "飞行社",
}

DEFAULT_WAIT_MS = 1000
DEFAULT_SWITCH_WAIT_MS = 1000
MIN_UI_ACTION_INTERVAL_SECONDS = 1.0
MIN_WAIT_SECONDS = 0.01


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).casefold()


def attach_trace(payload: dict[str, Any], *, command: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or "trace" in payload:
        return payload
    try:
        payload["trace"] = tactile_trace.build_fast_path_trace(
            payload,
            platform="macos",
            command=command,
            instruction=command,
        )
    except Exception as exc:
        payload["trace_error"] = str(exc)
    return payload


def wait_seconds(args: Any, *, default_ms: int = DEFAULT_WAIT_MS) -> float:
    wait_ms = getattr(args, "wait_ms", default_ms)
    return max(float(wait_ms) / 1000.0, MIN_UI_ACTION_INTERVAL_SECONDS)


def utf8_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("LANG", "en_US.UTF-8")
    env["LC_ALL"] = "en_US.UTF-8"
    return env


def text_of(element: dict[str, Any]) -> str:
    return str(element.get("text") or element.get("title") or element.get("description") or "")


def compact_element(element: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(element, dict):
        return None
    return {
        "role": element.get("role"),
        "text": text_of(element) or None,
        "frame": {
            "x": element.get("x"),
            "y": element.get("y"),
            "width": element.get("width"),
            "height": element.get("height"),
        },
        "axPath": element.get("axPath") or element.get("ax_path"),
    }


@dataclass
class FastContext:
    repo: Path
    ensure_products: Callable[[Path, list[str]], None]
    debug_tool: Callable[[Path, str], Path]
    product_cache: dict[str, str] = field(default_factory=dict)

    def product(self, name: str) -> str:
        if name not in self.product_cache:
            self.ensure_products(self.repo, [name])
            self.product_cache[name] = os.fspath(self.debug_tool(self.repo, name))
        return self.product_cache[name]

    def sleep_after_ui_action(self, delay: float | None) -> None:
        time.sleep(max(float(delay or 0), MIN_UI_ACTION_INTERVAL_SECONDS))

    def run(
        self,
        cmd: list[str],
        *,
        timeout: float = 10,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=utf8_env(),
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {detail[-1200:]}")
        return proc

    def open_app(self, target: str | None = None) -> tuple[int, dict[str, Any]]:
        opener = self.product("AppOpenerTool")
        candidates: list[str] = []
        if target:
            candidates.append(target)
        candidates.extend(candidate for candidate in DEFAULT_TARGETS if candidate not in candidates)
        errors: list[dict[str, str]] = []
        for candidate in candidates:
            proc = self.run([opener, candidate], timeout=30, check=False)
            if proc.returncode == 0:
                raw = proc.stdout.strip()
                try:
                    pid = int(raw)
                    self.sleep_after_ui_action(MIN_UI_ACTION_INTERVAL_SECONDS)
                    return pid, {"target": candidate, "pid": pid}
                except ValueError:
                    errors.append({"target": candidate, "error": f"unexpected pid output: {raw}"})
            else:
                errors.append({"target": candidate, "error": (proc.stderr or proc.stdout).strip()[-500:]})
        raise RuntimeError(f"could not open Feishu/Lark: {errors}")

    def traverse(self, pid: int) -> dict[str, Any]:
        import json

        traversal = self.product("TraversalTool")
        proc = self.run([traversal, "--visible-only", "--no-activate", str(pid)], timeout=20)
        return json.loads(proc.stdout)

    def input_tool(self) -> str:
        return self.product("InputControllerTool")

    def keypress(self, key: str, *, delay: float = 0.08) -> dict[str, Any]:
        proc = self.run([self.input_tool(), "keypress", key], timeout=10)
        self.sleep_after_ui_action(delay)
        return {"key": key, "stderr": proc.stderr[-500:]}

    def click_center(self, element: dict[str, Any], *, delay: float = 0.1) -> dict[str, Any]:
        x = float(element.get("x") or 0) + (float(element.get("width") or 0) / 2.0)
        y = float(element.get("y") or 0) + (float(element.get("height") or 0) / 2.0)
        proc = self.run([self.input_tool(), "click", f"{x:.1f}", f"{y:.1f}"], timeout=10)
        self.sleep_after_ui_action(delay)
        return {"action": "click", "element": compact_element(element), "point": {"x": x, "y": y}, "stderr": proc.stderr[-500:]}

    def ax_action(self, pid: int, element: dict[str, Any], *, delay: float = 0.1) -> dict[str, Any]:
        ax_path = element.get("axPath") or element.get("ax_path")
        if not ax_path:
            raise RuntimeError(f"element has no AX path: {compact_element(element)}")
        if os.getenv("TACTILE_VIRTUAL_CURSOR_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            self.product("VirtualCursorTool")
        errors: list[str] = []
        for action in ("axactivate", "axpress", "axselect", "axfocus"):
            proc = self.run([self.input_tool(), action, str(pid), str(ax_path)], timeout=10, check=False)
            self.sleep_after_ui_action(delay)
            if proc.returncode == 0:
                return {"action": action, "element": compact_element(element), "stderr": proc.stderr[-500:]}
            errors.append((proc.stderr or proc.stdout or "").strip()[-500:])
        raise RuntimeError(f"AX action failed for {compact_element(element)}: {errors}")

    def ax_focus(self, pid: int, element: dict[str, Any], *, delay: float = 0.06) -> dict[str, Any]:
        ax_path = element.get("axPath") or element.get("ax_path")
        if not ax_path:
            raise RuntimeError(f"element has no AX path: {compact_element(element)}")
        proc = self.run([self.input_tool(), "axfocus", str(pid), str(ax_path)], timeout=10)
        self.sleep_after_ui_action(delay)
        return {"action": "axfocus", "element": compact_element(element), "stderr": proc.stderr[-500:]}

    def paste_text(
        self,
        text: str,
        *,
        replace_existing: bool = True,
        restore_clipboard: bool = False,
        delay: float = 0.08,
    ) -> dict[str, Any]:
        previous: str | None = None
        if restore_clipboard:
            previous = self.run(["pbpaste"], timeout=5, check=False).stdout
        self.run(["pbcopy"], timeout=5, input_text=text)
        if replace_existing:
            self.keypress("cmd+a", delay=0.02)
        paste = self.keypress("cmd+v", delay=delay)
        if restore_clipboard and previous is not None:
            self.run(["pbcopy"], timeout=5, input_text=previous, check=False)
        return {
            "text_length": len(text),
            "replace_existing": replace_existing,
            "restore_clipboard": restore_clipboard,
            "paste": paste,
        }

    def copy_selected_text(self) -> str:
        self.keypress("cmd+c", delay=0.08)
        return self.run(["pbpaste"], timeout=5, check=False).stdout.strip()


def elements(traversal: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in traversal.get("elements") or [] if isinstance(item, dict)]


def find_text_element(
    traversal: dict[str, Any],
    query: str,
    *,
    roles: tuple[str, ...] = (),
    exact: bool = False,
) -> dict[str, Any] | None:
    query_norm = normalize_text(query)
    if not query_norm:
        return None
    candidates: list[dict[str, Any]] = []
    for element in elements(traversal):
        role = str(element.get("role") or "")
        if roles and not any(marker in role for marker in roles):
            continue
        text = text_of(element)
        text_norm = normalize_text(text)
        if not text_norm:
            continue
        if (exact and text_norm == query_norm) or (not exact and query_norm in text_norm):
            candidates.append(element)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (0 if normalize_text(text_of(item)) == query_norm else 1, len(text_of(item))))
    return candidates[0]


def find_compose_element(
    traversal: dict[str, Any],
    chat: str | None = None,
    *,
    require_chat: bool = False,
) -> dict[str, Any] | None:
    textareas = [
        item
        for item in elements(traversal)
        if "AXTextArea" in str(item.get("role") or "") or "AXTextField" in str(item.get("role") or "")
    ]
    if chat:
        chat_norm = normalize_text(chat)
        for item in textareas:
            if "发送给" in text_of(item) and chat_norm in normalize_text(text_of(item)):
                return item
        if require_chat:
            return None
    for item in textareas:
        if "发送给" in text_of(item):
            return item
    return textareas[0] if textareas else None


def find_text_input_containing(traversal: dict[str, Any], query: str) -> dict[str, Any] | None:
    query_norm = normalize_text(query)
    if not query_norm:
        return None
    for item in elements(traversal):
        role = str(item.get("role") or "")
        if ("AXTextArea" in role or "AXTextField" in role) and query_norm in normalize_text(text_of(item)):
            return item
    return None


def click_with_ax_or_coordinate(ctx: FastContext, pid: int, element: dict[str, Any], *, delay: float = 0.1) -> dict[str, Any]:
    try:
        result = ctx.ax_action(pid, element, delay=delay)
        result["mode"] = "direct_ax"
        return result
    except RuntimeError as exc:
        result = ctx.click_center(element, delay=delay)
        result["mode"] = "coordinate_fallback"
        result["fallback_error"] = str(exc)[-500:]
        return result


def find_cloud_doc_create_entry(traversal: dict[str, Any]) -> dict[str, Any] | None:
    for query in ("TitleBarMenu-CREATE_DOC", "创建文档", "新建文档"):
        target = find_text_element(traversal, query, roles=("AXButton", "AXMenuItem"), exact=False)
        if target is not None:
            return target
    return None


def find_cloud_doc_new_button(traversal: dict[str, Any]) -> dict[str, Any] | None:
    for query in ("新建", "创建"):
        target = find_text_element(traversal, query, roles=("AXButton",), exact=True)
        if target is not None:
            return target
    visible = elements(traversal)
    y_values = [float(item.get("y")) for item in visible if isinstance(item.get("y"), (int, float))]
    top_y = min(y_values) if y_values else None
    candidates: list[dict[str, Any]] = []
    for item in visible:
        role = str(item.get("role") or "")
        if "AXButton" not in role or text_of(item):
            continue
        try:
            x = float(item.get("x") or 0)
            y = float(item.get("y") or 0)
            width = float(item.get("width") or 0)
            height = float(item.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if not (16 <= width <= 48 and 16 <= height <= 48 and x >= 360):
            continue
        if top_y is not None and not (top_y <= y <= top_y + 180):
            continue
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            abs((float(item.get("x") or 0) + float(item.get("width") or 0) / 2.0) - 471.0),
            float(item.get("y") or 0),
        )
    )
    return candidates[0]


def fill_frontmost_browser_doc(ctx: FastContext, args: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    wait = wait_seconds(args)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    if title:
        steps.append(
            {
                "step": "paste_browser_title",
                "title_length": len(title),
                "result": ctx.paste_text(
                    title,
                    replace_existing=True,
                    restore_clipboard=getattr(args, "restore_clipboard", False),
                    delay=wait,
                ),
            }
        )
    if body:
        steps.append({"step": "focus_browser_body", "result": ctx.keypress("enter", delay=max(wait, 0.2))})
        steps.append(
            {
                "step": "paste_browser_body",
                "body_length": len(body),
                "result": ctx.paste_text(
                    body,
                    replace_existing=False,
                    restore_clipboard=getattr(args, "restore_clipboard", False),
                    delay=wait,
                ),
            }
        )
    return steps


def copy_frontmost_browser_url(ctx: FastContext, args: Any) -> tuple[str, list[dict[str, Any]]]:
    wait = wait_seconds(args)
    steps = [{"step": "focus_browser_address", "result": ctx.keypress("cmd+l", delay=wait)}]
    url = ctx.copy_selected_text()
    steps.append({"step": "copy_browser_url", "url": url})
    return url, steps


def open_search(ctx: FastContext, *, query: str | None, submit: bool, wait: float, restore_clipboard: bool) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    steps.append({"step": "open_global_search", "result": ctx.keypress("cmd+k", delay=wait)})
    if query is not None:
        steps.append(
            {
                "step": "paste_query",
                "query": query,
                "result": ctx.paste_text(query, replace_existing=True, restore_clipboard=restore_clipboard, delay=wait),
            }
        )
    if submit:
        steps.append({"step": "submit_search", "result": ctx.keypress("enter", delay=wait)})
    return steps


def open_chat(
    ctx: FastContext,
    args: Any,
    *,
    verify: bool | None = None,
    pid: int | None = None,
    opened: dict[str, Any] | None = None,
    initial_steps: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    if pid is None:
        pid, opened = ctx.open_app(getattr(args, "target", None))
    wait = wait_seconds(args)
    steps = list(initial_steps or [])
    if opened is not None:
        steps.append({"step": "open_app", "result": opened})
    steps.extend(open_search(ctx, query=args.chat, submit=True, wait=wait, restore_clipboard=getattr(args, "restore_clipboard", False)))
    verification: dict[str, Any] = {"mode": "skipped"}
    verify_requested = bool(getattr(args, "verify", False)) if verify is None else verify
    if verify_requested:
        traversal = ctx.traverse(pid)
        compose = find_compose_element(traversal, args.chat, require_chat=True)
        title = find_text_element(traversal, args.chat, roles=("AXStaticText", "AXButton", "AXTextArea"))
        verification = {
            "chat": args.chat,
            "confirmed": bool(compose or title),
            "compose": compact_element(compose),
            "title_or_text": compact_element(title),
        }
        if not verification["confirmed"]:
            return 1, {"status": "not_verified", "chat": args.chat, "pid": pid, "steps": steps, "verification": verification}
    return 0, {"status": "success", "chat": args.chat, "pid": pid, "steps": steps, "verification": verification}


def send_message(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    pid: int | None = None
    opened: dict[str, Any] | None = None
    initial_steps: list[dict[str, Any]] = []
    org = getattr(args, "org", None)
    if org:
        org_args = SimpleNamespace(
            target=getattr(args, "target", None),
            name=org,
            wait_ms=max(int(getattr(args, "wait_ms", DEFAULT_WAIT_MS)), DEFAULT_SWITCH_WAIT_MS),
            dry_run=False,
        )
        org_code, org_payload = switch_org(ctx, org_args)
        if org_code != 0:
            return org_code, {"status": "failed", "reason": "organization switch failed", "org": org, "switch": org_payload}
        pid = int(org_payload["pid"])
        opened = None
        initial_steps.append({"step": "switch_org", "result": org_payload})

    chat_args = SimpleNamespace(**vars(args))
    chat_args.verify = False
    open_code, open_payload = open_chat(
        ctx,
        chat_args,
        verify=False,
        pid=pid,
        opened=opened,
        initial_steps=initial_steps,
    )
    if open_code != 0:
        return open_code, open_payload
    pid = int(open_payload["pid"])
    wait = wait_seconds(args)
    steps = list(open_payload.get("steps") or [])
    traversal = ctx.traverse(pid)
    compose = find_compose_element(traversal, args.chat, require_chat=True)
    if compose is None:
        fallback_compose = find_compose_element(traversal, args.chat)
        title = find_text_element(traversal, args.chat, roles=("AXStaticText", "AXButton", "AXTextArea"))
        return 1, {
            "status": "not_verified",
            "reason": "could not confirm target chat compose input after opening chat",
            "chat": args.chat,
            "pid": pid,
            "steps": steps,
            "verification": {
                "chat": args.chat,
                "compose": compact_element(fallback_compose),
                "title_or_text": compact_element(title),
            },
        }
    steps.append({"step": "focus_compose", "result": ctx.ax_focus(pid, compose, delay=wait)})
    steps.append(
        {
            "step": "paste_message",
            "message_length": len(args.message),
            "result": ctx.paste_text(
                args.message,
                replace_existing=not getattr(args, "keep_existing_draft", False),
                restore_clipboard=getattr(args, "restore_clipboard", False),
                delay=wait,
            ),
        }
    )
    sent = bool(getattr(args, "send", False)) and not bool(getattr(args, "draft_only", False))
    if sent:
        steps.append({"step": "send", "key": args.send_key, "result": ctx.keypress(args.send_key, delay=wait)})
    verification: dict[str, Any] = {
        "mode": "target_only",
        "chat": args.chat,
        "target_confirmed": True,
        "target_compose": compact_element(compose),
    }
    if getattr(args, "verify", False):
        refreshed = ctx.traverse(pid)
        refreshed_compose = find_compose_element(refreshed, args.chat)
        message_input = find_text_input_containing(refreshed, args.message)
        refreshed_compose_text = text_of(refreshed_compose) if refreshed_compose else ""
        compose_text = text_of(message_input or refreshed_compose) if (message_input or refreshed_compose) else ""
        verification = {
            **verification,
            "mode": "verified",
            "chat": args.chat,
            "message_length": len(args.message),
            "sent_requested": sent,
            "compose": compact_element(message_input or refreshed_compose),
            "compose_has_message": bool(args.message and args.message in compose_text),
            "compose_cleared_after_send": bool(sent and args.message and args.message not in refreshed_compose_text),
        }
    return 0, {"status": "success", "chat": args.chat, "message_sent": sent, "pid": pid, "steps": steps, "verification": verification}


def open_section(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    section = SECTION_ALIASES.get(normalize_text(args.section), args.section)
    pid, opened = ctx.open_app(getattr(args, "target", None))
    traversal = ctx.traverse(pid)
    target = find_text_element(traversal, section, roles=("AXRadioButton", "AXButton"), exact=True)
    if target is None:
        target = find_text_element(traversal, section, roles=("AXRadioButton", "AXButton"), exact=False)
    if target is None:
        return 1, {"status": "not_found", "section": section, "available_sections": list(PRIMARY_SECTIONS), "pid": pid, "open_app": opened}
    if getattr(args, "dry_run", False):
        return 0, {"status": "dry_run", "section": section, "pid": pid, "target": compact_element(target)}
    action = ctx.ax_action(pid, target)
    return 0, {"status": "success", "section": section, "pid": pid, "open_app": opened, "action": action}


def switch_org(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    pid, opened = ctx.open_app(getattr(args, "target", None))
    steps: list[dict[str, Any]] = [{"step": "open_app", "result": opened}]
    traversal = ctx.traverse(pid)
    target = find_text_element(traversal, args.name, roles=("AXButton",), exact=False)
    if target is None:
        more = find_text_element(traversal, "更多账号", roles=("AXButton",), exact=False)
        if more is not None and not getattr(args, "dry_run", False):
            steps.append({"step": "open_more_accounts", "result": ctx.ax_action(pid, more, delay=max(wait_seconds(args, default_ms=DEFAULT_SWITCH_WAIT_MS), 0.06))})
            traversal = ctx.traverse(pid)
            target = find_text_element(traversal, args.name, roles=("AXButton", "AXStaticText"), exact=False)
    if target is None:
        return 1, {"status": "not_found", "org": args.name, "pid": pid, "steps": steps}
    if getattr(args, "dry_run", False):
        return 0, {"status": "dry_run", "org": args.name, "pid": pid, "target": compact_element(target), "steps": steps}
    steps.append({"step": "switch_org", "result": ctx.ax_action(pid, target, delay=max(wait_seconds(args, default_ms=DEFAULT_SWITCH_WAIT_MS), 0.06))})
    return 0, {"status": "success", "org": args.name, "pid": pid, "steps": steps}


def global_search(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    pid, opened = ctx.open_app(getattr(args, "target", None))
    wait = wait_seconds(args)
    steps = [{"step": "open_app", "result": opened}]
    steps.extend(open_search(ctx, query=args.query, submit=bool(getattr(args, "open", False)), wait=wait, restore_clipboard=getattr(args, "restore_clipboard", False)))
    return 0, {"status": "success", "query": args.query, "opened_result": bool(getattr(args, "open", False)), "pid": pid, "steps": steps}


def open_url(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    allowed = (
        args.url.startswith("lark://")
        or args.url.startswith("feishu://")
        or args.url.startswith("feishu-open://")
        or args.url.startswith("x-feishu://")
        or re.match(r"^https://[^/]*(feishu|larksuite)\.(cn|com)/", args.url) is not None
    )
    if not allowed:
        return 1, {"status": "rejected", "reason": "URL is not a recognized Feishu/Lark URL", "url": args.url}
    proc = subprocess.run(["open", args.url], text=True, capture_output=True, timeout=10)
    return (0 if proc.returncode == 0 else proc.returncode), {
        "status": "success" if proc.returncode == 0 else "failed",
        "url": args.url,
        "stderr": proc.stderr[-500:],
    }


def create_doc(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    org = getattr(args, "org", None)
    if org:
        org_args = SimpleNamespace(
            target=getattr(args, "target", None),
            name=org,
            wait_ms=max(int(getattr(args, "wait_ms", DEFAULT_WAIT_MS)), DEFAULT_SWITCH_WAIT_MS),
            dry_run=False,
        )
        org_code, org_payload = switch_org(ctx, org_args)
        steps.append({"step": "switch_org", "result": org_payload})
        if org_code != 0:
            return org_code, {"status": "failed", "reason": "organization switch failed", "org": org, "steps": steps}

    section_args = SimpleNamespace(target=getattr(args, "target", None), section="云文档", dry_run=False)
    section_code, section_payload = open_section(ctx, section_args)
    steps.append({"step": "open_cloud_docs", "result": section_payload})
    if section_code != 0:
        return section_code, {"status": "failed", "reason": "cloud docs section not opened", "steps": steps}

    pid = int(section_payload["pid"])
    wait = wait_seconds(args)
    if getattr(args, "dry_run", False):
        traversal = ctx.traverse(pid)
        return 0, {
            "status": "dry_run",
            "pid": pid,
            "new_button": compact_element(find_cloud_doc_new_button(traversal)),
            "create_entry": compact_element(find_cloud_doc_create_entry(traversal)),
            "steps": steps,
        }

    traversal = ctx.traverse(pid)
    new_button = find_cloud_doc_new_button(traversal)
    if new_button is None:
        return 1, {"status": "not_found", "reason": "could not find cloud docs new/create button", "pid": pid, "steps": steps}
    steps.append({"step": "open_create_menu", "result": click_with_ax_or_coordinate(ctx, pid, new_button, delay=wait)})

    traversal = ctx.traverse(pid)
    create_entry = find_cloud_doc_create_entry(traversal)
    if create_entry is None:
        return 1, {"status": "not_found", "reason": "could not find create document menu entry", "pid": pid, "steps": steps}
    steps.append({"step": "create_document", "result": click_with_ax_or_coordinate(ctx, pid, create_entry, delay=wait)})

    browser_wait = max(float(getattr(args, "browser_wait_ms", 2500)) / 1000.0, MIN_WAIT_SECONDS)
    time.sleep(browser_wait)
    steps.append({"step": "wait_for_default_browser", "seconds": browser_wait})
    steps.extend(fill_frontmost_browser_doc(ctx, args))

    autosave_wait = max(float(getattr(args, "autosave_wait_ms", 800)) / 1000.0, 0.0)
    if autosave_wait:
        time.sleep(autosave_wait)
        steps.append({"step": "wait_for_autosave", "seconds": autosave_wait})

    url = ""
    send_to = getattr(args, "send_to", None)
    if getattr(args, "copy_url", False) or send_to:
        url, url_steps = copy_frontmost_browser_url(ctx, args)
        steps.extend(url_steps)
        if send_to and not re.match(r"^https://[^/]*(feishu|larksuite)\.(cn|com)/", url):
            return 1, {
                "status": "not_verified",
                "reason": "copied browser URL does not look like a Feishu/Lark document URL",
                "url": url,
                "pid": pid,
                "steps": steps,
            }

    sent = False
    if send_to:
        prefix = getattr(args, "message_prefix", None) or ""
        message_parts = [part for part in (prefix, getattr(args, "title", None), url) if part]
        message = "\n".join(message_parts) if message_parts else url
        send_args = SimpleNamespace(
            target=getattr(args, "target", None),
            chat=send_to,
            message=message,
            org=org,
            send=bool(getattr(args, "send", False)),
            draft_only=bool(getattr(args, "draft_only", False)),
            send_key=getattr(args, "send_key", "enter"),
            wait_ms=getattr(args, "wait_ms", DEFAULT_WAIT_MS),
            verify=True,
            restore_clipboard=getattr(args, "restore_clipboard", False),
            keep_existing_draft=False,
        )
        send_code, send_payload = send_message(ctx, send_args)
        steps.append({"step": "send_document_link", "result": send_payload})
        if send_code != 0:
            return send_code, {"status": "failed", "reason": "document link send failed", "url": url, "pid": pid, "steps": steps}
        sent = bool(send_payload.get("message_sent"))

    return 0, {
        "status": "success",
        "pid": pid,
        "browser_handoff": True,
        "url": url or None,
        "sent": sent,
        "steps": steps,
    }


def list_buttons(ctx: FastContext, args: Any) -> tuple[int, dict[str, Any]]:
    pid, opened = ctx.open_app(getattr(args, "target", None))
    traversal = ctx.traverse(pid)
    seen: set[str] = set()
    controls: list[dict[str, Any]] = []
    for item in elements(traversal):
        role = str(item.get("role") or "")
        label = text_of(item)
        label_norm = normalize_text(label)
        if not label_norm or label_norm in seen:
            continue
        if "AXRadioButton" in role or "AXButton" in role:
            seen.add(label_norm)
            controls.append({"role": role, "text": label, "frame": compact_element(item)["frame"]})
    primary = [item for item in controls if item["text"] in PRIMARY_SECTIONS or item["text"] in {"搜索（⌘＋K）", "创建", "我的头像", "更多账号"}]
    primary_text = {entry["text"] for entry in primary}
    orgs = [item for item in controls if item["text"] not in primary_text and ("公司" in item["text"] or "团队" in item["text"] or "用户" in item["text"])]
    return 0, {"status": "success", "pid": pid, "open_app": opened, "primary_controls": primary, "organization_controls": orgs, "all_labeled_controls": controls[:120]}


def dispatch(
    args: Any,
    *,
    repo: Path,
    ensure_products: Callable[[Path, list[str]], None],
    debug_tool: Callable[[Path, str], Path],
    write_or_print: Callable[[Any, Path | None], None],
) -> int:
    ctx = FastContext(repo=repo, ensure_products=ensure_products, debug_tool=debug_tool)
    handlers = {
        "feishu-list-buttons": list_buttons,
        "feishu-open-section": open_section,
        "feishu-search": global_search,
        "feishu-open-app": global_search,
        "feishu-open-chat": open_chat,
        "feishu-send-message": send_message,
        "feishu-switch-org": switch_org,
        "feishu-open-url": open_url,
        "feishu-create-doc": create_doc,
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise SystemExit(f"unsupported Feishu fast command: {args.command}")
    try:
        code, payload = handler(ctx, args)
    except Exception as exc:
        code, payload = 1, {"status": "failed", "command": args.command, "error": str(exc)}
    payload = attach_trace(payload, command=args.command)
    write_or_print(payload, getattr(args, "output", None))
    return code
