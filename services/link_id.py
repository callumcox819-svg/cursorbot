"""Extract public link/ad ID from generated GAG/GOO URLs."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_DIGIT_SEGMENT_RE = re.compile(r"(?:^|/)(\d{5,})(?:/|$|\?|#)")


def link_id_from_generated_url(url: str | None) -> str | None:
    """
    Best-effort: numeric id from generated link path or query.
    Example: https://example.com/buy/210743034 -> 210743034
    """
    u = (url or "").strip()
    if not u:
        return None

    try:
        parsed = urlparse(u)
    except Exception:
        return None

    parts = [p for p in (parsed.path or "").split("/") if p]
    for seg in reversed(parts):
        if seg.isdigit() and len(seg) >= 5:
            return seg

    try:
        qs = parse_qs(parsed.query or "")
        for key in ("id", "adId", "ad_id", "order_id", "orderId"):
            vals = qs.get(key) or []
            if vals and str(vals[0]).strip().isdigit():
                return str(vals[0]).strip()
    except Exception:
        pass

    m = _DIGIT_SEGMENT_RE.search(u)
    if m:
        return m.group(1)
    return None
