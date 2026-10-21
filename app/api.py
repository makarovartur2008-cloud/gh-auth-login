import datetime as dt
from datetime import date, timedelta

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as sv
from app.config import settings
from app.db import Client, Expense, Order, Product, Recurring, SessionLocal, User

router = APIRouter(prefix="/api")


def get_db():
    with SessionLocal() as s:
        yield s


def current_user(
    s: Session = Depends(get_db),
    x_init_data: str = Header(default=""),
) -> User:
    tg_id = None
    if x_init_data:
        try:
            data = safe_parse_webapp_init_data(settings.bot_token, x_init_data)
            tg_id = data.user.id if data.user else None
        except ValueError:
            pass
    elif settings.dev_user_tg_id:
        tg_id = settings.dev_user_tg_id
    user = sv.get_user(s, tg_id) if tg_id else None
    if not user:
        raise HTTPException(403, "Нет доступа. Открой приложение из Telegram-бота.")
    return user


# ---------- сериализация ----------


def product_out(p: Product) -> dict:
    return dict(id=p.id, name=p.name, unit=p.unit, price=p.price, cost=p.cost, min_qty=p.min_qty,
                emoji=p.emoji, active=p.active, sort=p.sort)


def client_out(c: Client) -> dict:
    return dict(id=c.id, name=c.name, phone=c.phone, phone_fmt=sv.fmt_phone(c.phone), address=c.address,
                entrance=c.entrance, floor=c.floor, apartment=c.apartment, notes=c.notes, label=sv.client_label(c))


def order_out(o: Order) -> dict:
    return dict(
        id=o.id, date=o.date.isoformat(), status=o.status, payment=o.payment, via_friend=o.via_friend,
        friend_fee=o.friend_fee, notes=o.notes, recurring_id=o.recurring_id, total=o.total, source=o.source,
        client=client_out(o.client),
        items=[dict(product_id=i.product_id, name=i.product.name, emoji=i.product.emoji, unit=i.product.unit,
                    qty=i.qty, price=i.price, sum=round(i.qty * i.price)) for i in o.items],
    )


def recurring_out(r: Recurring, s: Session) -> dict:
    items = []
    for it in r.items:
        p = s.get(Product, it["product_id"])
        if p:
            items.append(dict(it, name=p.name, unit=p.unit, emoji=p.emoji))
    return dict(id=r.id, client_id=r.client_id, client_name=r.client.name, every_days=r.every_days,
                next_date=r.next_date.isoformat(), active=r.active, items=items,
                total=round(sum(i["qty"] * i["price"] for i in items)))


# ---------- схемы ----------


class ItemIn(BaseModel):
    product_id: int
    qty: float
    price: float | None = None


class ClientIn(BaseModel):
    name: str = ""
    phone: str = ""
    address: str = ""
    entrance: str = ""
    floor: str = ""
    apartment: str = ""
    notes: str = ""


class OrderIn(BaseModel):
    client_id: int | None = None
    client: ClientIn | None = None
    date: dt.date
    items: list[ItemIn]
    notes: str = ""
    via_friend: bool | None = None
    every_days: int | None = None


class OrderPatch(BaseModel):
    date: dt.date | None = None
    items: list[ItemIn] | None = None
    notes: str | None = None
    via_friend: bool | None = None
    status: str | None = None
    payment: str | None = None


class RecurringIn(BaseModel):
    every_days: int | None = None
    next_date: date | None = None
    active: bool | None = None
    items: list[ItemIn] | None = None


class ExpenseIn(BaseModel):
    date: dt.date
    amount: int
    category: str = "other"
    note: str = ""


class ProductIn(BaseModel):
    name: str
    unit: str = "кг"
    price: float = 0
    cost: float = 0
    min_qty: float = 0
    emoji: str = ""
    active: bool = True


# ---------- эндпоинты ----------


@router.get("/me")
def me(u: User = Depends(current_user)):
    return dict(name=u.name, role=u.role, today=sv.today().isoformat(), sack_kg=settings.sack_kg,
                friend_fee=settings.friend_fee)


@router.get("/products")
def list_products(s: Session = Depends(get_db), u: User = Depends(current_user)):
    return [product_out(p) for p in sv.products(s, only_active=False)]


@router.post("/products")
def add_product(body: ProductIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    p = Product(**body.model_dump(), sort=len(sv.products(s, only_active=False)))
    s.add(p)
    s.commit()
    return product_out(p)


@router.put("/products/{pid}")
def edit_product(pid: int, body: ProductIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    p = s.get(Product, pid) or _404()
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    s.commit()
    return product_out(p)


@router.get("/clients")
def list_clients(q: str = "", s: Session = Depends(get_db), u: User = Depends(current_user)):
    regular = {r.client_id for r in s.scalars(select(Recurring).where(Recurring.active))}
    out = []
    for c in sv.search_clients(s, q):
        d = client_out(c)
        d["regular"] = c.id in regular
        out.append(d)
    return out


@router.post("/clients")
def add_client(body: ClientIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    if not body.name.strip():
        raise HTTPException(400, "Укажи имя")
    c, _ = sv.upsert_client(s, body.model_dump())
    s.commit()
    return client_out(c)


@router.get("/clients/{cid}")
def get_client(cid: int, s: Session = Depends(get_db), u: User = Depends(current_user)):
    c = s.get(Client, cid) or _404()
    orders = s.scalars(sv.order_query().where(Order.client_id == cid).order_by(Order.date.desc())).all()
    done = [o for o in orders if o.status == "done"]
    return dict(
        client_out(c),
        orders=[order_out(o) for o in orders],
        recurring=[recurring_out(r, s) for r in c.recurring],
        stats=dict(orders=len(done), revenue=sum(o.total for o in done),
                   debt=sum(o.total for o in done if o.payment == "debt")),
    )


@router.put("/clients/{cid}")
def edit_client(cid: int, body: ClientIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    c = s.get(Client, cid) or _404()
    for k, v in body.model_dump().items():
        setattr(c, k, v.strip())
    s.commit()
    return client_out(c)


@router.get("/orders")
def list_orders(day: date, s: Session = Depends(get_db), u: User = Depends(current_user)):
    sv.generate_recurring(s)
    orders = sv.orders_on(s, day, include_cancelled=True)
    have = {o.recurring_id for o in orders if o.recurring_id}
    planned = [recurring_out(r, s) for r in sv.planned_on(s, day) if r.id not in have] if day > sv.today() else []
    overdue = [order_out(o) for o in sv.open_orders_before(s, day)] if day == sv.today() else []
    return dict(orders=[order_out(o) for o in orders], planned=planned, overdue=overdue)


@router.get("/orders/counts")
def order_counts(start: date, days: int = 14, s: Session = Depends(get_db), u: User = Depends(current_user)):
    end = start + timedelta(days=days - 1)
    out = {}
    for o in s.scalars(select(Order).where(Order.date >= start, Order.date <= end, Order.status == "new")):
        out[o.date.isoformat()] = out.get(o.date.isoformat(), 0) + 1
    t = sv.today()
    # плановые заказы постоянных клиентов на будущие дни
    for i in range(days):
        d = start + timedelta(days=i)
        if d > t + timedelta(days=1):
            n = len(sv.planned_on(s, d))
            if n:
                out[d.isoformat()] = out.get(d.isoformat(), 0) + n
    return out


@router.get("/orders/debts")
def list_debts(s: Session = Depends(get_db), u: User = Depends(current_user)):
    return [order_out(o) for o in sv.debts(s)]


@router.post("/orders")
def add_order(body: OrderIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    if body.client_id:
        client = s.get(Client, body.client_id) or _404()
    elif body.client and body.client.name.strip():
        client, _ = sv.upsert_client(s, body.client.model_dump())
    else:
        raise HTTPException(400, "Выбери клиента или укажи имя нового")
    if not body.items:
        raise HTTPException(400, "Добавь хотя бы один товар")
    via_friend = body.via_friend if body.via_friend is not None else u.role != "owner"
    o = sv.create_order(s, client, body.date, [i.model_dump() for i in body.items], notes=body.notes,
                        via_friend=via_friend, created_by=u.tg_id)
    if body.every_days:
        sv.make_recurring(s, o, body.every_days)
    s.commit()
    return order_out(o)


@router.patch("/orders/{oid}")
def edit_order(oid: int, body: OrderPatch, s: Session = Depends(get_db), u: User = Depends(current_user)):
    o = s.scalar(sv.order_query().where(Order.id == oid)) or _404()
    if body.date:
        o.date = body.date
    if body.items is not None:
        sv.set_items(s, o, [i.model_dump() for i in body.items])
    if body.notes is not None:
        o.notes = body.notes
    if body.via_friend is not None:
        sv.set_via_friend(o, body.via_friend)
    if body.status:
        if body.status not in ("new", "done", "cancelled"):
            raise HTTPException(400, "Неизвестный статус")
        sv.set_status(o, body.status, body.payment)
    elif body.payment and o.status == "done":
        o.payment = body.payment
    s.commit()
    return order_out(o)


@router.delete("/orders/{oid}")
def delete_order(oid: int, s: Session = Depends(get_db), u: User = Depends(current_user)):
    o = s.get(Order, oid) or _404()
    s.delete(o)
    s.commit()
    return {"ok": True}


@router.put("/recurring/{rid}")
def edit_recurring(rid: int, body: RecurringIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    r = s.get(Recurring, rid) or _404()
    if body.every_days:
        r.every_days = max(1, body.every_days)
    if body.next_date:
        r.next_date = body.next_date
    if body.active is not None:
        r.active = body.active
    if body.items is not None:
        r.items = [dict(product_id=i.product_id, qty=i.qty,
                        price=i.price if i.price is not None else s.get(Product, i.product_id).price)
                   for i in body.items if s.get(Product, i.product_id)]
    s.commit()
    return recurring_out(r, s)


@router.post("/recurring/from-order/{oid}")
def recurring_from_order(oid: int, every_days: int = 7, s: Session = Depends(get_db),
                         u: User = Depends(current_user)):
    o = s.scalar(sv.order_query().where(Order.id == oid)) or _404()
    r = sv.make_recurring(s, o, max(1, every_days))
    s.commit()
    return recurring_out(r, s)


@router.get("/report")
def get_report(start: date, end: date, s: Session = Depends(get_db), u: User = Depends(current_user)):
    return sv.report(s, start, end).as_dict()


@router.get("/sources")
def get_sources(start: date, end: date, s: Session = Depends(get_db), u: User = Depends(current_user)):
    return sv.sources_stats(s, start, end)


@router.get("/expenses")
def list_expenses(start: date, end: date, s: Session = Depends(get_db), u: User = Depends(current_user)):
    q = select(Expense).where(Expense.date >= start, Expense.date <= end).order_by(Expense.date.desc(), Expense.id.desc())
    return [dict(id=e.id, date=e.date.isoformat(), amount=e.amount, category=e.category,
                 category_name=sv.EXPENSE_CATEGORIES.get(e.category, ""), note=e.note) for e in s.scalars(q)]


@router.post("/expenses")
def add_expense(body: ExpenseIn, s: Session = Depends(get_db), u: User = Depends(current_user)):
    if body.amount <= 0:
        raise HTTPException(400, "Сумма должна быть больше нуля")
    e = sv.add_expense(s, body.amount, body.category, body.note, body.date, u.tg_id)
    s.commit()
    return {"id": e.id}


@router.delete("/expenses/{eid}")
def delete_expense(eid: int, s: Session = Depends(get_db), u: User = Depends(current_user)):
    e = s.get(Expense, eid) or _404()
    s.delete(e)
    s.commit()
    return {"ok": True}


def _404():
    raise HTTPException(404, "Не найдено")
