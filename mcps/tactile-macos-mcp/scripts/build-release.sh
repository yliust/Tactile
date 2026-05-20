#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/.build"
BIN_DIR="$ROOT_DIR/bin"
LD_WRAPPER="$ROOT_DIR/scripts/ld-wrapper/ld"
SWIFT_BIN="${SWIFT_BIN:-swift}"

build_args=(
  build
  -c
  release
  -Xswiftc
  -swift-version
  -Xswiftc
  5
  -Xswiftc
  "-use-ld=$LD_WRAPPER"
)

(
  cd "$ROOT_DIR"
  "$SWIFT_BIN" "${build_args[@]}"
)

BUILD_OUTPUT_DIR="$(
  cd "$ROOT_DIR"
  "$SWIFT_BIN" build -c release --show-bin-path
)"

mkdir -p "$BIN_DIR"
cp "$BUILD_OUTPUT_DIR/tactile-macos-mcp" "$BIN_DIR/tactile-macos-mcp"
cp "$BUILD_OUTPUT_DIR/screenshot-helper" "$BIN_DIR/screenshot-helper"
chmod 755 "$BIN_DIR/tactile-macos-mcp" "$BIN_DIR/screenshot-helper"

rm -rf "$BUILD_DIR"
