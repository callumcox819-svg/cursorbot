"""Проверка SOCKS5 перед рассылкой и периодически во время /send."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from aiogram import Bot
from sqlalchemy import select

from database import db_session
from models import Proxy
from proxy_manager import is_socks5_proxy
from services.proxy_verify import refresh_proxies_status

logger = logging.getLogger(__name__)

MAIL_PROXY_RECHECK_SEC = max(60, min(600, int(os.getenv("MAIL_PROXY_RECHECK_SEC", "120"))))
MAIL_PROXY_PREFLIGHT_TIMEOUT = max(18, min(45, int(os.getenv("MAIL_PROXY_PREFLIGHT_TIMEOUT", "28"))))
MAIL_PROXY_PREFLIGHT_CONCURRENCY = max(
    1, min(4, int(os.getenv("MAIL_PROXY_PREFLIGHT_CONCURRENCY", "2")))
)


@dataclass(frozen=True)
class ProxyHealthSummary:
    total: int
    ok: int
    unknown: int
    bad: int

    def format_lines(self) -> str:
        return (
            f"SOCKS5: <b>{self.total}</b> · 🟢 SMTP OK: <b>{self.ok}</b> · "
            f"🟡 неясно: <b>{self.unknown}</b> · 🔴 мёртв при рассылке: <b>{self.bad}</b>"
        )


async def summarize_proxy_health(session, user_id: int) -> ProxyHealthSummary:
    rows = list(
        (await session.execute(select(Proxy).where(Proxy.user_id == int(user_id)))).scalars().all()
    )
    socks = [p for p in rows if is_socks5_proxy(p)]
    ok = unk = bad = 0
    for p in socks:
        if p.is_active is True:
            ok += 1
        elif p.is_active is False:
            bad += 1
        else:
            unk += 1
    return ProxyHealthSummary(len(socks), ok, unk, bad)


async def run_proxy_health_check(session, user_id: int) -> ProxyHealthSummary:
    """Туннель + smtp.gmail.com:587 — как в меню «Прокси»."""
    await refresh_proxies_status(
        session,
        int(user_id),
        concurrency=MAIL_PROXY_PREFLIGHT_CONCURRENCY,
        timeout=MAIL_PROXY_PREFLIGHT_TIMEOUT,
    )
    return await summarize_proxy_health(session, user_id)


def mailing_may_start(summary: ProxyHealthSummary) -> Tuple[bool, str]:
    if summary.total <= 0:
        return False, "Нет SOCKS5 в «Прокси»."
    if summary.ok >= 1:
        return True, summary.format_lines()
    if summary.unknown >= 1:
        return (
            True,
            summary.format_lines()
            + "\n<i>Чёткого SMTP OK нет (таймаут/сеть) — рассылка всё равно стартует.</i>",
        )
    return (
        False,
        summary.format_lines()
        + "\n\n❌ Все прокси помечены 🔴 после сбоя туннеля при рассылке. "
        "Замените их или дождитесь «Проверить прокси» (🟢).",
    )


async def preflight_proxies_for_mailing(db_user_id: int) -> Tuple[bool, ProxyHealthSummary, str]:
    async with db_session() as session:
        summary = await run_proxy_health_check(session, db_user_id)
    ok, detail = mailing_may_start(summary)
    return ok, summary, detail


async def mailing_proxy_watch_loop(
    *,
    tg_user_id: int,
    db_user_id: int,
    bot: Optional[Bot] = None,
    chat_id: Optional[int] = None,
) -> None:
    """Каждые MAIL_PROXY_RECHECK_SEC перепроверяет прокси, пока идёт рассылка."""
    from services.sending_state import get_sending_state

    last_ok = -1
    while True:
        await asyncio.sleep(MAIL_PROXY_RECHECK_SEC)
        st = get_sending_state(tg_user_id)
        if not st or not st.is_running or st.is_stopping:
            break
        # Не держим _PROXY_LOCK параллельно с SMTP-рассылкой — иначе 0/73 «зависает».
        if st.is_running:
            logger.info("skip proxy recheck during mailing tg=%s", tg_user_id)
            continue
        try:
            async with db_session() as session:
                summary = await run_proxy_health_check(session, db_user_id)
        except Exception:
            logger.exception("mailing proxy recheck failed tg=%s", tg_user_id)
            continue

        logger.info(
            "mailing proxy recheck tg=%s %s",
            tg_user_id,
            summary,
        )
        if bot and chat_id and summary.ok != last_ok:
            last_ok = summary.ok
            if summary.ok == 0 and summary.total > 0 and summary.bad == summary.total:
                try:
                    await bot.send_message(
                        int(chat_id),
                        "⚠️ <b>Перепроверка прокси</b>\n"
                        f"{summary.format_lines()}\n\n"
                        "<i>Рассылка продолжается — попытки идут по всем SOCKS5.</i>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
