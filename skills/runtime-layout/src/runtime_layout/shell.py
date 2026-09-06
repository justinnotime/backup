"""Compile trusted configuration once into fast Bash path bindings."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .paths import Layout


def emit(layout: Layout) -> str:
    quote = shlex.quote
    root = layout.config["root"]
    variable = root.get("environment")

    def literal(value: str) -> str:
        pieces = re.split(
            r"(\{(?:root|home|repository|[0-9]+)\}|\$(?:HOME\b|\{HOME\})|~(?=/|$))", value
        )
        result = []
        for piece in pieces:
            if piece == "{root}":
                result.append('"$(_rl_root)"')
            elif piece in ("{home}", "$HOME", "${HOME}", "~"):
                result.append('"$HOME"')
            elif piece == "{repository}":
                result.append('"$(_rl_repository)"')
            elif re.fullmatch(r"\{[0-9]+\}", piece):
                result.append('"${' + str(int(piece[1:-1]) + 1) + '}"')
            elif piece:
                result.append(quote(piece))
        return "".join(result) or "''"

    code = [Path(__file__).with_name("paths.sh").read_text()]
    # A single main-checkout query initializes this shell. Overrides stay dynamic.
    repository_rule = layout.config.get("repository", {})
    environment = repository_rule.get("environment")
    source_repository = layout.repository(ignore_override=True)
    code += [f"_rl_repository_default={quote(str(source_repository))}"]
    value = (
        f'"${{{environment}:-$_rl_repository_default}}"'
        if environment
        else '"$_rl_repository_default"'
    )
    code += [f"_rl_repository() {{ printf '%s\\n' {value}; }}"]
    default = literal(root["default"])
    if variable:
        operator = ":-" if root.get("shell_empty_environment", "default") == "default" else "-"
        code += [
            f"_rl_root() {{ local fallback={default}; printf '%s\\n' \"${{{variable}{operator}$fallback}}\"; }}"
        ]
        check = (
            f'-n "${{{variable}:-}}"'
            if root.get("shell_activation", "nonempty") == "nonempty"
            else f"${{{variable}+set}} == set"
        )
        code += [f'_rl_active() {{ [[ {check} || -d "$(_rl_root)" ]]; }}']
    else:
        code += [
            f"_rl_root() {{ printf '%s\\n' {default}; }}",
            '_rl_active() { [[ -d "$(_rl_root)" ]]; }',
        ]
    note = layout.config.get(
        "shell_legacy_note", layout.config.get("legacy_note", "NOTE using legacy path {path}")
    )
    prefix, _, suffix = note.partition("{path}")
    code += [f"_rl_note() {{ printf '%s%s%s\\n' {quote(prefix)} \"$1\" {quote(suffix)} >&2; }}"]
    for name, rule in layout.config["paths"].items():
        body = [
            f'[[ $# -eq {rule.get("arguments", 0)} ]] || {{ echo "invalid path argument count" >&2; return 2; }}'
        ]
        for variable in rule.get("environment", []):
            body += [
                f'if [[ -n "${{{variable}:-}}" ]]; then printf \'%s\\n\' "${{{variable}}}"; return; fi'
            ]
        kind = rule["kind"]
        simple = {"root": "_rl_root", "active_flag": "_rl_active", "repository": "_rl_repository"}
        if kind in simple:
            body += [simple[kind]]
        elif kind == "sibling":
            body += [
                f"local base; base=$(_rl_repository) || return; printf '%s/%s\\n' \"${{base%/*}}\" {quote(rule['name'])}"
            ]
        elif kind == "alias":
            body += [f'_rl_path_{rule["target"]} "$@"']
        else:
            target = literal(rule["path"])
            old = [literal(value) for value in rule.get("legacy", [])]
            if kind == "fixed":
                body += [f"printf '%s\\n' {target}"]
            elif kind == "active":
                body += [f"_rl_active_choose {target} {old[0]}"]
            elif kind == "content":
                body += [f"_rl_content {target} {old[0]}"]
            elif kind == "file":
                body += [f"_rl_file {target} " + " ".join(old)]
            elif kind == "glob":
                body += [f"_rl_glob {quote(rule['pattern'])} {target} " + " ".join(old)]
        code += [f"_rl_path_{name}() {{ " + "; ".join(body) + "; }"]
    for function, route in layout.config.get("shell_functions", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function):
            raise ValueError("invalid shell function name")
        primitives = {
            "@file": '_rl_file "$(_rl_root)/$1" "${@:2}"',
            "@active": '_rl_active_choose "$(_rl_root)/$1" "$2"',
            "@content": '_rl_content "$(_rl_root)/$1" "$2"',
            "@note": '_rl_note "$@"',
        }
        if route not in primitives and route not in layout.config["paths"]:
            raise ValueError("unknown shell binding")
        command = primitives.get(route, f'_rl_path_{route} "$@"')
        code += [f"{function}() {{ {command}; }}"]
    return "\n".join(code) + "\n"
