from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
import unicodedata

logger = logging.getLogger(__name__)

HARD_BLACKLIST = ["bruno", "pierre", "evelyn", "marco", "peter", "tom", "hans", "claude"]


@dataclass
class ValidationConfig:
    api_key: str
    validation_url: str
    domains: List[str]

    min_len: int = 3
    max_len: int = 12
    blacklist: List[str] | None = None
    max_emails_per_seller: int = 4
    concurrency: int = 6
    use_ssl_verify: bool = True

    # ✅ важно: только имя+фамилия
    require_first_and_last: bool = True


def strip_accents(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_name(raw: str) -> str:
    if not raw:
        return ""
    s = " ".join(str(raw).strip().split())
    s = s.replace(". ", ".").replace(" .", ".")
    s = strip_accents(s)
    return s


def has_first_and_last(name: str) -> bool:
    parts = [p for p in normalize_name(name).split() if len(p) > 1]
    return len(parts) >= 2


def make_local_part(name: str) -> str:
    name = normalize_name(name)
    name_clean = re.sub(r"[^A-Za-z0-9\.\s-]", "", name)
    parts = name_clean.replace("-", " ").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].lower()
    first = parts[0]
    last = parts[-1]
    local = f"{first}.{last}"
    return re.sub(r"\.+", ".", local).lower()


def is_blacklisted(name: str, user_blacklist: List[str]) -> bool:
    if not name:
        return True
    n = normalize_name(name).lower()
    for b in HARD_BLACKLIST + user_blacklist:
        if b and (n == b or b in n.split()):
            return True
    return False


async def validate_email(session: aiohttp.ClientSession, email: str, cfg: ValidationConfig) -> bool:
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    params = {"email": email}
    ssl = None if cfg.use_ssl_verify else False

    try:
        async with session.get(cfg.validation_url, params=params, headers=headers, timeout=20, ssl=ssl) as resp:
            if resp.status != 200:
                return False
            try:
                data = await resp.json()
            except Exception:
                return False

            st = str(data.get("status") or data.get("result") or "").lower()
            if st in ("deliverable", "valid", "accepted"):
                return True

            if data.get("isDeliverable") is True or data.get("is_deliverable") is True or data.get("deliverable") is True:
                return True
            if data.get("smtp_check") is True or data.get("is_valid") is True:
                return True

            return False
    except Exception:
        return False


def _extract_fields(it: Dict[str, Any]) -> Dict[str, Any]:
    name = it.get("item_person_name") or it.get("person_name") or it.get("name") or it.get("seller") or ""
    title = it.get("item_title") or it.get("title") or it.get("name") or ""
    price = it.get("item_price") or it.get("price") or ""
    link = it.get("item_link") or it.get("link") or it.get("url") or ""
    photo = it.get("item_photo") or it.get("photo") or it.get("image") or ""
    return {"name": str(name or ""), "title": str(title or ""), "price": str(price or ""), "link": str(link or ""), "photo": str(photo or "")}


async def validate_offers(items: List[Dict[str, Any]], cfg: ValidationConfig) -> List[Dict[str, Any]]:
    """
    Возвращает список результатов:
    [
      {"name":..., "emails":[...], "title":..., "price":..., "link":..., "photo":..., "raw": original_item},
      ...
    ]
    """
    domains = cfg.domains[:]
    user_blacklist = cfg.blacklist or []

    connector = aiohttp.TCPConnector(limit_per_host=max(1, int(cfg.concurrency)))
    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))

    async with aiohttp.ClientSession(connector=connector) as session:

        async def process_one(it: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            fields = _extract_fields(it)
            name = normalize_name(fields["name"])
            if not name:
                return None

            if cfg.require_first_and_last and not has_first_and_last(name):
                return None

            if is_blacklisted(name, user_blacklist):
                return None

            local = make_local_part(name)
            local_len = local.replace(".", "")
            if not (cfg.min_len <= len(local_len) <= cfg.max_len):
                return None

            found: List[str] = []
            for dom in domains:
                if len(found) >= cfg.max_emails_per_seller:
                    break
                candidate = f"{local}@{dom}".lower()

                async with sem:
                    ok = await validate_email(session, candidate, cfg)
                if ok:
                    found.append(candidate)

            if not found:
                return None

            return {
                "name": name,
                "emails": found,
                "title": fields["title"],
                "price": fields["price"],
                "link": fields["link"],
                "photo": fields["photo"],
                "raw": it,
            }

        tasks = [asyncio.create_task(process_one(it)) for it in items]
        out: List[Dict[str, Any]] = []
        for t in asyncio.as_completed(tasks):
            r = await t
            if r:
                out.append(r)

        return out
