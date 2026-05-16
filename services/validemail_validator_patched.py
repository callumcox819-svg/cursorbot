import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp


HARD_BLACKLIST = {"bruno", "pierre", "evelyn", "marco", "peter", "tom", "hans", "claude"}


@dataclass
class ValidationConfig:
    validemail_api_key: str
    validemail_url: str = "https://validemail.co/api/v1/validate"
    concurrency: int = 6
    timeout_sec: int = 15
    use_ssl_verify: bool = True

    # правила
    require_first_and_last: bool = True
    min_len: int = 3
    max_len: int = 12
    max_emails_per_seller: int = 4
    user_blacklist: List[str] = None


def strip_accents(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_name(raw: str) -> str:
    """
    🇩🇪 DE-friendly нормализация:
    ä→ae, ö→oe, ü→ue, ß→ss (и верхний регистр тоже)
    затем удаляем прочие диакритики (é→e и т.п.)
    """
    if not raw:
        return ""

    s = " ".join(str(raw).strip().split())

    # Немецкие умляуты/ß (важно сделать ДО strip_accents)
    s = (
        s.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("Ä", "Ae")
        .replace("Ö", "Oe")
        .replace("Ü", "Ue")
    )

    # прочие диакритики
    s = strip_accents(s)

    # косметика для точек
    s = s.replace(". ", ".").replace(" .", ".")
    return s


def has_first_last(name: str) -> bool:
    parts = normalize_name(name).split()
    return len(parts) >= 2


def make_local_part(name: str) -> str:
    """
    Делает local-part в стиле first.last (как в твоём рабочем боте).
    """
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
    local = re.sub(r"\.+", ".", local).lower()
    return local


def is_blacklisted(name: str, user_blacklist: Optional[List[str]]) -> bool:
    if not name:
        return True
    n = normalize_name(name).lower()
    ub = [b.lower().strip() for b in (user_blacklist or []) if b and str(b).strip()]
    for b in HARD_BLACKLIST.union(set(ub)):
        if b and (n == b or b in n.split()):
            return True
    return False


async def _validate_email_http(session: aiohttp.ClientSession, email: str, cfg: ValidationConfig) -> bool:
    headers = {}
    if cfg.validemail_api_key:
        headers["Authorization"] = f"Bearer {cfg.validemail_api_key}"

    params = {"email": email}
    ssl = None if cfg.use_ssl_verify else False

    try:
        async with session.get(
            cfg.validemail_url,
            params=params,
            headers=headers,
            timeout=cfg.timeout_sec,
            ssl=ssl,
        ) as resp:
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


def extract_offer_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вытаскиваем поля из “типичного” JSON.
    Если у тебя другое имя ключа — добавим сюда 1 строку.
    """
    name_raw = (
        item.get("item_person_name")
        or item.get("person_name")
        or item.get("name")
        or item.get("seller")
        or ""
    )

    title = item.get("item_title") or item.get("title") or item.get("name") or ""
    price = item.get("item_price") or item.get("price") or ""
    link = item.get("item_link") or item.get("link") or item.get("url") or ""
    photo = item.get("item_photo") or item.get("photo") or item.get("image") or ""

    return {
        "person_name": normalize_name(name_raw),
        "title": str(title) if title is not None else "",
        "price": str(price) if price is not None else "",
        "link": str(link) if link is not None else "",
        "photo": str(photo) if photo is not None else "",
        "raw": item,
    }


async def validate_offers(items: List[Dict[str, Any]], domains: List[str], cfg: ValidationConfig, progress_cb: Optional[callable] = None) -> List[Dict[str, Any]]:
    """
    Возвращает список:
      { person_name, title, price, link, photo, raw, emails:[...] }
    """
    domains = [d.strip().lower() for d in (domains or []) if d and str(d).strip()]
    if not domains or not cfg.validemail_api_key:
        return []

    domains = domains[: max(1, cfg.max_emails_per_seller)]

    connector = aiohttp.TCPConnector(
        limit=max(20, cfg.concurrency * 4),
        limit_per_host=max(1, cfg.concurrency * 2),
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    sem = asyncio.Semaphore(max(1, cfg.concurrency))

    # Email-level progress counters (UI only; does not affect validation logic)
    email_total_checks = 0
    email_done_checks = 0
    email_active_checks = 0
    email_lock = asyncio.Lock()

    # Item-level counters (for mixed progress display)
    items_done = 0
    items_valid = 0

    async def _safe_progress() -> None:
        if progress_cb is None:
            return
        try:
            async with email_lock:
                dc = email_done_checks
                tc = email_total_checks
                ac = email_active_checks
            # Backward compatible: some callers expect 3 args
            try:
                await progress_cb(items_done, len(items), items_valid, dc, tc, ac)
            except TypeError:
                await progress_cb(items_done, len(items), items_valid)
        except Exception:
            pass

    # Speed-up: cache ValidEmail results and deduplicate concurrent checks for the same email.
    # This does NOT change validation logic; it only avoids repeated network calls.
    cache: dict[str, bool] = {}
    inflight: dict[str, asyncio.Future] = {}

    async def validate_cached(email: str) -> bool:
        nonlocal email_done_checks, email_active_checks

        cached = cache.get(email)
        if cached is not None:
            return cached

        fut = inflight.get(email)
        if fut is not None:
            return await fut

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        inflight[email] = fut
        try:
            async with sem:
                async with email_lock:
                    email_active_checks += 1
                try:
                    ok = await _validate_email_http(session, email, cfg)
                finally:
                    async with email_lock:
                        email_active_checks -= 1
                        email_done_checks += 1

            cache[email] = ok
            if not fut.done():
                fut.set_result(ok)

            # update UI (throttled upstream)
            await _safe_progress()
            return ok
        except Exception:
            cache[email] = False
            if not fut.done():
                fut.set_result(False)
            async with email_lock:
                email_done_checks += 1
            await _safe_progress()
            return False
        finally:
            inflight.pop(email, None)

    results: List[Dict[str, Any]] = []

    async with aiohttp.ClientSession(connector=connector) as session:

        async def process_one(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            nonlocal email_total_checks
            fields = extract_offer_fields(item)
            name = fields["person_name"]

            if not name:
                return None
            if cfg.require_first_and_last and not has_first_last(name):
                return None
            if is_blacklisted(name, cfg.user_blacklist):
                return None

            local = make_local_part(name)
            local_for_len = local.replace(".", "")
            if not (cfg.min_len <= len(local_for_len) <= cfg.max_len):
                return None

            found: List[str] = []

            # Validate all domain candidates in parallel (bounded by the same semaphore),
            # then keep the original domain order when selecting results.
            candidates = [f"{local}@{dom}".lower() for dom in domains]
            async with email_lock:
                nonlocal email_total_checks
                email_total_checks += len(candidates)
            await _safe_progress()
            checks = [asyncio.create_task(validate_cached(c)) for c in candidates]

            # gather preserves order of candidates
            try:
                oks = await asyncio.gather(*checks, return_exceptions=True)
            finally:
                for t in checks:
                    if not t.done():
                        t.cancel()

            for candidate, ok in zip(candidates, oks):
                if len(found) >= cfg.max_emails_per_seller:
                    break
                if ok is True:
                    found.append(candidate)

            if not found:
                return None

            out = dict(fields)
            out["emails"] = found
            return out

        tasks = [asyncio.create_task(process_one(it)) for it in items]
        for coro in asyncio.as_completed(tasks):
            r = await coro
            items_done += 1
            if r:
                results.append(r)
                items_valid = len(results)
            await _safe_progress()

    return results
