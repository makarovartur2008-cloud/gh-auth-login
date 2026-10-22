"""Бот в мессенджере MAX (dev.max.ru): клиент открывает https://max.ru/<бот>?start=c_<token>
и подписывается на статус заказа. Long polling через GET /updates.

Что взято из документации dev.max.ru:
  - адрес API https://platform-api2.max.ru, токен в заголовке Authorization;
  - GET /me → username бота; GET /updates?marker&timeout&types → {"updates": [...], "marker": N};
  - событие bot_started: chat_id, user, payload (строка из ссылки ?start=, до 128 символов);
  - POST /messages?user_id=… (или chat_id=…), тело {"text": "..."} (до 4000 символов).
Не подтверждено документацией (сделано «как в SDK»): user.user_id внутри bot_started и
message.sender.user_id / message.recipient.chat_id в message_created.
"""

import asyncio
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)
BASE = "https://platform-api2.max.ru"
POLL_TIMEOUT = 30


def _headers() -> dict:
    return {"Authorization": settings.max_bot_token}


async def api(method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=BASE, timeout=POLL_TIMEOUT + 15) as c:
        r = await c.request(method, path, params=params, json=json, headers=_headers())
    r.raise_for_status()
    return r.json() if r.content else {}


def chat_ref(user_id, chat_id) -> str:
    """Что сохранить в client.max_chat_id: id пользователя (надёжнее) или «c<chat_id>»."""
    if user_id:
        return str(user_id)
    return f"c{chat_id}" if chat_id else ""


async def send_message(ref: str, text: str):
    if not settings.max_bot_token or not ref:
        raise RuntimeError("MAX не настроен или клиент не подписан")
    params = {"chat_id": ref[1:]} if ref.startswith("c") else {"user_id": ref}
    await api("POST", "/messages", params=params, json={"text": text[:4000]})


async def handle_update(upd: dict):
    from app import notify
    from app.db import SessionLocal

    kind = upd.get("update_type")
    if kind == "bot_started":
        user = upd.get("user") or {}
        ref = chat_ref(user.get("user_id"), upd.get("chat_id"))
        payload = upd.get("payload") or ""
        if not ref:
            return
        if not payload.startswith("c_"):
            return await send_message(ref, "Здравствуйте! Здесь будут сообщения о вашем заказе. "
                                      + notify.auto_reply_text())
        with SessionLocal() as s:
            o = notify.find_by_token(s, payload[2:])
            if not o:
                text = "Ссылка устарела или с ошибкой. " + notify.auto_reply_text()
            else:
                o.client.max_chat_id = ref
                s.commit()
                text = notify.subscribed_text(o)
        await send_message(ref, text)
    elif kind == "message_created":
        msg = upd.get("message") or {}
        sender = msg.get("sender") or {}
        if sender.get("is_bot"):
            return
        ref = chat_ref(sender.get("user_id"), (msg.get("recipient") or {}).get("chat_id"))
        if ref:
            await send_message(ref, notify.auto_reply_text())


async def poll_forever():
    """Бесконечный цикл long polling. Ошибки пишем в лог и повторяем с паузой — приложение не падает."""
    from app import notify

    marker, fails = None, 0
    while True:
        try:
            if not notify.MAX_BOT_USERNAME:
                notify.MAX_BOT_USERNAME = (await api("GET", "/me")).get("username") or ""
            params = {"timeout": POLL_TIMEOUT, "types": "bot_started,message_created"}
            if marker is not None:
                params["marker"] = marker
            data = await api("GET", "/updates", params=params)
            marker = data.get("marker", marker)
            fails = 0
            for upd in data.get("updates") or []:
                try:
                    await handle_update(upd)
                except Exception:
                    log.exception("MAX: не обработано событие %s", upd.get("update_type"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails += 1
            delay = min(300, 2 ** min(fails, 8))
            log.warning("MAX: ошибка опроса (%s), повтор через %s с", e, delay)
            await asyncio.sleep(delay)
