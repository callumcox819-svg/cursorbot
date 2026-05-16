from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot
from sqlalchemy import select

from config import config
from database import Session
from models import EmailAccount, IncomingMail, User, Offer
from services.smtp_proxy_send import send_email_via_account_with_proxy
from services.user_settings import get_user_setting

logger = logging.getLogger(__name__)

AUTO_CFG_KEY = "auto_reply_cfg"
COUNTRY_KEY = "country"
GAG_SERVICE_KEY = "gag_service"  # tutti_ch / post_ch

# Placeholders
LINK_PLACEHOLDER_RE = re.compile(r"\{\{\s*LINK\s*\}\}", re.I)
GEN_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}", re.I)


def _country_html_dir(code: str) -> Path:
    c = (code or "").strip().upper() or "DE"
    # folders: data/HTMLde, data/HTMLno, data/HTMLch
    return Path("data") / ("HTML" + c.lower())


def _normalize_subject(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()
    return s


def _split_csv_words(s: str) -> list[str]:
    raw = (s or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _extract_reply_only_preview(raw: str) -> str:
    if not raw:
        return ""
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    markers = [
        "\nOn ",
        "On ",
        "\nAm ",
        "Am ",
        "\nLe ",
        "Le ",
        "\n-----Original Message-----",
        "\nFrom:",
        "\nОт:",
    ]
    cut_pos = None
    for mk in markers:
        p = txt.find(mk)
        if p != -1:
            cut_pos = p if cut_pos is None else min(cut_pos, p)
    if cut_pos is not None:
        txt = txt[:cut_pos]

    lines: list[str] = []
    for line in txt.split("\n"):
        if line.strip().startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


async def _load_html_template(session: Session, user: User, name: str) -> str:
    country = ((await get_user_setting(session, user, COUNTRY_KEY)) or "DE").strip().upper() or "DE"
    cdir = _country_html_dir(country)
    fallback_dir = Path("data") / "html"

    file_name = (name or "").strip() or "confirmation.html"

    # CH: service-specific folders first
    if country == "CH":
        service = ((await get_user_setting(session, user, GAG_SERVICE_KEY)) or "").strip()
        if service:
            p_service = cdir / service / file_name
            if p_service.exists():
                try:
                    return p_service.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    return ""

    # default country folder
    p = cdir / file_name
    if not p.exists():
        p = fallback_dir / file_name

    # fallback to confirmation.html
    if not p.exists():
        p = cdir / "confirmation.html"
    if not p.exists():
        p = fallback_dir / "confirmation.html"

    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _apply_link_placeholders(html_text: str, link: str, ctx: dict[str, str] | None = None) -> str:
    """Apply placeholders in HTML (or plain text).

    Always replaces {{LINK}} if link present.
    If ctx provided, replaces {{ITEM_TITLE}}, {{PRICE}}, {{BUYER_NAME}}, {{ADDRESS}}, {{IMAGE_URL}}, {{IMAGE}}.
    {{IMAGE}} expands into an <img ...> tag (email-safe).
    """
    if not html_text:
        return ""

    out = html_text

    if link:
        out = LINK_PLACEHOLDER_RE.sub(link, out)

    if not ctx:
        return out

    def _img_tag(url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        return (
            f'<img src="{u}" alt="" '
            'style="display:block;width:100%;max-width:160px;height:auto;border-radius:6px;border:0;" />'
        )

    def _repl(m: re.Match) -> str:
        key = (m.group(1) or "").strip().upper()
        if not key:
            return m.group(0)
        if key == "IMAGE":
            return _img_tag(ctx.get("IMAGE_URL", ""))
        val = ctx.get(key, "")
        return val if val is not None else ""

    return GEN_PLACEHOLDER_RE.sub(_repl, out)


def _render_text_template(template: str, meta: dict[str, Any]) -> str:
    link = (meta.get("generated_link") or meta.get("ad_url") or "").strip()
    from_name = (meta.get("from_name") or "").strip()
    subject = (meta.get("subject") or "").strip()

    out = (template or "")
    out = out.replace("{LINK}", link)
    out = out.replace("{FROM_NAME}", from_name)
    out = out.replace("{SUBJECT}", subject)
    return out.strip()


@dataclass
class _Tpl:
    title: str
    text: str


async def _load_templates(tg_id: int) -> list[_Tpl]:
    from handlers.templates import load_templates

    return [_Tpl(title=t.title, text=t.text) for t in await load_templates(int(tg_id))]


def _match_keywords(*, text: str, keywords: list[str], stopwords: list[str]) -> bool:
    t = (text or "").lower()
    if not keywords:
        return False
    if stopwords and any(sw.lower() in t for sw in stopwords if sw):
        return False
    return any(kw.lower() in t for kw in keywords if kw)


def _matched_keywords(*, text: str, keywords: list[str]) -> list[str]:
    """Return a stable list of matched keywords (case-insensitive substring match)."""
    t = (text or "").lower()
    out: list[str] = []
    for kw in keywords or []:
        k = (kw or "").strip()
        if not k:
            continue
        if k.lower() in t:
            out.append(k)
    # keep deterministic order, no dups
    uniq: list[str] = []
    seen: set[str] = set()
    for k in out:
        kk = k.lower()
        if kk not in seen:
            uniq.append(k)
            seen.add(kk)
    return uniq


async def _tg_report(bot: Bot, tg_id: int, tg_msg_id: int, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=int(tg_id),
            text=text,
            reply_to_message_id=(int(tg_msg_id) if tg_msg_id else None),
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("[AUTO] Failed to send TG report")


async def handle_auto_for_mail(mail_id: int, meta: dict) -> None:
    """Auto action for IncomingMail.

    - Priority: auto_send > auto_reply
    - HTML is sent ONLY if generated_link exists
    - Country-based HTML folders, CH service subfolders
    """
    async with Session() as session:
        mail = (await session.execute(select(IncomingMail).where(IncomingMail.id == int(mail_id)).limit(1))).scalars().first()
        if not mail:
            return

        user = (await session.execute(select(User).where(User.id == int(mail.user_id)).limit(1))).scalars().first()
        if not user:
            return

        acc = (await session.execute(select(EmailAccount).where(EmailAccount.id == int(mail.account_id)).limit(1))).scalars().first()
        if not acc:
            return

        cfg_raw = await get_user_setting(session, user, AUTO_CFG_KEY)
        if not cfg_raw:
            return
        try:
            cfg = json.loads(cfg_raw)
        except Exception:
            return
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", False)):
            return

        lines = cfg.get("lines") or []
        if not isinstance(lines, list) or not lines:
            return

        try:
            idx = int(cfg.get("current_line", 0) or 0)
        except Exception:
            idx = 0
        idx = max(0, min(idx, len(lines) - 1))
        ln = lines[idx] if isinstance(lines[idx], dict) else {}
        if not bool(ln.get("enabled", True)):
            return

        keywords = _split_csv_words(str(ln.get("keywords") or ""))
        stopwords = _split_csv_words(str(ln.get("stopwords") or ""))

        subject = (meta.get("subject") or mail.subject or "")
        body = (meta.get("body") or mail.body or "")
        preview = _extract_reply_only_preview(str(body))
        match_text = f"{subject}\n{preview}".strip()
        if not _match_keywords(text=match_text, keywords=keywords, stopwords=stopwords):
            return

        # --- TG report: which line matched + matched keywords (как в "идеальном" боте из видео)
        bot = Bot(token=config.BOT_TOKEN)
        tg_id = int(getattr(user, "telegram_id", 0) or 0)
        tg_msg_id = int(meta.get("tg_msg_id") or 0)

        try:
            line_no = idx + 1
            await _tg_report(bot, tg_id, tg_msg_id, f"✅ {line_no}-я линия активна")
            mkeys = _matched_keywords(text=match_text, keywords=keywords)
            if mkeys:
                await _tg_report(
                    bot,
                    tg_id,
                    tg_msg_id,
                    f"{line_no}-я линия. найдены кейворды:\n\n" + "\n".join(f"-{k}" for k in mkeys),
                )
        except Exception:
            # отчёты не должны ломать авто-ответ
            logger.exception("[AUTO] Failed to send line reports")

        # --- Auto price adjust (как в видео): прибавить price_add к цене оффера
        try:
            price_add = int((ln.get("price_add") or 0) or 0)
        except Exception:
            price_add = 0

        if price_add and getattr(mail, "resolved_offer_id", None):
            try:
                offer0 = (await session.execute(select(Offer).where(Offer.id == int(mail.resolved_offer_id)).limit(1))).scalars().first()
                if offer0:
                    # parse numeric part from stored price like "€ 20.00" or "20" etc
                    rawp = (offer0.price or "").strip()
                    m = re.search(r"([0-9]+(?:[\.,][0-9]+)?)", rawp)
                    cur = float(m.group(1).replace(",", ".")) if m else 0.0
                    newp = cur + float(price_add)
                    offer0.price = f"€ {newp}"
                    await session.commit()
                    await _tg_report(bot, tg_id, tg_msg_id, f"Цена товара с ID {offer0.id} была авто-изменена на € {newp}")
            except Exception:
                logger.exception("[AUTO] Failed to auto-change offer price")


        # timings
        t = ln.get("timings") or {}
        try:
            t_gen = int((t.get("gen_link") or 0) or 0)
        except Exception:
            t_gen = 0
        try:
            t_tpl = int((t.get("send_template") or 0) or 0)
        except Exception:
            t_tpl = 0
        try:
            t_rep = int((t.get("send_reply") or 0) or 0)
        except Exception:
            t_rep = 0

        auto_create_link = bool(cfg.get("auto_create_link", False)) and bool(ln.get("gen_link_enabled", True))
        auto_send = bool(cfg.get("auto_send", False))

        ad_url = (meta.get("ad_url") or mail.ad_url or "")
        generated_link = (meta.get("generated_link") or mail.generated_link or "")

        # (do not force link generation here - keep project behavior)
        if auto_create_link and t_gen > 0:
            await asyncio.sleep(min(max(t_gen, 0), 120))

        # bot/tg_id/tg_msg_id уже использованы выше для отчётов; создадим заново безопасно
        # bot/tg_id/tg_msg_id already defined above

        # 1) auto_send (text template)
        if auto_send:
            tpl_items = await _load_templates(int(getattr(user, "telegram_id", 0) or 0))
            tpl_idx = ln.get("template_idx", None)
            body_text = ""
            if tpl_idx is not None:
                try:
                    ti = int(tpl_idx)
                except Exception:
                    ti = -1
                if 0 <= ti < len(tpl_items):
                    body_text = _render_text_template(
                        tpl_items[ti].text,
                        {**meta, "ad_url": ad_url, "generated_link": generated_link},
                    )

            if body_text:
                if t_tpl > 0:
                    await asyncio.sleep(min(max(t_tpl, 0), 120))

                out_subject = _normalize_subject(subject)
                out_subject = f"Re: {out_subject}" if out_subject else "Re:"

                ok, err = await send_email_via_account_with_proxy(
                    session,
                    int(user.id),
                    acc,
                    (mail.from_email or "").strip(),
                    out_subject,
                    body_text,
                    sender_name=(getattr(user, "sender_name", None) or None),
                )
                if ok:
                    await _tg_report(bot, tg_id, tg_msg_id, "✅ Авто-отправка выполнена")
                else:
                    await _tg_report(bot, tg_id, tg_msg_id, f"❌ Авто-отправка: ошибка\n{(err or '').strip()}")
                return

        # 2) auto_reply (HTML)
        if t_rep > 0:
            await asyncio.sleep(min(max(t_rep, 0), 120))

        html_name = (ln.get("html_template") or "pickup.html").strip() or "pickup.html"

        if not generated_link:
            await _tg_report(bot, tg_id, tg_msg_id, "ℹ️ Ссылка не была сгенерирована — HTML не отправлен.")
            return

        # Build ctx from DB (Offer) + CH profile
        offer = None
        if getattr(mail, "resolved_offer_id", None):
            offer = (await session.execute(select(Offer).where(Offer.id == int(mail.resolved_offer_id)).limit(1))).scalars().first()

        item_title = ((offer.title if offer else None) or (meta.get("item_title") if isinstance(meta, dict) else None) or "").strip()
        price = ((offer.price if offer else None) or (meta.get("price") if isinstance(meta, dict) else None) or "").strip()
        image_url = ((offer.photo if offer else None) or (meta.get("image") if isinstance(meta, dict) else None) or "").strip()

        buyer_name = (meta.get("buyer_name") or "").strip() if isinstance(meta, dict) else ""
        address = (meta.get("address") or "").strip() if isinstance(meta, dict) else ""

        country = ((await get_user_setting(session, user, COUNTRY_KEY)) or "").strip().upper()
        if country == "CH":
            prof_name = ((await get_user_setting(session, user, "gag_profile_name")) or "").strip()
            prof_addr = ((await get_user_setting(session, user, "gag_profile_address")) or "").strip()
            if not buyer_name:
                buyer_name = prof_name
            if not address:
                address = prof_addr

        ctx = {
            "ITEM_TITLE": item_title,
            "PRICE": price,
            "BUYER_NAME": buyer_name,
            "ADDRESS": address,
            "IMAGE_URL": image_url,
        }

        html_tpl = await _load_html_template(session, user, html_name)
        html_out = _apply_link_placeholders(html_tpl, generated_link, ctx)
        if not html_out.strip():
            await _tg_report(bot, tg_id, tg_msg_id, "❌ HTML-шаблон пустой или не найден")
            return

        out_subject = _normalize_subject(subject)
        out_subject = f"Re: {out_subject}" if out_subject else "Re:"

        ok, err = await send_email_via_account_with_proxy(
            session,
            int(user.id),
            acc,
            (mail.from_email or "").strip(),
            out_subject,
            html_out,
            sender_name=(getattr(user, "sender_name", None) or None),
            is_html=True,
        )

        if ok:
            await _tg_report(bot, tg_id, tg_msg_id, "✅ Авто-ответ отправлен")
        else:
            await _tg_report(bot, tg_id, tg_msg_id, f"❌ Авто-ответ: ошибка\n{(err or '').strip()}")
