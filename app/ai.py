"""Голос/текст/скриншот → структурированные действия (заказ, расход, оплата)."""

import asyncio
import base64
import io
import json
import logging
import datetime as dt
from datetime import date, timedelta

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app import pick_models
from app import services as sv
from app.config import settings
from app.db import Client, Order, Product

log = logging.getLogger(__name__)


class AIError(Exception):
    pass


def _client() -> AsyncOpenAI:
    if not settings.ai_api_key:
        raise AIError("ИИ не настроен (нет AI_API_KEY). Заказы можно добавлять в приложении.")
    return AsyncOpenAI(api_key=settings.ai_api_key, base_url=settings.ai_base_url or None)


async def choose_models() -> None:
    """Модели, не заданные явно (AI_MODEL / AI_STT_MODEL), выбираем из каталога сервиса.

    Нужно там, где нет deploy.sh (Amvera): названия моделей в каталогах меняются.
    """
    auto = [f for f in ("ai_model", "ai_stt_model") if f not in settings.model_fields_set]
    if not settings.ai_api_key or not auto:
        return
    try:
        ids = await asyncio.to_thread(pick_models.fetch_ids, settings.ai_base_url, settings.ai_api_key)
    except Exception as e:  # нет сети, неверный ключ — остаются значения по умолчанию
        log.warning("Не удалось получить список моделей ИИ: %s", e)
        return
    for field, pick in (("ai_model", pick_models.pick_chat), ("ai_stt_model", pick_models.pick_stt)):
        if field in auto and (model := pick(ids)):
            setattr(settings, field, model)
    log.info("Модели ИИ: текст=%s, голос=%s", settings.ai_model, settings.ai_stt_model)


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    f = io.BytesIO(audio)
    f.name = filename
    try:
        res = await _client().audio.transcriptions.create(model=settings.ai_stt_model, file=f, language="ru")
    except AIError:
        raise
    except Exception as e:  # сеть, ключ, лимиты
        log.exception("transcribe failed")
        raise AIError(f"Не получилось распознать голос: {e}") from e
    return (res.text or "").strip()


# ---------- схема ответа ИИ ----------


class ClientIn(BaseModel):
    id: int | None = None
    name: str = ""
    phone: str = ""
    address: str = ""
    entrance: str = ""
    floor: str = ""
    apartment: str = ""
    notes: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _none_to_empty(cls, v, info):
        if info.field_name == "id":
            return v
        return "" if v is None else str(v)


class ItemIn(BaseModel):
    product_id: int
    qty: float
    price: float | None = None


class Action(BaseModel):
    type: str  # order | expense | payment
    # order
    client: ClientIn | None = None
    date: dt.date | None = None
    items: list[ItemIn] = Field(default_factory=list)
    status: str = "new"
    payment: str | None = None
    every_days: int | None = None
    notes: str = ""
    # expense
    category: str = "other"
    amount: float | None = None
    note: str = ""
    # payment
    client_id: int | None = None


class Parsed(BaseModel):
    actions: list[Action] = Field(default_factory=list)
    reply: str = ""


def build_prompt(s: Session) -> str:
    t = sv.today()
    days = "\n".join(
        f"  {(t + timedelta(days=i)).isoformat()} — {sv.WEEKDAYS[(t + timedelta(days=i)).weekday()]}"
        + (" (сегодня)" if i == 0 else " (завтра)" if i == 1 else "")
        for i in range(8)
    )
    prods = "\n".join(
        f"  id={p.id}: {p.name}, цена {sv.fmt_qty(p.price)} ₽/{p.unit}" + (" (цена не задана)" if not p.price else "")
        for p in sv.products(s)
    )
    clients = "\n".join(
        f"  id={c.id}: {c.name}; {sv.fmt_phone(c.phone)}; {sv.client_label(c)}"
        for c in sv.search_clients(s)[-300:]
    ) or "  (пока пусто)"
    return f"""Ты помощник продавца овощей (картошка, лук, морковь, свёкла, мёд) с доставкой по городу.
Тебе присылают расшифровку голосового сообщения, текст или скриншот переписки с Авито.
Преобразуй это в JSON-действия. Отвечай ТОЛЬКО JSON вида {{"actions": [...], "reply": "..."}}.

Даты:
{days}
Если дата заказа не названа — сегодня. «В пятницу» — ближайшая пятница.

Товары (product_id):
{prods}
Правила: «мешок»/«сетка» картошки = {settings.sack_kg} кг. Если цена за единицу не названа — price: null (возьмётся из прайса).
Если назвали общую сумму за товар («10 кг лука за 500») — price = сумма / количество.
Лук без уточнения цвета — обычный «Лук». Морковь без «мытая» — обычная «Морковь».
Мёд: «литровая банка» — 1 л, «трёхлитровая» — 3 л, qty = число банок.

Клиенты в базе:
{clients}
Если клиент из базы — укажи client.id. Иначе client.id = null и заполни что известно.

Типы действий:
1) Заказ: {{"type":"order","client":{{"id":null,"name":"","phone":"","address":"","entrance":"","floor":"","apartment":"","notes":""}},
   "date":"YYYY-MM-DD","items":[{{"product_id":1,"qty":50,"price":null}}],
   "status":"new" или "done","payment":null|"cash"|"transfer"|"debt","every_days":null|7|14,"notes":""}}
   status "done" — если продавец говорит, что УЖЕ отвёз/продал. payment: наличка=cash, перевод/на карту=transfer, в долг=debt.
   every_days — если клиент постоянный («каждую неделю» = 7, «раз в две недели» = 14).
2) Расход: {{"type":"expense","category":"fuel"|"purchase"|"other","amount":3000,"note":"","date":"YYYY-MM-DD"}}
   fuel — газ/бензин/заправка, purchase — закупка товара, other — остальное.
3) Оплата существующего заказа/долга: {{"type":"payment","client_id":5,"payment":"cash"|"transfer"}}
   (только если клиент из базы и речь об оплате ранее записанного заказа без новых товаров).

Если сообщение не про заказы/деньги или непонятно — actions пустой, а в reply коротко спроси, что уточнить.
Не выдумывай данные, которых нет в сообщении."""


async def parse(s: Session, text: str = "", image: bytes | None = None) -> Parsed:
    content: list | str
    if image:
        b64 = base64.b64encode(image).decode()
        content = [
            {"type": "text", "text": text or "Скриншот переписки с клиентом. Создай заявку."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        content = text
    try:
        res = await _client().chat.completions.create(
            model=settings.ai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": build_prompt(s)}, {"role": "user", "content": content}],
        )
        raw = res.choices[0].message.content or "{}"
    except AIError:
        raise
    except Exception as e:
        log.exception("parse failed")
        raise AIError(f"ИИ не ответил: {e}") from e
    return parse_json(raw)


def parse_json(raw: str) -> Parsed:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Parsed(reply="Не понял сообщение, попробуй ещё раз.")
    actions = []
    for a in data.get("actions") or []:
        try:
            actions.append(Action.model_validate(a))
        except ValidationError:
            log.warning("bad action skipped: %s", a)
    return Parsed(actions=actions, reply=str(data.get("reply") or ""))


# ---------- превью и применение ----------


def preview(s: Session, p: Parsed) -> str:
    out = []
    for a in p.actions:
        if a.type == "order" and a.client:
            known = s.get(Client, a.client.id) if a.client.id else sv.find_client(s, a.client.phone)
            who = f"<b>{sv.esc(known.name)}</b> (из базы)" if known else f"<b>{sv.esc(a.client.name or 'Без имени')}</b> 🆕 новый клиент"
            lines = [("✅ Продажа" if a.status == "done" else "🛒 Заказ") + f" — {sv.fmt_date(a.date or sv.today())}", who]
            c = a.client
            if c.phone:
                lines.append(f"📞 {sv.fmt_phone(c.phone)}")
            addr = ", ".join(x for x in [c.address, c.entrance and f"под. {c.entrance}",
                                         c.floor and f"эт. {c.floor}", c.apartment and f"кв. {c.apartment}"] if x)
            if addr:
                lines.append(f"📍 {sv.esc(addr)}")
            total = 0.0
            for it in a.items:
                prod = s.get(Product, it.product_id)
                if not prod:
                    continue
                price = it.price if it.price is not None else prod.price
                total += it.qty * price
                warn = " ⚠️ цена не задана" if not price else ""
                if prod.min_qty and it.qty < prod.min_qty:
                    warn += f" ⚠️ меньше минимума ({sv.fmt_qty(prod.min_qty)} {prod.unit})"
                lines.append(f"{prod.emoji or '•'} {sv.esc(prod.name)} — {sv.fmt_qty(it.qty)} {prod.unit} × "
                             f"{sv.fmt_qty(price)} = {sv.fmt_money(it.qty * price)}{warn}")
            if not a.items:
                lines.append("⚠️ товары не указаны")
            elif not any((p := s.get(Product, it.product_id)) and sv.is_potato(p) for it in a.items):
                lines.append("⚠️ без картошки — такой заказ не возим")
            lines.append(f"💰 {sv.fmt_money(total)}")
            if a.status == "done":
                lines.append(f"оплата: {sv.PAYMENTS.get(a.payment or 'cash')}")
            if a.every_days:
                lines.append(f"🔁 постоянный: каждые {a.every_days} дн.")
            if a.notes:
                lines.append(f"📝 {sv.esc(a.notes)}")
            out.append("\n".join(lines))
        elif a.type == "expense" and a.amount:
            out.append(f"💸 Расход — {sv.EXPENSE_CATEGORIES.get(a.category, 'прочее')}: "
                       f"<b>{sv.fmt_money(a.amount)}</b>" + (f"\n📝 {sv.esc(a.note)}" if a.note else ""))
        elif a.type == "payment" and a.client_id:
            c = s.get(Client, a.client_id)
            if c:
                out.append(f"💵 Оплата от <b>{sv.esc(c.name)}</b> ({sv.PAYMENTS.get(a.payment or 'cash')})")
    return "\n\n".join(out)


def apply(s: Session, p: Parsed, created_by: int | None, via_friend: bool) -> list[str]:
    """Сохраняет действия в базу. Возвращает короткие строки-итоги."""
    done = []
    for a in p.actions:
        if a.type == "order" and a.client:
            client, created = sv.upsert_client(s, a.client.model_dump(exclude={"id"}), a.client.id)
            order = sv.create_order(
                s, client, a.date or sv.today(), [i.model_dump() for i in a.items],
                notes=a.notes, via_friend=via_friend, status=a.status,
                payment=a.payment, created_by=created_by,
            )
            if a.every_days:
                sv.make_recurring(s, order, a.every_days)
            done.append(f"{'🆕 ' if created else ''}{client.name}: заказ №{order.id} на {sv.fmt_date(order.date)}, "
                        f"{sv.fmt_money(order.total)}")
        elif a.type == "expense" and a.amount:
            sv.add_expense(s, int(a.amount), a.category, a.note, a.date, created_by)
            done.append(f"Расход {sv.fmt_money(a.amount)} записан")
        elif a.type == "payment" and a.client_id:
            c = s.get(Client, a.client_id)
            if c:
                closed: list[Order] = sv.pay_client(s, c, a.payment or "cash")
                done.append(f"{c.name}: оплачено заказов — {len(closed)}" if closed
                            else f"{c.name}: нет открытых заказов или долгов")
    s.commit()
    return done
