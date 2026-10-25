from datetime import timedelta

from app import services as sv
from app.db import Product


def potato(s):
    return s.query(Product).filter_by(name="Картошка белая").one()


def test_phone_normalization():
    assert sv.normalize_phone("8 (900) 123-45-67") == "79001234567"
    assert sv.normalize_phone("+7 900 123 45 67") == "79001234567"
    assert sv.normalize_phone("9001234567") == "79001234567"
    assert sv.fmt_phone("89001234567") == "+7 900 123-45-67"


def test_upsert_client_matches_by_phone(s):
    c1, created1 = sv.upsert_client(s, {"name": "Марина", "phone": "89001234567"})
    c2, created2 = sv.upsert_client(s, {"name": "Марина Петровна", "phone": "+7 900 123-45-67", "floor": "4"})
    assert created1 and not created2
    assert c1.id == c2.id
    assert c2.name == "Марина" and c2.floor == "4"


def test_order_total_and_report(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "Иван"})
    t = sv.today()
    o = sv.create_order(s, c, t, [{"product_id": p.id, "qty": 50}], via_friend=True)
    assert o.total == 50 * 45
    sv.set_status(o, "done", "transfer")
    sv.add_expense(s, 3000, "fuel", day=t)
    sv.add_expense(s, 10000, "purchase", day=t)  # закупка не уменьшает прибыль второй раз
    s.commit()
    r = sv.report(s, t, t)
    assert r.revenue == 2250
    assert r.cogs == 50 * 20
    assert r.friend_fees == 50
    assert r.transfer == 2250
    assert r.purchases == 10000
    assert r.profit == 2250 - 1000 - 50 - 3000


def test_open_and_cancelled_orders_not_in_revenue(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "Оля"})
    t = sv.today()
    sv.create_order(s, c, t, [{"product_id": p.id, "qty": 50}])
    o2 = sv.create_order(s, c, t, [{"product_id": p.id, "qty": 50}])
    sv.set_status(o2, "cancelled")
    s.commit()
    r = sv.report(s, t, t)
    assert r.revenue == 0 and r.open_orders == 1


def test_recurring_generation(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "Постоянный"})
    t = sv.today()
    o = sv.create_order(s, c, t - timedelta(days=6), [{"product_id": p.id, "qty": 50, "price": 40}])
    r = sv.make_recurring(s, o, 7)
    s.commit()
    assert r.next_date == t + timedelta(days=1)
    created = sv.generate_recurring(s)
    assert len(created) == 1 and created[0].date == t + timedelta(days=1)
    assert created[0].items[0].price == 40
    assert r.next_date == t + timedelta(days=8)
    # повторный запуск не плодит дубли
    assert sv.generate_recurring(s) == []
    # плановые на будущее
    assert sv.planned_on(s, t + timedelta(days=8)) == [r]
    assert sv.planned_on(s, t + timedelta(days=9)) == []


def test_recurring_skips_missed_past_dates(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "Пропал"})
    t = sv.today()
    o = sv.create_order(s, c, t - timedelta(days=30), [{"product_id": p.id, "qty": 50}])
    r = sv.make_recurring(s, o, 14)  # next = t-16
    s.commit()
    created = sv.generate_recurring(s)
    assert all(x.date >= t for x in created)
    assert r.next_date > t + timedelta(days=1)


def test_pay_client_closes_debt(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "Должник"})
    o = sv.create_order(s, c, sv.today(), [{"product_id": p.id, "qty": 50}], status="done", payment="debt")
    s.commit()
    assert sv.debts(s) == [o]
    closed = sv.pay_client(s, c, "cash")
    assert closed == [o] and o.payment == "cash"
    assert sv.debts(s) == []


def test_order_text_renders(s):
    p = potato(s)
    c, _ = sv.upsert_client(s, {"name": "<Тест>", "phone": "89001234567", "address": "Ленина 5", "entrance": "2"})
    o = sv.create_order(s, c, sv.today(), [{"product_id": p.id, "qty": 50}])
    s.commit()
    text = sv.order_text(o)
    assert "&lt;Тест&gt;" in text and "под. 2" in text and "2 250 ₽" in text


def test_old_minimums_migrated_once(s):
    from app.db import engine, migrate

    potato = s.query(Product).filter_by(name="Картошка белая").one()
    onion = s.query(Product).filter_by(name="Лук").one()
    potato.min_qty, onion.min_qty = 50, 0  # как в базе до изменения
    s.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 0")
    migrate(engine)
    s.expire_all()
    assert (potato.min_qty, onion.min_qty) == (25, 5)
    onion.min_qty = 0  # Артур сам поменял в приложении — повторный запуск не трогает
    s.commit()
    migrate(engine)
    s.expire_all()
    assert onion.min_qty == 0


def test_potato_min_text(s):
    items = sv.products(s)
    assert sv.potato_min_text(items) == "от 1 сетки (25 кг)"
    for p in items:
        if sv.is_potato(p):
            p.min_qty = 50
    assert sv.potato_min_text(items) == "от 2 сеток (50 кг)"
