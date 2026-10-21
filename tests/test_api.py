from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import services as sv
from app.config import settings
from app.db import SessionLocal, User


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "dev_user_tg_id", 111)
    with SessionLocal() as s:
        s.add(User(tg_id=111, name="Артур", role="owner"))
        s.commit()
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_no_access_without_user(monkeypatch):
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/me").status_code == 403
        assert c.get("/api/me", headers={"X-Init-Data": "bad"}).status_code == 403


def test_full_flow(client):
    prods = client.get("/api/products").json()
    potato = prods[0]
    t = sv.today()
    tomorrow = (t + timedelta(days=1)).isoformat()
    r = client.post("/api/orders", json={
        "client": {"name": "Марина", "phone": "89001234567", "address": "Ленина 5"},
        "date": t.isoformat(), "items": [{"product_id": potato["id"], "qty": 50}], "every_days": 7})
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["total"] == 2250 and order["via_friend"] is False

    day = client.get(f"/api/orders?day={t.isoformat()}").json()
    assert len(day["orders"]) == 1
    future = (t + timedelta(days=14)).isoformat()
    assert len(client.get(f"/api/orders?day={future}").json()["planned"]) == 1
    counts = client.get(f"/api/orders/counts?start={t.isoformat()}&days=21").json()
    assert counts[t.isoformat()] == 1 and counts[future] == 1

    assert client.patch(f"/api/orders/{order['id']}", json={"status": "done", "payment": "cash"}).json()["status"] == "done"
    client.post("/api/expenses", json={"date": t.isoformat(), "amount": 500, "category": "fuel"})
    rep = client.get(f"/api/report?start={t}&end={t}").json()
    assert rep["revenue"] == 2250 and rep["profit"] == 2250 - 1000 - 500

    cl = client.get("/api/clients?q=900123").json()
    assert cl[0]["regular"] is True
    card = client.get(f"/api/clients/{cl[0]['id']}").json()
    assert card["stats"]["orders"] == 1 and len(card["recurring"]) == 1

    rec = card["recurring"][0]
    upd = client.put(f"/api/recurring/{rec['id']}", json={"every_days": 14, "next_date": tomorrow}).json()
    assert upd["every_days"] == 14
    edit = client.put(f"/api/products/{potato['id']}", json={**potato, "price": 50}).json()
    assert edit["price"] == 50

    assert client.get("/").status_code == 200
