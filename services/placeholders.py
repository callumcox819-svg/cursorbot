from __future__ import annotations

import re
from typing import Any, Optional

# Supported placeholders (case-insensitive, with optional spaces):
# {{LINK}}, {{ITEM_TITLE}}, {{PRICE}}, {{BUYER_NAME}}, {{ADDRESS}}, {{IMAGE_URL}}, {{IMAGE}}
LINK_PLACEHOLDER_RE = re.compile(r"\{\{\s*LINK\s*\}\}", re.I)
GEN_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}", re.I)


def apply_placeholders(text: str, link: str = "", ctx: Optional[dict[str, Any]] = None) -> str:
    """
    Apply placeholders inside HTML or plain text templates.

    - Always replaces {{LINK}} if link provided.
    - If ctx provided, replaces keys like {{ITEM_TITLE}}, {{PRICE}}, {{BUYER_NAME}}, {{ADDRESS}}, {{IMAGE_URL}}.
    - {{IMAGE}} expands into an <img ...> tag using ctx["IMAGE_URL"] (email-safe inline styling).
    """
    if not text:
        return ""

    out = text

    if link:
        out = LINK_PLACEHOLDER_RE.sub(str(link), out)

    if not ctx:
        return out

    def _img_tag(url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        return (
            f'<img src="{u}" alt="" '
            'style="display:block;width:100%;max-width:160px;height:auto;border-radius:6px;border:0;" />'
        )

    def _repl(m: re.Match) -> str:
        key = (m.group(1) or "").strip().upper()
        if not key:
            return m.group(0)
        if key == "IMAGE":
            return _img_tag(str(ctx.get("IMAGE_URL", "") or ""))
        val = ctx.get(key, "")
        return "" if val is None else str(val)

    return GEN_PLACEHOLDER_RE.sub(_repl, out)
