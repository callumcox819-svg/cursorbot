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
from services.validemail_validator import (
    ValidationConfig,
    merge_validation_domains,
    validate_offers,
)
from services.offer_storage import save_all_offers_from_import
from services.seller_name import MIN_NAME_TOKEN_LEN, seller_name_eligible_for_validation, seller_name_from_item

router = Router()

REPLACE_OLD_FOR_USER = True
REQUIRE_FIRST_AND_LAST = False
PROGRESS_UPDATE_INTERVAL = 3  # seconds


def _progress_bar(done: int, total: int, width: int = 20) -> tuple[str, int]:
    if total <= 0:
        return "░" * width, 0
    pct = int((done / total) * 100)
    filled = max(0, min(width, int((done / total) * width)))
    return ("█" * filled + "░" * (width - filled)), pct


def _validation_user_line(message: Message) -> str:
    u = message.from_user
    if not u:
        return ""
    un = f"@{u.username}" if u.username else ""
    return f"👤 <code>{u.id}</code> {un}".strip()


def _format_validation_status(
    *,
    finished: bool,
    user_line: str,
    processed: int,
    total: int,
    added: int,
    duplicates: int,
    in_blacklist: int,
    added_blacklist: int,
    short_nicks: int,
    no_email: int,
    errors: int,
) -> str:
    title = "✅ Подбор завершён" if finished else "🔎 Подбор email…"
    bar, pct = _progress_bar(processed, total)
    lines = [
        f"<b>{title}</b>",
        user_line,
        f"<code>{bar}</code> <b>{pct}%</b>",
        "",
        f"📄 Объявлений обработано: <b>{processed}/{total}</b>",
        f"📧 Добавлено: <b>{added}</b>",
        f"♻️ Дубликатов: <b>{duplicates}</b>",
        f"⛔ В ЧС: <b>{in_blacklist}</b>",
        f"➕ Добавлено в ЧС: <b>{added_blacklist}</b>",
        f"✂️ Коротких ников: <b>{short_nicks}</b>",
        f"📬 Без email: <b>{no_email}</b>",
        f"⚠️ Ошибок: <b>{errors}</b>",
    ]
    return "\n".join(l for l in lines if l is not None)


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

    total_offers = len(items)
    user_line = _validation_user_line(message)
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
        domains = merge_validation_domains(pr + db_domains)

        if not domains:
            return await status_msg.edit_text("❌ У тебя нет доменов.")

    cfg = ValidationConfig(
        validemail_api_keys=api_keys,
        validation_url=config.VALIDEMAIL_URL,
        concurrency=max(8, int(getattr(config, "VALIDEMAIL_CONCURRENCY", 20) or 20)),
        max_emails_per_seller=3,
        require_first_and_last=REQUIRE_FIRST_AND_LAST,
        max_len=40,
        min_len=MIN_NAME_TOKEN_LEN,
    )

    live_stats: dict = {"offers_total": total_offers}
    ui_state = {"last_text": ""}
    stop_evt = asyncio.Event()

    def _progress_cb(done: int, total: int, limit: int, in_use: int) -> None:
        pass

    def _ui_from_stats(vstats: dict, *, finished: bool = False) -> str:
        total = int(vstats.get("offers_total") or total_offers)
        seller_i = int(vstats.get("seller_index") or 0)
        added = int(vstats.get("sellers_with_email") or 0)
        short_n = int(vstats.get("short_nicks") or 0)
        bl = int(vstats.get("blacklisted") or 0)
        no_name = int(vstats.get("no_name") or 0)
        dup = int(vstats.get("duplicates") or 0)
        err = int(vstats.get("api_errors") or 0)
        skip_fixed = short_n + bl + no_name
        if finished:
            processed = total
        else:
            processed = min(total, skip_fixed + seller_i)
        eligible = int(vstats.get("offers_eligible") or 0)
        if finished:
            no_email = max(0, eligible - added)
        else:
            no_email = max(0, seller_i - added)
        return _format_validation_status(
            finished=finished,
            user_line=user_line,
            processed=processed,
            total=total,
            added=added,
            duplicates=dup,
            in_blacklist=0,
            added_blacklist=bl,
            short_nicks=short_n,
            no_email=no_email,
            errors=err,
        )

    try:
        await status_msg.edit_text(
            _ui_from_stats(live_stats, finished=False), parse_mode="HTML"
        )
    except Exception:
        pass

    async def _progress_updater(msg: Message, stop: asyncio.Event, vstats: dict) -> None:
        while not stop.is_set():
            text = _ui_from_stats(vstats, finished=False)
            if text != ui_state.get("last_text"):
                try:
                    await msg.edit_text(text, parse_mode="HTML")
                    ui_state["last_text"] = text
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)

    updater = asyncio.create_task(_progress_updater(status_msg, stop_evt, live_stats))

    try:
        validated = await validate_offers(
            items, domains, cfg, progress_cb=_progress_cb, stats=live_stats
        )
    finally:
        stop_evt.set()
        await updater

    validated_count = len(validated or [])
    eligible = int(live_stats.get("offers_eligible") or 0)

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

    live_stats["sellers_with_email"] = offers_with_email
    live_stats["offers_eligible"] = eligible

    try:
        await status_msg.edit_text(
            _ui_from_stats(live_stats, finished=True), parse_mode="HTML"
        )
    except Exception:
        pass

    await message.answer_document(
        FSInputFile(out_path),
        caption=f"📎 Результат · в БД {offers_saved}/{total_offers} · email {saved_email_count}",
    )
