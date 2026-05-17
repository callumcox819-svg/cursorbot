"""Проверка SMTP-активности ящика (логин + проба MAIL/RCPT) через текущий SOCKS5-патч."""

from __future__ import annotations

import logging
import smtplib
from typing import Optional, Tuple

from models import EmailAccount
from services.sender import (
    SMTP_TIMEOUT_SEC,
    _extract_code_text_from_exception,
    _is_blocked,
    _is_invalid_creds,
    _is_proxy_error,
    _is_rate_limit,
    _is_web_login_required,
    _marker,
    _smtp_host_port,
    normalize_send_error,
)
from services.smtp_block_control import is_smtp_account_block_error

logger = logging.getLogger(__name__)


def _err_from_docmd(code: int, resp: bytes | str) -> str:
    if isinstance(resp, bytes):
        text = resp.decode("utf-8", "ignore")
    else:
        text = str(resp or "")
    return f"{code} {text}".strip()


def _classify_status(err: str) -> Tuple[Optional[str], str]:
    """
    (status_to_set, short_reason)
    None status = не менять (проблема прокси/таймаут).
    """
    norm = normalize_send_error(err)
    if is_smtp_account_block_error(norm):
        return "smtp_blocked", norm
    kind = norm.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind in ("ACCOUNT_INVALID_CREDENTIALS", "ACCOUNT_WEB_LOGIN_REQUIRED"):
        return "bad", norm
    if kind in ("PROXY_ERROR", "SMTP_TIMEOUT"):
        return None, norm
    if "proxy" in norm.lower() or "timeout" in norm.lower():
        return None, norm
    return "error", norm


def _classify_exception(e: Exception) -> Tuple[Optional[str], str]:
    code, text = _extract_code_text_from_exception(e)
    if _is_proxy_error(e, text):
        return None, _marker("PROXY_ERROR", code or "socks", text or str(e))
    if _is_invalid_creds(code, text):
        return "bad", _marker("ACCOUNT_INVALID_CREDENTIALS", code, text)
    if _is_web_login_required(text):
        return "bad", _marker("ACCOUNT_WEB_LOGIN_REQUIRED", code, text)
    if _is_rate_limit(code, text) or _is_blocked(code, text):
        return "smtp_blocked", _marker("ACCOUNT_RATE_LIMIT", code, text)
    err = f"{type(e).__name__}: {code or ''} {text}".strip() or str(e)
    return _classify_status(err)


def check_smtp_account_sync(account: EmailAccount) -> Tuple[Optional[str], Optional[str]]:
    """
    Проверка SMTP (прокси должен быть уже применён через ProxySMTPContext).

    Returns:
        (new_status, last_error) — new_status None = статус в БД не менять.
    """
    email = (account.email or "").strip()
    pwd = (account.password or "").strip()
    if not email or not pwd:
        return "bad", "Пустой email или пароль"

    host, port = _smtp_host_port(getattr(account, "provider", "") or "", email)
    probe_rcpt = f"smtp-probe-{account.id or 0}@invalid.local"

    s: smtplib.SMTP | None = None
    try:
        s = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SEC)
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(email, pwd)

        code, resp = s.docmd("MAIL", f"FROM:<{email}>")
        if code and int(code) >= 400:
            err = _err_from_docmd(int(code), resp)
            if is_smtp_account_block_error(err):
                return "smtp_blocked", err
            st, _ = _classify_status(err)
            return st or "smtp_blocked", err

        code, resp = s.docmd("RCPT", f"TO:<{probe_rcpt}>")
        if code and int(code) >= 400:
            err = _err_from_docmd(int(code), resp)
            if is_smtp_account_block_error(err):
                return "smtp_blocked", err
            # Отказ фиктивному получателю — SMTP и лимиты в порядке
        try:
            s.rset()
        except Exception:
            pass

        logger.info("[SMTP check] OK %s", email)
        return "active", None

    except Exception as e:
        st, err = _classify_exception(e)
        logger.warning("[SMTP check] FAIL %s: %s", email, err)
        return st or "error", err
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass


async def check_smtp_account_with_proxy(
    session,
    user_id: int,
    account: EmailAccount,
) -> Tuple[Optional[str], Optional[str]]:
    """Проверка через SOCKS5 с ротацией прокси при сбое туннеля."""
    from proxy_manager import ProxySMTPContext
    from services.smtp_proxy_send import choose_required_proxy
    from services.sender import is_definite_proxy_failure, should_retry_send_with_other_proxy
    from services.proxy_manager import ProxyManager

    last_err: str | None = None
    tried_ids: set[int] = set()

    while True:
        proxy, pick_err = await choose_required_proxy(session, user_id, exclude_ids=tried_ids)
        if pick_err:
            return None, pick_err
        if not proxy:
            break

        pid = int(proxy.id)
        tried_ids.add(pid)

        async with ProxySMTPContext(proxy):
            import asyncio

            st, err = await asyncio.to_thread(check_smtp_account_sync, account)

        if st is not None:
            return st, err

        last_err = err
        if not should_retry_send_with_other_proxy(err):
            return None, err

        try:
            await ProxyManager.note_proxy_failure(
                session, pid, (err or "")[:500], deactivate=is_definite_proxy_failure(err)
            )
        except Exception:
            pass

    return None, last_err or "PROXY_ERROR|no_active_proxy"
