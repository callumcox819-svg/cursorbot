from __future__ import annotations

import asyncio
import logging
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional, Tuple, List

from models import EmailAccount
from database import Session
from sqlalchemy import select as sa_select, func

from services.placeholders import apply_placeholders
from services.smtp_proxy_guard import smtp_proxy_required_error

SMTP_BY_PROVIDER = {
    "gmail": ("smtp.gmail.com", 587),
    "gmx": ("mail.gmx.net", 587),
    "web": ("smtp.web.de", 587),
    "1and1": ("smtp.1and1.com", 587),
    "ionos": ("smtp.ionos.com", 587),
    "outlook": ("smtp.office365.com", 587),
    "office365": ("smtp.office365.com", 587),
    "hotmail": ("smtp.office365.com", 587),
    "live": ("smtp.office365.com", 587),
    "yahoo": ("smtp.mail.yahoo.com", 587),
    "yandex": ("smtp.yandex.com", 587),
    "mailru": ("smtp.mail.ru", 587),
    "rambler": ("smtp.rambler.ru", 587),
    "zoho": ("smtp.zoho.com", 587),
}

SMTP_TIMEOUT_SEC = max(20, min(120, int(os.getenv("SMTP_TIMEOUT_SEC", "60"))))

logger = logging.getLogger(__name__)


def _sanitize_header_line(value: str) -> str:
    """Заголовки SMTP не допускают переносы строк."""
    s = (value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()


def _looks_like_html(body: str) -> bool:
    b = (body or "").lower()
    return "<html" in b or "<body" in b or "</" in b


def _smtp_host_port(provider: str, email: str) -> tuple[str, int]:
    p = (provider or "").strip().lower()
    if p in SMTP_BY_PROVIDER:
        return SMTP_BY_PROVIDER[p]
    domain = (email or "").split("@")[-1].lower().strip()
    if "gmail" in domain:
        return SMTP_BY_PROVIDER["gmail"]
    if "gmx" in domain:
        return SMTP_BY_PROVIDER["gmx"]
    if domain.endswith("web.de"):
        return SMTP_BY_PROVIDER["web"]
    if "outlook" in domain or "office365" in domain or "hotmail" in domain or "live" in domain:
        return SMTP_BY_PROVIDER["office365"]
    if "yahoo" in domain:
        return SMTP_BY_PROVIDER["yahoo"]
    if "yandex" in domain:
        return SMTP_BY_PROVIDER["yandex"]
    if "mail.ru" in domain:
        return SMTP_BY_PROVIDER["mailru"]
    if "rambler" in domain:
        return SMTP_BY_PROVIDER["rambler"]
    return SMTP_BY_PROVIDER["gmail"]


def _strip_html(html_text: str) -> str:
    import re as _re
    import html as _html
    txt = _re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text or "")
    txt = _re.sub(r"(?is)<br\s*/?>", "\n", txt)
    txt = _re.sub(r"(?is)</p\s*>", "\n\n", txt)
    txt = _re.sub(r"(?is)<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    txt = _re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = _re.sub(r"\n\s+\n", "\n\n", txt)
    return txt.strip()


def _build_message(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
):
    subj = _sanitize_header_line(subject or "")
    b = body or ""

    # Some call sites explicitly request HTML sending (legacy compatibility).
    # If not provided, infer from content.
    if is_html is None:
        is_html = _looks_like_html(b)

    if is_html:
        msg = MIMEMultipart("alternative")
        plain = _strip_html(b) or " "
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(b, "html", "utf-8"))
    else:
        msg = MIMEText(b, "plain", "utf-8")

    if sender_name:
        msg["From"] = formataddr((sender_name, from_email))
    else:
        msg["From"] = from_email

    msg["To"] = to_email
    msg["Subject"] = subj
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(from_email.split("@")[-1] if "@" in from_email else None))
    return msg


def _extract_code_text_from_exception(e: Exception) -> tuple[Optional[str], str]:
    code = None
    text = ""

    smtp_code = getattr(e, "smtp_code", None)
    smtp_error = getattr(e, "smtp_error", None)
    if smtp_code is not None:
        try:
            code = str(int(smtp_code))
        except Exception:
            code = str(smtp_code)
    if smtp_error is not None:
        try:
            if isinstance(smtp_error, bytes):
                text = smtp_error.decode("utf-8", "ignore")
            else:
                text = str(smtp_error)
        except Exception:
            text = str(smtp_error)

    if not text:
        try:
            text = " ".join([str(a) for a in getattr(e, "args", []) if a is not None]).strip()
        except Exception:
            text = str(e)

    return code, (text or "").strip()


# Только явные сбои SOCKS/прокси — НЕ голый SMTP timeout (часто лаг Gmail/прокси).
_PROXY_PATTERNS_STRICT = [
    r"generalproxyerror",
    r"sockshttperror",
    r"pysocks",
    r"\bsocks5?\b",
    r"proxy connection",
    r"can'?t connect to proxy",
    r"cannot connect to proxy",
    r"http proxy server did not return",
    r"0x05",
    r"doesn't support ipv6",
    r"pysocks doesn't support ipv6",
]

_SMTP_TIMEOUT_PATTERNS = [
    r"timed out",
    r"timeout",
    r"temporarily unavailable",
]

_TRANSIENT_CONNECTION_PATTERNS = [
    r"ssleoferror",
    r"unexpected_eof_while_reading",
    r"eof occurred in violation of protocol",
    r"connection reset",
    r"broken pipe",
    r"connection unexpectedly closed",
    r"connection aborted",
    r"errno 104",
    r"errno 54",
]

_INVALID_CRED_PATTERNS = [
    r"authentication failed",
    r"invalid login",
    r"bad credentials",
    r"username and password not accepted",
    r"534-5\.7\.9",
    r"535-5\.7\.8",
    r"5\.7\.8",
    r"5\.7\.1.*authentication required",
]

_WEB_LOGIN_PATTERNS = [
    r"web login required",
    r"5\.7\.14",
    r"please log in via your web browser",
]

_RATE_LIMIT_PATTERNS = [
    r"rate limit",
    r"too many",
    r"try again later",
    r"4\.7\.0",
    r"4\.7\.1",
]

_BLOCKED_PATTERNS = [
    r"blocked",
    r"blacklist",
    r"rejected",
    r"5\.7\.1",
    r"5\.7\.0",
]

_HARD_PATTERNS = [
    r"user unknown",
    r"recipient address rejected",
    r"no such user",
    r"mailbox unavailable",
    r"5\.1\.1",
    r"5\.1\.0",
]


def _is_proxy_error(e: Exception, text: str) -> bool:
    """Только явный сбой SOCKS/туннеля — не любой SMTP timeout/disconnect."""
    t = (text or "").lower()
    en = type(e).__name__
    if en in (
        "GeneralProxyError",
        "ProxyError",
        "SOCKS5Error",
        "SOCKS4Error",
        "ProxyConnectionError",
    ):
        return True
    if isinstance(e, OSError) and "ipv6" in t and "pysocks" in t:
        return True
    mod = type(e).__module__ or ""
    if "socks" in mod.lower():
        return True
    if any(re.search(p, t) for p in _PROXY_PATTERNS_STRICT):
        return True
    return False


def _is_smtp_timeout_text(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    if any(re.search(p, t) for p in _SMTP_TIMEOUT_PATTERNS):
        if any(re.search(p, t) for p in _PROXY_PATTERNS_STRICT):
            return False
        return True
    return False


def is_transient_connection_error(err: str | None) -> bool:
    """Обрыв TLS/сокета — часто другой прокси помогает; это не «прокси мёртв»."""
    s = (err or "").strip()
    if not s:
        return False
    t = s.lower()
    if any(re.search(p, t) for p in _TRANSIENT_CONNECTION_PATTERNS):
        return True
    if "ssl" in t and ("eof" in t or "unexpected" in t):
        return True
    return False


def is_smtp_timeout_error(err: str | None) -> bool:
    s = normalize_send_error(err)
    kind = s.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind == "SMTP_TIMEOUT":
        return True
    if kind == "PROXY_ERROR":
        parts = s.split("|")
        if len(parts) >= 2 and parts[1].strip().lower() == "timeout":
            return True
    return _is_smtp_timeout_text(s)


def is_definite_proxy_failure(err: str | None) -> bool:
    """Можно помечать прокси неактивным только при явной ошибке туннеля."""
    s = normalize_send_error(err)
    if not is_proxy_error_marker(s):
        return False
    t = s.lower()
    if "no_active_proxy" in t or "no_proxy_context" in t:
        return False
    definite = (
        "generalproxyerror",
        "socks",
        "proxy connection",
        "can't connect to proxy",
        "cannot connect to proxy",
        "authentication failed",  # socks auth
        "0x05",  # socks5 reply
        "network is unreachable",
        "getaddrinfo failed",
        "pysocks",
    )
    return any(x in t for x in definite)


_KNOWN_ERROR_KINDS = frozenset(
    {
        "PROXY_ERROR",
        "SMTP_TIMEOUT",
        "ACCOUNT_INVALID_CREDENTIALS",
        "ACCOUNT_WEB_LOGIN_REQUIRED",
        "ACCOUNT_RATE_LIMIT",
        "ACCOUNT_BLOCKED",
        "RECIPIENT_DEAD",
        "RECIPIENT_REFUSED",
        "TG_ERROR",
        "NO_ACCOUNTS",
    }
)


def normalize_send_error(err: str | None) -> str:
    """Приводит сырой GeneralProxyError и пр. к формату PROXY_ERROR|… для /stat."""
    s = (err or "").strip()
    if not s:
        return "UNKNOWN"
    kind = s.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind in _KNOWN_ERROR_KINDS:
        return s
    t = s.lower()
    if _is_smtp_timeout_text(s):
        return _marker("SMTP_TIMEOUT", "timeout", s)
    if any(re.search(p, t) for p in _PROXY_PATTERNS_STRICT):
        return _marker("PROXY_ERROR", "socks", s)
    if _is_hard_bounce(None, t):
        return _marker("RECIPIENT_DEAD", None, s)
    return s


def is_proxy_error_marker(err: str | None) -> bool:
    s = normalize_send_error(err)
    return s.split("|", 1)[0].split(":", 1)[0].strip().upper() == "PROXY_ERROR"


def should_retry_send_with_other_proxy(err: str | None) -> bool:
    """Нужен другой SOCKS5 (не смена пароля почты и не hard bounce)."""
    s = normalize_send_error(err or "")
    kind = s.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind in {
        "ACCOUNT_INVALID_CREDENTIALS",
        "ACCOUNT_WEB_LOGIN_REQUIRED",
        "ACCOUNT_RATE_LIMIT",
        "ACCOUNT_BLOCKED",
        "RECIPIENT_DEAD",
        "RECIPIENT_REFUSED",
    }:
        return False
    if "no_active_proxy" in s.lower() or "no_proxy_context" in s.lower():
        return False
    if is_smtp_timeout_error(err) or is_transient_connection_error(err):
        return True
    if is_definite_proxy_failure(err) or is_proxy_error_marker(err):
        return True
    return False


def _is_invalid_creds(code: Optional[str], text: str) -> bool:
    t = (text or "").lower()
    if code in {"535", "534"}:
        return True
    return any(re.search(p, t) for p in _INVALID_CRED_PATTERNS)


def _is_web_login_required(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in _WEB_LOGIN_PATTERNS)


def _is_rate_limit(code: Optional[str], text: str) -> bool:
    t = (text or "").lower()
    if code in {"421", "450", "451", "452", "454"}:
        return True
    return any(re.search(p, t) for p in _RATE_LIMIT_PATTERNS)


def _is_blocked(code: Optional[str], text: str) -> bool:
    t = (text or "").lower()
    if code in {"550", "553", "554"}:
        return any(re.search(p, t) for p in _BLOCKED_PATTERNS) or True
    return any(re.search(p, t) for p in _BLOCKED_PATTERNS)


def _is_hard_bounce(code: Optional[str], text: str) -> bool:
    t = (text or "").lower()
    if code in {"550", "551", "552", "553", "554"}:
        return any(re.search(p, t) for p in _HARD_PATTERNS)
    if "5.1.1" in t or "5.1.0" in t:
        return True
    return any(re.search(p, t) for p in _HARD_PATTERNS)


def _marker(kind: str, code: Optional[str], text: str) -> str:
    code_s = (code or "").strip()
    text_s = (text or "").strip()
    if code_s and text_s:
        return f"{kind}:{code_s}:{text_s}"
    if code_s:
        return f"{kind}:{code_s}"
    if text_s:
        return f"{kind}:{text_s}"
    return kind


def _send_plain_sync(
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
    smtp_timeout_sec: float | None = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    guard_err = smtp_proxy_required_error()
    if guard_err:
        return False, guard_err, None

    # Safe placeholder pass (does not require ctx/link; keeps existing behavior if none)
    body = apply_placeholders(body)
    host, port = _smtp_host_port(getattr(account, "provider", "") or "", account.email)
    msg = _build_message(
        from_email=account.email,
        to_email=to_email,
        subject=subject,
        body=body,
        sender_name=sender_name,
        is_html=is_html,
    )

    tmo = float(smtp_timeout_sec if smtp_timeout_sec is not None else SMTP_TIMEOUT_SEC)
    try:
        with smtplib.SMTP(host, port, timeout=tmo) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(account.email, (account.password or "").strip())

            refused = s.send_message(msg, from_addr=account.email, to_addrs=[to_email])
            if refused:
                logger.warning("[SMTP] Recipient refused: %s", refused)
                return False, "RECIPIENT_REFUSED", None

        msgid = msg.get("Message-ID")
        logger.info(
            "[SMTP] Sent mail via %s -> %s subject=%r msgid=%s",
            account.email,
            to_email,
            subject,
            msgid,
        )
        return True, None, msgid

    except Exception as e:
        code, text = _extract_code_text_from_exception(e)

        if _is_proxy_error(e, text):
            return False, _marker("PROXY_ERROR", code or "socks", text or str(e)), None
        if _is_smtp_timeout_text(text or str(e)):
            return False, _marker("SMTP_TIMEOUT", code or "timeout", text or str(e)), None

        if _is_invalid_creds(code, text):
            return False, _marker("ACCOUNT_INVALID_CREDENTIALS", code, text), None
        if _is_web_login_required(text):
            return False, _marker("ACCOUNT_WEB_LOGIN_REQUIRED", code, text), None

        if _is_rate_limit(code, text):
            return False, _marker("ACCOUNT_RATE_LIMIT", code, text), None

        if _is_blocked(code, text):
            return False, _marker("ACCOUNT_BLOCKED", code, text), None

        if _is_hard_bounce(code, text):
            return False, _marker("RECIPIENT_DEAD", code, text), None

        return False, f"{type(e).__name__}: {code or ''} {text}".strip() or str(e), None


async def send_email_via_account(
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
    *,
    smtp_timeout_sec: float | None = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    return await asyncio.to_thread(
        _send_plain_sync,
        account,
        to_email,
        subject,
        body,
        sender_name,
        is_html,
        smtp_timeout_sec,
    )


def _send_batch_sync(
    account: EmailAccount,
    items: list[tuple[str, str, str]],
    sender_name: Optional[str] = None,
    smtp_timeout_sec: float | None = None,
) -> List[Tuple[bool, Optional[str]]]:
    guard_err = smtp_proxy_required_error()
    if guard_err:
        return [(False, guard_err) for _ in items]

    host, port = _smtp_host_port(getattr(account, "provider", "") or "", account.email)
    results: List[Tuple[bool, Optional[str]]] = []
    tmo = float(smtp_timeout_sec if smtp_timeout_sec is not None else SMTP_TIMEOUT_SEC)

    try:
        with smtplib.SMTP(host, port, timeout=tmo) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(account.email, (account.password or "").strip())

            for to_email, subject, body in items:
                body = apply_placeholders(body)
                msg = _build_message(from_email=account.email, to_email=to_email, subject=subject, body=body, sender_name=sender_name)
                try:
                    refused = s.sendmail(account.email, [to_email], msg.as_string())
                    if refused:
                        logger.warning("[SMTP] Recipient refused (batch): %s", refused)
                        results.append((False, "RECIPIENT_REFUSED"))
                    else:
                        logger.info(
                            "[SMTP] Sent mail (batch) via %s -> %s subject=%r msgid=%s",
                            account.email,
                            to_email,
                            subject,
                            msg.get("Message-ID"),
                        )
                        results.append((True, None))
                except Exception as e:
                    code, text = _extract_code_text_from_exception(e)

                    if _is_proxy_error(e, text):
                        results.append((False, _marker("PROXY_ERROR", code or "socks", text or str(e))))
                    elif _is_smtp_timeout_text(text or str(e)):
                        results.append((False, _marker("SMTP_TIMEOUT", code or "timeout", text or str(e))))
                    elif _is_invalid_creds(code, text):
                        results.append((False, _marker("ACCOUNT_INVALID_CREDENTIALS", code, text)))
                    elif _is_web_login_required(text):
                        results.append((False, _marker("ACCOUNT_WEB_LOGIN_REQUIRED", code, text)))
                    elif _is_rate_limit(code, text):
                        results.append((False, _marker("ACCOUNT_RATE_LIMIT", code, text)))
                    elif _is_blocked(code, text):
                        results.append((False, _marker("ACCOUNT_BLOCKED", code, text)))
                    elif _is_hard_bounce(code, text):
                        results.append((False, _marker("RECIPIENT_DEAD", code, text)))
                    else:
                        results.append((False, f"{type(e).__name__}: {code or ''} {text}".strip() or str(e)))

    except Exception as e:
        code, text = _extract_code_text_from_exception(e)
        err = f"{type(e).__name__}: {code or ''} {text}".strip() or str(e)
        return [(False, err) for _ in items]

    return results


async def send_batch_via_account(
    account: EmailAccount,
    items: list[tuple[str, str, str]],
    sender_name: Optional[str] = None,
    *,
    smtp_timeout_sec: float | None = None,
) -> List[Tuple[bool, Optional[str]]]:
    return await asyncio.to_thread(
        _send_batch_sync, account, items, sender_name, smtp_timeout_sec
    )
