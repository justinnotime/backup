"""Text-free observations; rules describe visible actions, never their value."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def seconds(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return value / 1000
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except ValueError:
            pass
    return None


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(content_text(item) for item in value)
    if isinstance(value, dict):
        return content_text(value.get("text", value.get("content", "")))
    return ""


class FeatureRules:
    def __init__(self, config=None):
        config = config or {}
        self.patterns = {}
        for field in (
            "peer_patterns",
            "notification_patterns",
            "context_patterns",
            "function_rules",
            "action_rules",
            "project_rules",
        ):
            values = config.get(field, [] if field.endswith("patterns") else {})
            if field.endswith("patterns"):
                self.patterns[field] = tuple(
                    re.compile(p, re.IGNORECASE) for p in values
                )
            else:
                self.patterns[field] = tuple(
                    (label, re.compile(p, re.IGNORECASE)) for label, p in values.items()
                )

    def matches(self, field, text):
        return any(p.search(text) for p in self.patterns[field])

    def labels(self, field, text):
        return sorted(label for label, p in self.patterns[field] if p.search(text))

    def origin(self, text, native="", child=False, system=False):
        text = text.strip()
        if self.matches("peer_patterns", text) or text.startswith(
            ("<teammate-message", "Message Type: MESSAGE", "Message Type: FINAL_ANSWER")
        ):
            return "peer", "explicit-marker"
        if native in ("agent", "peer", "teammate"):
            return "peer", "native-origin"
        if text.startswith("<task-notification>") or self.matches(
            "notification_patterns", text
        ):
            return "notification", "notification-marker"
        if (
            system
            or text.startswith(
                (
                    "<system-reminder>",
                    "# AGENTS.md instructions",
                    "<environment_context>",
                    "This session is being continued",
                    "Base directory for this skill:",
                    "<local-command-",
                    "/compact",
                )
            )
            or self.matches("context_patterns", text)
        ):
            return "system-context", "context-marker"
        if child:
            return "delegated-or-unknown", "child-session"
        if native in ("human", "user"):
            return "human", "native-origin"
        return "unknown", "user-role-only"

    def actions(self, name, args):
        serialized = args if isinstance(args, str) else json.dumps(args, sort_keys=True)
        value = name + " " + serialized
        result = set(self.labels("action_rules", value))
        name = name.lower()
        if any(p in name for p in ("send_message", "followup_task", "sendmessage")):
            result.add("coordinate-send")
        elif any(p in name for p in ("wait_agent", "list_agents")):
            result.add("coordinate-receive")
        elif any(
            p in name for p in ("spawn_agent", "taskcreate", "taskupdate")
        ) or name in ("task", "agent"):
            result.add("delegate")
        elif any(p in name for p in ("write_stdin", "sleep")):
            result.add("wait-observe")
        elif any(
            p in name for p in ("websearch", "webfetch", "search_query", "web__run")
        ):
            result.add("external-research")
        elif any(p in name for p in ("apply_patch", "edit", "write")):
            result.add("edit")
        elif name in ("read", "readfile", "glob", "grep", "ls"):
            result.add("inspect-files")
        # Shell strings may contain quoted code. These are action candidates,
        # not proof that an executable ran or that the task succeeded.
        if name in ("bash", "shell") or "exec" in name:
            patterns = {
                "test": r"\b(pytest|unittest)\b|\b(cargo|go|npm|pnpm)\s+(run\s+)?test\b",
                "inspect-git": r"\bgit\s+(diff|show|log|status|blame)\b",
                "inspect-files": r"\b(rg|grep|cat|head|tail|sed|find|ls)\b",
                "edit": r"apply_patch|\.write_(text|bytes)\s*\(|\bsed\s+-i\b",
                "benchmark": r"\b(fio|iperf3|hyperfine)\b|\bcargo\s+bench\b",
                "wait-observe": r"\b(sleep|wait|pgrep)\b",
            }
            for label, pattern in patterns.items():
                if re.search(pattern, serialized, re.IGNORECASE):
                    result.add(label)
        return result or {"other-tool"}


class Trace:
    def __init__(self, harness, session, rules, child=False):
        self.harness, self.session, self.rules, self.child = (
            harness,
            session,
            rules,
            child,
        )
        self.task = None
        self.wake = None
        self.ready = None
        self.input_kind = "unknown"
        self.compactions = 0
        self.events = []
        self.tool_labels = {}
        self.last_representation = None
        self.calls = {}

    def input(self, text, at, location, native="", system=False, representation=None):
        at = seconds(at)
        self.ready = at
        origin, evidence = self.rules.origin(text, native, self.child, system)
        signature = digest((self.harness, self.session, at, text))
        # Codex may store the same input in two different transcript streams.
        if (
            self.wake
            and representation
            and self.last_representation != representation
            and (
                self.wake["_digest"] == digest(text)
                and at is not None
                and self.wake["at"] is not None
                and abs(at - self.wake["at"]) < 2
            )
        ):
            self.last_representation = representation
            return
        event = {
            "id": signature,
            "at": at,
            "origin": origin,
            "origin_evidence": evidence,
            "previous_input_gap_seconds": at - self.wake["at"]
            if self.wake and at is not None and self.wake["at"] is not None
            else None,
            "previous_task_gap_seconds": at - self.task["at"]
            if self.task and at is not None and self.task["at"] is not None
            else None,
            "chars": len(text),
            "function_candidates": self.rules.labels("function_rules", text),
            "location": location,
            "_digest": digest(text),
        }
        self.events.append(event)
        self.last_representation = representation
        self.wake = event
        self.input_kind = origin
        if origin not in ("notification", "system-context"):
            self.task = event

    def result(self, at, tool_id=None):
        self.ready = seconds(at)
        self.input_kind = (
            "inbox-result"
            if "coordinate-receive" in self.tool_labels.get(tool_id, ())
            else "tool-result"
        )

    def compact(self):
        self.compactions += 1
        self.input_kind = "compaction"
        # Missing request timing after compaction must not use a stale bound.
        self.ready = None

    def call(self, key, at):
        if key not in self.calls:
            self.calls[key] = {
                "task_id": self.task["id"] if self.task else None,
                "task_origin": self.task["origin"] if self.task else "unknown",
                "function_candidates": self.task["function_candidates"]
                if self.task
                else [],
                "wake_id": self.wake["id"] if self.wake else None,
                "wake_origin": self.wake["origin"] if self.wake else "unknown",
                "wake_at": self.wake["at"] if self.wake else None,
                "wake_gap_seconds": self.wake["previous_input_gap_seconds"]
                if self.wake
                else None,
                "task_gap_seconds": self.task["previous_task_gap_seconds"]
                if self.task
                else None,
                "input_kind": self.input_kind,
                "ready_at": self.ready,
                "first_response_at": seconds(at),
                "request_start_at": None,
                "compactions": self.compactions,
                "actions": set(),
                "tools": {},
            }
        return self.calls[key]

    def tool(self, call, key, name, args):
        key = key or digest((name, args))
        if key in call["tools"]:
            return
        labels = self.rules.actions(name, args)
        call["tools"][key] = sorted(labels)
        call["actions"].update(labels)
        self.tool_labels[key] = labels


def call_features(call):
    return {
        **{k: v for k, v in call.items() if k not in ("actions", "tools")},
        "actions": sorted(call["actions"]),
        "tool_count": len(call["tools"]),
    }
