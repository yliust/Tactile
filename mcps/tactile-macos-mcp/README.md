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
Pass `summary_mode=full` for the old full element listing,
`summary_mode=metadata` for paths and window metadata only, `element_filter` to
return only matching elements, or `element_limit` to adjust the compact listing.
Pass `observation_mode=ax` for AX-only speed/privacy, or
`observation_mode=ax_ocr_visual` to also attach the screenshot image to the MCP
tool result. The intended targeting priority is AX elements first, then OCR
lines, then raw screenshot pixel coordinates from the attached image. Element
output labels Accessibility coordinates as `screenFrame`/`screenCenter` and
screenshot pixels as `screenshotFrame`/`screenshotCenter`. Raw `click` `x`/`y`
defaults to screenshot pixel coordinates, but `click` also accepts
`coordinate_space=screen` or `screen_x`/`screen_y` for macOS screen points. Raw
`scroll` and `drag` coordinates remain screenshot pixel coordinates.

`scroll` uses controlled wheel events for both element-index and coordinate
targets. `pages=1` is a gentle calibrated scroll step, and fractional `pages`
values can be used for smaller adjustments. Native AX scroll actions remain
available through `perform_secondary_action` when a full accessibility scroll is
desired.

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
