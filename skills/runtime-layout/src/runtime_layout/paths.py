"""One configured path policy for Python callers and generated shell bindings."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


class Layout:
    def __init__(self, configuration: dict, *, repository_source: str | Path):
        if configuration.get("schema") != "runtime-layout/v1":
            raise ValueError("unsupported runtime layout schema")
        self.config = configuration
        self.source = Path(repository_source).resolve()
        self.noted: set[str] = set()
        if not isinstance(configuration.get("paths"), dict):
            raise TypeError("paths must be an object")
        root = configuration.get("root", {})
        if not isinstance(root.get("default"), str) or not root["default"]:
            raise ValueError("root.default must be a path")
        for name in [
            root.get("environment"),
            configuration.get("repository", {}).get("environment"),
        ]:
            if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable name")
        for name, rule in configuration["paths"].items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid path name")
            if not isinstance(rule, dict) or rule.get("kind") not in {
                "fixed",
                "file",
                "active",
                "content",
                "glob",
                "repository",
                "sibling",
                "root",
                "active_flag",
                "alias",
            }:
                raise ValueError("invalid path rule")
            count = rule.get("arguments", 0)
            if type(count) is not int or count < 0:
                raise ValueError("argument count must be a nonnegative integer")
            if rule["kind"] == "alias" and rule.get("target") not in configuration["paths"]:
                raise ValueError("unknown alias target")
            if not isinstance(rule.get("environment", []), list):
                raise ValueError("environment overrides must be a list")
            for variable in rule.get("environment", []):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                    raise ValueError("invalid environment variable name")

    @classmethod
    def load(cls, path: str | Path, *, repository_source: str | Path) -> Layout:
        return cls(
            json.loads(Path(path).read_text(encoding="utf-8")), repository_source=repository_source
        )

    def root(self) -> Path:
        rule = self.config["root"]
        value = os.environ.get(rule.get("environment", ""), rule["default"])
        if not value and rule.get("empty_environment", "value") == "default":
            value = rule["default"]
        return Path(
            value.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        ).expanduser()

    def active(self) -> bool:
        variable = self.config["root"].get("environment")
        present = variable in os.environ if variable else False
        if self.config["root"].get("activation", "present") == "nonempty":
            present = bool(os.environ.get(variable or ""))
        return present or self.root().is_dir()

    def repository(self, *, ignore_override: bool = False) -> Path:
        rule = self.config.get("repository", {})
        override = os.environ.get(rule.get("environment", ""))
        if override and not ignore_override:
            return Path(override)
        result = subprocess.run(
            ["git", "-C", str(self.source), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        checkout = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                checkout = Path(line[9:])
            elif line == "branch refs/heads/" + rule.get("branch", "main") and checkout:
                return checkout
        return self.source

    def expand(self, value: str, args: tuple[str, ...] = ()) -> Path:
        def replace(match):
            key = match.group(1)
            if key.isdigit():
                try:
                    return str(args[int(key)])
                except IndexError as exc:
                    raise ValueError("missing path argument") from exc
            if key == "root":
                return str(self.root())
            if key == "home":
                return str(Path.home())
            if key == "repository":
                return str(self.repository())
            raise ValueError("unknown path placeholder")

        value = re.sub(r"\$(?:\{HOME\}|HOME\b)", lambda _match: str(Path.home()), value)
        return Path(re.sub(r"\{([^{}]+)\}", replace, value)).expanduser()

    def note_legacy(self, path: Path) -> None:
        if str(path) not in self.noted:
            self.noted.add(str(path))
            message = self.config.get("legacy_note", "NOTE using legacy path {path}")
            print(message.replace("{path}", str(path)), file=sys.stderr, flush=True)

    def resolve_file(
        self, new_rel: str, legacy: list[str] | None = None, env: str | None = None
    ) -> Path:
        if env and os.environ.get(env):
            return Path(os.environ[env]).expanduser()
        target = self.root() / new_rel
        if target.exists():
            return target
        for value in legacy or []:
            candidate = Path(value).expanduser()
            if candidate.exists():
                self.note_legacy(candidate)
                return candidate
        return target

    def active_path(self, new_rel: str, legacy: str) -> Path:
        return self.root() / new_rel if self.active() else Path(legacy).expanduser()

    @staticmethod
    def has_content(path: Path) -> bool:
        try:
            next(path.iterdir())
            return True
        except (StopIteration, FileNotFoundError, NotADirectoryError):
            return False

    def active_dir(self, new_rel: str, legacy: str) -> Path:
        target, old = self.root() / new_rel, Path(legacy).expanduser()
        if self.has_content(target):
            return target
        if self.has_content(old):
            self.note_legacy(old)
            return old
        return target if self.active() else old

    def resolve(self, name: str, *args: str) -> Path | bool:
        rule = self.config["paths"][name]
        expected = rule.get("arguments", 0)
        if len(args) != expected:
            raise ValueError(f"{name} expects {expected} arguments")
        for variable in rule.get("environment", []):
            value = os.environ.get(variable)
            if value:
                return (
                    Path(value).expanduser() if rule.get("expand_override", True) else Path(value)
                )
        kind = rule["kind"]
        if kind == "root":
            return self.root()
        if kind == "active_flag":
            return self.active()
        if kind == "repository":
            return self.repository()
        if kind == "sibling":
            return self.repository().parent / rule["name"]
        if kind == "alias":
            return self.resolve(rule["target"], *args)
        target = self.expand(rule["path"], args)
        old = [self.expand(value, args) for value in rule.get("legacy", [])]
        if kind == "fixed":
            return target
        if kind == "active":
            return target if self.active() else old[0]
        if kind == "content":
            if self.has_content(target):
                return target
            if self.has_content(old[0]):
                self.note_legacy(old[0])
                return old[0]
            return target if self.active() else old[0]
        if kind == "file":
            if target.exists():
                return target
            for path in old:
                if path.exists():
                    self.note_legacy(path)
                    return path
            return target
        if kind == "glob":
            for path in [target, *old]:
                if any(path.glob(rule["pattern"])):
                    if path != target:
                        self.note_legacy(path)
                    return path
            return target
        raise ValueError("unsupported path rule")
