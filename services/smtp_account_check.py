"""Проверка SMTP-активности ящика (логин + MAIL FROM) через SOCKS5."""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from dataclasses import dataclass
from typing import Callable, Awaitable, List, Optional, Tuple

from models import EmailAccount, Proxy
from services.sender import (
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

SMTP_CHECK_TIMEOUT_SEC = max(8, min(45, int(os.getenv("SMTP_CHECK_TIMEOUT_SEC", "15"))))
SMTP_CHECK_WORKERS = max(1, min(10, int(os.getenv("ACCOUNTS_SMTP_CHECK_CONCURRENCY", "5"))))

_NO_ACCESS_KINDS = frozenset(
    {"ACCOUNT_INVALID_CREDENTIALS", "ACCOUNT_WEB_LOGIN_REQUIRED"}
)
_NO_ACCESS_PHRASES = (
    "username and password not accepted",
    "invalid credentials",
    "authentication unsuccessful",
    "web login required",
    "please log in via your web browser",
    "application-specific password required",
    "less secure app",
)


def is_account_no_access_error(err: str | None) -> bool:
    """Нет доступа к ящику (неверный пароль, web login и т.п.) — не путать с прокси."""
    norm = normalize_send_error(err or "")
    kind = norm.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind in _NO_ACCESS_KINDS:
        return True
    t = norm.lower()
    if "proxy" in t or "socks" in t or kind == "PROXY_ERROR":
        return False
    if any(p in t for p in _NO_ACCESS_PHRASES):
        return True
    if kind.startswith("ACCOUNT_INVALID") or kind.startswith("ACCOUNT_WEB"):
        return True
    return False


def is_account_no_access_status(st: str | None) -> bool:
    return (st or "").strip().lower() == "bad"


def _err_from_docmd(code: int, resp: bytes | str) -> str:
    if isinstance(resp, bytes):
        text = resp.decode("utf-8", "ignore")
    else:
        text = str(resp or "")
    return f"{code} {text}".strip()


def is_transient_smtp_check_failure(err: str | None) -> bool:
    """Прокси/туннель/рукопожатие SMTP — не значит, что ящик мёртв."""
    t = f"{type(err).__name__ if isinstance(err, BaseException) else ''} {(err or '')}".lower()
    names = (
        "smtpnotsupportederror",
        "smtpserverdisconnected",
        "smtpconnecterror",
        "timeouterror",
        "connectionerror",
        "oserror",
        "sslerror",
    )
    if any(n in t for n in names):
        return True
    phrases = (
        "starttls extension not supported",
        "connection unexpectedly closed",
        "eof occurred",
        "connection reset",
        "timed out",
        "temporarily unavailable",
        "proxy",
        "socks",
    )
    return any(p in t for p in phrases)


def _classify_status(err: str) -> Tuple[Optional[str], str]:
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
    if is_transient_smtp_check_failure(norm):
        return None, norm
    return "error", norm


def _classify_exception(e: Exception) -> Tuple[Optional[str], str]:
    code, text = _extract_code_text_from_exception(e)
    if is_transient_smtp_check_failure(str(e)) or is_transient_smtp_check_failure(text):
        return None, _marker("PROXY_ERROR", code or "smtp_check", text or str(e))
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


def _close_smtp(s: smtplib.SMTP | None) -> None:
    if s is None:
        return
    try:
        s.quit()
    except Exception:
        try:
            s.close()
        except Exception:
            pass


def _smtp_check_on_client(
    s: smtplib.SMTP,
    account: EmailAccount,
) -> Tuple[Optional[str], Optional[str]]:
    email = (account.email or "").strip()
    pwd = (account.password or "").strip()
    if not email or not pwd:
        return "bad", "Пустой email или пароль"

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

    try:
        s.rset()
    except Exception:
        pass

    logger.info("[SMTP check] OK %s", email)
    return "active", None


def _connect_smtp_via_socks(proxy: Proxy, host: str, port: int, *, timeout: float) -> smtplib.SMTP:
    """Отдельное SOCKS-соединение без глобального PySocks-патча (можно параллелить)."""
    import socks

    px_host = (proxy.host or "").strip()
    px_port = int(proxy.port or 0)
    if not px_host or not px_port:
        raise ValueError("Proxy host/port is empty")

    username = (proxy.username or "").strip() or None
    password = (proxy.password or "").strip() or None

    sock = socks.socksocket()
    sock.set_proxy(
        socks.SOCKS5,
        px_host,
        px_port,
        username=username,
        password=password,
        rdns=True,
    )
    sock.settimeout(timeout)
    sock.connect((host, int(port)))

    client = smtplib.SMTP(timeout=timeout)
    client.sock = sock
    client.file = sock.makefile("rb")
    client.host = host
    client.port = int(port)
    code, _msg = client.getreply()
    if code != 220:
        raise smtplib.SMTPConnectError(code, repr(_msg))
    return client


def check_smtp_account_via_proxy_isolated(
    proxy: Proxy,
    account: EmailAccount,
    *,
    timeout: float | None = None,
) -> Tuple[Optional[str], Optional[str]]:
    """SMTP-проверка через SOCKS5 без глобального lock (для параллельного batch)."""
    tmo = float(timeout if timeout is not None else SMTP_CHECK_TIMEOUT_SEC)
    email = (account.email or "").strip()
    host, port = _smtp_host_port(getattr(account, "provider", "") or "", email)
    s: smtplib.SMTP | None = None
    try:
        s = _connect_smtp_via_socks(proxy, host, port, timeout=tmo)
        return _smtp_check_on_client(s, account)
    except Exception as e:
        st, err = _classify_exception(e)
        logger.warning("[SMTP check] FAIL %s: %s", email, err)
        return st or "error", err
    finally:
        _close_smtp(s)


def check_smtp_account_sync(account: EmailAccount) -> Tuple[Optional[str], Optional[str]]:
    """
    Проверка SMTP (прокси должен быть уже применён через ProxySMTPContext).
    """
    email = (account.email or "").strip()
    host, port = _smtp_host_port(getattr(account, "provider", "") or "", email)
    s: smtplib.SMTP | None = None
    try:
        s = smtplib.SMTP(host, port, timeout=SMTP_CHECK_TIMEOUT_SEC)
        return _smtp_check_on_client(s, account)
    except Exception as e:
        st, err = _classify_exception(e)
        logger.warning("[SMTP check] FAIL %s: %s", email, err)
        return st or "error", err
    finally:
        _close_smtp(s)


async def check_smtp_account_with_proxy(
    session,
    user_id: int,
    account: EmailAccount,
) -> Tuple[Optional[str], Optional[str]]:
    """Одна проверка через SOCKS5 с ротацией прокси при сбое туннеля."""
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


@dataclass
class SmtpCheckResult:
    account_id: int
    email: str
    status: Optional[str]
    error: Optional[str]


async def _load_user_proxies(session, user_id: int) -> List[Proxy]:
    from sqlalchemy import select as sa_select
    from sqlalchemy import or_

    active_cond = or_(Proxy.is_active.is_(True), Proxy.is_active.is_(None))
    rows = (
        await session.execute(
            sa_select(Proxy)
            .where(Proxy.user_id == int(user_id))
            .where(active_cond)
            .order_by(Proxy.id)
        )
    ).scalars().all()
    from proxy_manager import is_socks5_proxy

    return [p for p in rows if is_socks5_proxy(p)]


async def check_smtp_accounts_parallel(
    session,
    user_id: int,
    accounts: List[EmailAccount],
    *,
    workers: int | None = None,
    on_progress: Callable[[int, int, str | None], Awaitable[None]] | None = None,
) -> List[SmtpCheckResult]:
    """
    Параллельная SMTP-проверка (несколько ящиков одновременно, отдельный SOCKS на поток).
    """
    if not accounts:
        return []

    n_workers = workers if workers is not None else SMTP_CHECK_WORKERS
    sem = asyncio.Semaphore(max(1, n_workers))
    proxies = await _load_user_proxies(session, user_id)
    total = len(accounts)
    done = 0

    async def _one(acc: EmailAccount) -> SmtpCheckResult:
        nonlocal done
        acc_id = int(acc.id)
        email = (acc.email or "").strip()
        async with sem:
            if not proxies:
                st, err = None, "PROXY_ERROR|no_active_proxy|No active proxy configured"
            else:
                proxy = proxies[acc_id % len(proxies)]
                try:
                    st, err = await asyncio.to_thread(
                        check_smtp_account_via_proxy_isolated,
                        proxy,
                        acc,
                        timeout=SMTP_CHECK_TIMEOUT_SEC,
                    )
                except Exception as e:
                    logger.exception("[SMTP check] thread crash %s", email)
                    st, err = _classify_exception(e)
        done += 1
        if on_progress:
            try:
                await on_progress(done, total, email)
            except Exception:
                logger.exception("[SMTP check] on_progress failed")
        return SmtpCheckResult(account_id=acc_id, email=email, status=st, error=err)

    tasks = [asyncio.create_task(_one(a)) for a in accounts]
    out: List[SmtpCheckResult] = []
    for fut in asyncio.as_completed(tasks):
        out.append(await fut)
    out.sort(key=lambda r: r.account_id)
    return out
