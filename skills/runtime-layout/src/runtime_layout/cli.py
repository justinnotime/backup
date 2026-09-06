"""Explicit configuration entry points; inspection never applies a migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .paths import Layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--repository-source", default=str(Path.cwd()))
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("key", nargs="?")
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    try:
        layout = Layout.load(args.config, repository_source=args.repository_source)
        if args.apply and not args.migrate:
            raise ValueError("--apply requires the migration entry")
        if args.shell:
            if args.migrate or args.key:
                raise ValueError("shell output cannot be combined with another operation")
            from .shell import emit

            sys.stdout.write(emit(layout))
        elif args.migrate:
            from .migration import Migrator

            operation = Migrator(layout)
            print(json.dumps(operation.apply() if args.apply else operation.plan(), indent=2))
        elif args.key:
            value = layout.resolve(args.key, *args.arguments)
            if isinstance(value, bool):
                return 0 if value else 1
            print(value)
        else:
            for name, rule in layout.config["paths"].items():
                if not rule.get("arguments"):
                    print(f"{name:24} {layout.resolve(name)}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(f"ERROR runtime layout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
