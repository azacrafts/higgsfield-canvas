from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str
    groq_api_key: str
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    canvas_agent_model: str = "claude-sonnet-4-6"
    chat_agent_model: str = "claude-sonnet-4-6"
    classifier_model: str = "claude-haiku-4-5-20251001"

    command_flush_delay_s: float = 3.5
    db_save_debounce_s: float = 2.0
    agent_max_turns: int = 8
    context_debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
