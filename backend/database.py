import os

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from backend.config import settings


# --- DB connection pool sizing ------------------------------------------------
# Railway Postgres max_connections = 100, SHARED across every consumer:
#   • this app pool (below)                    up to DB_POOL_SIZE + DB_MAX_OVERFLOW (default 40)
#   • a pipeline run (separate process, same    bounded by agent concurrency ~10 (never the
#     module engine → same defaults)            full pool; connections open lazily on demand)
#   • alembic migrations on deploy (NullPool)   ~1-2, transient, run BEFORE uvicorn starts
#   • admin/psql + Postgres superuser reserve   ~5 (superuser_reserved_connections default 3)
# Worst-case realistic concurrent total (drafts live + a dirty pipeline refresh firing):
#   40 (app) + ~12 (pipeline) + ~3 (admin) + ~2 (migration) + 3 (reserve) ≈ 60 of 100. ~40 margin.
# NEVER size the app to claim all 100. Tunable in Railway via env WITHOUT a code deploy.
_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "20"))
_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "20"))
_POOL_TIMEOUT = int(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "30"))
# Railway can silently drop idle connections; recycle before they go stale (pre_ping
# also validates on checkout — belt and suspenders).
_POOL_RECYCLE = int(os.environ.get("DB_POOL_RECYCLE_SECONDS", "1800"))  # 30 min

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    pool_pre_ping=True,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=_POOL_TIMEOUT,
    pool_recycle=_POOL_RECYCLE,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
