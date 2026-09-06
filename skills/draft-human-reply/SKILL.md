---
name: draft-human-reply
description: Draft, rewrite, or review email and chat replies in the user's own voice. Use when a reply should sound more natural, less formulaic, or ready to copy. Supports iterative feedback and an explicitly requested transfer of the final draft to a private staging destination; drafting does not authorize delivery to the recipient.
---

# Draft human reply

Preserve the user's intended facts, terms, negotiation position, and asks while
making the message sound like something they would write. Do not invent facts,
promises, deadlines, concessions, typos, or deliberate mistakes.

If the user supplied a writing profile, read it. Otherwise, check
`$DRAFT_HUMAN_REPLY_PROFILE` when set, or
`${XDG_CONFIG_HOME:-$HOME/.config}/draft-human-reply/profile.md` when present.
These are optional private preferences, not files shipped with the Skill.
If an explicitly selected profile cannot be read, say so rather than claiming
to have applied it. Current user instructions take precedence over a profile.
Without a profile, use the user's wording and the thread's tone; do not invent
preferences for an unfamiliar user.

Work from the supplied conversation and the user's own draft. Identify what
the recipient needs to understand or do, which numbers and conditions must
remain exact, which outward terms the user chose to include, and which internal
limits or fallback positions must stay private. Consider the relationship,
channel, language, and level of formality. Fetch additional context only when
needed and available through an authorized reading tool. Sufficient context
means write now, without adding background just to make the draft look fuller.

Prefer the user's concrete words over polished abstractions. Keep each intended
ask clear. Match the user's natural punctuation and sentence rhythm; avoid
repeated setup, staged transitions, ornamental lists, or a closing summary that
only repeats the point. Informal writing should still be easy to read.

Read the full draft as the recipient before showing it. Check every number,
date, condition, commitment, and ask against the source. Preserve every outward
term the user intended to disclose while keeping undisclosed internal strategy
private. Check that the draft sounds spoken in the user's register and that
shortening it did not remove a caveat or change the bargaining position.

Show the copyable draft first, with explanations outside it. Offer another
version only when it represents a useful choice. Apply feedback to the entire
draft, then repeat the checks; a local wording edit must not leave an old error
elsewhere. When the draft still feels formulaic, simplify its sentence structure
before substituting isolated words. Do not manufacture errors to sound human.

Drafting and approval of wording do not authorize sending. If the user explicitly
requests a transfer to a private staging destination, use that destination's
available integration and configuration. Transfer only the exact approved or
requested draft, without a title, explanation, code fence, or assistant signature.
Confirm the destination when it is ambiguous. Report success only after the
integration confirms it. A staging transfer does not authorize sending to the
actual recipient; recipient delivery belongs to the relevant platform workflow
with the user's authorization.

This Skill has no sending client and no required runtime dependencies. Its
development-only format check is `uv run --project /path/to/draft-human-reply --locked skills-ref validate /path/to/draft-human-reply`.
