import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import api
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
    polling = None
    if settings.bot_token:
        from app.bot import broadcast_day, make_bot

        bot, dp = make_bot()
        scheduler.add_job(broadcast_day, "cron", hour=settings.morning_hour, minute=0, args=[bot, 0])
        scheduler.add_job(broadcast_day, "cron", hour=settings.evening_hour, minute=0, args=[bot, 1])
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, работает только приложение")
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    if polling:
        polling.cancel()


app = FastAPI(title="Картошка", lifespan=lifespan)
app.include_router(api.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True}
