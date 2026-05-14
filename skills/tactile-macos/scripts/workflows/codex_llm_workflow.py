#!/usr/bin/env python3
"""
LLM-driven workflow for controlling a macOS app through accessibility traversal.

Pipeline:
1. Open or activate a target app.
2. Traverse the app's accessibility elements with TraversalTool.
3. Ask an LLM to choose the next small action plan from the current UI state.
4. Optionally execute that plan with InputControllerTool.
5. Re-observe and continue until the LLM returns finish or the step limit is reached.

The LLM call intentionally reuses scripts/utils/llm_config.py from this skill.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORKFLOW_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = WORKFLOW_DIR.parent
WORKFLOW_SKILL_ROOT = SCRIPTS_ROOT.parent
SWIFT_PACKAGE_ROOT = Path(os.getenv("TACTILE_MACOS_SWIFT_PACKAGE", os.fspath(SCRIPTS_ROOT / "MacosUseSDK"))).expanduser().resolve()
REPO_ROOT = SWIFT_PACKAGE_ROOT
if os.fspath(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SCRIPTS_ROOT))

from utils import artifacts as artifact_utils
from utils import tactile_trace

APP_GUIDE_DIR = WORKFLOW_SKILL_ROOT / "references" / "app-guides"
DEBUG_DIR = SWIFT_PACKAGE_ROOT / ".build" / "debug"
CORE_TOOL_DIR_ENV = "TACTILE_MACOS_TOOL_DIR"
DEBUG_AX_GRID_ENV = "TACTILE_DEBUG_AX_GRID"
DEBUG_AX_GRID_DURATION_ENV = "TACTILE_DEBUG_AX_GRID_DURATION"
DEFAULT_DEBUG_AX_GRID_DURATION = 1.5
REQUIRED_PRODUCTS = ("AppOpenerTool", "TraversalTool", "InputControllerTool")
MENU_ROLE_PREFIXES = ("AXMenuBar", "AXMenuItem")
MENU_ROLES = {"AXMenu"}
WORKFLOW_MODES = ("auto", "ax-rich", "ax-poor")
VISUAL_PLANNING_MODES = ("auto", "off", "on")
CAPABILITY_SELECTION_MODES = ("auto", "profile", "llm")
ALLOWED_ACTION_TYPES = {
    "click",
    "doubleclick",
    "rightclick",
    "mousemove",
    "scroll",
    "writetext",
    "keypress",
    "wait",
    "finish",
}
ARTIFACT_SUBDIR = artifact_utils.ARTIFACT_SUBDIR
default_artifact_path = artifact_utils.default_artifact_path
find_workspace_root = artifact_utils.find_workspace_root
is_temporary_path = artifact_utils.is_temporary_path
safe_path_component = artifact_utils.safe_path_component
session_artifact_dir = artifact_utils.session_artifact_dir
session_scoped_output_path = artifact_utils.session_scoped_output_path
tempfile = artifact_utils.tempfile
UTF8_CLIPBOARD_COMMAND_PREFIX = ("env", "LC_ALL=en_US.UTF-8", "LANG=en_US.UTF-8")

OCR_SWIFT_SOURCE = r"""
import Foundation
import Vision
import AppKit

struct OCRFrame: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let frame: OCRFrame
}

struct OCRPayload: Codable {
    let image: String
    let imageWidth: Int
    let imageHeight: Int
    let languages: [String]
    let recognitionLevel: String
    let lines: [OCRLine]
}

let args = CommandLine.arguments
if args.count < 4 {
    fputs("usage: swift - <image-path> <comma-languages> <accurate|fast>\n", stderr)
    exit(2)
}

let imagePath = args[1]
let languages = args[2].split(separator: ",").map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
let recognitionLevel = args[3]
let imageURL = URL(fileURLWithPath: imagePath)

guard let image = NSImage(contentsOf: imageURL),
      let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cgImage = bitmap.cgImage else {
    fputs("failed to load image: \(imagePath)\n", stderr)
    exit(2)
}

let imageWidth = CGFloat(cgImage.width)
let imageHeight = CGFloat(cgImage.height)
var lines: [OCRLine] = []
var requestError: Error?

let request = VNRecognizeTextRequest { request, error in
    requestError = error
    let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let frame = OCRFrame(
            x: Double(box.minX * imageWidth),
            y: Double((1.0 - box.maxY) * imageHeight),
            width: Double(box.width * imageWidth),
            height: Double(box.height * imageHeight)
        )
        lines.append(OCRLine(text: candidate.string, confidence: candidate.confidence, frame: frame))
    }
}

request.recognitionLevel = recognitionLevel == "fast" ? .fast : .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = languages
if #available(macOS 13.0, *) {
    request.revision = VNRecognizeTextRequestRevision3
}

do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
} catch {
    fputs("OCR failed: \(error)\n", stderr)
    exit(1)
}

if let requestError = requestError {
    fputs("OCR request failed: \(requestError)\n", stderr)
    exit(1)
}

lines.sort {
    if abs($0.frame.y - $1.frame.y) > 4 {
        return $0.frame.y < $1.frame.y
    }
    return $0.frame.x < $1.frame.x
}

let payload = OCRPayload(
    image: imagePath,
    imageWidth: Int(imageWidth),
    imageHeight: Int(imageHeight),
    languages: languages,
    recognitionLevel: recognitionLevel,
    lines: lines
)
let data = try JSONEncoder().encode(payload)
print(String(data: data, encoding: .utf8)!)
"""


@dataclass(frozen=True)
class UiElement:
    element_id: str
    role: str
    text: str | None
    x: float | None
    y: float | None
    width: float | None
    height: float | None
    ax_path: str | None = None
    source: str = "ax"
    ocr_confidence: float | None = None

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
    path: str | None = None
    bundle_id: str | None = None
    source: str = "filesystem"


@dataclass(frozen=True)
class ProfileRegionSpec:
    name: str
    text: str
    x: str
    y: str
    width: str
    height: str


@dataclass(frozen=True)
class AppProfile:
    name: str
    workflow_mode: str
    match_terms: tuple[str, ...]
    guidance: str = ""
    visual_planning: bool = False
    fixed_strategy: bool = True
    guide_path: str | None = None
    profile_regions: tuple[ProfileRegionSpec, ...] = ()


APP_PROFILES: tuple[AppProfile, ...] = (
    AppProfile(
        name="ax-rich-default",
        workflow_mode="ax-rich",
        match_terms=("slack", "chrome", "safari", "edge", "firefox", "browser"),
    ),
)

_APP_GUIDE_PROFILE_CACHE: dict[Path, tuple[AppProfile, ...]] = {}
_APP_GUIDE_WARNING_CACHE: dict[Path, tuple[str, ...]] = {}


def markdown_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip().casefold()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def markdown_cell(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`") and len(value) >= 2:
        value = value[1:-1]
    return value.strip()


def parse_markdown_table(section: str) -> list[dict[str, str]]:
    table_lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]
    if len(table_lines) < 2:
        return []
    headers = [markdown_cell(cell).casefold().replace(" ", "_") for cell in table_lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for raw_line in table_lines[1:]:
        cells = [markdown_cell(cell) for cell in raw_line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells):
            continue
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        rows.append({headers[i]: cells[i] for i in range(len(headers))})
    return rows


def parse_markdown_list(section: str) -> tuple[str, ...]:
    values: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("- ", "* ")):
            continue
        value = markdown_cell(stripped[2:].strip())
        if value:
            values.append(value)
    return tuple(values)


def parse_profile_table(section: str, *, fallback_name: str) -> dict[str, str]:
    rows = parse_markdown_table(section)
    if not rows:
        raise ValueError("missing Profile markdown table")
    first = rows[0]
    if {"name", "workflow_mode"}.issubset(first.keys()):
        return first
    profile: dict[str, str] = {}
    for row in rows:
        key = row.get("key") or row.get("field") or row.get("name")
        value = row.get("value") or row.get("setting") or row.get("workflow_mode")
        if key and value:
            profile[key.casefold().replace(" ", "_")] = value
    if not profile:
        raise ValueError("Profile table must contain either profile columns or key/value rows")
    profile.setdefault("name", fallback_name)
    return profile


def parse_markdown_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return default


def parse_profile_region_specs(section: str) -> tuple[ProfileRegionSpec, ...]:
    specs: list[ProfileRegionSpec] = []
    for row in parse_markdown_table(section):
        name = row.get("id") or row.get("name")
        text = row.get("description") or row.get("text")
        x = row.get("x")
        y = row.get("y")
        width = row.get("width")
        height = row.get("height")
        if not all([name, text, x, y, width, height]):
            continue
        specs.append(
            ProfileRegionSpec(
                name=str(name),
                text=str(text),
                x=str(x),
                y=str(y),
                width=str(width),
                height=str(height),
            )
        )
    return tuple(specs)


def parse_app_guide(path: Path) -> AppProfile:
    raw = path.read_text(encoding="utf-8")
    title = markdown_title(raw, path.stem)
    sections = markdown_sections(raw)
    profile_values = parse_profile_table(sections.get("profile", ""), fallback_name=safe_path_component(path.stem).casefold())
    name = profile_values.get("name", safe_path_component(path.stem).casefold()).strip()
    workflow_mode = profile_values.get("workflow_mode", "ax-rich").strip()
    if workflow_mode not in {"ax-rich", "ax-poor"}:
        workflow_mode = "ax-rich"
    match_terms = parse_markdown_list(sections.get("match terms", ""))
    if not match_terms:
        match_terms = (title, name)
    planner_guidance = sections.get("planner guidance", "").strip()
    pitfalls = sections.get("pitfalls", "").strip()
    guidance_parts = [f"App guide: {title} ({path.name})"]
    if planner_guidance:
        guidance_parts.append("Planner Guidance:\n" + planner_guidance)
    if pitfalls:
        guidance_parts.append("Pitfalls:\n" + pitfalls)
    guidance = "\n\n".join(guidance_parts).strip() + "\n\n"
    return AppProfile(
        name=name,
        workflow_mode=workflow_mode,
        match_terms=tuple(match_terms),
        guidance=guidance,
        visual_planning=parse_markdown_bool(profile_values.get("visual_planning"), False),
        fixed_strategy=parse_markdown_bool(profile_values.get("fixed_strategy"), True),
        guide_path=os.fspath(path),
        profile_regions=parse_profile_region_specs(sections.get("profile regions", "")),
    )


def load_app_guide_profiles(guide_dir: Path | None = None) -> tuple[AppProfile, ...]:
    resolved_dir = (guide_dir or APP_GUIDE_DIR).expanduser().resolve()
    if resolved_dir in _APP_GUIDE_PROFILE_CACHE:
        return _APP_GUIDE_PROFILE_CACHE[resolved_dir]
    profiles: list[AppProfile] = []
    warnings: list[str] = []
    if not resolved_dir.is_dir():
        warnings.append(f"app guide directory not found: {resolved_dir}")
    else:
        for path in sorted(resolved_dir.glob("*.md")):
            try:
                profiles.append(parse_app_guide(path))
            except Exception as exc:
                warnings.append(f"failed to parse app guide {path}: {exc}")
    _APP_GUIDE_PROFILE_CACHE[resolved_dir] = tuple(profiles)
    _APP_GUIDE_WARNING_CACHE[resolved_dir] = tuple(warnings)
    return tuple(profiles)


def app_guide_warnings(guide_dir: Path | None = None) -> tuple[str, ...]:
    resolved_dir = (guide_dir or APP_GUIDE_DIR).expanduser().resolve()
    if resolved_dir not in _APP_GUIDE_WARNING_CACHE:
        load_app_guide_profiles(resolved_dir)
    return _APP_GUIDE_WARNING_CACHE.get(resolved_dir, ())


def available_app_profiles(guide_dir: Path | None = None) -> tuple[AppProfile, ...]:
    return (*load_app_guide_profiles(guide_dir), *APP_PROFILES)


def workflow_run_artifact_dir(plan_output: Path | None, *, cwd: Path | None = None) -> Path:
    if plan_output is not None:
        base_dir = plan_output.parent
        run_name = safe_path_component(plan_output.stem)
    else:
        base_dir = session_artifact_dir(cwd=cwd)
        run_name = safe_path_component(f"workflow-run-{int(time.time() * 1000)}")
    artifact_dir = base_dir / run_name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        timeout=timeout,
    )


def clipboard_command(command: str) -> list[str]:
    return [*UTF8_CLIPBOARD_COMMAND_PREFIX, command]


def ensure_tools() -> None:
    for product in REQUIRED_PRODUCTS:
        ensure_product(product)


def ensure_product(product: str) -> None:
    if product_is_current(product):
        return
    print(f"building Swift product: {product}", file=sys.stderr)
    try:
        run_command(["swift", "build", "--product", product], timeout=120)
    except subprocess.CalledProcessError as exc:
        output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
        if "no_warn_duplicate_libraries" not in output:
            raise
        print(
            "warning: default Swift toolchain emitted an unsupported linker flag; retrying with Xcode default toolchain",
            file=sys.stderr,
        )
        env = dict(os.environ)
        env["TOOLCHAINS"] = "com.apple.dt.toolchain.XcodeDefault"
        subprocess.run(
            [
                "xcrun",
                "swift",
                "build",
                "--scratch-path",
                os.fspath(fallback_scratch_path()),
                "--product",
                product,
            ],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
            env=env,
        )


def find_tool_path(name: str) -> Path | None:
    core_tool_dir = os.getenv(CORE_TOOL_DIR_ENV)
    if core_tool_dir:
        core_path = Path(core_tool_dir).expanduser() / name
        if core_path.exists():
            return core_path

    path = DEBUG_DIR / name
    fallback_path = fallback_scratch_path() / "debug" / name
    existing = [candidate for candidate in (path, fallback_path) if candidate.exists()]
    if not existing:
        return None
    return max(existing, key=lambda candidate: candidate.stat().st_mtime)


def fallback_scratch_path() -> Path:
    return Path("/tmp") / f"tactile-macos-swift-{safe_path_component(os.fspath(SWIFT_PACKAGE_ROOT))}"


def latest_swift_source_mtime() -> float:
    candidates = [SWIFT_PACKAGE_ROOT / "Package.swift", *(SWIFT_PACKAGE_ROOT / "Sources").rglob("*.swift")]
    return max((path.stat().st_mtime for path in candidates if path.exists()), default=0.0)


def product_is_current(product: str) -> bool:
    path = find_tool_path(product)
    return path is not None and path.stat().st_mtime >= latest_swift_source_mtime()


def tool_path(name: str) -> str:
    path = find_tool_path(name)
    if path is None:
        raise FileNotFoundError(f"missing tool: {DEBUG_DIR / name}")
    return os.fspath(path)


def env_flag_enabled(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_debug_ax_grid_duration(value: float | None) -> float:
    if value is not None and value > 0:
        return float(value)
    raw = os.getenv(DEBUG_AX_GRID_DURATION_ENV)
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return DEFAULT_DEBUG_AX_GRID_DURATION


def launch_debug_ax_grid(
    pid: int,
    duration: float,
    *,
    label: str | None = None,
    traversal: dict[str, Any] | None = None,
    artifact_dir: Path | None = None,
) -> bool:
    try:
        ensure_product("HighlightTraversalTool")
        cmd = [tool_path("HighlightTraversalTool")]
        if traversal is not None:
            output_dir = artifact_dir or session_artifact_dir(cwd=Path.cwd())
            output_dir.mkdir(parents=True, exist_ok=True)
            safe_label = safe_path_component(label or f"pid-{pid}")
            input_json = output_dir / f"{safe_label}-debug-ax-grid-traversal.json"
            input_json.write_text(json.dumps(traversal, ensure_ascii=False), encoding="utf-8")
            cmd.extend(["--input-json", os.fspath(input_json)])
        else:
            cmd.extend([str(pid), "--no-activate"])
        cmd.extend(["--duration", str(duration)])
        subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        suffix = f" for {label}" if label else ""
        source = "current traversal" if traversal is not None else "pid traversal"
        print(f"debug: AX grid overlay launched{suffix} on pid {pid} from {source} ({duration}s)", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"warning: failed to launch AX grid overlay for pid {pid}: {exc}", file=sys.stderr)
        return False


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


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


def read_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
        return data if isinstance(data, dict) else {}
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

    result: dict[str, str] = {}
    pattern = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;')
    for key, value in pattern.findall(text):
        result[key.replace(r"\"", '"')] = value.replace(r"\"", '"')
    return result


def discover_app_paths() -> list[Path]:
    paths: list[Path] = []

    try:
        proc = run_command(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            check=False,
            timeout=20,
        )
        if proc.returncode == 0:
            paths.extend(Path(line) for line in proc.stdout.splitlines() if line.endswith(".app"))
    except Exception:
        pass

    roots = [
        Path("/Applications"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path.home() / "Applications",
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            paths.extend(root.glob("*.app"))
            paths.extend(root.glob("*/*.app"))
        except OSError:
            continue

    by_path: dict[str, Path] = {}
    for path in paths:
        try:
            if path.exists() and path.suffix == ".app":
                by_path[str(path.resolve())] = path
        except OSError:
            continue
    return sorted(by_path.values(), key=lambda item: str(item).casefold())


def localized_bundle_names(app_path: Path) -> list[str]:
    names: list[str] = []
    resources = app_path / "Contents" / "Resources"
    if not resources.exists():
        return names
    for strings_path in resources.glob("*.lproj/InfoPlist.strings"):
        strings = read_strings_file(strings_path)
        for key in ("CFBundleDisplayName", "CFBundleName"):
            value = strings.get(key)
            if isinstance(value, str):
                names.append(value)
    return names


def app_candidate_from_path(app_path: Path) -> AppCandidate | None:
    info = read_plist(app_path / "Contents" / "Info.plist")
    aliases: list[str] = [
        app_path.stem,
        str(info.get("CFBundleDisplayName") or ""),
        str(info.get("CFBundleName") or ""),
        str(info.get("CFBundleExecutable") or ""),
        str(info.get("CFBundleIdentifier") or ""),
    ]
    aliases.extend(localized_bundle_names(app_path))
    aliases_tuple = unique_preserving_order(aliases)
    if not aliases_tuple:
        return None
    bundle_id = info.get("CFBundleIdentifier")
    display_name = aliases_tuple[0]
    for alias in aliases_tuple:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
            display_name = alias
            break
    return AppCandidate(
        display_name=display_name,
        identifier=os.fspath(app_path),
        aliases=aliases_tuple,
        path=os.fspath(app_path),
        bundle_id=bundle_id if isinstance(bundle_id, str) else None,
        source="filesystem",
    )


def running_app_candidates() -> list[AppCandidate]:
    script = (
        'tell application "System Events"\n'
        'set outputText to ""\n'
        'repeat with p in application processes\n'
        'set outputText to outputText & (name of p as text) & tab & (unix id of p as text) & linefeed\n'
        'end repeat\n'
        'return outputText\n'
        'end tell\n'
    )
    try:
        proc = run_command(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    candidates: list[AppCandidate] = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        name, pid = line.split("\t", 1)
        aliases = unique_preserving_order([name])
        if not aliases:
            continue
        candidates.append(
            AppCandidate(
                display_name=aliases[0],
                identifier=aliases[0],
                aliases=aliases,
                source=f"running:{pid}",
            )
        )
    return candidates


def discover_apps() -> list[AppCandidate]:
    candidates: list[AppCandidate] = []
    for app_path in discover_app_paths():
        candidate = app_candidate_from_path(app_path)
        if candidate is not None:
            candidates.append(candidate)
    candidates.extend(running_app_candidates())

    by_key: dict[str, AppCandidate] = {}
    for candidate in candidates:
        keys = [candidate.path or "", candidate.bundle_id or "", normalize_name(candidate.display_name)]
        key = next((item for item in keys if item), candidate.identifier)
        if key not in by_key or by_key[key].path is None:
            by_key[key] = candidate
    return list(by_key.values())


def running_pid_from_source(source: str | None) -> int | None:
    if not isinstance(source, str) or not source.startswith("running:"):
        return None
    try:
        return int(source.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def app_candidate_search_values(candidate: AppCandidate, extra_values: Iterable[str] = ()) -> list[str]:
    values = [
        candidate.display_name,
        candidate.identifier,
        candidate.bundle_id or "",
        candidate.path or "",
        candidate.source,
    ]
    values.extend(candidate.aliases)
    values.extend(extra_values)
    return [value for value in values if value]


def app_candidate_matches(candidate: AppCandidate, match: str | None, extra_values: Iterable[str] = ()) -> bool:
    if not match:
        return True
    try:
        pattern = re.compile(match, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(match), re.IGNORECASE)
    return any(pattern.search(value) for value in app_candidate_search_values(candidate, extra_values))


def compact_aliases(values: Iterable[str], *, limit: int = 8) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        normalized = normalize_name(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(value)
        if len(aliases) >= limit:
            break
    return aliases


def related_running_apps(app: AppCandidate, running_apps: list[AppCandidate]) -> list[AppCandidate]:
    app_aliases = set()
    app_aliases.update(normalize_name(alias) for alias in app.aliases)
    if app.path:
        app_aliases.add(normalize_name(Path(app.path).stem))
    if app.bundle_id:
        app_aliases.add(normalize_name(app.bundle_id))
    app_aliases.discard("")

    related: list[AppCandidate] = []
    for running_app in running_apps:
        running_aliases = {normalize_name(alias) for alias in running_app.aliases}
        running_aliases.add(normalize_name(running_app.display_name))
        running_aliases.discard("")
        if app_aliases.intersection(running_aliases):
            related.append(running_app)
    return related


def full_app_record(app: AppCandidate) -> dict[str, Any]:
    return {
        "display_name": app.display_name,
        "identifier": app.identifier,
        "bundle_id": app.bundle_id,
        "aliases": list(app.aliases),
        "source": app.source,
    }


def compact_app_record(app: AppCandidate, running_apps: list[AppCandidate]) -> dict[str, Any]:
    running_pids = [pid for item in running_apps for pid in [running_pid_from_source(item.source)] if pid is not None]
    running_names = compact_aliases(item.display_name for item in running_apps)
    aliases = compact_aliases([app.display_name, *app.aliases, *(name for item in running_apps for name in item.aliases)])
    record: dict[str, Any] = {
        "display_name": app.display_name,
        "identifier": app.identifier,
        "bundle_id": app.bundle_id,
        "aliases": aliases,
        "source": "filesystem" if app.path is not None else app.source,
        "path": app.path,
        "running": bool(running_pids),
    }
    if running_pids:
        record["running_pid"] = running_pids[0]
        record["running_pids"] = running_pids
        record["running_names"] = running_names
    return record


def app_record_preference_score(record: dict[str, Any]) -> tuple[int, str]:
    display_name = str(record.get("display_name") or "")
    identifier = str(record.get("identifier") or "")
    text = f"{display_name} {identifier}".casefold()
    score = 0
    if record.get("path"):
        score += 1000
    if record.get("bundle_id"):
        score += 100
    if record.get("running"):
        score += 25
    if "helper" in text or "renderer" in text or "crash" in text:
        score -= 500
    return score, display_name.casefold()


def is_helper_like_app_record(record: dict[str, Any]) -> bool:
    values = [
        str(record.get("display_name") or ""),
        str(record.get("identifier") or ""),
        str(record.get("bundle_id") or ""),
        str(record.get("source") or ""),
    ]
    aliases = record.get("aliases") or []
    if isinstance(aliases, list):
        values.extend(str(alias) for alias in aliases)
    text = " ".join(values).casefold()
    helper_markers = (
        " helper",
        "helper ",
        "renderer",
        "crash",
        "appex",
        "app ex",
        "login item",
    )
    return any(marker in text for marker in helper_markers)


def app_candidate_records(
    apps: list[AppCandidate],
    *,
    match: str | None = None,
    compact: bool = False,
    best: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    filesystem_apps = [app for app in apps if app.path is not None]
    running_apps = [app for app in apps if app.path is None and running_pid_from_source(app.source) is not None]

    if compact or best:
        consumed_running: set[str] = set()
        records: list[dict[str, Any]] = []
        for app in sorted(filesystem_apps, key=lambda item: item.display_name.casefold()):
            related = related_running_apps(app, running_apps)
            extra_values = [value for running_app in related for value in app_candidate_search_values(running_app)]
            if not app_candidate_matches(app, match, extra_values):
                continue
            for running_app in related:
                consumed_running.add(running_app.identifier)
            records.append(compact_app_record(app, related))

        for app in sorted(running_apps, key=lambda item: item.display_name.casefold()):
            if app.identifier in consumed_running:
                continue
            if not app_candidate_matches(app, match):
                continue
            records.append(compact_app_record(app, [app]))

        records.sort(key=lambda record: (-app_record_preference_score(record)[0], app_record_preference_score(record)[1]))
        non_helper_records = [record for record in records if not is_helper_like_app_record(record)]
        if non_helper_records:
            records = non_helper_records
    else:
        records = [
            full_app_record(app)
            for app in sorted(apps, key=lambda item: item.display_name.casefold())
            if app_candidate_matches(app, match)
        ]

    if best:
        return records[:1]
    if limit is not None and limit >= 0:
        return records[:limit]
    return records


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
            or (app.bundle_id and normalize_name(app.bundle_id) == target_norm)
            or (app.path and normalize_name(Path(app.path).stem) == target_norm)
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


def open_or_activate_app(app_identifier: str) -> int:
    try:
        proc = run_command([tool_path("AppOpenerTool"), app_identifier], timeout=30)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"failed to open app identifier {app_identifier!r}: {exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc
    matches = re.findall(r"\d+", proc.stdout)
    if not matches:
        raise RuntimeError(f"could not parse pid from AppOpenerTool stdout: {proc.stdout!r}")
    return int(matches[-1])


def traverse_app(pid: int, *, no_activate: bool = True) -> dict[str, Any]:
    attempts: list[tuple[bool, float]] = [(no_activate, 20)]
    if no_activate:
        attempts.extend([(True, 30), (False, 30)])
    else:
        attempts.append((False, 30))

    last_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    for index, (attempt_no_activate, timeout) in enumerate(attempts):
        cmd = [tool_path("TraversalTool"), "--visible-only"]
        if attempt_no_activate:
            cmd.append("--no-activate")
        cmd.append(str(pid))
        try:
            proc = run_command(cmd, timeout=timeout)
            return json.loads(proc.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if index < len(attempts) - 1:
                time.sleep(0.5 * (index + 1))

    assert last_error is not None
    raise last_error


def profile_match_text(target_identifier: str, target_resolution: dict[str, Any], traversal: dict[str, Any] | None = None) -> str:
    parts = [
        target_identifier,
        str(target_resolution.get("display_name", "")),
        str(target_resolution.get("matched_alias", "")),
        str(target_resolution.get("bundle_id", "")),
        str(target_resolution.get("input", "")),
    ]
    aliases = target_resolution.get("aliases")
    if isinstance(aliases, list):
        parts.extend(str(alias) for alias in aliases)
    if traversal is not None:
        parts.append(str(traversal.get("app_name", "")))
    return normalize_name(" ".join(parts))


def resolve_app_profile(
    target_identifier: str,
    target_resolution: dict[str, Any],
    traversal: dict[str, Any] | None = None,
    *,
    guide_dir: Path | None = None,
) -> AppProfile:
    haystack = profile_match_text(target_identifier, target_resolution, traversal)
    for profile in available_app_profiles(guide_dir):
        if any(normalize_name(term) in haystack for term in profile.match_terms):
            return profile
    return AppProfile(name="generic-ax-rich", workflow_mode="ax-rich", match_terms=(), fixed_strategy=False)


def resolve_workflow_mode(requested_mode: str, profile: AppProfile) -> str:
    if requested_mode == "auto":
        return profile.workflow_mode
    return requested_mode


def resolve_visual_planning(requested_mode: str, workflow_mode: str, profile: AppProfile) -> bool:
    if requested_mode == "on":
        return True
    if requested_mode == "off":
        return False
    return workflow_mode == "ax-poor" or profile.visual_planning


def should_use_llm_capability_selection(requested_selection: str, profile: AppProfile, *, mock_plan: bool = False) -> bool:
    if requested_selection == "profile":
        return False
    if requested_selection == "llm":
        return True
    if mock_plan:
        return False
    return not profile.fixed_strategy


def parse_llm_json_object(raw_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        _, extract_and_convert_dict = load_llm_helpers()
        parsed = extract_and_convert_dict(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM did not return a JSON object: {raw_text[:500]!r}")
    return parsed


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


def normalize_capability_decision(
    raw: dict[str, Any],
    *,
    fallback_workflow_mode: str,
    fallback_visual_planning: bool,
    source: str,
) -> dict[str, Any]:
    workflow_mode = str(raw.get("workflow_mode") or raw.get("mode") or fallback_workflow_mode).strip().lower()
    if workflow_mode not in {"ax-rich", "ax-poor"}:
        workflow_mode = fallback_workflow_mode

    confidence: float | None = None
    try:
        if raw.get("confidence") is not None:
            confidence = min(1.0, max(0.0, float(raw.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None

    return {
        "source": source,
        "workflow_mode": workflow_mode,
        "visual_planning": parse_bool(raw.get("visual_planning"), fallback_visual_planning),
        "confidence": confidence,
        "reason": clean_text(raw.get("reason") or raw.get("summary") or raw.get("explanation"), limit=500),
    }


def apply_capability_decision(
    *,
    requested_mode: str,
    requested_visual_planning: str,
    profile: AppProfile,
    decision: dict[str, Any] | None,
) -> tuple[str, bool]:
    workflow_mode = resolve_workflow_mode(requested_mode, profile)
    visual_planning = resolve_visual_planning(requested_visual_planning, workflow_mode, profile)
    if decision is None:
        return workflow_mode, visual_planning
    if requested_mode == "auto":
        decided_mode = str(decision.get("workflow_mode") or workflow_mode)
        if decided_mode in {"ax-rich", "ax-poor"}:
            workflow_mode = decided_mode
    if requested_visual_planning == "auto":
        visual_planning = parse_bool(decision.get("visual_planning"), visual_planning)
    return workflow_mode, visual_planning


def window_region_from_traversal(traversal: dict[str, Any], window_index: int = 0) -> tuple[float, float, float, float] | None:
    windows = [
        frame
        for element in traversal.get("elements", [])
        if isinstance(element, dict) and base_role(str(element.get("role", ""))) == "AXWindow"
        for frame in [element_frame(element)]
        if frame is not None
    ]
    if not windows:
        return None
    return windows[min(max(window_index, 0), len(windows) - 1)]


def capture_region(region: tuple[float, float, float, float], output: Path) -> Path:
    x, y, width, height = region
    output.parent.mkdir(parents=True, exist_ok=True)
    region_arg = f"-R{int(round(x))},{int(round(y))},{int(round(width))},{int(round(height))}"
    run_command(["screencapture", "-x", region_arg, os.fspath(output)], timeout=15)
    return output


def prepare_visual_planner_image(
    screenshot_path: Path,
    *,
    artifact_dir: Path,
    step_number: int,
    max_width: int,
) -> Path:
    if max_width <= 0:
        return screenshot_path

    output = artifact_dir / f"step-{step_number:02d}-visual.png"
    try:
        proc = run_command(
            ["sips", "-Z", str(max_width), os.fspath(screenshot_path), "--out", os.fspath(output)],
            check=False,
            timeout=20,
        )
        if proc.returncode == 0 and output.exists():
            return output
    except Exception as exc:
        print(f"warning: visual screenshot resize failed, using original screenshot: {exc}", file=sys.stderr)
    return screenshot_path


def image_file_base64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def capability_ax_summary(traversal: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    text_count = 0
    direct_ax_count = 0
    interactive_count = 0
    for element in elements:
        role = str(element.get("role") or "")
        role_base = base_role(role)
        role_counts[role_base] = role_counts.get(role_base, 0) + 1
        if clean_text(element.get("text")):
            text_count += 1
        if element.get("direct_ax"):
            direct_ax_count += 1
        if any(token in role for token in ("Button", "TextField", "TextArea", "SearchField", "ComboBox", "CheckBox", "RadioButton", "PopUpButton", "Row", "Cell")):
            interactive_count += 1
    return {
        "traversal_stats": traversal.get("stats", {}),
        "visible_ax_elements_sent": len(elements),
        "text_element_count": text_count,
        "direct_ax_count": direct_ax_count,
        "interactive_element_count": interactive_count,
        "role_counts": dict(sorted(role_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
    }


def build_capability_selection_prompt(
    *,
    user_instruction: str,
    target_identifier: str,
    target_resolution: dict[str, Any],
    traversal: dict[str, Any],
    app_profile: AppProfile,
    fallback_workflow_mode: str,
    fallback_visual_planning: bool,
    elements: list[dict[str, Any]],
    ax_summary: dict[str, Any],
    screenshot_attached: bool,
) -> str:
    payload = {
        "target_identifier": target_identifier,
        "target_resolution": target_resolution,
        "target_app": traversal.get("app_name", target_identifier),
        "user_instruction": user_instruction,
        "current_profile": {
            "name": app_profile.name,
            "fixed_strategy": app_profile.fixed_strategy,
            "fallback_workflow_mode": fallback_workflow_mode,
            "fallback_visual_planning": fallback_visual_planning,
        },
        "ax_summary": ax_summary,
        "sample_ax_elements": elements[:80],
        "screenshot_attached": screenshot_attached,
    }
    return (
        "You are choosing the macOS app automation capability mode before any task planning.\n"
        "Do not plan UI actions. Choose only the observation/control strategy for this app state.\n\n"
        "Available strategies:\n"
        "- ax-rich: Accessibility exposes enough visible, actionable controls and text. The workflow still runs local OCR; prefer AX elements and direct AX actions, then OCR text, then coordinates only as fallback.\n"
        "- ax-poor: Accessibility is sparse, generic, unlabeled, stale, canvas-like, or missing the useful text. The workflow still prioritizes AX first, then local OCR, then app/profile region hints.\n"
        "- visual_planning=true: the planner should receive screenshots only when important state is visual-only, icon-only, custom-rendered, or selected/highlighted state is not represented in AX/OCR.\n\n"
        "Decision rules:\n"
        "1. Choose ax-rich when AX has meaningful roles, labels, direct_ax paths, and likely target controls.\n"
        "2. Choose ax-poor when the useful UI is mostly unlabeled groups, static images, rows without text, custom content, or AX count/text quality is low.\n"
        "3. Enable visual_planning only when AX and OCR are insufficient to understand layout, visual state, icons, media, canvas, or ambiguous rows.\n"
        "4. If evidence is mixed, prefer the conservative hybrid path: ax-poor, and add visual_planning only for state that cannot be read from AX/OCR.\n"
        "5. Return JSON only: {\"workflow_mode\":\"ax-rich|ax-poor\",\"visual_planning\":true|false,\"confidence\":0.0,\"reason\":\"...\"}.\n\n"
        "Current capability evidence JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def choose_app_capabilities(
    *,
    user_instruction: str,
    target_identifier: str,
    target_resolution: dict[str, Any],
    traversal: dict[str, Any],
    app_profile: AppProfile,
    artifact_dir: Path,
    visual_max_width: int,
    include_menus: bool,
    model: str | None,
) -> dict[str, Any]:
    fallback_workflow_mode = resolve_workflow_mode("auto", app_profile)
    fallback_visual_planning = resolve_visual_planning("auto", fallback_workflow_mode, app_profile)
    elements, _ = summarize_elements(
        traversal,
        max_elements=120,
        include_menus=include_menus,
        include_virtual_hints=False,
    )
    ax_summary = capability_ax_summary(traversal, elements)

    screenshot_path: Path | None = None
    planner_image_path: Path | None = None
    planner_images: list[str] = []
    screenshot_error: str | None = None
    window_frame = window_region_from_traversal(traversal)
    if window_frame is not None:
        try:
            screenshot_path = capture_region(window_frame, artifact_dir / "capability-selection-screenshot.png")
            planner_image_path = prepare_visual_planner_image(
                screenshot_path,
                artifact_dir=artifact_dir,
                step_number=0,
                max_width=visual_max_width,
            )
            planner_images.append(image_file_base64(planner_image_path))
        except Exception as exc:
            screenshot_error = str(exc)

    prompt = build_capability_selection_prompt(
        user_instruction=user_instruction,
        target_identifier=target_identifier,
        target_resolution=target_resolution,
        traversal=traversal,
        app_profile=app_profile,
        fallback_workflow_mode=fallback_workflow_mode,
        fallback_visual_planning=fallback_visual_planning,
        elements=elements,
        ax_summary=ax_summary,
        screenshot_attached=bool(planner_images),
    )
    call_llm, _ = load_llm_helpers()
    llm_kwargs = {key: value for key, value in {"model_name": model}.items() if value}
    if planner_images:
        llm_kwargs["image_base64"] = planner_images
    raw = call_llm(prompt, **llm_kwargs)
    decision = normalize_capability_decision(
        parse_llm_json_object(raw),
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


def profile_capability_decision(profile: AppProfile, workflow_mode: str, visual_planning: bool) -> dict[str, Any]:
    return {
        "source": "profile",
        "workflow_mode": workflow_mode,
        "visual_planning": visual_planning,
        "confidence": 1.0,
        "reason": "Matched a fixed app capability profile.",
        "profile": profile.name,
        "profile_fixed_strategy": profile.fixed_strategy,
        "app_guide_path": profile.guide_path,
    }


def run_local_ocr(image_path: Path, languages: str, recognition_level: str) -> dict[str, Any]:
    proc = run_command(
        ["swift", "-", os.fspath(image_path), languages, recognition_level],
        timeout=60,
        input_text=OCR_SWIFT_SOURCE,
    )
    return json.loads(proc.stdout)


def add_screen_frames_to_ocr_payload(
    payload: dict[str, Any],
    region: tuple[float, float, float, float] | None,
) -> None:
    if region is None:
        return

    try:
        image_width = float(payload["imageWidth"])
        image_height = float(payload["imageHeight"])
    except (KeyError, TypeError, ValueError):
        return

    region_x, region_y, region_width, region_height = region
    if image_width <= 0 or image_height <= 0 or region_width <= 0 or region_height <= 0:
        return

    scale_x = image_width / region_width
    scale_y = image_height / region_height
    payload["coordinateSpace"] = {
        "frame": "image_pixels_relative_to_screenshot",
        "screenFrame": "screen_points_top_left",
    }

    for line in payload.get("lines", []):
        if not isinstance(line, dict):
            continue
        frame = line.get("frame")
        if not isinstance(frame, dict):
            continue
        try:
            x = float(frame["x"])
            y = float(frame["y"])
            width = float(frame["width"])
            height = float(frame["height"])
        except (KeyError, TypeError, ValueError):
            continue
        screen_frame = {
            "x": region_x + x / scale_x,
            "y": region_y + y / scale_y,
            "width": width / scale_x,
            "height": height / scale_y,
        }
        line["imageFrame"] = dict(frame)
        line["screenFrame"] = screen_frame
        line["screenCenter"] = {
            "x": screen_frame["x"] + screen_frame["width"] / 2.0,
            "y": screen_frame["y"] + screen_frame["height"] / 2.0,
        }


def clean_text(value: Any, *, limit: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    if not compact:
        return None
    if len(compact) > limit:
        return compact[: limit - 1] + "..."
    return compact


def base_role(role: str) -> str:
    return role.split(" ", 1)[0]


def is_menu_role(role: str) -> bool:
    role_base = base_role(role)
    return role_base in MENU_ROLES or any(role_base.startswith(prefix) for prefix in MENU_ROLE_PREFIXES)


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


def frame_contains_point(frame: tuple[float, float, float, float], point: tuple[float, float], *, margin: float = 8.0) -> bool:
    x, y, width, height = frame
    px, py = point
    return x - margin <= px <= x + width + margin and y - margin <= py <= y + height + margin


def center_of_frame(frame: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, width, height = frame
    return (x + width / 2.0, y + height / 2.0)


def is_inside_any_window(element: dict[str, Any], window_frames: list[tuple[float, float, float, float]]) -> bool:
    if not window_frames:
        return True
    role = str(element.get("role", ""))
    if base_role(role) == "AXWindow" or is_menu_role(role):
        return True
    frame = element_frame(element)
    if frame is None:
        return False
    return any(frame_contains_point(window_frame, center_of_frame(frame)) for window_frame in window_frames)


def element_priority(element: dict[str, Any]) -> tuple[int, float, float]:
    role = element.get("role", "")
    text = clean_text(element.get("text")) or ""
    y = float(element.get("y") or 0)
    width = float(element.get("width") or 0)

    if "AXTextArea" in role or "AXTextField" in role or "AXSearchField" in role or "AXComboBox" in role:
        return (0, -y, -width)
    if "AXButton" in role or "AXCheckBox" in role or "AXRadioButton" in role or "AXPopUpButton" in role:
        return (1, -y, -len(text))
    if "AXList" in role or "AXTable" in role or "AXRow" in role or "AXCell" in role:
        return (2, -y, -len(text))
    if text:
        return (3, -y, -len(text))
    return (4, -y, 0)


def summarize_elements(
    traversal: dict[str, Any],
    *,
    max_elements: int,
    include_menus: bool = False,
    include_virtual_hints: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, UiElement]]:
    raw_elements = traversal.get("elements") or []
    window_frames = [
        frame
        for element in raw_elements
        if isinstance(element, dict) and base_role(str(element.get("role", ""))) == "AXWindow"
        for frame in [element_frame(element)]
        if frame is not None
    ]
    candidates: list[dict[str, Any]] = []
    for element in raw_elements:
        if not isinstance(element, dict):
            continue
        if element.get("x") is None or element.get("y") is None:
            continue
        if not include_menus and is_menu_role(str(element.get("role", ""))):
            continue
        if not is_inside_any_window(element, window_frames):
            continue
        candidates.append(element)

    candidates.sort(key=element_priority)
    selected = candidates[:max_elements]

    summary: list[dict[str, Any]] = []
    index: dict[str, UiElement] = {}
    for i, element in enumerate(selected):
        element_id = f"e{i}"
        ui_element = UiElement(
            element_id=element_id,
            role=str(element.get("role") or ""),
            text=clean_text(element.get("text")),
            x=float(element["x"]) if element.get("x") is not None else None,
            y=float(element["y"]) if element.get("y") is not None else None,
            width=float(element["width"]) if element.get("width") is not None else None,
            height=float(element["height"]) if element.get("height") is not None else None,
            ax_path=clean_text(element.get("axPath") or element.get("ax_path"), limit=1000),
        )
        index[element_id] = ui_element
        summary.append(
            {
                "id": element_id,
                "source": ui_element.source,
                "role": ui_element.role,
                "text": ui_element.text,
                "direct_ax": ui_element.ax_path is not None,
                "frame": {
                    "x": ui_element.x,
                    "y": ui_element.y,
                    "width": ui_element.width,
                    "height": ui_element.height,
                },
                "center": {
                    "x": ui_element.center[0],
                    "y": ui_element.center[1],
                },
            }
        )
    if include_virtual_hints:
        add_virtual_region_hints(summary, index, window_frames, app_name=str(traversal.get("app_name", "")))
    return summary, index


def add_virtual_region_hints(
    summary: list[dict[str, Any]],
    index: dict[str, UiElement],
    window_frames: list[tuple[float, float, float, float]],
    *,
    app_name: str = "",
) -> None:
    has_real_text_input = any(
        "AXTextArea" in element.role or "AXTextField" in element.role or "AXSearchField" in element.role or "AXComboBox" in element.role
        for element in index.values()
    )
    if has_real_text_input:
        return

    for window_i, window_frame in enumerate(window_frames[:2]):
        x, y, width, height = window_frame
        if width < 300 or height < 220:
            continue
        hint_specs = [
            (
                "top-left search/input candidate generated by workflow; use only when a real search field is missing",
                x + 70,
                y + 32,
                min(280, max(160, width * 0.34)),
                42,
            ),
            (
                "bottom compose/input candidate generated by workflow; use only after the intended conversation or document area is visibly selected",
                x + width * 0.32,
                y + height - 110,
                width * 0.62,
                74,
            ),
        ]
        for hint_text, hx, hy, hwidth, hheight in hint_specs:
            hint_id = f"h{len(index)}"
            ui_element = UiElement(
                element_id=hint_id,
                role="VirtualRegion",
                text=hint_text,
                x=float(hx),
                y=float(hy),
                width=float(hwidth),
                height=float(hheight),
                ax_path=None,
                source="virtual_region",
            )
            index[hint_id] = ui_element
            summary.append(
                {
                    "id": hint_id,
                    "source": ui_element.source,
                    "role": ui_element.role,
                    "text": ui_element.text,
                    "direct_ax": False,
                    "frame": {
                        "x": ui_element.x,
                        "y": ui_element.y,
                        "width": ui_element.width,
                        "height": ui_element.height,
                    },
                    "center": {
                        "x": ui_element.center[0],
                        "y": ui_element.center[1],
                    },
                }
            )


def summarize_ocr_lines(
    ocr_payload: dict[str, Any] | None,
    index: dict[str, UiElement],
    *,
    max_lines: int,
) -> list[dict[str, Any]]:
    if not ocr_payload:
        return []
    summary: list[dict[str, Any]] = []
    lines = [line for line in ocr_payload.get("lines", []) if isinstance(line, dict)]
    for line in lines[:max_lines]:
        text = clean_text(line.get("text"))
        frame = line.get("screenFrame")
        if not text or not isinstance(frame, dict):
            continue
        try:
            x = float(frame["x"])
            y = float(frame["y"])
            width = float(frame["width"])
            height = float(frame["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        confidence = None
        try:
            confidence = float(line.get("confidence")) if line.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        element_id = f"o{len(summary)}"
        ui_element = UiElement(
            element_id=element_id,
            role="OCRLine",
            text=text,
            x=x,
            y=y,
            width=width,
            height=height,
            ax_path=None,
            source="ocr",
            ocr_confidence=confidence,
        )
        index[element_id] = ui_element
        summary.append(
            {
                "id": element_id,
                "source": "ocr",
                "role": ui_element.role,
                "text": ui_element.text,
                "direct_ax": False,
                "ocr_confidence": confidence,
                "frame": {"x": x, "y": y, "width": width, "height": height},
                "center": {"x": ui_element.center[0], "y": ui_element.center[1]},
            }
        )
    return summary


def resolve_region_value(raw_value: str, *, origin: float, span: float, is_position: bool) -> float:
    value = raw_value.strip().casefold()
    if value.endswith("%"):
        number = float(value[:-1].strip()) * span / 100.0
    elif value.endswith("px"):
        number = float(value[:-2].strip())
    else:
        number = float(value)
    return origin + number if is_position else number


def profile_regions_for_window(profile: AppProfile, window_frame: tuple[float, float, float, float] | None) -> list[dict[str, Any]]:
    if window_frame is None or not profile.profile_regions:
        return []
    x, y, width, height = window_frame
    regions = []
    for spec in profile.profile_regions:
        try:
            rx = resolve_region_value(spec.x, origin=x, span=width, is_position=True)
            ry = resolve_region_value(spec.y, origin=y, span=height, is_position=True)
            rwidth = resolve_region_value(spec.width, origin=x, span=width, is_position=False)
            rheight = resolve_region_value(spec.height, origin=y, span=height, is_position=False)
        except (TypeError, ValueError):
            continue
        if rwidth <= 0 or rheight <= 0:
            continue
        regions.append(
            {
                "name": spec.name,
                "text": spec.text,
                "frame": {"x": float(rx), "y": float(ry), "width": float(rwidth), "height": float(rheight)},
            }
        )
    return regions


def add_profile_regions(
    regions: list[dict[str, Any]],
    index: dict[str, UiElement],
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for region in regions:
        frame = region.get("frame")
        if not isinstance(frame, dict):
            continue
        try:
            x = float(frame["x"])
            y = float(frame["y"])
            width = float(frame["width"])
            height = float(frame["height"])
        except (KeyError, TypeError, ValueError):
            continue
        element_id = f"p{len(summary)}"
        ui_element = UiElement(
            element_id=element_id,
            role="ProfileRegion",
            text=clean_text(region.get("text"), limit=220) or str(region.get("name") or "profile region"),
            x=x,
            y=y,
            width=width,
            height=height,
            ax_path=None,
            source="profile_region",
        )
        index[element_id] = ui_element
        summary.append(
            {
                "id": element_id,
                "source": "profile_region",
                "role": ui_element.role,
                "name": region.get("name"),
                "text": ui_element.text,
                "direct_ax": False,
                "frame": {"x": x, "y": y, "width": width, "height": height},
                "center": {"x": ui_element.center[0], "y": ui_element.center[1]},
            }
        )
    return summary


def build_planner_prompt(
    user_instruction: str,
    target_identifier: str,
    traversal: dict[str, Any],
    elements: list[dict[str, Any]],
    observation: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    step_number: int,
    max_steps: int,
    max_actions_per_step: int,
    workflow_mode: str,
    app_profile: AppProfile,
) -> str:
    payload = {
        "target_identifier": target_identifier,
        "target_app": traversal.get("app_name", target_identifier),
        "workflow_mode": workflow_mode,
        "app_profile": app_profile.name,
        "app_guide_path": app_profile.guide_path,
        "step_number": step_number,
        "max_steps": max_steps,
        "stats": traversal.get("stats", {}),
        "elements": elements,
        "observation": observation,
        "recent_history": history[-5:],
        "user_instruction": user_instruction,
    }
    app_guidance = app_profile.guidance
    mode_guidance = ""
    if workflow_mode == "ax-poor":
        mode_guidance = (
            "AX-poor workflow guidance:\n"
            "- The observation includes ax_elements, ocr_lines, screenshot_path, profile_regions, and visual_observation.\n"
            "- Priority is AX > OCR > visual planner. Prefer real AX elements when they clearly match the target.\n"
            "- If AX elements are missing or ambiguous, use OCRLine elements whose text and screenFrame match the intended target.\n"
            "- If OCR is still insufficient, use ProfileRegion elements as coarse coordinate hints or inspect the attached screenshot only when visual_observation.image_attached_to_planner=true.\n"
            "- Never type a message body until the intended recipient/conversation is visibly selected.\n\n"
        )
    else:
        mode_guidance = (
            "AX-rich workflow guidance:\n"
            "- The observation includes ax_elements and ocr_lines. Priority is AX > OCR > visual planner.\n"
            "- Prefer visible direct_ax elements because this app exposes useful Accessibility controls.\n"
            "- Use OCRLine elements only when AX lacks the needed visible text or AX text is ambiguous.\n"
            "- If recent history shows a direct AX click did not change the UI, prefer a different AX control, keyboard shortcut, or wait/re-observe path. Use coordinate clicks only when the current observation gives a fresh center point for the exact visible control.\n\n"
        )
    return (
        "You are a cautious macOS UI automation planner.\n"
        "You control exactly one target application through accessibility elements and keyboard/mouse events.\n"
        "Given the current UI state, the prior step history, and the user's goal, choose exactly one next UI action.\n"
        "The controller will execute that one action, re-traverse the UI, and ask you again.\n"
        "Return a compact JSON object only. Do not use markdown. Do not include actions outside the allowed schema.\n\n"
        "Allowed action schema:\n"
        "- {\"type\":\"click\",\"element_id\":\"e12\"}\n"
        "- {\"type\":\"click\",\"x\":123,\"y\":456}\n"
        "- {\"type\":\"doubleclick\",\"element_id\":\"e12\"}\n"
        "- {\"type\":\"rightclick\",\"element_id\":\"e12\"}\n"
        "- {\"type\":\"mousemove\",\"element_id\":\"e12\"}\n"
        "- {\"type\":\"scroll\",\"element_id\":\"e12\",\"deltaY\":5,\"deltaX\":0}\n"
        "- {\"type\":\"writetext\",\"text\":\"text to type\"}\n"
        "- {\"type\":\"writetext\",\"element_id\":\"e12\",\"text\":\"text to set\"}\n"
        "- {\"type\":\"keypress\",\"key\":\"enter\"}  // examples: enter, return, tab, escape, cmd+a, cmd+s\n"
        "- {\"type\":\"wait\",\"seconds\":1.0}\n"
        "- {\"type\":\"finish\"}\n\n"
        "Element notes:\n"
        "- direct_ax=true means the controller can ask Swift to operate that AX element directly without converting it to a mouse coordinate.\n"
        "- Some apps report direct AX success without changing transient UI; if recent_history shows a successful direct_ax action and the same UI is still visible, choose a different reliable AX control or keyboard route. Use a coordinate click on the same visible control only when the current observation still shows that exact control and the action is low risk.\n"
        "- Elements with source=virtual_region, source=profile_region, or role=OCRLine are coordinate-backed hints, not real AX elements. OCRLine is preferred over visual screenshot reasoning when AX is insufficient.\n"
        "- If visual_observation.image_attached_to_planner=true, a screenshot is attached to this request. Use it only after AX and OCR are insufficient for visual-only state such as selected rows, popovers, icons, badges, or unlabeled controls.\n"
        "- When using the screenshot without a matching element_id, return raw screen-point coordinates and include \"source\":\"visual\" in that action. Treat the attached image as potentially resized; map only relative positions into visual_observation.coordinate_space.screenshot_region.\n"
        "- Hidden/off-window AX elements are filtered out before you see the element list.\n\n"
        f"{mode_guidance}"
        f"{app_guidance}"
        "Planning rules:\n"
        "1. Return exactly one action unless the single action is finish.\n"
        "2. Prefer element_id over raw coordinates when an element is visible.\n"
        "3. Do not invent element ids. Use raw coordinates only when necessary.\n"
        "4. Use keyboard shortcuts when they are safer than navigating hidden menus.\n"
        "5. After opening dialogs, submitting forms, changing views, searching, or waiting for UI work, return only wait if needed so the controller can re-observe.\n"
        "6. For messaging/email tasks, do not type the message body until the recipient/conversation is visibly selected. Search/select the recipient first, then wait/re-observe, then type the message body in the compose field.\n"
        "7. If recent_history shows a writetext result with post_input_verification.expected_text_visible=false, treat that text entry as unverified. Do not press Enter or finish until the current visible UI, OCR, or attached screenshot clearly proves the text is in the compose field or sent history.\n"
        "8. If recent_history shows writetext skipped because text_already_present_in_text_target, do not write the same text again. Select/submit/finish, or explicitly clear and replace only when that is necessary.\n"
        "9. Use finish only when the user goal is complete, blocked, or requires human credentials/confirmation outside visible safe controls.\n"
        f"10. Hard limit: at most {max_actions_per_step} action in this step.\n"
        "11. Return this JSON shape exactly: {\"status\":\"continue|finished|blocked\",\"summary\":\"...\",\"actions\":[...]}.\n\n"
        "Current state JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def load_llm_helpers():
    module_path = SCRIPTS_ROOT / "utils" / "llm_config.py"
    spec = importlib.util.spec_from_file_location("_macos_app_workflow_llm_config", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load LLM helper module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.call_llm, module.extract_and_convert_dict


def parse_llm_plan(raw_text: str) -> dict[str, Any]:
    parsed = parse_llm_json_object(raw_text)
    if not isinstance(parsed.get("actions"), list):
        raise ValueError(f"LLM plan is missing an actions list: {parsed!r}")
    return parsed


def find_best_text_input(element_index: dict[str, UiElement]) -> str | None:
    candidates = [
        element
        for element in element_index.values()
        if "AXTextArea" in element.role or "AXTextField" in element.role or "AXSearchField" in element.role
    ]
    if not candidates:
        candidates = [
            element
            for element in element_index.values()
            if element.role == "VirtualRegion"
            and element.text
            and ("search/input candidate" in element.text or "search field candidate" in element.text)
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

    return {
        "status": "continue",
        "summary": "Fallback plan: advance by one UI action, then re-observe.",
        "actions": [action],
    }


def make_plan(
    user_instruction: str,
    target_identifier: str,
    traversal: dict[str, Any],
    elements: list[dict[str, Any]],
    observation: dict[str, Any],
    element_index: dict[str, UiElement],
    history: list[dict[str, Any]],
    *,
    step_number: int,
    max_steps: int,
    max_actions_per_step: int,
    workflow_mode: str,
    app_profile: AppProfile,
    model: str | None,
    mock_plan: bool,
    allow_fallback: bool,
    planner_images: list[str] | None = None,
) -> dict[str, Any]:
    if mock_plan:
        return fallback_plan(user_instruction, element_index, history)

    prompt = build_planner_prompt(
        user_instruction,
        target_identifier,
        traversal,
        elements,
        observation,
        history,
        step_number=step_number,
        max_steps=max_steps,
        max_actions_per_step=max_actions_per_step,
        workflow_mode=workflow_mode,
        app_profile=app_profile,
    )
    try:
        call_llm, _ = load_llm_helpers()
        llm_kwargs = {k: v for k, v in {"model_name": model}.items() if v}
        if planner_images:
            llm_kwargs["image_base64"] = planner_images
        raw = call_llm(prompt, **llm_kwargs)
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
        print(
            f"warning: planner returned more than {max_actions_per_step} action(s); only the first action(s) will execute before re-observation",
            file=sys.stderr,
        )

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


def action_element_snapshots(actions: list[dict[str, Any]], element_index: dict[str, UiElement]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in actions:
        element_id = action.get("element_id")
        if not element_id:
            if "x" in action and "y" in action:
                try:
                    x = float(action["x"])
                    y = float(action["y"])
                except (TypeError, ValueError):
                    continue
                snapshots.append(
                    {
                        "element_id": None,
                        "source": str(action.get("source") or "coordinate"),
                        "role": "Coordinate",
                        "text": clean_text(action.get("reason"), limit=180),
                        "frame": None,
                        "center": {"x": x, "y": y},
                        "direct_ax": False,
                        "ax_path": None,
                        "ocr_confidence": None,
                    }
                )
            continue
        element_key = str(element_id)
        if element_key in seen:
            continue
        seen.add(element_key)
        element = element_index.get(element_key)
        if element is None:
            continue
        frame: dict[str, float] | None = None
        if element.x is not None and element.y is not None and element.width is not None and element.height is not None:
            frame = {
                "x": element.x,
                "y": element.y,
                "width": element.width,
                "height": element.height,
            }
        center = None
        if frame is not None:
            center_point = element.center
            center = {"x": center_point[0], "y": center_point[1]}
        snapshots.append(
            {
                "element_id": element.element_id,
                "source": element.source,
                "role": element.role,
                "text": element.text,
                "frame": frame,
                "center": center,
                "direct_ax": element.ax_path is not None,
                "ax_path": element.ax_path,
                "ocr_confidence": element.ocr_confidence,
            }
        )
    return snapshots


def action_preserves_observation_context(action_type: str, element: UiElement | None) -> bool:
    if element is None:
        return False
    return action_type in {"click", "doubleclick", "rightclick", "mousemove", "scroll", "writetext"}


def maybe_activate_for_action(
    *,
    action_type: str,
    element: UiElement | None,
    target_identifier: str,
    target_pid: int,
    preserve_observation: bool = False,
) -> tuple[int, str]:
    if preserve_observation:
        return target_pid, "preserved_visual_observation"
    if action_preserves_observation_context(action_type, element):
        return target_pid, "preserved_observation"

    activated_pid = open_or_activate_app(target_identifier)
    time.sleep(0.25)
    return activated_pid, "activated_target"


def app_profile_name(profile: AppProfile | None) -> str:
    return profile.name if profile is not None else ""


def is_feishu_lark_context(target_identifier: str, profile: AppProfile | None = None) -> bool:
    if app_profile_name(profile) == "feishu-lark":
        return True
    normalized = normalize_name(target_identifier)
    lark_profile = next((item for item in load_app_guide_profiles() if item.name == "feishu-lark"), None)
    if lark_profile is None:
        return False
    return any(normalize_name(term) in normalized for term in lark_profile.match_terms)


def should_prefer_event_text_input(
    *,
    target_identifier: str,
    app_profile: AppProfile | None,
    element: UiElement | None,
) -> bool:
    if not is_feishu_lark_context(target_identifier, app_profile):
        return False
    if element is None:
        return True
    role = element.role
    return any(token in role for token in ("AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"))


def focus_text_target(
    *,
    input_tool: str,
    element: UiElement | None,
    target_pid: int,
    allow_coordinate_fallback: bool = True,
) -> dict[str, Any] | None:
    if element is None:
        return None
    diagnostics: dict[str, Any] = {
        "element_id": element.element_id,
        "frame": None,
        "focus_method": None,
        "focus_ok": False,
    }
    has_frame = element.x is not None and element.y is not None and element.width is not None and element.height is not None
    if has_frame:
        diagnostics["frame"] = {"x": element.x, "y": element.y, "width": element.width, "height": element.height}
    if element.ax_path is not None:
        diagnostics["focus_method"] = "direct_ax_focus"
        diagnostics["ax_path"] = element.ax_path
        try:
            proc = run_command([input_tool, "axfocus", str(target_pid), element.ax_path], timeout=10)
            diagnostics["focus_ok"] = True
            diagnostics["stderr"] = proc.stderr[-1000:]
            time.sleep(0.15)
            return diagnostics
        except subprocess.CalledProcessError as exc:
            diagnostics["focus_error"] = (exc.stderr or exc.stdout or str(exc)).strip()[-1000:]
            if not allow_coordinate_fallback:
                time.sleep(0.15)
                return diagnostics
    if has_frame:
        x, y = element.center
        diagnostics["focus_method"] = "coordinate_click"
        proc = run_command([input_tool, "click", f"{x:.1f}", f"{y:.1f}"], timeout=10)
        diagnostics["focus_ok"] = True
        diagnostics["point"] = {"x": x, "y": y}
        diagnostics["stderr"] = proc.stderr[-1000:]
        time.sleep(0.15)
        return diagnostics
    return diagnostics


def read_clipboard_text() -> tuple[str, bool, str | None]:
    try:
        proc = run_command(clipboard_command("pbpaste"), check=False, timeout=5)
    except Exception as exc:
        return ("", False, str(exc))
    if proc.returncode != 0:
        return ("", False, (proc.stderr or proc.stdout).strip() or f"pbpaste exited {proc.returncode}")
    return (proc.stdout, True, None)


def write_clipboard_text(text: str) -> tuple[bool, str | None]:
    try:
        proc = run_command(clipboard_command("pbcopy"), check=False, timeout=5, input_text=text)
    except Exception as exc:
        return (False, str(exc))
    if proc.returncode != 0:
        return (False, (proc.stderr or proc.stdout).strip() or f"pbcopy exited {proc.returncode}")
    return (True, None)


def paste_text_via_clipboard(
    *,
    input_tool: str,
    text: str,
    replace_existing: bool,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    previous_text, clipboard_read_ok, clipboard_read_error = read_clipboard_text()
    diagnostics: dict[str, Any] = {
        "clipboard_read_ok": clipboard_read_ok,
        "clipboard_restore_ok": None,
        "clipboard_restore_error": None,
        "replace_existing": replace_existing,
        "text_length": len(text),
    }
    if clipboard_read_error:
        diagnostics["clipboard_read_error"] = clipboard_read_error

    clipboard_write_ok, clipboard_write_error = write_clipboard_text(text)
    diagnostics["clipboard_write_ok"] = clipboard_write_ok
    if clipboard_write_error:
        diagnostics["clipboard_write_error"] = clipboard_write_error
    if not clipboard_write_ok:
        raise subprocess.CalledProcessError(1, clipboard_command("pbcopy"), stderr=clipboard_write_error or "pbcopy failed")

    try:
        if replace_existing:
            select_proc = run_command([input_tool, "keypress", "cmd+a"], timeout=10)
            diagnostics["select_all_ok"] = True
            diagnostics["select_all_stderr"] = select_proc.stderr[-1000:]
            time.sleep(0.08)
        proc = run_command([input_tool, "keypress", "cmd+v"], timeout=10)
        diagnostics["paste_ok"] = True
        diagnostics["paste_stderr"] = proc.stderr[-1000:]
        time.sleep(0.2)
        return proc, diagnostics
    finally:
        if clipboard_read_ok:
            restore_ok, restore_error = write_clipboard_text(previous_text)
            diagnostics["clipboard_restore_ok"] = restore_ok
            diagnostics["clipboard_restore_error"] = restore_error


def text_values_in_elements(elements: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for element in elements:
        text = clean_text(element.get("text"), limit=1000)
        if text:
            values.append(text)
    return values


def is_text_input_element(element: UiElement) -> bool:
    role = element.role.casefold()
    return any(
        marker in role
        for marker in (
            "axtextarea",
            "axtextfield",
            "text area",
            "text field",
            "文本输入区",
            "文本字段",
        )
    )


def text_already_present_in_text_target(
    text: str,
    element: UiElement | None,
    element_index: dict[str, UiElement],
) -> dict[str, Any] | None:
    expected_text = clean_text(text, limit=2000)
    if not expected_text:
        return None
    candidates = [element] if element is not None else [item for item in element_index.values() if is_text_input_element(item)]
    for candidate in candidates:
        if candidate is None:
            continue
        current_text = clean_text(candidate.text, limit=4000)
        if current_text and expected_text in current_text:
            return {
                "element_id": candidate.element_id,
                "role": candidate.role,
                "existing_text_length": len(current_text),
                "expected_text_length": len(expected_text),
            }
    return None


def verify_previous_text_input(step_record: dict[str, Any], current_elements: list[dict[str, Any]]) -> None:
    execution_results = step_record.get("execution_results")
    if not isinstance(execution_results, list):
        return
    plan_actions = (step_record.get("plan") or {}).get("actions") or []
    expected_text = next(
        (
            str(action.get("text"))
            for action in plan_actions
            if isinstance(action, dict) and str(action.get("type", "")).lower() == "writetext" and action.get("text") is not None
        ),
        None,
    )
    if not expected_text:
        return
    current_texts = text_values_in_elements(current_elements)
    expected_visible = any(expected_text in text for text in current_texts)
    empty_result_hints = (
        "通过姓名或邮箱查找联系人",
        "没有找到",
        "暂无结果",
        "无结果",
    )
    result_empty_or_untriggered = any(any(hint in text for hint in empty_result_hints) for text in current_texts)
    verification = {
        "expected_text_length": len(expected_text),
        "expected_text_visible": expected_visible,
        "empty_or_untriggered_result_hint_visible": result_empty_or_untriggered,
        "observation_text_count": len(current_texts),
    }
    for result in execution_results:
        action = result.get("action") if isinstance(result, dict) else None
        if not isinstance(action, dict) or action.get("type") != "writetext":
            continue
        result["post_input_verification"] = verification
        diagnostics = result.setdefault("input_diagnostics", {})
        if isinstance(diagnostics, dict):
            diagnostics["post_input_verification"] = verification


def observation_signature(elements: list[dict[str, Any]]) -> str:
    stable_items: list[str] = []
    for element in elements:
        frame = element.get("frame") or {}
        stable_items.append(
            "|".join(
                [
                    str(element.get("source") or "ax"),
                    str(element.get("role") or ""),
                    str(element.get("text") or ""),
                    str(round(float(frame.get("x", 0) or 0))),
                    str(round(float(frame.get("y", 0) or 0))),
                    str(round(float(frame.get("width", 0) or 0))),
                    str(round(float(frame.get("height", 0) or 0))),
                ]
            )
        )
    digest = hashlib.sha256("\n".join(sorted(stable_items)).encode("utf-8")).hexdigest()
    return digest[:16]


def direct_ax_click_candidate(result: dict[str, Any]) -> bool:
    action = result.get("action") or {}
    return bool(result.get("ok")) and result.get("mode") == "direct_ax" and action.get("type") == "click"


def should_auto_coordinate_fallback_from_direct_ax(profile: AppProfile | None) -> bool:
    return app_profile_name(profile) != "feishu-lark"


def should_coordinate_fallback_after_direct_ax_failure(profile: AppProfile | None) -> bool:
    return app_profile_name(profile) != "feishu-lark"


def execute_coordinate_fallback_from_snapshot(
    action: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    target_pid: int,
    reason: str,
) -> dict[str, Any] | None:
    center = snapshot.get("center")
    if not isinstance(center, dict):
        return None
    try:
        x = float(center["x"])
        y = float(center["y"])
    except (KeyError, TypeError, ValueError):
        return None
    input_tool = tool_path("InputControllerTool")
    proc = run_command([input_tool, "click", f"{x:.1f}", f"{y:.1f}"], timeout=10)
    time.sleep(0.2)
    return {
        "index": 1,
        "action": action,
        "ok": True,
        "activated_pid": target_pid,
        "activation": "preserved_observation",
        "mode": "coordinate",
        "fallback_from": "direct_ax",
        "fallback_reason": reason,
        "point": {"x": x, "y": y},
        "stderr": proc.stderr[-1000:],
    }


def execute_plan(
    actions: list[dict[str, Any]],
    element_index: dict[str, UiElement],
    *,
    target_identifier: str,
    target_pid: int,
    app_profile: AppProfile | None = None,
) -> list[dict[str, Any]]:
    input_tool: str | None = None

    def get_input_tool() -> str:
        nonlocal input_tool
        if input_tool is None:
            input_tool = tool_path("InputControllerTool")
        return input_tool

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

        if action_type in {"click", "doubleclick", "rightclick", "mousemove"}:
            current_input_tool = get_input_tool()
            element = action_element(action, element_index)
            activated_pid, activation = maybe_activate_for_action(
                action_type=action_type,
                element=element,
                target_identifier=target_identifier,
                target_pid=target_pid,
                preserve_observation=(element is None and action.get("source") == "visual"),
            )
            ax_pid = target_pid
            if action_type == "click" and element is not None and element.ax_path is not None:
                try:
                    proc = run_command([current_input_tool, "axactivate", str(ax_pid), element.ax_path], timeout=10)
                    results.append({
                        "index": i,
                        "action": action,
                        "ok": True,
                        "activated_pid": activated_pid,
                        "activation": activation,
                        "target_pid": ax_pid,
                        "mode": "direct_ax",
                        "ax_path": element.ax_path,
                        "stderr": proc.stderr[-1000:],
                    })
                    time.sleep(0.2)
                    continue
                except subprocess.CalledProcessError as exc:
                    direct_ax_error = (exc.stderr or exc.stdout or str(exc)).strip()[-1000:]
                    if is_feishu_lark_context(target_identifier, app_profile):
                        results.append(
                            {
                                "index": i,
                                "action": action,
                                "ok": False,
                                "activated_pid": activated_pid,
                                "activation": activation,
                                "target_pid": ax_pid,
                                "mode": "direct_ax",
                                "ax_path": element.ax_path,
                                "fallback_skipped": "coordinate_fallback_disabled_for_feishu_lark",
                                "error": direct_ax_error,
                            }
                        )
                        time.sleep(0.2)
                        continue
                    print(
                        "warning: direct AX activation failed; falling back to coordinate input: "
                        f"{direct_ax_error[-800:]}",
                        file=sys.stderr,
                    )
            x, y = action_point(action, element_index)
            proc = run_command([current_input_tool, action_type, f"{x:.1f}", f"{y:.1f}"], timeout=10)
            results.append(
                {
                    "index": i,
                    "action": action,
                    "ok": True,
                    "activated_pid": activated_pid,
                    "activation": activation,
                    "mode": "coordinate",
                    "point": {"x": x, "y": y},
                    "stderr": proc.stderr[-1000:],
                }
            )
            time.sleep(0.2)
            continue
        if action_type == "scroll":
            current_input_tool = get_input_tool()
            element = action_element(action, element_index)
            activated_pid, activation = maybe_activate_for_action(
                action_type=action_type,
                element=element,
                target_identifier=target_identifier,
                target_pid=target_pid,
                preserve_observation=(element is None and action.get("source") == "visual"),
            )
            x, y = action_point(action, element_index)
            delta_y = int(action.get("deltaY", action.get("delta_y", 5)))
            delta_x = int(action.get("deltaX", action.get("delta_x", 0)))
            proc = run_command([current_input_tool, "scroll", f"{x:.1f}", f"{y:.1f}", str(delta_y), str(delta_x)], timeout=10)
            results.append(
                {
                    "index": i,
                    "action": action,
                    "ok": True,
                    "activated_pid": activated_pid,
                    "activation": activation,
                    "point": {"x": x, "y": y},
                    "stderr": proc.stderr[-1000:],
                }
            )
            time.sleep(0.2)
            continue
        if action_type == "writetext":
            text = str(action.get("text", ""))
            element = action_element(action, element_index)
            existing_text_match = text_already_present_in_text_target(text, element, element_index)
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
                        "activation": "preserved_observation",
                        "skipped": "text_already_present_in_text_target",
                        "input_diagnostics": {
                            "skip_reason": "text_already_present_in_text_target",
                            "existing_text_match": existing_text_match,
                        },
                    }
                )
                time.sleep(0.2)
                continue
            current_input_tool = get_input_tool()
            activated_pid, activation = maybe_activate_for_action(
                action_type=action_type,
                element=element,
                target_identifier=target_identifier,
                target_pid=target_pid,
            )
            ax_pid = target_pid
            if text:
                prefer_event_input = should_prefer_event_text_input(
                    target_identifier=target_identifier,
                    app_profile=app_profile,
                    element=element,
                )
                input_diagnostics: dict[str, Any] = {
                    "preferred_input_method": "paste" if prefer_event_input else "direct_ax",
                    "text_length": len(text),
                    "element_id": element.element_id if element is not None else None,
                    "app_profile": app_profile_name(app_profile) or None,
                }
                if prefer_event_input:
                    try:
                        focus_diagnostics = focus_text_target(
                            input_tool=current_input_tool,
                            element=element,
                            target_pid=ax_pid,
                            allow_coordinate_fallback=not is_feishu_lark_context(target_identifier, app_profile),
                        )
                        if focus_diagnostics is not None:
                            input_diagnostics["focus"] = focus_diagnostics
                            if not focus_diagnostics.get("focus_ok"):
                                results.append(
                                    {
                                        "index": i,
                                        "action": {
                                            "type": "writetext",
                                            "element_id": element.element_id if element is not None else None,
                                            "text_length": len(text),
                                        },
                                        "ok": False,
                                        "activated_pid": activated_pid,
                                        "activation": activation,
                                        "mode": "focus_failed",
                                        "input_method": "clipboard_paste",
                                        "input_diagnostics": input_diagnostics,
                                        "error": str(focus_diagnostics.get("focus_error") or "failed to focus text target"),
                                    }
                                )
                                time.sleep(0.2)
                                continue
                        proc, paste_diagnostics = paste_text_via_clipboard(
                            input_tool=current_input_tool,
                            text=text,
                            replace_existing=True,
                        )
                        input_diagnostics.update(paste_diagnostics)
                        results.append(
                            {
                                "index": i,
                                "action": {
                                    "type": "writetext",
                                    "element_id": element.element_id if element is not None else None,
                                    "text_length": len(text),
                                },
                                "ok": True,
                                "activated_pid": activated_pid,
                                "activation": activation,
                                "mode": "paste",
                                "input_method": "clipboard_paste",
                                "input_diagnostics": input_diagnostics,
                                "stderr": proc.stderr[-1000:],
                            }
                        )
                        time.sleep(0.2)
                        continue
                    except subprocess.CalledProcessError as exc:
                        input_diagnostics["paste_error"] = (exc.stderr or exc.stdout or str(exc)).strip()[-1000:]
                        print(
                            "warning: clipboard paste input failed; falling back to keyboard text input: "
                            f"{input_diagnostics['paste_error']}",
                            file=sys.stderr,
                        )
                if element is not None and element.ax_path is not None and not prefer_event_input:
                    try:
                        proc = run_command([current_input_tool, "axsetvalue", str(ax_pid), element.ax_path, text], timeout=max(10, len(text) * 0.2))
                        results.append({
                            "index": i,
                            "action": {"type": "writetext", "element_id": element.element_id, "text_length": len(text)},
                            "ok": True,
                            "activated_pid": activated_pid,
                            "activation": activation,
                            "target_pid": ax_pid,
                            "mode": "direct_ax",
                            "input_method": "direct_ax_set_value",
                            "input_diagnostics": input_diagnostics,
                            "ax_path": element.ax_path,
                            "stderr": proc.stderr[-1000:],
                        })
                        time.sleep(0.2)
                        continue
                    except subprocess.CalledProcessError as exc:
                        print(
                            "warning: direct AX value set failed; falling back to keyboard text input: "
                            f"{(exc.stderr or exc.stdout).strip()[-800:]}",
                            file=sys.stderr,
                        )
                proc = run_command([current_input_tool, "writetext", text], timeout=max(10, len(text) * 0.2))
                results.append(
                    {
                        "index": i,
                        "action": {"type": "writetext", "text_length": len(text)},
                        "ok": True,
                        "activated_pid": activated_pid,
                        "activation": activation,
                        "mode": "keyboard",
                        "input_method": "keyboard_unicode",
                        "input_diagnostics": input_diagnostics,
                        "stderr": proc.stderr[-1000:],
                    }
                )
            else:
                results.append({"index": i, "action": action, "ok": True, "activated_pid": activated_pid, "activation": activation, "skipped": "empty text"})
            time.sleep(0.2)
            continue
        if action_type == "keypress":
            current_input_tool = get_input_tool()
            activated_pid, activation = maybe_activate_for_action(
                action_type=action_type,
                element=None,
                target_identifier=target_identifier,
                target_pid=target_pid,
            )
            key = str(action.get("key") or action.get("keys") or "").strip()
            if not key:
                raise ValueError(f"keypress action needs key: {action!r}")
            proc = run_command([current_input_tool, "keypress", key], timeout=10)
            results.append({"index": i, "action": action, "ok": True, "activated_pid": activated_pid, "activation": activation, "stderr": proc.stderr[-1000:]})
            time.sleep(0.2)
            continue
        raise ValueError(f"unhandled action type: {action_type}")
    return results


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_trace(run_log: dict[str, Any]) -> None:
    run_log["trace"] = tactile_trace.build_trace(run_log, platform="macos")


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


def build_step_observation(
    traversal: dict[str, Any],
    *,
    workflow_mode: str,
    app_profile: AppProfile,
    step_number: int,
    artifact_dir: Path,
    max_elements: int,
    max_ocr_lines: int,
    include_menus: bool,
    include_virtual_hints: bool,
    ocr_languages: str,
    ocr_recognition_level: str,
    visual_planning_enabled: bool = False,
    visual_max_width: int = 1280,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, UiElement], list[str]]:
    ax_elements, element_index = summarize_elements(
        traversal,
        max_elements=max_elements,
        include_menus=include_menus,
        include_virtual_hints=include_virtual_hints,
    )
    combined_elements = list(ax_elements)
    window_frame = window_region_from_traversal(traversal)
    screenshot_path: str | None = None
    ocr_payload: dict[str, Any] | None = None
    ocr_error: str | None = None
    ocr_elements: list[dict[str, Any]] = []
    profile_region_elements: list[dict[str, Any]] = []
    planner_images: list[str] = []
    visual_observation: dict[str, Any] = {
        "enabled": visual_planning_enabled,
        "image_attached_to_planner": False,
        "screenshot_path": None,
        "planner_image_path": None,
        "error": None,
    }

    # Capture once per step for local OCR. Attaching that screenshot to the
    # planner remains controlled separately by visual_planning_enabled.
    should_capture_screenshot = window_frame is not None
    if visual_planning_enabled and window_frame is None:
        visual_observation["error"] = "no visible AXWindow frame available for screenshot capture"
    if should_capture_screenshot:
        screenshot = artifact_dir / f"step-{step_number:02d}-screenshot.png"
        try:
            capture_region(window_frame, screenshot)
            screenshot_path = os.fspath(screenshot)
        except Exception as exc:
            screenshot_path = None
            ocr_error = str(exc)
            visual_observation["error"] = str(exc)
            print(f"warning: screenshot capture failed: {str(exc)[-800:]}", file=sys.stderr)

    if screenshot_path is not None and window_frame is not None:
        try:
            screenshot = Path(screenshot_path)
            ocr_payload = run_local_ocr(screenshot, ocr_languages, ocr_recognition_level)
            add_screen_frames_to_ocr_payload(ocr_payload, window_frame)
            ocr_payload["source"] = {
                "kind": "pid_window",
                "region": {
                    "x": window_frame[0],
                    "y": window_frame[1],
                    "width": window_frame[2],
                    "height": window_frame[3],
                },
                "screenshot": screenshot_path,
            }
            ocr_elements = summarize_ocr_lines(ocr_payload, element_index, max_lines=max_ocr_lines)
            combined_elements.extend(ocr_elements)
        except Exception as exc:
            ocr_error = str(exc)
            print(f"warning: OCR observation failed: {ocr_error[-800:]}", file=sys.stderr)

    if visual_planning_enabled and screenshot_path is not None and window_frame is not None:
        try:
            planner_image_path = prepare_visual_planner_image(
                Path(screenshot_path),
                artifact_dir=artifact_dir,
                step_number=step_number,
                max_width=visual_max_width,
            )
            planner_images.append(image_file_base64(planner_image_path))
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
                    },
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

    if workflow_mode == "ax-poor" and window_frame is not None:
        profile_region_elements = add_profile_regions(
            profile_regions_for_window(app_profile, window_frame),
            element_index,
        )
        combined_elements.extend(profile_region_elements)

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
    return observation, combined_elements, element_index, planner_images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan and optionally execute macOS UI actions from a natural-language instruction.")
    parser.add_argument("instruction", nargs="?", default="", help="Natural-language instruction for the target app.")
    parser.add_argument("--target", default=None, help="Optional app name, bundle id, or .app path override. By default the app is inferred from the instruction.")
    parser.add_argument("--list-apps", action="store_true", help="List discovered local apps and exit.")
    parser.add_argument("--match", help="With --list-apps, regex or literal text matched against app names, aliases, bundle IDs, paths, and running processes.")
    parser.add_argument("--compact", action="store_true", help="With --list-apps, print concise app records and merge matching running processes into installed apps.")
    parser.add_argument("--best", action="store_true", help="With --list-apps, print only the preferred matching app record. Implies compact ranking.")
    parser.add_argument("--limit", type=int, help="With --list-apps, maximum number of records to print.")
    parser.add_argument("--model", default=None, help="Override model_name passed to utils.llm_config.call_llm.")
    parser.add_argument("--mode", choices=WORKFLOW_MODES, default="auto", help="Workflow mode. auto chooses from fixed app profiles or the capability selector.")
    parser.add_argument("--capability-selection", choices=CAPABILITY_SELECTION_MODES, default="auto", help="How auto mode chooses app capabilities. auto uses fixed profiles for known apps and asks the LLM for unknown apps.")
    parser.add_argument("--visual-planning", choices=VISUAL_PLANNING_MODES, default="auto", help="Attach screenshots to the planner. auto enables it for AX-poor/profile-selected apps.")
    parser.add_argument("--visual-max-width", type=int, default=1280, help="Maximum width for planner screenshot images. Use 0 to attach the original capture.")
    parser.add_argument("--max-elements", type=int, default=180, help="Maximum summarized UI elements sent to the LLM.")
    parser.add_argument("--max-ocr-lines", type=int, default=80, help="Maximum OCR lines included for AX-poor workflow observations.")
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum observe-plan-act iterations when --execute is enabled.")
    parser.add_argument("--max-actions-per-step", type=int, default=1, help="Maximum actions the LLM may return for one observation step. Defaults to 1 so every action is followed by a fresh traversal.")
    parser.add_argument("--include-menus", action="store_true", help="Include AX menu elements in the LLM observation payload.")
    parser.add_argument("--no-virtual-hints", action="store_true", help="Disable generated coordinate hints for common search/input regions when AX does not expose real text fields.")
    parser.add_argument("--ocr-languages", default="zh-Hans,en-US", help="Comma-separated Vision OCR languages for AX-poor mode.")
    parser.add_argument("--ocr-recognition-level", choices=["accurate", "fast"], default="accurate", help="Vision OCR recognition level for AX-poor mode.")
    parser.add_argument("--debug-ax-grid", action="store_true", help=f"Draw a temporary red AX element grid for the target app on every workflow observation. Can also be enabled with {DEBUG_AX_GRID_ENV}=1.")
    parser.add_argument("--debug-ax-grid-duration", type=float, help=f"Seconds to keep the red AX grid visible. Defaults to {DEFAULT_DEBUG_AX_GRID_DURATION} or {DEBUG_AX_GRID_DURATION_ENV}.")
    parser.add_argument("--debug-observation", action="store_true", help="Print summarized element ids, roles, text, and frames before planning each step.")
    parser.add_argument("--execute", action="store_true", help="Execute the planned actions. Without this flag, only prints the plan.")
    parser.add_argument("--mock-plan", action="store_true", help="Skip the LLM call and use the deterministic fallback planner.")
    parser.add_argument("--no-fallback", action="store_true", help="Fail if the LLM call or plan parsing fails.")
    parser.add_argument("--plan-output", type=Path, default=None, help="Optional path to write the full run log JSON.")
    parser.add_argument("--traversal-output", type=Path, default=None, help="Optional path to write the latest raw traversal JSON.")
    args = parser.parse_args(argv)

    if args.list_apps:
        records = app_candidate_records(
            discover_apps(),
            match=args.match,
            compact=args.compact,
            best=args.best,
            limit=args.limit,
        )
        payload: Any = records[0] if args.best and records else (None if args.best else records)
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.instruction.strip():
        parser.error("instruction is required unless --list-apps is used")

    if args.execute and args.plan_output is None:
        args.plan_output = default_artifact_path("workflow-run", ".json", cwd=Path.cwd())
    args.plan_output = session_scoped_output_path(args.plan_output)
    args.traversal_output = session_scoped_output_path(args.traversal_output)

    ensure_tools()
    target_identifier, target_resolution = resolve_app_identifier(args.instruction, args.target)
    print(
        "target app: "
        f"{target_resolution.get('display_name', target_identifier)} "
        f"(matched: {target_resolution.get('matched_alias', target_resolution.get('input', ''))}, "
        f"identifier: {target_identifier})",
        file=sys.stderr,
    )
    pid = open_or_activate_app(target_identifier)
    app_profile = resolve_app_profile(target_identifier, target_resolution)
    workflow_mode, visual_planning = apply_capability_decision(
        requested_mode=args.mode,
        requested_visual_planning=args.visual_planning,
        profile=app_profile,
        decision=None,
    )
    artifact_dir = workflow_run_artifact_dir(args.plan_output, cwd=Path.cwd())
    capability_decision = profile_capability_decision(app_profile, workflow_mode, visual_planning)

    run_log: dict[str, Any] = {
        "target": {"identifier": target_identifier, "pid": pid, "resolution": target_resolution},
        "instruction": args.instruction,
        "execute": args.execute,
        "requested_mode": args.mode,
        "requested_capability_selection": args.capability_selection,
        "workflow_mode": workflow_mode,
        "app_profile": app_profile.name,
        "app_guide_path": app_profile.guide_path,
        "app_guide_warnings": list(app_guide_warnings()),
        "requested_visual_planning": args.visual_planning,
        "visual_planning": visual_planning,
        "capability_selection": capability_decision,
        "artifact_root": os.fspath(args.plan_output.parent if args.plan_output else session_artifact_dir(cwd=Path.cwd(), create=False)),
        "artifact_dir": os.fspath(artifact_dir),
        "plan_output": os.fspath(args.plan_output) if args.plan_output else None,
        "started_at": time.time(),
        "steps": [],
        "final_status": "running",
    }
    history: list[dict[str, Any]] = []
    max_steps = max(1, args.max_steps if args.execute else 1)
    previous_step_record: dict[str, Any] | None = None
    llm_capability_selection_done = False
    debug_ax_grid_enabled = args.debug_ax_grid or env_flag_enabled(DEBUG_AX_GRID_ENV)
    debug_ax_grid_duration = resolve_debug_ax_grid_duration(args.debug_ax_grid_duration)
    run_log["debug_ax_grid"] = {
        "enabled": debug_ax_grid_enabled,
        "duration": debug_ax_grid_duration if debug_ax_grid_enabled else None,
    }

    for step_number in range(1, max_steps + 1):
        traversal = traverse_app(pid, no_activate=True)
        if debug_ax_grid_enabled:
            launch_debug_ax_grid(
                pid,
                debug_ax_grid_duration,
                label=f"workflow step {step_number}",
                traversal=traversal,
                artifact_dir=artifact_dir,
            )
        app_profile = resolve_app_profile(target_identifier, target_resolution, traversal)
        should_select_with_llm = should_use_llm_capability_selection(
            args.capability_selection,
            app_profile,
            mock_plan=args.mock_plan,
        )
        if should_select_with_llm and not llm_capability_selection_done:
            llm_capability_selection_done = True
            try:
                capability_decision = choose_app_capabilities(
                    user_instruction=args.instruction,
                    target_identifier=target_identifier,
                    target_resolution=target_resolution,
                    traversal=traversal,
                    app_profile=app_profile,
                    artifact_dir=artifact_dir,
                    visual_max_width=args.visual_max_width,
                    include_menus=args.include_menus,
                    model=args.model,
                )
            except Exception as exc:
                fallback_mode = resolve_workflow_mode("auto", app_profile)
                fallback_visual = resolve_visual_planning("auto", fallback_mode, app_profile)
                capability_decision = normalize_capability_decision(
                    {
                        "workflow_mode": fallback_mode,
                        "visual_planning": fallback_visual,
                        "reason": f"LLM capability selection failed; using profile fallback: {exc}",
                    },
                    fallback_workflow_mode=fallback_mode,
                    fallback_visual_planning=fallback_visual,
                    source="profile-fallback",
                )
                capability_decision.update(
                    {
                        "profile": app_profile.name,
                        "profile_fixed_strategy": app_profile.fixed_strategy,
                        "error": str(exc),
                    }
                )
                print(f"warning: LLM capability selection failed, using profile fallback: {exc}", file=sys.stderr)
        elif not should_select_with_llm:
            profile_mode, profile_visual = apply_capability_decision(
                requested_mode=args.mode,
                requested_visual_planning=args.visual_planning,
                profile=app_profile,
                decision=None,
            )
            capability_decision = profile_capability_decision(app_profile, profile_mode, profile_visual)

        workflow_mode, visual_planning = apply_capability_decision(
            requested_mode=args.mode,
            requested_visual_planning=args.visual_planning,
            profile=app_profile,
            decision=capability_decision,
        )

        observation, elements, element_index, planner_images = build_step_observation(
            traversal,
            workflow_mode=workflow_mode,
            app_profile=app_profile,
            step_number=step_number,
            artifact_dir=artifact_dir,
            max_elements=args.max_elements,
            max_ocr_lines=args.max_ocr_lines,
            include_menus=args.include_menus,
            include_virtual_hints=not args.no_virtual_hints,
            ocr_languages=args.ocr_languages,
            ocr_recognition_level=args.ocr_recognition_level,
            visual_planning_enabled=visual_planning,
            visual_max_width=args.visual_max_width,
        )
        current_signature = observation_signature(elements)

        if previous_step_record is not None:
            verify_previous_text_input(previous_step_record, elements)

        if previous_step_record is not None and current_signature == previous_step_record.get("observation_signature_before"):
            execution_results = previous_step_record.get("execution_results") or []
            direct_ax_result = next((item for item in execution_results if isinstance(item, dict) and direct_ax_click_candidate(item)), None)
            if direct_ax_result is not None and should_auto_coordinate_fallback_from_direct_ax(app_profile):
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
                if isinstance(snapshot, dict):
                    fallback_result = execute_coordinate_fallback_from_snapshot(
                        action,
                        snapshot,
                        target_pid=pid,
                        reason="direct_ax_no_observation_change",
                    )
                    if fallback_result is not None:
                        execution_results.append(fallback_result)
                        previous_step_record["execution_results"] = execution_results
                        previous_step_record["direct_ax_noop_fallback"] = True
                        if history:
                            history[-1]["execution_results"] = execution_results
                        traversal = traverse_app(pid, no_activate=True)
                        observation, elements, element_index, planner_images = build_step_observation(
                            traversal,
                            workflow_mode=workflow_mode,
                            app_profile=app_profile,
                            step_number=step_number,
                            artifact_dir=artifact_dir,
                            max_elements=args.max_elements,
                            max_ocr_lines=args.max_ocr_lines,
                            include_menus=args.include_menus,
                            include_virtual_hints=not args.no_virtual_hints,
                            ocr_languages=args.ocr_languages,
                            ocr_recognition_level=args.ocr_recognition_level,
                            visual_planning_enabled=visual_planning,
                            visual_max_width=args.visual_max_width,
                        )
                        current_signature = observation_signature(elements)
            elif direct_ax_result is not None:
                previous_step_record["direct_ax_noop_fallback"] = False
                previous_step_record["direct_ax_noop_fallback_skipped"] = "disabled_for_app_profile"
                if history:
                    history[-1]["direct_ax_noop_fallback"] = False
                    history[-1]["direct_ax_noop_fallback_skipped"] = "disabled_for_app_profile"

        if args.traversal_output:
            write_json(args.traversal_output, traversal)
        if args.debug_observation:
            print_observation_debug(step_number, elements)
            write_json(artifact_dir / f"step-{step_number:02d}-observation.json", observation)
        plan = make_plan(
            args.instruction,
            target_identifier,
            traversal,
            elements,
            observation,
            element_index,
            history,
            step_number=step_number,
            max_steps=max_steps,
            max_actions_per_step=args.max_actions_per_step,
            workflow_mode=workflow_mode,
            app_profile=app_profile,
            model=args.model,
            mock_plan=args.mock_plan,
            allow_fallback=(not args.no_fallback and (not args.execute or args.mock_plan)),
            planner_images=planner_images,
        )
        actions = validate_plan(plan, element_index, max_actions_per_step=args.max_actions_per_step)
        plan["actions"] = actions

        step_record: dict[str, Any] = {
            "step": step_number,
            "target": {"app": traversal.get("app_name", target_identifier), "pid": pid},
            "workflow_mode": workflow_mode,
            "app_profile": app_profile.name,
            "app_guide_path": app_profile.guide_path,
            "capability_selection": capability_decision,
            "visual_planning": visual_planning,
            "element_count_sent_to_llm": len(elements),
            "traversal_stats": traversal.get("stats", {}),
            "observation_signature_before": current_signature,
            "observation_sources": {
                "ax_elements": len(observation.get("ax_elements") or []),
                "ocr_lines": len(observation.get("ocr_lines") or []),
                "profile_regions": len(observation.get("profile_regions") or []),
                "screenshot_path": observation.get("screenshot_path"),
                "visual_observation": observation.get("visual_observation"),
            },
            "plan": plan,
        }
        action_elements = action_element_snapshots(actions, element_index)
        step_record["action_elements"] = action_elements
        if args.debug_observation:
            step_record["observation"] = observation
        run_log["steps"].append(step_record)
        run_log["workflow_mode"] = workflow_mode
        run_log["app_profile"] = app_profile.name
        run_log["app_guide_path"] = app_profile.guide_path
        run_log["app_guide_warnings"] = list(app_guide_warnings())
        run_log["capability_selection"] = capability_decision
        run_log["visual_planning"] = visual_planning

        status = str(plan.get("status", "continue")).lower()
        if not args.execute:
            run_log["final_status"] = "dry_run"
            break

        execution_results = execute_plan(actions, element_index, target_identifier=target_identifier, target_pid=pid, app_profile=app_profile)
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

        previous_step_record = step_record
        if args.plan_output:
            refresh_trace(run_log)
            write_json(args.plan_output, run_log)
    else:
        run_log["final_status"] = "max_steps_reached"

    run_log["completed_at"] = time.time()
    refresh_trace(run_log)
    summary = {
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
        "plan_output": run_log.get("plan_output"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.plan_output:
        write_json(args.plan_output, run_log)

    if not args.execute:
        print("dry-run only; pass --execute to operate the UI", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
