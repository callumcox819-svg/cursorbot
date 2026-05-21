"""ASCII для plain-text рассылки: 7bit CTE и меньше «кракозябр» в заголовках."""

from __future__ import annotations

import os
import unicodedata


def fold_to_ascii(text: str) -> str:
    """é→e, ü→u, «» убираются — остаётся латиница для 7bit."""
    s = text or ""
    if not s:
        return s
    nfkd = unicodedata.normalize("NFKD", s)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def mail_fold_ascii_enabled() -> bool:
    raw = os.getenv("MAIL_FOLD_ASCII", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def fold_plain_mail_text(text: str) -> str:
    """По умолчанию вкл.: рассылка без диакритики → Content-Transfer-Encoding: 7bit."""
    if not mail_fold_ascii_enabled():
        return text or ""
    return fold_to_ascii(text or "")
