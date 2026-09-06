---
name: privacy-lint
description: Review an explicitly selected publication set for private identifiers, infrastructure details, operational history, or links into private material. Use before publishing or sharing named files, or for an explicit privacy audit. Keep personal patterns in an optional private profile; a folder name alone does not make its contents publication-bound.
---

# Publication privacy lint

Identify the exact files or artifacts being prepared for publication. Include
the content that will actually leave the private environment, such as examples,
attachments, rendered output, and generated metadata. Do not expand the audit
to unrelated private notes merely because they share a research directory.

Apply the user's stated publication policy. Read `$PRIVACY_LINT_PROFILE` when
set, or `${XDG_CONFIG_HOME:-$HOME/.config}/privacy-lint/profile.md` when present,
for optional private names, domains, machine labels, account handles, and local
publication rules. Current instructions take precedence. Disclose an unreadable
explicitly selected profile; do not claim its identifiers were checked. Without
a profile, inspect the selected content for contextual privacy issues and state
that no owner-specific identifier list was available.

Ask whether publishing the exact text would reveal information outside the
intended audience. Look for private hostnames and domains, account identifiers,
real infrastructure addresses, user-specific filesystem paths, internal room or
project identifiers, dated operational records, unapproved personal details, and
links or embedded material that lead back into a private repository. Check the
meaning and context as well as exact strings. First-person deployment prose can
reveal a private operational history even when individual names were replaced.

Public products, approved attribution, and reader-facing examples can be valid.
An occurrence needs a contextual decision rather than a mechanical deletion.
Do not treat an ordinary routable address as a safe invented example. Use clear
placeholders or the [IPv4 documentation ranges](https://www.rfc-editor.org/info/rfc5737/)
for synthetic examples. Standard loopback and bind-address examples can remain
when they do not describe the owner's deployment.

Prefer removing private operational detail that is unnecessary to the published
point. Keep personal history in its private source when it does not belong in
the public artifact. Generalize examples where that preserves their meaning;
do not fabricate a different real deployment or erase required attribution.
Only edit within the authorized publication task. Privacy review does not itself
authorize publishing, deleting unrelated private records, or widening access.

Resolve each finding with a specific disposition: removed, generalized, left
private, explicitly approved for this audience, or a documented false positive.
A blanket waiver is not evidence that a whole file is safe. Existing exceptions
need their reason and scope inspected. If credentials are found, handle the
exposure without repeating their values; publication privacy and credential
scanning are distinct checks.

Review the final artifact again after edits. Report the exact publication set,
the policy/profile applied, unresolved findings, and inspection limits. Do not
call content publication-ready while an applicable privacy question is unresolved.
Keep evidence containing private identifiers in private storage, not in the
public PR or published report.

This Skill is a review procedure, not a bundled classifier. Development format
validation is `uv run --project /path/to/privacy-lint --locked skills-ref validate /path/to/privacy-lint`.
