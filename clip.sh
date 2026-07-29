# clip — send stdin or file contents to the LOCAL clipboard.
# In a local Wayland session it pipes to wl-copy; over ssh/mosh it emits
# OSC 52, which the terminal (e.g. kitty) writes to the local clipboard.
# Usage: cat f | clip   /   clip f   /   somecmd | clip
#
# Inside tmux the sequence is passthrough-wrapped; tmux 3.3+ needs
# `set -g allow-passthrough all` — plain "on" silently drops copies fired
# from panes that are not currently visible (backgrounded jobs, switched
# windows).
clip() {
    if [ -n "$WAYLAND_DISPLAY" ] && command -v wl-copy >/dev/null 2>&1; then
        if [ $# -gt 0 ]; then cat -- "$@" | wl-copy; else wl-copy; fi
        return
    fi

    local b64
    if [ $# -gt 0 ]; then
        b64=$(cat -- "$@" | base64 | tr -d '\n')
    else
        b64=$(base64 | tr -d '\n')
    fi

    # OSC 52 payloads are part of terminal state; under mosh an oversized
    # payload drags down state sync for the whole session.
    if [ ${#b64} -gt 100000 ]; then
        printf 'clip: %d KB is large and will lag under mosh; use rsync instead\n' \
            $((${#b64} / 1024)) >&2
    fi

    # mosh only forwards the clipboard to the local terminal when its state
    # CHANGES between frames; clear it first, one mosh frame earlier, so
    # re-copying identical content still lands locally.
    _clip_osc52 ''
    sleep 0.1
    _clip_osc52 "$b64"
}

_clip_osc52() {
    if [ -n "$TMUX" ]; then
        printf '\033Ptmux;\033\033]52;c;%s\a\033\\' "$1" > /dev/tty
    else
        printf '\033]52;c;%s\a' "$1" > /dev/tty
    fi
}
