from conftest import synthetic_config

from activity_summary import facts
from activity_summary.config import activate


def configure(root):
    cfg = synthetic_config(root)
    cfg["facts"]["session_sources"] = [
        {"directory": "sources/conversations", "label": "configured-claw", "format": "claw"}
    ]
    activate(cfg)


def old_format():
    return "# Claw Session 01234567\n\n- **Date:** 2024-01-02 10:00 UTC\n- **Session ID:** `01234567-89ab-cdef-0123-456789abcdef`\n\n---\n\n## \U0001f464 User (10:00)\n\nOriginal request\n\n## \U0001f916 Assistant (10:01)\n\nOriginal response\n"


def managed_format():
    return "# Synthetic session\n\n- Managed-By: agent-session-extraction/v1\n- Tool: openclaw\n- Session: abcdef01-2345-6789-abcd-ef0123456789\n- Started: 2024-01-01 10:00:00Z\n\n---\n\n### 2024-01-01 10:00:00Z — user\n\n> Previous day request\n\n### 2024-01-02 11:00:00Z — user\n\n> Current day request\n\n### 2024-01-02 11:01:00Z — assistant\n\nCurrent response\n"


def test_same_selected_directory_reads_old_and_managed_without_missing_or_double_counting(tmp_path):
    directory = tmp_path / "sources/conversations"
    directory.mkdir(parents=True)
    (directory / "old.md").write_text(old_format())
    (directory / "managed.md").write_text(managed_format())
    configure(tmp_path)
    clusters = facts.cluster_sessions(str(tmp_path), "2024-01-02", 45)
    assert sum(item["messages"] for item in clusters) == 4
    assert sum(item["n_real_prompts"] for item in clusters) == 2
    prompts = [prompt for cluster in clusters for prompt in cluster["user_prompts"]]
    assert prompts == ["Original request", "Current day request"]
    managed = clusters[1]
    assert managed["continued_from"] == ["2024-01-01"]
    assert managed["sessions"][0]["source"] == "configured-claw"
    assert managed["sessions"][0]["session"] == "abcdef01"


def test_old_claw_keeps_existing_shape_and_values(tmp_path):
    directory = tmp_path / "sources/conversations"
    directory.mkdir(parents=True)
    (directory / "old.md").write_text(old_format())
    configure(tmp_path)
    result = facts.cluster_sessions(str(tmp_path), "2024-01-02", 45)
    assert result == [
        {
            "kind": "human",
            "time": "10:00–10:01Z",
            "span_min": 1,
            "messages": 2,
            "n_sessions": 1,
            "n_real_prompts": 1,
            "sessions": [
                {
                    "session": "01234567",
                    "source": "configured-claw",
                    "title": "",
                    "n_real_prompts": 1,
                    "started_on": "2024-01-02",
                }
            ],
            "user_prompts": ["Original request"],
            "continued_from": [],
        }
    ]


def test_managed_later_append_does_not_change_previous_date(tmp_path):
    directory = tmp_path / "sources/conversations"
    directory.mkdir(parents=True)
    path = directory / "managed.md"
    path.write_text(managed_format())
    configure(tmp_path)
    before = facts.cluster_sessions(str(tmp_path), "2024-01-02", 45)
    path.write_text(managed_format() + "\n### 2024-01-03 12:00:00Z — user\n\n> Later day request\n")
    assert facts.cluster_sessions(str(tmp_path), "2024-01-02", 45) == before
