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

# Домены для поиска email по имени (VoidParser / Facebook Marketplace — почти всегда gmail).
_MARKETPLACE_PROBE_DOMAINS = ("gmail.com", "gmx.ch", "gmx.net")

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
    Несколько типичных local-part для одного продавца.
    Реальные люди часто используют не только first.last.
  """
    out: list[str] = []
    seen: set[str] = set()

    def _add(local: str) -> None:
        local = re.sub(r"[^a-z0-9._+\-]", "", (local or "").lower())
        local = re.sub(r"\.+", ".", local).strip(".")
        if not local or local in seen:
            return
        seen.add(local)
        out.append(local)

    # Ники: alinafor20 → сразу как local-part
    for handle in pick_handle_locals(name):
        _add(handle)

    tokens = _pick_alpha_tokens(name)
    if require_first_and_last and len(tokens) < 2:
        return out
    if not tokens:
        return out

    if len(tokens) == 1:
        _add(tokens[0])
        return out

    first, last = tokens[0], tokens[-1]
    if len(first) < 1 or len(last) < 1:
        return out

    _add(f"{first}.{last}")
    _add(f"{first}{last}")
    _add(f"{first}_{last}")
    if len(last) >= 2:
        _add(f"{first}{last[0]}")
    if len(first) >= 1:
        _add(f"{first[0]}{last}")
    _add(f"{last}.{first}")
    _add(f"{last}{first}")
    if len(tokens) >= 3:
        mid = tokens[1]
        if len(mid) >= 2:
            _add(f"{first}.{mid}.{last}")
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


def probe_domains_for_import(items: list[dict[str, Any]]) -> list[str]:
    """Доп. домены для валидации по имени (не заменяют домены пользователя)."""
    has_fb = False
    for it in items or []:
        if not isinstance(it, dict):
            continue
        link = str(it.get("item_link") or it.get("link") or it.get("url") or "").lower()
        if "facebook.com" in link:
            has_fb = True
            break
    if not has_fb:
        return []
    return list(_MARKETPLACE_PROBE_DOMAINS)


def merge_validation_domains(
    user_domains: list[str], items: list[dict[str, Any]]
) -> list[str]:
    """Для Facebook: сначала gmail/gmx (как VoidParser), затем домены пользователя."""
    probe = probe_domains_for_import(items)
    ordered = (list(probe) + list(user_domains or [])) if probe else list(user_domains or [])
    seen: set[str] = set()
    out: list[str] = []
    for d in ordered:
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
            }
        )

    # 1) подготовка офферов (имя из JSON + blacklist + длина local-part)
    prepared: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        raw_name = seller_name_from_item(it)

        if not _name_is_usable(raw_name, require_first_and_last=require_fl):
            continue
        if _is_blacklisted(raw_name, user_blacklist):
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
            "locals": locals_list,
            "title": str(it.get("item_title") or it.get("title") or "").strip(),
            "price": str(it.get("item_price") or it.get("price") or "").strip(),
            "link": str(it.get("item_link") or it.get("link") or it.get("url") or "").strip(),
            "photo": str(it.get("item_photo") or it.get("photo") or it.get("image") or "").strip(),
        })

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

    # общий прогресс (оценка): варианты имени × домены из настроек
    locals_per = sum(len(p.get("locals") or []) for p in prepared)
    overall_total = max(1, locals_per * len(domains_clean))
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

    # 2) имя → local-part → @домен → ValidEmail (домены по приоритету)
    for dom_idx, dom in enumerate(domains_clean):
        if stats is not None:
            stats["current_domain"] = dom
            stats["domain_index"] = dom_idx + 1
            stats["domains_total"] = len(domains_clean)
        # индексы офферов, которым ещё нужны email
        need_idx = [i for i, f in enumerate(found_by_idx) if len(f) < per_seller_limit]
        if not need_idx:
            break

        # собираем кандидатов только для нужных офферов и только для текущего домена
        batch_emails: list[str] = []
        email_to_idx: dict[str, int] = {}
        for i in need_idx:
            for local in prepared[i].get("locals") or []:
                cand = f"{local}@{dom}".lower()
                if cand not in email_to_idx:  # дедуп внутри батча
                    email_to_idx[cand] = i
                    batch_emails.append(cand)

        if not batch_emails:
            continue

        base_done = overall_done

        def _wrap_progress(done: int, total: int, lim: int, in_use: int) -> None:
            if not progress_cb:
                return
            try:
                progress_cb(base_done + int(done or 0), overall_total, lim, in_use)
            except Exception:
                pass

        results = await validate_emails_fast(
            batch_emails,
            api_keys=api_keys,
            concurrency=limit,
            url=url,
            use_ssl_verify=bool(cfg.use_ssl_verify),
            progress_cb=_wrap_progress,
        )

        # считаем, что батч "проверен"
        overall_done += len(batch_emails)

        sellers_found = sum(1 for f in found_by_idx if f)
        if stats is not None:
            stats["emails_checked"] = overall_done
            stats["sellers_with_email"] = sellers_found
            stats["offers_validated"] = sellers_found
            eligible_o = int(stats.get("offers_eligible") or len(prepared))
            stats["offers_remaining"] = max(0, eligible_o - sellers_found)

        combos_valid = 0
        for e, ok, _raw in results:
            if not ok:
                continue
            combos_valid += 1
            key = (e or "").strip().lower()
            idx = email_to_idx.get(key)
            if idx is None:
                continue
            lst = found_by_idx[idx]
            if len(lst) < per_seller_limit and key not in lst:
                lst.append(key)
                if stats is not None:
                    stats["last_valid_email"] = key

        if stats is not None:
            stats["combinations_valid"] = int(stats.get("combinations_valid") or 0) + combos_valid
            stats["sellers_with_email"] = sum(1 for f in found_by_idx if f)
            stats["offers_validated"] = stats["sellers_with_email"]
            eligible_o = int(stats.get("offers_eligible") or len(prepared))
            stats["offers_remaining"] = max(0, eligible_o - int(stats["sellers_with_email"]))

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
