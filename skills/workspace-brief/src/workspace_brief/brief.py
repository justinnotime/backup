"""Local workspace briefing; the selected configuration owns paths and wording."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def expand(value: str, root: Path | None = None) -> str:
    if root is not None:
        value = value.replace("@root@", str(root))
    return os.path.expanduser(os.path.expandvars(value))


def load_config(path: str, root: str | None = None) -> dict:
    config = json.loads(Path(expand(path)).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != "workspace-brief/v1":
        raise ValueError("unsupported briefing configuration")
    selected = root or config["repository_root"]
    if not isinstance(selected, str) or not selected:
        raise ValueError("repository_root must be explicit")
    config["repository_root"] = Path(expand(selected))
    if not config["repository_root"].is_absolute():
        raise ValueError("repository_root must be absolute")
    for key in ("queue", "projects", "latest", "health", "worktrees", "storage", "hook"):
        if key in config and not isinstance(config[key], dict):
            raise ValueError("invalid briefing section")
    for key in ("header", "after_health", "footer"):
        if key in config and (
            not isinstance(config[key], list) or not all(isinstance(x, str) for x in config[key])
        ):
            raise ValueError("text sections must be string lists")
    return config


def path_at(value: str, root: Path) -> Path:
    path = Path(expand(value, root))
    return path if path.is_absolute() else root / path


def age(path: Path, now: datetime) -> str:
    days = max(0, int((now.timestamp() - int(path.stat().st_mtime)) / 86400))
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def title(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = re.search(r"^title:[ \t]*(.*)$", text, re.MULTILINE)
    if found:
        value = found.group(1).strip().strip("\"'")
    else:
        found = re.search(r"^# (.*)$", text, re.MULTILINE)
        value = found.group(1) if found else path.stem
    return value if len(value) <= 80 else value[:77] + "..."


def markdown_files(directory: Path, recursive: bool = False, pattern: str = "*.md") -> list[Path]:
    """Do not follow directory or file symlinks outside the selected source."""
    if not directory.is_dir() or directory.is_symlink():
        return []
    files = []
    for parent, directories, names in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in names:
            path = Path(parent) / name
            if path.match(pattern) and path.is_file() and not path.is_symlink():
                files.append(path)
        if not recursive:
            break
    return files


def newest(files: list[Path]) -> Path | None:
    return max(files, key=lambda path: (path.stat().st_mtime, str(path)), default=None)


def git(root: Path, *args: str, timeout: float = 2) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise ValueError("Git inspection failed")
    return result.stdout.strip()


def queue_lines(config: dict, root: Path) -> list[str]:
    command = config["argv"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("queue argv must be explicit")
    command = [expand(arg, root) for arg in command]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=config.get("timeout_seconds", 2),
            check=False,
        )
        if result.returncode:
            raise ValueError("queue inspection failed")
        return [
            config["heading"],
            *(config.get("indent", "  ") + line for line in result.stdout.splitlines()),
            "",
        ]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [config["heading"], config.get("unavailable", "  WARN queue unavailable"), ""]


def project_lines(config: dict, root: Path, now: datetime) -> list[str]:
    directory = path_at(config["directory"], root)
    if not directory.is_dir() or directory.is_symlink():
        return []
    directories = [path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()]
    candidates = []
    readme_name = config.get("readme", "README.md")
    for project in directories:
        files = markdown_files(project, recursive=True)
        if not files:
            continue
        readme = project / readme_name
        if readme.is_file() and not readme.is_symlink():
            match = re.search(
                r"^status:[ \t]*(.*)$",
                readme.read_text(encoding="utf-8", errors="replace"),
                re.MULTILINE,
            )
            status = match.group(1).strip().lower() if match else ""
            if any(
                status.startswith(prefix.lower())
                for prefix in config.get("exclude_status_prefixes", [])
            ):
                continue
        candidates.append((max(path.stat().st_mtime for path in files), project.name, project))
    selected = sorted(candidates, reverse=True)[: int(config.get("limit", 5))]
    if not selected:
        return []
    lines = [config["heading"].format(shown=len(selected), total=len(directories))]
    template = config.get("file_line", "    [{age:<4}] {path}: {title}")
    for _, name, project in selected:
        readme = project / readme_name
        if readme.is_file() and not readme.is_symlink():
            lines.append(
                template.format(
                    age=age(readme, now), path=f"{name}/{readme_name}", title=title(readme)
                )
            )
        else:
            lines.append(config.get("missing_readme", "    {project}/").format(project=name))
        latest = newest(markdown_files(project, pattern=config.get("latest_pattern", "20*.md")))
        if latest:
            lines.append(
                template.format(
                    age=age(latest, now), path=f"{name}/{latest.name}", title=title(latest)
                )
            )
    return [*lines, ""]


def latest_lines(config: dict, root: Path, now: datetime) -> list[str]:
    files = markdown_files(
        path_at(config["directory"], root), pattern=config.get("pattern", "*.md")
    )
    path = newest([path for path in files if path.name not in config.get("exclude", [])])
    if path is None:
        return []
    return [
        config["heading"],
        config.get("file_line", "    [{age:<4}] {path}: {title}").format(
            age=age(path, now), path=path.name, title=title(path)
        ),
        "",
    ]


def expected_date(config: dict, now: datetime) -> str:
    if config["period"] == "daily":
        days = 1 if now.hour >= config.get("ready_hour_utc", 0) else 2
    elif config["period"] == "weekly":
        end = int(config["period_end_weekday"])
        if not 0 <= end <= 6:
            raise ValueError("invalid period end weekday")
        days = (now.weekday() - end) % 7 or 7
        if days == 1 and now.hour < config.get("ready_hour_utc", 0):
            days += 7
    else:
        raise ValueError("unsupported artifact period")
    return (now.date() - timedelta(days=days)).isoformat()


def health_lines(config: dict, root: Path, now: datetime) -> list[str]:
    lines = [config["heading"]]
    marker_error = False
    marker_config = config.get("marker_source")
    if marker_config:
        try:
            job = path_at(marker_config["path"], root)
            selected = json.loads(job.read_text(encoding="utf-8"))
            if selected.get("schema_version") != marker_config["schema_version"]:
                raise ValueError("invalid marker configuration")
            value = selected.get(marker_config["field"])
            if value is not None:
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError("marker path must be absolute")
                marker = Path(value)
                if marker.is_file():
                    with marker.open(encoding="utf-8") as stream:
                        detail = stream.readline().strip()
                    lines.append(marker_config["line"].format(detail=detail, path=marker))
        except (OSError, ValueError, TypeError, AttributeError):
            marker_error = True
            lines.append(marker_config.get("error_line", "  WARN configured marker unavailable"))
    overdue = 0
    observed = 0
    for watch in config.get("logs", []):
        path = next(
            (path_at(item, root) for item in watch["paths"] if path_at(item, root).is_file()), None
        )
        if path is None:
            continue
        observed += 1
        minutes = int((now.timestamp() - int(path.stat().st_mtime)) / 60)
        if minutes > watch["cadence_minutes"] * config.get("cadence_multiplier", 2):
            lines.append(
                config["overdue_line"].format(
                    name=watch["name"],
                    age_minutes=minutes,
                    cadence_minutes=watch["cadence_minutes"],
                )
            )
            overdue += 1
    if overdue == 0:
        if observed and observed == len(config.get("logs", [])) and not marker_error:
            if "healthy_line" in config:
                lines.append(config["healthy_line"])
        else:
            lines.append(config.get("unknown_line", "  WARN configured logs unavailable"))
    for artifact in config.get("artifacts", []):
        date = expected_date(artifact, now)
        relative = artifact["path"].format(date=date)
        if not path_at(relative, root).is_file():
            lines.append(expand(artifact["missing_line"], root).format(date=date, path=relative))
    return lines


def worktree_lines(config: dict, root: Path, now: datetime) -> list[str]:
    lines = []
    deadline = time.monotonic() + float(config.get("budget_seconds", 2))

    def inspect(path: Path, *args: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git inspection", 0)
        return git(path, *args, timeout=remaining)

    for line in inspect(root, "worktree", "list", "--porcelain").splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[9:])
        if not path.is_dir() or path.resolve() == root.resolve():
            continue
        if any(
            path.is_relative_to(path_at(value, root)) for value in config.get("exclude_roots", [])
        ):
            continue
        days = int((now.timestamp() - int(inspect(path, "log", "-1", "--format=%ct"))) / 86400)
        if days >= config.get("idle_days", 2):
            dirty = len(inspect(path, "status", "--porcelain").splitlines())
            try:
                ahead = inspect(
                    path, "rev-list", "--count", config.get("base_ref", "HEAD") + "..HEAD"
                )
            except ValueError:
                ahead = "?"
            lines.append(
                config["line"].format(name=path.name, idle_days=days, dirty=dirty, ahead=ahead)
            )
    return lines


def storage_lines(config: dict, root: Path) -> list[str]:
    path = path_at(config["path"], root)
    values = os.statvfs(path)
    if values.f_favail >= config["minimum_free_inodes"] or values.f_files == 0:
        return []
    used = (100 * (values.f_files - values.f_ffree) + values.f_files - 1) // values.f_files
    return [config["line"].format(path=path, free=values.f_favail, used_percent=f"{used}%"), ""]


def render(
    config: dict, *, now: datetime | None = None, project_dir: str = "", debug: bool = False
) -> str:
    now = now or datetime.now(timezone.utc)
    root = config["repository_root"]
    if not root.is_dir():
        return config.get("missing_root", "").format(root=root) + "\n" if debug else ""
    lines = [expand(line, root) for line in config.get("header", [])]
    if debug and "debug_line" in config:
        lines.append(expand(config["debug_line"], root).format(project_dir=project_dir))
    for key, reader in (
        ("queue", queue_lines),
        ("projects", project_lines),
        ("latest", latest_lines),
        ("health", health_lines),
        ("worktrees", worktree_lines),
    ):
        if key not in config:
            continue
        try:
            lines.extend(
                reader(config[key], root) if key == "queue" else reader(config[key], root, now)
            )
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            lines.append(f"WARN workspace-brief: {key} unavailable")
    lines.extend(expand(line, root) for line in config.get("after_health", []))
    if "storage" in config:
        try:
            lines.extend(storage_lines(config["storage"], root))
        except OSError:
            pass
    lines.extend(expand(line, root) for line in config.get("footer", []))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("WORKSPACE_BRIEF_CONFIG"))
    parser.add_argument("--root")
    parser.add_argument("--project-dir", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    parser.add_argument("--project-limit", type=int)
    parser.add_argument("--marker-config")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.config:
            raise ValueError("select a briefing configuration")
        config = load_config(args.config, args.root)
        if args.project_limit is not None:
            if args.project_limit < 0:
                raise ValueError("project limit must not be negative")
            config["projects"]["limit"] = args.project_limit
        if args.marker_config:
            config["health"]["marker_source"]["path"] = args.marker_config
        if args.doctor:
            if not config["repository_root"].is_dir():
                raise ValueError("configured repository is unavailable")
            if "queue" in config:
                command = config["queue"]["argv"]
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(arg, str) for arg in command)
                ):
                    raise ValueError("invalid queue argv")
                if not shutil.which(expand(command[0], config["repository_root"])):
                    raise ValueError("queue command unavailable")
            if "worktrees" in config and not shutil.which("git"):
                raise ValueError("Git unavailable")
            print("OK workspace-brief: configuration loaded; no commands executed")
        else:
            if not sys.stdin.isatty():
                sys.stdin.read()
            print(render(config, project_dir=args.project_dir, debug=args.debug), end="")
    except (OSError, ValueError, KeyError, TypeError):
        print("WARN workspace-brief: configuration unavailable or invalid")
        return 1 if args.doctor else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
