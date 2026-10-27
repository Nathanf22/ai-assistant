"""Configuration lue depuis les variables d'environnement (ou un fichier .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    anthropic_api_key: str = ""

    # Modèle utilisé par l'agent conversationnel.
    llm_model: str = "claude-sonnet-5-5"
    llm_max_tokens: int = 1024
    llm_max_tool_rounds: int = 6
    # Tarifs (USD par million de tokens) pour estimer le coût dans les métriques.
    # Laisser à 0 si inconnus : seuls les volumes de tokens seront affichés.
    llm_input_usd_per_mtok: float = 0.0
    llm_output_usd_per_mtok: float = 0.0
    llm_cache_read_usd_per_mtok: float = 0.0
    llm_cache_write_usd_per_mtok: float = 0.0

    database_url: str = "sqlite+aiosqlite:///./coach.db"
    timezone: str = "Europe/Paris"

    # Identifiants Telegram autorisés à utiliser /stats (séparés par des virgules).
    admin_telegram_ids: str = ""

    # Pilote : si non vide, seuls ces codes d'invitation permettent de s'inscrire.
    pilot_invite_codes: str = ""

    # Délai (minutes) après la fin prévue d'une séance avant de demander un retour.
    feedback_delay_minutes: int = 45

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_telegram_ids.split(",") if x.strip()}

    @property
    def invite_codes(self) -> set[str]:
        return {x.strip().upper() for x in self.pilot_invite_codes.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
