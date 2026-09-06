"""Explicit private configuration and atomic local record writes."""

import json
import os
import re
import tempfile
from pathlib import Path


def default_config():
    return os.environ.get("GOOGLE_DOCS_AUTHORITY_CONFIG") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "google-docs-authority/config.json"
    )


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path, root_override=None):
    path = Path(os.path.expandvars(str(path))).expanduser().resolve()
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema") != "google-docs-authority/v1"
        or set(value) - {"schema", "write_token_file", "pageless", "registry"}
    ):
        raise ValueError("config-schema-invalid")

    def resolve(text, base=path.parent):
        if not isinstance(text, (str, os.PathLike)) or not str(text):
            raise ValueError("config-path-required")
        expanded = os.path.expandvars(str(text))
        if re.search(r"\$(?:\w+|\{[^}]+\})", expanded):
            raise ValueError("config-path-variable-unresolved")
        target = Path(expanded).expanduser()
        return ((base / target) if not target.is_absolute() else target).resolve()

    if value.get("write_token_file"):
        value["write_token_file"] = resolve(value["write_token_file"])
    value.setdefault("pageless", False)
    if type(value["pageless"]) is not bool:
        raise ValueError("config-pageless-invalid")
    if "registry" in value:
        registry = value["registry"]
        allowed = {
            "repository_root",
            "output",
            "source_directories",
            "mirror_directory",
            "source_lists",
        }
        if not isinstance(registry, dict) or set(registry) - allowed:
            raise ValueError("config-registry-invalid")
        root = resolve(root_override or registry.get("repository_root"))
        if not root.is_dir():
            raise ValueError("config-repository-root-missing")
        registry["repository_root"] = root

        def within_root(text):
            result = resolve(text, root)
            if not result.is_relative_to(root):
                raise ValueError("config-registry-path-outside-repository")
            return result

        registry["output"] = within_root(registry.get("output"))
        directories = registry.get("source_directories", [])
        if not isinstance(directories, list):
            raise ValueError("config-source-directories-invalid")
        registry["source_directories"] = [within_root(item) for item in directories]
        if any(not item.is_dir() for item in registry["source_directories"]):
            raise ValueError("config-source-directory-missing")
        if registry.get("mirror_directory"):
            registry["mirror_directory"] = within_root(registry["mirror_directory"])
        sources = registry.get("source_lists", {})
        if not isinstance(sources, dict) or any(
            not isinstance(k, str) or not k for k in sources
        ):
            raise ValueError("config-source-lists-invalid")
        registry["source_lists"] = {
            name: resolve(location) for name, location in sources.items()
        }
        if registry["output"] in {
            path,
            value.get("write_token_file"),
            *registry["source_lists"].values(),
        }:
            raise ValueError("config-output-must-be-distinct")
    return value
