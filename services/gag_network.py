from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
from aiohttp import ClientError


class GAGError(Exception):
    pass


def _is_transient_network_error(err: BaseException) -> bool:
    if isinstance(err, (asyncio.TimeoutError, ClientError, ConnectionResetError, OSError)):
        return True
    msg = str(err).lower()
    return (
        "connection_lost" in msg
        or "connection reset" in msg
        or "server disconnected" in msg
        or "cannot connect" in msg
    )


async def _post_json(endpoint: str, payload: dict[str, Any], *, timeout_sec: float = 25.0) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    last_err: BaseException | None = None
    attempts = 3

    for attempt in range(attempts):
        try:
            connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.post(endpoint, json=payload) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise GAGError(f"HTTP {resp.status}: {text[:300]}")
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        raise GAGError(f"Bad JSON: {text[:300]}")
            if not isinstance(data, dict):
                raise GAGError(f"Unexpected response: {str(data)[:300]}")
            return data
        except GAGError:
            raise
        except Exception as e:
            last_err = e
            if attempt + 1 >= attempts or not _is_transient_network_error(e):
                raise
            await asyncio.sleep(1.5 * (attempt + 1))

    if last_err:
        raise last_err
    raise GAGError("GAG request failed")


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
