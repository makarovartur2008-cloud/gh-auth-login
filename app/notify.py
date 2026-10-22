"""Сообщения клиенту о заказе: шаблоны, ссылки-подписки и рассылка по каналам.

Каналы: Telegram (клиент открыл бота по ссылке), MAX (то же в мессенджере MAX), SMS через sms.ru
(если задан SMS_RU_API_ID). WhatsApp — только вручную, кнопкой wa.me у карточки заказа.
"""

import logging
from datetime import timedelta
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app import services as sv
from app.config import settings
from app.db import Order

log = logging.getLogger(__name__)

KINDS = ("accepted", "eta", "on_the_way")
SMS_KINDS = ("accepted", "eta")  # SMS платные — только самое важное
CHANNEL_NAMES = {"telegram": "Telegram", "max": "MAX", "sms": "SMS"}

# username ботов — заполняются при запуске (bot.get_me() / MAX GET /me)
TG_BOT_USERNAME = ""
MAX_BOT_USERNAME = ""


# ---------- ссылки ----------


def tg_link(o: Order) -> str:
    return f"https://t.me/{TG_BOT_USERNAME}?start=c_{o.token}" if TG_BOT_USERNAME and o.token else ""


def max_link(o: Order) -> str:
    return f"https://max.ru/{MAX_BOT_USERNAME}?start=c_{o.token}" if MAX_BOT_USERNAME and o.token else ""


def wa_link(o: Order) -> str:
    """Ссылка «написать в WhatsApp» с готовым текстом — Артур отправляет сам одним нажатием."""
    phone = sv.normalize_phone(o.client.phone if o.client else "")
    if len(phone) != 11 or phone[0] != "7":
        return ""
    return f"https://wa.me/{phone}?text={quote(message_text(o, status_kind(o)))}"


def find_by_token(s: Session, token: str) -> Order | None:
    if not token:
        return None
    return s.scalar(sv.order_query().where(Order.token == token))


# ---------- тексты ----------


def _phone() -> str:
    return sv.fmt_phone(settings.contact_phone) if settings.contact_phone else ""


def _questions() -> str:
    return f" Вопросы: {_phone()}" if settings.contact_phone else ""


def _eta_head(o: Order) -> str:
    t = sv.today()
    word = {t: "Сегодня", t + timedelta(days=1): "Завтра"}.get(o.date)
    return f"{word} привезём ваш заказ №{o.id}" if word else f"Привезём ваш заказ №{o.id} {sv.fmt_date(o.date)}"


def status_kind(o: Order) -> str:
    """Что сообщить «сейчас»: время, если маршрут уже посчитан, иначе — что заказ принят."""
    return "eta" if sv.eta_text(o) else "accepted"


def message_text(o: Order, kind: str) -> str:
    eta = sv.eta_text(o)
    if kind == "accepted":
        when = f"Привезём {sv.fmt_date(o.date)}" + (f", {eta}." if eta else ".")
        if not eta:
            when += (" Перед приездом позвоним." if o.date <= sv.today()
                     else " Точное время сообщим накануне вечером.")
        return f"Заказ №{o.id} принят! {when} Сумма: {sv.fmt_money(o.total)}.{_questions()}"
    if kind == "eta":
        when = f", {eta}" if eta else ""
        return f"{_eta_head(o)}{when}. Сумма: {sv.fmt_money(o.total)}.{_questions()}"
    if kind == "on_the_way":
        return f"Выехал к вам, буду минут через 15–30.{_questions()}"
    raise ValueError(kind)


def sms_text(o: Order, kind: str) -> str:
    """Коротко, чтобы влезть в 1–2 SMS (70 символов кириллицы на сегмент)."""
    phone = sv.normalize_phone(settings.contact_phone)
    tel = f" Тел. 8{phone[1:]}" if len(phone) == 11 else ""
    when = o.date.strftime("%d.%m")
    eta = f" {o.eta_from}-{o.eta_to}" if o.eta_from and o.eta_to else ""
    if kind == "accepted":
        return f"Заказ №{o.id} принят. Привезём {when}{eta}.{tel}"
    return f"Заказ №{o.id} привезём {when}{eta}.{tel}"


def subscribed_text(o: Order) -> str:
    """Ответ клиенту сразу после подписки: подтверждение + текущий статус."""
    head = f"Вы подписаны на заказ №{o.id} ✅ Сюда придёт время доставки и сообщение, когда выедем."
    if o.status == "done":
        return f"Заказ №{o.id} уже доставлен. Спасибо!"
    if o.status == "cancelled":
        return f"Заказ №{o.id} отменён.{_questions()}"
    return head + "\n\n" + message_text(o, status_kind(o))


def auto_reply_text() -> str:
    return f"Спасибо! По вопросам звоните {_phone()}." if settings.contact_phone else \
        "Спасибо! Этот бот только присылает статус заказа."


# ---------- каналы ----------


async def send_tg(chat_id: int, text: str):
    from app import bot as tgbot

    if tgbot.BOT is None:
        raise RuntimeError("Telegram-бот не запущен")
    await tgbot.BOT.send_message(chat_id, text, parse_mode=None)


async def send_max(chat_ref: str, text: str):
    from app import max_bot

    await max_bot.send_message(chat_ref, text)


async def send_sms(phone: str, text: str):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://sms.ru/sms/send",
                        params=dict(api_id=settings.sms_ru_api_id, to=phone, msg=text, json=1))
    data = r.json()
    one = (data.get("sms") or {}).get(phone) or {}
    if data.get("status") != "OK" or one.get("status") != "OK":
        raise RuntimeError(f"sms.ru: {one.get('status_text') or data.get('status_text') or r.text[:200]}")


async def notify_client(s: Session, order: Order, kind: str) -> list[str]:
    """Отправляет клиенту сообщение kind по всем доступным каналам. Возвращает, какие каналы сработали.
    Никогда не падает: ошибка одного канала только пишется в лог."""
    sent = []
    try:
        c = order.client
        text = message_text(order, kind)
        jobs = []
        if c.tg_chat_id:
            jobs.append(("telegram", send_tg(c.tg_chat_id, text)))
        if c.max_chat_id and settings.max_bot_token:
            jobs.append(("max", send_max(c.max_chat_id, text)))
        phone = sv.normalize_phone(c.phone)
        if settings.sms_ru_api_id and kind in SMS_KINDS and len(phone) == 11:
            jobs.append(("sms", send_sms(phone, sms_text(order, kind))))
        for name, job in jobs:
            try:
                await job
                sent.append(name)
            except Exception:
                log.exception("Сообщение клиенту (%s, заказ %s) не отправлено", name, order.id)
    except Exception:
        log.exception("notify_client упал, заказ %s", getattr(order, "id", "?"))
    return sent


def channels_label(sent: list[str]) -> str:
    return ", ".join(CHANNEL_NAMES.get(x, x) for x in sent)


# ---------- вечерняя рассылка ----------


async def evening_eta(Session_) -> int:
    """Вечером: считаем маршрут на завтра и шлём каждому клиенту примерное время. Один раз на заказ."""
    from app import routing

    tomorrow = sv.today() + timedelta(days=1)
    count = 0
    with Session_() as s:
        try:
            await routing.plan_day(s, tomorrow)
        except Exception:
            log.exception("Маршрут на завтра не посчитан — шлём без времени")
        orders = s.scalars(sv.order_query().where(Order.date == tomorrow, Order.status == "new",
                                                  Order.eta_notified.is_(False))).all()
        for o in orders:
            if await notify_client(s, o, "eta"):
                o.eta_notified = True
                count += 1
                s.commit()
    return count

