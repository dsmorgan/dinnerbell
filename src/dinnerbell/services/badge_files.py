# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badge art as files in Slack (#105): each badge tier's PNG, uploaded once and reused by ID.

``BadgeFiles.file_ids`` answers "which Slack file shows this badge at this tier?" for the
messages and views that want art. A tier's file is uploaded the first time it's wanted and
remembered (``slack_badge_file``) with a hash of the PNG, so a restart reuses it and different art
uploads again. The PNG comes from an ``ArtSource`` and the upload from an ``Uploader``, both
passed in: this module knows neither the built-in drawing nor Slack.

Art is a nicety, so nothing here may break what shows it, and it holds it up only briefly:
``file_ids`` never raises and gives up after ``timeout`` seconds (``INLINE_TIMEOUT_SECONDS``). It
returns only the tiers it has: the caller shows text for the rest. After a failure, such as a
missing ``files:write`` scope, it logs one line and uploads nothing new for ``COOLDOWN``, so a
broken setup costs one slow call, not one per message; files already uploaded still show.
Two callers wanting the same new tier at once share one upload. Food Critic is never shown
(``QUIET_BADGES``).

Slack processes an upload after it returns the file ID, and refuses blocks that use a file it hasn't
finished with (#110). So a file uploaded in this call is returned only once ``files.info`` says it's
ready (``is_ready``), within the same ``timeout``; cached files were ready long ago and skip the wait.
The caller learns which files were new (``fresh``) so it can retry a refused post once.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.badge_files import BadgeFile, BadgeFileRepository
from dinnerbell.domain.badges import BY_KEY, QUIET_BADGES, TIERS, BadgeKey, Tier
from dinnerbell.services.badge_art import ArtSource
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

Uploader = Callable[[str, bytes, str], Awaitable[str]]
"""Uploads a PNG (file name, bytes, alt text) and returns the file's ID."""
INLINE_TIMEOUT_SECONDS = 3.0
"""The longest a message or view waits for art. A new tier's upload is three Slack calls; past this,
the caller shows text. What was uploaded stays cached, so a later ask finishes the rest."""
READY_POLL_SECONDS = 0.25
"""How long to wait between two ``files.info`` checks of a new file."""
RETRY_PAUSE_SECONDS = 1.5
"""How long a caller waits before posting once more after Slack refused blocks that use a file
uploaded in the same call (``BadgeFiles.pause`` does the waiting)."""
MAX_ERROR = 200
"""Characters of an error that the log line keeps."""
COOLDOWN = timedelta(minutes=15)
"""How long after a failure before art is tried again."""

FileInfo = Callable[[str], Awaitable[Mapping[str, Any]]]
"""Reads what Slack says about a file (``files.info``) by ID."""
Pause = Callable[[float], Awaitable[None]]

Wanted = tuple[str, str]
"""A badge key and a tier value, such as ``("chef", "gold")``."""


def is_ready(file: Mapping[str, Any]) -> bool:
    """Whether Slack has finished processing an uploaded image, from ``files.info``'s ``file``.

    Assumption (Slack documents no "ready" flag): an image is ready for blocks once it has a
    ``mimetype`` and either its dimensions (``original_w``) or any thumbnail (``thumb_*``). Slack sets
    those when processing finishes; ``url_private`` appears at once, so it proves nothing. The check
    is deliberately strict: a file that never meets it only costs a timeout to text badges.
    """
    if not file.get("mimetype"):
        return False
    return bool(file.get("original_w")) or any(k.startswith("thumb_") and file[k] for k in file)


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
        timeout: float = INLINE_TIMEOUT_SECONDS,
        file_info: FileInfo | None = None,
        pause: Pause = asyncio.sleep,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.art_for = art_for
        self.upload = upload
        self.clock = clock
        self.timeout = timeout
        self.file_info = file_info
        """Without it, a new file is trusted at once. A failed check trusts the file too."""
        self._info_warned = False
        self.pause = pause
        """Waits; tests pass one that doesn't."""
        self._locks: dict[tuple[int, Wanted], asyncio.Lock] = {}
        self._retry_at = datetime.min.replace(tzinfo=UTC)

    async def file_ids(
        self, household_id: int, wanted: Iterable[Wanted], fresh: set[str] | None = None
    ) -> dict[Wanted, str]:
        """The Slack file for each of ``wanted`` that has one, uploading what's new and waiting
        until Slack has processed it. Anything it can't get is left out. Never raises. The IDs
        uploaded by this call are added to ``fresh``, if given."""
        asked = {w for w in wanted if _valid(w)}
        found: dict[Wanted, str] = {}
        if not asked:
            return found
        try:
            async with asyncio.timeout(self.timeout):
                await self._resolve(household_id, asked, found, fresh if fresh is not None else set())
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

    async def _resolve(self, household_id: int, asked: set[Wanted], found: dict[Wanted, str], fresh: set[str]) -> None:
        async with self.sessionmaker() as db:
            known = await BadgeFileRepository(db).all(household_id)
        cooling = self.clock() < self._retry_at  # a failure pauses new uploads, never files already uploaded
        uploaded: dict[Wanted, str] = {}
        for want in sorted(asked):
            key, tier = want
            png = self.art_for(key, Tier(tier))
            row = known.get(want)
            if row is not None and row.content_hash == _hash(png):
                found[want] = row.file_id  # cached: ready long ago
            elif not cooling:
                uploaded[want] = await self._upload(household_id, want, png)
        # One wait for all the new files together, so six uploads don't cost six waits.
        await asyncio.gather(*(self._await_ready(file_id) for file_id in uploaded.values()))
        found.update(uploaded)
        fresh.update(uploaded.values())

    async def _await_ready(self, file_id: str) -> None:
        """Returns once Slack has processed ``file_id``. Waits only as long as the caller's timeout
        allows. If ``files.info`` can't be read, such as without ``files:read``, trusts the file:
        the caller's retry covers a file that wasn't ready."""
        if self.file_info is None:
            return
        while True:
            try:
                info = await self.file_info(file_id)
            except Exception as exc:
                if not self._info_warned:  # once per run, not once per file
                    self._info_warned = True
                    log.warning(
                        "badge art readiness not checked; the files:read scope (reinstall the Slack app) enables it",
                        extra={"error": (str(exc) or type(exc).__name__)[:MAX_ERROR]},
                    )
                return
            if is_ready(info):
                return
            await self.pause(READY_POLL_SECONDS)

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
            # Shielded: if the caller gives up now, the file is already in Slack, so the save still lands.
            await asyncio.shield(self._save(household_id, BadgeFile(key, tier, digest, file_id)))
            log.info("badge art uploaded", extra={"household_id": household_id, "badge": key, "tier": tier})
            return file_id

    async def _save(self, household_id: int, file: BadgeFile) -> None:
        async with self.sessionmaker() as db:
            await BadgeFileRepository(db).save(household_id, file, self.clock())
            await db.commit()

    def _failed(self, household_id: int, exc: BaseException) -> None:
        self._retry_at = self.clock() + COOLDOWN
        reason = "timed out" if isinstance(exc, TimeoutError) else (str(exc) or type(exc).__name__)[:MAX_ERROR]
        log.warning(
            "badge art not available, showing text badges "
            "(a missing_scope error means the Slack app needs the files:write scope: reinstall it)",
            extra={"household_id": household_id, "error": reason, "retry_minutes": COOLDOWN // timedelta(minutes=1)},
        )


def _valid(want: Wanted) -> bool:
    key, tier = want
    return key in BY_KEY and BadgeKey(key) not in QUIET_BADGES and tier in {t.value for t in TIERS}
