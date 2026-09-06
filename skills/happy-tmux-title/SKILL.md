---
name: happy-tmux-title
description: Set a Happy chat title to match this session's tmux window number and topic. Use when the user asks to rename a Happy conversation, match its title to the terminal tab, or keep that title current as the main topic changes. Requires a Happy title-change integration and a known tmux window.
---

# Happy tmux title

Set the Happy conversation title to `[N] <short topic>`, using this session's
tmux window number and a short description of its main work in the user's
language. Preserve an explicit topic or number supplied by the user.

1. Run this installed package's `scripts/window-index`. It resolves the exact
   `TMUX_PANE` on the socket recorded in `TMUX`, preferring the originating session.
2. Choose a concise topic, usually no more than about six words.
3. Call the Happy integration's title-change tool, commonly
   `mcp__happy__change_title`, with the complete title. Discover the tool through
   the current harness if it is not already available.

```bash
/path/to/happy-tmux-title/scripts/window-index
```

The lookup requires Python 3.10 or newer and tmux. It is read-only: it does not
rename a tmux window, switch tabs, send keys, or change the Happy title itself.
The package uses only the Python standard library at runtime.

Do not infer this session's number from the foreground tmux window. A pane can
also be linked into multiple sessions under different window numbers. The
helper rejects missing or ambiguous context instead of choosing another tab.
If the originating session has disappeared, it accepts the remaining pane
links only when they all agree on the window number.
If lookup fails and the user has not supplied a number, explain the missing
context and request the number. If the Happy title tool is unavailable, report
that limitation; do not claim the title changed or edit local history files.

Recompute the number after tmux renumbering or when refreshing the title for a
new topic. Confirm the Happy tool succeeded before reporting the new title.

Development checks:

```bash
uv sync --project /path/to/happy-tmux-title --locked
(cd /path/to/happy-tmux-title && uv run --no-sync pytest tests -q)
```

The tests create an isolated tmux server. They do not use an existing server
or require a Happy account. See the [tmux manual](https://man.openbsd.org/tmux.1)
for the socket, pane identifiers, and `list-panes` formats.
