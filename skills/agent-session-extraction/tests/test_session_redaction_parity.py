from __future__ import annotations

from agent_skills.sessions.manifest import RedactionSpec
from agent_skills.sessions.redact import Redactor

CANARIES = (
    (
        "private-key-block",
        (
            "-----BEGIN RSA PRIVATE KEY-----\nFAKECANARYBODY\n"
            "-----END RSA PRIVATE KEY-----"
        ),
        "FAKECANARYBODY",
    ),
    (
        "known-key-prefix",
        "gsk-FAKE00000000000000CANARY",
        "FAKE00000000000000CANARY",
    ),
    (
        "known-key-prefix",
        "e2b_FAKE00000000000000CANARY",
        "FAKE00000000000000CANARY",
    ),
    (
        "known-key-prefix",
        "ghp_FAKE00000000000000CANARY",
        "FAKE00000000000000CANARY",
    ),
    (
        "known-key-prefix",
        "gh_FAKE00000000000000CANARY0",
        "FAKE00000000000000CANARY0",
    ),
    (
        "known-key-prefix",
        "sk-FAKE00000000000000CANARY",
        "FAKE00000000000000CANARY",
    ),
    ("slack-token", "xoxb-000000-FAKECANARY00", "FAKECANARY00"),
    ("aws-access-key-id", "AKIAFAKECANARY000000", "AKIAFAKECANARY000000"),
    (
        "google-api-key",
        "AIzaFAKE0canary0FAKE0canary0",
        "FAKE0canary0FAKE0canary0",
    ),
    (
        "google-oauth-token",
        "ya29.FAKE0canary0FAKE0canary0",
        "FAKE0canary0FAKE0canary0",
    ),
    (
        "jwt",
        "eyJFAKEcanary0.eyJFAKEcanary0.FAKEsig",
        "eyJFAKEcanary0.eyJFAKEcanary0",
    ),
    (
        "x-access-token-url",
        "https://x-access-token:FAKECANARY@example.invalid/repository.git",
        ":FAKECANARY@",
    ),
    (
        "url-userinfo-password",
        "https://service:FAKECANARY@example.invalid/path",
        ":FAKECANARY@",
    ),
    (
        "bearer",
        "Authorization: Bearer FAKE0canary0FAKE0canary0",
        "FAKE0canary0FAKE0canary0",
    ),
    (
        "named-secret-field",
        "SERVICE_API_KEY=FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        '"client_secret": "FAKE0canary0FAKE"',
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        "CHAT_ACCESS_TOKEN=FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        "refresh_token: FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        "SERVICE_BOT_TOKEN=FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        "CLOUD_SECRET_ACCESS_KEY=FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
    (
        "named-secret-field",
        "password=FAKE0canary0FAKE",
        "FAKE0canary0FAKE",
    ),
)


def default_redactor() -> Redactor:
    return Redactor.from_spec(RedactionSpec(True, "default", ()))


def test_all_work_proven_generic_canary_shapes_are_visible_and_idempotent() -> None:
    redactor = default_redactor()

    assert len(CANARIES) == 21
    for expected_name, sample, must_vanish in CANARIES:
        transformed, counts = redactor.apply(sample)
        assert must_vanish not in transformed, expected_name
        assert f"[REDACTED:{expected_name}]" in transformed, expected_name
        assert counts.get(expected_name, 0) >= 1, expected_name
        assert redactor.apply(transformed)[0] == transformed, expected_name


def test_safe_context_is_kept_around_structured_credentials() -> None:
    redactor = default_redactor()

    url, _ = redactor.apply(
        "https://service:FAKECANARY@example.invalid/repository.git"
    )
    bearer, _ = redactor.apply(
        "Authorization: Bearer FAKE0canary0FAKE0canary0"
    )
    assignment, _ = redactor.apply("SERVICE_API_KEY=FAKE0canary0FAKE")

    assert url.startswith("https://service:[REDACTED:url-userinfo-password]@")
    assert bearer.startswith("Authorization: Bearer [REDACTED:bearer]")
    assert assignment.startswith("SERVICE_API_KEY=[REDACTED:named-secret-field]")


def test_clean_text_passes_through_byte_identical() -> None:
    clean = """commit 34bcce328abc fixed a check
max_tokens=4096, SESSION_KEY = "fixture_template"
clone from https://example.invalid/organization/repository.git
the x-access-token mechanism and the gsk- prefix, in prose
gsk-proxy-config-notes is a filename, not a token
Bearer of bad news; [REDACTED:jwt] from an earlier pass
"pending": 0, SERVICE_API_KEY mentioned with no value
普通文字必须原样通过。"""

    transformed, counts = default_redactor().apply(clean)

    assert transformed == clean
    assert counts == {}
