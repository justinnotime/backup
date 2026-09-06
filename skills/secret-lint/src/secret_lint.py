"""Local credential-shape detection and length-preserving text redaction.

The low-level scan API contains matches for internal transformation only. CLI
reports deliberately contain locations and categories, never matched values.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

SHAPES = [
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("openai-anthropic-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("provider-api-key", re.compile(r"\b(?:gsk-|glpat-|ya29\.)[A-Za-z0-9_.-]{20,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("azure-account-key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{60,}")),
    (
        "sas-or-sig-param",
        re.compile(r"[?&](?:sig|sas|X-Amz-Signature)=[A-Za-z0-9%+/=_-]{30,}", re.IGNORECASE),
    ),
    ("url-embedded-cred", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]{1,64}:[^/\s:@]{4,}@[^\s/]+")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b")),
    # base64-wrapped PEM ("LS0tLS1..." = "-----...") and base64 DER cert/key
    # ("MII..." = 0x30 0x82 SEQUENCE) — kubeconfig client-key-data /
    # certificate-authority-data blobs land as bare base64, not a "-----BEGIN"
    # block, so the PEM-block detector never sees them.
    (
        "base64-pem-or-der",
        re.compile(r"\b(?:LS0tLS[A-Za-z0-9+/]{40,}|MII[A-Za-z0-9+/]{100,})={0,2}"),
    ),
]
HARD_DETECTORS = {n for n, _ in SHAPES} | {
    "pem-private-key",
    "private-ssh-block",
    "secretish-query-param",
    "secretish-kv",
    "authorization-token",
    "session-cookie",
}

AUTHORIZATION_RE = re.compile(
    r"(?i)\b(?:authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+|bearer\s+)"
    r"(?P<value>[A-Za-z0-9._~+/-]{8,}={0,2})"
)
COOKIE_RE = re.compile(r"(?i)\b(?:set-cookie|cookie)[\"']?\s*[:=]\s*[\"']?(?P<value>[^\r\n\"']+)")
COOKIE_VALUE_RE = re.compile(r"(?:^|;\s*)[^=;\s]+=(?P<value>[^;\s]{8,})")

PEM_BLOCK_RE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----(.*?)-----END \1-----", re.DOTALL
)

# key:value / key=value where the KEY names a credential. Masks the WHOLE
# value, so a secret containing characters outside the base64 charset (Azure
# client secrets carry '~', markdown-escaped as '\~') is masked as one unit
# instead of leaking the segments the generic candidate regex splits off.
KV_RE = re.compile(
    r'(?i)"?(?:client[_-]?secret|password|passwd|pwd|secret|api[_-]?key|'
    r"access[_-]?key|secret[_-]?key|account[_-]?key|token|sas[_-]?token|"
    r"connection[_-]?string|private[_-]?key|auth[_-]?token|bearer|"
    # kubeconfig / TLS material: the value is base64 cert/key data
    r"client[_-]?key[_-]?data|client[_-]?certificate[_-]?data|"
    r"certificate[_-]?authority[_-]?data|ca[_.-]?crt|tls[_.-]?(?:key|crt|cert)|"
    r'refresh[_-]?token|access[_-]?token|id[_-]?token)"?'
    r"\s*[:=]\s*"
    r'(?P<q>["\'])?(?P<val>(?(q)[^"\']+|[^\s,}{&]+))'
)

CONTEXT = re.compile(
    r"(?i)(secret|token|passwd|password|pwd|credential|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|[a-z0-9]*[_-]?key\b|psk|pre[_-]?shared|"
    r"connection[_-]?str|conn[_-]?str|redis|sas\b|cert|certificate|"
    r"auth|bearer|cookie|密码|密钥|凭证|私钥)"
)


def has_context(line: str) -> bool:
    """Secret-context test, tolerant of markdown-escaped separators
    (Google's export writes `API\\_KEY`, which would defeat a plain match)."""
    return bool(CONTEXT.search(line.replace("\\", "")))


CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.)[^\s)>\]\"']+", re.IGNORECASE)
PATH_RE = re.compile(
    r"(?:\.{0,2}|~)?/?[A-Za-z0-9+=_.%~@-]+(?:/[A-Za-z0-9+=_.%~@-]+){2,}/?"
    r"|(?:\.{0,2}|~)/[A-Za-z0-9+=_.%~@-]+(?:/[A-Za-z0-9+=_.%~@-]+)*/?"
)
URLPATH_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_&?%.~@:#-")
LEADING_RUN = re.compile(r"^[A-Za-z0-9+/=_&?%.~@:#-]+")
LOG_LINE_RE = re.compile(
    r"^\[?(?:"
    r"\d{2,4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}"
    r"|\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
    r"|[IWEF]\d{4} \d{2}:\d{2}:\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} \d{2}:\d{2}:\d{2}"
    r"|(?:INFO|WARN(?:ING)?|ERROR|DEBUG|TRACE|FATAL|CRITICAL|NOTICE)\b[: \]]"
    r")"
)
UUID_ANY = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
RUN_RE = re.compile(r"[A-Za-z0-9+=_-]+")
WORDISH_SEG = re.compile(
    r"^(?:[A-Za-z][a-z0-9]{0,11}|[A-Z]{2,10}|[0-9]{1,8}"
    r"|[0-9]{1,4}[xX]?[0-9]{0,3}[a-zA-Z]{1,4})$"
)
# Only credential-bearing query parameters are selected. Expiry, permissions
# and API-version fields are ordinary metadata.
SECRETISH_PARAMS = {
    "sig",
    "sas",
    "key",
    "token",
    "secret",
    "password",
    "apikey",
    "api_key",
    "access_token",
    "auth",
    "code",
    "x-amz-signature",
}
PLACEHOLDER_RE = re.compile(
    r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|<[^>]*>|\{\{?[^}]*\}\}?"
    r"|[A-Z][A-Z0-9_-]{2,}"
    r"|(?i:your|my)[-_][A-Za-z0-9_-]+"
    r"|(?i:x{3,}|\*{3,}|\.{3,}|placeholder|changeme|redacted|dummy|example))$"
)


def shannon(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def is_placeholder(v):
    return bool(PLACEHOLDER_RE.match(v.strip()))


def cred_password(url_cred):
    userinfo = url_cred.split("://", 1)[-1].rsplit("@", 1)[0]
    return userinfo.partition(":")[2]


def wordish_chain(tok):
    segs = [s for s in re.split(r"[-_]", tok) if s]
    return len(segs) >= 2 and all(WORDISH_SEG.match(s) for s in segs)


def classify_candidate(tok, line):
    """(tier, token) for a generic candidate, or None. tier in {ctx, heur}."""
    # JSON/YAML KEY position ("prefThumbprint": null): the token names a
    # field, the secret (if any) is the value and is caught by KV_RE / the
    # generic pass on the value side. Never mask a key name.
    if re.search(r'"' + re.escape(tok) + r'"\s*:', line):
        return None
    # identifier row: a bare name column directly followed by a UUID column
    # (e.g. `az ad sp` output: <displayName> <appId-uuid> <objectId-uuid>).
    # The name is not a secret; the UUIDs are already exempt.
    if re.search(re.escape(tok) + r"\s+[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", line):
        return None
    m = re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,30}=(?!=)(.+)$", tok)
    if m:
        tok = m.group(1)
        if len(tok) < 24:
            return None
    if "/" in tok:
        a, _, b = tok.partition("/")
        if wordish_chain(a) or wordish_chain(b) or WORDISH_SEG.match(a) or WORDISH_SEG.match(b):
            return None
        run = max(RUN_RE.findall(tok), key=len, default="")
        if len(run) < 24:
            return None
        tok = run
    mu = UUID_ANY.search(tok)
    if mu:
        rest = (tok[: mu.start()] + tok[mu.end() :]).strip("-_")
        if not rest or WORDISH_SEG.match(rest) or wordish_chain(rest):
            return None
    if wordish_chain(tok):
        return None
    if HEX_RE.match(tok):
        if len(tok) in (32, 40, 64) and not has_context(line):
            return None
        if shannon(tok) < 3.4:
            return None
        return ("ctx", tok) if has_context(line) else None
    letters = [c for c in tok if c.isalpha()]
    if letters and not any(c.isupper() for c in tok):
        if sum(c in "aeiou" for c in letters) / len(letters) >= 0.28 and len(letters) >= 0.6 * len(
            tok
        ):
            return None
        if shannon(tok) < 4.55:
            return None
    ent = shannon(tok)
    has_ctx = has_context(line)
    if ent >= 4.4 or (ent >= 3.9 and has_ctx):
        return ("ctx" if has_ctx else "heur", tok)
    return None


def url_spans_and_hits(line):
    spans, hits = [], []
    for m in URL_RE.finditer(line):
        url = m.group(0)
        try:
            query = urllib.parse.urlsplit(url).query
        except ValueError:
            query = ""
        for item in query.split("&"):
            key, separator, encoded = item.partition("=")
            if not separator:
                continue
            k, v = urllib.parse.unquote_plus(key), urllib.parse.unquote_plus(encoded)
            if k.lower() in SECRETISH_PARAMS and len(v) >= 20 and not is_placeholder(v):
                hits.append(("secretish-query-param", f"{k}={v}", encoded))
        spans.append((m.start(), m.end()))
    return spans, hits


def scan_line(line, prev_open):
    """Yield findings (detector, tier, value, target) for one line; also
    return whether a URL/path chain stays open into the next line.
    `target` is the exact substring a masker should replace."""
    out = []
    # Exclude inline image payloads without hiding unrelated keys on that line.
    line = re.sub(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "", line)
    found_here = set()
    for match in AUTHORIZATION_RE.finditer(line):
        value = match.group("value")
        if not is_placeholder(value):
            out.append(("authorization-token", "hard", value, value))
            found_here.add(match.span("value"))
    for header in COOKIE_RE.finditer(line):
        for match in COOKIE_VALUE_RE.finditer(header.group("value")):
            value = match.group("value")
            if not is_placeholder(value):
                out.append(("session-cookie", "hard", value, value))
    for name, rx in SHAPES:
        for m in rx.finditer(line):
            v = m.group(0)
            if name == "url-embedded-cred":
                pw = cred_password(v)
                if is_placeholder(pw):
                    continue
                target = pw
            elif name == "azure-account-key" or name == "sas-or-sig-param":
                target = v.split("=", 1)[1]
            else:
                target = v
            out.append((name, "hard", v, target))
            found_here.add((m.start(), m.end()))

    for m in KV_RE.finditer(line):
        val = m.group("val")
        if len(val) >= 8 and not is_placeholder(val):
            out.append(("secretish-kv", "hard", val, val))
            found_here.add((m.start("val"), m.end("val")))

    spans, qhits = url_spans_and_hits(line)
    for m in PATH_RE.finditer(line):
        spans.append((m.start(), m.end()))
    if prev_open:
        m = LEADING_RUN.match(line)
        if m:
            spans.append((0, m.end()))
    for name, value, target in qhits:
        out.append((name, "hard", value, target))

    if not LOG_LINE_RE.match(line.lstrip(" \t>|*")):
        for m in CANDIDATE.finditer(line):
            if any(s <= m.start() < e for s, e in found_here):
                continue
            if any(s <= m.start() < e for s, e in spans):
                continue
            r = classify_candidate(m.group(0), line)
            if r:
                tier, tok = r
                out.append(
                    ("high-entropy-token" + ("+context" if tier == "ctx" else ""), tier, tok, tok)
                )

    prev_open_next = (
        bool(line) and line[-1] in URLPATH_CHARS and any(e >= len(line) for _, e in spans)
    )
    return out, prev_open_next


def scan_text(text):
    """Yield (line_no, detector, tier, value, target) for a whole document,
    with PEM/OpenSSH private-key blocks reported as their own finding."""
    findings = []
    for m in PEM_BLOCK_RE.finditer(text):
        kind = m.group(1)
        det = "private-ssh-block" if "OPENSSH" in kind else "pem-private-key"
        ln = text.count("\n", 0, m.start()) + 1
        findings.append((ln, det, "hard", m.group(0)[:40] + "...", None))
    prev_open = False
    for ln, line in enumerate(text.splitlines(), 1):
        line_findings, prev_open = scan_line(line, prev_open)
        for det, tier, value, target in line_findings:
            findings.append((ln, det, tier, value, target))
    return findings


# --- masking -----------------------------------------------------------------


def mask_value(s):
    n = len(s)
    if n <= 6:
        h = t = 0
    elif n <= 12:
        h = t = 1
    elif n <= 24:
        h = t = 2
    else:
        h = t = 4
    while h + t >= n and (h or t):
        if t >= h:
            t -= 1
        else:
            h -= 1
    return s[:h] + "*" * (n - h - t) + (s[-t:] if t else "")


def mask_pem_body(match):
    head = f"-----BEGIN {match.group(1)}-----"
    tail = f"-----END {match.group(1)}-----"
    body = match.group(2)
    masked = "".join("*" if c not in "\r\n" else c for c in body)
    return head + masked + tail


def mask_text(text, tiers):
    """Return (masked_text, n_spans). tiers is a set drawn from
    {hard, ctx, heur}."""
    if not tiers or not set(tiers) <= {"hard", "ctx", "heur"}:
        raise ValueError("redaction tiers must select hard, ctx and/or heur")
    n = 0
    if "hard" in tiers:
        text, k = PEM_BLOCK_RE.subn(mask_pem_body, text)
        n += k
    # per-line targeted replacement for everything else
    by_line = defaultdict(list)
    identified = set()  # every secret value found anywhere in the doc
    lines = text.splitlines(keepends=True)
    # recompute line findings on the (possibly PEM-masked) text
    plain_prev = False
    for i, raw in enumerate(lines):
        line = raw.rstrip("\n").rstrip("\r")
        line_findings, plain_prev = scan_line(line, plain_prev)
        for det, tier, value, target in line_findings:
            if tier in tiers and target and target in line:
                by_line[i].append(target)
                identified.add(target)
    out = []
    for i, raw in enumerate(lines):
        line = raw
        # Per-line finds PLUS any secret value identified elsewhere in the
        # doc: the same secret often appears both under a labeled key (caught
        # by context) and in a bare `value: "..."` line (no context) — once
        # a value is known to be a secret, redact every occurrence.
        targets = set(by_line.get(i, []))
        for v in identified:
            if v in line:
                targets.add(v)
        for target in sorted(targets, key=len, reverse=True):
            masked = mask_value(target)
            # skip already-masked values (fixed point): keeps re-masking a
            # true no-op, so counts and idempotency hold across runs.
            if masked != target and target in line:
                line = line.replace(target, masked)
                n += 1
        out.append(line)
    return "".join(out), n


def parse_tiers(value):
    tiers = {item.strip() for item in value.split(",") if item.strip()}
    if not tiers or not tiers <= {"hard", "ctx", "heur"}:
        raise argparse.ArgumentTypeError("tiers must select hard, ctx and/or heur")
    return tiers


def label(path: Path) -> str:
    """A filename can itself contain a credential-shaped value."""
    return mask_text(str(path), {"hard", "ctx", "heur"})[0]


def input_files(inputs, issues, *, markdown_only=False):
    """Select files including hidden/untracked content, but never Git metadata."""
    seen = set()
    for value in inputs:
        path = Path(value).expanduser().absolute()
        if ".git" in path.parts:
            issues.append(
                {"file": label(path), "reason": "Git metadata is outside working-file scope"}
            )
            continue
        if path.is_symlink():
            issues.append({"file": label(path), "reason": "symlink not followed"})
            continue
        if not path.exists():
            issues.append({"file": label(path), "reason": "input does not exist"})
            continue
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = []

            def walk_error(error, path=path):
                issues.append(
                    {"file": label(Path(error.filename or path)), "reason": "directory unreadable"}
                )

            for current, directories, names in os.walk(path, followlinks=False, onerror=walk_error):
                for directory in directories[:]:
                    child = Path(current) / directory
                    if directory == ".git":
                        directories.remove(directory)
                    elif child.is_symlink():
                        directories.remove(directory)
                        issues.append({"file": label(child), "reason": "symlink not followed"})
                for name in names:
                    if name != ".git" and (not markdown_only or name.endswith(".md")):
                        candidates.append(Path(current) / name)
        else:
            issues.append(
                {"file": label(path), "reason": "input is not a regular file or directory"}
            )
            continue
        for candidate in sorted(candidates):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_symlink() or not candidate.is_file():
                issues.append(
                    {"file": label(candidate), "reason": "symlink or special file not read"}
                )
                continue
            yield candidate


def read_text(path, issues):
    try:
        data = path.read_bytes()
        if b"\0" in data:
            issues.append({"file": label(path), "reason": "binary content is not supported"})
            return None
        return data.decode("utf-8")
    except UnicodeError:
        issues.append({"file": label(path), "reason": "content is not UTF-8 text"})
    except OSError:
        issues.append({"file": label(path), "reason": "file unreadable"})
    return None


def inspect_inputs(inputs, tiers):
    findings, issues, scanned = [], [], 0
    for path in input_files(inputs, issues):
        text = read_text(path, issues)
        if text is None:
            continue
        scanned += 1
        for line, detector, tier, _value, _target in scan_text(text):
            if tier in tiers:
                findings.append(
                    {"file": label(path), "line": line, "category": detector, "tier": tier}
                )
    return {
        "schema": "secret-lint-report/v1",
        "scanned_files": scanned,
        "findings": findings,
        "incomplete": issues,
        "scope": "UTF-8 working files; Git metadata and history excluded",
    }


def atomic_write(path, text, *, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".secret-lint-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_command(args):
    report = inspect_inputs(args.paths, args.tier)
    if args.output:
        atomic_write(Path(args.output).expanduser(), json.dumps(report, indent=2) + "\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for finding in report["findings"]:
            print(
                f"CANDIDATE {finding['file']}:{finding['line']} {finding['category']} ({finding['tier']})"
            )
        for issue in report["incomplete"]:
            print(f"INCOMPLETE {issue['file']}: {issue['reason']}")
        print(
            f"NOTE scanned {report['scanned_files']} files; {len(report['findings'])} candidates; "
            f"{len(report['incomplete'])} incomplete inputs"
        )
    return 2 if report["incomplete"] else (1 if report["findings"] else 0)


def mask_command(args):
    issues, writes = [], 0
    # Preserve directory-mask selection: only Markdown, unless individual text
    # files are explicitly named. Scanning uses all UTF-8 files independently.
    for path in input_files(args.paths, issues, markdown_only=True):
        original = read_text(path, issues)
        if original is None:
            continue
        masked, count = mask_text(original, args.tier)
        if masked == original and not args.write_unchanged:
            continue
        destination = path if args.in_place else path.with_suffix(path.suffix + ".masked")
        if destination.is_symlink():
            issues.append({"file": label(destination), "reason": "output symlink refused"})
            continue
        mode = stat.S_IMODE(path.stat().st_mode) if args.in_place else 0o600
        atomic_write(destination, masked, mode=mode)
        writes += 1
        print(f"NOTE masked {count} spans: {label(destination)}")
    for issue in issues:
        print(f"INCOMPLETE {issue['file']}: {issue['reason']}", file=sys.stderr)
    print(f"NOTE wrote {writes} masked files")
    return 2 if issues else 0


def redact_command(args):
    try:
        original = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeError:
        print("FAIL: stdin must contain UTF-8 text", file=sys.stderr)
        return 2
    masked, count = mask_text(original, args.tier)
    if args.json:
        print(json.dumps({"text": masked, "replacements": count}, ensure_ascii=False))
    else:
        sys.stdout.write(masked)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser(
        "check", aliases=["report"], help="report candidate locations without matched values"
    )
    check.add_argument("paths", nargs="+")
    check.add_argument("--tier", type=parse_tiers, default={"hard", "ctx", "heur"})
    check.add_argument("--json", action="store_true")
    check.add_argument("--output", help="optional JSON report; never contains matched values")
    check.set_defaults(function=check_command)
    mask = modes.add_parser(
        "mask", help="write redacted copies or explicitly replace selected files"
    )
    mask.add_argument("paths", nargs="+")
    mask.add_argument("--tier", type=parse_tiers, default={"hard", "ctx", "heur"})
    mask.add_argument("--in-place", action="store_true")
    mask.add_argument("--write-unchanged", action="store_true")
    mask.set_defaults(function=mask_command)
    redact = modes.add_parser("redact", help="transform UTF-8 stdin to redacted stdout")
    redact.add_argument("--tier", type=parse_tiers, default={"hard", "ctx", "heur"})
    redact.add_argument("--json", action="store_true")
    redact.set_defaults(function=redact_command)
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except (OSError, ValueError):
        print("FAIL: selected file operation could not complete", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
