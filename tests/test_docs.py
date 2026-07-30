from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
IGNORED_PARTS = {".git", ".venv", "node_modules"}


def test_local_markdown_links_resolve():
    failures: list[str] = []
    for document in ROOT.rglob("*.md"):
        if IGNORED_PARTS.intersection(document.relative_to(ROOT).parts):
            continue
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            linked = (document.parent / unquote(parsed.path)).resolve()
            if not linked.exists():
                failures.append(
                    f"{document.relative_to(ROOT)} -> {raw_target} "
                    f"(missing {linked.relative_to(ROOT)})"
                )

    assert not failures, "broken local Markdown links:\n" + "\n".join(failures)
