import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update
from fastapi.testclient import TestClient

from app import reschedule as rs
from app import routing
from app import services as sv
from app.config import settings
from app.db import Order, Product, Reschedule, SessionLocal, User
from tests.test_notify import _callback, dispatcher


def at(hour: int, minute: int = 0):
    """Подменяет «сейчас» для правил переноса (сегодня, hour:minute)."""
    t = sv.today()
    return lambda: datetime(t.year, t.month, t.day, hour, minute, tzinfo=settings.zone)


def make_order(s, days=0, slot="", phone="89001112233", tg_chat_id=None) -> Order:
    p = s.query(Product).filter_by(name="Картошка белая").one()
    c, _ = sv.upsert_client(s, dict(name="Марина Петрова", phone=phone, address="Ленина 5"))
    c.tg_chat_id = tg_chat_id
    o = sv.create_order(s, c, sv.today() + timedelta(days=days), [dict(product_id=p.id, qty=50)])
    o.slot = slot
    s.commit()
    return o


@pytest.fixture
def morning(monkeypatch):
    monkeypatch.setattr(rs, "_now", at(9))
    monkeypatch.setattr(settings, "webapp_url", "https://kartoshka.example")
    monkeypatch.setattr(settings, "contact_phone", "89083003917")


# ---------- варианты ----------


def test_presets_today_evening_depends_on_time(s, monkeypatch, morning):
    t = sv.today()
    opts = rs.preset_options(t, "", ["today_evening", "next_same", "next_evening", "after_next"])
    assert [(o["date"], o["slot"]) for o in opts] == [
        (t.isoformat(), "evening"), ((t + timedelta(days=1)).isoformat(), ""),
        ((t + timedelta(days=1)).isoformat(), "evening"), ((t + timedelta(days=2)).isoformat(), "")]
    assert opts[0]["label"] == "Сегодня вечером (18:00–21:00)" and opts[1]["label"] == "Завтра днём"
    # в 17:30 вечерний рейс уже не предлагаем (меньше часа до начала)
    monkeypatch.setattr(rs, "_now", at(17, 30))
    assert "today_evening" not in rs.available_presets(t, "")
    # вечерний заказ: «сегодня вечером» — это он сам, не предлагаем; «тот же рейс» = завтра вечером
    opts = rs.preset_options(t, "evening", ["today_evening", "next_same", "next_evening"])
    assert [(o["date"], o["slot"]) for o in opts] == [((t + timedelta(days=1)).isoformat(), "evening")]


def test_self_options_exclude_current(s, morning):
    o = make_order(s, days=1)
    opts = rs.self_options(o)
    assert (o.date.isoformat(), "") not in [(x["date"], x["slot"]) for x in opts]
    assert opts[0]["slot"] == "evening" and opts[0]["date"] == sv.today().isoformat()
    assert len(opts) == 1 + rs.SELF_DAYS * 2 - 1


# ---------- предложение и ответ ----------


def test_offer_moves_to_first_option_and_gives_fixed_gift(s, morning):
    o = make_order(s)
    o.eta_from, o.eta_to = "12:00", "13:00"
    s.commit()
    r = rs.offer(s, o, ["today_evening", "next_same"], "carrot", created_by=1)
    s.commit()
    assert r.old_eta == "сегодня 12:00–13:00"
    assert (o.date, o.slot, o.eta_from) == (sv.today(), "evening", "")
    gift = [i for i in o.items if i.price == 0]
    assert o.bonus == "carrot" and len(gift) == 1 and gift[0].product.name == "Морковь"
    assert gift[0].qty == settings.bonus_kg and o.total == 50 * 45
    assert o.cogs == 50 * 20 + 5 * gift[0].product.cost  # подарок честно уменьшает прибыль
    text = rs.offer_text(r)
    assert "Марина, здравствуйте" in text and "5 кг моркови" in text and "https://kartoshka.example/r/" in text


def test_answer_with_gift_choice_and_new_offer_replaces_old(s, morning):
    o = make_order(s, days=1)
    r1 = rs.offer(s, o, ["next_same", "next_evening"], "choice")
    assert o.bonus == "" and r1.bonus_choices == ["carrot", "beet", "onion"]
    r2 = rs.offer(s, o, ["next_same", "next_evening"], "choice")
    s.commit()
    assert r1.status == "answered" and rs.pending(s, o).id == r2.id
    label = rs.answer(s, r2, 1, "beet")
    s.commit()
    assert label.endswith("вечером (18:00–21:00)") and o.slot == "evening" and o.bonus == "beet"
    # смена подарка не копит подарки
    sv.apply_bonus(s, o, "onion")
    assert [i.product.name for i in o.items if i.price == 0] == ["Лук"]


def test_preview_does_not_touch_db(s, morning):
    o = make_order(s)
    text = rs.preview(o, ["next_same"], "beet")
    s.commit()
    assert "5 кг свёклы" in text and s.query(Reschedule).count() == 0 and o.bonus == ""


# ---------- страница /r/<token> ----------


@pytest.fixture
def client(monkeypatch, morning):
    notified = []

    async def fake_notify(order_id, header):
        notified.append(header)
    monkeypatch.setattr("app.bot.notify_order", fake_notify)
    from app.main import app
    with TestClient(app) as c:
        c.notified = notified
        yield c


def test_page_offer_flow(s, client):
    o = make_order(s)
    rs.offer(s, o, ["today_evening", "next_same"], "choice")
    s.commit()
    info = client.get(f"/api/public/resched/{o.token}").json()
    assert info["mode"] == "offer" and len(info["options"]) == 2 and len(info["bonus_choices"]) == 3
    bad = client.post(f"/api/public/resched/{o.token}", json={"date": "2001-01-01", "slot": ""})
    assert bad.status_code == 400
    pick = info["options"][1]
    r = client.post(f"/api/public/resched/{o.token}", json={**pick, "bonus": "onion"})
    assert r.status_code == 200 and "5 кг лука" in r.json()["text"]
    with SessionLocal() as s2:
        got = s2.get(Order, o.id)
        assert got.date.isoformat() == pick["date"] and got.bonus == "onion"
        assert s2.query(Reschedule).one().status == "answered"
    assert "выбрал(а)" in client.notified[0] and "лука" in client.notified[0]
    # после ответа ссылка работает как «перенести самому»
    assert client.get(f"/api/public/resched/{o.token}").json()["mode"] == "self"
    assert client.get("/r/" + o.token).status_code == 200


def test_page_self_move_notifies_staff(s, client):
    o = make_order(s, days=1)
    info = client.get(f"/api/public/resched/{o.token}").json()
    assert info["mode"] == "self" and info["bonus_choices"] == []
    pick = next(x for x in info["options"] if x["slot"] == "evening" and x["date"] != sv.today().isoformat())
    assert client.post(f"/api/public/resched/{o.token}", json=pick).status_code == 200
    with SessionLocal() as s2:
        r = s2.query(Reschedule).one()
        assert r.kind == "client" and s2.get(Order, o.id).slot == "evening"
    assert "Клиент сам перенёс" in client.notified[0]


def test_page_closed_and_unknown(s, client):
    o = make_order(s)
    sv.set_status(o, "done", "cash")
    s.commit()
    assert client.get(f"/api/public/resched/{o.token}").json()["mode"] == "closed"
    assert client.post(f"/api/public/resched/{o.token}", json={"date": "x"}).status_code == 400
    assert client.get("/api/public/resched/nope").status_code == 404


def test_client_messages_have_move_link(s, morning):
    from app import notify
    o = make_order(s, days=1)
    assert f"https://kartoshka.example/r/{o.token}" in notify.message_text(o, "accepted")
    o.slot = "evening"
    assert "вечером 18:00–21:00" in notify.message_text(o, "eta")


# ---------- маршрут: дневной и вечерний рейсы ----------


def test_evening_trip_is_planned_separately(s, monkeypatch):
    async def geo(query):
        return {"Город, Ленина 5": (56.0, 60.01), "Город, Мира 1": (56.0, 60.02)}.get(query)

    async def matrix(points):
        steps = [[round(abs(a[1] - b[1]) / 0.01) for b in points] for a in points]
        return [[x * 600 for x in row] for row in steps], [[x * 1000 for x in row] for row in steps]
    monkeypatch.setattr(settings, "city", "Город")
    monkeypatch.setattr(settings, "depot_address", "")
    monkeypatch.setattr(routing, "_plans", {})
    monkeypatch.setattr(routing, "fetch_geocode", geo)
    monkeypatch.setattr(routing, "fetch_matrix", matrix)
    a = make_order(s, days=1)
    p = s.query(Product).first()
    c2, _ = sv.upsert_client(s, dict(name="Олег", phone="89005556677", address="Мира 1"))
    b = sv.create_order(s, c2, a.date, [dict(product_id=p.id, qty=50)])
    b.slot = "evening"
    s.commit()
    plan, route = asyncio.run(routing.get_plan(s, a.date))
    assert [o.id for o in route] == [a.id, b.id] and (a.route_pos, b.route_pos) == (1, 2)
    assert plan.stops == 1 and plan.evening.stops == 1 and plan.evening.departure == "18:00"
    assert b.eta_from >= "18:00" and a.eta_from < "12:00"


# ---------- бот: мастер переноса, напоминание, доступ ----------


@pytest.fixture
def bot(monkeypatch):
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
    monkeypatch.setattr(Bot, "__call__", fake_call)
    b = Bot("42:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    b.calls = calls

    def press(uid, data):
        asyncio.run(dispatcher().feed_update(b, Update.model_validate(_callback(uid, data), context={"bot": b})))
    b.press = press
    return b


def test_wizard_sends_offers(s, bot, morning, monkeypatch):
    from app import bot as tgbot
    sent_tg = []

    async def fake_tg(chat_id, text, reply_markup=None):
        sent_tg.append((chat_id, text, reply_markup))
    monkeypatch.setattr("app.notify.send_tg", fake_tg)
    s.add(User(tg_id=1, name="Артур", role="owner"))
    a = make_order(s, tg_chat_id=555)
    b = make_order(s, phone="89002223344")
    s.commit()
    bot.press(1, f"rs:day:{sv.today().isoformat()}")
    w = tgbot.WIZ[1]
    assert w["sel"] == {a.id, b.id} and "today_evening" in w["keys"]
    bot.press(1, "rs:go")                    # кого — всех
    bot.press(1, "rs:o:after_next")          # убрать «послезавтра»
    bot.press(1, "rs:go")
    bot.press(1, "rs:b:choice")              # подарок на выбор
    assert tgbot.WIZ[1]["step"] == "preview"
    bot.calls.clear()
    bot.press(1, "rs:send")
    assert 1 not in tgbot.WIZ
    with SessionLocal() as s2:
        rows = s2.query(Reschedule).all()
        assert len(rows) == 2 and all(r.status == "pending" for r in rows)
        assert all("after_next" not in str(r.options) for r in rows)
        assert s2.get(Order, a.id).slot == "evening"  # первый вариант — сегодня вечером
    assert len(sent_tg) == 1 and sent_tg[0][0] == 555 and sent_tg[0][2] is not None  # кнопка «Выбрать время»
    texts = [m.text for m in bot.calls if isinstance(m, SendMessage)]
    assert any("Ушло автоматически" in t for t in texts)
    manual = [m for m in bot.calls if isinstance(m, SendMessage) and "89002223344"[-2:] in (m.text or "")]
    assert manual and "wa.me/79002223344" in manual[0].reply_markup.inline_keyboard[0][0].url


def test_reminder_and_staff_pick(s, bot, morning, monkeypatch):
    from app import bot as tgbot
    s.add(User(tg_id=1, name="Артур", role="owner"))
    o = make_order(s)
    r = rs.offer(s, o, ["today_evening", "next_same"], "choice")
    s.commit()
    asyncio.run(tgbot.remind_reschedules(bot))
    assert not [m for m in bot.calls if isinstance(m, SendMessage)]  # ещё рано
    r.created_at = datetime.now() - timedelta(minutes=settings.reschedule_remind_min + 1)
    s.commit()
    asyncio.run(tgbot.remind_reschedules(bot))
    msgs = [m for m in bot.calls if isinstance(m, SendMessage)]
    assert len(msgs) == 1 and "Не ответил" in msgs[0].text
    assert msgs[0].reply_markup.inline_keyboard[1][0].callback_data == f"rs:pick:{r.id}:1"
    bot.calls.clear()
    asyncio.run(tgbot.remind_reschedules(bot))
    assert not [m for m in bot.calls if isinstance(m, SendMessage)]  # напоминаем один раз
    bot.press(1, f"rs:pick:{r.id}:1")
    with SessionLocal() as s2:
        got = s2.get(Order, o.id)
        assert got.date == sv.today() + timedelta(days=1) and got.bonus == "carrot"
        assert s2.get(Reschedule, r.id).status == "answered"


def test_reschedule_buttons_are_staff_only(s, bot, morning):
    s.add(User(tg_id=1, name="Артур", role="owner"))
    o = make_order(s, tg_chat_id=555)
    r = rs.offer(s, o, ["next_same"], "none")
    s.commit()
    for data in (f"rs:day:{sv.today().isoformat()}", f"rs:one:{o.id}", f"rs:pick:{r.id}:0", "rs:send", "rs:go"):
        for uid in (555, 777):
            bot.calls.clear()
            bot.press(uid, data)
            assert all(isinstance(m, AnswerCallbackQuery) and not m.text for m in bot.calls), (data, bot.calls)
    with SessionLocal() as s2:
        assert s2.get(Reschedule, r.id).status == "pending"
