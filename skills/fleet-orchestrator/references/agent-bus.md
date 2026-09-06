# Durable messaging

`scripts/matrix-bus.sh` and the installed `agent-bus` command operate the bundled
transport. Both local and Matrix modes keep identity, inbox, outgoing messages,
delivery leases, and processing acknowledgments in SQLite. Local mode writes
directly to the shared host database. Matrix mode additionally requires the
explicit private service and room configuration described in
[configuration](configuration.md).

Use `agent-bus --help` for the current commands. `members` and `unread` inspect
state; joining, sending, pulling a leased presentation, acknowledging, reviving,
or retiring an identity changes that state. Keep identity selection explicit.
Do not treat a registered identity as proof that a model is responsive.

The transport distinguishes accepted, delivered, presented, and processed.
Repeated presentation is bounded; a repeatedly unacknowledged message can be
parked until explicitly revived. Callers must deduplicate external side effects
by message identity. A processed acknowledgment belongs after actual handling,
not merely after fetching the message.

`scripts/agent-boot.sh` coordinates current-pane identity recovery, ORC
onboarding, and the configured startup briefing. It requires a real known pane
for terminal identities. `scripts/install-agent-bus-pull-notify.py` installs
only the explicitly selected hook/transport integration. Local mode requires
no Matrix dispatcher service. Matrix installation requires a caller-owned
service template; inspect its help for the supported substitutions.

No configuration, transport receipt, or peer message grants operator approval.
Only send messages within the user's existing authorization.
