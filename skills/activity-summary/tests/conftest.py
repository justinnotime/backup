import pytest

from activity_summary.config import activate


def synthetic_config(root):
    return {
        "schema": "activity-summary/v1",
        "repository_root": str(root),
        "facts": {
            "issue_directory": "sources/issues",
            "default_issue_repository": "example-org/alpha",
            "document_directory": "sources/documents",
            "wiki_project_directory": "knowledge/projects",
            "summary_directory": "summaries",
            "commit_directories": ["sources", "knowledge"],
            "project_patterns": [r"(?:sources|knowledge)/projects?/([^/]+)/"],
            "session_sources": [
                {
                    "directory": "sources/assistant-history",
                    "label": "assistant-history",
                    "format": "history",
                },
                {
                    "directory": "sources/delta-history",
                    "label": "delta-history",
                    "format": "history",
                },
            ],
            "gap_minutes": 45,
        },
        "daily": {
            "output_directory": "summaries",
            "validation": {"commentary_first_line_pattern": r"^(?:Open item|No open items):"},
        },
        "weekly": {"output_directory": "summaries/weekly"},
    }


@pytest.fixture(autouse=True)
def reset_settings(tmp_path):
    activate(synthetic_config(tmp_path))
