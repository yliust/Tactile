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
`summary_mode=compact`, returning a prioritized subset of AX elements plus local
macOS Vision OCR lines as coordinate-backed `OCRLine` elements with indexes like
`o0`. Full, untruncated state is still written to `/tmp/tactile-macos-mcp`.
When the target app is already running, `get_app_state` reuses the existing
process without re-activating it, which avoids pulling background popovers or
transient windows behind the app's main window during read-only observation.
Pass `summary_mode=full` for the old full element listing,
`summary_mode=metadata` for paths and window metadata only, `element_filter` to
return only matching elements using a case-insensitive regular expression, or
`element_limit` to adjust the compact listing.

`element_filter` narrows only the `get_app_state` summary returned to the model;
it does not type into, focus, search inside, or otherwise change the target app.
It matches each element's index, source, role, visible text, AX path, state flags,
and secondary action names. Use plain text for one target, for example
`张仲岳`, and regex alternation with `|` for multiple targets, for example
`search|搜索|输入|联系人|张仲岳`. Escape regex metacharacters when they should be
literal. If a focused query returns too little, increase `element_limit`, use
`summary_mode=full`, or inspect the `full_element_dump` path.

Pass `observation_mode=ax` for AX-only speed/privacy, or
`observation_mode=ax_ocr_visual` to also attach the screenshot image to the MCP
tool result. For AX elements, use `perform_secondary_action`. `click` is
coordinate-only and should be used for OCR lines or other visual/coordinate-backed
targets, with OCR lines preferred over raw screenshot pixel coordinates from
the attached image when both are available. Element output labels Accessibility
coordinates as `screenFrame`/`screenCenter` and screenshot pixels as
`screenshotFrame`/`screenshotCenter`. Raw `click` `x`/`y` defaults to
screenshot pixel coordinates, but `click` also accepts `coordinate_space=screen`
or `screen_x`/`screen_y` for macOS screen points. Raw `scroll` and `drag`
coordinates remain screenshot pixel coordinates.

## Build

```bash
xcrun swift build -c release \
  -Xswiftc -swift-version -Xswiftc 5 \
  -Xswiftc -use-ld="$(pwd)/scripts/ld-wrapper/ld"
```

`swift-sdk` requires Swift 6.1 or newer. On machines where Xcode's default
toolchain is older, use an active Swift toolchain that satisfies that
requirement. The extra Swift flags keep the current `swift-sdk` compiling under
Swift 6.2 and route links through `scripts/ld-wrapper`, which filters a Swift
6.2 linker flag that older Xcode 14 linkers do not understand. Use an absolute
wrapper path because clang rejects a relative `-fuse-ld` value.

## Run

```bash
bin/tactile-macos-mcp
```

Tool calls write full state, traversal, and screenshots to:

```text
/tmp/tactile-macos-mcp
```

The MCP response returns a compact text summary with those file paths.

## Test

```bash
python3 scripts/test_mcp.py --test tools
```
