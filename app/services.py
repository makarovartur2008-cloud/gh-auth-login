import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import Client, Expense, Order, OrderItem, Product, Recurring, User

PAYMENTS = {"cash": "наличка", "transfer": "перевод", "debt": "в долг"}
EXPENSE_CATEGORIES = {"fuel": "газ/бензин", "purchase": "закупка товара", "other": "прочее"}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def today() -> date:
    return datetime.now(settings.zone).date()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def fmt_phone(phone: str) -> str:
    d = normalize_phone(phone)
    if len(d) == 11:
        return f"+{d[0]} {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"
    return phone or ""


def fmt_qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def fmt_money(v: float) -> str:
    return f"{round(v):,}".replace(",", " ") + " ₽"


def fmt_date(d: date) -> str:
    t = today()
    label = {t: "сегодня", t + timedelta(days=1): "завтра", t - timedelta(days=1): "вчера"}.get(d)
    base = f"{d.strftime('%d.%m')} {WEEKDAYS[d.weekday()]}"
    return f"{label}, {base}" if label else base


# ---------- пользователи ----------


def get_user(s: Session, tg_id: int) -> User | None:
    return s.scalar(select(User).where(User.tg_id == tg_id))


def all_users(s: Session) -> list[User]:
    return list(s.scalars(select(User)))


# ---------- товары ----------


def is_potato(p: Product) -> bool:
    """Без картошки не возим: остальные товары — только в дополнение к ней."""
    return "картош" in p.name.lower()


def products(s: Session, only_active: bool = True) -> list[Product]:
    q = select(Product).order_by(Product.sort, Product.id)
    if only_active:
        q = q.where(Product.active)
    return list(s.scalars(q))


# ---------- клиенты ----------

CLIENT_FIELDS = ("name", "phone", "address", "entrance", "floor", "apartment", "notes")


def find_client(s: Session, phone: str = "", name: str = "") -> Client | None:
    p = normalize_phone(phone)
    if len(p) >= 10:
        for c in s.scalars(select(Client).where(Client.phone != "")):
            if normalize_phone(c.phone) == p:
                return c
    return None


def upsert_client(s: Session, data: dict, client_id: int | None = None,
                  only_empty: bool = False) -> tuple[Client, bool]:
    """Находит клиента по id или телефону, дополняет данные; иначе создаёт. Возвращает (клиент, создан_ли).
    only_empty — для заявок с сайта: у существующего клиента заполняем только пустые поля."""
    data = {k: str(v).strip() for k, v in data.items() if k in CLIENT_FIELDS and v not in (None, "")}
    client = s.get(Client, client_id) if client_id else None
    if client is None:
        client = find_client(s, data.get("phone", ""))
    created = client is None
    if created:
        client = Client(name=data.get("name") or "Без имени")
        s.add(client)
    for k, v in data.items():
        if not created and (k == "name" or (only_empty and getattr(client, k))):
            continue  # имя (и при only_empty — заполненные поля) существующего клиента не перетираем
        setattr(client, k, v)
    s.flush()
    return client, created


def search_clients(s: Session, q: str = "") -> list[Client]:
    clients = list(s.scalars(select(Client).order_by(Client.name)))
    q = q.strip().lower()
    if not q:
        return clients
    qd = re.sub(r"\D", "", q)
    return [
        c
        for c in clients
        if q in c.name.lower() or q in c.address.lower() or (len(qd) >= 3 and qd in normalize_phone(c.phone))
    ]


def client_label(c: Client) -> str:
    parts = [c.address]
    if c.entrance:
        parts.append(f"под. {c.entrance}")
    if c.floor:
        parts.append(f"эт. {c.floor}")
    if c.apartment:
        parts.append(f"кв. {c.apartment}")
    return ", ".join(p for p in parts if p)


# ---------- заказы ----------


def order_query():
    return select(Order).options(
        selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.client)
    )


def set_items(s: Session, order: Order, items: list[dict]):
    order.items.clear()
    for it in items:
        product = s.get(Product, int(it["product_id"]))
        if not product:
            continue
        qty = float(it.get("qty") or 0)
        if qty <= 0:
            continue
        price = it.get("price")
        price = float(price) if price not in (None, "") else product.price
        order.items.append(OrderItem(product=product, qty=qty, price=price, cost=product.cost))


def create_order(
    s: Session,
    client: Client,
    day: date,
    items: list[dict],
    *,
    notes: str = "",
    via_friend: bool = False,
    status: str = "new",
    payment: str | None = None,
    created_by: int | None = None,
    recurring_id: int | None = None,
    source: str = "",
) -> Order:
    order = Order(
        client=client,
        date=day,
        notes=notes or "",
        via_friend=via_friend,
        friend_fee=settings.friend_fee if via_friend else 0,
        created_by=created_by,
        recurring_id=recurring_id,
        source=source,
    )
    s.add(order)
    set_items(s, order, items)
    if status == "done":
        set_status(order, "done", payment or "cash")
    s.flush()
    return order


def set_via_friend(order: Order, via_friend: bool):
    order.via_friend = via_friend
    order.friend_fee = settings.friend_fee if via_friend else 0


def set_status(order: Order, status: str, payment: str | None = None):
    order.status = status
    if status == "done":
        order.payment = payment or order.payment or "cash"
        order.done_at = datetime.now()
    elif status == "new":
        order.payment = None
        order.done_at = None
    elif status == "cancelled":
        order.payment = None


def orders_on(s: Session, day: date, include_cancelled: bool = False) -> list[Order]:
    q = order_query().where(Order.date == day).order_by(Order.id)
    if not include_cancelled:
        q = q.where(Order.status != "cancelled")
    return list(s.scalars(q))


def open_orders_before(s: Session, day: date) -> list[Order]:
    """Недоставленные заказы за прошлые дни — чтобы ничего не потерялось."""
    q = order_query().where(Order.date < day, Order.status == "new").order_by(Order.date, Order.id)
    return list(s.scalars(q))


def debts(s: Session) -> list[Order]:
    q = order_query().where(Order.status == "done", Order.payment == "debt").order_by(Order.date)
    return list(s.scalars(q))


def pay_client(s: Session, client: Client, payment: str) -> list[Order]:
    """Клиент заплатил: закрываем его долги, а если долгов нет — ближайший открытый заказ."""
    closed = [o for o in debts(s) if o.client_id == client.id]
    if not closed:
        open_ = s.scalars(
            order_query()
            .where(Order.client_id == client.id, Order.status == "new")
            .order_by(Order.date)
        ).first()
        closed = [open_] if open_ else []
    for o in closed:
        set_status(o, "done", payment)
    return closed


# ---------- постоянные клиенты ----------


def recurring_hits(r: Recurring, day: date) -> bool:
    delta = (day - r.next_date).days
    return r.active and delta >= 0 and delta % max(r.every_days, 1) == 0


def generate_recurring(s: Session, up_to: date | None = None) -> list[Order]:
    """Создаёт заказы постоянным клиентам по графику (до завтрашнего дня включительно)."""
    t = today()
    up_to = up_to or t + timedelta(days=1)
    created = []
    for r in s.scalars(select(Recurring).where(Recurring.active)):
        step = timedelta(days=max(r.every_days, 1))
        while r.next_date <= up_to:
            if r.next_date >= t:
                exists = s.scalar(
                    select(Order.id).where(Order.recurring_id == r.id, Order.date == r.next_date)
                )
                if not exists:
                    created.append(
                        create_order(s, r.client, r.next_date, r.items, recurring_id=r.id,
                                     notes="🔁 постоянный")
                    )
            r.next_date += step
    s.commit()
    return created


def make_recurring(s: Session, order: Order, every_days: int) -> Recurring:
    r = Recurring(
        client=order.client,
        every_days=every_days,
        next_date=order.date + timedelta(days=every_days),
        items=[{"product_id": i.product_id, "qty": i.qty, "price": i.price} for i in order.items],
    )
    s.add(r)
    s.flush()
    order.recurring_id = r.id
    return r


def planned_on(s: Session, day: date) -> list[Recurring]:
    """Постоянные клиенты, которым по графику положен заказ в этот день (ещё не созданный)."""
    return [
        r
        for r in s.scalars(select(Recurring).where(Recurring.active).options(selectinload(Recurring.client)))
        if recurring_hits(r, day)
    ]


# ---------- деньги ----------


def add_expense(s: Session, amount: int, category: str = "other", note: str = "",
                day: date | None = None, created_by: int | None = None) -> Expense:
    if category not in EXPENSE_CATEGORIES:
        category = "other"
    e = Expense(amount=int(amount), category=category, note=note or "", date=day or today(),
                created_by=created_by)
    s.add(e)
    s.flush()
    return e


@dataclass
class Report:
    start: date
    end: date
    orders: int = 0
    revenue: int = 0
    cogs: int = 0
    friend_fees: int = 0
    cash: int = 0
    transfer: int = 0
    debt: int = 0
    expenses: dict = field(default_factory=dict)  # категория -> сумма (без закупки)
    purchases: int = 0
    by_product: dict = field(default_factory=dict)  # название -> {qty, unit, revenue}
    open_orders: int = 0

    @property
    def expenses_total(self) -> int:
        return sum(self.expenses.values())

    @property
    def profit(self) -> int:
        return self.revenue - self.cogs - self.friend_fees - self.expenses_total

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        d["expenses_total"], d["profit"] = self.expenses_total, self.profit
        return d


def report(s: Session, start: date, end: date) -> Report:
    """Прибыль = выручка − закупочная стоимость проданного − 50 ₽ другу за заявки − расходы (газ, прочее).
    Закупка товара партией показывается отдельно и в прибыль не идёт — её уже учитывает себестоимость."""
    r = Report(start, end)
    orders = s.scalars(order_query().where(Order.date >= start, Order.date <= end)).all()
    for o in orders:
        if o.status == "new":
            r.open_orders += 1
        if o.status != "done":
            continue
        r.orders += 1
        total = o.total
        r.revenue += total
        r.cogs += o.cogs
        r.friend_fees += o.friend_fee
        if o.payment in ("cash", "transfer", "debt"):
            setattr(r, o.payment, getattr(r, o.payment) + total)
        for i in o.items:
            p = r.by_product.setdefault(i.product.name, {"qty": 0, "unit": i.product.unit, "revenue": 0})
            p["qty"] += i.qty
            p["revenue"] += round(i.qty * i.price)
    for e in s.scalars(select(Expense).where(Expense.date >= start, Expense.date <= end)):
        if e.category == "purchase":
            r.purchases += e.amount
        else:
            r.expenses[e.category] = r.expenses.get(e.category, 0) + e.amount
    return r


def period(name: str) -> tuple[date, date]:
    t = today()
    if name == "week":
        return t - timedelta(days=t.weekday()), t
    if name == "month":
        return t.replace(day=1), t
    if name == "yesterday":
        return t - timedelta(days=1), t - timedelta(days=1)
    return t, t


# ---------- время доставки ----------


def eta_text(o: Order) -> str:
    """«примерно 14:00–15:00» или пусто, если маршрут ещё не считали."""
    if o.eta_from and o.eta_to:
        return f"примерно {o.eta_from}–{o.eta_to}"
    return ""


# ---------- тексты для бота ----------

STATUS_ICON = {"new": "🟡", "done": "✅", "cancelled": "❌"}


def order_text(o: Order, with_date: bool = False) -> str:
    c = o.client
    lines = [f"{STATUS_ICON.get(o.status, '')} <b>{esc(c.name)}</b>  · №{o.id}"]
    if with_date:
        lines.append(f"📅 {fmt_date(o.date)}")
    if c.phone:
        lines.append(f"📞 {fmt_phone(c.phone)}")
    addr = client_label(c)
    if addr:
        lines.append(f"📍 {esc(addr)}")
    for i in o.items:
        p = i.product
        lines.append(f"{p.emoji or '•'} {esc(p.name)} — {fmt_qty(i.qty)} {p.unit} × {fmt_qty(i.price)} = "
                     f"{fmt_money(i.qty * i.price)}")
    lines.append(f"💰 <b>{fmt_money(o.total)}</b>")
    extra = []
    if o.recurring_id:
        extra.append("🔁 постоянный")
    if o.via_friend:
        extra.append(f"🤝 через друга (−{o.friend_fee} ₽)")
    if o.status == "done":
        extra.append(f"оплата: {PAYMENTS.get(o.payment, '—')}")
    if o.source:
        extra.append(f"🌐 {esc(o.source)}")
    if extra:
        lines.append(" · ".join(extra))
    note = (o.notes or "").replace("🔁 постоянный", "").strip()
    if note:
        lines.append(f"📝 {esc(note)}")
    if c.notes:
        lines.append(f"ℹ️ {esc(c.notes)}")
    return "\n".join(lines)


def report_text(r: Report, title: str) -> str:
    lines = [f"💰 <b>{title}</b> ({r.start.strftime('%d.%m')}–{r.end.strftime('%d.%m')})", ""]
    lines.append(f"Доставлено заказов: <b>{r.orders}</b>")
    if r.open_orders:
        lines.append(f"Ещё не доставлено: {r.open_orders}")
    lines.append(f"Выручка: <b>{fmt_money(r.revenue)}</b>")
    lines.append(f"   наличка {fmt_money(r.cash)} · перевод {fmt_money(r.transfer)}"
                 + (f" · в долг {fmt_money(r.debt)}" if r.debt else ""))
    lines.append(f"Закупочная стоимость: −{fmt_money(r.cogs)}")
    if r.friend_fees:
        lines.append(f"Другу за заявки: −{fmt_money(r.friend_fees)}")
    for cat, v in r.expenses.items():
        lines.append(f"{EXPENSE_CATEGORIES[cat].capitalize()}: −{fmt_money(v)}")
    lines.append("")
    lines.append(f"🟢 <b>Чистая прибыль: {fmt_money(r.profit)}</b>")
    if r.purchases:
        lines.append(f"\nЗакуплено товара: {fmt_money(r.purchases)}")
    if r.by_product:
        lines.append("\nПродано:")
        for name, p in r.by_product.items():
            lines.append(f"• {esc(name)}: {fmt_qty(p['qty'])} {p['unit']} — {fmt_money(p['revenue'])}")
    return "\n".join(lines)


def esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sources_stats(s: Session, start: date, end: date) -> list[dict]:
    """Сколько заказов пришло с каждой QR-листовки / сайта за период."""
    out: dict[str, dict] = {}
    for o in s.scalars(order_query().where(Order.date >= start, Order.date <= end, Order.source != "")):
        d = out.setdefault(o.source, {"source": o.source, "orders": 0, "done": 0, "revenue": 0})
        d["orders"] += 1
        if o.status == "done":
            d["done"] += 1
            d["revenue"] += o.total
    return sorted(out.values(), key=lambda d: -d["orders"])
