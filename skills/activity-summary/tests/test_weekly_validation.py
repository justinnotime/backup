import pytest

from activity_summary.weekly_validation import sanitize, validate


def document(missing="", body="Synthetic observations."):
    return f"""---
title: Weekly summary 2024-01-01..2024-01-07
type: summary
generator: weekly-summary
week: 2024-01-01..2024-01-07
inputs_sha256: {"a" * 64}
created: 2024-01-08
updated: 2024-01-08T00:00:00Z
sources: selected daily summaries
missing_inputs: [{missing}]
---

# Weekly summary

## Headlines

{body}

## Projects

Selected recorded events.

## Commentary

Continue the selected work.
"""


def test_weekly_valid_and_wrong_hash_or_window_rejected(tmp_path):
    path = tmp_path / "weekly.md"
    path.write_text(document())
    assert validate(path, "2024-01-07", "a" * 64, "", []) == []
    assert validate(path, "2024-01-07", "b" * 64, "", [])
    path.write_text(document().replace("week: 2024-01-01", "week: 2023-12-31"))
    assert validate(path, "2024-01-07", "a" * 64, "", [])


@pytest.mark.parametrize("body", ["sample#123", "https://github.com/example-org/sample/issues/123"])
def test_weekly_cannot_invent_github_identity(tmp_path, body):
    path = tmp_path / "weekly.md"
    path.write_text(document(body=body))
    assert validate(path, "2024-01-07", "a" * 64, "", [])
    assert validate(path, "2024-01-07", "a" * 64, body, []) == []


def test_missing_inputs_must_match_and_be_acknowledged(tmp_path):
    path = tmp_path / "weekly.md"
    path.write_text(document("2024-01-03", "Missing inputs: 2024-01-03"))
    assert validate(path, "2024-01-07", "a" * 64, "", ["2024-01-03"]) == []
    assert validate(path, "2024-01-07", "a" * 64, "", [])
    path.write_text(document("2024-01-03"))
    assert validate(path, "2024-01-07", "a" * 64, "", ["2024-01-03"])


def test_commentary_sanitizer_does_not_change_other_sections():
    text = document(body="sample#123").replace(
        "Continue the selected work.",
        "sample#123 and https://github.com/example-org/sample/issues/123",
    )
    cleaned = sanitize(text, {})
    assert "sample#123" in cleaned.split("## Commentary")[0]
    assert "sample#123" not in cleaned.split("## Commentary")[1]
