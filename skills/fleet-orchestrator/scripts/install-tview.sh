#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 -B "$HERE/install" --command tview \
  --target "${TVIEW_TARGET:-$HOME/.local/bin/tview}" --replace "$@"
