"""Context guard: SMTP must run only inside ProxySMTPContext."""
from __future__ import annotations

import contextvars
from typing import Optional

_smtp_via_proxy: contextvars.ContextVar[bool] = contextvars.ContextVar("smtp_via_proxy", default=False)


def smtp_proxy_guard_enter() -> contextvars.Token[bool]:
    return _smtp_via_proxy.set(True)


def smtp_proxy_guard_exit(token: contextvars.Token[bool]) -> None:
    _smtp_via_proxy.reset(token)


def smtp_proxy_required_error() -> Optional[str]:
    if not _smtp_via_proxy.get():
        return "PROXY_ERROR|no_proxy_context|SMTP send without proxy is forbidden"
    return None
