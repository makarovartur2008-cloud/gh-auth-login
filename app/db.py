import os
import secrets
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(10), default="member")  # owner | member
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Invite(Base):
    __tablename__ = "invites"
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(10), default="кг")  # кг | шт
    price: Mapped[float] = mapped_column(Float, default=0)  # цена продажи за единицу
    cost: Mapped[float] = mapped_column(Float, default=0)  # закупка за единицу
    min_qty: Mapped[float] = mapped_column(Float, default=0)
    emoji: Mapped[str] = mapped_column(String(8), default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    entrance: Mapped[str] = mapped_column(String(10), default="")
    floor: Mapped[str] = mapped_column(String(10), default="")
    apartment: Mapped[str] = mapped_column(String(10), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # координаты адреса (заполняет app/routing.py); geo_address — для какого адреса посчитаны
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    geo_address: Mapped[str] = mapped_column(String(250), default="")
    # куда писать клиенту о заказе (заполняет app/notify.py, когда клиент подписался)
    tg_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_chat_id: Mapped[str] = mapped_column(String(40), default="")

    orders: Mapped[list["Order"]] = relationship(back_populates="client")
    recurring: Mapped[list["Recurring"]] = relationship(back_populates="client")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    # new — ждёт доставки, done — доставлен, cancelled — отменён
    status: Mapped[str] = mapped_column(String(10), default="new")
    # cash | transfer | debt (в долг) | None
    payment: Mapped[str | None] = mapped_column(String(10), nullable=True)
    via_friend: Mapped[bool] = mapped_column(Boolean, default=False)
    friend_fee: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    recurring_id: Mapped[int | None] = mapped_column(ForeignKey("recurring.id"), nullable=True)
    # откуда заказ: пусто — внесли сами, иначе метка QR-листовки (например «Ленина 5») или «сайт»
    source: Mapped[str] = mapped_column(String(60), default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # маршрут: порядковый номер точки и окно прибытия «HH:MM» (заполняет app/routing.py)
    route_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eta_from: Mapped[str] = mapped_column(String(5), default="")
    eta_to: Mapped[str] = mapped_column(String(5), default="")
    # секрет для ссылки «следить за заказом» (подписка клиента на сообщения)
    token: Mapped[str] = mapped_column(String(24), default=lambda: secrets.token_urlsafe(12))
    # клиенту уже отправили «завтра привезём, примерно …» (сбрасывается при переносе)
    eta_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # рейс: "" — дневной (от ROUTE_START), "evening" — вечерний (EVENING_START–EVENING_END)
    slot: Mapped[str] = mapped_column(String(10), default="")
    # подарок за перенос: carrot | beet | onion (строка-подарок с ценой 0 в items)
    bonus: Mapped[str] = mapped_column(String(10), default="")

    client: Mapped[Client] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )

    @property
    def total(self) -> int:
        return round(sum(i.qty * i.price for i in self.items))

    @property
    def cogs(self) -> int:
        return round(sum(i.qty * i.cost for i in self.items))


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float, default=0)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class Recurring(Base):
    """Постоянный клиент: заказ повторяется каждые every_days дней."""

    __tablename__ = "recurring"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    every_days: Mapped[int] = mapped_column(Integer, default=7)
    next_date: Mapped[date] = mapped_column(Date)
    # [{"product_id": 1, "qty": 50, "price": 45}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    client: Mapped[Client] = relationship(back_populates="recurring")


class Reschedule(Base):
    """Перенос доставки: предложение клиенту (kind=staff) или перенос самим клиентом (kind=client)."""

    __tablename__ = "reschedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="staff")
    # [{"date": "2026-10-23", "slot": "", "label": "Завтра днём"}, ...]
    options: Mapped[list] = mapped_column(JSON, default=list)
    # из чего клиент выбирает подарок: ["carrot", "beet", "onion"]; [] — подарок уже решён или без него
    bonus_choices: Mapped[list] = mapped_column(JSON, default=list)
    old_date: Mapped[date] = mapped_column(Date)
    old_slot: Mapped[str] = mapped_column(String(10), default="")
    old_eta: Mapped[str] = mapped_column(String(20), default="")
    # pending — ждём ответа, answered — клиент (или мы за него) выбрал
    status: Mapped[str] = mapped_column(String(10), default="pending")
    chosen: Mapped[int | None] = mapped_column(Integer, nullable=True)  # индекс в options
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    order: Mapped["Order"] = relationship()


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    # fuel — газ/бензин, purchase — закупка товара, other — прочее
    category: Mapped[str] = mapped_column(String(10), default="other")
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# Начальный прайс. Меняется в приложении во вкладке «Цены». Мёд свой — закупка 0.
DEFAULT_PRODUCTS = [
    dict(name="Картошка белая", unit="кг", price=45, cost=20, min_qty=25, emoji="🥔"),
    dict(name="Картошка красная", unit="кг", price=45, cost=20, min_qty=25, emoji="🥔"),
    dict(name="Лук", unit="кг", price=40, cost=20, min_qty=5, emoji="🧅"),
    dict(name="Лук красный", unit="кг", price=80, cost=20, min_qty=5, emoji="🧅"),
    dict(name="Морковь", unit="кг", price=40, cost=25, min_qty=5, emoji="🥕"),
    # мытую берём только мешками — продаём от 20 кг
    dict(name="Морковь мытая", unit="кг", price=70, cost=45, min_qty=20, emoji="🥕"),
    dict(name="Свёкла", unit="кг", price=40, cost=22, min_qty=5, emoji="🟣"),
    dict(name="Мёд липовый 1 л", unit="шт", price=700, cost=0, emoji="🍯"),
    dict(name="Мёд липовый 3 л", unit="шт", price=2000, cost=0, emoji="🍯"),
]


def make_engine(path: str):
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    return engine


# Колонки, добавленные после первого выпуска: (таблица, колонка, SQL-описание)
NEW_COLUMNS = [
    ("orders", "source", "VARCHAR(60) DEFAULT '' NOT NULL"),
    ("orders", "route_pos", "INTEGER"),
    ("orders", "eta_from", "VARCHAR(5) DEFAULT '' NOT NULL"),
    ("orders", "eta_to", "VARCHAR(5) DEFAULT '' NOT NULL"),
    ("orders", "token", "VARCHAR(24) DEFAULT '' NOT NULL"),
    ("orders", "eta_notified", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("orders", "slot", "VARCHAR(10) DEFAULT '' NOT NULL"),
    ("orders", "bonus", "VARCHAR(10) DEFAULT '' NOT NULL"),
    ("clients", "lat", "FLOAT"),
    ("clients", "lon", "FLOAT"),
    ("clients", "geo_address", "VARCHAR(250) DEFAULT '' NOT NULL"),
    ("clients", "tg_chat_id", "INTEGER"),
    ("clients", "max_chat_id", "VARCHAR(40) DEFAULT '' NOT NULL"),
]


# Минимумы в заказе: картошка от 1 сетки (было 2), овощи от 5 кг, мытая морковь от 20 кг.
# (товар, старое значение, новое) — меняем только нетронутые старые значения
MIN_QTY_V1 = [("Картошка белая", 50, 25), ("Картошка красная", 50, 25), ("Лук", 0, 5), ("Лук красный", 0, 5),
              ("Морковь", 0, 5), ("Морковь мытая", 0, 20), ("Свёкла", 0, 5)]


def migrate(engine):
    """Добавляет новые колонки в уже существующую базу."""
    with engine.begin() as conn:
        for table, col, ddl in NEW_COLUMNS:
            cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # старым заказам — свои токены
        for (oid,) in conn.exec_driver_sql("SELECT id FROM orders WHERE token = ''").fetchall():
            conn.exec_driver_sql("UPDATE orders SET token = ? WHERE id = ?", (secrets.token_urlsafe(12), oid))
        # правки данных — по одному разу на базу (номер хранится в самом файле SQLite)
        if conn.exec_driver_sql("PRAGMA user_version").scalar() < 1:
            for name, old, new in MIN_QTY_V1:
                conn.exec_driver_sql("UPDATE products SET min_qty = ? WHERE name = ? AND min_qty = ?", (new, name, old))
            conn.exec_driver_sql("PRAGMA user_version = 1")


def init_db(engine) -> sessionmaker:
    Base.metadata.create_all(engine)
    migrate(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as s:
        if not s.query(Product).first():
            for i, p in enumerate(DEFAULT_PRODUCTS):
                s.add(Product(sort=i, **p))
            s.commit()
    return Session


engine = make_engine(settings.db_path)
SessionLocal = init_db(engine)
