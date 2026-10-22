"""Клиентская часть Telegram-бота: подписка на статус заказа по ссылке t.me/<бот>?start=c_<token>.

Роутер подключается ДО служебного (app/bot.py), поэтому клиент никогда не попадает в /start
сотрудников и не может стать владельцем.
"""

from aiogram import Router
from aiogram.types import Message
from sqlalchemy import select

from app import notify
from app import services as sv
from app.db import Client, SessionLocal

router = Router()


def _start_payload(m: Message) -> str:
    parts = (m.text or "").split(maxsplit=1)
    if parts and parts[0].split("@")[0] == "/start" and len(parts) > 1:
        return parts[1].strip()
    return ""


def is_client_start(m: Message) -> bool:
    return _start_payload(m).startswith("c_")


def is_subscribed_client(m: Message) -> bool:
    """Не сотрудник, но уже подписан на заказ — значит, это клиент."""
    if not m.from_user:
        return False
    with SessionLocal() as s:
        if sv.get_user(s, m.from_user.id):
            return False
        return s.scalar(select(Client.id).where(Client.tg_chat_id == m.chat.id).limit(1)) is not None


@router.message(is_client_start)
async def client_start(m: Message):
    token = _start_payload(m)[2:]
    with SessionLocal() as s:
        o = notify.find_by_token(s, token)
        if not o:
            return await m.answer("Ссылка устарела или с ошибкой. " + notify.auto_reply_text(), parse_mode=None)
        o.client.tg_chat_id = m.chat.id
        s.commit()
        text = notify.subscribed_text(o)
    await m.answer(text, parse_mode=None)


@router.message(is_subscribed_client)
async def client_any(m: Message):
    await m.answer(notify.auto_reply_text(), parse_mode=None)
