"""Статус ящика в UI: 🔴 только блок SMTP или неверный пароль, не прокси."""

from __future__ import annotations

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount
from services.smtp_account_check import is_account_no_access_error

_PROXY_STATUS = frozenset({"proxy_error", "proxy", "dead_proxy"})
_CRED_STATUS = frozenset({"bad", "invalid", "no_access", "invalid_credentials"})


def _st(acc: EmailAccount) -> str:
    return (acc.status or "active").strip().lower()


def is_proxy_related_account_state(
    status: str | None,
    last_error: str | None = None,
) -> bool:
    st = (status or "").strip().lower()
    if st in _PROXY_STATUS:
        return True
    err = (last_error or "").lower()
    if not err:
        return False
    markers = (
        "proxy_error",
        "no_active_proxy",
        "no_proxy_assigned",
        "dead_proxy",
        "bound_proxy",
        "socks",
        "smtp_timeout|pool",
        "connection unexpectedly closed",
        "smtpserverdisconnected",
        "starttls extension not supported",
    )
    return any(m in err for m in markers)


def is_account_credentials_dead(
    status: str | None,
    last_error: str | None = None,
) -> bool:
    """Неверный пароль / нет доступа — показываем 🔴."""
    st = (status or "").strip().lower()
    if st in _CRED_STATUS:
        return True
    if st == "error" and is_account_no_access_error(last_error):
        return True
    return is_account_no_access_error(last_error)


def is_account_smtp_blocked_status(status: str | None) -> bool:
    return (status or "").strip().lower() == "smtp_blocked"


def is_account_active_for_mailing(status: str | None) -> bool:
    st = (status or "active").strip().lower()
    return st in ("active", "enabled", "")


def account_list_emoji(acc: EmailAccount) -> str:
    if is_account_smtp_blocked_status(acc.status):
        return "🟡"
    if is_account_credentials_dead(acc.status, acc.last_error):
        return "🔴"
    return "🟢"


def is_account_truly_inactive(acc: EmailAccount) -> bool:
    """«Удалить все неактивные» — только неверный пароль, не блок SMTP и не прокси."""
    if is_account_smtp_blocked_status(acc.status):
        return False
    if is_proxy_related_account_state(acc.status, acc.last_error):
        return False
    return is_account_credentials_dead(acc.status, acc.last_error)


async def heal_accounts_mislabeled_by_proxy(session: AsyncSession, user_id: int) -> int:
    """
    proxy_error / error из‑за прокси → active.
    smtp_blocked и 🔴 по паролю не трогаем.
    """
    uid = int(user_id)
    n = 0

    res = await session.execute(
        update(EmailAccount)
        .where(EmailAccount.user_id == uid)
        .where(EmailAccount.status.in_(list(_PROXY_STATUS)))
        .values(status="active", last_error=None, proxy_id=None)
    )
    n += int(res.rowcount or 0)

    res2 = await session.execute(
        update(EmailAccount)
        .where(EmailAccount.user_id == uid)
        .where(EmailAccount.status == "error")
        .where(
            or_(
                EmailAccount.last_error.ilike("%proxy%"),
                EmailAccount.last_error.ilike("%socks%"),
                EmailAccount.last_error.ilike("%no_active_proxy%"),
                EmailAccount.last_error.ilike("%smtp_timeout%"),
                EmailAccount.last_error.ilike("%connection unexpectedly%"),
                EmailAccount.last_error.ilike("%smtpserverdisconnected%"),
            )
        )
        .values(status="active", last_error=None, proxy_id=None)
    )
    n += int(res2.rowcount or 0)

    await session.execute(
        update(EmailAccount)
        .where(EmailAccount.user_id == uid)
        .where(EmailAccount.proxy_id.isnot(None))
        .where(EmailAccount.status == "active")
        .values(proxy_id=None)
    )

    rows = (
        await session.execute(select(EmailAccount).where(EmailAccount.user_id == uid))
    ).scalars().all()
    extra = 0
    for acc in rows:
        if is_account_smtp_blocked_status(acc.status):
            continue
        if is_account_credentials_dead(acc.status, acc.last_error):
            continue
        if is_proxy_related_account_state(acc.status, acc.last_error):
            acc.status = "active"
            acc.last_error = None
            acc.proxy_id = None
            extra += 1

    if n or extra:
        await session.commit()
    return n + extra
