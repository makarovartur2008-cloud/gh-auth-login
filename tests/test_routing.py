import asyncio
import itertools
import random
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import routing
from app import services as sv
from app.config import settings
from app.db import Order, Product, SessionLocal, User

DEPOT = (56.0, 60.0)


def pt(k: int) -> tuple[float, float]:
    """Точки на одной линии: k-я — в k «кварталах» от склада."""
    return 56.0, 60.0 + k * 0.01


class FakeNet:
    """Подмена Nominatim и OSRM: никакой сети в тестах."""

    def __init__(self, places: dict[str, tuple[float, float]]):
        self.places = places
        self.geocode_calls: list[str] = []
        self.matrix_calls = 0
        self.osrm_down = False

    async def geocode(self, query):
        self.geocode_calls.append(query)
        return self.places.get(query)

    async def matrix(self, points):
        self.matrix_calls += 1
        if self.osrm_down:
            raise RuntimeError("OSRM timeout")
        # 10 минут и 1 км на каждый «квартал» по линии
        steps = [[round(abs(a[1] - b[1]) / 0.01) for b in points] for a in points]
        return [[s * 600 for s in row] for row in steps], [[s * 1000 for s in row] for row in steps]


@pytest.fixture
def net(monkeypatch):
    monkeypatch.setattr(settings, "city", "Город")
    monkeypatch.setattr(settings, "depot_address", "Склад 1")
    monkeypatch.setattr(settings, "route_start", "10:00")
    monkeypatch.setattr(settings, "service_min", 10)
    monkeypatch.setattr(settings, "eta_window_min", 60)
    monkeypatch.setattr(settings, "traffic_factor", 1.3)
    monkeypatch.setattr(routing, "_plans", {})
    monkeypatch.setattr(routing, "_depot_cache", {})
    fake = FakeNet({"Город, Склад 1": DEPOT, "Город, Первая 1": pt(1), "Город, Вторая 2": pt(2),
                    "Город, Третья 3": pt(3)})
    monkeypatch.setattr(routing, "fetch_geocode", fake.geocode)
    monkeypatch.setattr(routing, "fetch_matrix", fake.matrix)
    return fake


def potato(s):
    return s.query(Product).filter_by(name="Картошка белая").one()


def add_order(s, name, address, day):
    c, _ = sv.upsert_client(s, {"name": name, "address": address})
    o = sv.create_order(s, c, day, [{"product_id": potato(s).id, "qty": 50}])
    s.commit()
    return o


# ---------- порядок объезда ----------


def test_route_order_on_toy_matrix():
    # склад 0, точки на линии: 1 — дальше всех, 2 — ближе всех
    pos = [0, 30, 10, 20]
    m = [[abs(a - b) for b in pos] for a in pos]
    assert routing.order_stops(m, 0) == [0, 2, 3, 1]
    # без склада — тоже по линии, с края
    stops = [30, 10, 20]
    assert routing.order_stops([[abs(a - b) for b in stops] for a in stops], None) in ([1, 2, 0], [0, 2, 1])


def test_two_opt_never_worse_and_close_to_optimum():
    rnd = random.Random(7)
    for _ in range(20):
        pts = [(rnd.random(), rnd.random()) for _ in range(7)]
        m = [[((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** .5 for b in pts] for a in pts]
        nn = routing.nearest_neighbour(m, 0)
        best = routing.order_stops(m, 0)
        assert best[0] == 0 and sorted(best) == list(range(7))
        assert routing.path_cost(m, best) <= routing.path_cost(m, nn) + 1e-9
        opt = min(routing.path_cost(m, [0, *p]) for p in itertools.permutations(range(1, 7)))
        assert routing.path_cost(m, best) <= opt * 1.15
    # детерминированно
    assert routing.order_stops(m, 0) == routing.order_stops(m, 0)


# ---------- окно времени ----------


def at(hh, mm):
    return datetime(2026, 10, 23, hh, mm, tzinfo=settings.zone)


def test_eta_window_snaps_to_quarters(monkeypatch):
    monkeypatch.setattr(settings, "eta_window_min", 60)
    assert routing.eta_window(at(14, 20)) == ("13:45", "14:45")
    assert routing.eta_window(at(14, 0)) == ("13:30", "14:30")
    assert routing.eta_window(at(14, 7)) == ("13:30", "14:30")
    assert routing.eta_window(at(14, 8)) == ("13:45", "14:45")  # 13:38 ближе к 13:45
    assert routing.eta_window(at(10, 7), 90) == ("09:15", "10:45")


def test_eta_window_never_before_departure(monkeypatch):
    monkeypatch.setattr(settings, "eta_window_min", 60)
    # выезд 10:00, прибытие 10:13 — было бы 09:45–10:45
    assert routing.eta_window(at(10, 13), not_before=at(10, 0)) == ("10:00", "11:00")
    # выезд «сейчас» 12:34: ближайшая четверть 12:45 не позже прибытия 12:50
    assert routing.eta_window(at(12, 50), not_before=at(12, 34)) == ("12:45", "13:45")
    # прибытие 12:40 раньше 12:45 — окно с самого выезда
    assert routing.eta_window(at(12, 40), not_before=at(12, 34)) == ("12:34", "13:34")
    # далёкие точки не трогаем
    assert routing.eta_window(at(14, 20), not_before=at(10, 0)) == ("13:45", "14:45")


def test_departure_time(monkeypatch):
    monkeypatch.setattr(settings, "route_start", "10:00")
    day = date(2026, 10, 23)
    assert routing.departure_time(day, now=at(8, 0)).strftime("%H:%M") == "10:00"
    # сегодня уже позже выезда — считаем от текущего времени
    assert routing.departure_time(day, now=at(12, 34)).strftime("%H:%M") == "12:34"
    # завтрашний день — всегда route_start
    assert routing.departure_time(day + timedelta(days=1), now=at(12, 34)).strftime("%H:%M") == "10:00"


# ---------- время в пути ----------


def test_osrm_applies_traffic_factor(net):
    dur, dist, osrm = asyncio.run(routing.travel_matrix([pt(0), pt(2)]))
    assert osrm and dur[0][1] == pytest.approx(1200 * 1.3) and dist[0][1] == 2000


def test_osrm_failure_falls_back_to_haversine(net):
    net.osrm_down = True
    a, b = (56.0, 60.0), (56.009, 60.0)  # ~1 км по прямой
    dur, dist, osrm = asyncio.run(routing.travel_matrix([a, b]))
    km = routing.haversine_km(a, b)
    assert not osrm and km == pytest.approx(1.0, abs=0.01)
    assert dist[0][1] == pytest.approx(km * 1.4 * 1000)
    assert dur[0][1] == pytest.approx(km * 1.4 / 30 * 3600)


# ---------- план дня ----------


def test_plan_day_sets_fields_and_commits(net):
    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        o3 = add_order(s, "Тройка", "Третья 3", day)
        lost = add_order(s, "Потеряшка", "Нет такой 99", day)
        o1 = add_order(s, "Единица", "Первая 1", day)
        o2 = add_order(s, "Двойка", "Вторая 2", day)
        done = add_order(s, "Готово", "Первая 1", day)
        sv.set_status(done, "done", "cash")
        s.commit()
        route = asyncio.run(routing.plan_day(s, day))
        assert [o.id for o in route] == [o1.id, o2.id, o3.id, lost.id]

    with SessionLocal() as s:  # всё сохранено в базе
        got = {o.id: o for o in s.scalars(sv.order_query().where(Order.date == day))}
        assert [got[i].route_pos for i in (o1.id, o2.id, o3.id, lost.id)] == [1, 2, 3, 4]
        # выезд 10:00; 13 мин дороги (10 × 1.3) до каждой точки + 10 мин на точке
        # прибытие 10:13, 10:36, 10:59 → окна от «прибытие − 30 мин» по четвертям часа,
        # но не раньше выезда (у первой точки было бы 09:45)
        assert (got[o1.id].eta_from, got[o1.id].eta_to) == ("10:00", "11:00")
        assert (got[o2.id].eta_from, got[o2.id].eta_to) == ("10:00", "11:00")
        assert (got[o3.id].eta_from, got[o3.id].eta_to) == ("10:30", "11:30")
        # ненайденный адрес — в конце и без времени
        assert got[lost.id].eta_from == "" and got[lost.id].client.lat is None
        assert got[done.id].route_pos is None
        assert got[o1.id].client.geo_address == "Город, Первая 1"
        assert "🕑 10:00–11:00" in sv.order_text(got[o1.id])
        assert "🕑" not in sv.order_text(got[lost.id])

    plan = routing.last_plan(day)
    assert plan.departure == "10:00" and plan.total_km == 3.0
    assert plan.total_min == 3 * 13 + 3 * 10
    assert plan.yandex_url == ("https://yandex.ru/maps/?rtext=56.000000,60.000000~56.000000,60.010000~"
                               "56.000000,60.020000~56.000000,60.030000&rtt=auto")


def test_plan_without_depot_starts_from_a_stop(net, monkeypatch):
    monkeypatch.setattr(settings, "depot_address", "")
    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        add_order(s, "Тройка", "Третья 3", day)
        add_order(s, "Единица", "Первая 1", day)
        add_order(s, "Двойка", "Вторая 2", day)
        route = asyncio.run(routing.plan_day(s, day))
        names = [o.client.name for o in route]
        assert names in (["Единица", "Двойка", "Тройка"], ["Тройка", "Двойка", "Единица"])
        assert (route[0].eta_from, route[0].eta_to) == ("10:00", "11:00")  # первая точка — сразу в 10:00
    assert routing.last_plan(day).total_km == 2.0


def test_geocode_cache(net):
    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        o = add_order(s, "Единица", "Первая 1", day)
        add_order(s, "Потеряшка", "Нет такой 99", day)
        asyncio.run(routing.plan_day(s, day))
        assert sorted(net.geocode_calls) == ["Город, Нет такой 99", "Город, Первая 1", "Город, Склад 1"]
        net.geocode_calls.clear()
        asyncio.run(routing.plan_day(s, day))
        assert net.geocode_calls == []  # адреса не менялись — карту не спрашиваем (и «не найден» тоже помним)
        o.client.address = "Вторая 2"
        s.commit()
        asyncio.run(routing.plan_day(s, day))
        assert net.geocode_calls == ["Город, Вторая 2"]
        assert (o.client.lat, o.client.lon) == pt(2)


def test_geocode_network_error_is_not_cached(net, monkeypatch):
    async def boom(q):
        raise RuntimeError("timeout")

    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        o = add_order(s, "Единица", "Первая 1", day)
        monkeypatch.setattr(routing, "fetch_geocode", boom)
        route = asyncio.run(routing.plan_day(s, day))
        assert route[0].eta_from == "" and o.client.geo_address == ""
        monkeypatch.setattr(routing, "fetch_geocode", net.geocode)
        route = asyncio.run(routing.plan_day(s, day))
        assert route[0].eta_from and o.client.lat is not None


def test_send_day_lists_route(net):
    from app import bot

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        add_order(s, "Двойка", "Вторая 2", day)
        add_order(s, "Единица", "Первая 1", day)
        add_order(s, "Потеряшка", "Нет такой 99", day)
    fb = FakeBot()
    asyncio.run(bot.send_day(fb, 1, 1))
    head, kb = fb.sent[0]
    assert "выезд 10:00" in head and "≈ 2 км" in head and "yandex.ru/maps/?rtext=56.000000,60.000000~" in head
    assert kb.inline_keyboard[0][0].callback_data == f"route:{day.isoformat()}"
    texts = [t for t, _ in fb.sent[1:]]
    assert "Точка 1 из 3" in texts[0] and "Единица" in texts[0] and "🕑" in texts[0]
    assert "Точка 3 из 3" in texts[2] and "адрес не найден на карте — проверь" in texts[2]


# ---------- API ----------


@pytest.fixture
def client(monkeypatch, net):
    monkeypatch.setattr(settings, "dev_user_tg_id", 111)
    with SessionLocal() as s:
        s.add(User(tg_id=111, name="Артур", role="owner"))
        s.commit()
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_route_api(client, net):
    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        o2 = add_order(s, "Двойка", "Вторая 2", day)
        o1 = add_order(s, "Единица", "Первая 1", day)
        lost = add_order(s, "Потеряшка", "Нет такой 99", day)
    r = client.get(f"/api/route?day={day.isoformat()}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["departure"] == "10:00" and data["total_km"] == 2.0 and data["yandex_url"].startswith("https://yandex.ru")
    assert [st["order_id"] for st in data["stops"]] == [o1.id, o2.id, lost.id]
    first = data["stops"][0]
    assert first["pos"] == 1 and first["eta_from"] == "10:00" and first["geocoded"] is True
    assert first["name"] == "Единица" and first["lat"] == 56.0
    assert data["stops"][2]["geocoded"] is False and data["stops"][2]["eta_from"] == ""

    # повторный GET без изменений — из кэша, без OSRM
    calls = net.matrix_calls
    assert client.get(f"/api/route?day={day.isoformat()}").json() == data
    assert net.matrix_calls == calls
    # POST — пересчёт
    r = client.post(f"/api/route/plan?day={day.isoformat()}")
    assert r.status_code == 200 and net.matrix_calls == calls + 1
    assert r.json()["stops"] == data["stops"]
    # пустой день
    empty = client.get(f"/api/route?day={(day + timedelta(days=5)).isoformat()}").json()
    assert empty["stops"] == [] and empty["yandex_url"] == ""


def test_send_day_reuses_plan_and_matches_evening_eta(net, monkeypatch):
    """Рассылка всем сотрудникам и вечернее сообщение клиентам — один расчёт, одно и то же время."""
    from app import bot, notify

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            self.sent.append((chat_id, text))

    sent_to_clients = []

    async def fake_tg(chat_id, text):
        sent_to_clients.append(text)
    monkeypatch.setattr(notify, "send_tg", fake_tg)

    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        s.add_all([User(tg_id=1, name="Артур", role="owner"), User(tg_id=2, name="Друг", role="member")])
        o1 = add_order(s, "Единица", "Первая 1", day)
        o1.client.tg_chat_id = 555
        add_order(s, "Двойка", "Вторая 2", day)
        s.commit()
    fb = FakeBot()
    asyncio.run(bot.broadcast_day(fb, 1))
    assert net.matrix_calls == 1  # второму сотруднику — тот же расчёт
    a = [t for c, t in fb.sent if c == 1]
    b = [t for c, t in fb.sent if c == 2]
    assert a == b and "🕑 10:00–11:00" in a[1]

    # кэш «устарел» по времени, но заказы те же — вечерняя рассылка не пересчитывает
    assert asyncio.run(notify.evening_eta(SessionLocal)) == 1
    assert net.matrix_calls == 1
    assert "примерно 10:00–11:00" in sent_to_clients[0]

    # кнопка «Пересчитать» — считает заново
    asyncio.run(bot.send_day(fb, 1, 1, force_route=True))
    assert net.matrix_calls == 2
    # изменился состав заказов — пересчёт сам
    with SessionLocal() as s:
        add_order(s, "Тройка", "Третья 3", day)
    asyncio.run(bot.send_day(fb, 1, 1))
    assert net.matrix_calls == 3


def test_moving_order_resets_eta(client, net):
    day = sv.today() + timedelta(days=1)
    with SessionLocal() as s:
        o = add_order(s, "Единица", "Первая 1", day)
        o.eta_from, o.eta_to, o.route_pos, o.eta_notified = "10:00", "11:00", 1, True
        s.commit()
    r = client.patch(f"/api/orders/{o.id}", json={"date": (day + timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        got = s.get(Order, o.id)
        assert got.date == day + timedelta(days=1)
        assert not got.eta_notified and got.eta_from == "" and got.route_pos is None
        # та же дата — ничего не сбрасываем
        got.eta_from, got.eta_notified = "12:00", True
        s.commit()
    client.patch(f"/api/orders/{o.id}", json={"date": (day + timedelta(days=1)).isoformat(), "notes": "x"})
    with SessionLocal() as s:
        assert s.get(Order, o.id).eta_notified and s.get(Order, o.id).eta_from == "12:00"
