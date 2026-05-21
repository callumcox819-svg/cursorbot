from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from aiogram import Bot
from sqlalchemy import select

from database import Session
from models import User
from services.validemail_fast import validate_emails_fast
from services.seller_name import (
    MIN_NAME_TOKEN_LEN,
    normalize_seller_name,
    pick_handle_locals,
    pick_name_tokens,
    pick_name_tokens_for_email,
    seller_name_eligible_for_validation,
    seller_name_from_item,
)

logger = logging.getLogger(__name__)

# Только пользовательский blacklist из настроек (не режем имена из JSON автоматически).
DEFAULT_VALIDEMAIL_URL = "https://validemail.co/api/v1/validate"


@dataclass
class ValidationConfig:
    validemail_api_key: str | None = None
    validemail_api_keys: list[str] | None = None
    validation_url: str = DEFAULT_VALIDEMAIL_URL

    concurrency: int = 12
    max_emails_per_seller: int = 4
    min_len: int = MIN_NAME_TOKEN_LEN
    max_len: int = 40
    require_first_and_last: bool = False

    user_blacklist: list[str] | None = None
    use_ssl_verify: bool = True
    # Личный ЧС имён продавцов (Maria Johansen и т.д.) — повторно не валидировать
    seller_name_keys: set[str] | None = None


# -------------------------
# Helpers: name normalization
# -------------------------

def _normalize_name(raw: str) -> str:
    return normalize_seller_name(raw)


def _pick_alpha_tokens(name: str) -> list[str]:
    return pick_name_tokens_for_email(name)


def _pick_first_last_alpha_tokens(name: str) -> tuple[str, str]:
    tokens = _pick_alpha_tokens(name)
    if len(tokens) < 2:
        return "", ""
    return tokens[0], tokens[-1]


def _name_is_usable(name: str, *, require_first_and_last: bool) -> bool:
    if not seller_name_eligible_for_validation(name):
        return False
    # Ники Semiuel2421 / Andora01 — local-part из handle, без буквенных tokens.
    if pick_handle_locals(name):
        return True
    tokens = _pick_alpha_tokens(name)
    if require_first_and_last:
        return len(tokens) >= 2
    return len(tokens) >= 1


def _name_has_first_last(name: str) -> bool:
    return _name_is_usable(name, require_first_and_last=True)


def _make_local_part_from_name(name: str, *, require_first_and_last: bool) -> str:
    """Один основной local-part (first.last или одно слово)."""
    variants = _make_local_part_variants(name, require_first_and_last=require_first_and_last)
    return variants[0] if variants else ""


def _make_local_part_variants(name: str, *, require_first_and_last: bool) -> list[str]:
    """
    Логины из имени продавца (готовые email из JSON не используем).
    Приоритет: ник (Semiuel2421) или first.last (Sam Day → sam.day), затем firstlast.
    """
    out: list[str] = []
    seen: set[str] = set()
    norm = _normalize_name(name)
    parts = [p for p in re.split(r"[\s\-']+", norm) if p.strip()]

    def _add(local: str) -> None:
        local = re.sub(r"[^a-z0-9._+\-]", "", (local or "").lower())
        local = re.sub(r"\.+", ".", local).strip(".")
        if not local or local in seen:
            return
        seen.add(local)
        out.append(local)

    handles = pick_handle_locals(name)
    if handles and len(parts) <= 1:
        for h in handles:
            _add(h)
        return out

    tokens = _pick_alpha_tokens(name)
    if require_first_and_last and len(tokens) < 2:
        for h in handles:
            _add(h)
        return out
    if not tokens:
        for h in handles:
            _add(h)
        return out

    if len(tokens) == 1:
        for h in handles:
            _add(h)
        _add(tokens[0])
        return out

    first, last = tokens[0], tokens[-1]
    if len(first) < 1 or len(last) < 1:
        return out

    for h in handles:
        _add(h)
    _add(f"{first}.{last}")
    _add(f"{first}{last}")
    return out


def _is_blacklisted(name: str, user_blacklist: Iterable[str] | None) -> bool:
    """Только явный blacklist пользователя (полное имя)."""
    if not name or not user_blacklist:
        return False
    n = _normalize_name(name).lower()
    for b in user_blacklist:
        bb = str(b or "").strip().lower()
        if bb and bb == n:
            return True
    return False


def _len_for_limits(local_part: str) -> int:
    return len((local_part or "").replace(".", ""))


def _is_api_failure(_ok: bool, raw: object) -> bool:
    """Сбой API/сети. Ответ «email не существует» — не ошибка."""
    if not isinstance(raw, dict):
        return True
    try:
        st = int(raw.get("_http_status"))
        if st in (401, 403, 429) or st >= 500:
            return True
    except (TypeError, ValueError):
        pass
    err = raw.get("error")
    if err is None:
        err = raw.get("message")
    if err is not None:
        es = str(err or "").strip().lower()
        if es in ("", "empty", "no api key"):
            return False
        benign = (
            "invalid email",
            "not valid",
            "undeliverable",
            "not deliverable",
            "does not exist",
            "doesn't exist",
            "no mx",
            "mailbox",
            "rejected",
            "disposable",
            "unverified",
            "unknown user",
            "user unknown",
            "address not found",
            "no such user",
        )
        if any(p in es for p in benign):
            return False
        return True
    for key in (
        "IsValid",
        "isValid",
        "State",
        "state",
        "Score",
        "score",
        "Reason",
        "reason",
        "is_valid",
        "valid",
        "status",
        "result",
        "isDeliverable",
        "is_deliverable",
        "deliverable",
        "smtp_check",
    ):
        if key in raw:
            return False
    return False


# -------------------------
# NEW API helpers (/validate)
# -------------------------

def _extract_emails_from_offer(offer: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("emails", "email", "seller_email", "from_email"):
        if key not in offer:
            continue
        v = offer.get(key)
        if isinstance(v, str):
            e = v.strip()
            if e:
                out.append(e)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    e = x.strip()
                    if e:
                        out.append(e)
                elif isinstance(x, dict):
                    ev = x.get("email")
                    if isinstance(ev, str) and ev.strip():
                        out.append(ev.strip())

    seen = set()
    uniq: list[str] = []
    for e in out:
        el = e.lower()
        if el not in seen:
            seen.add(el)
            uniq.append(e)
    return uniq


async def _get_validemail_key_for_user(session: Session, telegram_id: int) -> str | None:
    user = (await session.execute(select(User).where(User.telegram_id == int(telegram_id)))).scalars().first()
    if not user:
        return None
    key = (getattr(user, "validemail_key", None) or "").strip()
    return key or None


# -------------------------
# OLD API (handlers/validation.py) — УСКОРЕННЫЙ
# -------------------------

ProgressCb = Callable[[int, int, int, int], None]


def merge_validation_domains(user_domains: list[str]) -> list[str]:
    """
    Домены в порядке «Приоритет отправки» (настройки) + остальные из БД, без дублей.
    Никакого принудительного gmail — только ваш список.
    """
    seen: set[str] = set()
    out: list[str] = []
    for d in user_domains or []:
        dd = str(d or "").strip().lower()
        if dd and dd not in seen:
            seen.add(dd)
            out.append(dd)
    return out


async def _validate_offers_old(
    items: list[dict[str, Any]],
    domains: list[str],
    cfg: ValidationConfig,
    *,
    progress_cb: ProgressCb | None = None,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    # домены: uniq + clean (сохраняем порядок = приоритет)
    domains_clean: list[str] = []
    seen_dom = set()
    for d in domains or []:
        dd = str(d or "").strip().lower()
        if dd and dd not in seen_dom:
            seen_dom.add(dd)
            domains_clean.append(dd)
    if not domains_clean:
        return []

    # После фикса парсинга ответа API — не использовать старый кэш с ложными "invalid".
    try:
        from services.validemail_fast import _CACHE

        _CACHE.clear()
    except Exception:
        pass

    user_blacklist = cfg.user_blacklist or []
    require_fl = bool(cfg.require_first_and_last)

    if stats is not None:
        stats.clear()
        stats.update(
            {
                "offers_total": len(items),
                "offers_eligible": 0,
                "offers_validated": 0,
                "offers_remaining": len(items),
                "emails_checked": 0,
                "emails_total": 0,
                "combinations_valid": 0,
                "current_domain": "",
                "sellers_with_email": 0,
                "last_valid_email": "",
                "seller_index": 0,
                "sellers_total": 0,
                "current_seller_name": "",
                "current_probe": "",
                "short_nicks": 0,
                "blacklisted": 0,
                "duplicates": 0,
                "api_errors": 0,
                "no_name": 0,
            }
        )

    from services.seller_blacklist import seller_name_key

    seller_bl = set(cfg.seller_name_keys or set())
    batch_seen_names: set[str] = set()
    pending_seller_names: set[str] = set()
    if stats is not None:
        stats["pending_seller_names"] = pending_seller_names

    # 1) Имя из JSON → local-part (готовые email в файле игнорируем)
    prepared: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        raw_name = seller_name_from_item(it)
        if not (raw_name or "").strip():
            if stats is not None:
                stats["no_name"] = int(stats.get("no_name") or 0) + 1
            continue

        name_key = seller_name_key(raw_name)
        # 1 продавец = 1 лот за прогон (первый в JSON); повтор имени — как ЧС
        if name_key and (name_key in seller_bl or name_key in batch_seen_names):
            if stats is not None:
                stats["blacklisted"] = int(stats.get("blacklisted") or 0) + 1
            continue

        if _is_blacklisted(raw_name, user_blacklist):
            if stats is not None:
                stats["blacklisted"] = int(stats.get("blacklisted") or 0) + 1
            continue

        if not _name_is_usable(raw_name, require_first_and_last=require_fl):
            if stats is not None:
                stats["short_nicks"] = int(stats.get("short_nicks") or 0) + 1
            continue

        locals_list: list[str] = []
        for local in _make_local_part_variants(raw_name, require_first_and_last=require_fl):
            ln = _len_for_limits(local)
            if int(cfg.min_len) <= ln <= int(cfg.max_len):
                locals_list.append(local)

        if not locals_list:
            continue

        prepared.append({
            "raw": it,
            "person_name": raw_name,
            "name_key": name_key,
            "locals": locals_list,
            "title": str(it.get("item_title") or it.get("title") or "").strip(),
            "price": str(it.get("item_price") or it.get("price") or "").strip(),
            "link": str(it.get("item_link") or it.get("link") or it.get("url") or "").strip(),
            "photo": str(it.get("item_photo") or it.get("photo") or it.get("image") or "").strip(),
        })
        if name_key:
            batch_seen_names.add(name_key)

    if stats is not None:
        stats["offers_eligible"] = len(prepared)
        stats["offers_remaining"] = max(0, int(stats.get("offers_total", 0)) - 0)

    if not prepared:
        return []

    # ✅ ТЗ: домены проверяем по приоритету, но на одного продавца сохраняем максимум N (сейчас N=2),
    # и не проверяем дальше для конкретного продавца, если уже набрали лимит.
    per_seller_limit = max(1, int(cfg.max_emails_per_seller))

    # хранит найденные валидные emails по индексу prepared
    found_by_idx: list[list[str]] = [[] for _ in prepared]

    # оценка проверок: сначала 1 логин × домен, потом запасные варианты
    n_dom = len(domains_clean)
    overall_total = max(
        1,
        sum(
            n_dom * max(1, len(p.get("locals") or []))
            for p in prepared
        ),
    )
    overall_done = 0

    if stats is not None:
        stats["emails_total"] = overall_total

    limit = max(2, int(cfg.concurrency))
    if progress_cb:
        try:
            progress_cb(0, overall_total, limit, 0)
        except Exception:
            pass

    api_keys = [str(k).strip() for k in (cfg.validemail_api_keys or []) if str(k).strip()]
    if not api_keys:
        single = str(cfg.validemail_api_key or "").strip()
        if single:
            api_keys = [single]
    url = str(cfg.validation_url or DEFAULT_VALIDEMAIL_URL).strip()

    seen_valid_emails: set[str] = set()
    state_lock = asyncio.Lock()
    n_keys = len(api_keys)
    per_key_limit = max(4, limit // n_keys) if n_keys > 1 else limit
    parallel_pool = per_key_limit * n_keys if n_keys > 1 else limit
    sellers_completed = 0

    if n_keys >= 2:
        logger.info(
            "validemail: %s keys in parallel (stride), concurrency/key=%s pool=%s sellers=%s",
            n_keys,
            per_key_limit,
            parallel_pool,
            len(prepared),
        )

    async def _run_batch(
        batch_emails: list[str],
        *,
        seller_i: int,
        dom: str,
        api_key: str,
    ) -> list[tuple[str, bool, dict]]:
        if not batch_emails:
            return []
        async with state_lock:
            if stats is not None:
                stats["current_domain"] = dom
                stats["current_probe"] = batch_emails[0]
            base_done = overall_done

        def _wrap_progress(
            done: int, total: int, lim: int, in_use: int, _bd: int = base_done
        ) -> None:
            if not progress_cb:
                return
            try:
                progress_cb(_bd + int(done or 0), overall_total, parallel_pool, in_use)
            except Exception:
                pass

        return await validate_emails_fast(
            batch_emails,
            api_keys=[api_key],
            concurrency=per_key_limit,
            url=url,
            use_ssl_verify=bool(cfg.use_ssl_verify),
            progress_cb=lambda d, t, l, u, _bd=base_done: _wrap_progress(d, t, l, u, _bd),
        )

    async def _consume_results(
        seller_i: int, results: list[tuple[str, bool, dict]]
    ) -> int:
        nonlocal overall_done
        async with state_lock:
            overall_done += len(results)
            combos_valid = 0
            for _e, ok, raw in results:
                if not ok:
                    if stats is not None and _is_api_failure(ok, raw):
                        stats["api_errors"] = int(stats.get("api_errors") or 0) + 1
                    continue
                combos_valid += 1
                key = (_e or "").strip().lower()
                lst = found_by_idx[seller_i]
                if len(lst) >= per_seller_limit or key in lst:
                    continue
                if key in seen_valid_emails:
                    if stats is not None:
                        stats["duplicates"] = int(stats.get("duplicates") or 0) + 1
                    continue
                seen_valid_emails.add(key)
                lst.append(key)
                if stats is not None:
                    stats["last_valid_email"] = key
            return combos_valid

    def _refresh_stats() -> None:
        if stats is None:
            return
        sellers_found = sum(1 for f in found_by_idx if f)
        stats["emails_checked"] = overall_done
        stats["sellers_with_email"] = sellers_found
        stats["offers_validated"] = sellers_found
        eligible_o = int(stats.get("offers_eligible") or len(prepared))
        stats["offers_remaining"] = max(0, eligible_o - sellers_found)

    async def _validate_seller(i: int, api_key: str) -> None:
        row = prepared[i]
        async with state_lock:
            if stats is not None:
                stats["current_seller_name"] = str(row.get("person_name") or "")[:60]

        locals_list = list(row.get("locals") or [])
        if not locals_list:
            return

        primary = locals_list[0]
        extra_locals = locals_list[1:]

        for dom in domains_clean:
            if len(found_by_idx[i]) >= per_seller_limit:
                break
            batch = [f"{primary}@{dom}".lower()]
            results = await _run_batch(batch, seller_i=i, dom=dom, api_key=api_key)
            cv = await _consume_results(i, results)
            async with state_lock:
                if stats is not None:
                    stats["combinations_valid"] = int(stats.get("combinations_valid") or 0) + cv
                _refresh_stats()
            if found_by_idx[i]:
                break

        if found_by_idx[i] or not extra_locals:
            return

        for dom in domains_clean:
            if len(found_by_idx[i]) >= per_seller_limit:
                break
            batch = [f"{local}@{dom}".lower() for local in extra_locals]
            results = await _run_batch(batch, seller_i=i, dom=dom, api_key=api_key)
            cv = await _consume_results(i, results)
            async with state_lock:
                if stats is not None:
                    stats["combinations_valid"] = int(stats.get("combinations_valid") or 0) + cv
                _refresh_stats()
            if found_by_idx[i]:
                break

        if found_by_idx[i]:
            nk = str(prepared[i].get("name_key") or "").strip()
            if nk:
                async with state_lock:
                    batch_seen_names.add(nk)
                    pending_seller_names.add(nk)

    async def _worker(key_idx: int) -> None:
        nonlocal sellers_completed
        my_key = api_keys[key_idx]
        for i in range(key_idx, len(prepared), n_keys):
            await _validate_seller(i, my_key)
            async with state_lock:
                sellers_completed += 1
                if stats is not None:
                    stats["seller_index"] = sellers_completed
                    stats["sellers_total"] = len(prepared)
                _refresh_stats()

    # 2) Продавцы: при 2+ ключах — два потока (каждый ключ свой), иначе последовательно
    n_sellers = len(prepared)
    if stats is not None:
        stats["sellers_total"] = n_sellers

    if n_keys >= 2:
        await asyncio.gather(*(_worker(k) for k in range(n_keys)))
    else:
        for i in range(n_sellers):
            await _validate_seller(i, api_keys[0])
            sellers_completed = i + 1
            if stats is not None:
                stats["seller_index"] = sellers_completed
            _refresh_stats()

    # 3) собираем результат
    out_rows: list[dict[str, Any]] = []
    for i, row in enumerate(prepared):
        found = found_by_idx[i][:per_seller_limit]
        if not found:
            continue
        out_rows.append({
            "raw": row["raw"],
            "person_name": _normalize_name(row["person_name"]),
            "title": row["title"],
            "price": row["price"],
            "link": row["link"],
            "photo": row["photo"],
            "emails": found,
        })

    return out_rows



# -------------------------
# NEW API (services/validator.py)
# -------------------------

async def _validate_offers_new(
    *,
    telegram_id: int,
    offers: list[dict[str, Any]],
    bot: Bot,
    chat_id: int,
    config: ValidationConfig | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    cfg = config or ValidationConfig()

    all_emails: list[str] = []
    offer_emails: list[list[str]] = []
    for off in offers:
        ems = _extract_emails_from_offer(off)
        offer_emails.append(ems)
        all_emails.extend(ems)

    seen = set()
    uniq_emails: list[str] = []
    for e in all_emails:
        el = e.strip().lower()
        if el and el not in seen:
            seen.add(el)
            uniq_emails.append(e.strip())

    from services.validemail_keys import resolve_validemail_api_keys

    api_keys = [str(k).strip() for k in (cfg.validemail_api_keys or []) if str(k).strip()]
    if not api_keys:
        single = (cfg.validemail_api_key or "").strip()
        if single:
            api_keys = [single]
    if not api_keys:
        api_keys = resolve_validemail_api_keys()

    if not api_keys:
        return {
            "summary_text": "❌ Не найден validemail API key. Задай VALIDEMAIL_API_KEYS в config.",
            "output_json_bytes": None,
            "output_filename": None,
            "stats": {"total_offers": len(offers), "total_emails": len(uniq_emails), "error": "no_api_key"},
        }

    total = len(uniq_emails)
    progress_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"🔎 Валидация началась…\nEmail'ов: <b>{total}</b>",
        parse_mode="HTML",
    )

    results = await validate_emails_fast(
        uniq_emails,
        api_keys=api_keys,
        concurrency=max(2, int(cfg.concurrency)),
        url=str(cfg.validation_url or DEFAULT_VALIDEMAIL_URL).strip(),
        use_ssl_verify=bool(cfg.use_ssl_verify),
        progress_cb=None,
    )

    by_email: dict[str, tuple[bool, dict]] = {}
    for e, ok, raw in results:
        by_email[(e or "").strip().lower()] = (bool(ok), raw if isinstance(raw, dict) else {"raw": str(raw)})

    valid_count = 0
    invalid_count = 0
    offers_out: list[dict[str, Any]] = []

    for off, ems in zip(offers, offer_emails):
        off2 = dict(off)
        checks: list[dict[str, Any]] = []
        any_valid = False

        for e in ems:
            key = e.strip().lower()
            ok, raw = by_email.get(key, (False, {"error": "not_checked"}))
            checks.append({"email": e, "ok": ok, "raw": raw})
            if ok:
                any_valid = True

        off2["validemail_checked"] = True
        off2["validemail_any_ok"] = any_valid
        off2["validemail_results"] = checks

        if ems:
            if any_valid:
                valid_count += 1
            else:
                invalid_count += 1

        offers_out.append(off2)

    elapsed = max(0.01, time.time() - t0)

    try:
        await progress_msg.edit_text(
            "✅ Валидация завершена.\n"
            f"Офферов: <b>{len(offers)}</b>\n"
            f"Уникальных email: <b>{total}</b>\n"
            f"Офферов с валидным email: <b>{valid_count}</b>\n"
            f"Офферов без валидного email: <b>{invalid_count}</b>\n"
            f"Время: <b>{elapsed:.1f}s</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    out_bytes = json.dumps(offers_out, ensure_ascii=False, indent=2).encode("utf-8")
    out_name = f"validated_{telegram_id}.json"

    return {
        "summary_text": (
            "✅ Валидация завершена.\n"
            f"Офферов: {len(offers)} | Уникальных email: {total} | "
            f"OK-офферов: {valid_count} | BAD-офферов: {invalid_count} | "
            f"{elapsed:.1f}s"
        ),
        "output_json_bytes": out_bytes,
        "output_filename": out_name,
        "stats": {
            "total_offers": len(offers),
            "unique_emails": total,
            "offers_any_ok": valid_count,
            "offers_all_bad": invalid_count,
            "seconds": elapsed,
        },
    }


# -------------------------
# Public wrapper (оба интерфейса)
# -------------------------

async def validate_offers(*args, **kwargs):
    """
    OLD: validate_offers(items, domains, cfg, progress_cb=...)
    NEW: validate_offers(telegram_id=..., offers=..., bot=..., chat_id=..., config=...)
    """
    if "telegram_id" in kwargs or "offers" in kwargs:
        return await _validate_offers_new(
            telegram_id=int(kwargs["telegram_id"]),
            offers=list(kwargs["offers"]),
            bot=kwargs["bot"],
            chat_id=int(kwargs["chat_id"]),
            config=kwargs.get("config"),
        )

    if len(args) >= 3 and isinstance(args[0], list) and isinstance(args[1], list):
        items = args[0]
        domains = args[1]
        cfg = args[2]
        progress_cb = kwargs.get("progress_cb")
        stats = kwargs.get("stats")
        return await _validate_offers_old(
            items, domains, cfg, progress_cb=progress_cb, stats=stats
        )

    raise TypeError("validate_offers(): unsupported call signature")
