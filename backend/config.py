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

    # Stripe billing — server-only secrets (never sent to the client, never logged).
    # Mode-agnostic: whether these hold sk_test_/whsec_(test) or sk_live_/whsec_(live)
    # and which price ids are populated is purely an environment concern, no code branch.
    stripe_secret_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None
    # Subscription price ids (recurring monthly, one per tier)
    stripe_price_standard_monthly: Optional[str] = None
    stripe_price_standard_season: Optional[str] = None
    stripe_price_pro_monthly: Optional[str] = None
    stripe_price_pro_season: Optional[str] = None
    # Credit-pack price ids (one-time payments) — one per CREDIT_PACKS entry
    stripe_price_pack_100: Optional[str] = None
    stripe_price_pack_200: Optional[str] = None
    stripe_price_pack_500: Optional[str] = None

    # Rate limits — requests per minute, per client IP
    rate_limit_api_rpm: int = 120        # general API endpoints
    rate_limit_pipeline_rpm: int = 2     # expensive pipeline triggers (each ~$10 run)
    rate_limit_auth_rpm: int = 10        # auth endpoints

    # Admin allowlist — comma-separated emails permitted to reach the operator-only
    # pipeline + admin routers (paid-compute triggers, cost reports, backtests).
    # FAIL-CLOSED: if empty, NO ONE is admin and every operator route 403s. Must be
    # set in prod (Railway env) or the admin UI + HTTP pipeline triggers stop working.
    admin_emails: str = ""

    # How many seasons of draft history a league sync imports
    league_sync_history_seasons: int = 4

    @property
    def clerk_enabled(self) -> bool:
        """True when Clerk is configured."""
        return bool(self.clerk_secret_key)

    @property
    def stripe_enabled(self) -> bool:
        """True when Stripe billing is configured (secret key present)."""
        return bool(self.stripe_secret_key)

    @property
    def admin_email_set(self) -> set[str]:
        """Normalized (lowercased) set of admin emails from ADMIN_EMAILS."""
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    def is_admin_email(self, email: str | None) -> bool:
        """True only when a non-empty email is in the (non-empty) admin allowlist."""
        return bool(email) and email.strip().lower() in self.admin_email_set


settings = Settings()
