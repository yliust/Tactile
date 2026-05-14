#!/usr/bin/env python3
"""Small composable interfaces for the skill-local MacosUseSDK runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
if os.fspath(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SCRIPTS_ROOT))

from utils import artifacts as artifact_utils
from utils import tactile_trace

DEFAULT_REPO = SKILL_ROOT / "scripts" / "MacosUseSDK"
DEFAULT_WORKFLOW_DIR = SCRIPTS_ROOT / "workflows"
ALL_PRODUCTS = (
    "AppOpenerTool",
    "TraversalTool",
    "HighlightTraversalTool",
    "InputControllerTool",
    "VisualInputTool",
    "VirtualCursorTool",
)
CORE_TOOL_DIR_ENV = "TACTILE_MACOS_TOOL_DIR"
DEBUG_AX_GRID_ENV = "TACTILE_DEBUG_AX_GRID"
DEBUG_AX_GRID_DURATION_ENV = "TACTILE_DEBUG_AX_GRID_DURATION"
DEFAULT_DEBUG_AX_GRID_DURATION = 1.5
CURSOR_CLICK_PRE_EFFECT_SECONDS = 0.08
CURSOR_SETTLE_WHEN_HIDDEN_SECONDS = 0.72
CURSOR_SETTLE_MIN_SECONDS = 0.10
CURSOR_SETTLE_MAX_SECONDS = 1.05
CURSOR_MOTION_BASE_SECONDS = 1.280 / 1.45
ARTIFACT_SUBDIR = artifact_utils.ARTIFACT_SUBDIR
default_artifact_path = artifact_utils.default_artifact_path
find_workspace_root = artifact_utils.find_workspace_root
is_temporary_path = artifact_utils.is_temporary_path
safe_path_component = artifact_utils.safe_path_component
session_artifact_dir = artifact_utils.session_artifact_dir
session_scoped_output_path = artifact_utils.session_scoped_output_path
tempfile = artifact_utils.tempfile


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


def arg_list_has_option(values: list[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in values)


def env_flag_enabled(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def debug_ax_grid_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "debug_ax_grid", False)) or env_flag_enabled(DEBUG_AX_GRID_ENV)


def debug_ax_grid_duration(args: argparse.Namespace | None = None) -> float:
    explicit = getattr(args, "debug_ax_grid_duration", None) if args is not None else None
    if explicit is not None and explicit > 0:
        return float(explicit)
    raw = os.getenv(DEBUG_AX_GRID_DURATION_ENV)
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return DEFAULT_DEBUG_AX_GRID_DURATION


def repo_path(value: str | None) -> Path:
    return Path(value or DEFAULT_REPO).expanduser().resolve()


def run(
    cmd: list[str],
    *,
    repo: Path,
    check: bool = True,
    capture: bool = False,
    timeout: float | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "cwd": repo,
        "text": True,
        "check": check,
        "timeout": timeout,
    }
    if env is not None:
        kwargs["env"] = env
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    if input_text is not None:
        kwargs["input"] = input_text
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.CalledProcessError as exc:
        if capture:
            if exc.stdout:
                sys.stderr.write(exc.stdout)
            if exc.stderr:
                sys.stderr.write(exc.stderr)
        raise


def core_tool(product: str) -> Path | None:
    raw = os.getenv(CORE_TOOL_DIR_ENV)
    if not raw:
        return None
    path = Path(raw).expanduser() / product
    if path.exists():
        return path
    return None


def fallback_scratch_path(repo: Path) -> Path:
    return Path("/tmp") / f"tactile-macos-swift-{safe_path_component(os.fspath(repo))}"


def newest_existing_path(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def debug_tool(repo: Path, product: str) -> Path:
    configured = core_tool(product)
    if configured is not None:
        return configured
    default_path = repo / ".build" / "debug" / product
    fallback_path = fallback_scratch_path(repo) / "debug" / product
    if newest := newest_existing_path([default_path, fallback_path]):
        return newest
    return default_path


def latest_swift_source_mtime(repo: Path) -> float:
    candidates = [repo / "Package.swift", *(repo / "Sources").rglob("*.swift")]
    return max((path.stat().st_mtime for path in candidates if path.exists()), default=0.0)


def product_is_current(repo: Path, product: str) -> bool:
    path = debug_tool(repo, product)
    return path.exists() and path.stat().st_mtime >= latest_swift_source_mtime(repo)


def state_dir(repo: Path) -> Path:
    return repo / ".state"


def cursor_state_path(repo: Path) -> Path:
    return state_dir(repo) / "virtual-cursor.json"


def cursor_pid_path(repo: Path) -> Path:
    return state_dir(repo) / "virtual-cursor.pid"


def cursor_build_stamp_path(repo: Path) -> Path:
    return state_dir(repo) / "virtual-cursor.buildstamp"


def ensure_state_dirs(repo: Path) -> None:
    state_dir(repo).mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_cursor_state(
    repo: Path,
    point: dict[str, float] | None,
    *,
    event: str = "idle",
    label: str | None = None,
    visible: bool = True,
) -> None:
    write_json(
        cursor_state_path(repo),
        {
            "visible": visible,
            "point": point or {"x": 0.0, "y": 0.0},
            "event": event,
            "label": label,
            "updatedAt": time.time(),
        },
    )


def read_cursor_pid(repo: Path) -> int | None:
    try:
        return int(cursor_pid_path(repo).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def write_cursor_pid(repo: Path, pid: int) -> None:
    ensure_state_dirs(repo)
    cursor_pid_path(repo).write_text(f"{pid}\n", encoding="utf-8")


def clear_cursor_pid(repo: Path) -> None:
    try:
        cursor_pid_path(repo).unlink()
    except FileNotFoundError:
        pass


def read_cursor_build_stamp(repo: Path) -> str | None:
    try:
        return cursor_build_stamp_path(repo).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def write_cursor_build_stamp(repo: Path, stamp: str) -> None:
    ensure_state_dirs(repo)
    cursor_build_stamp_path(repo).write_text(f"{stamp}\n", encoding="utf-8")


def clear_cursor_build_stamp(repo: Path) -> None:
    try:
        cursor_build_stamp_path(repo).unlink()
    except FileNotFoundError:
        pass


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def ensure_cursor_process(repo: Path) -> None:
    ensure_products(repo, ["VirtualCursorTool"])
    cursor_tool = debug_tool(repo, "VirtualCursorTool")
    build_stamp = str(cursor_tool.stat().st_mtime_ns)
    pid = read_cursor_pid(repo)
    if pid and pid_is_alive(pid) and read_cursor_build_stamp(repo) == build_stamp:
        return
    if pid and pid_is_alive(pid):
        stop_pid(pid)
        time.sleep(0.05)
    clear_cursor_pid(repo)
    clear_cursor_build_stamp(repo)
    if not cursor_state_path(repo).exists():
        write_cursor_state(repo, {"x": 0.0, "y": 0.0}, event="idle", visible=False)
    proc = subprocess.Popen(
        [os.fspath(cursor_tool), os.fspath(cursor_state_path(repo))],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_cursor_pid(repo, proc.pid)
    write_cursor_build_stamp(repo, build_stamp)


def cursor_status_payload(repo: Path) -> dict[str, Any]:
    pid = read_cursor_pid(repo)
    state_payload = None
    path = cursor_state_path(repo)
    if path.exists():
        try:
            state_payload = read_json(path)
        except (OSError, json.JSONDecodeError):
            state_payload = None
    return {
        "ok": True,
        "running": bool(pid and pid_is_alive(pid)),
        "pid": pid,
        "statePath": os.fspath(path),
        "state": state_payload,
    }


def last_visible_cursor_point(repo: Path) -> dict[str, float] | None:
    path = cursor_state_path(repo)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("visible") is not True:
        return None
    point = payload.get("point")
    if not isinstance(point, dict):
        return None
    try:
        return {"x": float(point["x"]), "y": float(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def cursor_settle_delay(repo: Path, point: dict[str, float]) -> float:
    previous = last_visible_cursor_point(repo)
    if previous is None:
        return CURSOR_SETTLE_WHEN_HIDDEN_SECONDS

    distance = math.hypot(float(point["x"]) - previous["x"], float(point["y"]) - previous["y"])
    if distance < 2.0:
        return CURSOR_SETTLE_MIN_SECONDS

    factor = max(0.55, min(1.80, distance / 520.0))
    delay = max(0.42, CURSOR_MOTION_BASE_SECONDS * factor)
    return min(max(delay, CURSOR_SETTLE_MIN_SECONDS), CURSOR_SETTLE_MAX_SECONDS)


def prepare_cursor_click(repo: Path, point: dict[str, float], *, settle_seconds: float | None = None) -> None:
    ensure_cursor_process(repo)
    delay = cursor_settle_delay(repo, point) if settle_seconds is None else max(0.0, settle_seconds)
    write_cursor_state(repo, point, event="move", visible=True)
    time.sleep(delay)
    write_cursor_state(repo, point, event="click", visible=True)
    time.sleep(CURSOR_CLICK_PRE_EFFECT_SECONDS)


def ensure_products(repo: Path, products: list[str]) -> None:
    for product in products:
        if product_is_current(repo, product):
            continue
        print(f"building Swift product: {product}", file=sys.stderr)
        try:
            run(["swift", "build", "--product", product], repo=repo, capture=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
            if "no_warn_duplicate_libraries" not in output:
                if exc.stdout:
                    sys.stderr.write(exc.stdout)
                if exc.stderr:
                    sys.stderr.write(exc.stderr)
                raise
            print(
                "warning: default Swift toolchain emitted an unsupported linker flag; retrying with Xcode default toolchain",
                file=sys.stderr,
            )
            env = dict(os.environ)
            env["TOOLCHAINS"] = "com.apple.dt.toolchain.XcodeDefault"
            run(
                [
                    "xcrun",
                    "swift",
                    "build",
                    "--scratch-path",
                    os.fspath(fallback_scratch_path(repo)),
                    "--product",
                    product,
                ],
                repo=repo,
                capture=True,
                timeout=120,
                env=env,
            )


def launch_debug_ax_grid(
    repo: Path,
    pid: int,
    duration: float,
    *,
    traversal: dict[str, Any] | None = None,
) -> subprocess.Popen[str] | None:
    try:
        ensure_products(repo, ["HighlightTraversalTool"])
        cmd = [os.fspath(debug_tool(repo, "HighlightTraversalTool"))]
        if traversal is not None:
            input_json = default_artifact_path("debug-ax-grid-traversal", ".json", cwd=Path.cwd())
            input_json.parent.mkdir(parents=True, exist_ok=True)
            input_json.write_text(json.dumps(traversal, ensure_ascii=False), encoding="utf-8")
            cmd.extend(["--input-json", os.fspath(input_json)])
        else:
            cmd.extend([str(pid), "--no-activate"])
        cmd.extend(["--duration", str(duration)])
        proc = subprocess.Popen(
            cmd,
            cwd=repo,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"debug: AX grid overlay launched for pid {pid} ({duration}s)", file=sys.stderr)
        return proc
    except Exception as exc:
        print(f"warning: failed to launch AX grid overlay for pid {pid}: {exc}", file=sys.stderr)
        return None


def maybe_launch_debug_ax_grid(repo: Path, args: argparse.Namespace, pid: int | None) -> None:
    if not debug_ax_grid_requested(args):
        return
    if pid is None:
        print("warning: --debug-ax-grid requested but no target pid is available for this command", file=sys.stderr)
        return
    launch_debug_ax_grid(repo, pid, debug_ax_grid_duration(args))


def load_workflow_module(repo: Path):
    module_path = DEFAULT_WORKFLOW_DIR / "codex_llm_workflow.py"
    previous_swift_package = os.environ.get("TACTILE_MACOS_SWIFT_PACKAGE")
    os.environ["TACTILE_MACOS_SWIFT_PACKAGE"] = os.fspath(repo)
    spec = importlib.util.spec_from_file_location("_macos_app_workflow_codex_llm_workflow", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load workflow module from {module_path}")
    workflow = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = workflow
    try:
        spec.loader.exec_module(workflow)
    finally:
        if previous_swift_package is None:
            os.environ.pop("TACTILE_MACOS_SWIFT_PACKAGE", None)
        else:
            os.environ["TACTILE_MACOS_SWIFT_PACKAGE"] = previous_swift_package
    return workflow


def load_feishu_fast_module():
    module_path = SCRIPTS_ROOT / "feishu_fast.py"
    spec = importlib.util.spec_from_file_location("_macos_app_workflow_feishu_fast", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load Feishu fast module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_app_exploration_module():
    module_path = SCRIPTS_ROOT / "app_exploration.py"
    spec = importlib.util.spec_from_file_location("_macos_app_exploration", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load app exploration module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_or_print(data: Any, output: Path | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    output = session_scoped_output_path(output)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")


def write_text_or_print(text: str, output: Path | None) -> None:
    output = session_scoped_output_path(output)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")


def write_jsonl_or_print(rows: list[dict[str, Any]], output: Path | None) -> Path | None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    output = session_scoped_output_path(output)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
        return output
    print(text, end="")
    return None


def attach_ax_paths(elements: list[dict[str, Any]], element_index: dict[str, Any]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for element in elements:
        item = dict(element)
        ui_element = element_index.get(str(item.get("id")))
        if ui_element is not None and getattr(ui_element, "ax_path", None):
            item["ax_path"] = ui_element.ax_path
        enriched.append(item)
    return enriched


def add_global(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=None,
        help="MacosUseSDK Swift package root. Defaults to this skill's scripts/MacosUseSDK directory.",
    )
    parser.add_argument(
        "--debug-ax-grid",
        action="store_true",
        help=f"Draw a temporary red AX element grid for the target pid. Can also be enabled with {DEBUG_AX_GRID_ENV}=1.",
    )
    parser.add_argument(
        "--debug-ax-grid-duration",
        type=float,
        help=f"Seconds to keep the red AX grid visible. Defaults to {DEFAULT_DEBUG_AX_GRID_DURATION} or {DEBUG_AX_GRID_DURATION_ENV}.",
    )


def cmd_build(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    products = args.products or list(ALL_PRODUCTS)
    ensure_products(repo, products)
    for product in products:
        print(debug_tool(repo, product))
    return 0


def cmd_tool_path(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_products(repo, [args.product])
    print(debug_tool(repo, args.product))
    return 0


def cmd_list_apps(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    workflow = load_workflow_module(repo)
    records = workflow.app_candidate_records(
        workflow.discover_apps(),
        match=args.match,
        compact=args.compact,
        best=args.best,
        limit=args.limit,
    )
    payload = records[0] if args.best and records else (None if args.best else records)
    write_or_print(payload, args.output)
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    workflow = load_workflow_module(repo)
    identifier, details = workflow.resolve_app_identifier(args.instruction, args.target)
    write_or_print({"identifier": identifier, "details": details}, args.output)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_products(repo, ["AppOpenerTool"])
    proc = run([os.fspath(debug_tool(repo, "AppOpenerTool")), args.identifier], repo=repo, capture=True, timeout=30)
    pid_text = proc.stdout.strip()
    pid = int(pid_text)
    maybe_launch_debug_ax_grid(repo, args, pid)
    if args.json:
        write_or_print({"identifier": args.identifier, "pid": pid}, args.output)
    elif args.output:
        output = session_scoped_output_path(args.output)
        assert output is not None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(pid_text + "\n", encoding="utf-8")
        print(output)
    else:
        print(pid_text)
    return 0


def cmd_traverse(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_products(repo, ["TraversalTool"])
    cmd = [os.fspath(debug_tool(repo, "TraversalTool"))]
    if args.visible_only:
        cmd.append("--visible-only")
    if args.no_activate:
        cmd.append("--no-activate")
    cmd.append(str(args.pid))
    proc = run(cmd, repo=repo, capture=True, timeout=20)
    traversal = json.loads(proc.stdout)
    if debug_ax_grid_requested(args):
        launch_debug_ax_grid(repo, args.pid, debug_ax_grid_duration(args), traversal=traversal)
    if args.summary:
        workflow = load_workflow_module(repo)
        elements, element_index = workflow.summarize_elements(
            traversal,
            max_elements=args.max_elements,
            include_menus=args.include_menus,
            include_virtual_hints=not args.no_virtual_hints,
        )
        payload = {
            "app_name": traversal.get("app_name"),
            "stats": traversal.get("stats", {}),
            "elements": attach_ax_paths(elements, element_index),
        }
        write_or_print(payload, args.output)
    else:
        write_or_print(traversal, args.output)
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_products(repo, ["AppOpenerTool", "TraversalTool"])
    workflow = load_workflow_module(repo)
    if args.pid is not None:
        pid = args.pid
        identifier = args.target or str(pid)
        details = {"mode": "pid", "input": args.target, "identifier": identifier, "pid": pid}
    else:
        identifier, details = workflow.resolve_app_identifier(args.instruction or args.target, args.target)
        pid = workflow.open_or_activate_app(identifier)
    traversal = workflow.traverse_app(pid, no_activate=args.no_activate)
    if debug_ax_grid_requested(args):
        launch_debug_ax_grid(repo, pid, debug_ax_grid_duration(args), traversal=traversal)
    elements, element_index = workflow.summarize_elements(
        traversal,
        max_elements=args.max_elements,
        include_menus=args.include_menus,
        include_virtual_hints=not args.no_virtual_hints,
    )
    write_or_print(
        {
            "target": {"identifier": identifier, "pid": pid, "resolution": details},
            "app_name": traversal.get("app_name"),
            "stats": traversal.get("stats", {}),
            "elements": attach_ax_paths(elements, element_index),
        },
        args.output,
    )
    return 0


def cmd_highlight(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_products(repo, ["HighlightTraversalTool"])
    cmd = [
        os.fspath(debug_tool(repo, "HighlightTraversalTool")),
        str(args.pid),
        "--no-activate",
        "--duration",
        str(args.duration),
    ]
    return run(cmd, repo=repo).returncode


def cmd_input(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    product = "VisualInputTool" if args.visual else "InputControllerTool"
    ensure_products(repo, [product])
    action_args = args.action_args[1:] if args.action_args[:1] == ["--"] else args.action_args
    maybe_launch_debug_ax_grid(repo, args, args.debug_ax_grid_pid)
    cmd = [os.fspath(debug_tool(repo, product)), args.action, *action_args]
    if args.visual and args.duration is not None:
        cmd.extend(["--duration", str(args.duration)])
    return run(cmd, repo=repo).returncode


def cmd_ax(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    products = ["InputControllerTool"]
    if args.action in {"axactivate", "axpress"} and env_flag_enabled("TACTILE_VIRTUAL_CURSOR_ENABLED"):
        products.append("VirtualCursorTool")
    ensure_products(repo, products)
    if args.action == "axsetvalue" and args.value is None:
        raise SystemExit("axsetvalue requires a value argument")
    maybe_launch_debug_ax_grid(repo, args, args.pid)
    cmd = [os.fspath(debug_tool(repo, "InputControllerTool")), args.action, str(args.pid), args.ax_path]
    if args.value is not None:
        cmd.append(args.value)
    return run(cmd, repo=repo).returncode


def parse_region(value: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise SystemExit("--region must be formatted as x,y,width,height")
    try:
        x, y, width, height = (float(part) for part in parts)
    except ValueError as exc:
        raise SystemExit("--region values must be numbers") from exc
    if width <= 0 or height <= 0:
        raise SystemExit("--region width and height must be positive")
    return x, y, width, height


def region_move_test_waypoints(region: tuple[float, float, float, float]) -> list[dict[str, float]]:
    x, y, width, height = region
    pad_x = min(width * 0.18, 140.0)
    pad_y = min(height * 0.18, 100.0)
    left = x + pad_x
    right = x + width - pad_x
    top = y + pad_y
    bottom = y + height - pad_y
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    return [
        {"x": center_x, "y": center_y},
        {"x": left, "y": top},
        {"x": right, "y": top},
        {"x": right, "y": bottom},
        {"x": left, "y": bottom},
        {"x": center_x, "y": center_y},
    ]


def fallback_move_test_waypoints(repo: Path) -> list[dict[str, float]]:
    point = last_visible_cursor_point(repo) or {"x": 240.0, "y": 240.0}
    center_x = float(point.get("x", 240.0))
    center_y = float(point.get("y", 240.0))
    radius = 90.0
    return [
        {"x": center_x, "y": center_y},
        {"x": center_x - radius, "y": center_y - radius * 0.55},
        {"x": center_x + radius, "y": center_y - radius * 0.55},
        {"x": center_x + radius, "y": center_y + radius * 0.55},
        {"x": center_x - radius, "y": center_y + radius * 0.55},
        {"x": center_x, "y": center_y},
    ]


def interpolate_waypoints(waypoints: list[dict[str, float]], steps: int) -> list[dict[str, float]]:
    if len(waypoints) < 2:
        return waypoints
    steps = max(2, steps)
    segments = len(waypoints) - 1
    points: list[dict[str, float]] = []
    for index in range(steps):
        progress = index / float(steps - 1)
        segment_position = min(progress * segments, segments - 1e-9)
        segment_index = min(int(segment_position), segments - 1)
        local_t = segment_position - segment_index
        start = waypoints[segment_index]
        end = waypoints[segment_index + 1]
        points.append(
            {
                "x": float(start["x"]) + (float(end["x"]) - float(start["x"])) * local_t,
                "y": float(start["y"]) + (float(end["y"]) - float(start["y"])) * local_t,
            }
        )
    points[-1] = waypoints[-1]
    return points


def cmd_cursor(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    ensure_state_dirs(repo)

    if args.cursor_command == "status":
        write_or_print(cursor_status_payload(repo), args.output)
        return 0

    if args.cursor_command == "start":
        write_cursor_state(repo, {"x": 0.0, "y": 0.0}, event="idle", visible=False)
        ensure_cursor_process(repo)
        write_or_print(cursor_status_payload(repo), args.output)
        return 0

    if args.cursor_command == "stop":
        pid = read_cursor_pid(repo)
        stopped = False
        if pid and pid_is_alive(pid):
            stopped = stop_pid(pid)
        clear_cursor_pid(repo)
        clear_cursor_build_stamp(repo)
        write_cursor_state(repo, {"x": 0.0, "y": 0.0}, event="stop", visible=False)
        write_or_print({"ok": True, "stopped": stopped, "pid": pid}, args.output)
        return 0

    if args.cursor_command in {"move", "click"}:
        if len(args.coords) != 2:
            raise SystemExit(f"cursor {args.cursor_command} requires <x> <y>")
        try:
            point = {"x": float(args.coords[0]), "y": float(args.coords[1])}
        except ValueError as exc:
            raise SystemExit("cursor coordinates must be numbers") from exc
        if args.cursor_command == "click":
            prepare_cursor_click(repo, point, settle_seconds=args.settle_ms / 1000.0 if args.settle_ms is not None else None)
            event = "click"
        else:
            ensure_cursor_process(repo)
            event = args.event or "move"
            write_cursor_state(repo, point, event=event, label=args.label, visible=True)
        write_or_print({"ok": True, "action": f"cursor.{args.cursor_command}", "event": event, "point": point}, args.output)
        return 0

    if args.cursor_command in {"move_test", "move-test"}:
        if args.duration <= 0:
            raise SystemExit("--duration must be > 0")
        if args.steps < 2:
            raise SystemExit("--steps must be >= 2")
        ensure_cursor_process(repo)
        if args.pid is not None:
            region = window_region_from_pid(repo, args.pid, args.window_index)
        elif args.region is not None:
            region = parse_region(args.region)
        else:
            region = None
        waypoints = region_move_test_waypoints(region) if region else fallback_move_test_waypoints(repo)
        points = interpolate_waypoints(waypoints, args.steps)
        delay = args.duration / max(len(points) - 1, 1)
        for point in points:
            write_cursor_state(repo, point, event="move_test", visible=True)
            time.sleep(delay)
        write_or_print(
            {
                "ok": True,
                "action": "cursor.move_test",
                "duration": args.duration,
                "steps": len(points),
                "region": {"x": region[0], "y": region[1], "width": region[2], "height": region[3]} if region else None,
                "start": points[0],
                "end": points[-1],
            },
            args.output,
        )
        return 0

    raise SystemExit(f"unsupported cursor command: {args.cursor_command}")


def window_region_from_pid(repo: Path, pid: int, window_index: int) -> tuple[float, float, float, float]:
    ensure_products(repo, ["TraversalTool"])
    proc = run(
        [os.fspath(debug_tool(repo, "TraversalTool")), "--visible-only", "--no-activate", str(pid)],
        repo=repo,
        capture=True,
        timeout=20,
    )
    traversal = json.loads(proc.stdout)
    windows = [
        element
        for element in traversal.get("elements", [])
        if "AXWindow" in str(element.get("role", ""))
        and all(element.get(key) is not None for key in ("x", "y", "width", "height"))
    ]
    if not windows:
        raise SystemExit(f"no visible AXWindow found for pid {pid}")
    try:
        window = windows[window_index]
    except IndexError as exc:
        raise SystemExit(f"window index {window_index} is out of range; found {len(windows)} window(s)") from exc
    return (float(window["x"]), float(window["y"]), float(window["width"]), float(window["height"]))


def capture_region(repo: Path, region: tuple[float, float, float, float], output: Path | None) -> Path:
    x, y, width, height = region
    image_path = session_scoped_output_path(output) or default_artifact_path("ocr-screenshot", ".png", cwd=Path.cwd())
    image_path.parent.mkdir(parents=True, exist_ok=True)
    region_arg = f"-R{int(round(x))},{int(round(y))},{int(round(width))},{int(round(height))}"
    run(["screencapture", "-x", region_arg, os.fspath(image_path)], repo=repo, capture=True, timeout=15)
    return image_path


def run_local_ocr(repo: Path, image_path: Path, languages: str, recognition_level: str) -> dict[str, Any]:
    proc = run(
        ["swift", "-", os.fspath(image_path), languages, recognition_level],
        repo=repo,
        capture=True,
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


def format_ocr_payload(payload: dict[str, Any], output_format: str) -> str:
    lines = payload.get("lines", [])
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output_format == "text":
        return "\n".join(str(line.get("text", "")) for line in lines).rstrip() + "\n"
    rows = []
    for line in lines:
        frame = line.get("screenFrame") or line.get("frame") or {}
        text = str(line.get("text", "")).replace("\n", " ")
        rows.append(
            "\t".join(
                [
                    f"{float(frame.get('x', 0)):.0f}",
                    f"{float(frame.get('y', 0)):.0f}",
                    f"{float(frame.get('width', 0)):.0f}",
                    f"{float(frame.get('height', 0)):.0f}",
                    f"{float(line.get('confidence', 0)):.2f}",
                    text,
                ]
            )
        )
    return "\n".join(rows).rstrip() + "\n"


def cmd_ocr(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    source_count = sum(value is not None for value in (args.image, args.region, args.pid))
    if source_count != 1:
        raise SystemExit("choose exactly one OCR source: --image, --region, or --pid")

    region: tuple[float, float, float, float] | None = None
    screenshot_path: Path | None = None
    if args.image is not None:
        image_path = args.image.expanduser().resolve()
        if not image_path.exists():
            raise SystemExit(f"image not found: {image_path}")
    else:
        region = parse_region(args.region) if args.region is not None else window_region_from_pid(repo, args.pid, args.window_index)
        if args.pid is not None:
            maybe_launch_debug_ax_grid(repo, args, args.pid)
        screenshot_path = capture_region(repo, region, args.screenshot_output)
        image_path = screenshot_path

    payload = run_local_ocr(repo, image_path, args.languages, args.recognition_level)
    add_screen_frames_to_ocr_payload(payload, region)
    payload["source"] = {
        "kind": "image" if args.image is not None else ("region" if args.region is not None else "pid_window"),
        "pid": args.pid,
        "window_index": args.window_index if args.pid is not None else None,
        "region": {"x": region[0], "y": region[1], "width": region[2], "height": region[3]} if region else None,
        "screenshot": os.fspath(screenshot_path) if screenshot_path else None,
    }
    if args.contains:
        payload["lines"] = [line for line in payload.get("lines", []) if args.contains in str(line.get("text", ""))]

    write_text_or_print(format_ocr_payload(payload, args.format), args.output)
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    script = DEFAULT_WORKFLOW_DIR / "llm_app_workflow.py"
    workflow_args = args.workflow_args[1:] if args.workflow_args[:1] == ["--"] else args.workflow_args
    if arg_list_has_option(workflow_args, "--execute") and not arg_list_has_option(workflow_args, "--plan-output"):
        workflow_args = [
            *workflow_args,
            "--plan-output",
            os.fspath(default_artifact_path("workflow-run", ".json", cwd=Path.cwd())),
        ]
    env = dict(os.environ)
    env["TACTILE_MACOS_SWIFT_PACKAGE"] = os.fspath(repo)
    if getattr(args, "debug_ax_grid", False):
        env[DEBUG_AX_GRID_ENV] = "1"
    if getattr(args, "debug_ax_grid_duration", None) is not None:
        env[DEBUG_AX_GRID_DURATION_ENV] = str(args.debug_ax_grid_duration)
    cmd = [sys.executable, os.fspath(script), args.instruction, *workflow_args]
    return run(cmd, repo=repo, env=env).returncode


def cmd_feishu_fast(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    module = load_feishu_fast_module()
    return module.dispatch(
        args,
        repo=repo,
        ensure_products=ensure_products,
        debug_tool=debug_tool,
        write_or_print=write_or_print,
    )


def cmd_artifact_dir(args: argparse.Namespace) -> int:
    print(session_artifact_dir(cwd=Path.cwd()))
    return 0


def cmd_plan_log(args: argparse.Namespace) -> int:
    data = json.loads(args.path.read_text(encoding="utf-8"))
    trace_summary = tactile_trace.trace_summary(data.get("trace"))
    steps = []
    for step in data.get("steps", []):
        execution = step.get("execution_results") or []
        steps.append(
            {
                "step": step.get("step"),
                "workflow_mode": step.get("workflow_mode"),
                "app_profile": step.get("app_profile"),
                "capability_selection": step.get("capability_selection"),
                "visual_planning": step.get("visual_planning"),
                "observation_sources": step.get("observation_sources"),
                "summary": (step.get("plan") or {}).get("summary"),
                "actions": (step.get("plan") or {}).get("actions"),
                "action_elements": step.get("action_elements") or [],
                "direct_ax_noop_fallback": step.get("direct_ax_noop_fallback", False),
                "execution": [
                    {
                        "ok": item.get("ok"),
                        "mode": item.get("mode"),
                        "action": item.get("action"),
                        "point": item.get("point"),
                        "fallback_from": item.get("fallback_from"),
                        "fallback_reason": item.get("fallback_reason"),
                        "input_method": item.get("input_method"),
                        "input_diagnostics": item.get("input_diagnostics"),
                        "post_input_verification": item.get("post_input_verification"),
                        "ax_path": item.get("ax_path"),
                        "skipped": item.get("skipped"),
                    }
                    for item in execution
                ],
            }
        )
    write_or_print(
        {
            "final_status": data.get("final_status"),
            "workflow_mode": data.get("workflow_mode"),
            "app_profile": data.get("app_profile"),
            "requested_capability_selection": data.get("requested_capability_selection"),
            "capability_selection": data.get("capability_selection"),
            "visual_planning": data.get("visual_planning"),
            "debug_ax_grid": data.get("debug_ax_grid"),
            "artifact_root": data.get("artifact_root"),
            "artifact_dir": data.get("artifact_dir"),
            "plan_output": data.get("plan_output") or os.fspath(args.path),
            "target": data.get("target"),
            "instruction": data.get("instruction"),
            "trace_summary": trace_summary,
            "steps": steps,
        },
        args.output,
    )
    return 0


def cmd_trace_replay(args: argparse.Namespace) -> int:
    write_or_print(tactile_trace.replay_trace_files(args.paths), args.output)
    return 0


def cmd_profile_app(args: argparse.Namespace) -> int:
    module = load_app_exploration_module()
    profile = module.profile_target(args.target, guide_dir=args.guide_dir or module.APP_GUIDE_DIR)
    write_or_print(profile, args.output)
    return 0


def cmd_catalog_actions(args: argparse.Namespace) -> int:
    module = load_app_exploration_module()
    profile = module.load_json_file(args.profile)
    catalog = module.catalog_from_profile(profile)
    write_or_print(catalog, args.output)
    return 0


def cmd_run_adapter(args: argparse.Namespace) -> int:
    module = load_app_exploration_module()
    inputs: dict[str, Any] = {}
    if args.inputs_json:
        loaded_inputs = json.loads(args.inputs_json)
        if not isinstance(loaded_inputs, dict):
            raise SystemExit("--inputs-json must decode to a JSON object")
        inputs = loaded_inputs
    result = module.run_adapter(
        args.app,
        args.task,
        strategy=args.strategy,
        verify=args.verify,
        catalog_path=args.catalog,
        inputs=inputs,
    )
    write_or_print(result, args.output)
    return 0


def cmd_eval_suite(args: argparse.Namespace) -> int:
    module = load_app_exploration_module()
    runs, summary = module.eval_suite(args.suite, strategy=args.strategy, runs=args.runs)
    if args.output:
        output = write_jsonl_or_print(runs, args.output)
        print(json.dumps({"output": os.fspath(output), "summary": summary}, ensure_ascii=False, indent=2))
    else:
        write_or_print({"runs": runs, "summary": summary}, None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build one or more Swift products.")
    add_global(build)
    build.add_argument("products", nargs="*", help="Product names. Defaults to the common runtime tools.")
    build.set_defaults(func=cmd_build)

    tool_path_parser = subparsers.add_parser("tool-path", help="Print the built path for one Swift product.")
    add_global(tool_path_parser)
    tool_path_parser.add_argument("product")
    tool_path_parser.set_defaults(func=cmd_tool_path)

    list_apps = subparsers.add_parser("list-apps", help="List app names, aliases, bundle IDs, and paths.")
    add_global(list_apps)
    list_apps.add_argument("--match", help="Regex or literal text matched against app names, aliases, bundle IDs, paths, and running processes.")
    list_apps.add_argument("--compact", action="store_true", help="Print concise app records and merge matching running processes into installed apps.")
    list_apps.add_argument("--best", action="store_true", help="Print only the preferred matching app record. Implies compact ranking.")
    list_apps.add_argument("--limit", type=int, help="Maximum number of records to print.")
    list_apps.add_argument("--output", type=Path)
    list_apps.set_defaults(func=cmd_list_apps)

    resolve = subparsers.add_parser("resolve", help="Resolve a natural-language task to a target app.")
    add_global(resolve)
    resolve.add_argument("instruction")
    resolve.add_argument("--target")
    resolve.add_argument("--output", type=Path)
    resolve.set_defaults(func=cmd_resolve)

    open_parser = subparsers.add_parser("open", help="Open or activate an app and print its PID.")
    add_global(open_parser)
    open_parser.add_argument("identifier", help="App name, bundle ID, or .app path.")
    open_parser.add_argument("--json", action="store_true", help="Print JSON instead of only the PID.")
    open_parser.add_argument("--output", type=Path)
    open_parser.set_defaults(func=cmd_open)

    traverse = subparsers.add_parser("traverse", help="Traverse a running app by PID.")
    add_global(traverse)
    traverse.add_argument("pid", type=int)
    traverse.add_argument("--visible-only", action="store_true", default=True)
    traverse.add_argument("--all", dest="visible_only", action="store_false", help="Include invisible/non-geometric elements.")
    traverse.add_argument("--no-activate", action="store_true", default=True)
    traverse.add_argument("--activate", dest="no_activate", action="store_false")
    traverse.add_argument("--summary", action="store_true", help="Print planner-ready compact elements.")
    traverse.add_argument("--max-elements", type=int, default=180)
    traverse.add_argument("--include-menus", action="store_true")
    traverse.add_argument("--no-virtual-hints", action="store_true")
    traverse.add_argument("--output", type=Path)
    traverse.set_defaults(func=cmd_traverse)

    observe = subparsers.add_parser("observe", help="Resolve/open an app, traverse it, and print a compact observation.")
    add_global(observe)
    observe.add_argument("instruction", nargs="?", default="", help="Instruction used only for target inference.")
    observe.add_argument("--target", help="Explicit app name, bundle ID, or .app path.")
    observe.add_argument("--pid", type=int, help="Traverse an existing app process without resolving/opening a target.")
    observe.add_argument("--no-activate", action="store_true", default=True, help="Do not activate the app before traversal.")
    observe.add_argument("--activate", dest="no_activate", action="store_false", help="Activate during traversal.")
    observe.add_argument("--max-elements", type=int, default=180)
    observe.add_argument("--include-menus", action="store_true")
    observe.add_argument("--no-virtual-hints", action="store_true")
    observe.add_argument("--output", type=Path)
    observe.set_defaults(func=cmd_observe)

    highlight = subparsers.add_parser("highlight", help="Draw temporary boxes around traversed UI elements.")
    add_global(highlight)
    highlight.add_argument("pid", type=int)
    highlight.add_argument("--duration", type=float, default=3.0)
    highlight.set_defaults(func=cmd_highlight)

    input_parser = subparsers.add_parser("input", help="Send coordinate, keyboard, mouse, or text input.")
    add_global(input_parser)
    input_parser.add_argument("--visual", action="store_true", help="Use VisualInputTool when available.")
    input_parser.add_argument("--duration", type=float, help="Visual feedback duration for --visual.")
    input_parser.add_argument("--debug-ax-grid-pid", type=int, help="PID to highlight when --debug-ax-grid is enabled for raw input commands.")
    input_parser.add_argument("action", help="click, doubleclick, rightclick, mousemove, scroll, keypress, writetext.")
    input_parser.add_argument("action_args", nargs=argparse.REMAINDER)
    input_parser.set_defaults(func=cmd_input)

    ax = subparsers.add_parser("ax", help="Operate a traversed AX element by axPath.")
    add_global(ax)
    ax.add_argument("action", choices=["axactivate", "axpress", "axfocus", "axselect", "axsetvalue"])
    ax.add_argument("pid", type=int)
    ax.add_argument("ax_path")
    ax.add_argument("value", nargs="?")
    ax.set_defaults(func=cmd_ax)

    cursor = subparsers.add_parser("cursor", help="Manage the persistent virtual cursor overlay.")
    add_global(cursor)
    cursor.add_argument("cursor_command", choices=["start", "stop", "status", "move", "click", "move_test", "move-test"])
    cursor.add_argument("coords", nargs="*", help="For move/click: x y in top-left screen coordinates.")
    cursor.add_argument("--event", default="move", help="State event name for cursor move. Defaults to move.")
    cursor.add_argument("--label", help="Optional cursor label text.")
    cursor.add_argument("--settle-ms", type=float, help="Click movement settle delay override in milliseconds.")
    cursor.add_argument("--duration", type=float, default=6.0, help="Movement test duration in seconds.")
    cursor.add_argument("--steps", type=int, default=6, help="Number of cursor target positions for move_test.")
    cursor.add_argument("--region", help="Movement test region as x,y,width,height in top-left screen coordinates.")
    cursor.add_argument("--pid", type=int, help="Use the visible AXWindow for this pid as the movement test region.")
    cursor.add_argument("--window-index", type=int, default=0)
    cursor.add_argument("--output", type=Path)
    cursor.set_defaults(func=cmd_cursor)

    ocr = subparsers.add_parser("ocr", help="Run local macOS Vision OCR on an image, screen region, or app window.")
    add_global(ocr)
    source = ocr.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="Existing image file to OCR.")
    source.add_argument("--region", help="Screen capture region as x,y,width,height using top-left screen coordinates.")
    source.add_argument("--pid", type=int, help="Capture and OCR the visible AXWindow for a running app PID.")
    ocr.add_argument("--window-index", type=int, default=0, help="When using --pid, choose which visible AXWindow to capture.")
    ocr.add_argument("--screenshot-output", type=Path, help="Where to save the captured screenshot for --region or --pid.")
    ocr.add_argument("--languages", default="zh-Hans,en-US", help="Comma-separated Vision recognition languages.")
    ocr.add_argument("--recognition-level", choices=["accurate", "fast"], default="accurate")
    ocr.add_argument("--format", choices=["json", "tsv", "text"], default="json")
    ocr.add_argument("--contains", help="Only keep OCR lines containing this literal text.")
    ocr.add_argument("--output", type=Path)
    ocr.set_defaults(func=cmd_ocr)

    profile_app = subparsers.add_parser("profile-app", help="Statically profile an app, URL, or bundle for public/source-aware automation surfaces.")
    profile_app.add_argument("--target", required=True, help="App bundle path, bundle identifier/name, or web URL.")
    profile_app.add_argument("--guide-dir", type=Path, help="Directory containing Tactile app guides. Defaults to references/app-guides.")
    profile_app.add_argument("--output", type=Path)
    profile_app.set_defaults(func=cmd_profile_app)

    catalog_actions = subparsers.add_parser("catalog-actions", help="Build a CapabilityCatalog from a profile-app JSON output.")
    catalog_actions.add_argument("--profile", type=Path, required=True)
    catalog_actions.add_argument("--output", type=Path)
    catalog_actions.set_defaults(func=cmd_catalog_actions)

    run_adapter = subparsers.add_parser("run-adapter", help="Route one catalog task through a dry-run adapter strategy.")
    run_adapter.add_argument("--app", required=True, help="Known app key/name such as feishu, wechat, or tencent-meeting.")
    run_adapter.add_argument("--task", required=True, help="Action id such as feishu.open_messages.")
    run_adapter.add_argument("--strategy", choices=["baseline", "code-aware", "ax", "visual"], default="code-aware")
    run_adapter.add_argument("--catalog", type=Path, help="Optional catalog-actions JSON file. When omitted, a built-in catalog is generated.")
    run_adapter.add_argument("--verify", action="store_true", help="Require a structured verifier in the dry-run result.")
    run_adapter.add_argument("--inputs-json", help="Optional JSON object with task input placeholders.")
    run_adapter.add_argument("--output", type=Path)
    run_adapter.set_defaults(func=cmd_run_adapter)

    eval_suite = subparsers.add_parser("eval-suite", help="Run a dry-run adapter evaluation suite and emit JSONL run records.")
    eval_suite.add_argument("--suite", type=Path, required=True, help="JSON or simple YAML suite file.")
    eval_suite.add_argument("--strategy", choices=["baseline", "code-aware", "ax", "visual"], default="code-aware")
    eval_suite.add_argument("--runs", type=int, default=10)
    eval_suite.add_argument("--output", type=Path, help="Write run records as JSONL.")
    eval_suite.set_defaults(func=cmd_eval_suite)

    workflow = subparsers.add_parser("workflow", help="Run the end-to-end LLM observe-plan-act workflow.")
    add_global(workflow)
    workflow.add_argument("instruction")
    workflow.add_argument("workflow_args", nargs=argparse.REMAINDER)
    workflow.set_defaults(func=cmd_workflow)

    feishu_list_buttons = subparsers.add_parser("feishu-list-buttons", help="List visible Feishu/Lark labeled navigation and account controls.")
    add_global(feishu_list_buttons)
    feishu_list_buttons.add_argument("--target", default="com.electron.lark")
    feishu_list_buttons.add_argument("--output", type=Path)
    feishu_list_buttons.set_defaults(func=cmd_feishu_fast)

    feishu_open_section = subparsers.add_parser("feishu-open-section", help="Fast Feishu/Lark path: open a visible left navigation section by AX label.")
    add_global(feishu_open_section)
    feishu_open_section.add_argument("section", help="Section label or alias, e.g. 消息, 日历, 云文档, 工作台, contacts.")
    feishu_open_section.add_argument("--target", default="com.electron.lark")
    feishu_open_section.add_argument("--dry-run", action="store_true")
    feishu_open_section.add_argument("--output", type=Path)
    feishu_open_section.set_defaults(func=cmd_feishu_fast)

    feishu_search = subparsers.add_parser("feishu-search", help="Fast Feishu/Lark path: open global search with Cmd+K and paste a query.")
    add_global(feishu_search)
    feishu_search.add_argument("query")
    feishu_search.add_argument("--target", default="com.electron.lark")
    feishu_search.add_argument("--open", action="store_true", help="Press Enter after pasting the query.")
    feishu_search.add_argument("--wait-ms", type=int, default=100)
    feishu_search.add_argument("--restore-clipboard", action="store_true")
    feishu_search.add_argument("--output", type=Path)
    feishu_search.set_defaults(func=cmd_feishu_fast)

    feishu_open_app = subparsers.add_parser("feishu-open-app", help="Fast Feishu/Lark path: search and open an app/workplace item such as 飞书汇报.")
    add_global(feishu_open_app)
    feishu_open_app.add_argument("query")
    feishu_open_app.add_argument("--target", default="com.electron.lark")
    feishu_open_app.add_argument("--open", action="store_true", default=True)
    feishu_open_app.add_argument("--wait-ms", type=int, default=100)
    feishu_open_app.add_argument("--restore-clipboard", action="store_true")
    feishu_open_app.add_argument("--output", type=Path)
    feishu_open_app.set_defaults(func=cmd_feishu_fast)

    feishu_open_chat = subparsers.add_parser("feishu-open-chat", help="Fast Feishu/Lark path: open a chat/contact through global search.")
    add_global(feishu_open_chat)
    feishu_open_chat.add_argument("--target", default="com.electron.lark")
    feishu_open_chat.add_argument("--chat", required=True)
    feishu_open_chat.add_argument("--wait-ms", type=int, default=100)
    feishu_open_chat.add_argument("--verify", action="store_true")
    feishu_open_chat.add_argument("--restore-clipboard", action="store_true")
    feishu_open_chat.add_argument("--output", type=Path)
    feishu_open_chat.set_defaults(func=cmd_feishu_fast)

    feishu_send_message = subparsers.add_parser("feishu-send-message", help="Fast Feishu/Lark path: open chat, focus compose, paste a message, optionally send.")
    add_global(feishu_send_message)
    feishu_send_message.add_argument("--target", default="com.electron.lark")
    feishu_send_message.add_argument("--chat", required=True)
    feishu_send_message.add_argument("--message", required=True)
    feishu_send_message.add_argument("--org", help="Switch to this organization before opening the chat.")
    feishu_send_message.add_argument("--send", action="store_true", help="Press the send key after pasting.")
    feishu_send_message.add_argument("--draft-only", action="store_true", help="Paste the draft but do not send.")
    feishu_send_message.add_argument("--send-key", default="enter")
    feishu_send_message.add_argument("--wait-ms", type=int, default=100)
    feishu_send_message.add_argument("--verify", action="store_true")
    feishu_send_message.add_argument("--restore-clipboard", action="store_true")
    feishu_send_message.add_argument("--keep-existing-draft", action="store_true")
    feishu_send_message.add_argument("--output", type=Path)
    feishu_send_message.set_defaults(func=cmd_feishu_fast)

    feishu_switch_org = subparsers.add_parser("feishu-switch-org", help="Fast Feishu/Lark path: switch by visible organization/account button label.")
    add_global(feishu_switch_org)
    feishu_switch_org.add_argument("--target", default="com.electron.lark")
    feishu_switch_org.add_argument("--name", required=True)
    feishu_switch_org.add_argument("--wait-ms", type=int, default=120)
    feishu_switch_org.add_argument("--dry-run", action="store_true")
    feishu_switch_org.add_argument("--output", type=Path)
    feishu_switch_org.set_defaults(func=cmd_feishu_fast)

    feishu_open_url = subparsers.add_parser("feishu-open-url", help="Open a recognized Feishu/Lark URL directly through macOS.")
    add_global(feishu_open_url)
    feishu_open_url.add_argument("--url", required=True)
    feishu_open_url.add_argument("--output", type=Path)
    feishu_open_url.set_defaults(func=cmd_feishu_fast)

    feishu_create_doc = subparsers.add_parser("feishu-create-doc", help="Fast Feishu/Lark path: create a cloud doc, then optionally fill/share it in the default browser.")
    add_global(feishu_create_doc)
    feishu_create_doc.add_argument("--target", default="com.electron.lark")
    feishu_create_doc.add_argument("--org", help="Switch to this organization before creating the doc.")
    feishu_create_doc.add_argument("--title", help="Paste this title into the browser doc after it opens.")
    feishu_create_doc.add_argument("--body", help="Paste this body into the browser doc after it opens.")
    feishu_create_doc.add_argument("--copy-url", action="store_true", help="Copy the frontmost browser URL after creating/filling the doc.")
    feishu_create_doc.add_argument("--send-to", help="Open this chat and draft/send the created document URL.")
    feishu_create_doc.add_argument("--send", action="store_true", help="Send the document link when --send-to is provided.")
    feishu_create_doc.add_argument("--draft-only", action="store_true", help="Draft the document link but do not send it.")
    feishu_create_doc.add_argument("--send-key", default="enter")
    feishu_create_doc.add_argument("--message-prefix", help="Optional text to put before the title and URL when sharing.")
    feishu_create_doc.add_argument("--wait-ms", type=int, default=100)
    feishu_create_doc.add_argument("--browser-wait-ms", type=int, default=2500)
    feishu_create_doc.add_argument("--autosave-wait-ms", type=int, default=800)
    feishu_create_doc.add_argument("--restore-clipboard", action="store_true")
    feishu_create_doc.add_argument("--dry-run", action="store_true")
    feishu_create_doc.add_argument("--output", type=Path)
    feishu_create_doc.set_defaults(func=cmd_feishu_fast)

    artifact_dir = subparsers.add_parser("artifact-dir", help="Print the session-scoped macOS workflow artifact directory.")
    artifact_dir.set_defaults(func=cmd_artifact_dir)

    plan_log = subparsers.add_parser("plan-log", help="Summarize a workflow plan-output JSON file.")
    plan_log.add_argument("path", type=Path)
    plan_log.add_argument("--output", type=Path)
    plan_log.set_defaults(func=cmd_plan_log)

    trace_replay = subparsers.add_parser("trace-replay", help="Aggregate metrics from trace fixtures, run logs, or JSONL traces.")
    trace_replay.add_argument("paths", nargs="+", type=Path)
    trace_replay.add_argument("--output", type=Path)
    trace_replay.set_defaults(func=cmd_trace_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
