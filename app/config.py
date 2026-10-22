from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    # Адрес приложения (https), например https://1-2-3-4.sslip.io
    webapp_url: str = ""

    # ИИ: любой OpenAI-совместимый сервис (по умолчанию ProxyAPI — оплата в рублях)
    ai_api_key: str = ""
    ai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    ai_model: str = "gpt-4o-mini"
    ai_stt_model: str = "whisper-1"

    tz: str = "Europe/Moscow"
    db_path: str = "data/app.db"

    morning_hour: int = 8
    evening_hour: int = 20
    friend_fee: int = 50
    # Телефон на листовке и странице заказа (необязательно)
    contact_phone: str = ""
    sack_kg: int = 25

    # --- Маршрут и время доставки ---
    # Город — подставляется к адресам при поиске на карте (например «Екатеринбург»)
    city: str = ""
    # Откуда выезжаешь утром (адрес склада/дома)
    depot_address: str = ""
    route_start: str = "10:00"  # во сколько выезд
    service_min: int = 10  # минут на одну точку (поднять мешки, рассчитаться)
    eta_window_min: int = 60  # ширина окна, которое называем клиенту
    # у бесплатных карт нет пробок — время в пути умножаем на этот коэффициент
    traffic_factor: float = 1.3

    # --- Сообщения клиентам ---
    max_bot_token: str = ""  # бот в мессенджере MAX (dev.max.ru)
    sms_ru_api_id: str = ""  # SMS через sms.ru (платно, по желанию)

    # Только для разработки: пускать в API без Telegram
    dev_user_tg_id: int = 0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


settings = Settings()
