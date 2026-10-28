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

Slack may refuse blocks that use a file it hasn't finished processing (#110), and has no way to ask
that without an extra scope. So the caller learns which files were new (``fresh``) and, if Slack
refuses its post, retries up to twice after ``RETRY_PAUSES``. Cached files get no retry.

Better still, nothing is new when it's wanted: ``prewarm`` uploads every tier in the background after
startup (#112), gently and once, so the first announcement finds files Slack finished processing long ago.

App Home can show one picture of all a member's badges (#114). ``composite`` answers "which file is
this member's picture?" from the cache only, and only once it has been in Slack for
``COMPOSITE_SETTLE``: a picture Home shows is never new, so the first view has no broken image.
When it's missing or out of date, ``composites_soon`` draws and uploads it in the background, one
member at a time, bounded by ``COMPOSITE_TIMEOUT``, and never raises: it shares the failure cooldown,
the per-key lock and the refusal handling above. A picture is current while its hash, of the PNG
a ``CompositeSource`` draws from the badges held, matches: a new tier or new art draws another.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.badge_files import BadgeComposite, BadgeFile, BadgeFileRepository
from dinnerbell.domain.badges import BADGES, BY_KEY, QUIET_BADGES, TIERS, BadgeKey, Tier
from dinnerbell.services.badge_art import ArtSource, CompositeSource, Held
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

Uploader = Callable[[str, bytes, str], Awaitable[str]]
"""Uploads a PNG (file name, bytes, alt text) and returns the file's ID."""
INLINE_TIMEOUT_SECONDS = 3.0
"""The longest a message or view waits for art. A new tier's upload is three Slack calls; past this,
the caller shows text. What was uploaded stays cached, so a later ask finishes the rest."""
RETRY_PAUSES = (1.5, 3.0)
"""Seconds to wait before each retry of a post Slack refused over blocks, when a file in it was
uploaded in the same call (``BadgeFiles.pause`` does the waiting). At most about 4.5 s in all."""
FRESH_FOR = timedelta(seconds=10)
"""How long after an upload this process still treats the file as new for retries, whichever caller
asks (a concurrent caller gets the cached ID but may hit the same not-yet-processed file)."""
MAX_ERROR = 200
"""Characters of an error that the log line keeps."""
COOLDOWN = timedelta(minutes=15)
"""How long after a failure before art is tried again."""

PREWARM_SPACING = 2.0
"""Seconds between the uploads of ``prewarm``, so the 60 files trickle in instead of arriving as a burst."""
PREWARM_UPLOAD_TIMEOUT = 60.0
"""The longest ``prewarm`` waits for one upload (three Slack calls) before counting a failure."""
PREWARM_FAILURES = 3
"""Failures in a row after which ``prewarm`` gives up. First use uploads what's left, as ever."""

COMPOSITE_SETTLE = timedelta(seconds=30)
"""How long a member's composite picture must have been in Slack before App Home shows it. Until then
Home shows the compact icon row, so a first view never has a picture Slack hasn't finished processing."""
COMPOSITE_TIMEOUT = 60.0
"""The longest drawing and uploading one member's composite takes (three Slack calls) before it counts as a failure."""

Pause = Callable[[float], Awaitable[None]]
HeldOf = Callable[[], Awaitable[Sequence[Held] | None]]
"""What a member holds now, in composite order, or None when they should get no picture (not an
active member). Read in the background, so a change since the ask is the one drawn."""

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
        composite_for: CompositeSource,
        upload: Uploader,
        clock: Clock,
        timeout: float = INLINE_TIMEOUT_SECONDS,
        pause: Pause = asyncio.sleep,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.art_for = art_for
        self.composite_for = composite_for
        self.upload = upload
        self.clock = clock
        self.timeout = timeout
        self.pause = pause
        """Waits; tests pass one that doesn't."""
        self._locks: dict[tuple[int, Wanted], asyncio.Lock] = {}
        self._retry_at = datetime.min.replace(tzinfo=UTC)
        self._uploaded_at: dict[str, datetime] = {}
        self._saves: set[asyncio.Future[None]] = set()
        self._composite_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._composite_busy: set[tuple[int, int]] = set()
        self._composite_tasks: set[asyncio.Task[None]] = set()

    async def file_ids(
        self, household_id: int, wanted: Iterable[Wanted], fresh: set[str] | None = None
    ) -> dict[Wanted, str]:
        """The Slack file for each of ``wanted`` that has one, uploading what's new. Anything it
        can't get is left out. Never raises. The IDs uploaded by this call are added to ``fresh``, if given."""
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

    async def settle(self, within: float) -> None:
        """Wait up to ``within`` seconds for saves of files already uploaded to Slack, and for pictures
        being made, so a shutdown cancelling an upload doesn't lose its row or race the database
        closing. Cancels the pictures left. Never raises."""
        pending: set[asyncio.Future[None]] = {*self._saves, *self._composite_tasks}
        if pending:
            await asyncio.wait(pending, timeout=within)
        left = set(self._composite_tasks)
        for task in left:
            task.cancel()  # a picture still drawing is made again on the next view
        if left:
            await asyncio.wait(left)  # none may run on against a closing database

    async def forget(self, household_id: int, file_ids: Iterable[str], *, cool: bool = False) -> None:
        """Drop remembered files Slack refused, so the next use uploads them again. Never raises.
        ``cool`` also pauses new uploads for ``COOLDOWN``: Slack refused files that just uploaded
        even after the retries, so uploading again at once would only repeat the wait."""
        if cool:
            self._retry_at = self.clock() + COOLDOWN
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
                found[want] = row.file_id
                if self._is_recent(row.file_id):
                    fresh.add(row.file_id)
            elif not cooling:
                uploaded[want] = await self._upload(household_id, want, png)
        found.update(uploaded)
        fresh.update(uploaded.values())

    async def _upload(self, household_id: int, want: Wanted, png: bytes) -> str:
        key, tier = want
        lock = self._locks.setdefault((household_id, want), asyncio.Lock())
        async with lock:
            digest = _hash(png)
            async with self.sessionmaker() as db:  # another caller may have uploaded it while this one waited
                row = (await BadgeFileRepository(db).all(household_id)).get(want)
            if row is not None and row.content_hash == digest:
                return row.file_id  # a concurrent caller's upload: _is_recent makes it fresh here too
            file_id = await self.upload(f"{key}-{tier}.png", png, f"{BY_KEY[BadgeKey(key)].name}, {Tier(tier).label}")
            now = self.clock()
            self._uploaded_at = {i: t for i, t in self._uploaded_at.items() if now - t < FRESH_FOR}
            self._uploaded_at[file_id] = now
            # Shielded: if the caller gives up now, the file is already in Slack, so the save still lands.
            saving = asyncio.ensure_future(self._save(household_id, BadgeFile(key, tier, digest, file_id)))
            self._saves.add(saving)
            saving.add_done_callback(self._saves.discard)
            await asyncio.shield(saving)
            log.info("badge art uploaded", extra={"household_id": household_id, "badge": key, "tier": tier})
            return file_id

    async def composite(
        self, household_id: int, member_id: int, held: Sequence[Held], fresh: set[str] | None = None
    ) -> str | None:
        """The Slack file of the member's picture of ``held``, or None: there's nothing to draw, it
        isn't uploaded yet, or it has been in Slack for less than ``COMPOSITE_SETTLE``. A picture
        that's missing or out of date is made in the background. Reads the cache only, and never
        raises. The ID goes in ``fresh`` if it counts as new (it can't, while ``COMPOSITE_SETTLE``
        outlasts ``FRESH_FOR``, but a caller that retries on ``fresh`` stays right if that changes)."""
        if not held:
            return None
        try:
            digest = _hash(self.composite_for(held))
            async with self.sessionmaker() as db:
                row = await BadgeFileRepository(db).composite(household_id, member_id)
        except Exception:
            log.exception("badge composite could not be checked", extra={"household_id": household_id})
            return None
        if row is None or row.content_hash != digest:
            await self.composites_soon(household_id, [(member_id, _held_now(held))])
            return None
        if self.clock() - row.uploaded_at < COMPOSITE_SETTLE:
            return None
        if self._is_recent(row.file_id) and fresh is not None:
            fresh.add(row.file_id)
        return row.file_id

    async def composites_soon(self, household_id: int, members: Sequence[tuple[int, HeldOf]]) -> None:
        """Start making the pictures of ``members`` (an ID and what they hold), one after another,
        in the background, and return at once. Skips a member whose picture is already being made,
        and everyone while a failure's ``COOLDOWN`` runs. Never raises."""
        if self.clock() < self._retry_at:
            return
        todo = [(m, held_of) for m, held_of in members if (household_id, m) not in self._composite_busy]
        if not todo:
            return
        self._composite_busy.update((household_id, m) for m, _ in todo)
        task = asyncio.ensure_future(self._make_composites(household_id, todo))
        self._composite_tasks.add(task)
        task.add_done_callback(self._composite_tasks.discard)

    async def idle(self) -> None:
        """Wait for the pictures being made. For tests, and for a shutdown that wants them done."""
        while self._composite_tasks:
            await asyncio.wait(set(self._composite_tasks))

    async def _make_composites(self, household_id: int, todo: Sequence[tuple[int, HeldOf]]) -> None:
        try:
            for member_id, held_of in todo:
                try:
                    async with asyncio.timeout(COMPOSITE_TIMEOUT):
                        held = await held_of()
                        if held:
                            await self._upload_composite(household_id, member_id, held)
                except asyncio.CancelledError:
                    raise
                except (_UploadFailed, TimeoutError) as exc:
                    self._failed(household_id, exc.__cause__ or exc)
                    return  # the cooldown covers everyone else; the next view or award tries again
                except Exception:  # not Slack's doing (the database, the drawing): no cooldown for the art
                    log.exception(
                        "badge composite could not be made",
                        extra={"household_id": household_id, "member_id": member_id},
                    )
                finally:
                    self._composite_busy.discard((household_id, member_id))
        finally:
            self._composite_busy.difference_update((household_id, m) for m, _ in todo)

    async def _upload_composite(self, household_id: int, member_id: int, held: Sequence[Held]) -> None:
        png = self.composite_for(held)
        digest = _hash(png)
        lock = self._composite_locks.setdefault((household_id, member_id), asyncio.Lock())
        async with lock:
            async with self.sessionmaker() as db:
                row = await BadgeFileRepository(db).composite(household_id, member_id)
            if row is not None and row.content_hash == digest:
                return
            names = ", ".join(f"{BY_KEY[BadgeKey(key)].name} ({Tier(tier).label})" for key, tier in held)
            try:
                file_id = await self.upload(f"badges-{digest[:12]}.png", png, f"Badges: {names}")
            except Exception as exc:
                raise _UploadFailed from exc
            now = self.clock()
            self._uploaded_at = {i: t for i, t in self._uploaded_at.items() if now - t < FRESH_FOR}
            self._uploaded_at[file_id] = now
            saving = asyncio.ensure_future(
                self._save_composite(household_id, member_id, BadgeComposite(digest, file_id, now))
            )
            self._saves.add(saving)
            saving.add_done_callback(self._saves.discard)
            await asyncio.shield(saving)
            log.info("badge composite uploaded", extra={"household_id": household_id, "member_id": member_id})

    async def _save_composite(self, household_id: int, member_id: int, file: BadgeComposite) -> None:
        async with self.sessionmaker() as db:
            await BadgeFileRepository(db).save_composite(household_id, member_id, file)
            await db.commit()

    async def prewarm(self, household_id: int, *, spacing: float = PREWARM_SPACING) -> bool:
        """Upload every badge tier that isn't cached yet (all but Food Critic), one at a time,
        waiting ``spacing`` seconds after each. Safe to repeat and to stop: it skips what's cached
        (so a restart resumes where the last run stopped) and each upload is saved as it lands. After a
        failure it waits out the ``COOLDOWN`` before the next try. It stops for good on a missing
        ``files:write`` scope (one log line, no retries), and after ``PREWARM_FAILURES`` other failures.
        Logs one INFO line when every tier is in, and returns whether that happened. Never raises
        (other than being cancelled)."""
        failures = 0
        uploaded = 0
        try:
            async with self.sessionmaker() as db:
                known = await BadgeFileRepository(db).all(household_id)
            todo = []
            for want in all_wanted():
                row = known.get(want)
                if row is None or row.content_hash != _hash(self.art_for(want[0], Tier(want[1]))):
                    todo.append(want)
            total = len(all_wanted())
            while todo:
                wait = (self._retry_at - self.clock()).total_seconds()
                if wait > 0:
                    await self.pause(wait)
                want = todo[0]
                try:
                    async with asyncio.timeout(PREWARM_UPLOAD_TIMEOUT):
                        await self._upload(household_id, want, self.art_for(want[0], Tier(want[1])))
                except Exception as exc:
                    self._failed(household_id, exc)
                    failures += 1
                    if _missing_scope(exc) or failures >= PREWARM_FAILURES:
                        return False
                    continue
                todo.pop(0)
                uploaded += 1
                failures = 0  # PREWARM_FAILURES counts failures in a row
                await self.pause(spacing)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("badge art prewarm failed", extra={"household_id": household_id})
            return False
        log.info(
            "badge art prewarmed",
            extra={"household_id": household_id, "uploaded": uploaded, "cached": total - uploaded, "total": total},
        )
        return True

    def _is_recent(self, file_id: str) -> bool:
        uploaded = self._uploaded_at.get(file_id)
        return uploaded is not None and self.clock() - uploaded < FRESH_FOR

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


class _UploadFailed(Exception):
    """Slack refused or failed a composite's upload (the cause): the only failure that pauses uploads."""


def _held_now(held: Sequence[Held]) -> HeldOf:
    async def read() -> Sequence[Held]:
        return held

    return read


def _valid(want: Wanted) -> bool:
    key, tier = want
    return key in BY_KEY and BadgeKey(key) not in QUIET_BADGES and tier in {t.value for t in TIERS}


def all_wanted() -> list[Wanted]:
    """Every badge tier that has art in Slack, in a steady order: all but Food Critic."""
    return sorted((b.key.value, t.value) for b in BADGES if b.key not in QUIET_BADGES for t in TIERS)


def _missing_scope(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == "missing_scope" or "missing_scope" in str(exc)
