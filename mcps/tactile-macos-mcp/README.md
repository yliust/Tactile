# tactile-macos-mcp

Computer Use style MCP facade for the local tactile macOS runtime.

The server exposes nine generic tools over stdio:

- `list_apps`
- `get_app_state`
- `click`
- `perform_secondary_action`
- `set_value` (disabled, retained for compatibility)
- `scroll`
- `drag`
- `press_key`
- `type_text`

The public tool interface intentionally mirrors the native Computer Use MCP
shape. Internally, this server uses the local `MacosUseSDK` Swift package and
can choose AX, CGEvent, typed text, or clipboard fallback strategies without
changing MCP schemas.

`get_app_state` defaults to `observation_mode=ax_ocr` and
`summary_mode=tsv`, returning a structured one-row-per-element listing across AX
elements and local macOS Vision OCR lines, with OCR rows using indexes like
`o0`. Full, untruncated state is still written to `/tmp/tactile-macos-mcp`.
When a fresh app-use session starts, `get_app_state` foregrounds the target app
once and tries to raise its first visible window through macOS Accessibility so
the real app window, not just the menu bar process, becomes observable. If the
first AX traversal still looks degraded, for example only a menu bar and a few
floating controls are visible, the server performs one recovery raise and
re-runs traversal before returning state. Later observations in that same short
session reuse the existing process without re-activating it, which avoids
pulling background popovers or transient windows behind the app's main window
during read-only observation.
Pass `summary_mode=full` for the old full element listing,
`summary_mode=metadata` for paths and window metadata only, or `element_filter`
to return only matching elements using a case-insensitive regular expression.

`element_filter` narrows only the `get_app_state` summary returned to the model;
it does not type into, focus, search inside, or otherwise change the target app.
It matches each element's index, source, role, visible text, AX path, state flags,
and secondary action names. Use plain text for one target, for example
`张仲岳`, and regex alternation with `|` for multiple targets, for example
`search|搜索|输入|联系人|张仲岳`. Escape regex metacharacters when they should be
literal. If a focused query returns too little, increase `element_limit`, use
the default `summary_mode=tsv` without filtering, use `summary_mode=full`, or
inspect the `full_element_dump` path.

Pass `observation_mode=ax` for AX-only speed/privacy, or
`observation_mode=ax_ocr_visual` to also attach the screenshot image to the MCP
tool result. For AX elements, use `perform_secondary_action`. `click` is
coordinate-only and should be used for OCR lines or other visual/coordinate-backed
targets. All public coordinates use macOS screen points with a top-left origin:
AX elements expose `screenFrame`/`screenCenter`, OCR lines expose
`screenFrame`/`screenCenter`, and raw `click`, `scroll`, and `drag` inputs all
use screen-point coordinates. The attached screenshot in visual mode is for
reasoning only; its covered on-screen region is reported separately.
For chat-style UIs, prefer `observation_mode=ax_ocr_visual` when the task is
semantic rather than purely operational, for example identifying the latest
message, who sent which bubble, or whether a message was recalled. In those
cases, use the screenshot to understand timeline and bubble ownership, then use
AX/OCR output to target controls.

## Build

```bash
bash scripts/build-release.sh
```

`swift-sdk` requires Swift 6.1 or newer. On machines where Xcode's default
toolchain is older, use an active Swift toolchain that satisfies that
requirement. The extra Swift flags keep the current `swift-sdk` compiling under
Swift 6.2 and route links through `scripts/ld-wrapper`, which filters a Swift
6.2 linker flag that older Xcode 14 linkers do not understand. Use an absolute
wrapper path because clang rejects a relative `-fuse-ld` value.

The build script copies the real `tactile-macos-mcp` and `screenshot-helper`
executables into `bin/`, then removes `.build/` so the package tree does not
keep the full Swift build cache.

## Run

```bash
bin/tactile-macos-mcp
```

Tool calls write full state, traversal, and screenshots to:

```text
/tmp/tactile-macos-mcp
```

The MCP response returns a metadata header plus a tab-separated table with one
row per AX/OCR element and the same screen-point coordinate contract used by the
action tools.

## Test

```bash
python3 scripts/test_mcp.py --test tools
```
