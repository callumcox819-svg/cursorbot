from __future__ import annotations

import re
from typing import List, Optional

import aiohttp

_WS_RE = re.compile(r"\s+")
_BAD_TRANSLATION_RE = re.compile(
    r"query length limit exceeded|max allowed query|invalid source language|langpair",
    re.I,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    t = _HTML_TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", t).strip()


def _norm_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def _split_text_for_translate(t: str, *, max_len: int = 420) -> List[str]:
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
    t = _norm_text(text)
    if not t:
        return None
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    out = await _translate_chunk_gtx(t, timeout=timeout)
    if out:
        return out
    return await _translate_chunk_mymemory(t, timeout=timeout)


async def translate_to_ru(text: str, *, preserve_blocks: bool = False) -> Optional[str]:
    """
    Translate to Russian. If preserve_blocks=True, split by blank lines / quote separators
    so each reply in a thread is translated separately (clearer UX).
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
