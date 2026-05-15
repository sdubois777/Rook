from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: str

    # Yahoo Fantasy API — all optional until Stage 10
    yahoo_client_id: Optional[str] = None
    yahoo_client_secret: Optional[str] = None
    yahoo_redirect_uri: Optional[str] = None
    yahoo_league_id: Optional[str] = None
    yahoo_refresh_token: Optional[str] = None

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/fantasy_football"

    # App
    secret_key: str
    environment: str = "development"

    # Optional
    rapidapi_key: Optional[str] = None

    # Platform token encryption
    platform_token_encryption_key: str = ""

    # App URL (for ESPN bookmarklet redirect)
    app_url: str = "http://localhost:8000"

    # In-season data APIs (Stage 20+)
    the_odds_api_key: Optional[str] = None
    openweathermap_api_key: Optional[str] = None

    # Clerk authentication
    clerk_secret_key: Optional[str] = None
    vite_clerk_publishable_key: Optional[str] = None
    clerk_webhook_secret: Optional[str] = None

    @property
    def clerk_enabled(self) -> bool:
        """True when Clerk is configured."""
        return bool(self.clerk_secret_key)


settings = Settings()
