"""Публичная часть: страница заказа по QR-коду и листовка для печати. Вход без Telegram."""

import time
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

import segno
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import notify, photos
from app import services as sv
from app.api import get_db
from app.config import settings
from app.db import Order, Product, SessionLocal

router = APIRouter()
STATIC = Path(__file__).parent / "static"

# Защита от спама: не больше LIMIT заявок с одного адреса за WINDOW секунд
LIMIT, WINDOW = 5, 3600
_hits: dict[str, list[float]] = {}
DAYS_AHEAD = 14


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "")


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < WINDOW]
    _hits[ip] = hits
    if len(hits) >= LIMIT:
        return True
    hits.append(now)
    return False


def public_products(s: Session) -> list[Product]:
    return [p for p in sv.products(s) if p.price > 0]


def base_url(request: Request) -> str:
    return (settings.webapp_url or str(request.base_url)).rstrip("/")


# ---------- страница заказа ----------


@router.get("/order")
def order_page():
    return FileResponse(STATIC / "order.html", headers={"Cache-Control": "no-cache"})


@router.get("/api/public/info")
def info(s: Session = Depends(get_db)):
    t = sv.today()
    return dict(
        phone=sv.fmt_phone(settings.contact_phone) if settings.contact_phone else "",
        sack_kg=settings.sack_kg,
        days=[dict(date=(t + timedelta(days=i)).isoformat(), label=sv.fmt_date(t + timedelta(days=i)))
              for i in range(DAYS_AHEAD)],
        products=[dict(id=p.id, name=p.name, unit=p.unit, price=p.price, min_qty=p.min_qty, emoji=p.emoji,
                       potato=sv.is_potato(p), photo=photos.url(p.photo))
                  for p in public_products(s)],
    )


class PublicItem(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=2000)


class PublicOrder(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(max_length=30)
    address: str = Field(min_length=3, max_length=200)
    entrance: str = Field(default="", max_length=10)
    floor: str = Field(default="", max_length=10)
    apartment: str = Field(default="", max_length=10)
    comment: str = Field(default="", max_length=500)
    date: str
    items: list[PublicItem] = Field(min_length=1, max_length=20)
    source: str = Field(default="", max_length=60)
    website: str = ""  # ловушка для ботов: люди это поле не видят


@router.post("/api/public/order")
async def create_public_order(body: PublicOrder, request: Request, background: BackgroundTasks,
                              s: Session = Depends(get_db)):
    if body.website:  # заполнил скрытое поле — бот; делаем вид, что всё ок
        return {"ok": True, "id": 0}
    if len(sv.normalize_phone(body.phone)) != 11:
        raise HTTPException(400, "Проверьте номер телефона")
    try:
        day = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "Выберите день доставки")
    t = sv.today()
    if not t <= day < t + timedelta(days=DAYS_AHEAD):
        raise HTTPException(400, "Выберите день доставки из списка")
    allowed = {p.id: p for p in public_products(s)}
    items = []
    for it in body.items:
        p = allowed.get(it.product_id)
        if not p:
            raise HTTPException(400, "Товар закончился, обновите страницу")
        if p.min_qty and it.qty < p.min_qty:
            raise HTTPException(400, f"{p.name} — от {sv.fmt_qty(p.min_qty)} {p.unit}")
        items.append({"product_id": p.id, "qty": it.qty, "price": p.price})
    if not any(sv.is_potato(allowed[i["product_id"]]) for i in items):
        raise HTTPException(400, f"Возим только вместе с картошкой — добавьте {sv.potato_min_text(list(allowed.values()))}")
    if rate_limited(client_ip(request)):
        raise HTTPException(429, "Слишком много заявок. Позвоните нам или попробуйте позже.")

    client, created = sv.upsert_client(
        s, dict(name=body.name, phone=body.phone, address=body.address, entrance=body.entrance,
                floor=body.floor, apartment=body.apartment), only_empty=True)
    notes = ["🌐 заявка с сайта"]
    addr_given = ", ".join(x for x in [body.address, body.entrance and f"под. {body.entrance}",
                                       body.floor and f"эт. {body.floor}",
                                       body.apartment and f"кв. {body.apartment}"] if x)
    if not created and addr_given != sv.client_label(client):
        notes.append(f"адрес в заявке: {addr_given}")
    if body.comment.strip():
        notes.append(body.comment.strip())
    order = sv.create_order(s, client, day, items, notes="\n".join(notes),
                            source=body.source.strip() or "сайт")
    s.commit()

    header = "🔔 <b>Новая заявка с сайта</b>" + ("" if created else " (клиент уже есть в базе)")
    # уведомления — после ответа клиенту, чтобы страница не ждала Telegram и SMS
    background.add_task(notify_new_web_order, order.id, header)
    return {"ok": True, "id": order.id, "total": order.total, "token": order.token,
            "tg_link": notify.tg_link(order), "max_link": notify.max_link(order)}


async def notify_new_web_order(order_id: int, header: str):
    from app.bot import notify_order
    await notify_order(order_id, header)
    # обычно дойдёт только SMS: в Telegram/MAX клиент подпишется через секунду, и статус придёт тогда
    with SessionLocal() as s:
        order = s.scalar(sv.order_query().where(Order.id == order_id))
        if order:
            await notify.notify_client(s, order, "accepted")


# ---------- перенос доставки (/r/<token>) ----------


@router.get("/r/{token}")
def reschedule_page(token: str):
    return FileResponse(STATIC / "reschedule.html", headers={"Cache-Control": "no-cache"})


def _resched_order(s: Session, token: str) -> Order:
    o = notify.find_by_token(s, token)
    if not o:
        raise HTTPException(404, "Ссылка устарела или с ошибкой")
    return o


@router.get("/api/public/resched/{token}")
def reschedule_info(token: str, s: Session = Depends(get_db)):
    from app import reschedule

    o = _resched_order(s, token)
    r = reschedule.pending(s, o)
    phone = sv.fmt_phone(settings.contact_phone) if settings.contact_phone else ""
    base = dict(order_id=o.id, status=o.status, phone=phone, total=o.total,
                current=sv.day_slot_label(o.date, o.slot), eta=sv.eta_text(o),
                bonus=sv.bonus_label(o.bonus),
                items=[f"{i.product.name} — {sv.fmt_qty(i.qty)} {i.product.unit}"
                       + (" (подарок)" if i.price == 0 and o.bonus else "") for i in o.items])
    if o.status != "new":
        return dict(base, mode="closed", options=[], bonus_choices=[])
    if r:
        return dict(base, mode="offer", apology=r.old_eta, options=r.options,
                    bonus_choices=[dict(key=k, label=sv.bonus_label(k), emoji=sv.BONUS_EMOJI[k])
                                   for k in r.bonus_choices])
    return dict(base, mode="self", options=reschedule.self_options(o), bonus_choices=[])


class ReschedChoice(BaseModel):
    date: str
    slot: str = ""
    bonus: str = ""


@router.post("/api/public/resched/{token}")
async def reschedule_choose(token: str, body: ReschedChoice, background: BackgroundTasks,
                            s: Session = Depends(get_db)):
    from app import reschedule

    o = _resched_order(s, token)
    if o.status != "new":
        raise HTTPException(400, "Заказ уже доставлен или отменён")
    r = reschedule.pending(s, o)
    options = r.options if r else reschedule.self_options(o)
    idx = next((i for i, x in enumerate(options) if x["date"] == body.date and x["slot"] == body.slot), None)
    if idx is None:
        raise HTTPException(400, "Этот вариант уже недоступен — обновите страницу")
    old = sv.day_slot_label(o.date, o.slot)
    if r:
        label = reschedule.answer(s, r, idx, body.bonus)
        header = f"✅ <b>{sv.esc(o.client.name)}</b> выбрал(а) время после переноса: {sv.esc(label)}"
    else:
        reschedule.client_move(s, o, date.fromisoformat(body.date), body.slot)
        label = options[idx]["label"]
        header = (f"🔄 <b>Клиент сам перенёс доставку</b>\n{sv.esc(o.client.name)}: "
                  f"было {sv.esc(old.lower())} → стало <b>{sv.esc(label.lower())}</b>")
    if o.bonus:
        header += f"\n🎁 Подарок: {sv.bonus_label(o.bonus)}"
    s.commit()
    text = reschedule.chosen_text(o, label)
    background.add_task(_after_choice, o.id, header, text)
    return {"ok": True, "label": label, "text": text}


async def _after_choice(order_id: int, header: str, client_text: str):
    from app import reschedule
    from app.bot import notify_order

    await notify_order(order_id, header)
    with SessionLocal() as s:
        o = s.scalar(sv.order_query().where(Order.id == order_id))
        if o:
            await reschedule.tell_client(o, client_text)


# ---------- QR и листовка ----------


def order_link(request: Request, source: str) -> str:
    url = base_url(request) + "/order"
    return url + (f"?s={quote(source)}" if source else "")


@router.get("/qr.svg")
def qr_svg(request: Request, s: str = ""):
    svg = segno.make(order_link(request, s), error="m").svg_inline(scale=10, border=2)
    return HTMLResponse(svg, media_type="image/svg+xml")


@router.get("/photos/{name}")
def photo(name: str):
    p = photos.path(name)
    if not p:
        raise HTTPException(404)
    # имя файла меняется при каждой замене фото — можно кэшировать надолго
    return FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/flyer", response_class=HTMLResponse)
def flyer(request: Request, s: str = "", db: Session = Depends(get_db)):
    """Лист A4 с двумя листовками A5 (разрезать пополам). Печать: «Поделиться → Печать» или «Сохранить как PDF»."""
    link = order_link(request, s)
    qr = segno.make(link, error="m").svg_inline(scale=10, border=0)
    prices = []
    items = public_products(db)
    for p in items:
        unit = "кг" if p.unit == "кг" else "банка"
        price = f"{sv.fmt_qty(p.price)} ₽/{unit}"
        note = f" <small>от {sv.fmt_qty(p.min_qty)} кг</small>" if p.min_qty else ""
        if sv.is_potato(p):  # картошку продаём сетками — цена за сетку крупно
            note = f' <small class="line">сетка {settings.sack_kg} кг · {price}</small>'
            price = f"{sv.fmt_money(p.price * settings.sack_kg)}/сетка"
        prices.append(f"<li><span>{escape(p.emoji)} {escape(p.name)}{note}</span><b>{price}</b></li>")
    phone = sv.fmt_phone(settings.contact_phone) if settings.contact_phone else ""
    one = f"""
<section class="a5">
  <div class="left">
    <h1>🥔 Картошка<br>с доставкой на дом</h1>
    <p class="sub">По городу и пригороду · каждый день, круглый год<br>Картошка {sv.potato_min_text(items)} — лук, морковь, свёклу и мёд добавим к ней</p>
    <ul class="prices">{''.join(prices)}</ul>
    <p class="pay">Оплата при получении: наличные или перевод</p>
  </div>
  <div class="right">
    {f'<p class="phone">📞 Звоните<b>{escape(phone)}</b></p>' if phone else ''}
    <div class="qr">{qr}</div>
    <p class="scan">{'или наведите' if phone else 'Наведите'} камеру<br>на код и закажите онлайн</p>
  </div>
</section>"""
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Листовка с QR</title>
<style>
@page {{ size: A4 portrait; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #1d1d1b; background: #e9e6df; }}
.tip {{ max-width: 210mm; margin: 12px auto; padding: 10px 14px; background: #fff7d6; border-radius: 10px; font-size: 14px; }}
.tip button {{ font: inherit; padding: 8px 14px; border: 0; border-radius: 8px; background: #2e7d32; color: #fff; margin-top: 6px; }}
.sheet {{ width: 210mm; height: 297mm; margin: 0 auto 20px; background: #fff; }}
.a5 {{ height: 148.5mm; padding: 11mm 12mm; display: flex; gap: 9mm; border-bottom: 1px dashed #bbb; }}
.a5:last-child {{ border-bottom: 0; }}
.left {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
h1 {{ font-size: 27pt; line-height: 1.05; margin: 0 0 3mm; color: #2e5e1e; }}
.sub {{ margin: 0 0 4mm; font-size: 11pt; color: #555; }}
.prices {{ list-style: none; margin: 0; padding: 0; font-size: 12.5pt; }}
.prices li {{ display: flex; justify-content: space-between; gap: 4mm; padding: 1.4mm 0; border-bottom: 1px dotted #ccc; }}
.prices small {{ color: #777; font-size: 9pt; }}
.prices small.line {{ display: block; }}
.prices b {{ white-space: nowrap; }}
.pay {{ margin-top: auto; font-size: 10.5pt; font-weight: 600; color: #2e5e1e; }}
.right {{ width: 68mm; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;
  background: #f4f8ef; border-radius: 6mm; padding: 6mm; }}
.qr svg {{ width: 54mm; height: 54mm; display: block; }}
.scan {{ font-size: 11.5pt; font-weight: 700; margin: 4mm 0 0; }}
.phone {{ font-size: 11pt; margin: 0 0 4mm; }}
.phone b {{ display: block; font-size: 17pt; white-space: nowrap; }}
@media print {{ body {{ background: #fff; }} .tip {{ display: none; }} .sheet {{ margin: 0; }} }}
</style></head><body>
<div class="tip">На листе A4 две листовки A5 — после печати разрежьте по пунктиру.
Метка QR: <b>{escape(s or 'без метки')}</b>. Ссылка: {escape(link)}<br>
<button onclick="window.print()">🖨 Печать / сохранить PDF</button></div>
<div class="sheet">{one}{one}</div>
</body></html>"""
    return HTMLResponse(page)
