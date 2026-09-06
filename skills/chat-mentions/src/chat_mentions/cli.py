"""Collect Teams attention events and manage local reply drafts."""

import argparse
import os
import sys
from pathlib import Path

from . import drafts
from .config import load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=os.environ.get("CHAT_MENTIONS_CONFIG")
        or str(
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "chat-mentions/config.json"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "collect", help="collect configured Teams attention events; never send"
    )
    sub.add_parser(
        "doctor", help="inspect local settings without authentication or network"
    )
    command = sub.add_parser("open", help="queue events without a local draft")
    command.add_argument("--limit", type=int, default=20)
    command.set_defaults(fn=drafts.cmd_open)
    command = sub.add_parser("new", help="write a reply draft without sending")
    command.add_argument("--chat-id", required=True)
    command.add_argument("--msg-id", required=True)
    command.add_argument("--topic")
    command.add_argument("--sender")
    command.add_argument("--body")
    command.set_defaults(fn=drafts.cmd_new)
    command = sub.add_parser("list")
    command.add_argument(
        "--status", choices=drafts.STATUSES + ("all",), default="pending"
    )
    command.set_defaults(fn=drafts.cmd_list)
    for verb, fn in [
        ("show", drafts.cmd_show),
        ("dismiss", drafts.cmd_dismiss),
        ("mark-sent", drafts.cmd_mark_sent),
    ]:
        command = sub.add_parser(verb)
        command.add_argument("ref", help="exact message ID or path to a stored draft")
        if verb == "mark-sent":
            command.add_argument(
                "--note",
                required=True,
                help="confirmed platform message ID; records only, never sends",
            )
        command.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    try:
        settings = load(args.config)
        drafts.configure(settings)
        if args.command == "doctor":
            print(
                "OK local configuration parsed; no network or authentication attempted"
            )
            print(
                "collection: "
                + ("enabled" if settings["collection_enabled"] else "disabled")
            )
            print(
                "queue: " + ("present" if drafts.queue_path().is_file() else "absent")
            )
        elif args.command == "collect":
            from .collector import run

            run(settings)
        else:
            if args.command == "open" and args.limit < 0:
                raise ValueError("queue-limit-must-be-nonnegative")
            if args.command == "mark-sent" and not args.note.strip():
                raise ValueError("confirmed-platform-message-id-required")
            args.fn(args)
        return 0
    except ValueError as error:
        print("FAIL " + str(error), file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError):
        print("FAIL local-data-or-file-error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
