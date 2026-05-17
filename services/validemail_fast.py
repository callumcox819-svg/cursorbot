from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import aiohttp


@dataclass
class CacheItem:
    ok: bool
    ts: float
    raw: dict


# cache key includes URL to avoid mixing different providers/endpoints
_CACHE: dict[str, CacheItem] = {}
_CACHE_TTL_SEC = 60 * 60 * 6  # 6 часов

_SESSION: aiohttp.ClientSession | None = None


def _cache_key(url: str, email: str) -> str:
    u = (url or "").strip().lower()
    e = (email or "").strip().lower()
    return f"{u}::{e}"


def _cache_get(url: str, email: str) -> CacheItem | None:
    k = _cache_key(url, email)
    if not k.strip(":"):
        return None
    item = _CACHE.get(k)
    if not item:
        return None
    if time.time() - item.ts > _CACHE_TTL_SEC:
        _CACHE.pop(k, None)
        return None
    return item


def _cache_set(url: str, email: str, ok: bool, raw: dict) -> None:
    k = _cache_key(url, email)
    if not k.strip(":"):
        return
    _CACHE[k] = CacheItem(ok=bool(ok), ts=time.time(), raw=raw or {})


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        return _SESSION

    timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_connect=5, sock_read=10)
    connector = aiohttp.TCPConnector(limit=300, ttl_dns_cache=300)
    _SESSION = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _SESSION


async def close_validemail_session() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _normalize_ok(data: object) -> bool:
    """
    Универсальная нормализация под разные ответы ValidEmail.
    validemail.co: IsValid, State=Deliverable, Score (см. docs).
    """
    if not isinstance(data, dict):
        return False

    # validemail.co / validemail.net (PascalCase)
    if data.get("IsValid") is True or data.get("isValid") is True:
        return True
    state = str(data.get("State") or data.get("state") or "").lower().strip()
    if state in ("deliverable", "valid", "ok", "accepted"):
        return True
    reason = str(data.get("Reason") or data.get("reason") or "").lower()
    if "accepted" in reason and "invalid" not in reason:
        return True
    try:
        score = int(data.get("Score") if data.get("Score") is not None else data.get("score") or 0)
        if score >= 80 and state != "not deliverable":
            return True
    except (TypeError, ValueError):
        pass

    # lowercase / legacy
    if data.get("is_valid") is True or data.get("valid") is True:
        return True

    status = str(
        data.get("status") or data.get("result") or data.get("State") or ""
    ).lower().strip()
    if status in ("valid", "ok", "deliverable", "accepted"):
        return True

    if data.get("isDeliverable") is True or data.get("is_deliverable") is True or data.get("deliverable") is True:
        return True

    if data.get("smtp_check") is True:
        return True

    return False


def _build_request(url: str, api_key: str, email: str) -> tuple[dict, dict]:
    """
    Возвращает (headers, params) для GET запроса.
    Поддерживает:
      - validemail.co (Bearer, params email)
      - api.validemail.net (api_key query, params api_key+email)
      - auto по URL
    """
    u = (url or "").strip()
    ul = u.lower()
    headers: dict = {}
    params: dict = {}

    if "validemail.co" in ul:
        # https://validemail.co/api/v1/validate?email=...
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        params["email"] = email
        return headers, params

    # default / legacy style (query api_key)
    params["api_key"] = api_key
    params["email"] = email
    return headers, params


ProgressCb = Callable[[int, int, int, int], None]


async def _check_one(
    email: str,
    *,
    api_key: str,
    url: str,
    use_ssl_verify: bool,
    semaphore: asyncio.Semaphore,
    lock: asyncio.Lock,
    progress_cb: ProgressCb | None,
    counters: dict,
    limit: int,
) -> tuple[str, bool, dict]:
    email_lc = (email or "").strip().lower()
    if not email_lc:
        return email, False, {"error": "empty"}

    cached = _cache_get(url, email_lc)
    if cached:
        # cached тоже считаем как "done"
        async with lock:
            counters["done"] += 1
            if progress_cb:
                try:
                    progress_cb(counters["done"], counters["total"], limit, counters["in_use"])
                except Exception:
                    pass
        return email, cached.ok, cached.raw

    async with semaphore:
        async with lock:
            counters["in_use"] += 1
            if progress_cb:
                try:
                    progress_cb(counters["done"], counters["total"], limit, counters["in_use"])
                except Exception:
                    pass

        try:
            s = await _get_session()
            headers, params = _build_request(url, api_key, email_lc)
            ssl = None if use_ssl_verify else False
            async with s.get(url, params=params, headers=headers, ssl=ssl) as r:
                # иногда сервис возвращает json с неправильным content-type
                data = await r.json(content_type=None)
                raw = data if isinstance(data, dict) else {"raw": str(data)}
                if isinstance(raw, dict):
                    raw["_http_status"] = int(r.status)
                ok = _normalize_ok(data) if int(r.status) == 200 else False
                _cache_set(url, email_lc, ok, raw)
                return email, ok, raw
        except Exception as e:
            raw = {"error": str(e)}
            _cache_set(url, email_lc, False, raw)
            return email, False, raw
        finally:
            async with lock:
                counters["in_use"] -= 1
                counters["done"] += 1
                if progress_cb:
                    try:
                        progress_cb(counters["done"], counters["total"], limit, counters["in_use"])
                    except Exception:
                        pass


async def _validate_emails_single_key(
    emails_list: list[str],
    *,
    api_key: str,
    concurrency: int,
    url: str,
    use_ssl_verify: bool,
    progress_cb: ProgressCb | None,
    counters: dict | None = None,
    shared_limit: int | None = None,
) -> list[tuple[str, bool, dict]]:
    api_key = (api_key or "").strip()
    if not api_key:
        return [(e, False, {"error": "no api key"}) for e in emails_list]

    limit = max(2, int(concurrency))
    display_limit = int(shared_limit) if shared_limit is not None else limit
    sem = asyncio.Semaphore(limit)
    lock = asyncio.Lock()

    local_counters = counters if counters is not None else {
        "done": 0,
        "in_use": 0,
        "total": len(emails_list),
    }
    if counters is None and progress_cb:
        try:
            progress_cb(0, local_counters["total"], display_limit, 0)
        except Exception:
            pass

    tasks = [
        asyncio.create_task(
            _check_one(
                e,
                api_key=api_key,
                url=url,
                use_ssl_verify=use_ssl_verify,
                semaphore=sem,
                lock=lock,
                progress_cb=progress_cb,
                counters=local_counters,
                limit=display_limit,
            )
        )
        for e in emails_list
    ]
    return await asyncio.gather(*tasks)


async def validate_emails_fast(
    emails: Iterable[str],
    *,
    api_key: str | None = None,
    api_keys: list[str] | None = None,
    concurrency: int = 25,
    url: str = "https://validemail.co/api/v1/validate",
    use_ssl_verify: bool = True,
    progress_cb: ProgressCb | None = None,
) -> list[tuple[str, bool, dict]]:
    """
    Быстрая параллельная проверка email.
    Несколько api_keys: emails делятся между ключами, каждый ключ — свой пул запросов.
  """
    url = (url or "").strip() or "https://validemail.co/api/v1/validate"
    emails_list = [str(e).strip() for e in emails if str(e).strip()]

    keys = [str(k).strip() for k in (api_keys or []) if str(k).strip()]
    if not keys:
        single = (api_key or "").strip()
        if single:
            keys = [single]

    if not keys:
        return [(e, False, {"error": "no api key"}) for e in emails_list]

    if len(keys) == 1:
        return await _validate_emails_single_key(
            emails_list,
            api_key=keys[0],
            concurrency=concurrency,
            url=url,
            use_ssl_verify=use_ssl_verify,
            progress_cb=progress_cb,
        )

    n_keys = len(keys)
    per_key_limit = max(2, int(concurrency) // n_keys)
    total_limit = per_key_limit * n_keys

    buckets: list[list[tuple[int, str]]] = [[] for _ in range(n_keys)]
    for i, e in enumerate(emails_list):
        buckets[i % n_keys].append((i, e))

    shared_counters = {"done": 0, "in_use": 0, "total": len(emails_list)}
    if progress_cb:
        try:
            progress_cb(0, shared_counters["total"], total_limit, 0)
        except Exception:
            pass

    async def _run_bucket(key_idx: int, bucket: list[tuple[int, str]]) -> list[tuple[int, str, bool, dict]]:
        if not bucket:
            return []
        emails_only = [e for _, e in bucket]
        rows = await _validate_emails_single_key(
            emails_only,
            api_key=keys[key_idx],
            concurrency=per_key_limit,
            url=url,
            use_ssl_verify=use_ssl_verify,
            progress_cb=progress_cb,
            counters=shared_counters,
            shared_limit=total_limit,
        )
        return [(bucket[i][0], rows[i][0], rows[i][1], rows[i][2]) for i in range(len(rows))]

    merged: list[tuple[str, bool, dict] | None] = [None] * len(emails_list)
    bucket_results = await asyncio.gather(*(_run_bucket(i, b) for i, b in enumerate(buckets)))
    for chunk in bucket_results:
        for orig_i, email, ok, raw in chunk:
            merged[orig_i] = (email, ok, raw)

    out: list[tuple[str, bool, dict]] = []
    for i, e in enumerate(emails_list):
        row = merged[i]
        if row is None:
            out.append((e, False, {"error": "not_checked"}))
        else:
            out.append(row)
    return out
