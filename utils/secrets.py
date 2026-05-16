"""Helpers for working with secrets copied from Telegram.

Telegram clients sometimes insert invisible / zero-width characters when a user
copies a key or profile id. We also want to be resilient to accidental spaces
and newlines.
"""

from __future__ import annotations

import re


_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def clean_secret(value: str | None) -> str:
    """Normalize a secret/key/profile id.

    - Removes zero-width characters.
    - Removes all whitespace (spaces/newlines/tabs).

    Returns an empty string for None/empty input.
    """

    v = (value or "")
    v = _ZERO_WIDTH_RE.sub("", v)
    v = "".join(v.split())
    return v
