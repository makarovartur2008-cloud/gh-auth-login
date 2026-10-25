"""Копия базы одним файлом: бот каждый вечер присылает её владельцу (на Amvera «Начальный» своих копий нет)."""

import sqlite3
from pathlib import Path

from app.config import settings


def make_copy(dst: Path) -> Path:
    """Целостная копия SQLite, даже если база в этот момент пишется."""
    src = sqlite3.connect(settings.db_path)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def counts(path: Path) -> tuple[int, int]:
    """(клиентов, заказов) в копии — для подписи к файлу."""
    con = sqlite3.connect(path)
    try:
        return (con.execute("select count(*) from clients").fetchone()[0],
                con.execute("select count(*) from orders").fetchone()[0])
    finally:
        con.close()
