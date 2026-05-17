"""Выбор HTML-шаблонов строго по сервису GAG (tutti / post / ricardo)."""

from __future__ import annotations

from pathlib import Path

from services.gag_keys import gag_service_for_html_dir, is_valid_gag_service, normalize_gag_service

HTMLCH_ROOT = Path("data") / "HTMLch"

GO_FILENAME = "confirmation.html"
BACK_FILENAME = "return.html"


def html_subdir_for_service(service_code: str | None) -> str | None:
    """Имя папки data/HTMLch/<subdir>/ или None если сервис не выбран."""
    if not is_valid_gag_service(service_code):
        return None
    sub = gag_service_for_html_dir(service_code)
    return sub or None


def html_template_path(service_code: str | None, filename: str) -> Path | None:
    """Путь к файлу только внутри папки сервиса (без fallback на корень HTMLch)."""
    sub = html_subdir_for_service(service_code)
    if not sub:
        return None
    p = HTMLCH_ROOT / sub / filename
    return p if p.is_file() else None


def list_html_templates_for_service(service_code: str | None) -> list[str]:
    sub = html_subdir_for_service(service_code)
    if not sub:
        return []
    d = HTMLCH_ROOT / sub
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.glob("*.html"))


def service_label_for_path(subdir: str) -> str:
    """Человекочитаемое имя сервиса по имени папки."""
    if subdir == "post_ch":
        return "ПОСТ (post.ch)"
    if subdir == "tutti_ch":
        return "ТУТТИ"
    if subdir == "ricardo_ch":
        return "Ricardo.ch"
    return subdir


def canonical_service_name(service_code: str | None) -> str | None:
    return normalize_gag_service(service_code)


async def load_html_for_user(
    session,
    user,
    *,
    gag_service_key: str,
    filename: str,
) -> tuple[str, str | None, str | None]:
    """
    Загрузить HTML только из папки выбранного сервиса.

    Returns:
        (html_text, subdir, error_message)
    """
    from services.user_settings import get_user_setting

    raw = (await get_user_setting(session, user, gag_service_key) or "").strip()
    if not is_valid_gag_service(raw):
        return (
            "",
            None,
            "Не выбран сервис GAG. Открой 👤 Профиль → 🧭 Выбор сервиса (ТУТТИ / ПОСТ / Ricardo).",
        )
    sub = html_subdir_for_service(raw)
    p = html_template_path(raw, filename)
    if not p:
        label = service_label_for_path(sub or "")
        return "", sub, f"Шаблон <code>{filename}</code> не найден для сервиса <b>{label}</b>."
    try:
        return p.read_text(encoding="utf-8", errors="ignore"), sub, None
    except Exception as e:
        return "", sub, f"Ошибка чтения шаблона: {e}"
