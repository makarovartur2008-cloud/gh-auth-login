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
    sack_kg: int = 25

    # Только для разработки: пускать в API без Telegram
    dev_user_tg_id: int = 0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


settings = Settings()
