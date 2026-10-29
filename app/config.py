from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    # Адрес приложения (https), например https://1-2-3-4.sslip.io или https://kartoshka-....amvera.io
    webapp_url: str = ""

    # ИИ: любой OpenAI-совместимый сервис (по умолчанию ProxyAPI — оплата в рублях)
    ai_api_key: str = ""
    ai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    ai_model: str = "gpt-4o-mini"
    ai_stt_model: str = "whisper-1"

    tz: str = "Europe/Samara"  # Самара: UTC+4, на час больше Москвы
    db_path: str = "data/app.db"

    morning_hour: int = 8
    evening_hour: int = 20
    backup_hour: int = 22  # в 22:30 бот присылает владельцу копию базы
    friend_fee: int = 50
    # Телефон на листовке и странице заказа
    contact_phone: str = "89083003917"
    sack_kg: int = 25

    # --- Маршрут и время доставки ---
    # Город — подставляется к адресам при поиске на карте (например «Екатеринбург»)
    city: str = "Самара"
    # Откуда выезжаешь утром (адрес склада/дома)
    depot_address: str = "Олимпийская улица, 68"
    route_start: str = "10:00"  # во сколько выезд
    service_min: int = 10  # минут на одну точку (поднять мешки, рассчитаться)
    eta_window_min: int = 60  # ширина окна, которое называем клиенту
    # у бесплатных карт нет пробок — время в пути умножаем на этот коэффициент
    traffic_factor: float = 1.3
    # вечерний рейс: окно, которое обещаем клиенту
    evening_start: str = "18:00"
    evening_end: str = "21:00"

    # --- Перенос доставки ---
    reschedule_remind_min: int = 120  # через сколько напомнить про тех, кто не ответил
    bonus_kg: float = 5  # сколько кг дарим за перенос

    # --- Сообщения клиентам ---
    # --- Отзывы (QR на обороте визитки ведёт на /otzyv) ---
    review_gift: str = "5 кг моркови"  # что дарим за отзыв к следующему заказу
    # если задать (например, ссылку на отзывы в Яндекс Картах), /otzyv будет вести туда — QR на визитках менять не нужно
    review_url: str = ""
    # профиль на Авито: на странице отзыва — кнопка «Оставить отзыв на Авито»
    avito_url: str = ""

    max_bot_token: str = ""  # бот в мессенджере MAX (dev.max.ru)
    sms_ru_api_id: str = ""  # SMS через sms.ru (платно, по желанию)

    # Только для разработки: пускать в API без Telegram
    dev_user_tg_id: int = 0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


settings = Settings()
