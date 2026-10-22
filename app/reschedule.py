"""Перенос доставки: извинение клиенту, варианты времени, подарок за неудобство.

Сценарии:
  * Артур не успевает → в боте «⏰ Перенести доставки» → клиентам уходит извинение со ссылкой,
    где они выбирают время (и подарок, если «на выбор»). Заказ сразу ставится на первый вариант,
    чтобы не потерялся. Через RESCHEDULE_REMIND_MIN бот напоминает про тех, кто не ответил.
  * Клиенту неудобно → по ссылке /r/<token> он сам выбирает другой день/рейс, Артуру приходит уведомление.
Выбор всегда делается на странице /r/<token> — так одна и та же ссылка работает в Telegram, MAX,
SMS и WhatsApp.
"""

import logging
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as sv
from app.config import settings
from app.db import Order, Reschedule

log = logging.getLogger(__name__)

# заготовки вариантов для мастера в боте (ключ -> подпись кнопки)
PRESETS = {
    "today_evening": "Сегодня вечером",
    "next_same": "Завтра, тот же рейс",
    "next_day": "Завтра днём",
    "next_evening": "Завтра вечером",
    "after_next": "Послезавтра днём",
}
# что можно выбрать подарком: «на выбор клиента» или конкретный
BONUS_MODES = {
    "choice": "🎁 На выбор: морковь / свёкла / лук",
    "carrot": "🥕 5 кг моркови",
    "beet": "🟣 5 кг свёклы",
    "onion": "🧅 5 кг лука",
    "none": "Без подарка",
}
SELF_DAYS = 7  # на сколько дней вперёд клиент может перенести сам


def _now() -> datetime:
    return datetime.now(settings.zone)


def _evening_open(day: date) -> bool:
    """Вечерний рейс на этот день ещё можно предложить (до его начала больше часа)."""
    hh, mm = map(int, settings.evening_start.split(":"))
    start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=settings.zone)
    return start - _now() > timedelta(hours=1)


def option(day: date, slot: str) -> dict:
    return {"date": day.isoformat(), "slot": slot, "label": sv.day_slot_label(day, slot)}


def preset_options(day: date, slot: str, keys: list[str]) -> list[dict]:
    """Варианты для заказа с исходным днём/рейсом. Невозможные (вечер, который уже прошёл) пропускаем,
    повторы — тоже."""
    base = max(day, sv.today())
    nxt = base + timedelta(days=1)
    make = {
        "today_evening": (sv.today(), "evening") if slot != "evening" and _evening_open(sv.today()) else None,
        "next_same": (nxt, slot),
        "next_day": (nxt, ""),
        "next_evening": (nxt, "evening"),
        "after_next": (base + timedelta(days=2), ""),
    }
    out, seen = [], set()
    for k in keys:
        v = make.get(k)
        if v and v not in seen and v != (day, slot):
            seen.add(v)
            out.append(option(*v))
    return out


def available_presets(day: date, slot: str) -> list[str]:
    """Какие заготовки имеют смысл для этого дня (для кнопок мастера)."""
    return [k for k in PRESETS if preset_options(day, slot, [k])]


def self_options(o: Order) -> list[dict]:
    """Что клиент может выбрать сам: сегодняшний вечер (если ещё рано) и следующие дни, днём и вечером."""
    t = sv.today()
    out = []
    if _evening_open(t):
        out.append(option(t, "evening"))
    for i in range(1, SELF_DAYS + 1):
        d = t + timedelta(days=i)
        out += [option(d, ""), option(d, "evening")]
    return [x for x in out if (date.fromisoformat(x["date"]), x["slot"]) != (o.date, o.slot)]


def pending(s: Session, o: Order) -> Reschedule | None:
    return s.scalar(select(Reschedule).where(Reschedule.order_id == o.id, Reschedule.status == "pending")
                    .order_by(Reschedule.id.desc()).limit(1))


def _when(o: Order) -> str:
    """«сегодня 12:00–13:00» / «завтра вечером (18:00–21:00)» — что было обещано."""
    label = sv.day_slot_label(o.date, o.slot).lower()
    if o.slot != "evening" and o.eta_from and o.eta_to:
        label = label.replace(" днём", "") + f" {o.eta_from}–{o.eta_to}"
    return label


def offer(s: Session, o: Order, keys: list[str], bonus_mode: str, created_by: int | None = None) -> Reschedule | None:
    """Предложение перенести заказ. Заказ сразу ставим на первый вариант, фиксированный подарок кладём сразу."""
    opts = preset_options(o.date, o.slot, keys)
    if not opts or o.status != "new":
        return None
    for old in s.scalars(select(Reschedule).where(Reschedule.order_id == o.id, Reschedule.status == "pending")):
        old.status = "answered"  # прежнее предложение больше не актуально
    r = Reschedule(order=o, kind="staff", options=opts, old_date=o.date, old_slot=o.slot, old_eta=_when(o),
                   bonus_choices=list(sv.BONUSES) if bonus_mode == "choice" else [], created_by=created_by)
    s.add(r)
    if bonus_mode in sv.BONUSES:
        sv.apply_bonus(s, o, bonus_mode)
    first = opts[0]
    sv.move_order(o, date.fromisoformat(first["date"]), first["slot"])
    s.flush()
    return r


def preview(o: Order, keys: list[str], bonus_mode: str) -> str:
    """Как будет выглядеть извинение — без записи в базу."""
    r = Reschedule(kind="staff", options=preset_options(o.date, o.slot, keys), old_eta=_when(o),
                   bonus_choices=list(sv.BONUSES) if bonus_mode == "choice" else [])
    r.order = o
    return offer_text(r, gift=bonus_mode if bonus_mode in sv.BONUSES else "")


def answer(s: Session, r: Reschedule, idx: int, bonus: str = "") -> str:
    """Клиент (или Артур за него) выбрал вариант idx и, если нужно, подарок. Возвращает подпись варианта."""
    opt = r.options[idx]
    o = r.order
    sv.move_order(o, date.fromisoformat(opt["date"]), opt["slot"])
    if r.bonus_choices:
        sv.apply_bonus(s, o, bonus if bonus in r.bonus_choices else r.bonus_choices[0])
    r.status, r.chosen, r.answered_at = "answered", idx, datetime.now()
    return opt["label"]


def client_move(s: Session, o: Order, day: date, slot: str) -> Reschedule:
    """Клиент сам перенёс доставку (без предложения от нас)."""
    r = Reschedule(order=o, kind="client", options=[option(day, slot)], old_date=o.date, old_slot=o.slot,
                   old_eta=_when(o), status="answered", chosen=0, answered_at=datetime.now())
    s.add(r)
    sv.move_order(o, day, slot)
    s.flush()
    return r


def due_reminders(s: Session) -> list[Reschedule]:
    """Не ответили за RESCHEDULE_REMIND_MIN — пора напомнить Артуру (один раз)."""
    edge = datetime.now() - timedelta(minutes=settings.reschedule_remind_min)
    q = select(Reschedule).where(Reschedule.status == "pending", Reschedule.reminded.is_(False),
                                 Reschedule.created_at <= edge)
    return [r for r in s.scalars(q) if r.order and r.order.status == "new"]


# ---------- тексты и ссылки ----------


def link(o: Order) -> str:
    base = settings.webapp_url.rstrip("/")
    return f"{base}/r/{o.token}" if base and o.token else ""


def _phone() -> str:
    return sv.fmt_phone(settings.contact_phone) if settings.contact_phone else ""


def offer_text(r: Reschedule, gift: str | None = None) -> str:
    """Извинение клиенту (Telegram, MAX, WhatsApp). gift — подарок для предпросмотра (ещё не положен в заказ)."""
    o = r.order
    gift = o.bonus if gift is None else gift
    name = (o.client.name or "").split()[0] if o.client and o.client.name else ""
    lines = [f"{name + ', з' if name else 'З'}дравствуйте! Извините, доставка заказа №{o.id} "
             f"на {r.old_eta} переносится — задержались на закупке товара."]
    if r.bonus_choices:
        lines.append(f"В качестве извинения дарим {sv.fmt_qty(settings.bonus_kg)} кг на выбор: "
                     "морковь, свёклу или лук 🎁")
    elif gift in sv.BONUSES:
        lines.append(f"В качестве извинения дарим {sv.bonus_label(gift)} 🎁")
    lines.append("Выберите удобное время: " + "; ".join(x["label"] for x in r.options) + ".")
    url = link(o)
    if url:
        lines.append(f"Выбрать: {url}")
    else:
        lines.append("Ответьте, какой вариант удобнее" + (f", или позвоните {_phone()}." if _phone() else "."))
    return "\n".join(lines)


def offer_sms(r: Reschedule) -> str:
    """Коротко для SMS: извинение + ссылка."""
    url = link(r.order)
    gift = " Дарим 5 кг овощей." if (r.bonus_choices or r.order.bonus) else ""
    return f"Извините, доставку №{r.order.id} переносим.{gift} Выберите время: {url}" if url else \
        f"Извините, доставку №{r.order.id} переносим. Перезвоним."


def wa_link(r: Reschedule) -> str:
    phone = sv.normalize_phone(r.order.client.phone if r.order.client else "")
    if len(phone) != 11 or phone[0] != "7":
        return ""
    return f"https://wa.me/{phone}?text={quote(offer_text(r))}"


def chosen_text(o: Order, label: str) -> str:
    """Подтверждение клиенту после выбора."""
    gift = f" Подарок: {sv.bonus_label(o.bonus)} 🎁." if o.bonus else ""
    return f"Записали: {label}. Заказ №{o.id}.{gift} Спасибо за понимание!"


# ---------- рассылка ----------


async def send_offer(s: Session, r: Reschedule) -> list[str]:
    """Шлёт предложение по подключённым каналам (Telegram с кнопкой, MAX, SMS). Не падает."""
    from app import notify

    sent = []
    o, c = r.order, r.order.client
    text, url = offer_text(r), link(r.order)
    if c.tg_chat_id:
        try:
            kb = None
            if url.startswith("https://"):
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🕑 Выбрать время", url=url)]])
            await notify.send_tg(c.tg_chat_id, text, reply_markup=kb)
            sent.append("telegram")
        except Exception:
            log.exception("Перенос: Telegram не отправлен (заказ %s)", o.id)
    if c.max_chat_id and settings.max_bot_token:
        try:
            await notify.send_max(c.max_chat_id, text)
            sent.append("max")
        except Exception:
            log.exception("Перенос: MAX не отправлен (заказ %s)", o.id)
    phone = sv.normalize_phone(c.phone)
    if settings.sms_ru_api_id and len(phone) == 11:
        try:
            await notify.send_sms(phone, offer_sms(r))
            sent.append("sms")
        except Exception:
            log.exception("Перенос: SMS не отправлено (заказ %s)", o.id)
    return sent


async def tell_client(o: Order, text: str):
    """Подтверждение клиенту в мессенджеры, если подписан (SMS не тратим)."""
    from app import notify

    c = o.client
    for name, chat, send in (("telegram", c.tg_chat_id, notify.send_tg),
                             ("max", c.max_chat_id if settings.max_bot_token else "", notify.send_max)):
        if chat:
            try:
                await send(chat, text)
            except Exception:
                log.exception("Подтверждение переноса (%s, заказ %s) не отправлено", name, o.id)
