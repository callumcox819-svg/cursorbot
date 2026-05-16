from __future__ import annotations

import aiohttp
from typing import Tuple, Optional, List
from urllib.parse import urlparse
from pathlib import Path

API_BASE = "https://api.goo.network"

# Per-team API domains.
# IMPORTANT: we intentionally do not rely on config.py here.
TEAM_API_BASE = {
    # AQUA uses a legacy domain.
    "AQUA": "https://api-old.goo.network",
}

TEST_LINKS_PATH = Path("data/test_links.txt")


def _pick_api_base(team_key: str) -> str:
    """Choose API base URL depending on selected team/command."""
    if not team_key:
        return API_BASE
    return TEAM_API_BASE.get(str(team_key).strip().upper(), API_BASE)


def _host_from_base(api_base: str) -> str:
    try:
        return urlparse(api_base).netloc or "api.goo.network"
    except Exception:
        return "api.goo.network"


def _load_test_links() -> List[str]:
    """Read data/test_links.txt (if present)."""
    try:
        if not TEST_LINKS_PATH.exists():
            return []
        lines = [x.strip() for x in TEST_LINKS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()]
        # filter only kleinanzeigen links
        out = []
        for x in lines:
            if not x:
                continue
            lx = x.lower()
            if lx.startswith("http") and "kleinanzeigen.de" in lx:
                out.append(x)
        return out
    except Exception:
        return []


def _is_test_url(url: str, test_links: List[str]) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    # точное совпадение — чтобы НЕ влиять на реальные объявления
    return u in test_links


def _looks_like_parse_error(msg: str) -> bool:
    m = (msg or "").lower()
    return ("unknown error while parsing" in m) or ("parsing the ad" in m)


class GooError(Exception):
    pass


async def _call_api_once(
    *,
    api_base: str,
    user_api_key: str,
    team_api_key: str,
    profile_id: str,
    service: str,
    url: str,
    need_balance_checker: bool,
    timeout_s: int,
) -> Tuple[bool, str]:
    endpoint = f"{api_base}/api/generate/single/parse"
    headers = {
        "Authorization": f"Apikey {user_api_key}",
        "X-Team-Key": team_api_key,
        "Host": _host_from_base(api_base),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "service": service,
        "url": url,
        "isNeedBalanceChecker": bool(need_balance_checker),
        "profileID": profile_id,
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            async with session.post(endpoint, headers=headers, json=payload) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    status = data.get("status")
                    msg = data.get("message") or str(data)

                    if resp.status == 200 and status is True:
                        return True, str(msg).strip()

                    # Some errors use statusCode
                    if data.get("statusCode") is False and msg:
                        return False, str(msg).strip()

                    return False, str(msg).strip()

                return False, f"Unexpected response: {data}"
    except Exception as e:
        return False, f"HTTP error: {e}"


async def generate_single_parse(
    *,
    user_api_key: str,
    team_api_key: str,
    team_name: str,
    profile_id: str,
    service: str,
    url: str,
    need_balance_checker: bool = False,
    timeout_s: int = 25,
) -> Tuple[bool, str]:
    """
    Generate a link via GOO.NETWORK API (single/parse).
    Returns (ok, message_or_link).

    IMPORTANT FIX (TEST MAILS):
    - If the passed url is one of data/test_links.txt and parsing fails,
      we automatically try the other test links until success.
    - This makes BOTH "Создать ссылку" and auto-reply behave correctly
      WITHOUT changing any handlers/UI/callbacks.
    """
    user_api_key = (user_api_key or "").strip()
    team_api_key = (team_api_key or "").strip()
    profile_id = (profile_id or "").strip()
    service = (service or "").strip()
    url = (url or "").strip()

    if not user_api_key or not team_api_key or not profile_id or not service or not url:
        return False, "missing_keys"

    api_base = _pick_api_base(team_name)

    # --- primary attempt ---
    ok, msg = await _call_api_once(
        api_base=api_base,
        user_api_key=user_api_key,
        team_api_key=team_api_key,
        profile_id=profile_id,
        service=service,
        url=url,
        need_balance_checker=need_balance_checker,
        timeout_s=timeout_s,
    )

    if ok:
        return True, msg

    # --- TEST FALLBACK: only if url is in data/test_links.txt ---
    test_links = _load_test_links()
    if not _is_test_url(url, test_links):
        return False, msg

    # если ошибка не "парсинг объявления" — не гоняем лишнее
    if not _looks_like_parse_error(str(msg)):
        return False, msg

    # пробуем остальные тестовые ссылки (кроме текущей)
    for cand in test_links:
        if cand == url:
            continue
        ok2, msg2 = await _call_api_once(
            api_base=api_base,
            user_api_key=user_api_key,
            team_api_key=team_api_key,
            profile_id=profile_id,
            service=service,
            url=cand,
            need_balance_checker=need_balance_checker,
            timeout_s=timeout_s,
        )
        if ok2:
            return True, msg2

        # если это другая ошибка (например missing_keys) — смысла продолжать нет
        if not _looks_like_parse_error(str(msg2)):
            return False, msg2

    # если все тестовые ссылки не распарсились — возвращаем последнюю ошибку
    return False, msg
