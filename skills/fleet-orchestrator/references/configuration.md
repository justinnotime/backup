# Runtime configuration

Use `orc --config FILE ...` and `agent-bus --config FILE ...`, or set
`FLEET_ORCHESTRATOR_CONFIG` for every selected entry point. The default file is
`${XDG_CONFIG_HOME:-$HOME/.config}/fleet-orchestrator/config.json`. An absent
default file enables standalone local defaults; an explicitly selected missing
or invalid file is an error. Duplicate JSON fields are rejected.

Minimal configuration:

```json
{
  "schema": "fleet-runtime/v1",
  "runtime_dir": "~/state/example-fleet",
  "bus": {
    "transport": "local",
    "config_directory": "~/state/example-fleet/bus",
    "database": "~/state/example-fleet/bus/inbox.sqlite3"
  },
  "paths": {
    "ledger": "~/state/example-fleet/tasks.sqlite3"
  }
}
```

Paths accept `~`, `$HOME`, and other explicitly provided environment variables.
Commands are JSON argument arrays, never interpolated shell programs. Use
separate private scripts when a caller needs shell behavior.

Optional fields:

| Field | Purpose |
|---|---|
| `canonical_source_root` | Formal package installation permitted to write protected databases |
| `protected_databases`, `protected_named_database_roots` | Explicit production database identities and named-fleet roots protected from development copies |
| `paths.orchestrator_state`, `paths.lock_directory`, `paths.lock_prefix` | Runtime observations, snapshots, and default-fleet lock locations; an optional filename prefix preserves an existing lock identity |
| `paths.legacy_drive_state` | Source directory for an explicit legacy-state import |
| `fleets.profile_directory`, `fleets.runtime_directory`, `fleets.matrix_config_directory` | Named fleet profile and isolated storage roots |
| `tmux.server_file` | Optional terminal server selector |
| `matrix.homeserver`, `matrix.room`, `matrix.registry_room`, `matrix.token_file` | Required caller-selected Matrix service, distinct rooms, and private authorization-header file |
| `bus.event_namespace` | Matrix event namespace; preserve it when upgrading an existing transport |
| `bus.dispatcher_template`, `bus.named_dispatcher_template` | Caller-owned service template for an explicitly requested Matrix dispatcher install |
| `authority.merge_keys` | Repository-to-responsible-role mapping; unspecified repositories route to the operator |
| `authority.service_handle`, `authority.receipt_instructions` | Caller identity and review instructions |
| `github.owner`, `github.owner_defaults_file`, `github.sanctioned_logins_file` | GitHub selection and private responsibility mappings |
| `github.excluded_title_prefixes`, `github.excluded_branch_prefixes`, `github.owner_branch_pattern` | Explicit automatic-registration filters and branch identity pattern |
| `github.whole_repositories`, `github.mixed_repositories`, `github.path_substrings` | Review-inspection scope |
| `github.automatic_review_markers` | Comments excluded from substantive review evidence |
| `watched_repositories` | List of objects containing `path`, `kind` (`checkout` or `bare-hub`), and optional `exempt` paths |
| `watcher_exceptions_file`, `bus.watcher_exceptions` | Caller-approved watcher exceptions for task and transport inspection |
| `turn_report.seats_file` | JSON enrollment list for mechanical turn reporting |
| `seat_trailer` | Explicit ledger, member command, window vocabulary, host selector and Git trailer key; see [commit attribution](commit-attribution.md) |
| `commands.brief` | Optional caller-owned startup briefing command |
| `handoff.directory`, `handoff.publish_command` | Local handoff storage and optional external publication |
| `rollout.manifest`, `rollout.source_root`, `rollout.canonical_root`, `rollout.skill_sources` | Artifact manifest, source checkouts, and explicit Skill-name-to-directory mapping |
| `paths.topology`, `backup.config`, `backup.syncthing_configs`, `backup.folder_label` | Optional topology audit inputs |

Existing `NW_*`, `AGENT_BUS_*`, `MATRIX_BUS_*`, `NOTES_RUNTIME_DIR`, and
`DISPATCH_LEDGER_DB` overrides remain accepted for installed callers. They do
not require a particular repository. Named profile commands apply their complete
environment before importing runtime code; keep the same selector throughout
an operation.

Selecting `AGENT_BUS_CFG` or `MATRIX_BUS_CFG` also selects that directory's
`agent-bus-v3.sqlite3` and `auth.hdr`; an explicit `AGENT_BUS_DB` still takes
precedence. This keeps named-fleet credentials and state separate from default
configuration. Named-fleet locks live in `cache/locks` under the selected
fleet runtime, without the default fleet's optional lock prefix.

For a handoff publisher, ORC supplies `ORC_HANDOFF_SRC`, `ORC_HANDOFF_DST`
(a basename), `ORC_HANDOFF_DIRECTORY`, `ORC_HANDOFF_SUBJECT`, and
`ORC_HANDOFF_AGENT`. The command must finish successfully before ORC retires the
identity. Without it, ORC writes an atomic local handoff. An external publisher
is only needed when the caller requires publication beyond local storage.

A runtime configuration can name privileged commands and real identities.
Keep it private and owner-writable, review changes, and do not import an
untrusted peer's configuration. Do not include credential contents in the file.

Artifact entries may set `source_roots` with absolute `working` and `canonical`
paths when caller-owned deployment files live outside the public package.
Staging reads the working source; installation requires matching published
content from the canonical source. Both roots remain private configuration.
