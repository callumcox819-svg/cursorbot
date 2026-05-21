"""IMAP/SMTP хосты по домену почты (Gmail, GMX, iCloud)."""

from __future__ import annotations

import re
from typing import Tuple

# GMX DE/CH/AT — imap.gmx.net / mail.gmx.net
GMX_NET_DOMAINS = frozenset(
    {
        "gmx.de",
        "gmx.ch",
        "gmx.at",
        "gmx.eu",
        "gmx.org",
        "gmx.tm",
        "gmx.info",
        "gmx.biz",
        "gmx.top",
    }
)

# GMX international — imap.gmx.com / mail.gmx.com
GMX_COM_DOMAINS = frozenset(
    {
        "gmx.com",
        "gmx.net",
        "gmx.us",
        "gmx.co.uk",
    }
)

IMAP_GMAIL = ("imap.gmail.com", 993)
SMTP_GMAIL = ("smtp.gmail.com", 587)

IMAP_GMX_NET = ("imap.gmx.net", 993)
SMTP_GMX_NET = ("mail.gmx.net", 587)

IMAP_GMX_COM = ("imap.gmx.com", 993)
SMTP_GMX_COM = ("mail.gmx.com", 587)

IMAP_ICLOUD = ("imap.mail.me.com", 993)
SMTP_ICLOUD = ("smtp.mail.me.com", 587)


def email_domain(email: str) -> str:
    m = re.search(r"@([^@]+)$", (email or "").strip())
    if not m:
        raise ValueError("Некорректный email")
    return m.group(1).lower().strip()


def _gmx_cluster(domain: str) -> str | None:
    d = (domain or "").lower().strip()
    if d in GMX_NET_DOMAINS:
        return "net"
    if d in GMX_COM_DOMAINS:
        return "com"
    # поддомены и близкие зоны
    if d.endswith(".gmx.de") or d.endswith(".gmx.ch") or d.endswith(".gmx.at"):
        return "net"
    if d.endswith(".gmx.com") or d.endswith(".gmx.net"):
        return "com"
    parts = d.split(".")
    if len(parts) >= 2 and parts[-2] == "gmx":
        if parts[-1] in ("de", "ch", "at", "eu", "org"):
            return "net"
        return "com"
    if "gmx" in d:
        return "net"
    return None


def detect_mail_provider(email: str) -> Tuple[str, str, str, str]:
    """
    (imap_host, smtp_host, provider_id, provider_label)
    provider_id: gmail | gmx | icloud
    """
    domain = email_domain(email)

    if domain in ("gmail.com", "googlemail.com"):
        return IMAP_GMAIL[0], SMTP_GMAIL[0], "gmail", "Gmail"

    gmx = _gmx_cluster(domain)
    if gmx == "net":
        return IMAP_GMX_NET[0], SMTP_GMX_NET[0], "gmx", "GMX"
    if gmx == "com":
        return IMAP_GMX_COM[0], SMTP_GMX_COM[0], "gmx", "GMX"

    if domain == "icloud.com" or domain.endswith(".icloud.com"):
        return IMAP_ICLOUD[0], SMTP_ICLOUD[0], "icloud", "iCloud"

    if domain in ("me.com", "mac.com"):
        return IMAP_ICLOUD[0], SMTP_ICLOUD[0], "icloud", "iCloud"

    raise ValueError(
        f"Неизвестный домен: {domain}. Поддерживаются Gmail, GMX (gmx.de/ch/com/…), iCloud."
    )


def detect_imap_server(email: str) -> Tuple[str, str]:
    """Как в handlers/accounts: (imap_host, provider_id)."""
    imap_host, _, provider, _ = detect_mail_provider(email)
    return imap_host, provider


def smtp_host_port(email: str, provider: str = "") -> Tuple[str, int]:
    """SMTP host:port для рассылки."""
    p = (provider or "").strip().lower()
    if p == "gmail":
        return SMTP_GMAIL[0], SMTP_GMAIL[1]
    if p == "gmx":
        try:
            _, smtp_host, _, _ = detect_mail_provider(email)
            return smtp_host, 587
        except ValueError:
            return SMTP_GMX_NET[0], 587
    if p == "icloud":
        return SMTP_ICLOUD[0], SMTP_ICLOUD[1]
    try:
        _, smtp_host, _, _ = detect_mail_provider(email)
        return smtp_host, 587
    except ValueError:
        domain = email_domain(email) if "@" in (email or "") else ""
        return (f"smtp.{domain}", 587) if domain else SMTP_GMAIL


def imap_host_port(email: str, provider: str = "") -> Tuple[str, int]:
    p = (provider or "").strip().lower()
    if p == "gmail":
        return IMAP_GMAIL[0], IMAP_GMAIL[1]
    if p == "gmx":
        try:
            imap_host, _, _, _ = detect_mail_provider(email)
            return imap_host, 993
        except ValueError:
            return IMAP_GMX_NET[0], 993
    if p == "icloud":
        return IMAP_ICLOUD[0], 993
    try:
        imap_host, _, _, _ = detect_mail_provider(email)
        return imap_host, 993
    except ValueError:
        return IMAP_GMAIL[0], 993


def provider_password_hint(provider_id: str) -> str:
    p = (provider_id or "").strip().lower()
    if p == "gmail":
        return "пароль приложения Google (App Password)"
    if p == "gmx":
        return "обычный пароль GMX (в веб-почте включите POP3/IMAP)"
    if p == "icloud":
        return "пароль приложения Apple"
    return "пароль для IMAP/SMTP"
