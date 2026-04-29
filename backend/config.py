from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: str

    # Yahoo Fantasy API
    yahoo_client_id: Optional[str] = None
    yahoo_client_secret: Optional[str] = None
    yahoo_redirect_uri: str = "http://localhost:8000/auth/yahoo/callback"
    yahoo_league_id: Optional[str] = None
    yahoo_refresh_token: Optional[str] = None

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/fantasy_football"

    # App
    secret_key: str = "dev-secret-key-change-in-production"
    environment: str = "development"

    # Optional
    rapidapi_key: Optional[str] = None


settings = Settings()
