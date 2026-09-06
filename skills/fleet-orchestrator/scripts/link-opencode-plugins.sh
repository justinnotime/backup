#!/usr/bin/env bash
# Install version-controlled OpenCode plugins into the global OpenCode config.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_DIR/plugins/opencode"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins"

mkdir -p "$DEST_DIR"

for plugin in "$SRC_DIR"/*.{ts,js}; do
  [ -f "$plugin" ] || continue
  dest="$DEST_DIR/$(basename "$plugin")"
  tmp="$dest.tmp.$$"
  cp "$plugin" "$tmp"
  chmod --reference="$plugin" "$tmp"
  mv -f "$tmp" "$dest"
  echo "installed $(basename "$plugin") from $plugin"
done
