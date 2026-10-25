import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import ai, api, notify, public
from app import services as sv
from app.config import settings
from app.db import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


def recurring_job():
    with SessionLocal() as s:
        created = sv.generate_recurring(s)
    if created:
        log.info("Созданы заказы постоянных клиентов: %s", [o.id for o in created])


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = AsyncIOScheduler(timezone=settings.zone)
    scheduler.add_job(recurring_job, "interval", hours=1)
    recurring_job()
    await ai.choose_models()
    polling, max_polling = None, None
    # вечером: маршрут на завтра и примерное время каждому клиенту
    scheduler.add_job(notify.evening_eta, "cron", hour=settings.evening_hour, minute=5, args=[SessionLocal])
    if settings.bot_token:
        from app.bot import broadcast_day, make_bot

        bot, dp = make_bot()
        scheduler.add_job(broadcast_day, "cron", hour=settings.morning_hour, minute=0, args=[bot, 0])
        scheduler.add_job(broadcast_day, "cron", hour=settings.evening_hour, minute=0, args=[bot, 1])
        # кто не ответил на перенос доставки — напомнить (app/reschedule.py)
        from app.bot import remind_reschedules

        scheduler.add_job(remind_reschedules, "interval", minutes=10, args=[bot])
        try:
            notify.TG_BOT_USERNAME = (await bot.get_me()).username or ""
        except Exception:
            log.exception("Не удалось узнать username бота — ссылки-подписки в Telegram не будут показаны")
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, работает только приложение")
    if settings.max_bot_token:
        from app import max_bot

        max_polling = asyncio.create_task(max_bot.poll_forever())
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    for task in (polling, max_polling):
        if task:
            task.cancel()


app = FastAPI(title="Картошка", lifespan=lifespan)
app.include_router(api.router)
app.include_router(public.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True}
