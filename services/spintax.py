from __future__ import annotations

import random


def expand_spintax(text: str, *, max_passes: int = 30) -> str:
    """Expand simple spintax like: {Hi|Hello}.

    Supports nesting: {Hi|{Hello|Hey}}.
    """
    if not text:
        return ""

    s = str(text)
    for _ in range(max_passes):
        start = s.rfind("{")
        if start == -1:
            break
        end = s.find("}", start)
        if end == -1:
            break
        inner = s[start + 1 : end]
        # allow empty options, filter only completely empty?
        options = inner.split("|")
        choice = random.choice(options) if options else ""
        s = s[:start] + choice + s[end + 1 :]

    return s
