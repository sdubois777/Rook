"""
CredentialRepository — encrypted token storage.
All reads decrypt. All writes encrypt.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.integrations.token_encryption import (
    decrypt_token, encrypt_token,
)
from backend.models.platform_credential import PlatformCredential
from backend.repositories.base import BaseRepository


class CredentialRepository(BaseRepository[PlatformCredential]):
    model = PlatformCredential

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        platform: str,
    ) -> PlatformCredential | None:
        result = await self._session.execute(
            select(PlatformCredential)
            .where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_yahoo(
        self,
        user_id: uuid.UUID,
        access_token: str,
        refresh_token: str,
        expires_at: datetime | None,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="yahoo",
                access_token=encrypt_token(access_token),
                refresh_token=encrypt_token(refresh_token),
                token_expires_at=expires_at,
            )
            .on_conflict_do_update(
                constraint="uq_platform_credentials_user_platform",
                set_={
                    "access_token": encrypt_token(access_token),
                    "refresh_token": encrypt_token(refresh_token),
                    "token_expires_at": expires_at,
                },
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "yahoo")

    async def upsert_espn(
        self,
        user_id: uuid.UUID,
        espn_s2: str,
        swid: str,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="espn",
                espn_s2=encrypt_token(espn_s2),
                swid=encrypt_token(swid),
            )
            .on_conflict_do_update(
                constraint="uq_platform_credentials_user_platform",
                set_={
                    "espn_s2": encrypt_token(espn_s2),
                    "swid": encrypt_token(swid),
                },
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "espn")

    async def upsert_sleeper(
        self,
        user_id: uuid.UUID,
        sleeper_user_id: str,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="sleeper",
                sleeper_user_id=sleeper_user_id,
            )
            .on_conflict_do_update(
                constraint="uq_platform_credentials_user_platform",
                set_={"sleeper_user_id": sleeper_user_id},
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "sleeper")

    async def get_yahoo_tokens(
        self, user_id: uuid.UUID
    ) -> tuple[str, str, datetime | None] | None:
        """Returns (access_token, refresh_token, expires_at) decrypted."""
        cred = await self.get_for_user(user_id, "yahoo")
        if not cred or not cred.refresh_token:
            return None
        return (
            decrypt_token(cred.access_token or ""),
            decrypt_token(cred.refresh_token),
            cred.token_expires_at,
        )

    async def get_espn_cookies(
        self, user_id: uuid.UUID
    ) -> tuple[str, str] | None:
        """Returns (espn_s2, swid) decrypted."""
        cred = await self.get_for_user(user_id, "espn")
        if not cred or not cred.espn_s2:
            return None
        return (
            decrypt_token(cred.espn_s2),
            decrypt_token(cred.swid or ""),
        )

    async def disconnect(
        self,
        user_id: uuid.UUID,
        platform: str,
    ) -> None:
        await self._session.execute(
            delete(PlatformCredential)
            .where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
            )
        )
        await self._session.commit()
