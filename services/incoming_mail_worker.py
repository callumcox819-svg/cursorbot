# services/incoming_mail_worker.py
from __future__ import annotations

import asyncio
import email
import html
import imaplib
import logging
import re
import select as pyselect
import threading
import time
from email.header import decode_header
from email.utils import parseaddr
from typing import Optional, List, Tuple, Dict, Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select as sa_select, or_ as sa_or, func
from sqlalchemy.exc import OperationalError

from database import Session
from models import EmailAccount, User, ConversationLink, Offer, OfferEmail, IncomingMail
from services.link_id import link_id_from_generated_url
from services.user_settings import get_user_setting
from services.offer_matching import resolve_offer_for_incoming as _resolve_offer_for_incoming

logger = logging.getLogger(__name__)

# ---- CONFIG ----
USE_IMAP_IDLE = False              # ✅ только polling
IDLE_TIMEOUT_SEC = 60
POLL_FALLBACK_SEC = 20             # ✅ раз в 20 сек
DEFAULT_MAX_PER_ACCOUNT = 10

# ---- STATE ----
_worker_task: asyncio.Task | None = None
LAST_UID: Dict[int, int] = {}
_NOTIFY_ONCE: set[str] = set()

_ERROR_STREAK: Dict[int, int] = {}
_BACKOFF_UNTIL: Dict[int, float] = {}

_LAST_EOF_LOG: Dict[int, float] = {}
_EOF_LOG_COOLDOWN_SEC = 120.0

FULL_BODIES: Dict[tuple[int, str], str] = {}
FULL_META: Dict[tuple[int, str], Dict[str, Any]] = {}

def _now() -> float:
    return time.time()


def _e(s: str) -> str:
    return html.escape(s or "")


def _canon_email(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    local = local.strip()
    domain = domain.strip().lower()
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in ("googlemail.com", "gmail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _calc_backoff(streak: int) -> int:
    if streak <= 1:
        return 1
    if streak == 2:
        return 2
    if streak == 3:
        return 4
    if streak == 4:
        return 8
    if streak == 5:
        return 15
    if streak == 6:
        return 30
    return 60


def _is_invalid_credentials_error(e: Exception) -> bool:
    s = str(e).lower()
    return "authentication failed" in s or "invalid credentials" in s or "web login required" in s


def _is_transient_ssl_eof(e: Exception) -> bool:
    s = str(e).lower()
    return "eof occurred" in s or "connection reset" in s or ("ssl" in s and "eof" in s)


def _looks_like_spam(from_email: str, from_name: str, subject: str, body: str) -> bool:
    return False


def _is_google_system_mail(from_email: str, from_name: str, subject: str) -> bool:
    """Системные письма Google (безопасность, уведомления) — в Telegram не шлём."""
    f = (from_email or "").strip().lower()
    name = (from_name or "").strip().lower()
    subj = (subject or "").strip().lower()
    if name == "google":
        return True
    if not f or "@" not in f:
        return False
    local, _, domain = f.rpartition("@")
    if domain in ("google.com", "accounts.google.com", "googlemail.com"):
        return True
    if domain.endswith(".google.com"):
        return True
    if "accounts.google" in domain:
        return True
    if domain == "google.com" and local in (
        "no-reply",
        "noreply",
        "mail-noreply",
        "notification",
        "notifications",
    ):
        return True
    if "keamanan" in subj or "security" in subj and "google" in f:
        return True
    return False


def _extract_ad_link(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(https?://[^\s<>\"']+)", text)
    if not m:
        return None
    return m.group(1).strip()


def _is_smtp_block_bounce(from_email: str, subject: str, body: str) -> bool:
    s = (subject or "").lower()
    b = (body or "").lower()
    f = (from_email or "").lower()
    if "mailer-daemon" in f or "postmaster" in f:
        if "message blocked" in b or "5.7.1" in b:
            return True
    if "message blocked" in s or "5.7.1" in s:
        return True
    return False


def _truthy(v: str | None) -> bool:
    s = (v or "").strip().lower()
    return s in {"1", "true", "yes", "on", "y"}


async def _notify_once(bot: Bot, chat_id: int, *, key: str, text: str) -> None:
    if key in _NOTIFY_ONCE:
        return
    _NOTIFY_ONCE.add(key)
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        pass


async def _db_commit_retry(session, attempts: int = 3) -> None:
    last = None
    for _ in range(attempts):
        try:
            await session.commit()
            return
        except OperationalError as e:
            last = e
            await asyncio.sleep(0.2)
    if last:
        raise last


async def _set_last_seen_uid(acc_id: int, uid: int) -> None:
    try:
        async with Session() as session:
            acc = (
                await session.execute(
                    sa_select(EmailAccount).where(EmailAccount.id == int(acc_id)).limit(1)
                )
            ).scalars().first()
            if acc:
                acc.last_seen_uid = int(uid)
                await _db_commit_retry(session)
    except Exception:
        logger.exception("Failed to persist last_seen_uid for acc_id=%s", acc_id)


async def _upsert_convlink(
    *,
    user_id: int,
    inbox_email: str,
    contact_email: str,
    ad_url: str | None = None,
    generated_link: str | None = None,
    tg_message_id: int | None = None,
) -> None:
    inbox = (inbox_email or "").strip().lower()
    contact = (contact_email or "").strip().lower()
    if not inbox or not contact:
        return

    try:
        async with Session() as session:
            row = (await session.execute(
                sa_select(ConversationLink).where(
                    ConversationLink.user_id == int(user_id),
                    func.lower(ConversationLink.account_email) == inbox,
                    func.lower(ConversationLink.from_email) == contact,
                )
            )).scalars().first()

            if row is None:
                row = ConversationLink(
                    user_id=int(user_id),
                    account_email=inbox,
                    from_email=contact,
                    generated_link=(generated_link or None),
                    tg_message_id=int(tg_message_id) if tg_message_id is not None else None,
                )
                session.add(row)
            else:
                if ad_url:
                    row.ad_url = ad_url
                if generated_link:
                    row.generated_link = generated_link
                # Запоминаем anchor message_id только если его ещё нет, либо если явно передали.
                if tg_message_id is not None:
                    row.tg_message_id = int(tg_message_id)

            await _db_commit_retry(session)
    except Exception:
        logger.exception("Failed to upsert conversation_links")


async def _load_convlink(
    *,
    user_id: int,
    inbox_email: str,
    contact_email: str,
) -> ConversationLink | None:
    inbox = (inbox_email or "").strip().lower()
    contact = (contact_email or "").strip().lower()
    if not inbox or not contact:
        return None
    try:
        async with Session() as session:
            row = (await session.execute(
                sa_select(ConversationLink).where(
                    ConversationLink.user_id == int(user_id),
                    func.lower(ConversationLink.account_email) == inbox,
                    func.lower(ConversationLink.from_email) == contact,
                )
            )).scalars().first()
            return row
    except Exception:
        logger.exception("Failed to load conversation_links")
        return None


def _imap_connect(provider: str, email_addr: str) -> tuple[str, int]:
    return "imap.gmail.com", 993


def _find_all_mailbox_name(M: imaplib.IMAP4_SSL) -> str | None:
    try:
        typ, data = M.list()
        if typ != "OK" or not data:
            return None

        candidates: list[str] = []
        for raw in data:
            if not raw:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "ignore")
            else:
                line = str(raw)

            low = line.lower()
            m = re.findall(r'"([^"]+)"', line)
            name = m[-1] if m else line.split()[-1].strip('"')

            if (
                "\\all" in low
                or "all mail" in low
                or "alle nachrichten" in low
                or "todas as mensagens" in low
                or "tutti i messaggi" in low
            ):
                candidates.append(name)

        for p in ("[Gmail]/All Mail", "[Google Mail]/All Mail"):
            if p in candidates:
                return p

        return candidates[0] if candidates else None
    except Exception:
        return None


def _imap_connect_and_select(host: str, port: int, email_addr: str, password: str) -> imaplib.IMAP4_SSL:
    M = imaplib.IMAP4_SSL(host, port)
    M.login(email_addr, password)

    # ✅ читаем только INBOX
    typ, _ = M.select("INBOX")
    if typ != "OK":
        raise RuntimeError("IMAP select INBOX failed")

    return M


def _imap_supports_idle(M: imaplib.IMAP4_SSL) -> bool:
    try:
        caps = M.capabilities or ()
        return b"IDLE" in caps or "IDLE" in caps
    except Exception:
        return False


def _imap_idle_wait_sync(M: imaplib.IMAP4_SSL, timeout_sec: int) -> None:
    try:
        tag = M._new_tag()
        M.send(f"{tag} IDLE\r\n".encode())
        end = time.time() + float(timeout_sec)
        while time.time() < end:
            r, _, _ = pyselect.select([M.socket()], [], [], 1)
            if r:
                data = M.readline()
                if not data:
                    break
        M.send(b"DONE\r\n")
        M.readline()
    except Exception:
        pass


def _decode_mime_words(s: str) -> str:
    if not s:
        return ""
    try:
        parts = decode_header(s)
        out = []
        for t, enc in parts:
            if isinstance(t, bytes):
                out.append(t.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(t)
        return "".join(out)
    except Exception:
        return s


def _extract_text_from_msg(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    txt = payload.decode(charset, errors="ignore")
                except Exception:
                    txt = payload.decode("utf-8", errors="ignore")
                parts.append(txt)
        return "\n\n".join(parts).strip()
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="ignore").strip()
    except Exception:
        return payload.decode("utf-8", errors="ignore").strip()


def _imap_fetch_new_sync_raw(
    *,
    host: str,
    port: int,
    email_addr: str,
    password: str,
    last_uid: Optional[int],
) -> tuple[List[Tuple[str, str, str, str, str, str]], Optional[int]]:
    """Fetch new mails from INBOX and (for GMX only) from Spam/Junk.

    - INBOX: uses UID > last_uid (first run returns empty and sets last_uid=max_uid).
    - GMX Spam/Junk: fetches UNSEEN only, marks them as \Seen to avoid repeats,
      and prefixes uid as 'S:<uid>' so the async layer can filter/handle separately.
    """
    M = None

    def _fetch_uids(uids_list: list[int], *, uid_prefix: str = "") -> list[Tuple[str, str, str, str, str, str]]:
        out: list[Tuple[str, str, str, str, str, str]] = []
        for uid in uids_list:
            typ2, msg_data = M.uid("fetch", str(uid), "(RFC822)")
            if typ2 != "OK" or not msg_data:
                continue

            raw = None
            for item in msg_data:
                if isinstance(item, tuple) and item[1]:
                    raw = item[1]
                    break
            if not raw:
                continue

            msg = email.message_from_bytes(raw)

            from_raw = _decode_mime_words(msg.get("From", ""))
            subject = _decode_mime_words(msg.get("Subject", ""))
            date_str = msg.get("Date", "") or ""

            name, addr = parseaddr(from_raw)
            from_email = (addr or "").strip().lower()
            from_name = (name or "").strip()

            body = _extract_text_from_msg(msg)

            out.append((f"{uid_prefix}{uid}", from_email, from_name, subject, date_str, body))
        return out

    try:
        M = _imap_connect_and_select(host, port, email_addr, password)

        # --- INBOX ---
        typ, data = M.uid("search", None, "ALL")
        if typ != "OK":
            inbox_uids = []
        else:
            inbox_uids: list[int] = []
            if data and data[0]:
                inbox_uids = [int(x) for x in data[0].split() if x.isdigit()]

        inbox_mails: list[Tuple[str, str, str, str, str, str]] = []
        max_uid = last_uid

        if inbox_uids:
            max_uid = max(inbox_uids)

            # first run: don't forward old inbox mails
            if last_uid is None:
                # still may check GMX spam below
                inbox_new_uids: list[int] = []
            else:
                inbox_new_uids = [u for u in inbox_uids if u > int(last_uid)]
                if inbox_new_uids:
                    inbox_mails = _fetch_uids(sorted(inbox_new_uids)[-DEFAULT_MAX_PER_ACCOUNT:])
        else:
            inbox_new_uids = []

        # Determine updated last_uid for inbox
        updated_last_uid: Optional[int] = int(max_uid) if max_uid is not None else last_uid
        if last_uid is None and max_uid is not None:
            updated_last_uid = int(max_uid)

        # --- GMX Spam/Junk (UNSEEN only) ---
        spam_mails: list[Tuple[str, str, str, str, str, str]] = []
        is_gmx = ("gmx" in (host or "").lower()) or ("gmx" in (email_addr or "").lower())
        if is_gmx:
            spam_box_candidates = ["Spam", "Junk", "SPAM", "JUNK", "INBOX.Spam", "INBOX.Junk"]
            selected = False
            for box in spam_box_candidates:
                try:
                    typ_sel, _ = M.select(box)
                    if typ_sel == "OK":
                        selected = True
                        break
                except Exception:
                    continue

            if selected:
                try:
                    typ_s, data_s = M.uid("search", None, "UNSEEN")
                    if typ_s == "OK" and data_s and data_s[0]:
                        spam_uids = [int(x) for x in data_s[0].split() if x.isdigit()]
                        if spam_uids:
                            spam_mails = _fetch_uids(sorted(spam_uids)[-DEFAULT_MAX_PER_ACCOUNT:], uid_prefix="S:")
                            # mark seen to avoid re-processing forever
                            for su in spam_uids:
                                try:
                                    M.uid("store", str(su), "+FLAGS", r"(\\Seen)")
                                except Exception:
                                    pass
                finally:
                    # return to INBOX for consistency
                    try:
                        M.select("INBOX")
                    except Exception:
                        pass

        mails = inbox_mails + spam_mails

        # If first run and no inbox new mails, we still return spam matches (if any)
        if last_uid is None:
            return mails, (int(max_uid) if max_uid is not None else last_uid)

        # If no inbox new mails and no spam mails
        if not mails:
            return [], (int(max_uid) if max_uid is not None else last_uid)

        return mails, (int(max_uid) if max_uid is not None else last_uid)

    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass
def _strip_html_to_text(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    t = re.sub(r"</p\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
    return t


def _clean_mail_body_for_card(raw: str) -> str:
    """Текст письма для карточки: без HTML/CSS-мусора из шаблонов Google и т.п."""
    if not raw:
        return ""
    txt = raw
    low = txt.lower()
    if "<style" in low or "<html" in low or "<div" in low or "<span" in low:
        txt = re.sub(r"(?is)<style[^>]*>.*?</style>", "", txt)
        txt = _strip_html_to_text(txt)
    lines: list[str] = []
    for line in txt.replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            lines.append("")
            continue
        if re.match(r"^[\.\#\@\w\-\s,\[\]:]+\{", s):
            continue
        if re.match(r"^[\}\s;]+$", s):
            continue
        if s.startswith("@media") or s.startswith("@font-face"):
            continue
        lines.append(line.rstrip())
    txt = "\n".join(lines)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt


def _ensure_multiline_for_expandable(text: str) -> str:
    """Telegram показывает стрелку разворота только у многострочного expandable blockquote."""
    if not text:
        return "—"
    if "\n" in text:
        return text
    if len(text) <= 120:
        return text
    return "\n".join(text[i : i + 100] for i in range(0, len(text), 100))


def _extract_reply_only_preview(raw: str) -> str:
    """Preview for card.

    Требование из ТЗ (скрин №2):
    - показывать НЕ только последнее сообщение продавца,
      но и текст предыдущего письма (обычно это наше отправленное сообщение),
      если он присутствует в цепочке (quoted / 'Am ... schrieb', 'On ... wrote', etc.).
    - если в письме нет цепочки — показываем как раньше только ответ продавца.
    """
    if not raw:
        return ""

    txt = raw
    low = txt.lower()
    if "<html" in low or "<div" in low or "<span" in low or "<blockquote" in low:
        txt = _strip_html_to_text(txt)

    txt = txt.replace("\r\n", "\n").replace("\r", "\n")

    # 1) Верхняя часть: сообщение продавца (как раньше — до маркера цитирования)
    markers = [
        "\nOn ", "On ",
        "\nAm ", "Am ",
        "\nLe ", "Le ",
        "\n-----Original Message-----",
        "\nFrom:",
        "\nОт:",
    ]
    cut_pos = None
    for mk in markers:
        p = txt.find(mk)
        if p != -1:
            cut_pos = p if cut_pos is None else min(cut_pos, p)

    seller_part = txt if cut_pos is None else txt[:cut_pos]

    # отсекаем '>'-цитирование внутри seller_part
    seller_lines = []
    for line in seller_part.split("\n"):
        if line.strip().startswith(">") and seller_lines:
            break
        seller_lines.append(line)
    seller = "\n".join(seller_lines).strip()

    # 2) Попытка достать первую цитируемую часть (обычно наше письмо)
    quoted = ""
    if cut_pos is not None:
        rest = txt[cut_pos:]
        lines = rest.split("\n")
        start_idx = None
        for i, line in enumerate(lines):
            l = line.strip()
            if not l:
                continue
            if (" schrieb" in l.lower()) or (" wrote" in l.lower()) or ("original message" in l.lower()):
                start_idx = i + 1
                break
            if l.startswith(">"):
                start_idx = i
                break

        if start_idx is None:
            start_idx = 0

        buf = []
        for j in range(start_idx, len(lines)):
            l = lines[j]
            ls = l.strip()
            if j != start_idx and (ls.lower().startswith("on ") or ls.lower().startswith("am ") or ls.lower().startswith("le ") or "-----original message-----" in ls.lower()):
                break
            if j != start_idx and ls.startswith("From:"):
                break
            if ls.startswith(">"):
                l = l.lstrip("> ")
                ls = l.strip()
            if not ls and buf:
                break
            buf.append(l)

        quoted = "\n".join(buf).strip()

    if quoted:
        return (seller or "").strip() + "\n\n" + "--------" + "\n" + (quoted or "").strip()

    return (seller or "").strip()


def _service_label_from_link(link: str) -> str:
    l = (link or "").lower()
    if "ricardo.ch" in l:
        return "ricardo.ch"
    if "tutti.ch" in l:
        return "tutti.ch"
    if "post.ch" in l or "posta.ch" in l:
        return "post.ch"
    if "facebook.com" in l:
        return "facebook.com"
    return ""


def _service_html(label: str) -> str:
    s = (label or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("http"):
        url = s
    else:
        url = f"https://www.{low}"
    return f'<a href="{_e(url)}">{_e(s)}</a>'


def render_mail_text_chunks(
    *,
    account_email: str,
    inbox_label: str | None = None,
    from_name: str,
    from_email: str,
    subject: str,
    body: str,
    offer_id: int | None = None,
    link_id: str | None = None,
    service_label: str | None = None,
    product_title: str | None = None,
    translation: str | None = None,
) -> list[str]:
    shown = _ensure_multiline_for_expandable(_clean_mail_body_for_card((body or "").strip()))

    extra = ""
    # ID только из сгенерированной GAG-ссылки (не внутренний offer_id в БД).
    lid = (link_id or "").strip()
    if lid:
        extra += f"<b>ID:</b> <code>{_e(lid)}</code>\n"
    if service_label:
        extra += f"<b>Сервис:</b> {_service_html(service_label)}\n"
    if product_title:
        extra += f"<b>Товар:</b> <code>{_e(product_title)}</code>\n"
    if extra:
        extra = "\n" + extra

    label = (inbox_label or "").strip()
    if label:
        label_line = f'⚡ Получено сообщение на "<b>{_e(label)}</b>"'
    else:
        label_line = f"⚡ Получено сообщение на <code>{_e(account_email)}</code>"

    from_disp = (from_name or "").strip() or from_email
    head = (
        f"{label_line}\n"
        f"<code>{_e(account_email)}</code>\n"
        f'от "<code>{_e(from_disp)}</code>" <code>{_e(from_email)}</code>\n'
        f"{extra}\n"
        f"<b>Тема:</b>\n<blockquote><code>{_e(subject or '—')}</code></blockquote>\n\n"
        f"<b>Текст:</b>\n"
    )

    body_limit = 1400 if translation else 3200
    body_text = _e((shown[:body_limit] if shown else "—"))
    msg = head + f"<blockquote expandable><code>{body_text}</code></blockquote>"
    if translation:
        tr = _ensure_multiline_for_expandable(str(translation)[:1400])
        msg += (
            "\n\n<b>Перевод:</b>\n"
            f"<blockquote expandable><code>{_e(tr)}</code></blockquote>"
        )
    return [msg]


def build_kb(
    acc_id: int,
    uid: str,
    *,
    mail_id: int | None = None,
) -> InlineKeyboardMarkup:
    translate_cb = f"mail_translate:{mail_id}" if mail_id else f"mail_translate_stub:{acc_id}:{uid}"
    link_cb = f"goo_mail:{mail_id}" if mail_id else f"goo_link:{acc_id}:{uid}"
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🌍 Перевести", callback_data=translate_cb)],
        [InlineKeyboardButton(text="🔗 Создать ссылку", callback_data=link_cb)],
        [InlineKeyboardButton(text="📝 Написать ещё", callback_data=f"mail_reply:{acc_id}:{uid}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def mail_card_offer_meta(
    session,
    *,
    user_id: int,
    from_email: str,
    resolved_offer_id: int | None = None,
) -> tuple[int | None, str | None, str | None, str | None, str | None]:
    """Return offer_id, service_label, product_title, photo_url, offer_price."""
    offer_id = resolved_offer_id
    service_label = product_title = photo_url = offer_price = None
    try:
        off = None
        if resolved_offer_id:
            off = (
                await session.execute(
                    sa_select(Offer).where(Offer.id == int(resolved_offer_id)).where(Offer.user_id == int(user_id)).limit(1)
                )
            ).scalars().first()
        if not off:
            canon = _canon_email(from_email)
            off = (
                await session.execute(
                    sa_select(Offer)
                    .join(OfferEmail, OfferEmail.offer_id == Offer.id)
                    .where(Offer.user_id == int(user_id))
                    .where(func.lower(OfferEmail.email) == canon)
                    .order_by(Offer.id.desc())
                    .limit(1)
                )
            ).scalars().first()
        if off:
            offer_id = int(off.id)
            product_title = (off.title or "").strip() or None
            service_label = _service_label_from_link((off.link or "").strip())
            photo_url = (off.photo or "").strip() or None
            offer_price = (off.price or "").strip() or None
    except Exception:
        logger.exception("mail_card_offer_meta failed")
    return offer_id, service_label, product_title, photo_url, offer_price


async def build_mail_card_from_mail(
    session,
    mail: IncomingMail,
    *,
    inbox_label: str | None = None,
    translation: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if inbox_label is None:
        try:
            u = (
                await session.execute(sa_select(User).where(User.id == int(mail.user_id)).limit(1))
            ).scalars().first()
            if u:
                inbox_label = (getattr(u, "sender_name", None) or "").strip() or None
        except Exception:
            inbox_label = None

    oid, service_label, product_title, _photo, _price = await mail_card_offer_meta(
        session,
        user_id=int(mail.user_id),
        from_email=str(getattr(mail, "from_email", "") or ""),
        resolved_offer_id=getattr(mail, "resolved_offer_id", None),
    )

    generated_link = (getattr(mail, "generated_link", None) or "").strip()
    conv = await _load_convlink(
        user_id=int(mail.user_id),
        inbox_email=str(getattr(mail, "account_email", "") or ""),
        contact_email=str(getattr(mail, "from_email", "") or ""),
    )
    if conv and (conv.generated_link or "").strip():
        generated_link = (conv.generated_link or "").strip()
    link_id = link_id_from_generated_url(generated_link)

    chunks = render_mail_text_chunks(
        account_email=str(getattr(mail, "account_email", "") or ""),
        inbox_label=inbox_label,
        from_name=str(getattr(mail, "from_name", "") or ""),
        from_email=str(getattr(mail, "from_email", "") or ""),
        subject=str(getattr(mail, "subject", "") or ""),
        body=body_full,
        offer_id=oid or getattr(mail, "resolved_offer_id", None),
        link_id=link_id,
        service_label=service_label,
        product_title=product_title,
        translation=translation,
    )
    text = (chunks[0] if chunks else "—")[:4096]
    kb = build_kb(
        int(getattr(mail, "account_id", 0) or 0),
        str(getattr(mail, "imap_uid", 0) or "0"),
        mail_id=int(mail.id),
    )
    return text, kb


def _resolve_generated_link_for_card(
    *,
    conv: ConversationLink | None,
    mail_generated_link: str | None = None,
    meta_generated_link: str | None = None,
) -> str:
    for candidate in (
        meta_generated_link,
        mail_generated_link,
        (conv.generated_link if conv else None),
    ):
        s = (candidate or "").strip()
        if s:
            return s
    return ""


async def _try_pin(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        pass


async def _process_mails_for_account(
    bot: Bot,
    *,
    acc_id: int,
    tg_id: int,
    user_id: int,
    account_email: str,
    mails: List[Tuple[str, str, str, str, str, str]],
    max_per_account: int,
    last_uid: Optional[int],
) -> int:
    forwarded = 0

    inbox_label: str | None = None
    try:
        async with Session() as _s0:
            u0 = (
                await _s0.execute(sa_select(User).where(User.id == int(user_id)).limit(1))
            ).scalars().first()
            if u0:
                inbox_label = (getattr(u0, "sender_name", None) or "").strip() or None
    except Exception:
        inbox_label = None

    if last_uid is not None:
        LAST_UID[acc_id] = int(last_uid)
        await _set_last_seen_uid(acc_id, int(last_uid))

    for uid, from_email, from_name, subject, date_str, body in (mails or [])[:max_per_account]:
        uid_key = uid
        is_spam_box = False
        uid_num = None
        if isinstance(uid, str) and uid.startswith("S:"):
            is_spam_box = True
            try:
                uid_num = int(uid.split(":", 1)[1])
            except Exception:
                continue
        else:
            try:
                uid_num = int(uid)
            except Exception:
                continue

        # GMX: allow Spam/Junk only when subject matches an existing offer in DB
        if is_spam_box:
            if "gmx" not in (account_email or "").lower():
                continue
            subj_norm = _normalize_subject(subject)
            if len(subj_norm) < 4:
                continue
            try:
                async with Session() as _s:
                    hit = (await _s.execute(sa_select(Offer.id).where(Offer.title.ilike(f"%{subj_norm}%")).limit(1))).scalar()
                if not hit:
                    continue
            except Exception:
                logger.exception("Failed to check offer title for GMX spam")
                continue
        smtp_block_bounce = _is_smtp_block_bounce(from_email, subject, body)

        if (not is_spam_box) and _looks_like_spam(from_email, from_name, subject, body):
            continue

        try:
            body_clean = (body or "").strip()
            from_email_clean = (from_email or "").strip().lower()
            skip_telegram_notify = _is_google_system_mail(
                from_email_clean, from_name or "", subject or ""
            )
            inbox_email_clean = (account_email or "").strip().lower()

            FULL_BODIES[(acc_id, uid_key)] = body_clean
            FULL_META[(acc_id, uid_key)] = {
                "from_email": from_email_clean,
                "from_name": (from_name or "").strip(),
                "subject": subject or "",
                "account_email": inbox_email_clean,
                "date_str": date_str or "",
            }

            resolved_offer_id: int | None = None
            resolved_offer_email_id: int | None = None
            mail_db_id: int | None = None
            try:
                async with Session() as session:
                    resolved_offer_id, resolved_offer_email_id = await _resolve_offer_for_incoming(
                        session,
                        user_id=user_id,
                        from_email=from_email_clean,
                        subject=subject or "",
                        from_name=from_name or "",
                        body_text=body_clean or "",
                    )

                    existing = (
                        await session.execute(
                            sa_select(IncomingMail)
                            .where(IncomingMail.account_id == int(acc_id))
                            .where(IncomingMail.imap_uid == int(uid_num))
                            .limit(1)
                        )
                    ).scalars().first()

                    if not existing:
                        existing = IncomingMail(
                            user_id=int(user_id),
                            account_id=int(acc_id),
                            imap_uid=int(uid_num),
                        )
                        session.add(existing)

                    existing.account_email = inbox_email_clean
                    existing.from_email = from_email_clean
                    existing.from_name = (from_name or "").strip() or None
                    existing.subject = (subject or "").strip() or None
                    existing.date_str = (date_str or "").strip() or None
                    existing.body = body_clean or None
                    existing.resolved_offer_id = resolved_offer_id
                    existing.resolved_offer_email_id = resolved_offer_email_id

                    await _db_commit_retry(session)
                    mail_db_id = int(existing.id)

            except Exception:
                logger.exception("Failed to persist IncomingMail acc=%s uid=%s", acc_id, uid)

            if skip_telegram_notify:
                forwarded += 1
                continue

            # ad_url берём ТОЛЬКО из БД (по ТЗ: не ищем ссылку в теле письма)
            ad_url: str | None = None

            # ✅ если ссылки нет — берём из Offer.link (валидированные данные в БД)
            if (not ad_url) and resolved_offer_id:
                try:
                    async with Session() as session:
                        off_link = (
                            await session.execute(
                                sa_select(Offer.link)
                                .where(Offer.id == int(resolved_offer_id))
                                .where(Offer.user_id == int(user_id))
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if off_link:
                            ad_url = (off_link or "").strip()
                except Exception:
                    logger.exception("Failed to load Offer.link for resolved_offer_id=%s", resolved_offer_id)

            if ad_url:
                FULL_META[(acc_id, uid_key)]["ad_url"] = ad_url
                await _upsert_convlink(
                    user_id=user_id,
                    inbox_email=_canon_email(inbox_email_clean),
                    contact_email=_canon_email(from_email_clean),
                    )

            # ✅ ТЗ: трединг в Telegram требует, чтобы ConversationLink существовал даже если ссылки ещё нет.
            # Это НЕ добавляет новой логики ссылок, только гарантирует запись для хранения tg_message_id.
            await _upsert_convlink(
                user_id=user_id,
                inbox_email=_canon_email(inbox_email_clean),
                contact_email=_canon_email(from_email_clean),
            )

            conv = await _load_convlink(
                user_id=user_id,
                inbox_email=_canon_email(inbox_email_clean),
                contact_email=_canon_email(from_email_clean),
            )
            gen_link = _resolve_generated_link_for_card(
                conv=conv,
                meta_generated_link=FULL_META.get((acc_id, uid_key), {}).get("generated_link"),
            )
            if gen_link:
                FULL_META[(acc_id, uid_key)]["generated_link"] = gen_link
            if conv and conv.ad_url and not FULL_META[(acc_id, uid_key)].get("ad_url"):
                FULL_META[(acc_id, uid_key)]["ad_url"] = (conv.ad_url or "").strip()

            link_id = link_id_from_generated_url(gen_link)
            if link_id:
                FULL_META[(acc_id, uid_key)]["link_id"] = link_id

            # ✅ сохраняем в БД полные данные по письму (включая ссылки),
            # чтобы их можно было смотреть по кнопке ℹ️ Инфо и использовать дальше.
            if mail_db_id:
                try:
                    async with Session() as session:
                        mail_row = (
                            await session.execute(
                                sa_select(IncomingMail).where(IncomingMail.id == int(mail_db_id)).limit(1)
                            )
                        ).scalars().first()
                        if mail_row:
                            mail_row.ad_url = (FULL_META.get((acc_id, uid_key), {}).get("ad_url") or "").strip() or None
                            mail_row.generated_link = (
                                FULL_META.get((acc_id, uid_key), {}).get("generated_link") or ""
                            ).strip() or None
                            await _db_commit_retry(session)
                except Exception:
                    logger.exception("Failed to persist IncomingMail links mail_id=%s", mail_db_id)
            # ✅ ТЗ: подтягиваем товар/сервис/фото из БД по email отправителя (валиднутый email продавца)
            offer_id = None
            service_label = None
            product_title = None
            photo_url = None
            offer_price: str | None = None
            try:
                async with Session() as _s:
                    offer_id, service_label, product_title, photo_url, offer_price = await mail_card_offer_meta(
                        _s,
                        user_id=int(user_id),
                        from_email=from_email_clean,
                        resolved_offer_id=resolved_offer_id,
                    )
            except Exception:
                logger.exception("Failed to load Offer meta for incoming mail: from=%s", from_email_clean)

            photo_to_send: str | None = None
            photo_caption: str | None = None
            if photo_url:
                try:
                    is_first = False
                    try:
                        async with Session() as _s2:
                            cnt = (
                                await _s2.execute(
                                    sa_select(func.count(IncomingMail.id))
                                    .where(IncomingMail.user_id == int(user_id))
                                    .where(IncomingMail.account_id == int(acc_id))
                                    .where(IncomingMail.from_email == str(from_email_clean).strip())
                                )
                            ).scalar() or 0
                            is_first = int(cnt) <= 1
                    except Exception:
                        is_first = False

                    if is_first:
                        photo_to_send = photo_url
                        photo_caption = "📷 Фото товара (первый ответ)"
                        if offer_price:
                            photo_caption += f"\n💰 Цена: {offer_price} 💰"
                except Exception:
                    photo_to_send = None

            chunks = render_mail_text_chunks(
                account_email=account_email,
                inbox_label=inbox_label,
                from_name=from_name,
                from_email=from_email,
                subject=subject,
                body=body,
                offer_id=offer_id,
                link_id=link_id,
                service_label=service_label,
                product_title=product_title,
            )

            kb = build_kb(acc_id, uid, mail_id=mail_db_id)

            # ✅ ТЗ: повторные письма от продавца должны крепиться к первому сообщению.
            reply_to_id: int | None = None
            try:
                if conv and getattr(conv, "tg_message_id", None):
                    reply_to_id = int(conv.tg_message_id)
            except Exception:
                reply_to_id = None

            if chunks:
                m = await bot.send_message(
                    chat_id=tg_id,
                    text=chunks[0],
                    reply_markup=kb,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_id,
                )
            else:
                m = await bot.send_message(
                    chat_id=tg_id,
                    text="—",
                    reply_markup=kb,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_id,
                )

            # Если это первое сообщение в диалоге — пинуем и сохраняем anchor message_id.
            try:
                if reply_to_id is None:
                    await _try_pin(bot, tg_id, m.message_id)
                    await _upsert_convlink(
                        user_id=user_id,
                        inbox_email=_canon_email(inbox_email_clean),
                        contact_email=_canon_email(from_email_clean),
                        tg_message_id=int(m.message_id),
                    )
            except Exception:
                pass

            if photo_to_send:
                try:
                    await bot.send_photo(
                        chat_id=tg_id,
                        photo=photo_to_send,
                        caption=(photo_caption or "📷 Фото товара (первый ответ)"),
                        reply_to_message_id=int(m.message_id),
                    )
                except Exception:
                    pass

            # ✅ Если пришёл DSN/блокировка (Message blocked) — помечаем аккаунт как неактивный
            # и (при включённом контроле блокировок) пишем уведомление.
            if smtp_block_bounce and account_email:
                try:
                    async with Session() as session:
                        acc = (
                            await session.execute(
                                sa_select(EmailAccount).where(EmailAccount.id == int(acc_id)).limit(1)
                            )
                        ).scalars().first()
                        if acc:
                            acc.status = "smtp_blocked"
                            # коротко сохраняем причину
                            acc.last_error = (body_clean[:1000] if body_clean else (subject or ""))
                            await _db_commit_retry(session)

                        # настройка: block_control
                        u = (await session.execute(sa_select(User).where(User.id == int(user_id)).limit(1))).scalars().first()
                        block_control = False
                        if u:
                            block_control = _truthy(await get_user_setting(session, u, "block_control"))

                    # сообщение-статус (как на скрине)
                    status_msg = f"<b>{account_email}</b>: неактивен для отправок 🔴"
                    m2 = await bot.send_message(chat_id=tg_id, text=status_msg, parse_mode="HTML")
                    await _try_pin(bot, tg_id, m2.message_id)

                    if block_control:
                        warn = f"Возникла ошибка - поток для {account_email} остановлен⚡️"
                        await bot.send_message(chat_id=tg_id, text=warn)
                except Exception:
                    logger.exception("Failed to mark smtp_blocked for %s", account_email)

            forwarded += 1

        except Exception:
            logger.exception("Failed to forward incoming email acc=%s uid=%s", acc_id, uid)

    return forwarded


_IDLE_TASKS: Dict[int, asyncio.Task] = {}
_IDLE_STOPS: Dict[int, threading.Event] = {}
_EVENT_QUEUES: Dict[int, asyncio.Queue] = {}


async def _refresh_accounts_map() -> list[tuple[EmailAccount, int]]:
    async with Session() as session:
        accounts = (await session.execute(
            sa_select(EmailAccount).where(
                sa_or(
                    EmailAccount.status.is_(None),
                    EmailAccount.status.in_(["active", "enabled", "proxy_error", "smtp_blocked"]),
                )
            )
        )).scalars().all()

        users = (await session.execute(sa_select(User))).scalars().all()
        users_by_id = {u.id: u.telegram_id for u in users}

    out: list[tuple[EmailAccount, int]] = []
    for a in accounts:
        tg_id = users_by_id.get(a.user_id)
        if tg_id:
            out.append((a, int(tg_id)))
    return out


def _idle_thread_loop(
    acc_snapshot: dict[str, Any],
    start_last_uid: Optional[int],
    stop_evt: threading.Event,
    push_event: callable,
) -> None:
    last_uid = start_last_uid
    host, port = _imap_connect(acc_snapshot.get("provider") or "", acc_snapshot.get("email") or "")
    email_addr = str(acc_snapshot.get("email") or "")
    password = str(acc_snapshot.get("password") or "")

    M: Optional[imaplib.IMAP4_SSL] = None
    idle_ok = False

    while not stop_evt.is_set():
        try:
            if M is None:
                M = _imap_connect_and_select(host, port, email_addr, password)
                idle_ok = _imap_supports_idle(M)

                if last_uid is None:
                    _, max_uid0 = _imap_fetch_new_sync_raw(
                        host=host, port=port, email_addr=email_addr, password=password, last_uid=None
                    )
                    last_uid = max_uid0

            if USE_IMAP_IDLE and idle_ok:
                _imap_idle_wait_sync(M, IDLE_TIMEOUT_SEC)
            else:
                time.sleep(POLL_FALLBACK_SEC)

            mails, new_last = _imap_fetch_new_sync_raw(
                host=host, port=port, email_addr=email_addr, password=password, last_uid=last_uid
            )
            if new_last is not None:
                last_uid = int(new_last)

            if mails:
                push_event({"type": "mails", "mails": mails, "last_uid": last_uid})

        except Exception as e:
            if _is_invalid_credentials_error(e):
                push_event({"type": "invalid_creds", "error": str(e)})
                return

            try:
                if M is not None:
                    try:
                        M.logout()
                    except Exception:
                        pass
            finally:
                M = None

            push_event({"type": "error", "error": str(e)})
            time.sleep(2)

    try:
        if M is not None:
            M.logout()
    except Exception:
        pass


async def _start_idle_for_account(bot: Bot, acc: EmailAccount, tg_id: int) -> None:
    acc_id = int(acc.id)
    if acc_id in _IDLE_TASKS and not _IDLE_TASKS[acc_id].done():
        return

    stop_evt = threading.Event()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _IDLE_STOPS[acc_id] = stop_evt
    _EVENT_QUEUES[acc_id] = q

    loop = asyncio.get_running_loop()

    snap = {
        "id": acc_id,
        "user_id": int(acc.user_id),
        "email": str(acc.email),
        "password": str(acc.password or ""),
        "provider": str(getattr(acc, "provider", "") or ""),
    }

    start_last = getattr(acc, "last_seen_uid", None)
    if start_last is None:
        start_last = LAST_UID.get(acc_id)

    def push_event(item: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except Exception:
            pass

    async def _runner():
        thread_task = asyncio.create_task(
            asyncio.to_thread(_idle_thread_loop, snap, start_last, stop_evt, push_event)
        )

        try:
            while not stop_evt.is_set():
                item = await q.get()
                typ = item.get("type")

                if typ == "mails":
                    mails = item.get("mails") or []
                    last_uid = item.get("last_uid")
                    await _process_mails_for_account(
                        bot,
                        acc_id=acc_id,
                        tg_id=tg_id,
                        user_id=int(snap["user_id"]),
                        account_email=str(snap["email"]),
                        mails=mails,
                        max_per_account=DEFAULT_MAX_PER_ACCOUNT,
                        last_uid=last_uid,
                    )
                    _ERROR_STREAK.pop(acc_id, None)
                    _BACKOFF_UNTIL.pop(acc_id, None)

                elif typ == "invalid_creds":
                    stop_evt.set()
                    break

                elif typ == "error":
                    err_txt = str(item.get("error") or "")

                    if _is_transient_ssl_eof(Exception(err_txt)):
                        delay = 2
                        _ERROR_STREAK[acc_id] = 1
                        _BACKOFF_UNTIL[acc_id] = _now() + delay

                        last_log = _LAST_EOF_LOG.get(acc_id, 0.0)
                        if _now() - last_log >= _EOF_LOG_COOLDOWN_SEC:
                            _LAST_EOF_LOG[acc_id] = _now()
                            logger.info(
                                "IMAP reconnect acc=%s email=%s (EOF/TLS reset)",
                                acc_id, snap["email"]
                            )
                        await asyncio.sleep(delay)
                        continue

                    streak = int(_ERROR_STREAK.get(acc_id, 0)) + 1
                    _ERROR_STREAK[acc_id] = streak
                    delay = _calc_backoff(streak)
                    _BACKOFF_UNTIL[acc_id] = _now() + delay

                    logger.warning(
                        "IMAP error acc=%s email=%s backoff=%ss err=%s",
                        acc_id, snap["email"], delay, err_txt
                    )
                    await asyncio.sleep(min(3, delay))

        finally:
            stop_evt.set()
            try:
                thread_task.cancel()
            except Exception:
                pass

    _IDLE_TASKS[acc_id] = asyncio.create_task(_runner())


async def _idle_manager_loop(bot: Bot, *, poll_seconds: int) -> None:
    while True:
        try:
            now = _now()
            accounts = await _refresh_accounts_map()

            for acc, tg_id in accounts:
                acc_id = int(acc.id)
                until = _BACKOFF_UNTIL.get(acc_id)
                if until and now < float(until):
                    continue
                await _start_idle_for_account(bot, acc, tg_id)

        except Exception:
            logger.exception("Incoming mail manager loop error")

        await asyncio.sleep(max(5, int(poll_seconds)))


def start_incoming_mail_worker(bot: Bot, poll_seconds: int = 20) -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return

    async def _loop():
        await _idle_manager_loop(bot, poll_seconds=poll_seconds)

    _worker_task = asyncio.create_task(_loop())
    logger.info("Incoming mail worker started: poll=%ss idle=%s", poll_seconds, USE_IMAP_IDLE)
