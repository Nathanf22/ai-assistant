"""Settings read from environment variables (or a .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    anthropic_api_key: str = ""

    # Model used by the conversational agent.
    llm_model: str = "claude-haiku-5-5"
    llm_max_tokens: int = 1024
    llm_max_tool_rounds: int = 6
    # Prices (USD per million tokens) used to estimate cost in the metrics.
    # Leave at 0 if unknown: only token volumes are reported.
    llm_input_usd_per_mtok: float = 0.0
    llm_output_usd_per_mtok: float = 0.0
    llm_cache_read_usd_per_mtok: float = 0.0
    llm_cache_write_usd_per_mtok: float = 0.0

    database_url: str = "sqlite+aiosqlite:///./coach.db"
    timezone: str = "Europe/Paris"

    # Telegram user ids allowed to use /stats (comma-separated).
    admin_telegram_ids: str = ""

    # Pilot: when set, only these invite codes allow signing up.
    pilot_invite_codes: str = ""

    # Delay (minutes) after a session's planned end before asking for feedback.
    feedback_delay_minutes: int = 45

    # Webhook mode (production on Vercel). Leave both empty locally, where the bot polls.
    # Secret Telegram echoes in the X-Telegram-Bot-Api-Secret-Token header of every update.
    telegram_webhook_secret: str = ""
    # Bearer token required to call /api/tick and /api/set-webhook (Vercel Cron sends it).
    cron_secret: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_telegram_ids.split(",") if x.strip()}

    @property
    def invite_codes(self) -> set[str]:
        return {x.strip().upper() for x in self.pilot_invite_codes.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
