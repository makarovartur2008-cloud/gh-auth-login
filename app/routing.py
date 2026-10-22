"""Маршрут на день и примерное время доставки на каждый адрес.

Публичный интерфейс:
  plan_day(s, day) -> list[Order] — упорядочивает открытые заказы дня, заполняет
  order.route_pos / order.eta_from / order.eta_to и делает commit.
  last_plan(day) -> Plan | None — итоги последнего расчёта (выезд, км, время, ссылка на карту).

Адреса ищем в OpenStreetMap (Nominatim), время в пути — OSRM (без пробок, поэтому
умножаем на traffic_factor). Если OSRM недоступен — по прямой × 1.4 на 30 км/ч.
Сетевые вызовы — только fetch_geocode / fetch_matrix: в тестах их легко подменить.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import services as sv
from app.config import settings
from app.db import Client, Order

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/table/v1/driving/"
USER_AGENT = "kartoshka-delivery-bot/1.0 (+https://github.com/makarovartur2008-cloud/gh-auth-login)"

ROAD_FACTOR = 1.4  # дорога длиннее прямой примерно в 1.4 раза
FALLBACK_KMH = 30  # средняя скорость по городу, если OSRM не ответил
SNAP_MIN = 15  # окно времени привязываем к четвертям часа

Point = tuple[float, float]  # (lat, lon)


# ---------- сеть (подменяется в тестах) ----------

_last_geocode = 0.0
_geocode_lock: asyncio.Lock | None = None
_geocode_loop = None


def _lock() -> asyncio.Lock:
    # замок на текущий event loop (в тестах loop'ы разные)
    global _geocode_lock, _geocode_loop
    loop = asyncio.get_running_loop()
    if _geocode_lock is None or _geocode_loop is not loop:
        _geocode_lock, _geocode_loop = asyncio.Lock(), loop
    return _geocode_lock


async def fetch_geocode(query: str) -> Point | None:
    """Nominatim: адрес -> (lat, lon); None — не нашёл. Ошибка сети — исключение.
    Правила Nominatim: свой User-Agent и не чаще 1 запроса в секунду."""
    global _last_geocode
    async with _lock():
        wait = 1.0 - (time.monotonic() - _last_geocode)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as c:
                r = await c.get(NOMINATIM_URL, params={
                    "q": query, "format": "json", "limit": 1, "countrycodes": "ru", "accept-language": "ru"})
        finally:
            _last_geocode = time.monotonic()
        r.raise_for_status()
        data = r.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


async def fetch_matrix(points: list[Point]) -> tuple[list[list], list[list]]:
    """OSRM table: (durations в секундах, distances в метрах). Ошибка — исключение."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as c:
        r = await c.get(OSRM_URL + coords, params={"annotations": "duration,distance"})
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {data.get('code')}")
    return data["durations"], data["distances"]


# ---------- геокодирование ----------

_depot_cache: dict[str, Point | None] = {}


def geo_query(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return ""
    city = settings.city.strip()
    return f"{city}, {address}" if city else address


async def geocode_client(c: Client) -> Point | None:
    """Координаты клиента; спрашиваем карту только если адрес поменялся."""
    q = geo_query(c.address)
    if not q:
        c.lat = c.lon = None
        c.geo_address = ""
        return None
    if c.geo_address == q:
        return (c.lat, c.lon) if c.lat is not None and c.lon is not None else None
    try:
        p = await fetch_geocode(q)
    except Exception:
        # сеть упала — не запоминаем, попробуем в следующий раз
        log.warning("геокодирование не удалось: %s", q, exc_info=True)
        return None
    c.lat, c.lon = p if p else (None, None)
    c.geo_address = q
    return p


async def geocode_depot() -> Point | None:
    q = geo_query(settings.depot_address)
    if not q:
        return None
    if q not in _depot_cache:
        try:
            _depot_cache[q] = await fetch_geocode(q)
        except Exception:
            log.warning("не нашёл точку выезда: %s", q, exc_info=True)
            return None
    return _depot_cache[q]


# ---------- время в пути ----------


def haversine_km(a: Point, b: Point) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def fallback_leg(a: Point, b: Point) -> tuple[float, float]:
    """По прямой × 1.4 на 30 км/ч: (секунды, метры)."""
    km = haversine_km(a, b) * ROAD_FACTOR
    return km / FALLBACK_KMH * 3600, km * 1000


async def travel_matrix(points: list[Point]) -> tuple[list[list[float]], list[list[float]], bool]:
    """(секунды с учётом пробок, метры, считал ли OSRM). Пустые клетки OSRM — по прямой."""
    n = len(points)
    dur = [[0.0] * n for _ in range(n)]
    dist = [[0.0] * n for _ in range(n)]
    if n < 2:
        return dur, dist, False
    try:
        od, odist = await fetch_matrix(points)
        osrm = True
    except Exception:
        log.warning("OSRM недоступен, считаю по прямой", exc_info=True)
        od, odist, osrm = None, None, False
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d = od[i][j] if od else None
            m = odist[i][j] if odist else None
            if d is None or m is None:
                dur[i][j], dist[i][j] = fallback_leg(points[i], points[j])
            else:
                # у OSRM нет пробок — накидываем коэффициент
                dur[i][j], dist[i][j] = float(d) * settings.traffic_factor, float(m)
    return dur, dist, osrm


# ---------- порядок объезда ----------


def path_cost(m: list[list[float]], path: list[int]) -> float:
    return sum(m[a][b] for a, b in zip(path, path[1:]))


def nearest_neighbour(m: list[list[float]], start: int) -> list[int]:
    path, left = [start], set(range(len(m))) - {start}
    while left:
        cur = path[-1]
        nxt = min(left, key=lambda j: (m[cur][j], j))
        path.append(nxt)
        left.remove(nxt)
    return path


def two_opt(m: list[list[float]], path: list[int], fixed_start: bool = True) -> list[int]:
    """Разворачиваем куски пути, пока становится короче. Путь открытый (без возврата)."""
    best, best_cost = path[:], path_cost(m, path)
    first = 1 if fixed_start else 0
    improved = True
    while improved:
        improved = False
        for i in range(first, len(best) - 1):
            for j in range(i + 1, len(best)):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                c = path_cost(m, cand)
                if c < best_cost - 1e-9:
                    best, best_cost, improved = cand, c, True
    return best


def order_stops(m: list[list[float]], start: int | None = 0) -> list[int]:
    """Порядок всех точек матрицы. start — откуда выезжаем (склад); None — с любой точки."""
    if not m:
        return []
    if start is not None:
        return two_opt(m, nearest_neighbour(m, start))
    # без склада: пробуем начать с каждой точки и берём самый короткий путь
    paths = [two_opt(m, nearest_neighbour(m, i), fixed_start=False) for i in range(len(m))]
    return min(paths, key=lambda p: (path_cost(m, p), p))


# ---------- время прибытия ----------


def parse_hhmm(t: str) -> tuple[int, int]:
    hh, mm = (t or "10:00").split(":")
    return int(hh), int(mm)


def departure_time(day: date, now: datetime | None = None) -> datetime:
    """Выезд в route_start; если считаем на сегодня и уже позже — от текущего времени."""
    hh, mm = parse_hhmm(settings.route_start)
    dep = datetime(day.year, day.month, day.day, hh, mm, tzinfo=settings.zone)
    now = now or datetime.now(settings.zone)
    if day == now.date() and now > dep:
        dep = now.replace(second=0, microsecond=0)
    return dep


def eta_window(arrival: datetime, width_min: int | None = None) -> tuple[str, str]:
    """Окно для клиента. Правило: начало = прибытие − половина ширины, округлённое
    до ближайшей четверти часа (ровно посередине — вверх); конец = начало + ширина.
    Пример: прибытие 14:20, ширина 60 → 13:50 → 13:45–14:45."""
    width = settings.eta_window_min if width_min is None else width_min
    start = arrival - timedelta(minutes=width / 2)
    mins = start.hour * 60 + start.minute + start.second / 60
    snapped = math.floor(mins / SNAP_MIN + 0.5) * SNAP_MIN
    base = start.replace(hour=0, minute=0, second=0, microsecond=0)
    frm = base + timedelta(minutes=snapped)
    to = frm + timedelta(minutes=width)
    return frm.strftime("%H:%M"), to.strftime("%H:%M")


# ---------- план дня ----------


@dataclass
class Plan:
    day: date
    departure: str = ""
    total_km: float = 0
    total_min: int = 0
    depot: Point | None = None
    waypoints: list[Point] = field(default_factory=list)  # склад (если есть) + найденные адреса по порядку
    order_ids: list[int] = field(default_factory=list)  # все открытые заказы в порядке маршрута
    osrm: bool = False
    key: tuple = ()

    @property
    def yandex_url(self) -> str:
        if not self.waypoints:
            return ""
        return "https://yandex.ru/maps/?rtext=" + "~".join(f"{a:.6f},{b:.6f}" for a, b in self.waypoints) + "&rtt=auto"


_plans: dict[date, Plan] = {}


def plan_key(orders: list[Order]) -> tuple:
    """Что должно совпасть, чтобы старый расчёт ещё годился."""
    return (settings.depot_address, settings.city, settings.route_start,
            tuple((o.id, o.client.address) for o in sorted(orders, key=lambda o: o.id)))


def last_plan(day: date) -> Plan | None:
    return _plans.get(day)


def open_orders(s: Session, day: date) -> list[Order]:
    return [o for o in sv.orders_on(s, day) if o.status == "new"]


async def plan_day(s: Session, day: date) -> list[Order]:
    orders = open_orders(s, day)
    plan = Plan(day=day, key=plan_key(orders))
    found: list[tuple[Order, Point]] = []
    lost: list[Order] = []
    seen: dict[int, Point | None] = {}
    for o in orders:
        if o.client_id not in seen:
            seen[o.client_id] = await geocode_client(o.client)
        if seen[o.client_id]:
            found.append((o, seen[o.client_id]))
        else:
            lost.append(o)

    # точки матрицы: [склад] + найденные адреса; shift — сдвиг индексов из-за склада
    depot = await geocode_depot() if found else None
    shift = 1 if depot else 0
    points = ([depot] if depot else []) + [p for _, p in found]
    dur, dist, plan.osrm = await travel_matrix(points)
    nodes = order_stops(dur, 0 if depot else None)[shift:]

    dep = departure_time(day)
    plan.departure = dep.strftime("%H:%M")
    plan.depot = depot
    # прибытие на точку = выезд + дорога + service_min на каждой предыдущей точке
    t, travel, meters = dep, 0.0, 0.0
    prev = 0 if depot else None
    route: list[Order] = []
    for node in nodes:
        if prev is not None:
            t += timedelta(seconds=dur[prev][node])
            travel += dur[prev][node]
            meters += dist[prev][node]
        o = found[node - shift][0]
        o.eta_from, o.eta_to = eta_window(t)
        route.append(o)
        t += timedelta(minutes=settings.service_min)
        prev = node
    # ненайденные адреса — в конец, без времени
    for o in lost:
        o.eta_from = o.eta_to = ""
        route.append(o)
    for pos, o in enumerate(route, 1):
        o.route_pos = pos

    plan.total_km = round(meters / 1000, 1)
    plan.total_min = round(travel / 60 + settings.service_min * len(found))
    plan.waypoints = ([depot] if depot else []) + [points[n] for n in nodes]
    plan.order_ids = [o.id for o in route]
    s.commit()
    _plans[day] = plan
    return route


async def get_plan(s: Session, day: date, force: bool = False) -> tuple[Plan, list[Order]]:
    """Последний расчёт, если заказы и адреса не менялись; иначе считаем заново."""
    plan = _plans.get(day)
    orders = open_orders(s, day)
    if force or not plan or plan.key != plan_key(orders):
        route = await plan_day(s, day)
        return _plans[day], route
    by_id = {o.id: o for o in orders}
    return plan, [by_id[i] for i in plan.order_ids if i in by_id]
