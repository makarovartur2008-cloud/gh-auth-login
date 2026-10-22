import logging
import secrets
import uuid
from datetime import timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app import ai, client_bot, notify
from app import services as sv
from app.config import settings
from app.db import Invite, Order, SessionLocal, User

log = logging.getLogger(__name__)
router = Router()

BTN_TODAY, BTN_TOMORROW, BTN_REPORT, BTN_DEBTS = "📋 Сегодня", "📅 Завтра", "💰 Отчёт", "📝 Долги"

# Распознанные, но ещё не подтверждённые сообщения: ключ -> (tg_id, Parsed)
PENDING: dict[str, tuple[int, ai.Parsed]] = {}

HELP = f"""<b>Как пользоваться</b>

🎤 Просто отправь <b>голосовое</b> или текст, например:
• «Марина, Ленина 5, подъезд 2, этаж 4, телефон 89001234567, два мешка белой картошки на завтра»
• «Отвёз Ивану мешок красной и 10 кг лука, заплатил 2000 переводом»
• «Заправился на 3000»
• «Ольга теперь берёт каждую неделю» (постоянный клиент)

📸 Можно переслать текст или <b>скриншот</b> переписки с Авито — бот сделает заявку.

Бот покажет, что понял, — нажми «✅ Сохранить».

{BTN_TODAY} / {BTN_TOMORROW} — список доставок с кнопками «Доставлено».
{BTN_REPORT} — прибыль за день/неделю/месяц.
📱 Приложение — клиенты, заказы на любой день, цены, расходы.

Мешок/сетка картошки = {settings.sack_kg} кг. Каждое утро в {settings.morning_hour}:00 приходит список на сегодня,
вечером в {settings.evening_hour}:00 — на завтра."""


def main_kb() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_DEBTS)]]
    if settings.webapp_url.startswith("https://"):
        rows.append([KeyboardButton(text="📱 Приложение", web_app=WebAppInfo(url=settings.webapp_url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def order_kb(o: Order) -> InlineKeyboardMarkup | None:
    if o.status == "new":
        rows = [
            [InlineKeyboardButton(text="✅ Наличка", callback_data=f"o:{o.id}:cash"),
             InlineKeyboardButton(text="✅ Перевод", callback_data=f"o:{o.id}:transfer")],
            [InlineKeyboardButton(text="📝 В долг", callback_data=f"o:{o.id}:debt"),
             InlineKeyboardButton(text="➡️ На завтра", callback_data=f"o:{o.id}:move"),
             InlineKeyboardButton(text="❌ Отмена", callback_data=f"o:{o.id}:cancel")],
            # сообщения клиенту (app/notify.py)
            [InlineKeyboardButton(text="📣 Сообщить клиенту", callback_data=f"n:{o.id}:status"),
             InlineKeyboardButton(text="🚚 Еду", callback_data=f"n:{o.id}:go")],
        ]
        wa = notify.wa_link(o)
        if wa:
            rows.append([InlineKeyboardButton(text="💬 WhatsApp", url=wa)])
    elif o.status == "done" and o.payment == "debt":
        rows = [[InlineKeyboardButton(text="💵 Оплатил наличкой", callback_data=f"o:{o.id}:cash"),
                 InlineKeyboardButton(text="💳 Перевод", callback_data=f"o:{o.id}:transfer")]]
    else:
        rows = [[InlineKeyboardButton(text="↩️ Вернуть в доставку", callback_data=f"o:{o.id}:reopen")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def allowed(tg_id: int) -> User | None:
    with SessionLocal() as s:
        return sv.get_user(s, tg_id)


# ---------- доступ ----------


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    name = m.from_user.full_name
    with SessionLocal() as s:
        user = sv.get_user(s, m.from_user.id)
        if not user and not sv.all_users(s):
            user = User(tg_id=m.from_user.id, name=name, role="owner")
            s.add(user)
            s.commit()
            await m.answer("👋 Ты владелец этого бота. Друзьям доступ даёт команда /invite.")
        elif not user and command.args and command.args.startswith("inv_"):
            inv = s.get(Invite, command.args[4:])
            if inv and not inv.used:
                inv.used = True
                user = User(tg_id=m.from_user.id, name=name, role="member")
                s.add(user)
                s.commit()
                for owner in sv.all_users(s):
                    if owner.role == "owner":
                        await m.bot.send_message(owner.tg_id, f"🤝 {sv.esc(name)} подключился к боту.")
            else:
                return await m.answer("Ссылка-приглашение недействительна. Попроси новую.")
        if not user:
            return await m.answer("⛔ Нет доступа. Попроси владельца прислать ссылку-приглашение.")
    await m.answer(HELP, reply_markup=main_kb())


@router.message(Command("help"))
async def help_(m: Message):
    if allowed(m.from_user.id):
        await m.answer(HELP, reply_markup=main_kb())


@router.message(Command("invite"))
async def invite(m: Message):
    user = allowed(m.from_user.id)
    if not user or user.role != "owner":
        return
    token = secrets.token_urlsafe(9)
    with SessionLocal() as s:
        s.add(Invite(token=token))
        s.commit()
    me = await m.bot.get_me()
    await m.answer("Отправь другу эту ссылку (одноразовая):\n"
                   f"https://t.me/{me.username}?start=inv_{token}\n\n"
                   "Заявки, которые создаёт друг, автоматически помечаются «через друга» (−"
                   f"{settings.friend_fee} ₽ из прибыли).")


@router.message(Command("users"))
async def users(m: Message):
    user = allowed(m.from_user.id)
    if not user or user.role != "owner":
        return
    with SessionLocal() as s:
        lines = [f"{'👑' if u.role == 'owner' else '👤'} {sv.esc(u.name)} — id {u.tg_id}" for u in sv.all_users(s)]
    await m.answer("Доступ есть у:\n" + "\n".join(lines) + "\n\nУбрать: /remove id")


@router.message(Command("remove"))
async def remove(m: Message, command: CommandObject):
    user = allowed(m.from_user.id)
    if not user or user.role != "owner" or not (command.args or "").strip().isdigit():
        return
    with SessionLocal() as s:
        u = sv.get_user(s, int(command.args.strip()))
        if u and u.role != "owner":
            s.delete(u)
            s.commit()
            return await m.answer("Доступ убран.")
    await m.answer("Не нашёл такого пользователя.")


# ---------- списки ----------


def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def route_head(plan, stops: int) -> str:
    """Строка о маршруте в шапке списка: выезд, км, время, ссылка на Яндекс Карты."""
    text = f"🧭 <b>Маршрут:</b> выезд {plan.departure}"
    if plan.total_km:
        text += f", ≈ {plan.total_km:g} км"
    if plan.total_min:
        text += f", ≈ {fmt_duration(plan.total_min)}"
    if not plan.osrm and stops > 1 and plan.total_km:
        text += " (по прямой)"
    if plan.yandex_url:
        text += f'\n<a href="{sv.esc(plan.yandex_url)}">🗺 Открыть маршрут в Яндекс Картах</a>'
    text += "\n<i>Время примерное, без учёта пробок.</i>"
    return text


async def send_day(bot: Bot, chat_id: int, offset: int, force_route: bool = False):
    from aiogram.types import LinkPreviewOptions

    from app import routing

    day = sv.today() + timedelta(days=offset)
    with SessionLocal() as s:
        sv.generate_recurring(s)
        # порядок объезда и примерное время на каждый адрес; пересчёт — только если заказы
        # или адреса поменялись (или нажали «Пересчитать»), чтобы у всех было одно и то же время
        try:
            plan, active = await routing.get_plan(s, day, force=force_route)
        except Exception:
            log.exception("route planning failed")
            s.rollback()
            active, plan = [o for o in sv.orders_on(s, day) if o.status == "new"], None
        orders = sv.orders_on(s, day)
        overdue = sv.open_orders_before(s, sv.today()) if offset == 0 else []
        head = f"{'📋' if offset == 0 else '📅'} <b>Доставки — {sv.fmt_date(day)}</b>\n"
        kb = None
        if not orders:
            head += "Заказов нет."
        else:
            kg = sum(i.qty for o in active for i in o.items if i.product.unit == "кг")
            head += (f"Всего: {len(orders)}, осталось отвезти: {len(active)}\n"
                     f"Сумма к получению: {sv.fmt_money(sum(o.total for o in active))}, груз ≈ {sv.fmt_qty(kg)} кг")
            load = {}
            for o in active:
                for i in o.items:
                    load[i.product.name] = load.get(i.product.name, 0) + i.qty
            if load:
                head += "\n\n<b>Загрузить:</b>\n" + "\n".join(
                    f"• {sv.esc(n)}: {sv.fmt_qty(q)}" for n, q in load.items())
            if active and plan:
                head += "\n\n" + route_head(plan, len(active))
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Пересчитать маршрут", callback_data=f"route:{day.isoformat()}")]])
        await bot.send_message(chat_id, head, reply_markup=kb,
                               link_preview_options=LinkPreviewOptions(is_disabled=True))
        for o in overdue:
            await bot.send_message(chat_id, "⚠️ <b>Не доставлен вовремя</b>\n" + sv.order_text(o, with_date=True),
                                   reply_markup=order_kb(o))
        # сначала открытые заказы по маршруту, потом уже доставленные
        for o in active + [o for o in orders if o.status != "new"]:
            text = sv.order_text(o)
            if o.status == "new" and o.route_pos and plan:
                pre = f"<b>Точка {o.route_pos} из {len(active)}</b>"
                if o.client.lat is None:
                    pre += "\n🗺❓ адрес не найден на карте — проверь"
                text = pre + "\n" + text
            await bot.send_message(chat_id, text, reply_markup=order_kb(o))


@router.callback_query(F.data.startswith("route:"))
async def route_cb(c: CallbackQuery):
    from datetime import date

    if not allowed(c.from_user.id):
        return await c.answer()
    try:
        offset = (date.fromisoformat(c.data[6:]) - sv.today()).days
    except ValueError:
        return await c.answer()
    if offset < 0:
        return await c.answer("Этот день уже прошёл")
    await c.answer("Пересчитываю маршрут…")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:  # старое сообщение уже нельзя редактировать — не страшно
        pass
    await send_day(c.bot, c.message.chat.id, offset, force_route=True)


@router.message(F.text == BTN_TODAY)
async def today_(m: Message):
    if allowed(m.from_user.id):
        await send_day(m.bot, m.chat.id, 0)


@router.message(F.text == BTN_TOMORROW)
async def tomorrow_(m: Message):
    if allowed(m.from_user.id):
        await send_day(m.bot, m.chat.id, 1)


@router.message(F.text == BTN_DEBTS)
async def debts_(m: Message):
    if not allowed(m.from_user.id):
        return
    with SessionLocal() as s:
        ds = sv.debts(s)
        if not ds:
            return await m.answer("Долгов нет 👍")
        await m.answer(f"📝 <b>Долги: {sv.fmt_money(sum(o.total for o in ds))}</b>")
        for o in ds:
            await m.answer(sv.order_text(o, with_date=True), reply_markup=order_kb(o))


REPORT_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Сегодня", callback_data="r:today"),
    InlineKeyboardButton(text="Вчера", callback_data="r:yesterday"),
    InlineKeyboardButton(text="Неделя", callback_data="r:week"),
    InlineKeyboardButton(text="Месяц", callback_data="r:month"),
]])
REPORT_TITLES = {"today": "Сегодня", "yesterday": "Вчера", "week": "Эта неделя", "month": "Этот месяц"}


@router.message(F.text == BTN_REPORT)
async def report_(m: Message):
    if not allowed(m.from_user.id):
        return
    with SessionLocal() as s:
        r = sv.report(s, *sv.period("today"))
    await m.answer(sv.report_text(r, "Сегодня"), reply_markup=REPORT_KB)


@router.callback_query(F.data.startswith("r:"))
async def report_cb(c: CallbackQuery):
    if not allowed(c.from_user.id):
        return await c.answer()
    name = c.data[2:]
    with SessionLocal() as s:
        r = sv.report(s, *sv.period(name))
    await c.message.edit_text(sv.report_text(r, REPORT_TITLES.get(name, "")), reply_markup=REPORT_KB)
    await c.answer()


@router.callback_query(F.data.startswith("o:"))
async def order_cb(c: CallbackQuery):
    if not allowed(c.from_user.id):
        return await c.answer()
    _, oid, action = c.data.split(":")
    with SessionLocal() as s:
        o = s.scalar(sv.order_query().where(Order.id == int(oid)))
        if not o:
            return await c.answer("Заказ не найден")
        if action in ("cash", "transfer", "debt"):
            sv.set_status(o, "done", action)
            note = {"cash": "Доставлено, наличка", "transfer": "Доставлено, перевод", "debt": "Записал в долг"}[action]
        elif action == "move":
            sv.move_order(o, max(o.date, sv.today()) + timedelta(days=1))
            note = "Перенёс на " + sv.fmt_date(o.date)
        elif action == "cancel":
            sv.set_status(o, "cancelled")
            note = "Отменён"
        elif action == "reopen":
            sv.set_status(o, "new")
            note = "Снова в доставке"
        else:
            return await c.answer()
        s.commit()
        await c.message.edit_text(sv.order_text(o, with_date=action == "move"), reply_markup=order_kb(o))
    await c.answer(note)


@router.callback_query(F.data.startswith("n:"))
async def notify_cb(c: CallbackQuery):
    """Кнопки «📣 Сообщить клиенту» и «🚚 Еду» на карточке заказа."""
    if not allowed(c.from_user.id):
        return await c.answer()
    _, oid, action = c.data.split(":")
    with SessionLocal() as s:
        o = s.scalar(sv.order_query().where(Order.id == int(oid)))
        if not o:
            return await c.answer("Заказ не найден")
        kind = "on_the_way" if action == "go" else notify.status_kind(o)
        sent = await notify.notify_client(s, o, kind)
        if sent and kind == "eta":
            o.eta_notified = True
            s.commit()
    if sent:
        await c.answer("Отправлено: " + notify.channels_label(sent))
    else:
        await c.answer("Клиент не подписан — нажми WhatsApp", show_alert=True)


# ---------- голос / текст / скриншоты ----------


async def handle_parsed(m: Message, parsed: ai.Parsed, heard: str = ""):
    with SessionLocal() as s:
        text = ai.preview(s, parsed)
    prefix = f"🎤 <i>{sv.esc(heard)}</i>\n\n" if heard else ""
    if not text:
        return await m.answer(prefix + (sv.esc(parsed.reply) or "Не понял, что записать. Скажи подробнее 🙏"))
    key = uuid.uuid4().hex[:10]
    PENDING[key] = (m.from_user.id, parsed)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data=f"p:{key}:ok"),
        InlineKeyboardButton(text="❌ Не то", callback_data=f"p:{key}:no"),
    ]])
    await m.answer(prefix + text + "\n\nВсё верно?", reply_markup=kb)


@router.callback_query(F.data.startswith("p:"))
async def pending_cb(c: CallbackQuery):
    user = allowed(c.from_user.id)
    if not user:
        return await c.answer()
    _, key, action = c.data.split(":")
    item = PENDING.pop(key, None)
    if not item:
        await c.message.edit_reply_markup(reply_markup=None)
        return await c.answer("Устарело, отправь ещё раз")
    if action != "ok":
        await c.message.edit_text(c.message.html_text + "\n\n❌ Не сохранено. Скажи ещё раз точнее.")
        return await c.answer()
    with SessionLocal() as s:
        done = ai.apply(s, item[1], created_by=c.from_user.id, via_friend=user.role != "owner")
    await c.message.edit_text(c.message.html_text.replace("\n\nВсё верно?", "") + "\n\n💾 <b>Сохранено</b>\n"
                              + "\n".join(f"• {sv.esc(d)}" for d in done))
    await c.answer("Сохранено")
    if user.role != "owner":
        with SessionLocal() as s:
            for u in sv.all_users(s):
                if u.role == "owner":
                    await c.bot.send_message(u.tg_id, f"🔔 <b>Новая заявка от {sv.esc(user.name)}</b>\n"
                                             + "\n".join(f"• {sv.esc(d)}" for d in done))


@router.message(F.voice | F.audio)
async def voice(m: Message):
    if not allowed(m.from_user.id):
        return
    await m.bot.send_chat_action(m.chat.id, "typing")
    media = m.voice or m.audio
    buf = await m.bot.download(media)
    try:
        heard = await ai.transcribe(buf.read(), "voice.ogg")
        if not heard:
            return await m.answer("Ничего не расслышал, повтори 🙏")
        with SessionLocal() as s:
            parsed = await ai.parse(s, heard)
    except ai.AIError as e:
        return await m.answer(f"⚠️ {sv.esc(str(e))}")
    await handle_parsed(m, parsed, heard)


@router.message(F.photo)
async def photo(m: Message):
    if not allowed(m.from_user.id):
        return
    await m.bot.send_chat_action(m.chat.id, "typing")
    buf = await m.bot.download(m.photo[-1])
    try:
        with SessionLocal() as s:
            parsed = await ai.parse(s, m.caption or "", image=buf.read())
    except ai.AIError as e:
        return await m.answer(f"⚠️ {sv.esc(str(e))}")
    await handle_parsed(m, parsed)


@router.message(F.text)
async def text(m: Message):
    if not allowed(m.from_user.id) or m.text.startswith("/"):
        return
    await m.bot.send_chat_action(m.chat.id, "typing")
    try:
        with SessionLocal() as s:
            parsed = await ai.parse(s, m.text)
    except ai.AIError as e:
        return await m.answer(f"⚠️ {sv.esc(str(e))}")
    await handle_parsed(m, parsed)


# ---------- рассылки по расписанию ----------


async def broadcast_day(bot: Bot, offset: int):
    with SessionLocal() as s:
        ids = [u.tg_id for u in sv.all_users(s)]
    for tg_id in ids:
        try:
            await send_day(bot, tg_id, offset)
        except Exception:
            log.exception("broadcast to %s failed", tg_id)


async def notify_order(order_id: int, header: str):
    """Шлёт всем пользователям карточку нового заказа (например, пришедшего по QR)."""
    if BOT is None:
        return
    with SessionLocal() as s:
        o = s.scalar(sv.order_query().where(Order.id == order_id))
        ids = [u.tg_id for u in sv.all_users(s)]
        if not o:
            return
        text, kb = f"{header}\n\n" + sv.order_text(o, with_date=True), order_kb(o)
    for tg_id in ids:
        try:
            await BOT.send_message(tg_id, text, reply_markup=kb)
        except Exception:
            log.exception("notify %s failed", tg_id)


BOT: Bot | None = None


def make_bot() -> tuple[Bot, Dispatcher]:
    global BOT
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT = bot
    dp = Dispatcher()
    dp.include_router(client_bot.router)  # клиенты — раньше сотрудников
    dp.include_router(router)
    return bot, dp
