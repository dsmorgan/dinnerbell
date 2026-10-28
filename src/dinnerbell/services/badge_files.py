# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badge art as files in Slack (#105): each badge tier's PNG, uploaded once and reused by ID.

``BadgeFiles.file_ids`` answers "which Slack file shows this badge at this tier?" for the
messages and views that want art. A tier's file is uploaded the first time it's wanted and
remembered (``slack_badge_file``) with a hash of the PNG, so a restart reuses it and different art
uploads again. The PNG comes from an ``ArtSource`` and the upload from an ``Uploader``, both
passed in: this module knows neither the built-in drawing nor Slack.

Art is a nicety, so nothing here may hold up or break what shows it. ``file_ids`` never raises
and gives up after ``timeout`` seconds, and returns only the tiers it has: the caller shows text
for the rest. After a failure, such as a missing ``files:write`` scope, it logs one line and
doesn't try again for ``COOLDOWN``, so a broken setup costs one slow call, not one per message.
Two callers wanting the same new tier at once share one upload. Food Critic is never shown
(``QUIET_BADGES``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.badge_files import BadgeFile, BadgeFileRepository
from dinnerbell.domain.badges import BY_KEY, QUIET_BADGES, TIERS, BadgeKey, Tier
from dinnerbell.services.badge_art import ArtSource
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

Uploader = Callable[[str, bytes, str], Awaitable[str]]
"""Uploads a PNG (file name, bytes, alt text) and returns the file's ID."""
TIMEOUT_SECONDS = 20.0
"""The longest a caller waits for art. The first use of a household uploads up to sixty files in
the worst case, but a message only wants the few it shows."""
COOLDOWN = timedelta(minutes=15)
"""How long after a failure before art is tried again."""

Wanted = tuple[str, str]
"""A badge key and a tier value, such as ``("chef", "gold")``."""


def _hash(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


class BadgeFiles:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        art_for: ArtSource,
        upload: Uploader,
        clock: Clock,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.art_for = art_for
        self.upload = upload
        self.clock = clock
        self.timeout = timeout
        self._locks: dict[tuple[int, Wanted], asyncio.Lock] = {}
        self._retry_at = datetime.min.replace(tzinfo=UTC)

    async def file_ids(self, household_id: int, wanted: Iterable[Wanted]) -> dict[Wanted, str]:
        """The Slack file for each of ``wanted`` that has one, uploading what's new. Anything it
        can't get is left out. Never raises."""
        asked = {w for w in wanted if _valid(w)}
        found: dict[Wanted, str] = {}
        if not asked or self.clock() < self._retry_at:
            return found
        try:
            async with asyncio.timeout(self.timeout):
                await self._resolve(household_id, asked, found)
        except Exception as exc:
            self._failed(household_id, exc)
        return found

    async def forget(self, household_id: int, file_ids: Iterable[str]) -> None:
        """Drop remembered files Slack refused, so the next use uploads them again. Never raises."""
        ids = set(file_ids)
        if not ids:
            return
        try:
            async with self.sessionmaker() as db:
                await BadgeFileRepository(db).forget(household_id, ids)
                await db.commit()
        except Exception:
            log.exception("badge art could not be forgotten", extra={"household_id": household_id})

    async def _resolve(self, household_id: int, asked: set[Wanted], found: dict[Wanted, str]) -> None:
        async with self.sessionmaker() as db:
            known = await BadgeFileRepository(db).all(household_id)
        for want in sorted(asked):
            key, tier = want
            png = self.art_for(key, Tier(tier))
            row = known.get(want)
            if row is not None and row.content_hash == _hash(png):
                found[want] = row.file_id
            else:
                found[want] = await self._upload(household_id, want, png)

    async def _upload(self, household_id: int, want: Wanted, png: bytes) -> str:
        key, tier = want
        lock = self._locks.setdefault((household_id, want), asyncio.Lock())
        async with lock:
            digest = _hash(png)
            async with self.sessionmaker() as db:  # another caller may have uploaded it while this one waited
                row = (await BadgeFileRepository(db).all(household_id)).get(want)
            if row is not None and row.content_hash == digest:
                return row.file_id
            file_id = await self.upload(f"{key}-{tier}.png", png, f"{BY_KEY[BadgeKey(key)].name}, {Tier(tier).label}")
            async with self.sessionmaker() as db:
                await BadgeFileRepository(db).save(household_id, BadgeFile(key, tier, digest, file_id), self.clock())
                await db.commit()
            log.info("badge art uploaded", extra={"household_id": household_id, "badge": key, "tier": tier})
            return file_id

    def _failed(self, household_id: int, exc: BaseException) -> None:
        self._retry_at = self.clock() + COOLDOWN
        reason = "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        log.warning(
            "badge art not available, showing text badges "
            "(a missing_scope error means the Slack app needs the files:write scope: reinstall it)",
            extra={"household_id": household_id, "error": reason, "retry_minutes": COOLDOWN // timedelta(minutes=1)},
        )


def _valid(want: Wanted) -> bool:
    key, tier = want
    return key in BY_KEY and BadgeKey(key) not in QUIET_BADGES and tier in {t.value for t in TIERS}
