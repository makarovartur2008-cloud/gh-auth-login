import json

from app import ai
from app import services as sv
from app.db import Client, Expense, Order, Product


def test_apply_order_expense_and_recurring(s):
    potato = s.query(Product).filter_by(name="Картошка красная").one()
    onion = s.query(Product).filter_by(name="Лук").one()
    t = sv.today()
    raw = json.dumps({"actions": [
        {"type": "order", "client": {"id": None, "name": "Марина", "phone": "89001234567", "address": "Ленина 5",
                                     "entrance": "2", "floor": "4", "apartment": None},
         "date": t.isoformat(), "items": [{"product_id": potato.id, "qty": 25, "price": None},
                                           {"product_id": onion.id, "qty": 10, "price": 50}],
         "status": "done", "payment": "transfer", "every_days": 7},
        {"type": "expense", "category": "fuel", "amount": 3000, "date": t.isoformat()},
        {"type": "nonsense"},
    ], "reply": ""})
    parsed = ai.parse_json(raw)
    preview = ai.preview(s, parsed)
    assert "новый клиент" in preview and "Расход" in preview
    done = ai.apply(s, parsed, created_by=1, via_friend=False)
    assert len(done) == 2
    o = s.query(Order).one()
    assert o.total == 25 * 45 + 500 and o.status == "done" and o.payment == "transfer"
    assert o.recurring_id is not None
    assert s.query(Expense).one().amount == 3000
    assert s.query(Client).one().floor == "4"


def test_payment_action(s):
    potato = s.query(Product).first()
    c, _ = sv.upsert_client(s, {"name": "Иван"})
    o = sv.create_order(s, c, sv.today(), [{"product_id": potato.id, "qty": 50}], status="done", payment="debt")
    s.commit()
    parsed = ai.parse_json(json.dumps({"actions": [{"type": "payment", "client_id": c.id, "payment": "cash"}]}))
    ai.apply(s, parsed, created_by=1, via_friend=False)
    s.refresh(o)
    assert o.payment == "cash"


def test_bad_json():
    assert ai.parse_json("not json").actions == []


def test_prompt_mentions_products(s):
    p = ai.build_prompt(s)
    assert "Картошка белая" in p and "25 кг" in p
