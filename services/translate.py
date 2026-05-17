from __future__ import annotations

import logging
import re
from typing import List, Optional

import aiohttp

from config import config

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_BAD_TRANSLATION_RE = re.compile(
    r"query length limit exceeded|max allowed query|invalid source language|langpair",
    re.I,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_DEEPSEEK_SYSTEM = (
    "Переведи текст на русский язык. Сохрани смысл, тон и переносы строк. "
    "Если фрагмент уже на русском — слегка отредактируй для ясности. "
    "Верни только перевод, без пояснений, кавычек и префиксов вроде «Перевод:»."
)


def _strip_html(text: str) -> str:
    t = _HTML_TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", t).strip()


def _norm_text(text: str, *, keep_lines: bool = False) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if keep_lines:
        lines = [_WS_RE.sub(" ", ln).strip() for ln in t.splitlines()]
        return "\n".join(ln for ln in lines if ln)
    return _WS_RE.sub(" ", t).strip()


def _translate_provider() -> str:
    p = (getattr(config, "TRANSLATE_PROVIDER", None) or "auto").strip().lower()
    return p if p in ("auto", "deepseek", "free") else "auto"


def _deepseek_enabled() -> bool:
    return bool((getattr(config, "DEEPSEEK_API_KEY", None) or "").strip())


def _chunk_max_len() -> int:
    if _deepseek_enabled() and _translate_provider() in ("auto", "deepseek"):
        return 3200
    return 420


def _split_text_for_translate(t: str, *, max_len: int | None = None) -> List[str]:
    max_len = int(max_len or _chunk_max_len())
    t = (t or "").strip()
    if not t:
        return []
    if len(t) <= max_len:
        return [t]

    chunks: List[str] = []
    buf = ""
    for part in re.split(r"(\n{2,})", t):
        if not part:
            continue
        if len(part) <= max_len and len(buf) + len(part) <= max_len:
            buf += part
            continue
        if buf.strip():
            chunks.append(buf.strip())
            buf = ""
        if len(part) <= max_len:
            buf = part
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", part):
            if not sentence:
                continue
            if len(sentence) <= max_len:
                if len(buf) + len(sentence) + 1 <= max_len:
                    buf = (buf + " " + sentence).strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = sentence
            else:
                if buf:
                    chunks.append(buf)
                    buf = ""
                for i in range(0, len(sentence), max_len):
                    chunks.append(sentence[i : i + max_len])
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


async def _translate_chunk_deepseek(text: str, *, timeout: aiohttp.ClientTimeout) -> Optional[str]:
    api_key = (getattr(config, "DEEPSEEK_API_KEY", None) or "").strip()
    if not api_key:
        return None

    base = (getattr(config, "DEEPSEEK_API_BASE", None) or "https://api.deepseek.com").rstrip("/")
    model = (getattr(config, "DEEPSEEK_MODEL", None) or "deepseek-chat").strip()
    url = f"{base}/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _DEEPSEEK_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    raw = await resp.text()
                    logger.warning("DeepSeek translate HTTP %s: %s", resp.status, raw[:400])
                    return None
                data = await resp.json(content_type=None)
                choices = (data or {}).get("choices") or []
                if not choices:
                    return None
                msg = choices[0].get("message") or {}
                out = (msg.get("content") or "").strip()
                if out.startswith("```"):
                    out = re.sub(r"^```[\w]*\n?", "", out)
                    out = re.sub(r"\n?```$", "", out).strip()
                if out and not _BAD_TRANSLATION_RE.search(out):
                    return out
    except Exception:
        logger.exception("DeepSeek translate request failed")
        return None
    return None


async def _translate_chunk_gtx(text: str, *, timeout: aiohttp.ClientTimeout) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "ru",
                    "dt": "t",
                    "q": text,
                },
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                if not isinstance(data, list) or not data or not isinstance(data[0], list):
                    return None
                parts = []
                for row in data[0]:
                    if isinstance(row, list) and row and isinstance(row[0], str):
                        parts.append(row[0])
                out = "".join(parts).strip()
                if out and not _BAD_TRANSLATION_RE.search(out):
                    return out
    except Exception:
        return None
    return None


async def _translate_chunk_mymemory(text: str, *, timeout: aiohttp.ClientTimeout) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "auto|ru"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                out = (((data or {}).get("responseData") or {}).get("translatedText") or "").strip()
                if out and not _BAD_TRANSLATION_RE.search(out):
                    return out
    except Exception:
        return None
    return None


async def _translate_single(text: str, *, timeout_sec: float = 22.0) -> Optional[str]:
    provider = _translate_provider()
    use_deepseek = provider in ("auto", "deepseek") and _deepseek_enabled()
    t = _norm_text(text, keep_lines=use_deepseek)
    if not t:
        return None

    if use_deepseek:
        ds_timeout = aiohttp.ClientTimeout(total=max(timeout_sec, 55.0))
        out = await _translate_chunk_deepseek(t, timeout=ds_timeout)
        if out:
            return out
        if provider == "deepseek":
            return None

    if provider in ("auto", "free"):
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        out = await _translate_chunk_gtx(t, timeout=timeout)
        if out:
            return out
        return await _translate_chunk_mymemory(t, timeout=timeout)

    return None


async def translate_to_ru(text: str, *, preserve_blocks: bool = False) -> Optional[str]:
    """
    Translate to Russian. If preserve_blocks=True, split by blank lines / quote separators
    so each reply in a thread is translated separately (clearer UX).

    Provider (TRANSLATE_PROVIDER):
      - auto: DeepSeek if DEEPSEEK_API_KEY set, else Google GTX + MyMemory
      - deepseek: only DeepSeek
      - free: only free APIs
    """
    raw = (text or "").strip()
    if not raw:
        return None

    if preserve_blocks:
        blocks = [b.strip() for b in re.split(r"\n(?:-{5,}|-{3,})\n|\n{2,}", raw) if b.strip()]
        if len(blocks) > 1:
            translated: List[str] = []
            for block in blocks:
                part = await _translate_single(block)
                if part:
                    translated.append(part)
            if translated:
                return "\n\n".join(translated)

    chunks = _split_text_for_translate(raw)
    if not chunks:
        return None
    if len(chunks) == 1:
        return await _translate_single(chunks[0])

    out_parts: List[str] = []
    for ch in chunks:
        part = await _translate_single(ch)
        if not part:
            return None
        out_parts.append(part)
    return "\n\n".join(out_parts)
