#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINT="$REPO_ROOT/scripts/fleet-lint.py"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

echo "test 1: synthetic topology passes declaration-level checks"
out="$(python3 "$LINT" --topology-only --topology "$REPO_ROOT/tests/fixtures/topology.json")" || { echo "FAIL: synthetic topology should lint clean"; echo "$out"; exit 1; }
echo "$out" | grep -q "exactly one session-extraction writer" \
  || { echo "FAIL: expected sole-writer confirmation"; echo "$out"; exit 1; }
echo "OK"

echo "test 2: two sole-writer nodes are rejected"
python3 - "$REPO_ROOT/tests/fixtures/topology.json" "$TMPDIR_T/two-writers.json" <<'PYEOF'
import json, sys
topo = json.load(open(sys.argv[1]))
for node in topo["nodes"].values():
    node["session_extraction"] = "sole-writer"
json.dump(topo, open(sys.argv[2], "w"))
PYEOF
if python3 "$LINT" --topology-only --topology "$TMPDIR_T/two-writers.json" >/dev/null 2>&1; then
  echo "FAIL: duplicate sole-writers must fail the lint"
  exit 1
fi
echo "OK"

echo "test 3: receives_backup naming an unknown node is rejected"
python3 - "$REPO_ROOT/tests/fixtures/topology.json" "$TMPDIR_T/unknown-node.json" <<'PYEOF'
import json, sys
topo = json.load(open(sys.argv[1]))
first = next(iter(topo["nodes"].values()))
first["receives_backup"].append("no-such-node")
json.dump(topo, open(sys.argv[2], "w"))
PYEOF
if python3 "$LINT" --topology-only --topology "$TMPDIR_T/unknown-node.json" >/dev/null 2>&1; then
  echo "FAIL: unknown receives_backup entry must fail the lint"
  exit 1
fi
echo "OK"

echo "test 4: a declared retired backup directory remains valid"
python3 - "$REPO_ROOT/tests/fixtures/topology.json" "$TMPDIR_T/retired-backup.json" <<'PYEOF'
import json, sys
topo = json.load(open(sys.argv[1]))
assert "retired-node" not in topo["nodes"]
assert "retired-node" in topo["retired_backup_directories"]
assert any("retired-node" in node["receives_backup"] for node in topo["nodes"].values())
json.dump(topo, open(sys.argv[2], "w"))
PYEOF
python3 "$LINT" --topology-only --topology "$TMPDIR_T/retired-backup.json" >/dev/null \
  || { echo "FAIL: declared retired backup directory should remain valid"; exit 1; }
echo "OK"

echo "All fleet-lint tests passed."
