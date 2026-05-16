from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from aiogram import Bot
from sqlalchemy import select

from database import Session
from models import User
from services.validemail_fast import validate_emails_fast

logger = logging.getLogger(__name__)

HARD_BLACKLIST = ["bruno", "pierre", "evelyn", "marco", "peter", "tom", "hans", "claude"]
DEFAULT_VALIDEMAIL_URL = "https://validemail.co/api/v1/validate"


@dataclass
class ValidationConfig:
    validemail_api_key: str | None = None
    validemail_api_keys: list[str] | None = None
    validation_url: str = DEFAULT_VALIDEMAIL_URL

    concurrency: int = 12
    max_emails_per_seller: int = 4
    min_len: int = 3
    max_len: int = 32
    require_first_and_last: bool = False

    user_blacklist: list[str] | None = None
    use_ssl_verify: bool = True


# -------------------------
# Helpers: name normalization
# -------------------------

def _strip_accents(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _normalize_name(raw: str) -> str:
    if not raw:
        return ""
    s = " ".join(str(raw).strip().split())
    s = s.replace("ß", "ss").replace("ẞ", "SS")
    s = _strip_accents(s)
    s = s.replace("’", "'").replace("`", "'")
    return s


_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]{2,}", re.UNICODE)


def _pick_alpha_tokens(name: str) -> list[str]:
    """Буквенные токены имени (>=2 букв), без мусора в конце строки."""
    s = _normalize_name(name)
    if not s:
        return []

    s2 = re.sub(r"[^A-Za-z0-9\.\s-]", " ", s)
    parts = [p for p in re.split(r"\s+", s2.strip()) if p]

    alpha: list[str] = []
    for p in parts:
        m = _ALPHA_TOKEN_RE.search(p)
        if m:
            alpha.append(m.group(0))
    return alpha


def _pick_first_last_alpha_tokens(name: str) -> tuple[str, str]:
    tokens = _pick_alpha_tokens(name)
    if len(tokens) < 2:
        return "", ""
    return tokens[0], tokens[-1]


def _name_is_usable(name: str, *, require_first_and_last: bool) -> bool:
    tokens = _pick_alpha_tokens(name)
    if require_first_and_last:
        return len(tokens) >= 2
    return len(tokens) >= 1


def _name_has_first_last(name: str) -> bool:
    return _name_is_usable(name, require_first_and_last=True)


def _make_local_part_from_name(name: str, *, require_first_and_last: bool) -> str:
    """
    local-part для email: first.last или одно слово, если фамилии нет.
    """
    tokens = _pick_alpha_tokens(name)
    if require_first_and_last:
        if len(tokens) < 2:
            return ""
        first, last = tokens[0], tokens[-1]
        local = f"{first}.{last}"
    else:
        if not tokens:
            return ""
        if len(tokens) == 1:
            local = tokens[0]
        else:
            local = f"{tokens[0]}.{tokens[-1]}"
    local = re.sub(r"\.+", ".", local).strip(".")
    return local.lower()


def _make_local_part_first_last(name: str) -> str:
    return _make_local_part_from_name(name, require_first_and_last=True)


def _is_blacklisted(name: str, user_blacklist: Iterable[str] | None) -> bool:
    if not name:
        return True
    n = _normalize_name(name).lower()
    bl = list(HARD_BLACKLIST)
    if user_blacklist:
        bl.extend([str(x).strip().lower() for x in user_blacklist if str(x).strip()])

    words = set(re.split(r"\s+", re.sub(r"[^a-z0-9\s-]", " ", n)))
    for b in bl:
        if not b:
            continue
        if b == n or b in words:
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
            }
        )

    # 1) подготовка офферов (имя из JSON + blacklist + длина local-part)
    prepared: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        raw_name = str(
            it.get("item_person_name")
            or it.get("person_name")
            or it.get("name")
            or it.get("seller")
            or ""
        ).strip()

        if not _name_is_usable(raw_name, require_first_and_last=require_fl):
            continue
        if _is_blacklisted(raw_name, user_blacklist):
            continue

        local = _make_local_part_from_name(raw_name, require_first_and_last=require_fl)
        if not local:
            continue

        ln = _len_for_limits(local)
        if not (int(cfg.min_len) <= ln <= int(cfg.max_len)):
            continue

        prepared.append({
            "raw": it,
            "person_name": raw_name,
            "local": local,
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

    # общий прогресс (примерный): максимум prepared*domains
    overall_total = len(prepared) * len(domains_clean)
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

    # 2) идём по доменам по приоритету (один домен = один батч)
    for dom in domains_clean:
        # индексы офферов, которым ещё нужны email
        need_idx = [i for i, f in enumerate(found_by_idx) if len(f) < per_seller_limit]
        if not need_idx:
            break

        # собираем кандидатов только для нужных офферов и только для текущего домена
        batch_emails: list[str] = []
        email_to_idx: dict[str, int] = {}
        for i in need_idx:
            local = prepared[i]["local"]
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

        if stats is not None:
            stats["emails_checked"] = overall_done
            stats["offers_validated"] = sum(1 for f in found_by_idx if f)
            total_o = int(stats.get("offers_total") or 0)
            stats["offers_remaining"] = max(0, total_o - int(stats["offers_validated"]))

        for e, ok, _raw in results:
            if not ok:
                continue
            key = (e or "").strip().lower()
            idx = email_to_idx.get(key)
            if idx is None:
                continue
            lst = found_by_idx[idx]
            if len(lst) < per_seller_limit and key not in lst:
                lst.append(key)

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
