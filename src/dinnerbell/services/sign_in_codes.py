# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code (FR-53, ADR 0010, spec 6.15).

An admin makes a code for any active member, admins included, on Admin > Members; a signed-in
member makes one for themselves on Preferences. A new code replaces the member's old one,
whoever made it. Guests and deactivated members never get one. The code is shown once and
stored only as a keyed hash (HMAC-SHA256 with a key derived from ``DINNERBELL_SECRET_KEY`` for
this purpose alone), so a copy of the database without the secret key can't test guesses at
it. It never reaches a log, an audit row, or a URL. Signing a member out everywhere, or
deactivating them, deletes their code.

Redeeming has two steps on ``/auth/code``. ``peek`` finds whose code it is without using it, so
the page can say "You're signing in as NAME". ``redeem`` uses it in one conditional UPDATE, so
two taps can't both win, checks the member again, and opens an ordinary session through
``services.auth.open_session``, the same path Sign in with Slack takes. Every failure looks the
same to the person typing: wrong, expired, used, a guest's, or a deactivated member's.

Everything here answers NotFound while Admin's **Web app** switch is off.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.db.sign_in_codes import SignInCodeRepository
from dinnerbell.domain.members import Member, MemberRuleViolation
from dinnerbell.domain.sessions import device_label
from dinnerbell.domain.sign_in_codes import ALPHABET, CODE_LENGTH, CODE_LIFETIME, display_code, normalize_code
from dinnerbell.services.auth import open_session
from dinnerbell.services.common import audit, require_admin, require_member
from dinnerbell.services.errors import NotFound
from dinnerbell.services.web_app import SWITCHED_OFF, require_web_app

log = logging.getLogger(__name__)

ADMINS_ONLY = "Only the household admin can make a sign-in code for someone else."
NO_GUESTS = "Guests can't sign in with a code."
REACTIVATE_FIRST = "Reactivate them first. A deactivated member can't sign in."


def hash_code(key: bytes, code: str) -> str:
    """The HMAC-SHA256 hex digest the database keeps in place of a normalized code. ``key`` is
    the install's sign-in code key (``derive_key(secret, "sign-in-code")``, made where the app
    is put together): without it, a hash in a database copy can't be checked against a guess,
    so the 49 bits of a code can't be searched offline."""
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    """A random code from ``secrets``: about 49 bits, for a code that lasts 15 minutes and
    sits behind the per-address limit."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """A new code, to show once. ``code`` is in its display form, such as ``K7M2Q-9XRTD``."""

    member: Member
    code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CodeSignIn:
    member: Member
    token: str


async def create_my_code(db: AsyncSession, actor: Member, *, key: bytes, now: datetime) -> IssuedCode:
    """A signed-in member's own code, on Preferences."""
    require_member(actor)
    await require_web_app(db, actor.household_id)
    return await _issue(db, actor, actor, key=key, now=now)


async def create_member_code(
    db: AsyncSession, actor: Member, member_id: int, *, key: bytes, now: datetime
) -> IssuedCode:
    """An admin's code for any active member, admins and themselves included. Refused for a
    guest or a deactivated member."""
    require_admin(actor, ADMINS_ONLY)
    await require_web_app(db, actor.household_id)
    member = await MemberRepository(db).get(actor.household_id, member_id)
    if member is None:
        raise NotFound("No such member.")
    if member.is_guest:
        raise MemberRuleViolation(NO_GUESTS)
    if not member.active:
        raise MemberRuleViolation(REACTIVATE_FIRST)
    return await _issue(db, actor, member, key=key, now=now)


async def _issue(db: AsyncSession, actor: Member, member: Member, *, key: bytes, now: datetime) -> IssuedCode:
    code = new_code()
    expires_at = now + CODE_LIFETIME
    async with write_transaction(db):
        await SignInCodeRepository(db).replace(
            member=member, code_hash=hash_code(key, code), created_by=actor, now=now, expires_at=expires_at
        )
        # SEC-9, and never the code or its hash.
        payload = {"expires_at": expires_at.isoformat()}
        await audit(db, member.household_id, actor, "member.sign_in_code_created", f"member:{member.id}", payload, now)
        await db.commit()
    log.info("sign-in code created", extra={"member_id": member.id, "actor_id": actor.id})
    return IssuedCode(member=member, code=display_code(code), expires_at=expires_at)


async def _household_id(db: AsyncSession) -> int:
    """The install's household, as ``services.web_app.web_app_enabled_for`` reads it for a
    visitor with no session, once its switch is checked."""
    household = await HouseholdRepository(db).first()
    if household is None:
        raise NotFound(SWITCHED_OFF)
    await require_web_app(db, household.id)
    return household.id


async def peek(db: AsyncSession, typed: str, *, key: bytes, now: datetime) -> Member | None:
    """Step 1: whose code ``typed`` is, without using it. None for every kind of failure."""
    household_id = await _household_id(db)
    code = normalize_code(typed)
    if code is None:
        return None
    stored = await SignInCodeRepository(db).usable(household_id, hash_code(key, code), now)
    await db.rollback()  # reads only; end the read transaction
    if stored is None:
        return None
    if not stored.member.is_member:
        log.info("sign-in code refused", extra={"member_id": stored.member.id, "reason": "not an active member"})
        return None
    return stored.member


async def redeem(
    db: AsyncSession, typed: str, *, key: bytes, now: datetime, replaces_token: str | None, user_agent: str | None
) -> CodeSignIn | None:
    """Step 2, **Continue**: use the code and sign the device in. None for every kind of
    failure. A code whose member was deactivated or made a guest since is used up all the same."""
    household_id = await _household_id(db)
    code = normalize_code(typed)
    if code is None:
        return None
    async with write_transaction(db):
        used = await SignInCodeRepository(db).use(household_id, hash_code(key, code), now)
        if used is None:
            return None
        _code_id, member_id = used
        member = await MemberRepository(db).get(household_id, member_id)
        if member is None or not member.is_member:
            await db.commit()
            log.info("sign-in code refused", extra={"member_id": member_id, "reason": "not an active member"})
            return None
        token = await open_session(db, member, now=now, replaces_token=replaces_token, user_agent=user_agent)
        # Signed in, though never linked to an OIDC subject: Admin > Members shows them active.
        member = await MemberRepository(db).update(household_id, member.id, now, code_signed_in_at=now)
        payload = {"device": device_label(user_agent)}
        await audit(db, household_id, member, "member.signed_in_with_code", f"member:{member.id}", payload, now)
        await db.commit()
    log.info("member signed in with a code", extra={"member_id": member.id})
    return CodeSignIn(member=member, token=token)


# Wrong codes across every address --------------------------------------------------------------

THROTTLE_THRESHOLD = 200
THROTTLE_WINDOW = timedelta(hours=1)
THROTTLE_STEP_SECONDS = 0.05
"""Each wrong code past the threshold adds this much to the wait."""
THROTTLE_MAX_SECONDS = 5.0
MAX_SLEEPERS = 100
"""At most this many attempts wait at once, from all addresses, to bound memory. Each address
may have one waiting; past either limit, an attempt is refused at once (429) instead of waiting.
One address can't fill the slots and lock everyone else out (ADR 0010: nothing locks out)."""
Sleep = Callable[[float], Awaitable[None]]


class CodeThrottle:
    """Wrong codes from every address in the last hour, in one-minute buckets (spec 6.15).
    Past ``threshold`` of them, every code attempt waits a little longer than the last, up to
    ``THROTTLE_MAX_SECONDS``. Nothing ever locks out: a member with the right code gets in,
    only slower.

    It lives in memory, one per process, like the per-address limit in ``web.ratelimit``:
    Dinnerbell runs one process, so memory sees every attempt, and a restart that forgets it
    only lifts a delay. Writing each wrong code to the database would let anyone with a script
    make SQLite write as fast as they can post. Sixty buckets at most, so memory is bounded."""

    def __init__(
        self, threshold: int = THROTTLE_THRESHOLD, sleep: Sleep = asyncio.sleep, max_sleepers: int = MAX_SLEEPERS
    ) -> None:
        self.threshold = threshold
        self.sleep = sleep
        self.max_sleepers = max_sleepers
        self.sleeping = 0
        """Attempts waiting right now."""
        self._waiting: set[str] = set()
        """The addresses with an attempt waiting right now."""
        self._buckets: deque[tuple[datetime, int]] = deque()

    def _prune(self, now: datetime) -> None:
        while self._buckets and now - self._buckets[0][0] >= THROTTLE_WINDOW:
            self._buckets.popleft()

    def failures(self, now: datetime) -> int:
        """Wrong codes in the last hour, from every address."""
        self._prune(now)
        return sum(count for _, count in self._buckets)

    def busy(self, now: datetime) -> bool:
        """Whether attempts are being slowed, for the notice on Admin > Web app."""
        return self.failures(now) > self.threshold

    def failed(self, now: datetime) -> None:
        """Count one wrong code."""
        was_busy = self.busy(now)
        minute = now.replace(second=0, microsecond=0)
        if self._buckets and self._buckets[-1][0] == minute:
            self._buckets[-1] = (minute, self._buckets[-1][1] + 1)
        else:
            self._buckets.append((minute, 1))
        if not was_busy and self.busy(now):
            log.warning(
                "over %d wrong sign-in codes in an hour; slowing every code attempt",
                self.threshold,
                extra={"failures": self.failures(now)},
            )

    def delay(self, now: datetime) -> float:
        """The seconds the next attempt waits: 0 up to the threshold, then growing."""
        excess = self.failures(now) - self.threshold
        return 0.0 if excess <= 0 else min(THROTTLE_MAX_SECONDS, THROTTLE_STEP_SECONDS * excess)

    async def wait(self, now: datetime, address: str) -> bool:
        """Wait out the delay before an attempt from ``address`` (the per-address limit's key) is
        checked. Returns False, without waiting, when that address has an attempt waiting
        already, or ``max_sleepers`` attempts are waiting in all: the caller refuses this one at
        once.

        The caller must hold no database connection while it waits, or a few waiting attempts
        could take every connection in the pool."""
        seconds = self.delay(now)
        if not seconds:
            return True
        if address in self._waiting or self.sleeping >= self.max_sleepers:
            log.info("refused a sign-in code attempt: one is waiting already", extra={"waiting": self.sleeping})
            return False
        self.sleeping += 1
        self._waiting.add(address)
        try:
            log.debug("slowing a sign-in code attempt", extra={"delay_seconds": seconds})
            await self.sleep(seconds)
        finally:
            self.sleeping -= 1
            self._waiting.discard(address)
        return True
