---
name: remote-clipboard
description: Install or use the shared `clip` shell function to send stdin or files to the local clipboard from local, SSH, mosh, or tmux sessions. Use for remote-to-local clipboard transfer; do not use it for large-file transfer.
---

# Remote Clipboard

`scripts/clip.sh` defines the `clip` shell function. Source it into the current
shell before use; the repository-root `clip.sh` remains a compatibility link.

```bash
. scripts/clip.sh
printf '%s\n' 'text' | clip
clip path/to/file
```

In a local Wayland session the function uses `wl-copy`; otherwise it emits OSC
52 to the local terminal. tmux 3.3 or newer must allow passthrough with
`set -g allow-passthrough all`.

Installing a persistent link or editing shell or tmux configuration requires an
explicit request. For payloads around 100 KB or larger, use a file-transfer
tool instead of OSC 52.
