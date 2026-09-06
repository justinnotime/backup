"""Stable text-oriented fingerprint; presentation and image bytes are excluded."""

from __future__ import annotations

import re
import unicodedata
from hashlib import sha256


def canonical(text: str) -> str:
    t = re.sub("^---\\n.*?\\n---\\n", "", text, flags=re.S)
    t = re.sub("<!--.*?-->", "", t, flags=re.S)
    t = re.sub("^[ \\t]*(?:`{3,}|~{3,})[^\\r\\n]*$", "", t, flags=re.M)
    t = re.sub(
        "^\\s*\\[[^\\]]+\\]:\\s*<?(?:data:|https?:|attachments/)[^\\n]*$",
        "",
        t,
        flags=re.M,
    )
    t = re.sub("data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "", t)
    t = re.sub("</?[a-zA-Z][^>]*>", "", t)
    t = re.sub("!\\[[^\\]]*\\](?:\\([^)]*\\)|\\[[^\\]]*\\])", "", t)
    t = re.sub("\\]\\([^)]*\\)", "]", t)
    t = t.replace("\\", "")
    t = re.sub("[\\*_`#|:\\-\\[\\]()<>]", "", t)
    t = re.sub("\\s+", "", t)
    return unicodedata.normalize("NFKC", t)


def fingerprint(text: str, length: int = 32) -> str:
    return "sha256:" + sha256(canonical(text).encode()).hexdigest()[:length]


def self_test():
    assert canonical("[ordinary words] remain") == canonical("ordinary words remain")
    assert canonical("short <5s clip") == "short5sclip"
    assert canonical("```python\nprint(1)\n```\n") == canonical(
        "```py\nprint(1)\n```\n"
    )
    assert fingerprint("value 1") != fingerprint("value 2")
    return 0


def main(argv=None):
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
    args = parser.parse_args(argv)
    if args.source is None:
        self_test()
        print("OK fingerprint self-test")
    else:
        print(fingerprint(args.source.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
