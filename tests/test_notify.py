import asyncio
import json
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

from app import max_bot, notify, routing
from app import services as sv
from app.config import settings
from app.db import Client, Order, Product, SessionLocal, User


def make_order(s, phone="8 900 111-22-33", days=1, **client_kw) -> Order:
    p = s.query(Product).filter_by(name="Картошка белая").one()
    c, _ = sv.upsert_client(s, dict(name="Нина", phone=phone, address="Ленина 5"))
    for k, v in client_kw.items():
        setattr(c, k, v)
    o = sv.create_order(s, c, sv.today() + timedelta(days=days), [{"product_id": p.id, "qty": 50, "price": 45}])
    s.commit()
    return o


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(settings, "contact_phone", "89083003917")


@pytest.fixture
def tg_sent(monkeypatch):
    sent = []

    async def fake(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(notify, "send_tg", fake)
    return sent


@pytest.fixture
def http(monkeypatch):
    """Подменяет сеть httpx: запросы складываются в список, ответы задаёт тест через http.routes."""
    calls, routes = [], {}

    def handler(req: httpx.Request):
        calls.append(req)
        status, body = routes.get(req.url.host, (200, {}))
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))

    class H:
        pass
    h = H()
    h.calls, h.routes = calls, routes
    return h


SMS_OK = (200, {"status": "OK", "status_code": 100,
                "sms": {"79001112233": {"status": "OK", "status_code": 100, "sms_id": "1"}}})


# ---------- шаблоны ----------


def test_templates_without_and_with_eta(s, phone):
    o = make_order(s)
    t = notify.message_text(o, "accepted")
    assert t.startswith(f"Заказ №{o.id} принят! Привезём завтра")
    assert "Точное время сообщим накануне вечером." in t and "2 250 ₽" in t and "+7 908 300-39-17" in t
    assert notify.status_kind(o) == "accepted"

    o.eta_from, o.eta_to = "14:00", "15:00"
    assert "завтра" in notify.message_text(o, "accepted") and "примерно 14:00–15:00" in notify.message_text(o, "accepted")
    assert "накануне" not in notify.message_text(o, "accepted")
    assert notify.message_text(o, "eta").startswith(f"Завтра привезём ваш заказ №{o.id}, примерно 14:00–15:00.")
    assert notify.message_text(o, "on_the_way").startswith("Выехал к вам, буду минут через 15–30.")
    assert notify.status_kind(o) == "eta"
    sms = notify.sms_text(o, "eta")
    assert "14:00-15:00" in sms and "89083003917" in sms and len(sms) <= 70


def test_wa_link(s, phone):
    o = make_order(s)
    url = notify.wa_link(o)
    assert url.startswith("https://wa.me/79001112233?text=")
    assert f"Заказ №{o.id} принят!" in unquote(url)
    o.client.phone = "123"
    assert notify.wa_link(o) == ""

    from app.bot import order_kb
    o.client.phone = "+7 900 111-22-33"
    btns = [b for row in order_kb(o).inline_keyboard for b in row]
    assert any(b.url and b.url.startswith("https://wa.me/79001112233") for b in btns)
    assert {f"n:{o.id}:status", f"n:{o.id}:go"} <= {b.callback_data for b in btns}
    o.status = "done"
    assert not any(b.url for row in order_kb(o).inline_keyboard for b in row)


# ---------- рассылка ----------


def test_fan_out_and_one_failing_channel(s, monkeypatch, tg_sent, http):
    monkeypatch.setattr(settings, "max_bot_token", "mx")
    monkeypatch.setattr(settings, "sms_ru_api_id", "key")
    o = make_order(s, tg_chat_id=555, max_chat_id="77")
    http.routes["sms.ru"] = SMS_OK
    assert asyncio.run(notify.notify_client(s, o, "accepted")) == ["telegram", "max", "sms"]
    assert tg_sent[0][0] == 555
    mx = next(r for r in http.calls if r.url.host == "platform-api2.max.ru")
    assert mx.headers["Authorization"] == "mx" and mx.url.params["user_id"] == "77"
    assert json.loads(mx.content)["text"].startswith(f"Заказ №{o.id} принят!")
    sms = next(r for r in http.calls if r.url.host == "sms.ru")
    assert sms.url.params["to"] == "79001112233" and sms.url.params["api_id"] == "key"

    # MAX сломался — остальные всё равно доходят, исключение наружу не летит
    http.routes["platform-api2.max.ru"] = (500, {})
    assert asyncio.run(notify.notify_client(s, o, "accepted")) == ["telegram", "sms"]
    # SMS только для «принят» и «время»
    http.calls.clear()
    assert asyncio.run(notify.notify_client(s, o, "on_the_way")) == ["telegram"]
    assert not any(r.url.host == "sms.ru" for r in http.calls)


def test_sms_skipped_when_not_configured(s, monkeypatch, http):
    monkeypatch.setattr(settings, "sms_ru_api_id", "")
    o = make_order(s)
    assert asyncio.run(notify.notify_client(s, o, "accepted")) == []
    assert http.calls == []


def test_sms_error_is_not_success(s, monkeypatch, http):
    monkeypatch.setattr(settings, "sms_ru_api_id", "key")
    http.routes["sms.ru"] = (200, {"status": "ERROR", "status_code": 200, "status_text": "Неправильный api_id"})
    o = make_order(s)
    assert asyncio.run(notify.notify_client(s, o, "accepted")) == []


def test_evening_job_sends_once(s, monkeypatch, tg_sent):
    async def fake_plan(sess, day):
        orders = [o for o in sv.orders_on(sess, day) if o.status == "new"]
        for o in orders:
            o.eta_from, o.eta_to = "12:00", "13:00"
        sess.commit()
        return orders
    monkeypatch.setattr(routing, "plan_day", fake_plan)
    o = make_order(s, tg_chat_id=555)
    make_order(s, phone="89005556677")  # не подписан — ему писать некуда
    make_order(s, phone="89005556678", days=2, tg_chat_id=556)  # не завтра

    assert asyncio.run(notify.evening_eta(SessionLocal)) == 1
    assert asyncio.run(notify.evening_eta(SessionLocal)) == 0
    assert len(tg_sent) == 1 and "примерно 12:00–13:00" in tg_sent[0][1]
    with SessionLocal() as s2:
        assert s2.get(Order, o.id).eta_notified


# ---------- подписка через Telegram ----------

_DP = None


def dispatcher() -> Dispatcher:
    global _DP
    if _DP is None:  # роутер можно подключить только один раз
        from app import bot as tgbot
        from app import client_bot
        _DP = Dispatcher()
        _DP.include_router(client_bot.router)
        _DP.include_router(tgbot.router)
    return _DP


@pytest.fixture
def tg(monkeypatch):
    sent = []

    async def fake_call(self, method, request_timeout=None):
        sent.append(method)
    monkeypatch.setattr(Bot, "__call__", fake_call)
    bot = Bot("42:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    def send(user_id, text):
        upd = Update.model_validate({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Нина"}, "text": text}}, context={"bot": bot})
        asyncio.run(dispatcher().feed_update(bot, upd))
        return sent[-1].text if sent else ""
    return send


def test_start_subscribes_client_and_never_makes_owner(s, tg, phone):
    o = make_order(s)
    reply = tg(555, f"/start c_{o.token}")
    assert f"Вы подписаны на заказ №{o.id}" in reply and "Точное время сообщим" in reply
    with SessionLocal() as s2:
        assert s2.query(User).count() == 0  # клиент не стал владельцем, хотя сотрудников ещё нет
        assert s2.get(Client, o.client_id).tg_chat_id == 555
    # любое другое сообщение подписанного клиента — вежливый автоответ с телефоном
    assert "+7 908 300-39-17" in tg(555, "/start")
    assert "+7 908 300-39-17" in tg(555, "Когда привезёте?")
    # плохая ссылка тоже не даёт доступ
    assert "устарела" in tg(556, "/start c_wrong")
    with SessionLocal() as s2:
        assert s2.query(User).count() == 0
    # обычный /start по-прежнему делает первого владельцем
    tg(1, "/start")
    with SessionLocal() as s2:
        assert s2.query(User).one().tg_id == 1


# ---------- MAX ----------


def test_max_bot_started_subscribes(s, monkeypatch, http, phone):
    monkeypatch.setattr(settings, "max_bot_token", "mx")
    o = make_order(s)
    asyncio.run(max_bot.handle_update({"update_type": "bot_started", "chat_id": 123, "timestamp": 0,
                                       "user": {"user_id": 99, "first_name": "Нина"},
                                       "payload": f"c_{o.token}"}))
    with SessionLocal() as s2:
        assert s2.get(Client, o.client_id).max_chat_id == "99"
    req = http.calls[-1]
    assert req.method == "POST" and req.url.path == "/messages" and req.url.params["user_id"] == "99"
    assert json.loads(req.content)["text"].startswith(f"Вы подписаны на заказ №{o.id}")


# ---------- страница заказа ----------


def test_public_order_returns_links_and_sends_sms(monkeypatch, http):
    from fastapi.testclient import TestClient

    from app import public
    from app.main import app
    public._hits.clear()
    monkeypatch.setattr(settings, "sms_ru_api_id", "key")
    monkeypatch.setattr(notify, "TG_BOT_USERNAME", "kartoshka_bot")
    http.routes["sms.ru"] = SMS_OK
    with SessionLocal() as s:
        pid = s.query(Product).filter_by(name="Картошка белая").one().id
    with TestClient(app) as c:
        r = c.post("/api/public/order", json=dict(
            name="Нина", phone="8 900 111-22-33", address="Ленина 5",
            date=(sv.today() + timedelta(days=1)).isoformat(), items=[{"product_id": pid, "qty": 50}]))
    data = r.json()
    assert r.status_code == 200 and data["token"]
    assert data["tg_link"] == f"https://t.me/kartoshka_bot?start=c_{data['token']}"
    assert data["max_link"] == ""  # MAX не настроен
    sms = [q for q in http.calls if q.url.host == "sms.ru"]
    assert len(sms) == 1 and "принят" in parse_qs(urlparse(str(sms[0].url)).query)["msg"][0]
