"""Фото товаров: лежат в папке photos рядом с базой (на Amvera — /data/photos, не пропадают при обновлениях)."""

import base64
import re
import secrets
from pathlib import Path

from app.config import settings

MAX_BYTES = 3 * 1024 * 1024  # приложение само ужимает фото до ~1000 px, это с запасом
NAME_RE = re.compile(r"^\d+-[0-9a-f]{8}\.(jpg|png|webp)$")
# расширение по первым байтам файла — расширению из браузера не верим
MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))


class PhotoError(ValueError):
    pass


def folder() -> Path:
    return Path(settings.db_path).resolve().parent / "photos"


def kind(raw: bytes) -> str:
    for magic, ext in MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    raise PhotoError("Это не фото (нужен JPG, PNG или WebP)")


def save(product_id: int, data_url: str) -> str:
    """data:image/...;base64,... → имя файла. Старое фото удаляет вызывающий (remove)."""
    head, _, b64 = data_url.partition(",")
    if not head.startswith("data:image/") or ";base64" not in head:
        raise PhotoError("Не получилось прочитать фото")
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        raise PhotoError("Не получилось прочитать фото") from None
    if len(raw) > MAX_BYTES:
        raise PhotoError("Фото слишком большое")
    name = f"{product_id}-{secrets.token_hex(4)}.{kind(raw)}"
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / name).write_bytes(raw)
    return name


def path(name: str) -> Path | None:
    """Файл фото по имени из адреса — только наши имена, без «../»."""
    if not NAME_RE.match(name or ""):
        return None
    p = folder() / name
    return p if p.is_file() else None


def remove(name: str):
    if p := path(name):
        p.unlink(missing_ok=True)


def url(name: str) -> str:
    return f"/photos/{name}" if name else ""
