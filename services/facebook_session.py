"""Парсинг и проверка cookies Facebook."""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

_FB_REQUIRED = ("c_user", "xs")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def parse_cookies(raw: str) -> dict[str, str]:
    """Строка Cookie / JSON / Netscape → dict."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустая строка cookies")

    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("JSON cookies должен быть объектом")
        return {str(k).strip(): str(v).strip() for k, v in data.items() if str(v).strip()}

    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        key, val = key.strip(), val.strip()
        if key and val:
            out[key] = val
    if not out:
        raise ValueError("Не удалось разобрать cookies")
    return out


def validate_cookies(cookies: dict[str, str]) -> None:
    missing = [k for k in _FB_REQUIRED if not (cookies.get(k) or "").strip()]
    if missing:
        raise ValueError(f"Нет обязательных cookies: {', '.join(missing)}")


def cookies_to_json(cookies: dict[str, str]) -> str:
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def cookies_from_json(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


async def check_session(
    cookies: dict[str, str],
    *,
    proxy_url: str | None = None,
    timeout_sec: float = 25.0,
) -> tuple[bool, str]:
    """GET /marketplace/ — проверка, что сессия живая."""
    validate_cookies(cookies)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookies_to_header(cookies),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://www.facebook.com/marketplace/",
                headers=headers,
                proxy=proxy_url,
                allow_redirects=True,
            ) as resp:
                body = (await resp.text())[:8000].lower()
                if resp.status >= 400:
                    return False, f"HTTP {resp.status}"
                if "login" in str(resp.url).lower() and "marketplace" not in str(resp.url).lower():
                    return False, "Редирект на логин — cookies устарели"
                if "checkpoint" in body or "security check" in body:
                    return False, "Facebook просит проверку (checkpoint)"
                if "marketplace" in body or resp.status == 200:
                    return True, "OK"
                return False, "Не похоже на Marketplace (обнови cookies)"
    except Exception as e:
        return False, str(e)[:200]
