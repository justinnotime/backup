#!/usr/bin/env python3
"""Assert every required resident Agent Bus process runs current code.

Resident Agent Bus processes keep the sealed source snapshot they execute as
an open memfd and compare its SHA-256 with the source between delivery cycles.
This script independently hashes that same immutable file descriptor:

For every watch-mode seat registered on THIS host it asserts, per seat:
  1. exactly one live watcher process matches the FULL pattern
     `agent-bus-v3.py watch <agent-id>` (never the bare `watch <agent-id>` —
     restart wrappers shadow it);
  2. that process loads the canonical script path;
  3. its sealed snapshot identity equals the canonical script's current
     content identity (timestamps never participate).
For Matrix transport, the pull-notify dispatcher process
(`agent-bus-v3.py dispatch`) gets the same three assertions. Local transport
writes directly to the target inbox and therefore has no dispatcher process.

Output: one PASS/FAIL/SKIP/NOTE line per seat, then a summary. Exit 0 only if
every asserted process passes; exit 1 lists the failing handles by name.
Passing here is NOT liveness — liveness additionally needs a round trip
(references/agent-bus.md → Failure semantics). 0 LLM calls.

Usage:
  python3 scripts/agent-bus-watcher-freshness.py
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
import runtime_config as cfg  # noqa: E402

BUS = (SCRIPT_DIR / "agent-bus-v3.py").resolve()
SOURCE_MEMFD_NAME = "memfd:agent-bus-v3-source"
SNAPSHOT_LOADER = (
    "import os,sys\n"
    "fd=int(sys.argv[1]);source=sys.argv[2];sys.argv=sys.argv[2:]\n"
    "size=os.fstat(fd).st_size;data=os.pread(fd,size,0)\n"
    "if len(data)!=size: raise RuntimeError('short Agent Bus source snapshot')\n"
    "exec(compile(data,source,'exec'),"
    "{'__name__':'__main__','__file__':source,'__package__':None,"
    "'__cached__':None,'_AGENT_BUS_SOURCE_FD':fd})"
)
# Linux UAPI constants are stable even when a Python build omits their names.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
SOURCE_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008



def cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a for a in raw.decode(errors="replace").split("\0") if a]


def process_environment(pid: int) -> dict[str, str]:
    """Read only the process environment needed to identify its fleet."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key in {
            b"NW_FLEET", b"AGENT_BUS_CFG", b"MATRIX_BUS_CFG"
        }:
            result[key.decode()] = value.decode(errors="replace")
    return result


def same_fleet_process(pid: int) -> bool:
    """Keep a generic dispatcher match inside the current fleet boundary."""
    candidate = process_environment(pid)
    current_name = os.environ.get("NW_FLEET", "").strip()
    candidate_name = candidate.get("NW_FLEET", "").strip()
    if current_name:
        current_cfg = os.environ.get("AGENT_BUS_CFG", os.environ.get("MATRIX_BUS_CFG"))
        candidate_cfg = candidate.get("AGENT_BUS_CFG", candidate.get("MATRIX_BUS_CFG"))
        return (
            candidate_name == current_name
            and candidate_cfg == current_cfg
        )
    return not candidate_name


def desired_source_identity() -> str:
    result = subprocess.run(
        [sys.executable, str(BUS), "source-identity"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    identity = result.stdout.strip()
    if result.returncode != 0 or not identity.startswith("sha256:") \
            or len(identity) != len("sha256:") + 64:
        raise RuntimeError(
            f"cannot compute canonical Agent Bus source identity: "
            f"{result.stderr.strip() or identity!r}"
        )
    return identity


def resident_contract(argv: list[str]) -> tuple[int, list[str]] | None:
    """Return the loader fd and public command for the one resident shape."""
    if len(argv) < 8 or argv[1:4] != ["-I", "-S", "-c"] \
            or argv[4] != SNAPSHOT_LOADER:
        return None
    try:
        source_fd = int(argv[5])
    except ValueError:
        return None
    if source_fd < 0:
        return None
    try:
        source_path = Path(argv[6]).resolve()
    except OSError:
        return None
    if source_path != BUS:
        return None
    return source_fd, argv[7:]


def executable_contract_error(pid: int, argv: list[str]) -> str:
    """Require the declared Python interpreter to be the running executable."""
    if not argv:
        return "empty process command line"
    try:
        actual = Path(f"/proc/{pid}/exe").resolve(strict=True)
        declared = Path(argv[0]).resolve(strict=True)
    except OSError as exc:
        return f"cannot inspect resident executable: {exc}"
    if actual != declared or not actual.name.startswith("python"):
        return f"resident executable {actual} does not match Python argv {declared}"
    return ""


def loaded_source_identity(pid: int, argv: list[str]) -> tuple[str | None, str]:
    """Hash the sealed fd named by the exact resident loader command."""
    contract = resident_contract(argv)
    if contract is None:
        return None, "process does not run the exact Agent Bus snapshot loader"
    source_fd, _public = contract
    entry = Path(f"/proc/{pid}/fd/{source_fd}")
    proof_fd = None
    try:
        target = os.readlink(entry)
        target_name = target.removeprefix("/").removesuffix(" (deleted)")
        if target_name != SOURCE_MEMFD_NAME:
            return None, f"loader fd {source_fd} is not the Agent Bus source snapshot"
        proof_fd = os.open(entry, os.O_RDONLY | os.O_CLOEXEC)
        seals = fcntl.fcntl(proof_fd, F_GET_SEALS)
        if seals & SOURCE_SEALS != SOURCE_SEALS:
            return None, "source snapshot is not fully sealed"
        digest = hashlib.sha256()
        while True:
            chunk = os.read(proof_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return "sha256:" + digest.hexdigest(), ""
    except OSError as exc:
        return None, f"cannot read source snapshot: {exc}"
    finally:
        if proof_fd is not None:
            os.close(proof_fd)


def matches_resident_command(argv: list[str], tail: list[str]) -> bool:
    contract = resident_contract(argv)
    if contract is None:
        return False
    _source_fd, public = contract
    if tail == ["dispatch"]:
        return bool(public) and public[0] == "dispatch" and "--once" not in public
    return public == tail


def matching_pids(tail: list[str]) -> list[int]:
    """Pids whose canonical script argv is the requested resident command."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        argv = cmdline(pid)
        if matches_resident_command(argv, tail) \
                and not executable_contract_error(pid, argv) \
                and same_fleet_process(pid):
            pids.append(pid)
    return pids


EXCEPTIONS_FILE = cfg.path("bus.watcher_exceptions")


def load_exceptions() -> dict[str, dict]:
    """agent_id -> exception entry. Operator-accepted no-watcher seats are
    rendered as a labeled ACCEPTED, never a FAIL and never silently."""
    if EXCEPTIONS_FILE is None:
        return {}
    try:
        data = json.loads(EXCEPTIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {e["agent_id"]: e for e in data.get("exceptions", [])
            if e.get("agent_id")}


def classify_reader(agent_id: str, exceptions: dict[str, dict],
                    matching_fn=None) -> tuple[str, int | None]:
    """Recognize a standard watcher or an explicit no-watcher exception.

    Ad-hoc ``unread`` loops are deliberately not inferred from process text:
    they neither expose an immutable loaded program nor prove repeated polls.
    """
    pids = (matching_fn or matching_pids)(["watch", agent_id])
    if pids:
        return "standard", pids[0]
    if agent_id in exceptions:
        return "accepted", None
    return "missing", None


def assert_process(label: str, tail: list[str], desired_identity: str,
                   fails: list[str]) -> None:
    pids = matching_pids(tail)
    if not pids:
        print(f"FAIL {label} — no process matches full pattern 'agent-bus-v3.py {' '.join(tail)}'")
        fails.append(label)
        return
    if len(pids) > 1:
        print(f"FAIL {label} — {len(pids)} processes match ({pids}); duplicates must be resolved first")
        fails.append(label)
        return
    pid = pids[0]
    argv = cmdline(pid)
    contract_error = executable_contract_error(pid, argv)
    if resident_contract(argv) is None or contract_error:
        detail = contract_error or "process does not run the exact snapshot loader"
        print(f"FAIL {label} — pid {pid} has no valid resident loader: {detail}")
        fails.append(label)
        return
    recorded, proof_error = loaded_source_identity(pid, argv)
    if recorded is None:
        print(f"FAIL {label} — pid {pid} has no verifiable loaded source:"
              f" {proof_error}")
        fails.append(label)
        return
    if recorded != desired_identity:
        print(f"FAIL {label} — pid {pid} loaded {recorded}, current source is {desired_identity}")
        fails.append(label)
        return
    print(f"PASS {label} — pid {pid} loaded current source {recorded}")


def main() -> int:
    try:
        desired_identity = desired_source_identity()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"FATAL: {exc}")
        return 2
    host = socket.gethostname()
    result = subprocess.run([sys.executable, str(BUS), "members"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FATAL: cannot read registry members: {result.stderr.strip()}")
        return 2
    members = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    exceptions = load_exceptions()
    local_transport = os.environ.get("AGENT_BUS_TRANSPORT", str(cfg.get("bus.transport", "local"))) == "local"
    fails: list[str] = []
    checked = 0
    print(f"# code: {BUS} identity {desired_identity}; host {host}; "
          f"{len(members)} registered seats")
    for m in sorted(members, key=lambda m: str(m.get("handle", ""))):
        handle = str(m.get("handle", "?"))
        if m.get("mode") != "watch":
            if local_transport:
                print(f"NOTE {handle} — pull seat: local delivery writes directly to its inbox")
            else:
                print(f"NOTE {handle} — pull seat: no watcher of its own; rides on the dispatcher line below")
            continue
        if m.get("host") != host:
            print(f"SKIP {handle} — registered on host {m.get('host')!r}; run this script there")
            continue
        checked += 1
        kind, pid = classify_reader(str(m.get("agent_id")), exceptions)
        if kind == "accepted":
            entry = exceptions[str(m.get("agent_id"))]
            print(f"ACCEPTED {handle} — operator-accepted no-watcher"
                  f" ({entry.get('accepted', '?')}): {entry.get('ruling', '')[:90]}")
            continue
        assert_process(handle, ["watch", str(m.get("agent_id"))], desired_identity, fails)
    if local_transport:
        print("NOTE dispatcher — not required for local transport")
    else:
        assert_process("dispatcher (systemd agent-bus-dispatcher.service; pull seats depend on it)",
                       ["dispatch"], desired_identity, fails)
        checked += 1
    if fails:
        print(f"\nRESULT: FAIL — {len(fails)}/{checked} on old/missing code: {', '.join(fails)}")
        return 1
    print(f"\nRESULT: PASS — all {checked} asserted processes run current code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
