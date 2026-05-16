# services/imap_idle_worker.py
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import time
from email.header import decode_header
from email.utils import parseaddr
from typing import Optional, List, Tuple, Dict

from aiogram import Bot
from sqlalchemy import select, or_
from sqlalchemy.exc import OperationalError

from database import Session
from models import EmailAccount, User

logger = logging.getLogger(__name__)

IMAP_BY_PROVIDER = {
    "gmail": ("imap.gmail.com", 993),
    "gmx": ("imap.gmx.com", 993),
    "icloud": ("imap.mail.me.com", 993),
    "outlook": ("outlook.office365.com", 993),
    "yahoo": ("imap.mail.yahoo.com", 993),
}

# как часто обновлять IDLE (обычно 29 мин лимит, берём 25)
IDLE_REFRESH_SEC = 25 * 60

# reconnect backoff
BACKOFF_BASE = 5
BACKOFF_CAP = 120

# ограничение параллельных соединений
ACCOUNT_CONCURRENCY = 10

_worker_task: Optional[asyncio.Task] = None
_account_tasks: Dict[int, asyncio.Task] = {}  # acc_id -> task


def _decode(v: str) -> str:
    if not v:
        return ""
    out = []
    for chunk, enc in decode_header(v):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", "replace"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def _imap_host(provider: str, email_addr: str) -> tuple[str, int]:
    p = (provider or "gmail").lower().strip()
    if p in IMAP_BY_PROVIDER:
        return IMAP_BY_PROVIDER[p]
    domain = email_addr.split("@")[-1]
    return f"imap.{domain}", 993


def _looks_like_spam(from_email: str, subject: str) -> bool:
    s = (from_email + " " + subject).lower()
    return any(x in s for x in ("mailer-daemon", "postmaster", "noreply", "no-reply"))


def _uid_int(x: bytes) -> Optional[int]:
    try:
        return int(x.decode("utf-8", "ignore"))
    except Exception:
        return None


def _is_invalid_credentials_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(x in s for x in (
        "invalid credentials",
        "auth failed",
        "web login required",
        "application-specific password",
        "username and password not accepted",
        "please log in via your web browser",
    ))


async def _delete_email_account(acc_id: int) -> None:
    try:
        async with Session() as session:
            acc = (await session.execute(select(EmailAccount).where(EmailAccount.id == int(acc_id)))).scalars().first()
            if acc:
                await session.delete(acc)
                await session.commit()
    except Exception:
        logger.exception("Failed to delete EmailAccount id=%s", acc_id)


async def _set_last_uid(acc_id: int, last_uid: int | None) -> None:
    try:
        async with Session() as session:
            acc = (await session.execute(select(EmailAccount).where(EmailAccount.id == int(acc_id)))).scalars().first()
            if not acc:
                return
            acc.last_seen_uid = int(last_uid) if last_uid is not None else None
            await session.commit()
    except Exception:
        logger.exception("Failed to set last_seen_uid acc_id=%s", acc_id)


def _search_new_uids(M: imaplib.IMAP4_SSL, last_uid: Optional[int]) -> tuple[list[int], Optional[int]]:
    # get all uids
    st, data = M.uid("search", None, "ALL")
    if st != "OK":
        return [], last_uid
    uids = [u for u in (_uid_int(x) for x in (data[0] or b"").split()) if u is not None]
    if not uids:
        return [], last_uid
    max_uid = max(uids)
    if last_uid is None:
        return [], max_uid
    new_uids = [u for u in uids if u > last_uid]
    return sorted(new_uids), max_uid


def _fetch_mail(M: imaplib.IMAP4_SSL, uid: int) -> tuple[str, str, str, str]:
    st, fetched = M.uid("fetch", str(uid).encode(), "(RFC822)")
    if st != "OK" or not fetched or not fetched[0]:
        return "", "", "", ""
    raw = fetched[0][1]
    msg = email.message_from_bytes(raw)
    from_name, from_email = parseaddr(msg.get("From", "") or "")
    subj = _decode(msg.get("Subject", "") or "")
    return from_email.strip(), _decode(from_name), subj, (msg.get("Date", "") or "")


async def _idle_account_loop(bot: Bot, acc: EmailAccount, tg_id: int, sem: asyncio.Semaphore) -> None:
    backoff = BACKOFF_BASE
    last_uid: Optional[int] = getattr(acc, "last_seen_uid", None)

    while True:
        async with sem:
            try:
                host, port = _imap_host(acc.provider, acc.email)
                M = imaplib.IMAP4_SSL(host, port, timeout=30)
                M.login(acc.email, (acc.password or "").strip())
                M.select("INBOX", readonly=True)

                # init last_uid on first connect
                new_uids, max_uid = _search_new_uids(M, last_uid)
                if last_uid is None and max_uid is not None:
                    last_uid = max_uid
                    await _set_last_uid(acc.id, last_uid)

                backoff = BACKOFF_BASE  # reset after successful connect
                last_idle_refresh = time.time()

                while True:
                    # start IDLE
                    tag = M._new_tag()
                    M.send(f"{tag} IDLE\r\n".encode())
                    # wait for "+ idling"
                    line = M.readline()
                    if not line or b"idling" not in line.lower():
                        raise RuntimeError(f"IDLE not accepted: {line!r}")

                    # wait server responses until refresh timeout
                    while True:
                        # refresh IDLE periodically
                        if time.time() - last_idle_refresh > IDLE_REFRESH_SEC:
                            break
                        # IMAP socket is blocking: read with timeout by setting sock timeout
                        try:
                            M.sock.settimeout(15)
                            resp = M.readline()
                        except Exception:
                            resp = b""

                        if not resp:
                            # timeout -> continue waiting
                            continue

                        rlow = resp.lower()
                        # EXISTS / RECENT => break idle to fetch
                        if b"exists" in rlow or b"recent" in rlow:
                            break

                    # stop IDLE
                    try:
                        M.send(b"DONE\r\n")
                        # read completion
                        _ = M.readline()
                    except Exception:
                        pass

                    # fetch new mails
                    new_uids, max_uid = _search_new_uids(M, last_uid)
                    if max_uid is not None and max_uid != last_uid:
                        last_uid = max_uid
                        await _set_last_uid(acc.id, last_uid)

                    for uid in new_uids[:10]:
                        from_email, from_name, subject, _date = _fetch_mail(M, uid)
                        if not from_email and not subject:
                            continue
                        if _looks_like_spam(from_email, subject):
                            continue
                        try:
                            await bot.send_message(
                                chat_id=tg_id,
                                text=f"📩 <b>{subject or '—'}</b>\n{from_email}",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                    last_idle_refresh = time.time()

            except Exception as e:
                if _is_invalid_credentials_error(e):
                    logger.error("IMAP invalid credentials %s -> deleting", acc.email)
                    await _delete_email_account(acc.id)
                    try:
                        await bot.send_message(
                            chat_id=tg_id,
                            text=f"🗑️ IMAP аккаунт удалён: <code>{acc.email}</code>\nПричина: invalid credentials / web login required",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    return

                logger.warning("IMAP IDLE error for %s: %r", acc.email, e)

        # reconnect backoff (outside semaphore)
        await asyncio.sleep(backoff)
        backoff = min(BACKOFF_CAP, backoff * 2)


async def start_imap_idle_worker(bot: Bot) -> None:
    """
    Стартует IDLE воркер: создаёт per-account задачи и следит за изменениями списка аккаунтов.
    """
    global _worker_task
    if _worker_task and not _worker_task.done():
        return

    sem = asyncio.Semaphore(ACCOUNT_CONCURRENCY)

    async def _supervisor():
        while True:
            try:
                async with Session() as session:
                    accounts = (await session.execute(
                        select(EmailAccount).where(
                            or_(EmailAccount.status.is_(None), EmailAccount.status.in_(["active", "enabled", "proxy_error"]))
                        )
                    )).scalars().all()
                    users = (await session.execute(select(User))).scalars().all()
                    users_by_id = {u.id: u.telegram_id for u in users}

                # стартуем новые задачи
                for acc in accounts:
                    if acc.id in _account_tasks and not _account_tasks[acc.id].done():
                        continue
                    tg_id = users_by_id.get(acc.user_id)
                    if not tg_id:
                        continue
                    _account_tasks[acc.id] = asyncio.create_task(_idle_account_loop(bot, acc, tg_id, sem))

                # чистим умершие
                dead = [k for k, t in _account_tasks.items() if t.done()]
                for k in dead:
                    _account_tasks.pop(k, None)

            except Exception:
                logger.exception("IMAP IDLE supervisor error")

            await asyncio.sleep(20)

    _worker_task = asyncio.create_task(_supervisor())
    logger.info("IMAP IDLE worker started (concurrency=%s)", ACCOUNT_CONCURRENCY)
