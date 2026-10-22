"""Маршрут на день и примерное время доставки на каждый адрес.

Заглушка-контракт: полную реализацию пишет отдельная задача. Публичный интерфейс:
  plan_day(s, day) -> list[Order] — упорядочивает открытые заказы дня, заполняет
  order.route_pos / order.eta_from / order.eta_to и делает commit.
"""

from datetime import date

from sqlalchemy.orm import Session

from app import services as sv
from app.db import Order


async def plan_day(s: Session, day: date) -> list[Order]:
    orders = [o for o in sv.orders_on(s, day) if o.status == "new"]
    for i, o in enumerate(orders, 1):
        o.route_pos = i
    s.commit()
    return orders
