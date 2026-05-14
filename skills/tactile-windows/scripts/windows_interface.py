#!/usr/bin/env python3
"""Small composable interfaces for a WindowsUseSDK checkout."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import locale
import os
import re
import shutil
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

REPO_ENV = "WINDOWS_USE_SDK_ROOT"
DEFAULT_REPO = SKILL_ROOT / "vendor" / "WindowsUseSDK"
ARTIFACT_SUBDIR = artifact_utils.ARTIFACT_SUBDIR
default_artifact_path = artifact_utils.default_artifact_path
session_artifact_dir = artifact_utils.session_artifact_dir
session_scoped_output_path = artifact_utils.session_scoped_output_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def sdk_root_from_candidate(candidate: Path) -> Path | None:
    if (candidate / "WindowsUseSDK.ps1").exists():
        return candidate
    for nested in (
        candidate / "vendor" / "WindowsUseSDK",
        candidate / "native" / "WindowsUseSDK",
    ):
        if (nested / "WindowsUseSDK.ps1").exists():
            return nested.resolve()
    return None


def find_repo_root(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        found = sdk_root_from_candidate(parent)
        if found:
            return found
    return None


def repo_path(value: str | None) -> Path:
    raw = value or os.environ.get(REPO_ENV)
    if raw:
        found = sdk_root_from_candidate(Path(raw).expanduser().resolve())
        if found:
            return found
    found = (
        sdk_root_from_candidate(DEFAULT_REPO)
        or find_repo_root(Path.cwd().resolve())
        or find_repo_root(Path(__file__).resolve())
    )
    if found:
        return found
    raise SystemExit(
        f"WindowsUseSDK not found: expected bundled {DEFAULT_REPO}, or pass --repo / set {REPO_ENV}"
    )


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


def run_sdk(repo: Path, args: list[str], *, timeout: float | None = None) -> dict[str, Any]:
    cmd = [
        powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        os.fspath(repo / "WindowsUseSDK.ps1"),
        *args,
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stdout = decode_process_output(proc.stdout)
    stderr = decode_process_output(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(
            f"WindowsUseSDK failed ({proc.returncode})\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"WindowsUseSDK returned non-JSON output: {stdout[:2000]!r}") from exc


def write_or_print(data: Any, output: Path | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    output = session_scoped_output_path(output)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")


def attach_fast_trace(
    payload: dict[str, Any],
    *,
    command: str,
    instruction: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or "trace" in payload:
        return payload
    try:
        payload["trace"] = tactile_trace.build_fast_path_trace(
            payload,
            platform="windows",
            command=command,
            instruction=instruction or command,
        )
    except Exception as exc:
        payload["trace_error"] = str(exc)
    return payload


def arg_list_has_option(values: list[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in values)


def normalize_match_text(value: Any) -> str:
    if value is None:
        return ""
    return "".join(ch.casefold() for ch in str(value) if ch.isalnum())


def positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def compact_lines(value: Any, *, limit: int = 12) -> list[str]:
    if not isinstance(value, str):
        return []
    lines = []
    for line in value.splitlines():
        compact = " ".join(line.split())
        if compact:
            lines.append(compact)
        if len(lines) >= limit:
            break
    return lines


def click_point(repo: Path, hwnd: int, x: float, y: float) -> dict[str, Any]:
    return run_sdk(repo, ["input", "--hwnd", str(hwnd), "click", str(round(x, 2)), str(round(y, 2))], timeout=15)


def keypress(repo: Path, hwnd: int, key: str) -> dict[str, Any]:
    return run_sdk(repo, ["input", "--hwnd", str(hwnd), "keypress", key], timeout=15)


def ocr_rect(repo: Path, hwnd: int, x: float, y: float, width: float, height: float) -> dict[str, Any]:
    rect = ",".join(str(round(value, 2)) for value in (x, y, width, height))
    return run_sdk(repo, ["ocr", "--hwnd", str(hwnd), "--rect", rect], timeout=60)


def ocr_window(repo: Path, hwnd: int) -> dict[str, Any]:
    return run_sdk(repo, ["ocr", "--hwnd", str(hwnd)], timeout=60)


def resolve_target_hwnd(repo: Path, target: str, hwnd: int | None) -> tuple[int, dict[str, Any]]:
    normalized_hwnd = positive_int_or_none(hwnd)
    if normalized_hwnd is not None:
        return normalized_hwnd, {"mode": "explicit_hwnd", "hwnd": normalized_hwnd}
    opened = run_sdk(repo, ["open", target], timeout=30)
    if not opened.get("hwnd"):
        raise SystemExit(f"Could not resolve hwnd for {target!r}: {opened}")
    time.sleep(0.5)
    return int(opened["hwnd"]), opened


def frame_from_payload(payload: dict[str, Any] | None) -> dict[str, float] | None:
    frame = payload.get("frame") if isinstance(payload, dict) else None
    if not isinstance(frame, dict):
        return None
    try:
        x = float(frame.get("x"))
        y = float(frame.get("y"))
        width = float(frame.get("width"))
        height = float(frame.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def wechat_profile_regions(frame: dict[str, float]) -> dict[str, Any]:
    x = frame["x"]
    y = frame["y"]
    width = frame["width"]
    height = frame["height"]
    return {
        "search_center": (x + 125, y + 55),
        "title_ocr": (x + 235, y + 35, 260, 40),
        "left_results": (x + 70, y + 40, 165, 360),
        "compose_center": (x + max(260, width * 0.45), y + height - 85),
        "send_center": (x + width - 60, y + height - 42),
        "draft_ocr": (x + 220, y + height - 280, max(1, width - 250), 240),
        "recent_sent_ocr": (x + 235, y + max(90, height - 590), max(1, width - 275), 350),
    }


def direct_win32_available() -> bool:
    return os.name == "nt"


def direct_activate_window(hwnd: int) -> dict[str, Any]:
    if not direct_win32_available():
        raise RuntimeError("direct Win32 input is only available on Windows")
    import ctypes

    user32 = ctypes.windll.user32
    with contextlib.suppress(Exception):
        user32.ShowWindow(int(hwnd), 9)
    ok = bool(user32.SetForegroundWindow(int(hwnd)))
    time.sleep(0.05)
    return {"mode": "direct_win32", "hwnd": hwnd, "foreground": ok}


def direct_click_point(hwnd: int, x: float, y: float) -> dict[str, Any]:
    if not direct_win32_available():
        raise RuntimeError("direct Win32 input is only available on Windows")
    import ctypes

    direct_activate_window(hwnd)
    user32 = ctypes.windll.user32
    ix = int(round(x))
    iy = int(round(y))
    if not user32.SetCursorPos(ix, iy):
        raise RuntimeError(f"SetCursorPos failed for ({ix}, {iy})")
    mouseeventf_leftdown = 0x0002
    mouseeventf_leftup = 0x0004
    user32.mouse_event(mouseeventf_leftdown, 0, 0, 0, 0)
    time.sleep(0.02)
    user32.mouse_event(mouseeventf_leftup, 0, 0, 0, 0)
    return {"mode": "direct_win32", "action": "click", "x": ix, "y": iy, "status": "success"}


def direct_keypress(hwnd: int, key: str) -> dict[str, Any]:
    if not direct_win32_available():
        raise RuntimeError("direct Win32 input is only available on Windows")
    import ctypes

    direct_activate_window(hwnd)
    user32 = ctypes.windll.user32
    keyeventf_keyup = 0x0002
    vk_map = {
        "ctrl": 0x11,
        "control": 0x11,
        "shift": 0x10,
        "alt": 0x12,
        "enter": 0x0D,
        "return": 0x0D,
        "escape": 0x1B,
        "esc": 0x1B,
        "tab": 0x09,
        "space": 0x20,
        "backspace": 0x08,
        "delete": 0x2E,
        "del": 0x2E,
        "a": 0x41,
        "v": 0x56,
    }
    parts = [part.strip().casefold() for part in key.split("+") if part.strip()]
    if not parts:
        raise RuntimeError("empty keypress")
    modifiers = parts[:-1]
    main = parts[-1]
    if len(main) == 1 and main.isalnum():
        vk_main = ord(main.upper())
    else:
        vk_main = vk_map.get(main)
    if vk_main is None:
        raise RuntimeError(f"unsupported direct keypress: {key}")
    modifier_vks = []
    for modifier in modifiers:
        vk = vk_map.get(modifier)
        if vk is None:
            raise RuntimeError(f"unsupported direct key modifier: {modifier}")
        modifier_vks.append(vk)
    for vk in modifier_vks:
        user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk_main, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk_main, 0, keyeventf_keyup, 0)
    for vk in reversed(modifier_vks):
        user32.keybd_event(vk, 0, keyeventf_keyup, 0)
    return {"mode": "direct_win32", "action": "keypress", "key": key, "status": "success"}


def _clipboard_apis() -> tuple[Any, Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hglobal = getattr(wintypes, "HGLOBAL", wintypes.HANDLE)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = hglobal
    user32.SetClipboardData.argtypes = [wintypes.UINT, hglobal]
    user32.SetClipboardData.restype = hglobal
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = hglobal
    kernel32.GlobalLock.argtypes = [hglobal]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [hglobal]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [hglobal]
    kernel32.GlobalFree.restype = hglobal
    return ctypes, user32, kernel32, hglobal


def get_windows_clipboard_text() -> str | None:
    if not direct_win32_available():
        return None
    ctypes, user32, kernel32, _ = _clipboard_apis()
    cf_unicode_text = 13
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(cf_unicode_text)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_windows_clipboard_text(text: str) -> None:
    if not direct_win32_available():
        raise RuntimeError("Windows clipboard is only available on Windows")
    ctypes, user32, kernel32, _ = _clipboard_apis()
    cf_unicode_text = 13
    gmem_moveable = 0x0002
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        raise RuntimeError("OpenClipboard failed")
    clipboard_owner_transferred = False
    handle = None
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("EmptyClipboard failed")
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            raise RuntimeError("GlobalAlloc failed")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("GlobalLock failed")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_unicode_text, handle):
            raise RuntimeError("SetClipboardData failed")
        clipboard_owner_transferred = True
    finally:
        user32.CloseClipboard()
        if handle and not clipboard_owner_transferred:
            kernel32.GlobalFree(handle)


def direct_paste_text(hwnd: int, text: str) -> dict[str, Any]:
    set_windows_clipboard_text(text)
    key_result = direct_keypress(hwnd, "ctrl+v")
    return {"mode": "direct_win32", "action": "pastetext", "text_length": len(text), "key_result": key_result}


def fast_click_point(repo: Path, hwnd: int, x: float, y: float, *, sdk_input: bool) -> dict[str, Any]:
    if sdk_input:
        return click_point(repo, hwnd, x, y)
    try:
        return direct_click_point(hwnd, x, y)
    except Exception as exc:
        fallback = click_point(repo, hwnd, x, y)
        return {"mode": "direct_win32_fallback", "direct_error": str(exc), "fallback": fallback}


def fast_keypress(repo: Path, hwnd: int, key: str, *, sdk_input: bool) -> dict[str, Any]:
    if sdk_input:
        return keypress(repo, hwnd, key)
    try:
        return direct_keypress(hwnd, key)
    except Exception as exc:
        fallback = keypress(repo, hwnd, key)
        return {"mode": "direct_win32_fallback", "direct_error": str(exc), "fallback": fallback}


def fast_paste_text(repo: Path, hwnd: int, text: str, *, sdk_input: bool) -> dict[str, Any]:
    if sdk_input:
        return input_action(repo, hwnd, "pastetext", text)
    try:
        return direct_paste_text(hwnd, text)
    except Exception as exc:
        fallback = input_action(repo, hwnd, "pastetext", text)
        return {"mode": "direct_win32_fallback", "direct_error": str(exc), "fallback": fallback}


def image_component_centers(image_path: Path, region: dict[str, Any], *, max_icons: int) -> list[tuple[float, float]]:
    try:
        from PIL import Image
    except Exception:
        return []

    try:
        image = Image.open(image_path).convert("RGB")
    except Exception:
        return []

    region_width = float(region.get("width") or 0)
    region_height = float(region.get("height") or 0)
    if region_width <= 0 or region_height <= 0:
        return []

    scale_x = image.width / region_width
    scale_y = image.height / region_height
    crop_left = 0
    crop_top = max(0, image.height - int(130 * scale_y))
    crop_right = min(image.width, int(360 * scale_x))
    crop_bottom = image.height
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return []

    mask: set[tuple[int, int]] = set()
    for py in range(crop_top, crop_bottom):
        for px in range(crop_left, crop_right):
            r, g, b = image.getpixel((px, py))
            maximum = max(r, g, b)
            minimum = min(r, g, b)
            saturation = maximum - minimum
            if r < 10 and g < 10 and b < 10:
                continue
            colored_icon = saturation > 34 and maximum > 70 and minimum < 245
            gray_icon = 55 <= maximum <= 210 and saturation < 34
            if colored_icon or gray_icon:
                mask.add((px, py))

    seen: set[tuple[int, int]] = set()
    centers: list[tuple[float, float]] = []
    for point in list(mask):
        if point in seen:
            continue
        stack = [point]
        seen.add(point)
        xs: list[int] = []
        ys: list[int] = []
        while stack:
            px, py = stack.pop()
            xs.append(px)
            ys.append(py)
            for nx in (px - 1, px, px + 1):
                for ny in (py - 1, py, py + 1):
                    if (nx, ny) == (px, py):
                        continue
                    neighbor = (nx, ny)
                    if neighbor in mask and neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
        if len(xs) < 80:
            continue
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        comp_width = (right - left + 1) / scale_x
        comp_height = (bottom - top + 1) / scale_y
        if not (8 <= comp_width <= 100 and 8 <= comp_height <= 100):
            continue
        center_x = float(region.get("x") or 0) + ((left + right) / 2.0) / scale_x
        center_y = float(region.get("y") or 0) + ((top + bottom) / 2.0) / scale_y
        # Keep the bottom dock, not chat avatars above it.
        if center_y < float(region.get("y") or 0) + region_height - 100:
            continue
        centers.append((center_x, center_y))

    centers.sort(key=lambda item: item[0])
    deduped: list[tuple[float, float]] = []
    for center in centers:
        if any(abs(center[0] - existing[0]) < 24 and abs(center[1] - existing[1]) < 24 for existing in deduped):
            continue
        deduped.append(center)
    return deduped[:max_icons]


def feishu_org_dock_centers(capture: dict[str, Any], *, max_icons: int) -> tuple[list[tuple[float, float]], str]:
    region = ((capture.get("capture") or {}).get("region") or {})
    image_path_raw = capture.get("image_path")
    if image_path_raw:
        detected = image_component_centers(Path(image_path_raw), region, max_icons=max_icons)
        if detected:
            first_x = min(center[0] for center in detected)
            dock_y = sorted(detected, key=lambda item: item[1])[-1][1]
            augmented = list(detected)
            # Feishu keeps the current workspace first and then lays the visible
            # workspace icons at roughly 60px intervals. Add missing slots so a
            # low-contrast icon or the gray "more" button is still reachable.
            for i in range(max_icons):
                candidate = (first_x + i * 60.0, dock_y)
                if candidate[0] > float(region.get("x") or 0) + min(float(region.get("width") or 0), 360):
                    break
                if not any(abs(candidate[0] - existing[0]) < 24 for existing in augmented):
                    augmented.append(candidate)
            augmented.sort(key=lambda item: item[0])
            return augmented[:max_icons], "image_components_plus_dock_spacing"

    x = float(region.get("x") or 0)
    y = float(region.get("y") or 0)
    width = float(region.get("width") or 0)
    height = float(region.get("height") or 0)
    if width <= 0 or height <= 0:
        return [], "unavailable"
    fallback = [(x + 56 + i * 60, y + height - 52) for i in range(max_icons)]
    fallback = [center for center in fallback if center[0] <= x + min(width, 360)]
    return fallback, "static_bottom_dock_spacing"


def input_action(repo: Path, hwnd: int, action: str, *action_args: Any) -> dict[str, Any]:
    string_args = [str(value) for value in action_args if value is not None]
    timeout = max(10, len(" ".join(string_args)) * 0.2)
    return run_sdk(repo, ["input", "--hwnd", str(hwnd), action, *string_args], timeout=timeout)


def input_text_with_fallback(repo: Path, hwnd: int, text: str) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for method in ("streamtext", "writetext", "pastetext"):
        try:
            result = input_action(repo, hwnd, method, text)
            return {"method": method, "result": result, "fallback_failures": failures}
        except SystemExit as exc:
            failures.append({"method": method, "error": str(exc)})
    raise SystemExit(f"text input failed with all methods: {failures}")


def elements_for_window(repo: Path, hwnd: int, *, query: str | None = None, view: str = "control", limit: int = 80) -> dict[str, Any]:
    sdk_args = ["elements", "--hwnd", str(hwnd), "--view", view, "--visible-only", "--no-activate", "--limit", str(limit)]
    if query:
        sdk_args.extend(["--query", query])
    return run_sdk(repo, sdk_args, timeout=25)


def find_virtual_region(elements: dict[str, Any], text_part: str) -> dict[str, Any] | None:
    needle = text_part.casefold()
    for element in elements.get("elements") or []:
        if not isinstance(element, dict):
            continue
        if str(element.get("role") or "").casefold() != "virtualregion":
            continue
        if needle in str(element.get("text") or "").casefold():
            return element
    return None


def element_frame(element: dict[str, Any]) -> dict[str, float] | None:
    frame = element.get("frame")
    if not isinstance(frame, dict):
        return None
    try:
        x = float(frame.get("x"))
        y = float(frame.get("y"))
        width = float(frame.get("width"))
        height = float(frame.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def element_center(element: dict[str, Any]) -> tuple[float, float] | None:
    center = element.get("center")
    if isinstance(center, dict):
        try:
            return float(center.get("x")), float(center.get("y"))
        except (TypeError, ValueError):
            pass
    frame = element_frame(element)
    if not frame:
        return None
    return frame["x"] + frame["width"] / 2.0, frame["y"] + frame["height"] / 2.0


def click_element_center(repo: Path, hwnd: int, element: dict[str, Any]) -> dict[str, Any]:
    center = element_center(element)
    if not center:
        raise SystemExit(f"element has no center: {element}")
    return click_point(repo, hwnd, center[0], center[1])


def ocr_element_frame(repo: Path, hwnd: int, element: dict[str, Any]) -> dict[str, Any]:
    frame = element_frame(element)
    if not frame:
        return {}
    return ocr_rect(repo, hwnd, frame["x"], frame["y"], frame["width"], frame["height"])


def line_center(line: dict[str, Any]) -> tuple[float, float] | None:
    center = line.get("center")
    if isinstance(center, dict):
        try:
            return float(center.get("x")), float(center.get("y"))
        except (TypeError, ValueError):
            pass
    frame = line.get("screen_frame") if isinstance(line.get("screen_frame"), dict) else line.get("frame")
    if not isinstance(frame, dict):
        return None
    try:
        x = float(frame.get("x"))
        y = float(frame.get("y"))
        width = float(frame.get("width"))
        height = float(frame.get("height"))
    except (TypeError, ValueError):
        return None
    return x + width / 2.0, y + height / 2.0


def line_screen_frame(line: dict[str, Any]) -> dict[str, float] | None:
    frame = line.get("screen_frame") if isinstance(line.get("screen_frame"), dict) else line.get("frame")
    if not isinstance(frame, dict):
        return None
    try:
        x = float(frame.get("x"))
        y = float(frame.get("y"))
        width = float(frame.get("width"))
        height = float(frame.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def line_top(line: dict[str, Any]) -> float:
    frame = line_screen_frame(line)
    return frame["y"] if frame else 0.0


def capture_region(payload: dict[str, Any]) -> dict[str, float]:
    region = ((payload.get("capture") or {}).get("region") or {})
    try:
        x = float(region.get("x") or 0)
        y = float(region.get("y") or 0)
        width = float(region.get("width") or 0)
        height = float(region.get("height") or 0)
    except (TypeError, ValueError):
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    return {"x": x, "y": y, "width": width, "height": height}


def relative_line_center(payload: dict[str, Any], line: dict[str, Any]) -> tuple[float, float] | None:
    center = line_center(line)
    if not center:
        return None
    region = capture_region(payload)
    return center[0] - region["x"], center[1] - region["y"]


def relative_line_top(payload: dict[str, Any], line: dict[str, Any]) -> float:
    return line_top(line) - capture_region(payload)["y"]


def find_ocr_lines_for_query(payload: dict[str, Any], query: str, *, min_relative_top: float | None = 72.0) -> list[dict[str, Any]]:
    query_norm = normalize_match_text(query)
    if not query_norm:
        return []
    region = capture_region(payload)
    region_width = max(region["width"], 1.0)
    region_height = max(region["height"], 1.0)
    matches: list[dict[str, Any]] = []
    for index, line in enumerate(payload.get("lines") or []):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "")
        norm = normalize_match_text(text)
        if not norm:
            continue
        if min_relative_top is not None and relative_line_top(payload, line) < min_relative_top:
            continue
        allow_contains = len(query_norm) >= 4 or any(ord(ch) > 127 for ch in query_norm)
        if norm != query_norm and not (allow_contains and query_norm in norm):
            continue

        center = line_center(line)
        frame = line_screen_frame(line)
        score = 100 if norm == query_norm else 50
        if center:
            rel_center = relative_line_center(payload, line)
            rel_x, rel_y = rel_center if rel_center else center
            if rel_x >= min(500.0, region_width * 0.24):
                score += 70
            elif rel_x < min(300.0, region_width * 0.16):
                score -= 35
            if region_height * 0.12 <= rel_y <= region_height * 0.58:
                score += 20
        if norm.startswith(f"q{query_norm}") or "搜索" in text:
            score -= 120
        if frame and frame["width"] < 18:
            score -= 30
        enriched = dict(line)
        enriched["_match_score"] = score
        enriched["_match_index"] = index
        matches.append(enriched)
    return sorted(matches, key=lambda item: (-int(item.get("_match_score") or 0), line_top(item), int(item.get("_match_index") or 0)))


def feishu_contact_result_reject_reason(payload: dict[str, Any], line: dict[str, Any], query: str) -> str | None:
    query_norm = normalize_match_text(query)
    text = str(line.get("text") or "")
    norm = normalize_match_text(text)
    if not query_norm or query_norm not in norm:
        return None
    if relative_line_top(payload, line) < 72:
        return "search chrome"
    if norm.startswith(f"q{query_norm}") or "搜索" in text:
        return "search input"
    if not (norm == query_norm or norm.startswith(query_norm)):
        return "target appears in secondary text or message snippet"
    reject_terms = ("包含", "群消息", "消息更新", "更新于", "问一问")
    normalized_reject_terms = tuple(normalize_match_text(term) for term in reject_terms)
    if any(term and term in norm for term in normalized_reject_terms):
        return "target appears in grouped/history/search metadata"
    group_norm = normalize_match_text("群")
    if group_norm in norm and group_norm not in query_norm and norm != query_norm:
        return "candidate appears to be a group, not the named contact"
    return None


def find_feishu_contact_result_lines(payload: dict[str, Any], query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query_norm = normalize_match_text(query)
    if not query_norm:
        return [], []
    region = capture_region(payload)
    region_width = max(region["width"], 1.0)
    region_height = max(region["height"], 1.0)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, line in enumerate(payload.get("lines") or []):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "")
        norm = normalize_match_text(text)
        if not norm or query_norm not in norm:
            continue

        reason = feishu_contact_result_reject_reason(payload, line, query)
        enriched = dict(line)
        enriched["_match_index"] = index
        if reason:
            enriched["_reject_reason"] = reason
            rejected.append(enriched)
            continue

        score = 120 if norm == query_norm else 90
        center = line_center(line)
        frame = line_screen_frame(line)
        if center:
            rel_center = relative_line_center(payload, line)
            rel_x, rel_y = rel_center if rel_center else center
            if rel_x <= min(520.0, region_width * 0.28):
                score += 35
            if region_height * 0.10 <= rel_y <= region_height * 0.60:
                score += 20
        if frame and frame["width"] < 18:
            score -= 40
        enriched["_match_score"] = score
        accepted.append(enriched)
    accepted.sort(key=lambda item: (-int(item.get("_match_score") or 0), line_top(item), int(item.get("_match_index") or 0)))
    rejected.sort(key=lambda item: (line_top(item), int(item.get("_match_index") or 0)))
    return accepted, rejected


def find_ocr_line_for_query(payload: dict[str, Any], query: str, *, min_relative_top: float | None = 72.0) -> dict[str, Any] | None:
    matches = find_ocr_lines_for_query(payload, query, min_relative_top=min_relative_top)
    return matches[0] if matches else None


def find_ocr_line_containing(payload: dict[str, Any], terms: tuple[str, ...], *, max_top: float | None = None) -> dict[str, Any] | None:
    normalized_terms = [normalize_match_text(term) for term in terms if normalize_match_text(term)]
    if not normalized_terms:
        return None
    candidates: list[dict[str, Any]] = []
    for line in payload.get("lines") or []:
        if not isinstance(line, dict):
            continue
        if max_top is not None and line_top(line) > max_top:
            continue
        norm = normalize_match_text(line.get("text"))
        if any(term in norm for term in normalized_terms):
            candidates.append(line)
    return sorted(candidates, key=line_top)[0] if candidates else None


def summarize_ocr_line(line: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(line, dict):
        return None
    center = line_center(line)
    return {
        "text": line.get("text"),
        "center": center,
        "top": line_top(line),
        "score": line.get("_match_score"),
        "reject_reason": line.get("_reject_reason"),
    }


def verify_feishu_chat_ready(repo: Path, hwnd: int, chat: str) -> dict[str, Any]:
    capture = ocr_window(repo, hwnd)
    region = capture_region(capture)
    x = region["x"]
    y = region["y"]
    width = region["width"]
    height = region["height"]
    if width <= 0 or height <= 0:
        return {
            "confirmed": False,
            "reason": "window OCR did not return a valid region",
            "window_lines": compact_lines(capture.get("text") or "", limit=14),
        }

    right_x = x + width * 0.28
    right_width = width * 0.68
    header = ocr_rect(repo, hwnd, right_x, y + 40, right_width, 180)
    compose = ocr_rect(repo, hwnd, right_x, max(y, y + height - 230), right_width, 210)
    chat_norm = normalize_match_text(chat)
    header_norm = normalize_match_text(header.get("text"))
    compose_norm = normalize_match_text(compose.get("text"))
    placeholder_line = find_ocr_line_containing(compose, ("发送给", "send to", chat))
    header_match = bool(chat_norm and chat_norm in header_norm)
    compose_match = bool(chat_norm and chat_norm in compose_norm)
    placeholder_match = bool(placeholder_line and chat_norm and chat_norm in normalize_match_text(placeholder_line.get("text")))
    confirmed = header_match or compose_match or placeholder_match
    return {
        "confirmed": confirmed,
        "reason": None if confirmed else "chat title or compose placeholder did not confirm the requested recipient",
        "header_match": header_match,
        "compose_match": compose_match,
        "placeholder_match": placeholder_match,
        "header_lines": compact_lines(header.get("text") or "", limit=10),
        "compose_lines": compact_lines(compose.get("text") or "", limit=10),
        "compose_center": line_center(placeholder_line) if placeholder_line else None,
    }


def verify_feishu_message_sent(repo: Path, hwnd: int, chat: str, message: str) -> dict[str, Any]:
    ready = verify_feishu_chat_ready(repo, hwnd, chat)
    capture = ocr_window(repo, hwnd)
    region = capture_region(capture)
    x = region["x"]
    y = region["y"]
    width = region["width"]
    height = region["height"]
    message_norm = normalize_match_text(message)
    if width <= 0 or height <= 0 or not message_norm:
        return {
            "confirmed": False,
            "confidence": "none",
            "reason": "window OCR did not return a valid region or message text is empty",
            "chat_ready": ready,
        }

    right_x = x + width * 0.28
    right_width = width * 0.68
    recent_top = y + max(120, height * 0.18)
    recent_bottom_guard = y + height - 240
    recent_height = max(120, recent_bottom_guard - recent_top)
    bottom_top = y + max(160, height - 460)
    bottom_height = max(90, recent_bottom_guard - bottom_top)
    recent = ocr_rect(repo, hwnd, right_x, recent_top, right_width, recent_height)
    recent_bottom = ocr_rect(repo, hwnd, right_x, bottom_top, right_width, bottom_height)
    compose = ocr_rect(repo, hwnd, right_x, max(y, y + height - 230), right_width, 210)
    recent_norm = normalize_match_text(f"{recent.get('text') or ''}\n{recent_bottom.get('text') or ''}")
    compose_norm = normalize_match_text(compose.get("text"))
    message_visible = message_norm in recent_norm
    compose_still_has_message = message_norm in compose_norm
    chat_confirmed = bool(ready.get("confirmed"))
    compose_cleared = not compose_still_has_message
    confirmed = chat_confirmed and compose_cleared
    confidence = "high" if confirmed and message_visible else "medium" if confirmed else "none"
    reason = None
    if not confirmed:
        if compose_still_has_message:
            reason = "message text is still visible in the compose area after pressing Enter"
        elif not chat_confirmed:
            reason = "chat was not confirmed after pressing Enter"
        else:
            reason = "message send could not be acknowledged"
    return {
        "confirmed": confirmed,
        "confidence": confidence,
        "reason": reason,
        "message_visible": message_visible,
        "compose_cleared": compose_cleared,
        "compose_still_has_message": compose_still_has_message,
        "chat_ready": ready,
        "recent_lines": compact_lines(recent.get("text") or "", limit=12),
        "recent_bottom_lines": compact_lines(recent_bottom.get("text") or "", limit=8),
        "compose_lines": compact_lines(compose.get("text") or "", limit=8),
    }


def send_feishu_via_compose_child(
    repo: Path,
    hwnd: int,
    *,
    chat: str,
    message: str,
    ready: dict[str, Any],
    wait_ms: int,
) -> dict[str, Any]:
    compose_center = ready.get("compose_center")
    if not (isinstance(compose_center, (list, tuple)) and len(compose_center) >= 2):
        refreshed = verify_feishu_chat_ready(repo, hwnd, chat)
        compose_center = refreshed.get("compose_center")
    if not (isinstance(compose_center, (list, tuple)) and len(compose_center) >= 2):
        return {
            "attempted": False,
            "reason": "could not identify a verified compose center for child-window send fallback",
        }

    x = float(compose_center[0])
    y = float(compose_center[1])
    click_result = click_point(repo, hwnd, x, y)
    probe = run_sdk(
        repo,
        [
            "probe",
            "--hwnd",
            str(hwnd),
            "--x",
            str(round(x, 2)),
            "--y",
            str(round(y, 2)),
            "--no-ocr",
        ],
        timeout=45,
    )
    element = probe.get("element") if isinstance(probe.get("element"), dict) else {}
    native_hwnd = positive_int_or_none(element.get("native_window_handle"))
    class_name = str(element.get("class_name") or "")
    if not native_hwnd or native_hwnd == hwnd or "Chrome_RenderWidgetHostHWND" not in class_name:
        return {
            "attempted": False,
            "reason": "probe did not find a compose Chromium child window",
            "compose_center": [x, y],
            "click_result": click_result,
            "probe_element": {
                "class_name": class_name,
                "native_window_handle": native_hwnd,
                "role": element.get("role"),
                "text": element.get("text"),
            },
        }

    keypress_result = keypress(repo, native_hwnd, "enter")
    time.sleep(max(0.35, wait_ms / 1000))
    sent_verification = verify_feishu_message_sent(repo, hwnd, chat, message)
    return {
        "attempted": True,
        "child_hwnd": native_hwnd,
        "compose_center": [x, y],
        "click_result": click_result,
        "keypress_result": keypress_result,
        "post_send_verification": sent_verification,
    }


def call_feishu_switch_org(
    repo: Path,
    *,
    target: str,
    hwnd: int | None,
    name: str,
    max_icons: int,
    wait_ms: int,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    output = Path(os.environ.get("TEMP", ".")) / f"luopanpilot-feishu-switch-{int(time.time() * 1000)}.json"
    namespace = argparse.Namespace(
        repo=os.fspath(repo),
        output=output,
        target=target,
        hwnd=hwnd,
        name=name,
        max_icons=max_icons,
        wait_ms=wait_ms,
        dry_run=dry_run,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        code = cmd_feishu_switch_org(namespace)
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    finally:
        try:
            output.unlink()
        except OSError:
            pass
    return code, payload


def cmd_feishu_switch_org(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    target = args.target or "飞书"
    wanted = args.name
    wanted_norm = normalize_match_text(wanted)
    if not wanted_norm:
        raise SystemExit("--name must contain at least one letter or number")

    hwnd, open_result = resolve_target_hwnd(repo, target, args.hwnd)
    steps: list[dict[str, Any]] = [{"step": "resolve_target", "hwnd": hwnd, "result": open_result}]

    try:
        keypress(repo, hwnd, "escape")
    except Exception as exc:
        steps.append({"step": "close_existing_popover", "ok": False, "error": str(exc)})
    else:
        steps.append({"step": "close_existing_popover", "ok": True})
    time.sleep(args.wait_ms / 1000)

    capture = ocr_window(repo, hwnd)
    region = ((capture.get("capture") or {}).get("region") or {})
    centers, detection = feishu_org_dock_centers(capture, max_icons=args.max_icons)
    steps.append(
        {
            "step": "detect_bottom_org_dock",
            "method": detection,
            "window_region": region,
            "candidates": [
                {"index": index + 1, "center": {"x": round(x, 2), "y": round(y, 2)}}
                for index, (x, y) in enumerate(centers)
            ],
        }
    )

    if args.dry_run:
        payload = {
            "status": "dry_run",
            "target_name": wanted,
            "hwnd": hwnd,
            "steps": steps,
        }
        write_or_print(attach_fast_trace(payload, command="feishu-switch-org"), args.output)
        return 0

    if not centers:
        payload = {
            "status": "not_found",
            "reason": "could not detect Feishu bottom organization dock",
            "target_name": wanted,
            "hwnd": hwnd,
            "steps": steps,
        }
        write_or_print(attach_fast_trace(payload, command="feishu-switch-org"), args.output)
        return 1

    wx = float(region.get("x") or 0)
    wy = float(region.get("y") or 0)
    ww = float(region.get("width") or 0)
    wh = float(region.get("height") or 0)
    profile_x = wx + 55
    profile_y = wy + 51
    panel_x = wx + 80
    panel_y = wy + 20
    panel_width = min(620, max(260, ww - 80))
    panel_height = min(740, max(260, wh - 40))

    forbidden_norms = [normalize_match_text(value) for value in ("加入已有企业", "创建新账号", "登录更多账号", "退出登录")]
    for index, (center_x, center_y) in enumerate(centers, start=1):
        click_point(repo, hwnd, center_x, center_y)
        time.sleep(args.wait_ms / 1000)
        try:
            keypress(repo, hwnd, "escape")
            time.sleep(0.15)
        except Exception:
            pass

        click_point(repo, hwnd, profile_x, profile_y)
        time.sleep(args.wait_ms / 1000)
        panel = ocr_rect(repo, hwnd, panel_x, panel_y, panel_width, panel_height)
        panel_text = panel.get("text") or ""
        panel_norm = normalize_match_text(panel_text)
        unsafe_visible = [item for item in forbidden_norms if item and item in panel_norm]
        matched = wanted_norm in panel_norm
        attempt = {
            "step": "probe_candidate",
            "candidate_index": index,
            "clicked_center": {"x": round(center_x, 2), "y": round(center_y, 2)},
            "profile_ocr_lines": compact_lines(panel_text),
            "matched": matched,
            "unsafe_account_actions_visible": bool(unsafe_visible),
        }
        steps.append(attempt)

        keypress(repo, hwnd, "escape")
        time.sleep(0.2)
        if matched:
            payload = {
                "status": "success",
                "target_name": wanted,
                "hwnd": hwnd,
                "selected_candidate_index": index,
                "verification": "matched target text in profile card after clicking bottom organization dock icon",
                "steps": steps,
            }
            write_or_print(attach_fast_trace(payload, command="feishu-switch-org"), args.output)
            return 0

    payload = {
        "status": "not_found",
        "target_name": wanted,
        "hwnd": hwnd,
        "reason": "no visible bottom dock candidate produced a profile card containing the requested organization text",
        "steps": steps,
    }
    write_or_print(attach_fast_trace(payload, command="feishu-switch-org"), args.output)
    return 1


def perform_feishu_open_chat(
    repo: Path,
    *,
    target: str,
    hwnd: int | None,
    chat: str,
    wait_ms: int,
    verify_result: bool,
    allow_first_result: bool,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    chat_norm = normalize_match_text(chat)
    if not chat_norm:
        raise SystemExit("--chat must contain at least one letter or number")

    hwnd, open_result = resolve_target_hwnd(repo, target, hwnd)
    steps: list[dict[str, Any]] = [{"step": "resolve_target", "hwnd": hwnd, "result": open_result}]

    try:
        keypress(repo, hwnd, "escape")
        steps.append({"step": "keypress", "key": "escape", "ok": True})
    except Exception as exc:
        steps.append({"step": "keypress", "key": "escape", "ok": False, "error": str(exc)})
    time.sleep(0.12)
    frame = (open_result or {}).get("frame") if isinstance(open_result, dict) else None
    if isinstance(frame, dict):
        try:
            focus_x = float(frame.get("x") or 0) + float(frame.get("width") or 0) / 2.0
            focus_y = float(frame.get("y") or 0) + 18
            click_point(repo, hwnd, focus_x, focus_y)
            steps.append({"step": "focus_window_titlebar", "center": {"x": round(focus_x, 2), "y": round(focus_y, 2)}})
            time.sleep(0.15)
        except Exception as exc:
            steps.append({"step": "focus_window_titlebar", "ok": False, "error": str(exc)})

    try:
        keypress(repo, hwnd, "ctrl+k")
        steps.append({"step": "keypress", "key": "ctrl+k", "ok": True})
    except Exception as exc:
        steps.append({"step": "keypress", "key": "ctrl+k", "ok": False, "error": str(exc)})
    time.sleep(wait_ms / 1000)

    try:
        keypress(repo, hwnd, "ctrl+a")
        steps.append({"step": "keypress", "key": "ctrl+a", "ok": True})
    except Exception as exc:
        steps.append({"step": "keypress", "key": "ctrl+a", "ok": False, "error": str(exc)})
    time.sleep(0.12)

    search_input = input_action(repo, hwnd, "pastetext", chat)
    steps.append({"step": "input_search_text", "text": chat, "method": "pastetext", "result": search_input})
    time.sleep(wait_ms / 1000)
    try:
        keypress(repo, hwnd, "ctrl+k")
        steps.append({"step": "reveal_search_results", "key": "ctrl+k", "ok": True})
        time.sleep(wait_ms / 1000)
    except Exception as exc:
        steps.append({"step": "reveal_search_results", "key": "ctrl+k", "ok": False, "error": str(exc)})

    result_lines: list[str] = []
    result_matched = False
    match_line: dict[str, Any] | None = None
    match_lines: list[dict[str, Any]] = []
    rejected_match_lines: list[dict[str, Any]] = []
    if verify_result:
        search_ocr = ocr_window(repo, hwnd)
        match_lines, rejected_match_lines = find_feishu_contact_result_lines(search_ocr, chat)
        match_line = match_lines[0] if match_lines else None
        result_lines = compact_lines(search_ocr.get("text") or "", limit=18)
        result_matched = match_line is not None
        steps.append(
            {
                "step": "inspect_search_results",
                "matched": result_matched,
                "matched_line": match_line.get("text") if match_line else None,
                "matched_center": line_center(match_line) if match_line else None,
                "matched_candidates": [summarize_ocr_line(line) for line in match_lines[:5]],
                "rejected_candidates": [summarize_ocr_line(line) for line in rejected_match_lines[:5]],
                "ocr_lines": result_lines,
            }
        )

    regions = elements_for_window(repo, hwnd, query="Feishu/Lark", limit=40)
    first_result = find_virtual_region(regions, "first search/chat result")
    if not match_line and first_result:
        steps.append(
            {
                "step": "fallback_first_result_region",
                "enabled": allow_first_result,
                "center": first_result.get("center"),
            }
        )

    if dry_run:
        return 0, {
            "status": "dry_run",
            "chat": chat,
            "hwnd": hwnd,
            "first_result_matched": result_matched,
            "matched_line": match_line.get("text") if match_line else None,
            "matched_candidates": [summarize_ocr_line(line) for line in match_lines[:5]],
            "rejected_candidates": [summarize_ocr_line(line) for line in rejected_match_lines[:5]],
            "steps": steps,
        }

    if not match_line and not first_result:
        return 1, {
            "status": "not_found",
            "chat": chat,
            "hwnd": hwnd,
            "reason": "could not find a matching OCR line or Feishu first result region",
            "steps": steps,
        }
    if verify_result and not match_line and rejected_match_lines:
        return 1, {
            "status": "not_found",
            "chat": chat,
            "hwnd": hwnd,
            "reason": "search OCR only matched non-contact rows; refusing to click grouped messages, snippets, or search metadata",
            "first_result_ocr_lines": result_lines,
            "rejected_candidates": [summarize_ocr_line(line) for line in rejected_match_lines[:5]],
            "steps": steps,
        }
    if verify_result and not match_line and not allow_first_result:
        return 1, {
            "status": "not_found",
            "chat": chat,
            "hwnd": hwnd,
            "reason": "search OCR did not identify a verified contact result row for the requested chat",
            "first_result_ocr_lines": result_lines,
            "steps": steps,
        }

    ready: dict[str, Any] | None = None
    clicked_match_line: dict[str, Any] | None = None
    if match_lines:
        for candidate in match_lines[:4]:
            center = line_center(candidate)
            if not center:
                steps.append({"step": "skip_unclickable_ocr_line", "candidate": summarize_ocr_line(candidate)})
                continue
            click_point(repo, hwnd, center[0], center[1])
            time.sleep(wait_ms / 1000)
            ready = verify_feishu_chat_ready(repo, hwnd, chat)
            steps.append(
                {
                    "step": "click_and_verify_ocr_line",
                    "center": {"x": round(center[0], 2), "y": round(center[1], 2)},
                    "candidate": summarize_ocr_line(candidate),
                    "verification": ready,
                }
            )
            if ready.get("confirmed"):
                clicked_match_line = candidate
                break
        if not clicked_match_line:
            return 1, {
                "status": "not_found",
                "chat": chat,
                "hwnd": hwnd,
                "reason": "clicked OCR search result candidates, but chat title/compose placeholder did not confirm the requested recipient",
                "first_result_ocr_lines": result_lines,
                "verification": ready,
                "steps": steps,
            }
    else:
        click_element_center(repo, hwnd, first_result)
        steps.append({"step": "click_first_result", "center": first_result.get("center"), "matched": result_matched})
        time.sleep(wait_ms / 1000)
        ready = verify_feishu_chat_ready(repo, hwnd, chat)
        steps.append({"step": "verify_first_result_opened_chat", "verification": ready})
        if not ready.get("confirmed"):
            return 1, {
                "status": "not_found",
                "chat": chat,
                "hwnd": hwnd,
                "reason": "first result click did not confirm the requested recipient",
                "first_result_ocr_lines": result_lines,
                "verification": ready,
                "steps": steps,
            }

    return 0, {
        "status": "success",
        "chat": chat,
        "hwnd": hwnd,
        "first_result_matched": result_matched,
        "matched_line": clicked_match_line.get("text") if clicked_match_line else match_line.get("text") if match_line else None,
        "verification": ready,
        "first_result_ocr_lines": result_lines,
        "steps": steps,
    }


def cmd_feishu_open_chat(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    code, payload = perform_feishu_open_chat(
        repo,
        target=args.target or "\u98de\u4e66",
        hwnd=positive_int_or_none(args.hwnd),
        chat=args.chat,
        wait_ms=args.wait_ms,
        verify_result=not args.no_verify_result,
        allow_first_result=args.allow_first_result,
        dry_run=args.dry_run,
    )
    payload = attach_fast_trace(payload, command="feishu-open-chat")
    write_or_print(payload, args.output)
    return code


def perform_feishu_send_message(
    repo: Path,
    *,
    target: str,
    hwnd: int | None,
    org_name: str | None,
    chat: str,
    message: str,
    wait_ms: int,
    verify_result: bool,
    allow_first_result: bool,
    send: bool,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    resolved_hwnd = positive_int_or_none(hwnd)
    if org_name:
        switch_code, switch_payload = call_feishu_switch_org(
            repo,
            target=target,
            hwnd=resolved_hwnd,
            name=org_name,
            max_icons=4,
            wait_ms=wait_ms,
            dry_run=dry_run,
        )
        steps.append({"step": "switch_org", "result": switch_payload})
        resolved_hwnd = int(switch_payload.get("hwnd") or resolved_hwnd or 0) or None
        if switch_code != 0 or dry_run:
            return switch_code, {
                "status": "dry_run" if dry_run else "failed",
                "reason": "organization switch did not complete" if switch_code != 0 else None,
                "org_name": org_name,
                "chat": chat,
                "hwnd": resolved_hwnd,
                "steps": steps,
            }

    open_code, open_payload = perform_feishu_open_chat(
        repo,
        target=target,
        hwnd=resolved_hwnd,
        chat=chat,
        wait_ms=wait_ms,
        verify_result=verify_result,
        allow_first_result=allow_first_result,
        dry_run=dry_run,
    )
    steps.append({"step": "open_chat", "result": open_payload})
    resolved_hwnd = int(open_payload.get("hwnd") or resolved_hwnd or 0) or None
    if open_code != 0 or dry_run:
        return open_code, {
            "status": "dry_run" if dry_run else "failed",
            "reason": "chat open did not complete" if open_code != 0 else None,
            "org_name": org_name,
            "chat": chat,
            "hwnd": resolved_hwnd,
            "steps": steps,
        }

    if not resolved_hwnd:
        raise SystemExit("Feishu hwnd unavailable after opening chat")

    ready = open_payload.get("verification") if isinstance(open_payload.get("verification"), dict) else None
    if not ready or not ready.get("confirmed"):
        ready = verify_feishu_chat_ready(repo, resolved_hwnd, chat)
    steps.append({"step": "verify_chat_ready_before_input", "verification": ready})
    if not ready.get("confirmed"):
        return 1, {
            "status": "failed",
            "reason": "chat title or compose placeholder did not confirm the requested recipient before typing",
            "chat": chat,
            "hwnd": resolved_hwnd,
            "verification": ready,
            "steps": steps,
        }

    regions = elements_for_window(repo, resolved_hwnd, query="Feishu/Lark", limit=40)
    compose = find_virtual_region(regions, "compose input candidate")
    compose_center = ready.get("compose_center")
    if not compose and not compose_center:
        return 1, {
            "status": "failed",
            "reason": "could not find Feishu compose input region or verified compose placeholder",
            "chat": chat,
            "hwnd": resolved_hwnd,
            "steps": steps,
        }

    if isinstance(compose_center, (list, tuple)) and len(compose_center) >= 2:
        click_point(repo, resolved_hwnd, float(compose_center[0]), float(compose_center[1]))
    else:
        click_element_center(repo, resolved_hwnd, compose)
    time.sleep(0.2)
    message_input = input_text_with_fallback(repo, resolved_hwnd, message)
    steps.append({
        "step": "input_message",
        "message_length": len(message),
        "method": message_input.get("method"),
        "compose_center": compose_center or compose.get("center"),
    })
    if send:
        time.sleep(0.15)
        keypress(repo, resolved_hwnd, "enter")
        steps.append({"step": "send_message", "key": "enter"})
        time.sleep(max(0.35, wait_ms / 1000))
        sent_verification = verify_feishu_message_sent(repo, resolved_hwnd, chat, message)
        steps.append({"step": "verify_message_sent", "verification": sent_verification})
        if sent_verification.get("compose_still_has_message"):
            fallback = send_feishu_via_compose_child(
                repo,
                resolved_hwnd,
                chat=chat,
                message=message,
                ready=ready,
                wait_ms=wait_ms,
            )
            steps.append({"step": "send_message_compose_child_fallback", "result": fallback})
            fallback_verification = fallback.get("post_send_verification") if isinstance(fallback, dict) else None
            if isinstance(fallback_verification, dict):
                sent_verification = fallback_verification
        if not sent_verification.get("confirmed"):
            return 1, {
                "status": "unverified",
                "reason": sent_verification.get("reason") or "message send could not be verified",
                "org_name": org_name,
                "chat": chat,
                "message_sent": False,
                "hwnd": resolved_hwnd,
                "post_send_verification": sent_verification,
                "steps": steps,
            }

    return 0, {
        "status": "success",
        "org_name": org_name,
        "chat": chat,
        "message_sent": bool(send),
        "hwnd": resolved_hwnd,
        "post_send_verification": sent_verification if send else None,
        "steps": steps,
    }


def cmd_feishu_send_message(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    code, payload = perform_feishu_send_message(
        repo,
        target=args.target or "\u98de\u4e66",
        hwnd=positive_int_or_none(args.hwnd),
        org_name=args.org_name,
        chat=args.chat,
        message=args.message,
        wait_ms=args.wait_ms,
        verify_result=not args.no_verify_result,
        allow_first_result=args.allow_first_result,
        send=not args.draft_only,
        dry_run=args.dry_run,
    )
    payload = attach_fast_trace(payload, command="feishu-send-message")
    write_or_print(payload, args.output)
    return code


def perform_wechat_send_message(
    repo: Path,
    *,
    target: str,
    hwnd: int | None,
    chat: str,
    message: str,
    wait_ms: int,
    title_ocr: bool,
    draft_ocr: bool,
    require_title_match: bool,
    require_draft_match: bool,
    send: bool,
    send_method: str,
    dry_run: bool,
    sdk_input: bool,
    restore_clipboard: bool,
    clear_existing_draft: bool,
) -> tuple[int, dict[str, Any]]:
    chat_norm = normalize_match_text(chat)
    message_norm = normalize_match_text(message)
    if not chat_norm:
        raise SystemExit("--chat must contain at least one letter or number")
    if not message:
        raise SystemExit("--message is required")

    resolved_hwnd, open_result = resolve_target_hwnd(repo, target, positive_int_or_none(hwnd))
    frame = frame_from_payload(open_result)
    steps: list[dict[str, Any]] = [{"step": "resolve_target", "hwnd": resolved_hwnd, "result": open_result}]
    if frame is None:
        refreshed = run_sdk(repo, ["open", target], timeout=30)
        refreshed_hwnd = positive_int_or_none(refreshed.get("hwnd"))
        if refreshed_hwnd:
            resolved_hwnd = refreshed_hwnd
        frame = frame_from_payload(refreshed)
        steps.append({"step": "refresh_frame", "hwnd": resolved_hwnd, "result": refreshed})
    if frame is None:
        return 1, {
            "status": "failed",
            "reason": "WeChat frame was unavailable; cannot compute profile regions safely",
            "chat": chat,
            "hwnd": resolved_hwnd,
            "steps": steps,
        }

    regions = wechat_profile_regions(frame)
    planned_points = {
        "search_center": {"x": round(regions["search_center"][0], 2), "y": round(regions["search_center"][1], 2)},
        "compose_center": {"x": round(regions["compose_center"][0], 2), "y": round(regions["compose_center"][1], 2)},
        "send_center": {"x": round(regions["send_center"][0], 2), "y": round(regions["send_center"][1], 2)},
    }
    if dry_run:
        return 0, {
            "status": "dry_run",
            "chat": chat,
            "message_length": len(message),
            "hwnd": resolved_hwnd,
            "frame": frame,
            "planned_points": planned_points,
            "would_send": bool(send),
            "send_method": send_method,
            "clear_existing_draft": clear_existing_draft,
            "steps": steps,
        }

    clipboard_snapshot = get_windows_clipboard_text() if restore_clipboard and not sdk_input else None
    try:
        search_x, search_y = regions["search_center"]
        steps.append({"step": "click_search", "result": fast_click_point(repo, resolved_hwnd, search_x, search_y, sdk_input=sdk_input)})
        time.sleep(0.08)
        steps.append({"step": "select_search_text", "result": fast_keypress(repo, resolved_hwnd, "ctrl+a", sdk_input=sdk_input)})
        time.sleep(0.05)
        steps.append({"step": "paste_search_text", "text": chat, "result": fast_paste_text(repo, resolved_hwnd, chat, sdk_input=sdk_input)})
        time.sleep(max(0.2, wait_ms / 1000))
        steps.append({"step": "open_search_result", "result": fast_keypress(repo, resolved_hwnd, "enter", sdk_input=sdk_input)})
        time.sleep(max(0.25, wait_ms / 1000))

        title_verification: dict[str, Any] | None = None
        if title_ocr:
            title_rect = regions["title_ocr"]
            title_payload = ocr_rect(repo, resolved_hwnd, *title_rect)
            title_matches = find_ocr_lines_for_query(title_payload, chat, min_relative_top=0.0)
            title_verification = {
                "matched": bool(title_matches),
                "matched_candidates": [summarize_ocr_line(line) for line in title_matches[:3]],
                "ocr_lines": compact_lines(title_payload.get("text") or "", limit=8),
            }
            steps.append({"step": "verify_title_ocr", "verification": title_verification})
            if require_title_match and not title_matches:
                return 1, {
                    "status": "not_found",
                    "reason": "target chat title OCR did not match --chat",
                    "chat": chat,
                    "hwnd": resolved_hwnd,
                    "title_verification": title_verification,
                    "steps": steps,
                }

        compose_x, compose_y = regions["compose_center"]
        steps.append({"step": "click_compose", "result": fast_click_point(repo, resolved_hwnd, compose_x, compose_y, sdk_input=sdk_input)})
        time.sleep(0.08)
        if clear_existing_draft:
            steps.append({"step": "select_existing_draft", "result": fast_keypress(repo, resolved_hwnd, "ctrl+a", sdk_input=sdk_input)})
            time.sleep(0.04)
        steps.append({"step": "paste_message", "message_length": len(message), "result": fast_paste_text(repo, resolved_hwnd, message, sdk_input=sdk_input)})
        time.sleep(max(0.15, wait_ms / 1000))

        draft_verification: dict[str, Any] | None = None
        if draft_ocr:
            draft_rect = regions["draft_ocr"]
            draft_payload = ocr_rect(repo, resolved_hwnd, *draft_rect)
            draft_matches = find_ocr_lines_for_query(draft_payload, message, min_relative_top=0.0)
            draft_verification = {
                "matched": bool(draft_matches) if message_norm else False,
                "matched_candidates": [summarize_ocr_line(line) for line in draft_matches[:3]],
                "ocr_lines": compact_lines(draft_payload.get("text") or "", limit=10),
            }
            steps.append({"step": "verify_draft_ocr", "verification": draft_verification})
            if require_draft_match and not draft_matches:
                return 1, {
                    "status": "failed",
                    "reason": "message draft OCR did not match --message",
                    "chat": chat,
                    "message_sent": False,
                    "hwnd": resolved_hwnd,
                    "draft_verification": draft_verification,
                    "steps": steps,
                }

        if send:
            if send_method == "button":
                send_x, send_y = regions["send_center"]
                steps.append({"step": "click_send_button", "result": fast_click_point(repo, resolved_hwnd, send_x, send_y, sdk_input=sdk_input)})
            else:
                steps.append({"step": "focus_compose_before_enter", "result": fast_click_point(repo, resolved_hwnd, compose_x, compose_y, sdk_input=sdk_input)})
                time.sleep(0.05)
                steps.append({"step": "send_message_enter", "result": fast_keypress(repo, resolved_hwnd, "enter", sdk_input=sdk_input)})
            time.sleep(max(0.25, wait_ms / 1000))

        return 0, {
            "status": "success",
            "chat": chat,
            "message_sent": bool(send),
            "send_method": send_method if send else None,
            "hwnd": resolved_hwnd,
            "frame": frame,
            "planned_points": planned_points,
            "clear_existing_draft": clear_existing_draft,
            "title_verification": title_verification,
            "draft_verification": draft_verification,
            "steps": steps,
        }
    finally:
        if restore_clipboard and not sdk_input and clipboard_snapshot is not None:
            with contextlib.suppress(Exception):
                set_windows_clipboard_text(clipboard_snapshot)


def cmd_wechat_send_message(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    code, payload = perform_wechat_send_message(
        repo,
        target=args.target or "\u5fae\u4fe1",
        hwnd=positive_int_or_none(args.hwnd),
        chat=args.chat,
        message=args.message,
        wait_ms=args.wait_ms,
        title_ocr=not args.no_title_ocr,
        draft_ocr=not args.no_draft_ocr,
        require_title_match=args.require_title_match,
        require_draft_match=args.require_draft_match,
        send=not args.draft_only,
        send_method=args.send_method,
        dry_run=args.dry_run,
        sdk_input=args.sdk_input,
        restore_clipboard=not args.no_restore_clipboard,
        clear_existing_draft=not args.keep_existing_draft,
    )
    payload = attach_fast_trace(payload, command="wechat-send-message")
    write_or_print(payload, args.output)
    return code


CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def parse_chinese_int(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = CHINESE_DIGITS.get(left, 1 if left == "" else None)
        if tens is None:
            return None
        ones = CHINESE_DIGITS.get(right, 0 if right == "" else None)
        if ones is None:
            return None
        return tens * 10 + ones
    if len(text) == 1:
        return CHINESE_DIGITS.get(text)
    return None


def parse_report_time(value: str, *, prefer_pm: bool = False) -> tuple[int, int]:
    raw = value.strip()
    if not raw:
        raise SystemExit("time value is required")
    lower = raw.casefold()
    minute = 0
    hour: int | None = None

    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", lower)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
    else:
        match = re.search(r"(\d{1,2})\s*(?:点|時|时|h)\s*(半|(\d{1,2})\s*分?)?", lower)
        if match:
            hour = int(match.group(1))
            minute = 30 if match.group(2) == "半" else int(match.group(3) or 0)
        else:
            match = re.search(r"([零〇一二两三四五六七八九十]{1,3})\s*(?:点|時|时)\s*(半|([零〇一二两三四五六七八九十]{1,3}|\d{1,2})\s*分?)?", raw)
            if match:
                hour = parse_chinese_int(match.group(1))
                if match.group(2) == "半":
                    minute = 30
                elif match.group(3):
                    minute_value = parse_chinese_int(match.group(3))
                    if minute_value is None:
                        raise SystemExit(f"could not parse minute from time: {value}")
                    minute = minute_value

    if hour is None:
        match = re.fullmatch(r"\d{1,2}", lower)
        if match:
            hour = int(lower)

    if hour is None:
        raise SystemExit(f"could not parse time: {value}")
    if any(term in lower for term in ("pm", "下午", "晚上", "晚间", "傍晚", "夜里", "夜间")) or prefer_pm:
        if 1 <= hour < 12:
            hour += 12
    if any(term in lower for term in ("am", "上午", "早上", "早晨", "清晨", "凌晨")) and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit(f"time out of range: {value}")
    return hour, minute


def parse_report_date(value: str | None) -> dt.date:
    raw = (value or "").strip()
    if not raw or raw in {"今天", "今日", "today"}:
        return dt.date.today()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"could not parse date: {value}")


def format_report_datetime(date_value: str | None, time_value: str, *, prefer_pm: bool = False) -> str:
    date = parse_report_date(date_value)
    hour, minute = parse_report_time(time_value, prefer_pm=prefer_pm)
    return f"{date:%Y-%m-%d} {hour:02d}:{minute:02d}"


def datetime_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def find_nearest_line(payload: dict[str, Any], terms: tuple[str, ...], *, min_top: float | None = None) -> dict[str, Any] | None:
    normalized_terms = [normalize_match_text(term) for term in terms if normalize_match_text(term)]
    if not normalized_terms:
        return None
    candidates: list[dict[str, Any]] = []
    for line in payload.get("lines") or []:
        if not isinstance(line, dict):
            continue
        if min_top is not None and line_top(line) < min_top:
            continue
        norm = normalize_match_text(line.get("text"))
        if any(term in norm for term in normalized_terms):
            candidates.append(line)
    return sorted(candidates, key=line_top)[0] if candidates else None


def click_and_paste(repo: Path, hwnd: int, x: float, y: float, text: str) -> dict[str, Any]:
    click_point(repo, hwnd, x, y)
    time.sleep(0.15)
    keypress(repo, hwnd, "ctrl+a")
    time.sleep(0.1)
    return input_action(repo, hwnd, "pastetext", text)


def verify_report_form_visible(capture: dict[str, Any]) -> bool:
    norm = normalize_match_text(capture.get("text"))
    # The form can be scrolled so the start/end labels are temporarily outside
    # the OCR viewport. The header still contains the report title plus the
    # autosave state; accept that as a real edit form, not a dashboard.
    form_shell_visible = bool(
        "已实时保存" in norm
        and "返回" in norm
        and ("工作日报和工作时间" in norm or ("工作日报" in norm and "工作时间" in norm))
    )
    recipient_section_visible = bool("已实时保存" in norm and "汇报给谁" in norm and "是否允许他人转发" in norm)
    has_required_fields = bool(
        ("工作日报" in norm or "工作时间" in norm)
        and ("工作起始时间" in norm or "起始时间" in norm)
        and ("工作结束时间" in norm or "结束时间" in norm)
    )
    # The summary dashboard table has the same column labels as the form. Treat
    # it as non-form unless we also see form-specific chrome or the exact submit
    # button. This avoids stopping early on the 汇报统计 page.
    if "汇报统计看板" in norm or ("查看模式" in norm and "导出" in norm):
        return False
    if form_shell_visible or recipient_section_visible:
        return True
    if not has_required_fields:
        return False
    has_exact_submit = any(
        isinstance(line, dict) and normalize_match_text(line.get("text")) == "提交"
        for line in capture.get("lines") or []
    )
    return bool(
        "已实时保存" in norm
        or ("返回" in norm and ("请输入" in norm or "需要协调与帮助" in norm or has_exact_submit))
        or ("需要协调与帮助" in norm and ("请输入" in norm or has_exact_submit))
    )


def ensure_report_form_edit_top(repo: Path, hwnd: int, capture: dict[str, Any], *, wait_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []

    def has_top_fields(payload: dict[str, Any]) -> bool:
        return bool(
            find_nearest_line(payload, ("今日总结",))
            and find_nearest_line(payload, ("工作起始时间", "起始时间"))
            and find_nearest_line(payload, ("工作结束时间", "结束时间"))
        )

    if has_top_fields(capture):
        return capture, {"status": "already_at_edit_top", "attempts": attempts}

    for key in ("ctrl+home", "home"):
        try:
            key_result = keypress(repo, hwnd, key)
            time.sleep(max(wait_ms, 650) / 1000)
            latest = ocr_window(repo, hwnd)
            ok = has_top_fields(latest)
            attempts.append(
                {
                    "key": key,
                    "result": key_result,
                    "top_fields_visible": ok,
                    "lines_after": compact_lines(latest.get("text") or "", limit=18),
                }
            )
            if ok:
                return latest, {"status": "success", "attempts": attempts}
            capture = latest
        except Exception as exc:
            attempts.append({"key": key, "ok": False, "error": str(exc)})

    return capture, {"status": "not_at_top", "attempts": attempts}


def find_report_entry_buttons(capture: dict[str, Any]) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for line in capture.get("lines") or []:
        if not isinstance(line, dict):
            continue
        norm = normalize_match_text(line.get("text"))
        if "现在去写" not in norm:
            continue
        center = line_center(line)
        if not center:
            continue
        buttons.append(
            {
                "line": line,
                "text": str(line.get("text") or ""),
                "center": center,
                "top": line_top(line),
            }
        )

    # The latest Feishu report reminder is normally the topmost visible card.
    # Older cards below it may also contain "现在去写", so keep order stable by y.
    return sorted(buttons, key=lambda item: item["top"])


def summarize_report_entry_button(button: dict[str, Any]) -> dict[str, Any]:
    center = button.get("center") or (0.0, 0.0)
    return {
        "text": button.get("text"),
        "center": {"x": round(float(center[0]), 2), "y": round(float(center[1]), 2)},
        "top": round(float(button.get("top") or 0), 2),
    }


def fuzzy_report_word(norm: str) -> bool:
    return "汇报" in norm or "匚报" in norm or "氵匚报" in norm or "扌皮" in norm


def fuzzy_write_report_line(line: dict[str, Any]) -> bool:
    norm = normalize_match_text(line.get("text"))
    return "写" in norm and fuzzy_report_word(norm)


def find_dashboard_write_report_button(capture: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for line in capture.get("lines") or []:
        if not isinstance(line, dict) or not fuzzy_write_report_line(line):
            continue
        center = line_center(line)
        if not center:
            continue
        # The dashboard button is in the top-right header. The picker title also
        # says "写汇报", but it appears lower and must not be clicked as a button.
        if center[0] < 1200 or center[1] > 240:
            continue
        candidates.append({"line": line, "center": center, "top": line_top(line)})
    return sorted(candidates, key=lambda item: item["center"][0], reverse=True)[0] if candidates else None


def report_template_picker_visible(capture: dict[str, Any]) -> bool:
    norm = normalize_match_text(capture.get("text"))
    has_picker_title = any(isinstance(line, dict) and fuzzy_write_report_line(line) and line_top(line) > 250 for line in capture.get("lines") or [])
    return bool(
        has_picker_title
        and ("工作周报" in norm or "工作月报" in norm)
        and ("工作日报" in norm or "工作日报和工作" in norm)
    )


def find_daily_report_template_card(capture: dict[str, Any]) -> dict[str, Any] | None:
    title_tops = [
        line_top(line)
        for line in capture.get("lines") or []
        if isinstance(line, dict) and fuzzy_write_report_line(line) and line_top(line) > 250
    ]
    min_top = min(title_tops) + 60 if title_tops else 330
    candidates: list[dict[str, Any]] = []
    for line in capture.get("lines") or []:
        if not isinstance(line, dict):
            continue
        norm = normalize_match_text(line.get("text"))
        if "工作日报和工作" not in norm and "工作日报" not in norm:
            continue
        center = line_center(line)
        if not center:
            continue
        if line_top(line) < min_top or center[0] < 650:
            continue
        candidates.append({"line": line, "center": center, "top": line_top(line)})
    return sorted(candidates, key=lambda item: (item["top"], item["center"][0]))[0] if candidates else None


def report_detail_drawer_visible(capture: dict[str, Any]) -> bool:
    norm = normalize_match_text(capture.get("text"))
    return bool("历史内容" in norm or ("编辑" in norm and "转发" in norm and "已读" in norm))


def report_app_context_visible(capture: dict[str, Any]) -> bool:
    if verify_report_form_visible(capture) or report_template_picker_visible(capture):
        return True
    if find_report_entry_buttons(capture):
        return True
    norm = normalize_match_text(capture.get("text"))
    if "清除" in norm and all(marker in norm for marker in ("应用", "联系人", "群组", "日程")):
        return False
    return bool(
        ("飞书汇报" in norm and any(marker in norm for marker in ("按人看", "汇总看", "我的汇报", "汇报统计看板", "写汇报", "配置仪表盘")))
        or ("汇报统计看板" in norm and ("工作日报和工作时间" in norm or "更新数据" in norm))
        or ("工作日报和工作时间" in norm and ("返回" in norm or "今日总结" in norm or "汇报统计看板" in norm))
    )


def capture_region(capture: dict[str, Any]) -> dict[str, float]:
    region = ((capture.get("capture") or {}).get("region") or {})
    if not isinstance(region, dict):
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    try:
        return {
            "x": float(region.get("x") or 0),
            "y": float(region.get("y") or 0),
            "width": float(region.get("width") or 0),
            "height": float(region.get("height") or 0),
        }
    except (TypeError, ValueError):
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}


def focus_feishu_window(repo: Path, hwnd: int, capture: dict[str, Any] | None = None) -> dict[str, Any] | None:
    region = capture_region(capture or {})
    if region["width"] <= 0 or region["height"] <= 0:
        return None
    focus_x = region["x"] + region["width"] / 2.0
    focus_y = region["y"] + 18
    return click_point(repo, hwnd, focus_x, focus_y)


def find_left_rail_report_entry(capture: dict[str, Any]) -> dict[str, Any] | None:
    region = capture_region(capture)
    left_limit = region["x"] + min(340, max(220, region["width"] * 0.18))
    top_limit = region["y"] + 150
    bottom_limit = region["y"] + max(0, region["height"] - 90)
    candidates: list[dict[str, Any]] = []
    for line in capture.get("lines") or []:
        if not isinstance(line, dict):
            continue
        norm = normalize_match_text(line.get("text"))
        if not fuzzy_report_word(norm):
            continue
        center = line_center(line)
        if not center:
            continue
        x, y = center
        if x > left_limit or y < top_limit or y > bottom_limit:
            continue
        score = 0
        if norm in {"汇报", "飞书汇报"}:
            score += 80
        if x <= region["x"] + 190:
            score += 30
        if y >= region["y"] + 650:
            score += 20
        candidates.append({"line": line, "center": center, "top": line_top(line), "score": score})
    return sorted(candidates, key=lambda item: (-int(item["score"]), item["center"][0], item["top"]))[0] if candidates else None


def find_report_search_candidates(capture: dict[str, Any]) -> list[dict[str, Any]]:
    region = capture_region(capture)
    candidates: list[dict[str, Any]] = []
    for index, line in enumerate(capture.get("lines") or []):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "")
        norm = normalize_match_text(text)
        if not norm or not fuzzy_report_word(norm):
            continue
        if norm.startswith("q"):
            continue
        if any(marker in norm for marker in ("如何", "哪里", "流程", "附件", "关键词")):
            continue
        center = line_center(line)
        if not center:
            continue
        x, y = center
        if "搜索" in norm and y < region["y"] + 260:
            continue
        if y < region["y"] + 150:
            continue

        score = 0
        if "飞书汇报" in norm:
            score += 120
        if norm in {"汇报", "飞书汇报"}:
            score += 80
        if "应用" in norm or "工作台" in norm:
            score += 25
        if x >= region["x"] + 320:
            score += 45
        if region["y"] + 220 <= y <= region["y"] + 920:
            score += 20
        if "消息" in norm or "发送给" in norm:
            score -= 80
        enriched = dict(line)
        enriched["_match_score"] = score
        enriched["_match_index"] = index
        candidates.append({"line": enriched, "center": center, "score": score, "top": line_top(line)})
    return sorted(candidates, key=lambda item: (-int(item["score"]), item["top"], int(item["line"].get("_match_index") or 0)))


def open_feishu_report_app(
    repo: Path,
    hwnd: int,
    *,
    wait_ms: int,
    initial_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture = initial_capture or ocr_window(repo, hwnd)
    attempts: list[dict[str, Any]] = []
    poll_delay = max(wait_ms, 650) / 1000

    if report_app_context_visible(capture):
        return {"status": "success", "capture": capture, "already_visible": True, "attempts": attempts}

    for key in ("escape", "escape"):
        try:
            key_result = keypress(repo, hwnd, key)
            attempts.append({"step": "dismiss_popover", "key": key, "result": key_result})
            time.sleep(0.12)
        except Exception as exc:
            attempts.append({"step": "dismiss_popover", "key": key, "ok": False, "error": str(exc)})

    capture = ocr_window(repo, hwnd)
    if report_app_context_visible(capture):
        return {"status": "success", "capture": capture, "already_visible": True, "attempts": attempts}

    left_entry = find_left_rail_report_entry(capture)
    if left_entry:
        center = left_entry["center"]
        click_result = click_point(repo, hwnd, center[0], center[1])
        time.sleep(poll_delay)
        capture = ocr_window(repo, hwnd)
        attempts.append(
            {
                "step": "click_left_rail_report_entry",
                "candidate": summarize_ocr_line(left_entry["line"]),
                "click_result": click_result,
                "report_app_visible": report_app_context_visible(capture),
                "lines_after": compact_lines(capture.get("text") or "", limit=16),
            }
        )
        if report_app_context_visible(capture):
            return {"status": "success", "capture": capture, "already_visible": False, "attempts": attempts}

    for query in ("汇报",):
        try:
            focus_result = focus_feishu_window(repo, hwnd, capture)
            if focus_result:
                attempts.append({"step": "focus_window_before_search", "result": focus_result})
                time.sleep(0.12)
        except Exception as exc:
            attempts.append({"step": "focus_window_before_search", "ok": False, "error": str(exc)})
        try:
            keypress(repo, hwnd, "ctrl+k")
            time.sleep(poll_delay)
            keypress(repo, hwnd, "ctrl+a")
            time.sleep(0.12)
            input_result = input_action(repo, hwnd, "pastetext", query)
            time.sleep(poll_delay)
            with contextlib.suppress(Exception):
                keypress(repo, hwnd, "ctrl+k")
                time.sleep(poll_delay)
            search_capture = ocr_window(repo, hwnd)
            candidates = find_report_search_candidates(search_capture)
            attempt: dict[str, Any] = {
                "step": "global_search_report_app",
                "query": query,
                "input_result": input_result,
                "candidates": [summarize_ocr_line(candidate["line"]) for candidate in candidates[:5]],
            }
            attempts.append(attempt)
        except Exception as exc:
            attempts.append({"step": "global_search_report_app", "query": query, "ok": False, "error": str(exc)})
            continue

        for candidate in candidates[:5]:
            center = candidate["center"]
            click_result = click_point(repo, hwnd, center[0], center[1])
            time.sleep(max(wait_ms, 900) / 1000)
            capture = ocr_window(repo, hwnd)
            visible = report_app_context_visible(capture)
            attempts.append(
                {
                    "step": "click_report_search_candidate",
                    "query": query,
                    "candidate": summarize_ocr_line(candidate["line"]),
                    "click_result": click_result,
                    "report_app_visible": visible,
                    "lines_after": compact_lines(capture.get("text") or "", limit=16),
                }
            )
            if visible:
                return {"status": "success", "capture": capture, "already_visible": False, "attempts": attempts}

    return {
        "status": "failed",
        "reason": "could not navigate to the Feishu report app from the current Feishu page",
        "attempts": attempts,
        "lines": compact_lines(capture.get("text") or "", limit=24),
    }


def open_report_form_from_any_feishu_state(
    repo: Path,
    hwnd: int,
    *,
    target: str,
    wait_ms: int,
    initial_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture = initial_capture or ocr_window(repo, hwnd)
    attempts: list[dict[str, Any]] = []

    for phase in ("current_view", "after_report_app_navigation"):
        if verify_report_form_visible(capture):
            return {"status": "success", "capture": capture, "attempts": attempts}

        entry_buttons = find_report_entry_buttons(capture)
        if entry_buttons:
            reminder_result = open_report_form_from_reminder(repo, hwnd, wait_ms=wait_ms, initial_capture=capture)
            reminder_capture = reminder_result.pop("capture", None)
            attempts.append({"phase": phase, "step": "open_report_form_from_reminder", "result": reminder_result})
            if reminder_capture:
                return {"status": "success", "capture": reminder_capture, "attempts": attempts}
            capture = ocr_window(repo, hwnd)

        report_app_result = open_report_form_from_report_app(repo, hwnd, wait_ms=wait_ms, initial_capture=capture)
        report_app_capture = report_app_result.pop("capture", None)
        attempts.append({"phase": phase, "step": "open_report_form_from_report_app", "result": report_app_result})
        if report_app_capture:
            return {"status": "success", "capture": report_app_capture, "attempts": attempts}

        if phase == "after_report_app_navigation":
            break

        navigation_result = open_feishu_report_app(repo, hwnd, wait_ms=wait_ms, initial_capture=capture)
        navigation_capture = navigation_result.pop("capture", None)
        attempts.append({"phase": phase, "step": "open_feishu_report_app", "result": navigation_result})
        if not navigation_capture:
            break
        capture = navigation_capture

    bot_code, bot_payload = perform_feishu_open_chat(
        repo,
        target=target,
        hwnd=hwnd,
        chat="汇报机器人",
        wait_ms=wait_ms,
        verify_result=True,
        allow_first_result=True,
        dry_run=False,
    )
    attempts.append({"phase": "fallback_report_bot", "step": "open_report_bot_chat", "code": bot_code, "result": bot_payload})
    if bot_code == 0:
        capture = ocr_window(repo, hwnd)
        reminder_result = open_report_form_from_reminder(repo, hwnd, wait_ms=wait_ms, initial_capture=capture)
        reminder_capture = reminder_result.pop("capture", None)
        attempts.append({"phase": "fallback_report_bot", "step": "open_report_form_from_reminder", "result": reminder_result})
        if reminder_capture:
            return {"status": "success", "capture": reminder_capture, "attempts": attempts}

    return {
        "status": "failed",
        "reason": "could not open the Feishu daily report form from the current Feishu state",
        "attempts": attempts,
        "lines": compact_lines(capture.get("text") or "", limit=24),
    }


def open_report_form_from_report_app(
    repo: Path,
    hwnd: int,
    *,
    wait_ms: int,
    initial_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture = initial_capture or ocr_window(repo, hwnd)
    attempts: list[dict[str, Any]] = []
    poll_delay = max(wait_ms, 650) / 1000
    dismissed_overlay = False

    for _ in range(6):
        if verify_report_form_visible(capture):
            return {"status": "success", "capture": capture, "attempts": attempts}

        if report_template_picker_visible(capture):
            card = find_daily_report_template_card(capture)
            if not card:
                return {
                    "status": "failed",
                    "reason": "report template picker is visible, but the 工作日报和工作时间 card was not found",
                    "attempts": attempts,
                    "lines": compact_lines(capture.get("text") or "", limit=24),
                }
            center = card["center"]
            click_result = click_point(repo, hwnd, center[0], center[1])
            time.sleep(poll_delay)
            capture = ocr_window(repo, hwnd)
            attempts.append(
                {
                    "step": "click_daily_report_template_card",
                    "button": summarize_report_entry_button({"text": card["line"].get("text"), "center": center, "top": card["top"]}),
                    "click_result": click_result,
                    "form_visible": verify_report_form_visible(capture),
                }
            )
            continue

        button = find_dashboard_write_report_button(capture)
        if button:
            center = button["center"]
            click_result = click_point(repo, hwnd, center[0], center[1])
            time.sleep(poll_delay)
            capture = ocr_window(repo, hwnd)
            attempts.append(
                {
                    "step": "click_dashboard_write_report",
                    "button": summarize_report_entry_button({"text": button["line"].get("text"), "center": center, "top": button["top"]}),
                    "click_result": click_result,
                    "picker_visible": report_template_picker_visible(capture),
                    "form_visible": verify_report_form_visible(capture),
                }
            )
            continue

        if not dismissed_overlay and report_detail_drawer_visible(capture):
            key_result = keypress(repo, hwnd, "esc")
            time.sleep(poll_delay)
            capture = ocr_window(repo, hwnd)
            dismissed_overlay = True
            attempts.append(
                {
                    "step": "dismiss_report_detail_drawer",
                    "result": key_result,
                    "lines_after": compact_lines(capture.get("text") or "", limit=16),
                }
            )
            continue

        break

    return {
        "status": "failed",
        "reason": "could not open the Feishu daily report form from the current report app view",
        "attempts": attempts,
        "lines": compact_lines(capture.get("text") or "", limit=24),
    }


def open_report_form_from_reminder(
    repo: Path,
    hwnd: int,
    *,
    wait_ms: int,
    initial_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture = initial_capture or ocr_window(repo, hwnd)
    buttons = find_report_entry_buttons(capture)
    attempts: list[dict[str, Any]] = []
    if not buttons:
        return {
            "status": "failed",
            "reason": "could not find a visible 现在去写 report entry button",
            "buttons": [],
            "lines": compact_lines(capture.get("text") or "", limit=24),
        }

    poll_delay = max(wait_ms, 500) / 1000
    for button in buttons[:4]:
        center = button["center"]
        click_result = click_point(repo, hwnd, center[0], center[1])
        attempt = {
            "button": summarize_report_entry_button(button),
            "click_result": click_result,
            "verified": False,
        }
        latest_capture: dict[str, Any] | None = None
        for _ in range(6):
            time.sleep(poll_delay)
            latest_capture = ocr_window(repo, hwnd)
            if verify_report_form_visible(latest_capture):
                attempt["verified"] = True
                attempts.append(attempt)
                return {
                    "status": "success",
                    "capture": latest_capture,
                    "buttons": [summarize_report_entry_button(item) for item in buttons],
                    "attempts": attempts,
                }
        if latest_capture:
            attempt["lines_after_click"] = compact_lines(latest_capture.get("text") or "", limit=16)
        attempts.append(attempt)

    return {
        "status": "failed",
        "reason": "clicked visible report entry button(s), but the daily report form did not become visible",
        "buttons": [summarize_report_entry_button(item) for item in buttons],
        "attempts": attempts,
    }


def set_report_datetime_field(
    repo: Path,
    hwnd: int,
    *,
    label_terms: tuple[str, ...],
    value: str,
    wait_ms: int,
) -> dict[str, Any]:
    before = ocr_window(repo, hwnd)
    label = find_nearest_line(before, label_terms)
    if not label:
        return {
            "status": "failed",
            "reason": f"could not find datetime field label: {label_terms}",
            "window_lines": compact_lines(before.get("text") or "", limit=20),
        }
    label_center = line_center(label)
    if not label_center:
        return {"status": "failed", "reason": "datetime label has no center", "label": summarize_ocr_line(label)}

    field_x = label_center[0] + 35
    field_y = label_center[1] + 60
    click_point(repo, hwnd, field_x, field_y)
    time.sleep(wait_ms / 1000)
    paste_result = click_and_paste(repo, hwnd, field_x + 80, field_y, value)
    time.sleep(0.25)

    popup = ocr_window(repo, hwnd)
    confirm_candidates: list[dict[str, Any]] = []
    for line in popup.get("lines") or []:
        if not isinstance(line, dict):
            continue
        if "确定" not in normalize_match_text(line.get("text")):
            continue
        center = line_center(line)
        if not center:
            continue
        if center[0] >= field_x + 250 and field_y - 240 <= center[1] <= field_y + 100:
            confirm_candidates.append(line)
    confirm = sorted(confirm_candidates, key=lambda item: abs((line_center(item) or (0, 0))[1] - field_y))[0] if confirm_candidates else None
    if not confirm:
        return {
            "status": "failed",
            "reason": "could not find datetime picker confirm button",
            "label": summarize_ocr_line(label),
            "popup_lines": compact_lines(popup.get("text") or "", limit=24),
        }
    confirm_center = line_center(confirm)
    click_point(repo, hwnd, confirm_center[0], confirm_center[1])
    time.sleep(wait_ms / 1000)

    after = ocr_rect(repo, hwnd, field_x - 180, field_y - 95, 720, 150)
    expected = datetime_digits(value)
    actual = datetime_digits(after.get("text") or "")
    ok = expected in actual
    return {
        "status": "success" if ok else "failed",
        "reason": None if ok else "datetime field verification failed",
        "value": value,
        "label": summarize_ocr_line(label),
        "field_center": {"x": round(field_x, 2), "y": round(field_y, 2)},
        "paste_result": paste_result,
        "confirm_center": {"x": round(confirm_center[0], 2), "y": round(confirm_center[1], 2)},
        "verification_lines": compact_lines(after.get("text") or "", limit=8),
    }


def find_report_submit_button(capture: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for line in capture.get("lines") or []:
        if not isinstance(line, dict):
            continue
        norm = normalize_match_text(line.get("text"))
        if norm != "提交":
            continue
        center = line_center(line)
        if not center:
            continue
        candidates.append({"line": line, "center": center, "top": line_top(line)})
    if candidates:
        return sorted(candidates, key=lambda item: item["top"], reverse=True)[0]

    return None


def infer_report_submit_center(capture: dict[str, Any]) -> tuple[tuple[float, float] | None, dict[str, Any]]:
    region = capture_region(capture)
    if region["width"] <= 0 or region["height"] <= 0:
        return None, {"reason": "window OCR did not return a valid region"}

    anchors: list[dict[str, Any]] = []
    for terms in (
        ("今日总结",),
        ("工作起始时间", "起始时间"),
        ("工作结束时间", "结束时间"),
        ("需要协调与帮助", "协调与帮助"),
        ("汇报给谁",),
    ):
        line = find_nearest_line(capture, terms)
        center = line_center(line) if line else None
        if center:
            anchors.append({"terms": terms, "line": summarize_ocr_line(line), "center": center})

    if anchors:
        anchor_x = min(float(item["center"][0]) for item in anchors)
        submit_x = max(region["x"] + 120, anchor_x + 22)
        source = "form_anchor_bottom_bar"
    else:
        submit_x = region["x"] + region["width"] * 0.36
        source = "window_ratio_bottom_bar"

    # Feishu keeps the submit action in a fixed bottom bar. OCR often misses the
    # text because it sits close to the taskbar, so aim at the visual center.
    submit_y = region["y"] + region["height"] - 42
    return (submit_x, submit_y), {
        "source": source,
        "window_region": region,
        "anchors": anchors[:5],
    }


def submit_report_form(repo: Path, hwnd: int, *, wait_ms: int) -> dict[str, Any]:
    before = ocr_window(repo, hwnd)
    button = find_report_submit_button(before)
    click_source = "ocr_submit_button"
    inference: dict[str, Any] | None = None
    if button:
        center = button["center"]
    else:
        center, inference = infer_report_submit_center(before)
        if not center:
            return {
                "status": "failed",
                "reason": "could not locate the submit button or infer the bottom submit bar",
                "inference": inference,
                "lines": compact_lines(before.get("text") or "", limit=24),
            }
        click_source = "bottom_bar_fallback"

    click_result = click_point(repo, hwnd, center[0], center[1])
    time.sleep(max(wait_ms, 900) / 1000)
    after = ocr_window(repo, hwnd)
    norm_after = normalize_match_text(after.get("text"))
    verified = (
        "提交成功" in norm_after
        or "已提交" in norm_after
        or "提交修改" in norm_after
        or "再次提交" in norm_after
        or not verify_report_form_visible(after)
    )
    return {
        "status": "success" if verified else "unknown",
        "source": click_source,
        "center": {"x": round(float(center[0]), 2), "y": round(float(center[1]), 2)},
        "inference": inference,
        "click_result": click_result,
        "verification_lines": compact_lines(after.get("text") or "", limit=24),
    }


def perform_feishu_fill_daily_report(
    repo: Path,
    *,
    target: str,
    hwnd: int | None,
    org_name: str | None,
    summary: str | None,
    start_time: str,
    end_time: str,
    date_value: str | None,
    help_text: str | None,
    submit: bool,
    open_form_only: bool,
    wait_ms: int,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    resolved_hwnd = positive_int_or_none(hwnd)
    if org_name:
        switch_code, switch_payload = call_feishu_switch_org(
            repo,
            target=target,
            hwnd=resolved_hwnd,
            name=org_name,
            max_icons=4,
            wait_ms=wait_ms,
            dry_run=dry_run,
        )
        steps.append({"step": "switch_org", "result": switch_payload})
        resolved_hwnd = int(switch_payload.get("hwnd") or resolved_hwnd or 0) or None
        if switch_code != 0 or dry_run:
            return switch_code, {
                "status": "dry_run" if dry_run else "failed",
                "reason": "organization switch did not complete" if switch_code != 0 else None,
                "org_name": org_name,
                "hwnd": resolved_hwnd,
                "steps": steps,
            }

    hwnd, open_result = resolve_target_hwnd(repo, target, resolved_hwnd)
    start_value = format_report_datetime(date_value, start_time)
    end_value = format_report_datetime(date_value, end_time, prefer_pm=True)
    capture = ocr_window(repo, hwnd)
    steps.extend(
        [
            {"step": "resolve_target", "hwnd": hwnd, "result": open_result},
            {
                "step": "plan_values",
                "org_name": org_name,
                "summary": summary,
                "start_datetime": start_value,
                "end_datetime": end_value,
                "help_text": help_text,
                "submit": submit,
            },
        ]
    )
    form_visible = verify_report_form_visible(capture)
    steps.append({"step": "verify_report_form_visible", "ok": form_visible, "lines": compact_lines(capture.get("text") or "", limit=24)})
    if dry_run:
        return 0, {"status": "dry_run", "hwnd": hwnd, "form_visible": form_visible, "steps": steps}
    if not form_visible:
        open_form_result = open_report_form_from_any_feishu_state(repo, hwnd, target=target, wait_ms=wait_ms, initial_capture=capture)
        opened_capture = open_form_result.pop("capture", None)
        steps.append({"step": "open_report_form_from_any_feishu_state", "result": open_form_result})
        if not opened_capture:
            return 1, {
                "status": "failed",
                "reason": "Feishu daily report form is not visible, and no supported navigation path could open it automatically",
                "hwnd": hwnd,
                "steps": steps,
            }
        capture = opened_capture
        form_visible = True

    if open_form_only:
        return 0, {
            "status": "success",
            "hwnd": hwnd,
            "org_name": org_name,
            "opened_form": True,
            "summary": summary,
            "start_datetime": start_value,
            "end_datetime": end_value,
            "help_text": help_text,
            "submitted": False,
            "submit_status": "skipped",
            "steps": steps,
        }

    capture, top_result = ensure_report_form_edit_top(repo, hwnd, capture, wait_ms=wait_ms)
    steps.append({"step": "ensure_report_form_edit_top", "result": top_result})
    if top_result.get("status") == "not_at_top":
        return 1, {
            "status": "failed",
            "reason": "Feishu daily report form is visible but its editable top fields could not be brought into view",
            "hwnd": hwnd,
            "steps": steps,
        }

    if summary:
        start_label = find_nearest_line(capture, ("工作起始时间", "起始时间"))
        start_center = line_center(start_label) if start_label else None
        if start_center:
            summary_x = start_center[0] + 120
            summary_y = max(180, start_center[1] - 248)
            result = click_and_paste(repo, hwnd, summary_x, summary_y, summary)
            steps.append({"step": "fill_summary", "center": {"x": round(summary_x, 2), "y": round(summary_y, 2)}, "result": result})
            time.sleep(wait_ms / 1000)
        else:
            steps.append({"step": "fill_summary", "ok": False, "reason": "could not anchor summary from start-time label"})

    start_result = set_report_datetime_field(
        repo,
        hwnd,
        label_terms=("工作起始时间", "起始时间"),
        value=start_value,
        wait_ms=wait_ms,
    )
    steps.append({"step": "set_start_datetime", "result": start_result})
    if start_result.get("status") != "success":
        return 1, {"status": "failed", "reason": "failed to set report start time", "hwnd": hwnd, "steps": steps}

    end_result = set_report_datetime_field(
        repo,
        hwnd,
        label_terms=("工作结束时间", "结束时间"),
        value=end_value,
        wait_ms=wait_ms,
    )
    steps.append({"step": "set_end_datetime", "result": end_result})
    if end_result.get("status") != "success":
        return 1, {"status": "failed", "reason": "failed to set report end time", "hwnd": hwnd, "steps": steps}

    if help_text:
        final_capture = ocr_window(repo, hwnd)
        help_label = find_nearest_line(final_capture, ("需要协调与帮助", "协调与帮助"))
        help_center = line_center(help_label) if help_label else None
        if help_center:
            result = click_and_paste(repo, hwnd, help_center[0] + 120, help_center[1] + 145, help_text)
            steps.append({"step": "fill_help_text", "result": result})
        else:
            steps.append({"step": "fill_help_text", "ok": False, "reason": "could not find help text label"})

    submit_result: dict[str, Any] | None = None
    if submit:
        submit_result = submit_report_form(repo, hwnd, wait_ms=wait_ms)
        steps.append({"step": "submit_report_form", "result": submit_result})
        if submit_result.get("status") == "failed":
            return 1, {"status": "failed", "reason": "failed to submit report form", "hwnd": hwnd, "steps": steps}

    return 0, {
        "status": "success",
        "hwnd": hwnd,
        "org_name": org_name,
        "summary": summary,
        "start_datetime": start_value,
        "end_datetime": end_value,
        "help_text": help_text,
        "submitted": bool(submit and submit_result and submit_result.get("status") == "success"),
        "submit_status": submit_result.get("status") if submit_result else "skipped",
        "autosave_note": "Feishu report page indicates changes are saved automatically before final submit.",
        "steps": steps,
    }


def cmd_feishu_fill_daily_report(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    code, payload = perform_feishu_fill_daily_report(
        repo,
        target=args.target or "\u98de\u4e66",
        hwnd=positive_int_or_none(args.hwnd),
        org_name=args.org_name,
        summary=args.summary,
        start_time=args.start_time,
        end_time=args.end_time,
        date_value=args.date,
        help_text=args.help_text,
        submit=not args.no_submit,
        open_form_only=args.open_form_only,
        wait_ms=args.wait_ms,
        dry_run=args.dry_run,
    )
    payload = attach_fast_trace(payload, command="feishu-fill-daily-report")
    write_or_print(payload, args.output)
    return code


def add_global(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=None,
        help="WindowsUseSDK checkout. Defaults to the bundled vendor/WindowsUseSDK, then WINDOWS_USE_SDK_ROOT or auto-detection.",
    )
    parser.add_argument("--output", type=Path)


def cmd_list_apps(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["list-apps"]
    if args.query:
        sdk_args.extend(["--query", args.query])
    if args.limit:
        sdk_args.extend(["--limit", str(args.limit)])
    write_or_print(run_sdk(repo, sdk_args, timeout=20), args.output)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["open", args.identifier]
    if args.no_activate:
        sdk_args.append("--no-activate")
    write_or_print(run_sdk(repo, sdk_args, timeout=30), args.output)
    return 0


def target_args(args: argparse.Namespace) -> list[str]:
    result: list[str] = []
    hwnd = positive_int_or_none(getattr(args, "hwnd", None))
    pid = positive_int_or_none(getattr(args, "pid", None))
    if hwnd is not None:
        result.extend(["--hwnd", str(hwnd)])
    if pid is not None:
        result.extend(["--pid", str(pid)])
    if getattr(args, "target", None):
        result.extend(["--target", args.target])
    return result


def cmd_traverse(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["traverse", *target_args(args)]
    sdk_args.extend(["--view", args.view])
    if args.visible_only:
        sdk_args.append("--visible-only")
    if args.no_activate:
        sdk_args.append("--no-activate")
    write_or_print(run_sdk(repo, sdk_args, timeout=25), args.output)
    return 0


def cmd_elements(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["elements", *target_args(args)]
    sdk_args.extend(["--view", args.view])
    if args.visible_only:
        sdk_args.append("--visible-only")
    if args.no_activate:
        sdk_args.append("--no-activate")
    if args.limit:
        sdk_args.extend(["--limit", str(args.limit)])
    if args.query:
        sdk_args.extend(["--query", args.query])
    write_or_print(run_sdk(repo, sdk_args, timeout=25), args.output)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = [
        "probe",
        *target_args(args),
        "--x",
        str(args.x),
        "--y",
        str(args.y),
        "--view",
        args.view,
        "--hover-ms",
        str(args.hover_ms),
        "--padding",
        str(args.padding),
    ]
    if args.no_ocr:
        sdk_args.append("--no-ocr")
    write_or_print(run_sdk(repo, sdk_args, timeout=45), args.output)
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["observe", args.identifier]
    sdk_args.extend(["--view", args.view])
    if args.visible_only:
        sdk_args.append("--visible-only")
    if args.no_activate:
        sdk_args.append("--no-activate")
    write_or_print(run_sdk(repo, sdk_args, timeout=35), args.output)
    return 0


def cmd_uia(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["uia", args.action, str(args.hwnd), args.uia_path]
    if args.value is not None:
        sdk_args.append(args.value)
    write_or_print(run_sdk(repo, sdk_args, timeout=15), args.output)
    return 0


def cmd_input(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["input"]
    hwnd = positive_int_or_none(args.hwnd)
    if hwnd is not None:
        sdk_args.extend(["--hwnd", str(hwnd)])
    action_args = args.action_args[1:] if args.action_args[:1] == ["--"] else args.action_args
    sdk_args.extend([args.action, *action_args])
    write_or_print(run_sdk(repo, sdk_args, timeout=max(10, len(" ".join(action_args)) * 0.2)), args.output)
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    sdk_args = ["ocr"]
    if args.image:
        sdk_args.extend(["--image", os.fspath(args.image)])
    hwnd = positive_int_or_none(args.hwnd)
    pid = positive_int_or_none(args.pid)
    if hwnd is not None:
        sdk_args.extend(["--hwnd", str(hwnd)])
    if pid is not None:
        sdk_args.extend(["--pid", str(pid)])
    if args.identifier:
        sdk_args.extend(["--identifier", args.identifier])
    if args.target:
        sdk_args.extend(["--target", args.target])
    if args.rect:
        sdk_args.extend(["--rect", args.rect])
    if args.uia_path:
        sdk_args.extend(["--uia-path", args.uia_path])
    write_or_print(run_sdk(repo, sdk_args, timeout=60), args.output)
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    repo = repo_path(args.repo)
    script = repo / "workflows" / "llm_app_workflow.py"
    workflow_args = args.workflow_args[1:] if args.workflow_args[:1] == ["--"] else args.workflow_args
    if arg_list_has_option(workflow_args, "--execute") and not arg_list_has_option(workflow_args, "--plan-output"):
        workflow_args = [
            *workflow_args,
            "--plan-output",
            os.fspath(default_artifact_path("workflow-run", ".json", cwd=Path.cwd())),
        ]
    cmd = [sys.executable, os.fspath(script), args.instruction, *workflow_args]
    return subprocess.run(cmd, cwd=repo).returncode


def cmd_artifact_dir(args: argparse.Namespace) -> int:
    print(session_artifact_dir(cwd=Path.cwd()))
    return 0


def cmd_plan_log(args: argparse.Namespace) -> int:
    data = json.loads(args.path.read_text(encoding="utf-8"))
    trace_summary = tactile_trace.trace_summary(data.get("trace"))
    steps = []
    for step in data.get("steps", []):
        steps.append(
            {
                "step": step.get("step"),
                "summary": (step.get("plan") or {}).get("summary"),
                "actions": (step.get("plan") or {}).get("actions"),
                "execution": [
                    {
                        "ok": item.get("ok"),
                        "mode": item.get("mode"),
                        "action": item.get("action"),
                    }
                    for item in (step.get("execution_results") or [])
                ],
            }
        )
    write_or_print(
        {
            "final_status": data.get("final_status"),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_apps = subparsers.add_parser("list-apps", help="List apps and running windows.")
    add_global(list_apps)
    list_apps.add_argument("--query")
    list_apps.add_argument("--limit", type=int, default=100)
    list_apps.set_defaults(func=cmd_list_apps)

    open_parser = subparsers.add_parser("open", help="Open or activate an app.")
    add_global(open_parser)
    open_parser.add_argument("identifier")
    open_parser.add_argument("--no-activate", action="store_true")
    open_parser.set_defaults(func=cmd_open)

    traverse = subparsers.add_parser("traverse", help="Traverse a running window.")
    add_global(traverse)
    traverse.add_argument("--hwnd", type=int)
    traverse.add_argument("--pid", type=int)
    traverse.add_argument("--target")
    traverse.add_argument("--visible-only", action="store_true", default=True)
    traverse.add_argument("--all", dest="visible_only", action="store_false")
    traverse.add_argument("--no-activate", action="store_true", default=True)
    traverse.add_argument("--activate", dest="no_activate", action="store_false")
    traverse.add_argument("--view", choices=["control", "raw", "content"], default="control")
    traverse.set_defaults(func=cmd_traverse)

    elements = subparsers.add_parser("elements", help="List concise UIA actionable/interactable elements.")
    add_global(elements)
    elements.add_argument("--hwnd", type=int)
    elements.add_argument("--pid", type=int)
    elements.add_argument("--target")
    elements.add_argument("--visible-only", action="store_true", default=True)
    elements.add_argument("--all", dest="visible_only", action="store_false")
    elements.add_argument("--no-activate", action="store_true", default=True)
    elements.add_argument("--activate", dest="no_activate", action="store_false")
    elements.add_argument("--limit", type=int, default=160)
    elements.add_argument("--query")
    elements.add_argument("--view", choices=["control", "raw", "content"], default="control")
    elements.set_defaults(func=cmd_elements)

    probe = subparsers.add_parser("probe", help="Hover a screen point, inspect UIA FromPoint, and optionally OCR the local region.")
    add_global(probe)
    probe.add_argument("--hwnd", type=int)
    probe.add_argument("--pid", type=int)
    probe.add_argument("--target")
    probe.add_argument("--x", type=float, required=True)
    probe.add_argument("--y", type=float, required=True)
    probe.add_argument("--view", choices=["control", "raw", "content"], default="control")
    probe.add_argument("--hover-ms", type=int, default=350)
    probe.add_argument("--padding", type=int, default=16)
    probe.add_argument("--no-ocr", action="store_true")
    probe.set_defaults(func=cmd_probe)

    observe = subparsers.add_parser("observe", help="Open/activate and traverse an app.")
    add_global(observe)
    observe.add_argument("identifier")
    observe.add_argument("--visible-only", action="store_true", default=True)
    observe.add_argument("--all", dest="visible_only", action="store_false")
    observe.add_argument("--no-activate", action="store_true", default=True)
    observe.add_argument("--activate", dest="no_activate", action="store_false")
    observe.add_argument("--view", choices=["control", "raw", "content"], default="control")
    observe.set_defaults(func=cmd_observe)

    uia = subparsers.add_parser("uia", help="Operate a UIA element by uia_path.")
    add_global(uia)
    uia.add_argument("action", choices=["activate", "click", "press", "focus", "select", "set_value", "uiaactivate", "uiaclick", "uiapress", "uiafocus", "uiaselect", "uiasetvalue"])
    uia.add_argument("hwnd", type=int)
    uia.add_argument("uia_path")
    uia.add_argument("value", nargs="?")
    uia.set_defaults(func=cmd_uia)

    input_parser = subparsers.add_parser("input", help="Send keyboard, mouse, scroll, or text input.")
    add_global(input_parser)
    input_parser.add_argument("--hwnd", type=int)
    input_parser.add_argument("action", choices=["keypress", "click", "doubleclick", "rightclick", "mousemove", "scroll", "writetext", "streamtext", "typetext", "pastetext", "clipboardtext"])
    input_parser.add_argument("action_args", nargs=argparse.REMAINDER)
    input_parser.set_defaults(func=cmd_input)

    ocr = subparsers.add_parser("ocr", help="Run Windows OCR on an image or target window.")
    add_global(ocr)
    ocr.add_argument("--image", type=Path)
    ocr.add_argument("--hwnd", type=int)
    ocr.add_argument("--pid", type=int)
    ocr.add_argument("--identifier")
    ocr.add_argument("--target")
    ocr.add_argument("--rect", help="Absolute screen rect as x,y,width,height.")
    ocr.add_argument("--uia-path", help="Capture and OCR the bounding rect of this UIA element.")
    ocr.set_defaults(func=cmd_ocr)

    workflow = subparsers.add_parser("workflow", help="Run the end-to-end LLM observe-plan-act workflow.")
    add_global(workflow)
    workflow.add_argument("instruction")
    workflow.add_argument("workflow_args", nargs=argparse.REMAINDER)
    workflow.set_defaults(func=cmd_workflow)

    artifact_dir = subparsers.add_parser("artifact-dir", help="Print the session-scoped Windows workflow artifact directory.")
    artifact_dir.set_defaults(func=cmd_artifact_dir)

    plan_log = subparsers.add_parser("plan-log", help="Summarize a workflow plan-output JSON file.")
    add_global(plan_log)
    plan_log.add_argument("path", type=Path)
    plan_log.set_defaults(func=cmd_plan_log)

    trace_replay = subparsers.add_parser("trace-replay", help="Aggregate metrics from trace fixtures, run logs, or JSONL traces.")
    add_global(trace_replay)
    trace_replay.add_argument("paths", nargs="+", type=Path)
    trace_replay.set_defaults(func=cmd_trace_replay)

    wechat_send_message = subparsers.add_parser(
        "wechat-send-message",
        help="Fast WeChat path: search a contact, paste a message, optionally OCR-check, and send.",
    )
    add_global(wechat_send_message)
    wechat_send_message.add_argument("--target", default="\u5fae\u4fe1", help="WeChat app target. Defaults to 微信.")
    wechat_send_message.add_argument("--hwnd", type=int, help="Existing WeChat window handle.")
    wechat_send_message.add_argument("--chat", required=True, help="Contact/group search text.")
    wechat_send_message.add_argument("--message", required=True, help="Message text to paste.")
    wechat_send_message.add_argument("--wait-ms", type=int, default=350, help="Delay after UI actions before OCR verification.")
    wechat_send_message.add_argument("--no-title-ocr", action="store_true", help="Skip targeted title OCR after opening the chat.")
    wechat_send_message.add_argument("--no-draft-ocr", action="store_true", help="Skip targeted draft OCR before sending.")
    wechat_send_message.add_argument("--require-title-match", action="store_true", help="Fail if title OCR does not contain --chat.")
    wechat_send_message.add_argument("--require-draft-match", action="store_true", help="Fail if draft OCR does not contain --message.")
    wechat_send_message.add_argument("--send-method", choices=["enter", "button"], default="enter", help="How to submit the WeChat draft. Defaults to focusing compose and pressing Enter.")
    wechat_send_message.add_argument("--draft-only", action="store_true", help="Type the message but do not click Send.")
    wechat_send_message.add_argument("--dry-run", action="store_true", help="Resolve WeChat and compute regions without typing or sending.")
    wechat_send_message.add_argument("--sdk-input", action="store_true", help="Use WindowsUseSDK input actions instead of direct Win32 input.")
    wechat_send_message.add_argument("--no-restore-clipboard", action="store_true", help="Leave pasted text in the clipboard after direct input.")
    wechat_send_message.add_argument("--keep-existing-draft", action="store_true", help="Append/replace at the current caret instead of selecting any existing compose draft first.")
    wechat_send_message.set_defaults(func=cmd_wechat_send_message)

    feishu_open_chat = subparsers.add_parser(
        "feishu-open-chat",
        help="Open a Feishu/Lark chat using Ctrl+K and the first-result virtual region.",
    )
    add_global(feishu_open_chat)
    feishu_open_chat.add_argument("--target", default="\u98de\u4e66", help="Feishu/Lark app target. Defaults to 飞书.")
    feishu_open_chat.add_argument("--hwnd", type=int, help="Existing Feishu/Lark window handle.")
    feishu_open_chat.add_argument("--chat", required=True, help="Chat/contact/group search text.")
    feishu_open_chat.add_argument("--wait-ms", type=int, default=450, help="Delay after UI actions before targeted inspection.")
    feishu_open_chat.add_argument("--no-verify-result", action="store_true", help="Skip targeted OCR of the first result row.")
    feishu_open_chat.add_argument("--allow-first-result", action="store_true", help="Open the first result region when OCR does not confirm the chat text.")
    feishu_open_chat.add_argument("--dry-run", action="store_true", help="Search and inspect but do not click the result.")
    feishu_open_chat.set_defaults(func=cmd_feishu_open_chat)

    feishu_send_message = subparsers.add_parser(
        "feishu-send-message",
        help="Fast Feishu/Lark path: optional org switch, open chat, focus compose, stream text, and send.",
    )
    add_global(feishu_send_message)
    feishu_send_message.add_argument("--target", default="\u98de\u4e66", help="Feishu/Lark app target. Defaults to 飞书.")
    feishu_send_message.add_argument("--hwnd", type=int, help="Existing Feishu/Lark window handle.")
    feishu_send_message.add_argument("--org-name", help="Optional organization/account text to switch to first, for example 个人用户.")
    feishu_send_message.add_argument("--chat", required=True, help="Chat/contact/group search text.")
    feishu_send_message.add_argument("--message", required=True, help="Message text to type.")
    feishu_send_message.add_argument("--wait-ms", type=int, default=450, help="Delay after UI actions before targeted inspection.")
    feishu_send_message.add_argument("--no-verify-result", action="store_true", help="Skip targeted OCR of the first result row.")
    feishu_send_message.add_argument("--allow-first-result", action="store_true", help="Open the first result region when OCR does not confirm the chat text.")
    feishu_send_message.add_argument("--draft-only", action="store_true", help="Type the message but do not press Enter.")
    feishu_send_message.add_argument("--dry-run", action="store_true", help="Run organization/chat lookup but do not type or send.")
    feishu_send_message.set_defaults(func=cmd_feishu_send_message)

    feishu_fill_daily_report = subparsers.add_parser(
        "feishu-fill-daily-report",
        help="Open a visible Feishu report reminder card if needed, then fill the daily report form and date-time picker fields.",
    )
    add_global(feishu_fill_daily_report)
    feishu_fill_daily_report.add_argument("--target", default="\u98de\u4e66", help="Feishu/Lark app target. Defaults to 飞书.")
    feishu_fill_daily_report.add_argument("--hwnd", type=int, help="Existing Feishu/Lark window handle.")
    feishu_fill_daily_report.add_argument("--org-name", help="Optional organization/account text to switch to first, for example 示例组织.")
    feishu_fill_daily_report.add_argument("--summary", help="今日总结 text to paste into the report editor.")
    feishu_fill_daily_report.add_argument("--start-time", required=True, help="Work start time, e.g. 10:00 or 上午十点.")
    feishu_fill_daily_report.add_argument("--end-time", required=True, help="Work end time, e.g. 20:00 or 晚上八点.")
    feishu_fill_daily_report.add_argument("--date", help="Report date as YYYY-MM-DD. Defaults to today.")
    feishu_fill_daily_report.add_argument("--help-text", help="Optional 需要协调与帮助 text.")
    feishu_fill_daily_report.add_argument("--no-submit", action="store_true", help="Fill and autosave the report, but do not click the final 提交 button.")
    feishu_fill_daily_report.add_argument("--open-form-only", action="store_true", help="Open the daily report form from any Feishu page, then stop without editing.")
    feishu_fill_daily_report.add_argument("--wait-ms", type=int, default=650, help="Delay after UI actions before OCR verification.")
    feishu_fill_daily_report.add_argument("--dry-run", action="store_true", help="Inspect the current form and planned values without editing.")
    feishu_fill_daily_report.set_defaults(func=cmd_feishu_fill_daily_report)

    feishu_switch_org = subparsers.add_parser(
        "feishu-switch-org",
        help="Switch Feishu/Lark by probing the bottom organization dock and verifying the profile card with OCR.",
    )
    add_global(feishu_switch_org)
    feishu_switch_org.add_argument("--target", default="飞书", help="Feishu/Lark app target. Defaults to 飞书.")
    feishu_switch_org.add_argument("--hwnd", type=int, help="Existing Feishu/Lark window handle.")
    feishu_switch_org.add_argument("--name", required=True, help="Organization/account text to verify, for example 个人用户.")
    feishu_switch_org.add_argument("--max-icons", type=int, default=4, help="Maximum visible bottom dock icons to probe.")
    feishu_switch_org.add_argument("--wait-ms", type=int, default=650, help="Delay after each UI action before OCR verification.")
    feishu_switch_org.add_argument("--dry-run", action="store_true", help="Only detect bottom dock candidates; do not click them.")
    feishu_switch_org.set_defaults(func=cmd_feishu_switch_org)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
