from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import public
from app import services as sv
from app.db import Client, Order, Product, SessionLocal, engine, migrate


@pytest.fixture
def client():
    public._hits.clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


def ids():
    with SessionLocal() as s:
        return {p.name: p.id for p in s.query(Product)}


def body(**kw):
    p = ids()
    d = dict(name="Нина", phone="8 900 111-22-33", address="Ленина 5", entrance="2", floor="3",
             date=(sv.today() + timedelta(days=1)).isoformat(), source="Ленина 5",
             items=[{"product_id": p["Картошка белая"], "qty": 50}, {"product_id": p["Морковь"], "qty": 5}])
    d.update(kw)
    return d


def test_info_hides_products_without_price(client):
    with SessionLocal() as s:
        s.query(Product).filter_by(name="Лук круглый").one().price = 0
        s.commit()
    info = client.get("/api/public/info").json()
    names = [p["name"] for p in info["products"]]
    assert "Картошка белая" in names and "Лук круглый" not in names and "Лук плоский" in names
    assert len(info["days"]) == public.DAYS_AHEAD


def test_order_created_with_source(client):
    r = client.post("/api/public/order", json=body())
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 50 * 45 + 5 * 40
    with SessionLocal() as s:
        o = s.scalar(sv.order_query())
        assert o.source == "Ленина 5" and o.status == "new" and not o.via_friend
        assert o.client.phone == "8 900 111-22-33"
        assert sv.sources_stats(s, sv.today(), sv.today() + timedelta(days=2))[0]["orders"] == 1


def test_validation(client):
    p = ids()
    assert client.post("/api/public/order", json=body(phone="123")).status_code == 400
    assert client.post("/api/public/order", json=body(date="2001-01-01")).status_code == 400
    small = body(items=[{"product_id": p["Картошка белая"], "qty": 20}])
    assert "от 25" in client.post("/api/public/order", json=small).json()["detail"]
    carrot = body(items=[{"product_id": p["Картошка белая"], "qty": 25}, {"product_id": p["Морковь"], "qty": 3}])
    assert "от 5" in client.post("/api/public/order", json=carrot).json()["detail"]
    washed = body(items=[{"product_id": p["Картошка белая"], "qty": 25}, {"product_id": p["Морковь мытая"], "qty": 10}])
    assert "от 20" in client.post("/api/public/order", json=washed).json()["detail"]
    no_potato = body(items=[{"product_id": p["Морковь"], "qty": 10}])
    assert "от 1 сетки (25 кг)" in client.post("/api/public/order", json=no_potato).json()["detail"]
    with SessionLocal() as s:
        assert s.query(Order).count() == 0


def test_honeypot_creates_nothing(client):
    assert client.post("/api/public/order", json=body(website="spam")).json()["ok"]
    with SessionLocal() as s:
        assert s.query(Order).count() == 0


def test_rate_limit(client):
    codes = [client.post("/api/public/order", json=body()).status_code for _ in range(public.LIMIT + 1)]
    assert codes[-1] == 429 and codes[0] == 200


def test_existing_client_not_overwritten(client):
    with SessionLocal() as s:
        sv.upsert_client(s, dict(name="Нина Ивановна", phone="89001112233", address="Мира 1"))
        s.commit()
    client.post("/api/public/order", json=body(name="Хакер", address="Другой адрес"))
    with SessionLocal() as s:
        c = s.query(Client).one()
        assert c.name == "Нина Ивановна" and c.address == "Мира 1" and c.entrance == "2"
        assert "адрес в заявке: Другой адрес" in s.query(Order).one().notes


def test_flyer_and_qr(client):
    r = client.get("/flyer?s=Ленина 5")
    assert r.status_code == 200 and "<svg" in r.text and "45 ₽/кг" in r.text
    assert client.get("/qr.svg").text.startswith("<svg")
    assert client.get("/order").status_code == 200


def test_migration_adds_source_column():
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE orders DROP COLUMN source")
    migrate(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(orders)"))}
    assert "source" in cols


def test_flyer_phone_above_qr(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "contact_phone", "89001234567")
    html = client.get("/flyer").text
    assert "+7 900 123-45-67" in html
    assert html.index("+7 900 123-45-67") < html.index('<div class="qr">')


def test_order_without_potato_rejected(client):
    p = ids()
    r = client.post("/api/public/order", json=body(items=[{"product_id": p["Морковь"], "qty": 5}]))
    assert r.status_code == 400 and "картошк" in r.json()["detail"]
    with SessionLocal() as s:
        assert s.query(Order).count() == 0
    assert all("potato" in x for x in client.get("/api/public/info").json()["products"])


def test_one_sack_of_potato_is_enough(client):
    p = ids()
    r = client.post("/api/public/order", json=body(items=[{"product_id": p["Картошка красная"], "qty": 25}]))
    assert r.status_code == 200, r.text


def test_flyer_shows_new_minimums(client):
    html = client.get("/flyer").text
    assert "Картошка от 1 сетки (25 кг)" in html and "от 20 кг" in html and "50 кг" not in html
    assert "1 125 ₽/сетка" in html


def test_review_saved_and_page_served(client):
    from app.db import Review

    assert "Отправить отзыв" in client.get("/otzyv").text
    assert client.get("/api/public/review-info").json()["gift"] == "5 кг моркови"
    r = client.post("/api/public/review", json={"name": "Нина", "phone": "89001112233", "rating": 5,
                                                 "text": "Картошка крупная, привезли вовремя"})
    assert r.status_code == 200 and r.json()["gift"] == "5 кг моркови"
    # пятёрка без текста — просим пару слов; ловушка для ботов ничего не пишет
    assert client.post("/api/public/review", json={"name": "Б", "rating": 5}).status_code == 400
    assert client.post("/api/public/review", json={"name": "Бот", "text": "x", "website": "spam"}).json()["ok"]
    with SessionLocal() as s:
        assert [(x.name, x.rating) for x in s.query(Review)] == [("Нина", 5)]


def test_review_url_redirects(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_url", "https://yandex.ru/maps/org/123/reviews/")
    r = client.get("/otzyv", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].startswith("https://yandex.ru/maps")


def test_review_notifies_staff(monkeypatch):
    import asyncio

    from app import bot as staff_bot
    from app.db import Review, User

    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    monkeypatch.setattr(staff_bot, "BOT", FakeBot())
    with SessionLocal() as s:
        s.add(User(tg_id=7, name="Артур", role="owner"))
        r = Review(name="Нина <b>", phone="89001112233", rating=4, text="Хорошо")
        s.add(r)
        s.commit()
        rid = r.id
    asyncio.run(public.notify_review(rid))
    assert sent and sent[0][0] == 7
    text = sent[0][1]
    assert "⭐⭐⭐⭐ " in text and "&lt;b&gt;" in text and "5 кг моркови" in text
