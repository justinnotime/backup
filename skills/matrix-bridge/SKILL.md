---
name: matrix-bridge
description: Send and receive text, images, or files through one privately configured Matrix room. Use for Matrix room transfers, including moving material between this machine and a phone or another Matrix client. Requires an existing account and an unencrypted room; does not configure a Matrix server or connect other chat networks.
---

# Matrix Bridge

Transfer content through a configured Matrix room. A phone running a Matrix
client is one possible endpoint; the commands work with other Matrix clients too.

Use the package's `mx-send` and `mx-recv` commands from any directory. Resolve
this installed Skill's directory to find them; neither command needs a sibling
Skill or another repository's source.

The private configuration chooses the server, room, account, credentials,
receive cursor, and download directory. Read [configuration](references/config.md)
when installing, relocating, or diagnosing the bridge. Missing configuration
is a setup problem; do not guess a room or reuse another channel.

```bash
/path/to/matrix-bridge/mx-recv --doctor
/path/to/matrix-bridge/mx-send --text "message requested by the user"
/path/to/matrix-bridge/mx-send --file /path/to/image.png /path/to/report.pdf
/path/to/matrix-bridge/mx-recv
/path/to/matrix-bridge/mx-recv --wait
```

- Send only the material the user has authorized for the configured destination.
  Preparing a draft or checking connectivity does not authorize sending it.
- `--doctor` checks the authenticated account and room without sending anything
  or advancing the receive cursor.
- With no cursor, the first receive records the current position and does not
  replay history. Subsequent receives print new text and download attachments;
  use the printed file paths to inspect received images or files.
- `--wait` polls for up to ten minutes and returns as soon as a new message is
  available. Invoke it when waiting for a transfer is part of the user's task.
- Receive failures preserve the cursor. A retry may repeat messages already
  printed before an interrupted state write; it must not silently skip failed
  downloads. A truncated sync timeline is an error, not an empty inbox.
- Send failures return nonzero. A network failure can leave delivery uncertain;
  inspect the room before repeating a send. There are no automatic send retries.
- This bridge does not implement end-to-end encryption or account login. It
  refuses encrypted rooms. Do not use the plaintext bridge to transfer secrets.

Keep a single receiver for each cursor. Preserve the configured cursor during
migration so installing a new package does not discard unread transfers.
