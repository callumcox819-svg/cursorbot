import json
import os
import tempfile
import time
import asyncio
import re
from typing import Any, Dict, List

from aiogram import Router, F
from aiogram.types import Message, FSInputFile

from sqlalchemy import select, delete

from database import Session
from models import Offer, OfferEmail, Domain
from services.users import get_or_create_user
from config import config
from services.validemail_keys import resolve_validemail_api_keys
from services.validemail_validator import ValidationConfig, validate_offers
from services.offer_storage import save_all_offers_from_import

router = Router()

REPLACE_OLD_FOR_USER = True
REQUIRE_FIRST_AND_LAST = False
PROGRESS_UPDATE_INTERVAL = 20  # seconds


def _norm_email(e: str) -> str:
    """Нормализация email для сохранения/поиска.

    - lower + strip
    - googlemail.com -> gmail.com
    - для gmail: убираем +tag (first.last+tag@gmail.com)
    """
    s = (e or "").strip().lower()
    if not s or "@" not in s:
        return ""
    local, domain = s.split("@", 1)
    domain = domain.strip()
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com" and "+" in local:
        local = local.split("+", 1)[0]
    local = local.strip()
    if not local:
        return ""
    return f"{local}@{domain}"


def _collect_raw_emails(raw: dict) -> list[str]:
    """Достаём "реальные" email из сырого item (если они там есть).

    Это НЕ меняет логику валидации. Только помогает потом найти Offer.link
    по фактическому from_email входящего письма.
    """
    out: list[str] = []
    if not isinstance(raw, dict):
        return out

    # самые явные поля
    for key in ("email", "seller_email", "contact_email", "from_email", "owner_email", "account_email"):
        v = raw.get(key)
        if isinstance(v, str) and "@" in v:
            out.append(v)

    # иногда прилетает списком
    v2 = raw.get("emails")
    if isinstance(v2, list):
        for x in v2:
            if isinstance(x, str) and "@" in x:
                out.append(x)

    v3 = raw.get("validated_emails")
    if isinstance(v3, list):
        for x in v3:
            if isinstance(x, str) and "@" in x:
                out.append(x)

    return out


# ===================== LOADERS =====================


async def _load_json_from_telegram_doc(message: Message) -> Any:
    file = await message.bot.download(message.document)
    raw = file.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return json.loads(raw.decode("latin-1"))


async def _load_text_from_telegram_doc(message: Message) -> str:
    file = await message.bot.download(message.document)
    raw = file.read()
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("latin-1")


# ===================== PARSERS =====================

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+")


def _normalize_person_name(raw_name: str) -> str:
    """Нормализовать имя продавца для отображения (1+ слово)."""
    s = (raw_name or "").strip()
    if not s:
        return ""
    words = _WORD_RE.findall(s)
    if not words:
        return s
    if len(words) == 1:
        return words[0]
    return f"{words[0]} {words[-1]}"


def _normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Нормализация структуры items из JSON.

    Здесь мы аккуратно синхронизируем ключевые поля:
    - person_name/name/item_person_name -> нормализованный person_name
    - title/item_title
    - price/item_price
    - link/item_link
    Ничего "умного" не изобретаем, просто приводим к единому виду.
    """
    out: List[Dict[str, Any]] = []
    for x in items or []:
        raw_name = str(
            x.get("person_name")
            or x.get("name")
            or x.get("item_person_name")
            or ""
        ).strip()

        norm = _normalize_person_name(raw_name)
        y = dict(x)

        if norm:
            y["name"] = norm
            y["person_name"] = norm

        # подстрахуем поля под наш pipeline
        if "title" not in y and isinstance(x.get("item_title"), str):
            y["title"] = x["item_title"]
        if "link" not in y and isinstance(x.get("item_link"), str):
            y["link"] = x["item_link"]
        if "price" not in y and isinstance(x.get("item_price"), (str, int, float)):
            y["price"] = str(x["item_price"])

        out.append(y)
    return out


def _extract_items(data: Any) -> List[Dict[str, Any]]:
    """Вытащить список офферов из произвольного JSON."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("items"), list):
            return data["data"]["items"]
    return []


def _parse_txt_offers(text: str) -> List[Dict[str, Any]]:
    """Парсер txt-файла в список словарей (fallback-формат)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # блоки отделяем пустыми строками
    blocks = [b.strip() for b in t.split("\n\n") if b.strip()]

    items: List[Dict[str, Any]] = []
    for block in blocks:
        title = ""
        seller = ""

        for line in block.split("\n"):
            s = line.strip()
            if not s:
                continue
            if "Продавец" in s or s.startswith("💼"):
                seller = s.split(":", 1)[-1].strip()
            elif not title and not s.startswith("🔗"):
                title = s.lstrip("📱").strip()

        m = re.search(r"(https?://[^\s\)]+)", block)
        link = m.group(1).strip() if m else ""

        if title or link:
            items.append(
                {
                    "title": title,
                    "item_title": title,
                    "person_name": seller,
                    "item_person_name": seller,
                    "link": link,
                    "item_link": link,
                }
            )

    return items


# ===================== MAIN HANDLER =====================


@router.message(F.document)
async def validation_handler(message: Message):
    ext = (message.document.file_name or "").lower()
    if not ext.endswith((".json", ".txt")):
        return await message.answer("❌ Пришли файл .json или .txt")

    status_msg = await message.answer("📥 Файл принят. Подготавливаю данные…")

    try:
        if ext.endswith(".json"):
            data = await _load_json_from_telegram_doc(message)
            items = _normalize_items(_extract_items(data))
        else:
            text = await _load_text_from_telegram_doc(message)
            items = _parse_txt_offers(text)
    except Exception as e:
        return await status_msg.edit_text(f"❌ Ошибка чтения файла: {e}")

    if not items:
        return await status_msg.edit_text("❌ В файле не найдено записей.")

    # --- UI статистика (не влияет на логику валидации) ---
    def _has_any_name(name: str) -> bool:
        good = [p for p in _WORD_RE.findall(name or "") if len(p) >= 2]
        return len(good) >= 1

    total_offers = len(items)
    offers_with_name = sum(
        1
        for it in items
        if _has_any_name(
            str(
                it.get("item_person_name")
                or it.get("person_name")
                or it.get("name")
                or it.get("seller")
                or ""
            )
        )
    )

    try:
        await status_msg.edit_text(
            "📥 Файл принят. Подготавливаю данные…\n"
            f"Всего объявлений: <b>{total_offers}</b>\n"
            f"С именем продавца: <b>{offers_with_name}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    tg_id = message.from_user.id

    async with Session() as session:
        user = await get_or_create_user(session, tg_id)

        api_keys = resolve_validemail_api_keys()

        if not api_keys:
            return await status_msg.edit_text("❌ ValidEmail API keys не заданы в config.py.")

        # ✅ Приоритет доменов: берём порядок из "Настройки -> Приоритет отправки" (user_setting: domain_priority).
        # Если приоритет не задан — используем порядок как в БД (Domain.id).
        db_domains = [
            (d.domain or "").strip().lower()
            for d in (
                await session.execute(
                    select(Domain).where(Domain.user_id == user.id).order_by(Domain.id)
                )
            ).scalars().all()
            if (d.domain or "").strip()
        ]

        # priority list can contain domains not yet in DB, and vice versa.
        priority_raw = None
        try:
            from services.user_settings import get_user_setting
            priority_raw = await get_user_setting(session, user, "domain_priority")
        except Exception:
            priority_raw = None

        # domain_priority is normally stored as JSON list (see settings.py),
        # but older DBs / migrations may contain raw text with newlines.
        priority_list = []
        if priority_raw:
            try:
                priority_list = json.loads(priority_raw)
            except Exception:
                # fallback: treat as "each domain on new line"
                priority_list = [x.strip() for x in str(priority_raw).splitlines() if x.strip()]
        if not isinstance(priority_list, list):
            priority_list = []

        pr = [str(x or "").strip().lower() for x in priority_list if str(x or "").strip()]
        seen: set[str] = set()
        domains: list[str] = []

        # Domains for validation (strict to your requirement):
        # - if user has set a priority list: validate ONLY those domains, in that exact order.
        #   They may be not saved in the Domain table — that's OK.
        # - otherwise: validate all saved domains (DB order).
        if pr:
            for d in pr:
                d = str(d or "").strip().lower()
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)
        else:
            for d in db_domains:
                d = str(d or "").strip().lower()
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)


        if not domains:
            return await status_msg.edit_text("❌ У тебя нет доменов.")

    cfg = ValidationConfig(
        validemail_api_keys=api_keys,
        validation_url=config.VALIDEMAIL_URL,
        concurrency=max(4, int(getattr(config, "VALIDEMAIL_CONCURRENCY", 12) or 12)),
        max_emails_per_seller=2,
        require_first_and_last=REQUIRE_FIRST_AND_LAST,
        max_len=32,
    )

    n_keys = len(api_keys)
    progress_msg = await message.answer(
        f"🔎 Запуск валидации…\n"
        f"Ключей API: <b>{n_keys}</b> | параллельно: <b>{cfg.concurrency}</b>",
        parse_mode="HTML",
    )

    live_stats: dict = {}
    state = {
        "done": 0,
        "total": 0,
        "limit": 0,
        "in_use": 0,
        "t0": time.time(),
        "offers_total": total_offers,
        "offers_with_name": offers_with_name,
        "last_text": "",
    }
    stop_evt = asyncio.Event()

    def _progress_cb(done: int, total: int, limit: int, in_use: int) -> None:
        # коллбек вызывается из валидатора; не await
        state["done"] = int(done or 0)
        state["total"] = int(total or 0)
        state["limit"] = int(limit or 0)
        state["in_use"] = int(in_use or 0)

    def _progress_bar(done: int, total: int, width: int = 22) -> tuple[str, int]:
        if total <= 0:
            return "", 0
        pct = int((done / total) * 100)
        filled = int((done / total) * width)
        filled = max(0, min(width, filled))
        return ("█" * filled + "░" * (width - filled)), pct

    def _fmt_eta(seconds: float) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}ч {m:02d}м"
        return f"{m}м {s:02d}с"

    async def _progress_updater(
        msg: Message, st: dict, stop: asyncio.Event, vstats: dict
    ) -> None:
        while not stop.is_set():
            done = int(st.get("done", 0) or 0)
            total = int(st.get("total", 0) or 0)
            limit = int(st.get("limit", 0) or 0)
            in_use = int(st.get("in_use", 0) or 0)
            offers_total_ = int(vstats.get("offers_total") or st.get("offers_total") or 0)
            validated_ = int(vstats.get("offers_validated") or 0)
            remaining_ = int(
                vstats.get("offers_remaining")
                if vstats.get("offers_remaining") is not None
                else max(0, offers_total_ - validated_)
            )
            eligible_ = int(vstats.get("offers_eligible") or st.get("offers_with_name") or 0)

            bar, pct = ("", 0)
            if total > 0:
                bar, pct = _progress_bar(done, total)
            elapsed = max(0.1, time.time() - float(st.get("t0") or time.time()))
            rate = done / elapsed if done > 0 else 0.0
            eta = (total - done) / rate if total > 0 and rate > 0 else 0

            text = (
                "🔎 <b>Валидация email</b>\n\n"
                f"👁 Просмотрено: <b>{offers_total_}</b>\n"
                f"📝 С именем (проверяем): <b>{eligible_}</b>\n"
                f"✅ Валидировано: <b>{validated_}</b>\n"
                f"⏳ Осталось: <b>{remaining_}</b>\n"
            )
            if total > 0:
                text += (
                    f"\n{bar} {pct}%\n"
                    f"Проверено адресов: {done}/{total}\n"
                    f"ETA: ~{_fmt_eta(eta)}\n"
                    f"Потоков: {in_use}/{(limit or 0)}"
                )
            if text != st.get("last_text"):
                try:
                    await msg.edit_text(text, parse_mode="HTML")
                    st["last_text"] = text
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)

    updater = asyncio.create_task(
        _progress_updater(progress_msg, state, stop_evt, live_stats)
    )

    try:
        validated = await validate_offers(
            items, domains, cfg, progress_cb=_progress_cb, stats=live_stats
        )
    finally:
        stop_evt.set()
        await updater

    validated_count = len(validated or [])

    await progress_msg.edit_text(
        "💾 Сохраняю все объявления в базу…\n"
        f"В файле: <b>{total_offers}</b> · с email: <b>{validated_count}</b>",
        parse_mode="HTML",
    )

    async with Session() as session:
        user = await get_or_create_user(session, tg_id)

        if REPLACE_OLD_FOR_USER:
            offer_ids = [
                o.id
                for o in (
                    await session.execute(
                        select(Offer).where(Offer.user_id == user.id)
                    )
                ).scalars().all()
            ]
            if offer_ids:
                await session.execute(
                    delete(OfferEmail).where(OfferEmail.offer_id.in_(offer_ids))
                )
            await session.execute(delete(Offer).where(Offer.user_id == user.id))
            await session.commit()

        offers_saved, offers_with_email, saved_email_count, output = await save_all_offers_from_import(
            session,
            user_id=int(user.id),
            items=items,
            validated_rows=validated or [],
            norm_email=_norm_email,
            max_emails_per_offer=2,
        )
        await session.commit()

    out_path = os.path.join(
        tempfile.gettempdir(),
        f"validated_{tg_id}_{int(time.time())}.json"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    await message.answer_document(
        FSInputFile(out_path),
        caption=(
            f"💾 Сохранено в БД: {offers_saved}/{total_offers}\n"
            f"✅ С валидным email: {offers_with_email}\n"
            f"⏳ Без email (данные есть): {max(0, offers_saved - offers_with_email)}\n"
            f"📧 Email записей: {saved_email_count}"
        ),
    )
