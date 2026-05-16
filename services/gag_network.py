from __future__ import annotations

from typing import Any

import aiohttp


class GAGError(Exception):
    pass


async def _post_json(endpoint: str, payload: dict[str, Any], *, timeout_sec: float = 25.0) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, json=payload) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise GAGError(f"HTTP {resp.status}: {text[:300]}")
            try:
                data = await resp.json()
            except Exception:
                raise GAGError(f"Bad JSON: {text[:300]}")
    if not isinstance(data, dict):
        raise GAGError(f"Unexpected response: {str(data)[:300]}")
    return data


async def generate_gag_url(
    *,
    endpoint: str,
    apikey: str,
    title: str,
    price: str,
    service: str,
    name: str | None = None,
    address: str | None = None,
    image: str | None = None,
    balanceChecker: int | None = None,
    domain: int | None = None,
    version: str | int | None = None,
    timeout_sec: float = 25.0,
) -> str:
    """
  POST https://imgbeoxo.com/generate
  version: 1 = /buy/, 2 = /, lk = /get/ (default)
    """
    payload: dict[str, Any] = {
        "apikey": apikey,
        "title": title,
        "price": price,
        "service": service,
    }
    if name:
        payload["name"] = name
    if address:
        payload["address"] = address
    if image:
        payload["image"] = image
    if balanceChecker is not None:
        payload["balanceChecker"] = int(balanceChecker)
    if domain is not None:
        payload["domain"] = int(domain)
    if version is not None:
        payload["version"] = version

    data = await _post_json(endpoint, payload, timeout_sec=timeout_sec)
    url = data.get("url")
    if not url:
        raise GAGError(f"No url in response: {str(data)[:300]}")
    return str(url)


async def send_gag_email(
    *,
    endpoint: str,
    apikey: str,
    ad_id: str,
    email: str,
    mailer: str,
    status: str,
    domain: str | None = None,
    lang: str | None = None,
    subject_type: str | None = None,
    timeout_sec: float = 25.0,
) -> dict[str, Any]:
    """
    POST https://imgbeoxo.com/send-email
    mailer: anafema, gosu, hype, pravosudie
    status: one, two, twolk, refund
    """
    payload: dict[str, Any] = {
        "apikey": apikey,
        "adId": ad_id,
        "email": email,
        "mailer": mailer,
        "status": status,
    }
    if domain:
        payload["domain"] = domain
    if lang:
        payload["lang"] = lang
    if subject_type:
        payload["subject_type"] = subject_type

    data = await _post_json(endpoint, payload, timeout_sec=timeout_sec)
    if not data.get("success"):
        raise GAGError(f"send-email failed: {str(data)[:300]}")
    return data
