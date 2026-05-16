"""Имя продавца из JSON парсера — правила для валидации email."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Слова короче 4 букв не участвуют в подстановке доменов.
MIN_NAME_TOKEN_LEN = 4


def seller_name_from_item(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("item_person_name")
        or item.get("person_name")
        or item.get("name")
        or item.get("seller")
        or ""
    ).strip()


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_seller_name(raw: str) -> str:
    if not raw:
        return ""
    s = " ".join(str(raw).strip().split())
    s = s.replace("ß", "ss").replace("ẞ", "SS")
    s = _strip_accents(s)
    return s.replace("'", "'").replace("`", "'")


def pick_name_tokens(name: str, *, min_len: int = MIN_NAME_TOKEN_LEN) -> list[str]:
    """Буквенные части имени (каждая >= min_len символов, только буквы)."""
    s = normalize_seller_name(name)
    if not s:
        return []

    s2 = re.sub(r"[^A-Za-z0-9.\s'\-]", " ", s)
    parts = re.split(r"[\s\-']+", s2.strip())

    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = p.strip(".")
        if len(p) >= min_len and p.isalpha():
            pl = p.lower()
            if pl not in seen:
                seen.add(pl)
                out.append(pl)
    return out


def _is_handle_token(h: str, *, min_len: int = MIN_NAME_TOKEN_LEN) -> bool:
    """Ник вида alinafor20: латиница+цифры, >= min_len, есть хотя бы одна буква."""
    if len(h) < min_len or len(h) > 64:
        return False
    if not h.isalnum():
        return False
    return any(c.isalpha() for c in h)


def pick_handle_locals(name: str, *, min_len: int = MIN_NAME_TOKEN_LEN) -> list[str]:
    """
    Никнеймы из профиля: alinafor20, coolguy99 и т.п.
    — одно слово в имени или часть с цифрами (не короче min_len).
    """
    s = normalize_seller_name(name)
    if not s:
        return []

    parts = [p for p in re.split(r"[\s\-']+", s) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    single_part = len(parts) <= 1

    for p in parts:
        h = re.sub(r"[^A-Za-z0-9]", "", p)
        if not _is_handle_token(h, min_len=min_len):
            continue
        if single_part or any(c.isdigit() for c in h):
            hl = h.lower()
            if hl not in seen:
                seen.add(hl)
                out.append(hl)
    return out


def seller_name_eligible_for_validation(name: str, *, min_token_len: int = MIN_NAME_TOKEN_LEN) -> bool:
    """Имя подходит для имя@домен: слово из букв >=4 или ник (alinafor20)."""
    return bool(
        pick_name_tokens(name, min_len=min_token_len)
        or pick_handle_locals(name, min_len=min_token_len)
    )
