"""Mechanical chunking and evidence selection, independent of provider access."""

import hashlib
import re

import yaml

H1_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
H2_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


def slugify(text: str) -> str:
    """Normalize a heading to a filesystem-safe anchor."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "untitled"


def split_by_h1(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs at H1 boundaries.

    Returns at least one chunk even if no H1s are present — the synthetic
    heading "(no tab)" wraps the whole text in that case.
    """
    matches = list(H1_RE.finditer(text))
    if not matches:
        return [("(no tab)", text)]
    out: list[tuple[str, str]] = []
    # Preface text before the first H1 (e.g. doc-level intro paragraph)
    if matches[0].start() > 0:
        preface = text[: matches[0].start()].strip()
        if len(preface) > 200:  # ignore the tiny "Synced from Google Docs" comment
            out.append(("(preface)", preface))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(1).strip()
        body = text[m.end() : end].strip()
        if body:
            out.append((heading, body))
    return out


def split_oversized(
    heading: str, body: str, max_chars: int = 16000, soft_chars: int = 12000
) -> list[tuple[str, str]]:
    """If a tab is too large, split further on H2 boundaries (or char cap)."""
    if len(body) <= max_chars:
        return [(heading, body)]
    h2_matches = list(H2_RE.finditer(body))
    if not h2_matches:
        return _char_split(heading, body, soft_chars)
    out: list[tuple[str, str]] = []
    current_parts: list[str] = []
    current_chars = 0
    boundaries = [m.start() for m in h2_matches] + [len(body)]
    prev = 0
    for boundary in boundaries:
        segment = body[prev:boundary]
        if current_chars + len(segment) > soft_chars and current_parts:
            out.append((heading, "".join(current_parts).strip()))
            current_parts = []
            current_chars = 0
        current_parts.append(segment)
        current_chars += len(segment)
        prev = boundary
    if current_parts:
        out.append((heading, "".join(current_parts).strip()))
    final: list[tuple[str, str]] = []
    for h, b in out:
        if len(b) > max_chars:
            final.extend(_char_split(h, b, soft_chars))
        else:
            final.append((h, b))
    return final


def _char_split(
    heading: str, body: str, soft_chars: int = 12000
) -> list[tuple[str, str]]:
    """Last-resort char-based split. Tries to break on blank lines."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(body):
        end = min(i + soft_chars, len(body))
        if end < len(body):
            # Find the last blank line before the soft cap
            blank = body.rfind("\n\n", i, end)
            if blank > i + (soft_chars // 2):
                end = blank
        out.append((heading, body[i:end].strip()))
        i = end
    return out


def make_chunks(
    slug: str, readme: str, max_chars: int = 16000, soft_chars: int = 12000
) -> list[dict]:
    """Returns a list of {chunk_id, heading, body, sha1}."""
    raw_chunks = []
    for heading, body in split_by_h1(readme):
        for sub_heading, sub_body in split_oversized(
            heading, body, max_chars, soft_chars
        ):
            raw_chunks.append((sub_heading, sub_body))

    out: list[dict] = []
    heading_seen: dict[str, int] = {}
    for idx, (heading, body) in enumerate(raw_chunks):
        anchor = slugify(heading)
        n = heading_seen.get(anchor, 0)
        chunk_id = f"{idx:03d}-{anchor}" + (f"-{n + 1}" if n else "")
        heading_seen[anchor] = n + 1
        out.append(
            {
                "chunk_id": chunk_id,
                "heading": heading,
                "body": body,
                "sha1": hashlib.sha1(body.encode("utf-8")).hexdigest(),
            }
        )
    return out


def build_user_prompt(
    slug: str,
    manifest: dict,
    year_context: str,
    chunk: dict,
    chunk_idx: int,
    total: int,
) -> str:
    doc_title = manifest.get("title", slug)
    source_url = manifest.get("sourceUrl", "")
    return (
        f"Doc: {doc_title}\n"
        f"Slug: {slug}\n"
        f"Source: {source_url}\n"
        f"Doc year context: {year_context}\n"
        f"Chunk: {chunk_idx + 1} of {total}\n"
        f"Tab heading: {chunk['heading']}\n"
        f"\n"
        f"---\n"
        f"{chunk['body']}\n"
        f"---\n"
    )


def chunk_text_for_match(data: dict) -> str:
    """Build a searchable text blob from a chunk's structured fields."""
    parts: list[str] = []
    parts.append(str(data.get("heading", "")))
    for t in data.get("tasks") or []:
        parts.append(t.get("task", ""))
        parts.extend(t.get("subtasks") or [])
        parts.extend(t.get("blockers") or [])
        parts.append(t.get("solution", "") or "")
        parts.extend(t.get("related_to") or [])
        parts.extend(t.get("files_touched") or [])
    for d in data.get("decisions") or []:
        parts.append(d.get("decision", ""))
        parts.append(d.get("rationale", ""))
        parts.append(d.get("alternative_rejected", "") or "")
    for c in data.get("concepts") or []:
        parts.append(c.get("term", ""))
        parts.append(c.get("definition", ""))
        parts.extend(c.get("aliases") or [])
    for bs in data.get("blockers_solutions") or []:
        parts.append(bs.get("blocker", ""))
        parts.append(bs.get("solution", "") or "")
    parts.extend(data.get("notable_quotes") or [])
    return " \n ".join(p for p in parts if p)


def chunk_matches_theme(text: str, theme: dict) -> bool:
    """Case-insensitive substring match against positive + negative terms."""
    lo = text.lower()
    for ex in theme.get("exclude_terms") or []:
        if ex.lower() in lo:
            return False
    for term in theme.get("search_terms") or []:
        if term.lower() in lo:
            return True
    return False


def compact_chunk_for_theme(c: dict, theme: dict, char_budget: int = 1800) -> dict:
    """Return a slimmed dict for the prompt: keep tasks/decisions/concepts/
    blockers/quotes that match the theme; drop references and people; truncate
    long fields. Filters lines down to those relevant to the theme so each
    chunk's contribution is dense.
    """
    terms = [t.lower() for t in theme.get("search_terms") or []]

    def text_matches(s: str) -> bool:
        if not s:
            return False
        lo = s.lower()
        return any(t in lo for t in terms)

    def relevant_task(t: dict) -> dict | None:
        text = " ".join(
            filter(
                None,
                [
                    t.get("task", ""),
                    " ".join(t.get("subtasks") or []),
                    " ".join(t.get("blockers") or []),
                    t.get("solution", "") or "",
                    " ".join(t.get("related_to") or []),
                ],
            )
        )
        if not text_matches(text):
            return None
        # Trim subtasks/blockers to first 6, drop files_touched (verbose), keep concise
        return {
            "task": t.get("task"),
            "status": t.get("status"),
            "subtasks": (t.get("subtasks") or [])[:6],
            "blockers": (t.get("blockers") or [])[:4],
            "solution": (t.get("solution") or "")[:300] or None,
            "files_touched": (t.get("files_touched") or [])[:4] or None,
        }

    def relevant_decision(d: dict) -> dict | None:
        text = " ".join(
            filter(
                None,
                [
                    d.get("decision", ""),
                    d.get("rationale", ""),
                    d.get("alternative_rejected", "") or "",
                ],
            )
        )
        if not text_matches(text):
            return None
        return {
            "decision": d.get("decision"),
            "rationale": (d.get("rationale") or "")[:250],
            "alternative_rejected": (d.get("alternative_rejected") or "")[:200] or None,
        }

    def relevant_concept(c2: dict) -> dict | None:
        text = " ".join(
            filter(
                None,
                [
                    c2.get("term", ""),
                    c2.get("definition", ""),
                    " ".join(c2.get("aliases") or []),
                ],
            )
        )
        if not text_matches(text):
            return None
        return {
            "term": c2.get("term"),
            "definition": (c2.get("definition") or "")[:200],
        }

    def relevant_bs(bs: dict) -> dict | None:
        text = " ".join(
            filter(None, [bs.get("blocker", ""), bs.get("solution", "") or ""])
        )
        if not text_matches(text):
            return None
        return {
            "blocker": (bs.get("blocker") or "")[:200],
            "solution": (bs.get("solution") or "")[:200] or None,
        }

    tasks = [t for t in (relevant_task(t) for t in c.get("tasks") or []) if t]
    decisions = [
        d for d in (relevant_decision(d) for d in c.get("decisions") or []) if d
    ]
    concepts = [k for k in (relevant_concept(k) for k in c.get("concepts") or []) if k]
    bs = [b for b in (relevant_bs(b) for b in c.get("blockers_solutions") or []) if b]
    # For quotes, include only if they match (or if other relevant fields exist)
    quotes_all = c.get("notable_quotes") or []
    quotes = [q for q in quotes_all if text_matches(q)][:3]
    if not (tasks or decisions or concepts or bs or quotes):
        # Fall back: this chunk hit the theme via some field we filtered out
        # (e.g. a related_to tag). Skip it.
        return {}

    out = {
        "_id": f"{c.get('doc_slug', '?')}/{c.get('chunk_id', '?')}",
        "heading": c.get("heading"),
        "dates": c.get("dates_found") or None,
    }
    if tasks:
        out["tasks"] = tasks
    if decisions:
        out["decisions"] = decisions
    if concepts:
        out["concepts"] = concepts
    if bs:
        out["blockers_solutions"] = bs
    if quotes:
        out["notable_quotes"] = quotes

    rendered = yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=120)
    if len(rendered) > char_budget:
        # Hard truncate the rendered text and append "... <truncated>"
        rendered = rendered[:char_budget] + "\n  # ... <truncated>\n"
    return {"_rendered": rendered}
