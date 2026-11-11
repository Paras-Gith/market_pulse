from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_secret_key: SecretStr = SecretStr("")
    database_url: str = "sqlite:///./marketpulse.db"
    groq_api_key: SecretStr = SecretStr("")
    news_feeds: str = (
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms,"
        "https://www.livemint.com/rss/markets,"
        "https://www.thehindu.com/business/markets/feeder/default.rss"
    )
    base_url: str = "http://localhost:8000"
    paypal_mode: str = "sandbox"
    paypal_client_id: SecretStr = SecretStr("")
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_plan_id: str = ""
    paypal_webhook_id: str = ""

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")
