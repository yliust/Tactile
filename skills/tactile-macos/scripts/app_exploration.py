"""Source/protocol-aware app exploration and adapter evaluation helpers."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPTS_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_ROOT.parent
APP_GUIDE_DIR = SKILL_ROOT / "references" / "app-guides"
if os.fspath(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SCRIPTS_ROOT))

from utils import tactile_trace

SCHEMA_VERSION = 1
MAX_RESOURCE_HINTS = 160
MAX_LOCALIZATION_SAMPLES = 80

STRATEGY_PRIORITIES: dict[str, tuple[str, ...]] = {
    "code-aware": (
        "public_interface",
        "dom_command",
        "app_fast_path",
        "fast_command",
        "existing_workflow",
        "workflow_command",
        "ax_action",
        "ocr_coordinate",
        "visual",
    ),
    "baseline": (
        "existing_workflow",
        "workflow_command",
        "ax_action",
        "ocr_coordinate",
        "visual",
    ),
    "ax": ("ax_action", "ocr_coordinate", "visual"),
    "visual": ("visual",),
}

SAFE_ACTUATOR_ORDER = (
    "public_interface",
    "dom_command",
    "app_fast_path",
    "fast_command",
    "existing_workflow",
    "workflow_command",
    "ax_action",
    "ocr_coordinate",
    "visual",
)


@dataclass(frozen=True)
class KnownApp:
    key: str
    display_name: str
    group: str
    match_terms: tuple[str, ...]
    default_target: str


KNOWN_APPS: tuple[KnownApp, ...] = (
    KnownApp(
        key="feishu",
        display_name="Feishu/Lark",
        group="electron-web",
        match_terms=("feishu", "飞书", "lark", "com.electron.lark"),
        default_target="com.electron.lark",
    ),
    KnownApp(
        key="wechat",
        display_name="WeChat",
        group="domestic-ax-ocr-stress",
        match_terms=("wechat", "微信", "weixin", "xinwechat", "com.tencent.xinwechat"),
        default_target="com.tencent.xinWeChat",
    ),
    KnownApp(
        key="tencent-meeting",
        display_name="Tencent Meeting",
        group="domestic-ax-ocr-stress",
        match_terms=("tencentmeeting", "tencent meeting", "腾讯会议", "voov", "wemeet"),
        default_target="TencentMeeting",
    ),
)


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return cleaned or "unknown"


def known_app_for_text(value: str) -> KnownApp | None:
    normalized = normalize_key(value)
    for app in KNOWN_APPS:
        if any(normalize_key(term) in normalized or normalized in normalize_key(term) for term in app.match_terms):
            return app
    return None


def read_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def read_strings_file(path: Path) -> dict[str, str]:
    data = read_plist(path)
    if data:
        return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}

    text: str | None = None
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeError:
            continue
        except OSError:
            return {}
    if text is None:
        return {}

    pattern = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;')
    return {
        key.replace(r"\"", '"'): value.replace(r"\"", '"')
        for key, value in pattern.findall(text)
    }


def app_bundle_info(app_path: Path) -> dict[str, Any]:
    info_path = app_path / "Contents" / "Info.plist"
    return read_plist(info_path)


def discover_app_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        proc = subprocess.run(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0:
            paths.extend(Path(line) for line in proc.stdout.splitlines() if line.endswith(".app"))
    except Exception:
        pass

    for root in (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"):
        if not root.exists():
            continue
        try:
            paths.extend(root.glob("*.app"))
            paths.extend(root.glob("*/*.app"))
        except OSError:
            continue
    return sorted({path.resolve() for path in paths if path.exists()})


def resolve_app_path(target: str) -> Path | None:
    expanded = Path(target).expanduser()
    if expanded.exists():
        return expanded.resolve()

    target_key = normalize_key(target)
    for path in discover_app_paths():
        info = app_bundle_info(path)
        candidates = [
            path.name,
            path.stem,
            str(info.get("CFBundleName") or ""),
            str(info.get("CFBundleDisplayName") or ""),
            str(info.get("CFBundleIdentifier") or ""),
        ]
        if any(target_key == normalize_key(candidate) or target_key in normalize_key(candidate) for candidate in candidates):
            return path
    return None


def url_schemes_from_info(info: dict[str, Any]) -> list[str]:
    schemes: list[str] = []
    for item in info.get("CFBundleURLTypes") or []:
        if not isinstance(item, dict):
            continue
        for scheme in item.get("CFBundleURLSchemes") or []:
            if isinstance(scheme, str) and scheme not in schemes:
                schemes.append(scheme)
    return schemes


def document_types_from_info(info: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in info.get("CFBundleDocumentTypes") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("CFBundleTypeName")
        if isinstance(name, str) and name not in result:
            result.append(name)
    return result


def detect_tech_stack(app_path: Path | None, info: dict[str, Any], target: str) -> list[str]:
    stack: list[str] = []
    if target.startswith(("http://", "https://")):
        stack.extend(["web", "dom"])
    if app_path is not None:
        stack.append("macos-app-bundle")
        resources = app_path / "Contents" / "Resources"
        frameworks = app_path / "Contents" / "Frameworks"
        if (resources / "app.asar").exists() or (resources / "app.asar.unpacked").exists():
            stack.extend(["electron", "asar"])
        if frameworks.exists() and any(path.name == "Electron Framework.framework" for path in frameworks.glob("*.framework")):
            if "electron" not in stack:
                stack.append("electron")
        if any(str(value).casefold().find("electron") >= 0 for value in info.values() if isinstance(value, str)):
            if "electron" not in stack:
                stack.append("electron")
    return stack or ["unknown"]


def collect_resource_hints(app_path: Path | None, *, max_items: int = MAX_RESOURCE_HINTS) -> list[dict[str, str]]:
    if app_path is None:
        return []
    roots = [app_path / "Contents" / "Resources", app_path / "Contents" / "Frameworks"]
    interesting = (
        "app.asar",
        "package.json",
        ".js",
        ".html",
        ".json",
        ".strings",
        ".framework",
        ".appex",
        ".xpc",
    )
    hints: list[dict[str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(dirnames)[:40]
            entries = [*(Path(current_root) / dirname for dirname in dirnames), *(Path(current_root) / filename for filename in filenames)]
            for path in sorted(entries, key=lambda item: str(item)):
                rel = str(path.relative_to(app_path))
                lower = rel.casefold()
                if any(marker in lower for marker in interesting):
                    hints.append({"path": rel, "kind": "directory" if path.is_dir() else "file"})
                    if len(hints) >= max_items:
                        return hints
    return hints


def collect_localization_samples(app_path: Path | None, *, max_items: int = MAX_LOCALIZATION_SAMPLES) -> list[dict[str, str]]:
    if app_path is None:
        return []
    resources = app_path / "Contents" / "Resources"
    samples: list[dict[str, str]] = []
    for strings_path in sorted(resources.glob("*.lproj/*.strings")):
        values = read_strings_file(strings_path)
        for key, value in sorted(values.items()):
            samples.append(
                {
                    "locale": strings_path.parent.name,
                    "file": strings_path.name,
                    "key": key,
                    "value": value,
                }
            )
            if len(samples) >= max_items:
                return samples
    return samples


def parse_markdown_list_after_heading(text: str, heading: str) -> tuple[str, ...]:
    lines = text.splitlines()
    in_section = False
    values: list[str] = []
    heading_marker = f"## {heading}".casefold()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if stripped.casefold() == heading_marker:
                in_section = True
                continue
            if in_section:
                break
        if in_section and stripped.startswith(("- ", "* ")):
            value = stripped[2:].strip().strip("`")
            if value:
                values.append(value)
    return tuple(values)


def parse_app_guides(guide_dir: Path = APP_GUIDE_DIR) -> list[dict[str, Any]]:
    guides: list[dict[str, Any]] = []
    if not guide_dir.exists():
        return guides
    for path in sorted(guide_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = path.stem
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        guides.append(
            {
                "title": title,
                "path": str(path),
                "match_terms": parse_markdown_list_after_heading(text, "Match Terms"),
            }
        )
    return guides


def matching_app_guide(app_key: str | None, target_text: str, guide_dir: Path = APP_GUIDE_DIR) -> dict[str, Any] | None:
    text_key = normalize_key(target_text)
    for guide in parse_app_guides(guide_dir):
        candidates = [guide["title"], Path(guide["path"]).stem, *guide.get("match_terms", ())]
        if app_key and normalize_key(app_key) in normalize_key(" ".join(candidates)):
            return guide
        if any(normalize_key(candidate) and normalize_key(candidate) in text_key for candidate in candidates):
            return guide
    return None


def profile_target(target: str, *, guide_dir: Path = APP_GUIDE_DIR) -> dict[str, Any]:
    app_path = None if target.startswith(("http://", "https://")) else resolve_app_path(target)
    info = app_bundle_info(app_path) if app_path is not None else {}
    identity_text = " ".join(
        str(value)
        for value in (
            target,
            info.get("CFBundleName"),
            info.get("CFBundleDisplayName"),
            info.get("CFBundleIdentifier"),
            app_path.name if app_path else "",
        )
        if value
    )
    known_app = known_app_for_text(identity_text)
    tech_stack = detect_tech_stack(app_path, info, target)
    if target.startswith(("http://", "https://")):
        group = "generic-web-control"
    elif known_app is not None:
        group = known_app.group
    elif "electron" in tech_stack:
        group = "electron-web"
    else:
        group = "unknown"

    bundle_name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or (app_path.stem if app_path else target)
    profile = {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "app_key": known_app.key if known_app else safe_id(str(bundle_name)).casefold(),
        "group": group,
        "identity": {
            "display_name": str(bundle_name),
            "bundle_id": info.get("CFBundleIdentifier"),
            "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion"),
            "path": str(app_path) if app_path is not None else None,
            "url": target if target.startswith(("http://", "https://")) else None,
            "executable": info.get("CFBundleExecutable"),
        },
        "detected_tech_stack": tech_stack,
        "public_interfaces": {
            "url_schemes": url_schemes_from_info(info),
            "document_types": document_types_from_info(info),
            "apple_script_enabled": bool(info.get("NSAppleScriptEnabled")),
            "protocol_handlers": url_schemes_from_info(info),
        },
        "bundle_probes": {
            "info_plist_keys": sorted(str(key) for key in info.keys()),
            "resource_hints": collect_resource_hints(app_path),
            "localization_samples": collect_localization_samples(app_path),
        },
        "app_guide": matching_app_guide(known_app.key if known_app else None, identity_text, guide_dir),
        "safety": {
            "static_only": True,
            "private_reverse_engineering": False,
            "database_writes_allowed": False,
        },
    }
    return profile


def synthetic_profile_for_known_app(known_app: KnownApp, *, guide_dir: Path = APP_GUIDE_DIR) -> dict[str, Any]:
    tech_stack = ["macos-app-bundle"]
    if known_app.group == "electron-web":
        tech_stack.extend(["electron", "webview"])
    return {
        "schema_version": SCHEMA_VERSION,
        "target": known_app.default_target,
        "app_key": known_app.key,
        "group": known_app.group,
        "identity": {
            "display_name": known_app.display_name,
            "bundle_id": known_app.default_target if "." in known_app.default_target else None,
            "version": None,
            "path": None,
            "url": None,
            "executable": None,
        },
        "detected_tech_stack": tech_stack,
        "public_interfaces": {
            "url_schemes": [],
            "document_types": [],
            "apple_script_enabled": False,
            "protocol_handlers": [],
        },
        "bundle_probes": {
            "info_plist_keys": [],
            "resource_hints": [],
            "localization_samples": [],
        },
        "app_guide": matching_app_guide(known_app.key, " ".join(known_app.match_terms), guide_dir),
        "safety": {
            "static_only": True,
            "private_reverse_engineering": False,
            "database_writes_allowed": False,
        },
    }


def verifier(expected_text: list[str], *, signals: tuple[str, ...] = ("AX", "OCR"), kind: str = "text_presence") -> dict[str, Any]:
    return {
        "kind": kind,
        "signals": list(signals),
        "expected_text": expected_text,
        "timeout_seconds": 3,
        "retry_limit": 1,
    }


def actuator(kind: str, description: str, **extra: Any) -> dict[str, Any]:
    payload = {"kind": kind, "description": description}
    payload.update(extra)
    return payload


def action_spec(
    action_id: str,
    intent: str,
    *,
    safety_level: str,
    preferred: dict[str, Any],
    fallbacks: list[dict[str, Any]],
    verify: dict[str, Any] | None,
    inputs: dict[str, Any] | None = None,
    experimental: bool = False,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "intent": intent,
        "safety_level": safety_level,
        "experimental": experimental or verify is None,
        "inputs": inputs or {},
        "preferred_actuator": preferred,
        "fallback_actuators": fallbacks,
        "verifier": verify,
    }


def feishu_actions() -> list[dict[str, Any]]:
    section_fallbacks = [
        actuator("ax_action", "Find the navigation AXButton by exact label and perform AXPress."),
        actuator("ocr_coordinate", "Find the visible section label with OCR and click its screenCenter."),
        actuator("visual", "Use one fresh visual click only after AX/OCR fail, then re-observe."),
    ]
    actions = [
        action_spec(
            "feishu.open_app",
            "Open or activate Feishu/Lark.",
            safety_level="safe",
            preferred=actuator("public_interface", "Open bundle com.electron.lark or /Applications/Lark.app."),
            fallbacks=[actuator("existing_workflow", "Use Tactile app opener resolution.")],
            verify=verifier(["消息", "日历", "云文档"], signals=("AX", "OCR")),
        )
    ]
    for action_id, label in (
        ("feishu.open_messages", "消息"),
        ("feishu.open_calendar", "日历"),
        ("feishu.open_docs", "云文档"),
        ("feishu.open_workplace", "工作台"),
    ):
        actions.append(
            action_spec(
                action_id,
                f"Open Feishu/Lark section {label}.",
                safety_level="safe",
                preferred=actuator("fast_command", "Use dedicated Feishu fast path.", command="feishu-open-section", argv=[label]),
                fallbacks=list(section_fallbacks),
                verify=verifier([label]),
            )
        )
    actions.extend(
        [
            action_spec(
                "feishu.search",
                "Open global search and draft a query.",
                safety_level="safe",
                preferred=actuator("fast_command", "Use Cmd+K fast search without opening result by default.", command="feishu-search", argv=["<query>"]),
                fallbacks=[
                    actuator("ax_action", "Focus the exposed search button or text field and paste the query."),
                    actuator("ocr_coordinate", "Locate 搜索 text and click its screenCenter."),
                    actuator("visual", "One visual click, then re-observe search field state."),
                ],
                verify=verifier(["<query>", "搜索"], signals=("AX", "OCR")),
                inputs={"query": "string"},
            ),
            action_spec(
                "feishu.switch_org",
                "Switch Feishu/Lark organization by visible account label.",
                safety_level="stateful",
                preferred=actuator("fast_command", "Use dedicated organization switch fast path.", command="feishu-switch-org", argv=["--name", "<org>"]),
                fallbacks=[
                    actuator("ax_action", "Use bottom account controls and transient popup AX elements."),
                    actuator("ocr_coordinate", "Use OCR only inside the account popup frame."),
                    actuator("visual", "One visual click in a freshly observed popup, then re-observe."),
                ],
                verify=verifier(["<org>"], signals=("AX", "OCR")),
                inputs={"org": "string"},
            ),
            action_spec(
                "feishu.open_chat_draft",
                "Open a chat and paste a draft without sending.",
                safety_level="draft_only",
                preferred=actuator("fast_command", "Use feishu-send-message without --send.", command="feishu-send-message", argv=["--chat", "<chat>", "--message", "<message>", "--draft-only", "--verify"]),
                fallbacks=[
                    actuator("ax_action", "Open chat via search, focus compose AXTextArea, paste draft."),
                    actuator("ocr_coordinate", "Use OCR to verify chat header and compose placeholder before paste."),
                    actuator("visual", "Only draft after visual verification of recipient and compose box."),
                ],
                verify=verifier(["<chat>", "<message>"], signals=("AX", "OCR")),
                inputs={"chat": "string", "message": "string"},
            ),
            action_spec(
                "feishu.create_doc_draft",
                "Create or open a cloud document draft without external sharing.",
                safety_level="draft_only",
                preferred=actuator("fast_command", "Use feishu-create-doc in dry-run/draft mode.", command="feishu-create-doc", argv=["--title", "<title>", "--body", "<body>", "--dry-run"]),
                fallbacks=[
                    actuator("workflow_command", "Use bounded AX-rich workflow to open create menu and stop before share/send."),
                    actuator("ax_action", "Use create button and verify browser document foreground."),
                    actuator("visual", "Use visual only for unlabeled create menu controls, then re-observe."),
                ],
                verify=verifier(["<title>"], signals=("AX", "OCR")),
                inputs={"title": "string", "body": "string"},
            ),
        ]
    )
    return actions


def wechat_actions() -> list[dict[str, Any]]:
    base_fallbacks = [
        actuator("ocr_coordinate", "Use OCR in the current WeChat profile region and click screenCenter."),
        actuator("visual", "Use one fresh visual click, then re-observe before the next action."),
    ]
    return [
        action_spec(
            "wechat.open_app",
            "Open or activate WeChat.",
            safety_level="safe",
            preferred=actuator("existing_workflow", "Resolve /Applications/WeChat.app or com.tencent.xinWeChat."),
            fallbacks=[actuator("visual", "Verify the WeChat main window visually only if AX/OCR are sparse.")],
            verify=verifier(["微信", "WeChat"], signals=("AX", "OCR", "visual")),
        ),
        action_spec(
            "wechat.search_contact",
            "Focus WeChat search and draft a contact query.",
            safety_level="safe",
            preferred=actuator("ax_action", "Focus AX search field when exposed, then paste the contact name."),
            fallbacks=list(base_fallbacks),
            verify=verifier(["<contact>"], signals=("OCR", "AX")),
            inputs={"contact": "string"},
        ),
        action_spec(
            "wechat.open_chat",
            "Open a visible matching contact or conversation.",
            safety_level="safe",
            preferred=actuator("ocr_coordinate", "Select a matching OCR row inside the search-result or chat-list region."),
            fallbacks=[
                actuator("ax_action", "Use a matching AX row only when the text and region match."),
                actuator("visual", "Use visual only after contact row and chat-list region are freshly verified."),
            ],
            verify=verifier(["<contact>"], signals=("OCR", "visual")),
            inputs={"contact": "string"},
        ),
        action_spec(
            "wechat.draft_message",
            "Paste a message draft into the selected chat without sending.",
            safety_level="draft_only",
            preferred=actuator("ocr_coordinate", "Focus compose box region after chat header verification and paste draft."),
            fallbacks=[
                actuator("ax_action", "Use exposed compose AXTextArea when available."),
                actuator("visual", "Use visual compose focus only after one fresh verification."),
            ],
            verify=verifier(["<contact>", "<message>"], signals=("OCR", "visual")),
            inputs={"contact": "string", "message": "string"},
        ),
        action_spec(
            "wechat.open_chat_info",
            "Open the chat information side panel.",
            safety_level="safe",
            preferred=actuator("ocr_coordinate", "Click the current chat info control after verifying the selected chat."),
            fallbacks=[actuator("visual", "Click the top-right info control once, then re-observe the panel.")],
            verify=verifier(["聊天信息", "<contact>"], signals=("OCR", "visual")),
            inputs={"contact": "string"},
        ),
        action_spec(
            "wechat.open_profile",
            "Open the selected contact profile page.",
            safety_level="safe",
            preferred=actuator("ocr_coordinate", "Use the chat info panel contact row or avatar region after verification."),
            fallbacks=[actuator("visual", "Use visual only in the current side-panel frame.")],
            verify=verifier(["<contact>", "微信号"], signals=("OCR", "visual")),
            inputs={"contact": "string"},
        ),
    ]


def tencent_meeting_actions() -> list[dict[str, Any]]:
    return [
        action_spec(
            "tencent-meeting.open_app",
            "Open or activate Tencent Meeting.",
            safety_level="safe",
            preferred=actuator("existing_workflow", "Resolve Tencent Meeting or VooV Meeting app bundle."),
            fallbacks=[actuator("visual", "Verify the Tencent Meeting home window visually if AX/OCR are sparse.")],
            verify=verifier(["腾讯会议", "Tencent Meeting"], signals=("AX", "OCR", "visual")),
        ),
        action_spec(
            "tencent-meeting.open_schedule",
            "Open the meeting schedule/list view.",
            safety_level="safe",
            preferred=actuator("ax_action", "Press the schedule/list control when exposed through AX."),
            fallbacks=[
                actuator("ocr_coordinate", "Find 日程/会议/预约 labels with OCR and click screenCenter."),
                actuator("visual", "Use one visual click, then re-observe the active window."),
            ],
            verify=verifier(["会议", "日程"], signals=("AX", "OCR")),
        ),
        action_spec(
            "tencent-meeting.open_schedule_dialog",
            "Open the schedule meeting dialog without submitting.",
            safety_level="draft_only",
            preferred=actuator("ax_action", "Press the 预约会议 control when exposed."),
            fallbacks=[
                actuator("ocr_coordinate", "Click the 预约会议 OCR line center in the current window."),
                actuator("visual", "Use one visual click and verify a dialog/window title change."),
            ],
            verify=verifier(["预约会议", "主题"], signals=("AX", "OCR")),
        ),
        action_spec(
            "tencent-meeting.draft_topic",
            "Fill a meeting topic draft without submitting.",
            safety_level="draft_only",
            preferred=actuator("ax_action", "Focus the topic text field and paste the title."),
            fallbacks=[
                actuator("ocr_coordinate", "Use OCR to locate 主题 field and paste after focusing it."),
                actuator("visual", "Only focus a visual field after fresh dialog verification."),
            ],
            verify=verifier(["<topic>"], signals=("AX", "OCR")),
            inputs={"topic": "string"},
        ),
        action_spec(
            "tencent-meeting.adjust_datetime_dry_run",
            "Adjust date/time controls and verify state without submitting.",
            safety_level="draft_only",
            preferred=actuator("ax_action", "Operate date/time picker controls through AX where possible."),
            fallbacks=[
                actuator("ocr_coordinate", "Use OCR within the active picker popup and re-observe after each change."),
                actuator("visual", "Use one visual click per freshly observed picker state."),
            ],
            verify=verifier(["<date>", "<time>"], signals=("AX", "OCR")),
            inputs={"date": "string", "time": "string"},
        ),
        action_spec(
            "tencent-meeting.view_meeting_info",
            "View or copy meeting information only when it is visible and verifiable.",
            safety_level="safe",
            preferred=actuator("ax_action", "Open visible meeting details through AX."),
            fallbacks=[
                actuator("ocr_coordinate", "Use OCR to choose a meeting row and verify details text."),
                actuator("visual", "Use visual only after row and popup frame are freshly observed."),
            ],
            verify=verifier(["会议号", "链接"], signals=("AX", "OCR")),
        ),
    ]


def generic_web_actions() -> list[dict[str, Any]]:
    return [
        action_spec(
            "web.inspect_routes",
            "Inspect discovered DOM routes/test ids without mutating the web app.",
            safety_level="safe",
            preferred=actuator("dom_command", "Use DOM/test-id/source metadata when available."),
            fallbacks=[actuator("ax_action", "Use browser AX tree."), actuator("visual", "Use screenshot only as final fallback.")],
            verify=verifier(["route", "test-id"], signals=("DOM", "AX")),
            experimental=True,
        ),
        action_spec(
            "web.fill_form_draft",
            "Fill a local form draft without submitting.",
            safety_level="draft_only",
            preferred=actuator("dom_command", "Use stable selectors or test ids."),
            fallbacks=[actuator("ax_action", "Use browser AX text fields."), actuator("ocr_coordinate", "Locate visible labels with OCR.")],
            verify=verifier(["<value>"], signals=("DOM", "AX", "OCR")),
            inputs={"field": "string", "value": "string"},
            experimental=True,
        ),
    ]


def actions_for_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    app_key = str(profile.get("app_key") or "")
    group = str(profile.get("group") or "")
    if app_key == "feishu":
        return feishu_actions()
    if app_key == "wechat":
        return wechat_actions()
    if app_key == "tencent-meeting":
        return tencent_meeting_actions()
    if group in {"electron-web", "generic-web-control"}:
        return generic_web_actions()
    return [
        action_spec(
            f"{safe_id(app_key or 'app')}.observe",
            "Observe the app through the existing Tactile AX/OCR ladder.",
            safety_level="safe",
            preferred=actuator("existing_workflow", "Use Tactile observe/workflow baseline."),
            fallbacks=[actuator("ax_action", "Use AX traversal."), actuator("ocr_coordinate", "Use OCR text fallback."), actuator("visual", "Use final visual fallback.")],
            verify=verifier(["window"], signals=("AX", "OCR", "visual")),
            experimental=True,
        )
    ]


def catalog_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    actions = actions_for_profile(profile)
    digest = json_digest(profile)
    return {
        "schema_version": SCHEMA_VERSION,
        "app": {
            "key": profile.get("app_key"),
            "display_name": (profile.get("identity") or {}).get("display_name"),
            "group": profile.get("group"),
            "profile_digest": digest,
        },
        "profile": profile,
        "actions": actions,
        "app_guide_metadata": guide_metadata_from_actions(profile, actions),
        "router": {
            "strategy_priorities": {key: list(value) for key, value in STRATEGY_PRIORITIES.items()},
            "visual_coordinate_policy": "single fresh visual action followed by re-observe; no repeated coordinate guesses",
        },
        "reliability_policy": {
            "required_verifier": True,
            "max_default_fallback_rate": 0.30,
            "no_irreversible_default_actions": True,
        },
    }


def guide_metadata_from_actions(profile: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "catalog_actions",
        "app": {
            "key": profile.get("app_key"),
            "display_name": (profile.get("identity") or {}).get("display_name"),
            "group": profile.get("group"),
        },
        "app_guide": profile.get("app_guide"),
        "intents": [
            {
                "id": action.get("id"),
                "intent": action.get("intent"),
                "safety_level": action.get("safety_level"),
                "experimental": action.get("experimental"),
                "inputs": action.get("inputs") or {},
                "verifier": action.get("verifier"),
                "preferred_actuator_kind": (action.get("preferred_actuator") or {}).get("kind")
                if isinstance(action.get("preferred_actuator"), dict)
                else None,
                "fallback_actuator_kinds": [
                    fallback.get("kind")
                    for fallback in action.get("fallback_actuators") or []
                    if isinstance(fallback, dict)
                ],
            }
            for action in actions
        ],
    }


def json_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:12]


def action_actuators(action: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    preferred = action.get("preferred_actuator")
    if isinstance(preferred, dict):
        result.append(preferred)
    for fallback in action.get("fallback_actuators") or []:
        if isinstance(fallback, dict):
            result.append(fallback)
    return result


def route_action(action: dict[str, Any], strategy: str) -> dict[str, Any]:
    if strategy not in STRATEGY_PRIORITIES:
        raise ValueError(f"unsupported strategy: {strategy}")
    actuators = action_actuators(action)
    priority = STRATEGY_PRIORITIES[strategy]
    if strategy == "code-aware" and actuators and actuators[0].get("kind") in priority:
        return {
            "strategy": strategy,
            "selected_actuator": actuators[0],
            "selected_index": 0,
            "fallback_count": 0,
            "available_actuators": actuators,
            "priority": list(priority),
        }
    for kind in priority:
        for index, item in enumerate(actuators):
            if item.get("kind") == kind:
                return {
                    "strategy": strategy,
                    "selected_actuator": item,
                    "selected_index": index,
                    "fallback_count": index,
                    "available_actuators": actuators,
                    "priority": list(priority),
                }
    return {
        "strategy": strategy,
        "selected_actuator": None,
        "selected_index": None,
        "fallback_count": len(actuators),
        "available_actuators": actuators,
        "priority": list(priority),
    }


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_for_app(app: str, catalog_path: Path | None = None) -> dict[str, Any]:
    if catalog_path is not None:
        return load_json_file(catalog_path)
    known = known_app_for_text(app)
    profile = synthetic_profile_for_known_app(known) if known is not None else profile_target(app)
    return catalog_from_profile(profile)


def find_action(catalog: dict[str, Any], task_id: str) -> dict[str, Any]:
    actions = [action for action in catalog.get("actions") or [] if isinstance(action, dict)]
    for action in actions:
        if action.get("id") == task_id:
            return action
    suffix_matches = [action for action in actions if str(action.get("id", "")).endswith(f".{task_id}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    raise KeyError(f"unknown task {task_id!r}; available tasks: {', '.join(str(action.get('id')) for action in actions)}")


def verifier_status(action: dict[str, Any], *, verify: bool) -> dict[str, Any]:
    verifier_spec = action.get("verifier")
    if not verify:
        return {"required": False, "covered": verifier_spec is not None, "status": "skipped"}
    if not isinstance(verifier_spec, dict):
        return {"required": True, "covered": False, "status": "missing"}
    signals = verifier_spec.get("signals") or []
    expected_text = verifier_spec.get("expected_text") or []
    covered = bool(signals and expected_text)
    return {
        "required": True,
        "covered": covered,
        "status": "planned" if covered else "incomplete",
        "spec": verifier_spec,
    }


def run_adapter(
    app: str,
    task_id: str,
    *,
    strategy: str,
    verify: bool = True,
    catalog_path: Path | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    catalog = catalog_for_app(app, catalog_path)
    action = find_action(catalog, task_id)
    route = route_action(action, strategy)
    verification = verifier_status(action, verify=verify)
    selected = route["selected_actuator"]
    success = selected is not None and (not verify or verification["covered"])
    if selected is None:
        error_category = "no_supported_actuator"
    elif verify and not verification["covered"]:
        error_category = "missing_verifier"
    else:
        error_category = None

    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "app": (catalog.get("app") or {}).get("key") or app,
        "task": action.get("id"),
        "strategy": strategy,
        "success": success,
        "duration_seconds": round(time.monotonic() - started, 6),
        "steps": [
            {
                "type": "route",
                "actuator": selected,
                "fallback_count": route["fallback_count"],
                "inputs": inputs or {},
                "note": "No UI mutation is performed in dry-run mode.",
            }
        ],
        "verification": verification,
        "fallback_count": route["fallback_count"],
        "llm_calls": 0,
        "ocr_calls": 0,
        "screenshot_calls": 0,
        "retry_count": 0,
        "error_category": error_category,
        "action": action,
        "route": route,
    }
    result["trace"] = adapter_trace(result, action=action, route=route, verification=verification)
    return result


def adapter_trace(
    result: dict[str, Any],
    *,
    action: dict[str, Any],
    route: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    selected = route.get("selected_actuator") if isinstance(route.get("selected_actuator"), dict) else {}
    actuator_kind = selected.get("kind") if isinstance(selected, dict) else None
    run_log = {
        "target": {
            "app": result.get("app"),
            "task": result.get("task"),
            "strategy": result.get("strategy"),
        },
        "instruction": str(result.get("task") or action.get("intent") or ""),
        "task_source": "adapter_dry_run",
        "final_status": "finished" if result.get("success") else "blocked",
        "steps": [
            {
                "step": 1,
                "target": {
                    "app": result.get("app"),
                    "task": result.get("task"),
                    "strategy": result.get("strategy"),
                },
                "plan": {
                    "status": "planned",
                    "summary": action.get("intent"),
                    "actions": [
                        {
                            "type": "route",
                            "source": actuator_kind,
                            "actuator_kind": actuator_kind,
                        }
                    ],
                },
                "execution_results": [
                    {
                        "index": 1,
                        "action": {
                            "type": "route",
                            "source": actuator_kind,
                            "actuator_kind": actuator_kind,
                        },
                        "ok": result.get("success"),
                        "mode": actuator_kind,
                        "fallback_from": "preferred_actuator" if int(result.get("fallback_count") or 0) > 0 else None,
                        "fallback_reason": f"selected fallback index {result.get('fallback_count')}"
                        if int(result.get("fallback_count") or 0) > 0
                        else None,
                    }
                ],
                "verification": verification,
            }
        ],
    }
    if result.get("error_category"):
        run_log["reason"] = result.get("error_category")
    return tactile_trace.build_trace(run_log, platform="macos")


def parse_scalar(value: str) -> Any:
    cleaned = value.strip()
    if cleaned in {"", "null", "Null", "NULL", "~"}:
        return None
    if cleaned in {"true", "True", "TRUE"}:
        return True
    if cleaned in {"false", "False", "FALSE"}:
        return False
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        return cleaned[1:-1]
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        return cleaned


def load_eval_suite(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    suite: dict[str, Any] = {}
    current_list: list[dict[str, Any]] | None = None
    current_item: dict[str, Any] | None = None
    for raw_line in raw.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            key = stripped[:-1].strip()
            current_list = []
            suite[key] = current_list
            current_item = None
            continue
        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            suite[key.strip()] = parse_scalar(value)
            current_list = None
            current_item = None
            continue
        if current_list is not None and stripped.startswith("- "):
            current_item = {}
            current_list.append(current_item)
            rest = stripped[2:].strip()
            if rest and ":" in rest:
                key, value = rest.split(":", 1)
                current_item[key.strip()] = parse_scalar(value)
            continue
        if current_item is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current_item[key.strip()] = parse_scalar(value)
            continue
    return suite


def app_from_task_id(task_id: str) -> str:
    if "." in task_id:
        return task_id.split(".", 1)[0]
    return task_id


def eval_suite(path: Path, *, strategy: str, runs: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    suite = load_eval_suite(path)
    tasks = suite.get("tasks") or []
    if not isinstance(tasks, list):
        raise ValueError("eval suite must contain a tasks list")
    results: list[dict[str, Any]] = []
    for run_index in range(max(1, runs)):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task") or task.get("id") or "")
            if not task_id:
                continue
            app = str(task.get("app") or app_from_task_id(task_id))
            inputs = task.get("inputs") if isinstance(task.get("inputs"), dict) else {}
            result = run_adapter(app, task_id, strategy=strategy, verify=True, inputs=inputs)
            result["suite"] = suite.get("name") or path.stem
            result["run_index"] = run_index
            results.append(result)
    return results, summarize_eval_runs(results)


def summarize_eval_runs(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {
            "total_runs": 0,
            "task_success_rate": 0.0,
            "verification_coverage": 0.0,
            "fallback_rate": 0.0,
        }
    successes = sum(1 for result in results if result.get("success") is True)
    covered = sum(1 for result in results if (result.get("verification") or {}).get("covered") is True)
    fallback_runs = sum(1 for result in results if int(result.get("fallback_count") or 0) > 0)
    misoperations = sum(1 for result in results if result.get("error_category") == "misoperation")
    false_positives = sum(1 for result in results if result.get("error_category") == "false_positive")
    durations = [float(result.get("duration_seconds") or 0) for result in results]
    return {
        "total_runs": total,
        "task_success_rate": successes / total,
        "verification_coverage": covered / total,
        "fallback_rate": fallback_runs / total,
        "misoperation_rate": misoperations / total,
        "false_positive_rate": false_positives / total,
        "mean_duration_seconds": sum(durations) / total,
        "llm_calls": sum(int(result.get("llm_calls") or 0) for result in results),
        "ocr_calls": sum(int(result.get("ocr_calls") or 0) for result in results),
        "screenshot_calls": sum(int(result.get("screenshot_calls") or 0) for result in results),
        "retry_count": sum(int(result.get("retry_count") or 0) for result in results),
        "by_task": summarize_by_key(results, "task"),
        "by_app": summarize_by_key(results, "app"),
    }


def summarize_by_key(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get(key) or "unknown"), []).append(result)
    summary: dict[str, Any] = {}
    for value, items in grouped.items():
        total = len(items)
        summary[value] = {
            "runs": total,
            "success_rate": sum(1 for item in items if item.get("success") is True) / total,
            "verification_coverage": sum(1 for item in items if (item.get("verification") or {}).get("covered") is True) / total,
            "fallback_rate": sum(1 for item in items if int(item.get("fallback_count") or 0) > 0) / total,
        }
    return summary
