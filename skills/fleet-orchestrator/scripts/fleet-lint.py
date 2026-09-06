#!/usr/bin/env python3
"""Check a configured backup and session-extraction topology."""


import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg

DEFAULT_TOPOLOGY = cfg.path("paths.topology")
BACKUP_CONFIG = cfg.path("backup.config", Path.home() / ".config" / "backup" / "config")
SYNCTHING_CONFIG_PATHS = cfg.get("backup.syncthing_configs", [
    "~/Library/Application Support/Syncthing/config.xml",
    "~/.local/state/syncthing/config.xml",
    "~/.config/syncthing/config.xml",
])
BACKUP_FOLDER_LABEL = cfg.get("backup.folder_label", "backup")

HOUSEKEEPING_IGNORE = re.compile(r"^\(\?d\)|^#|^$")

fails = 0
warns = 0


def ok(msg):
    print(f"OK   {msg}")


def warn(msg):
    global warns
    warns += 1
    print(f"WARN {msg}")


def fail(msg):
    global fails
    fails += 1
    print(f"FAIL {msg}")


def check_topology(topo):
    nodes = topo["nodes"]
    retired_backup_directories = set(topo.get("retired_backup_directories", []))
    known_backup_directories = set(nodes) | retired_backup_directories

    sole_writers = [k for k, n in nodes.items() if n["session_extraction"] == "sole-writer"]
    if len(sole_writers) == 1:
        ok(f"exactly one session-extraction writer: {sole_writers[0]}")
    else:
        fail(f"expected exactly one sole-writer node, found {len(sole_writers)}: {sole_writers}")

    for key, node in nodes.items():
        if key not in node["receives_backup"]:
            fail(f"{key}: receives_backup must include the node's own key")
        for received in node["receives_backup"]:
            if received not in known_backup_directories:
                fail(f"{key}: receives_backup names unknown directory '{received}'")

    replica_devices = topo.get("replica_only_devices", {})
    for key, node in nodes.items():
        if not node["writes_backup"]:
            continue
        receivers = [k for k, n in nodes.items() if k != key and key in n["receives_backup"]]
        receivers += [name for name, dev in replica_devices.items()
                      if "*" in dev.get("receives_backup", []) or key in dev.get("receives_backup", [])]
        if receivers:
            ok(f"{key}: backup replicated by {', '.join(receivers)}")
        else:
            warn(f"{key}: no other node or replica device receives this backup (no off-machine replica)")

    unverified = [k for k, n in nodes.items() if not n.get("verified")]
    if unverified:
        warn(f"entries not yet verified by their node agent: {', '.join(unverified)}")


def read_machine_id():
    if not os.path.exists(BACKUP_CONFIG):
        fail(f"no backup config at {BACKUP_CONFIG}; fleet nodes must set MACHINE_ID explicitly")
        return None
    with open(BACKUP_CONFIG) as f:
        for line in f:
            m = re.match(r'^MACHINE_ID="?([A-Za-z0-9._-]+)"?\s*$', line.strip())
            if m:
                return m.group(1)
    fail(f"MACHINE_ID is not set explicitly in {BACKUP_CONFIG} (hostname fallback is not allowed on fleet nodes)")
    return None


def find_syncthing_folder():
    for raw in SYNCTHING_CONFIG_PATHS:
        path = cfg.expand(raw)
        if os.path.exists(path):
            root = ET.parse(path).getroot()
            for folder in root.findall("folder"):
                if folder.get("label") == BACKUP_FOLDER_LABEL:
                    return path, folder
            fail(f"{path}: no Syncthing folder labeled '{BACKUP_FOLDER_LABEL}'")
            return path, None
    fail("no Syncthing config.xml found in any known location")
    return None, None


def read_ignore_selection(folder_path):
    stignore = os.path.join(cfg.expand(folder_path), ".stignore")
    if not os.path.exists(stignore):
        fail(f"{stignore} missing; a fleet node must select which backup directories it accepts")
        return None, False
    accepted = set()
    has_catchall = False
    for line in open(stignore):
        line = line.strip()
        if HOUSEKEEPING_IGNORE.match(line):
            continue
        if line.startswith("!"):
            accepted.add(line[1:].split("/")[0])
        elif line in ("*", "**"):
            has_catchall = True
    return accepted, has_catchall


def check_local(topo):
    nodes = topo["nodes"]

    machine_id = read_machine_id()
    if machine_id is None:
        return
    if machine_id not in nodes:
        fail(f"local MACHINE_ID '{machine_id}' is not a declared node key")
        return
    node = nodes[machine_id]
    ok(f"local node key: {machine_id}")

    config_path, folder = find_syncthing_folder()
    if folder is not None:
        declared_type = node["syncthing_folder_type"]
        actual_type = folder.get("type", "sendreceive")
        if actual_type == declared_type:
            ok(f"Syncthing backup folder type: {actual_type}")
        else:
            fail(f"Syncthing backup folder type is '{actual_type}', topology declares '{declared_type}' ({config_path})")

        accepted, has_catchall = read_ignore_selection(folder.get("path"))
        if accepted is not None:
            declared = set(node["receives_backup"])
            if not has_catchall:
                fail("backup .stignore has no final catch-all pattern; the node would accept every machine's backup")
            if accepted == declared:
                ok(f"backup .stignore accepts exactly: {', '.join(sorted(accepted))}")
            else:
                extra = accepted - declared
                missing = declared - accepted
                if extra:
                    fail(f"backup .stignore accepts undeclared directories: {', '.join(sorted(extra))}")
                if missing:
                    fail(f"backup .stignore does not accept declared directories: {', '.join(sorted(missing))}")

    try:
        crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        crontab = None
        warn("could not read crontab; cron checks skipped")
    if crontab is not None:
        cron_lines = [l for l in crontab.splitlines() if l.strip() and not l.strip().startswith("#")]
        if any("backup" in l for l in cron_lines):
            ok("backup cron entry present")
        else:
            warn("no backup cron entry found")
        if node["session_extraction"] != "self-legacy" and any("auto-extract" in l for l in cron_lines):
            fail("legacy auto-extract cron entry present; this node's extraction is "
                 f"'{node['session_extraction']}' and the entry must be removed "
                 "(the fleet has exactly one managed extraction writer)")
        else:
            ok("no forbidden legacy extraction cron entry")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topology", default=DEFAULT_TOPOLOGY)
    parser.add_argument("--topology-only", action="store_true")
    args = parser.parse_args()

    if args.topology is None:
        parser.error("--topology or paths.topology configuration is required")
    with open(args.topology) as f:
        topo = json.load(f)

    check_topology(topo)
    if not args.topology_only:
        check_local(topo)

    print(f"Summary: {fails} FAIL, {warns} WARN")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
