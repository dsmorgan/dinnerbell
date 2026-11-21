# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code, and Devices (#138, spec 6.15).

A code is made on Preferences or Admin > Members, shown once, and stored as a hash. ``/auth/code``
says whose it is, then **Continue** uses it once and signs the device in with an ordinary
session. Preferences lists a member's devices and signs any of them out; an admin signs a
member out of every device. Off, none of it exists."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, SessionRow, SignInCodeRow
from dinnerbell.domain.members import Role
from dinnerbell.domain.sessions import UNKNOWN_DEVICE, device_label
from dinnerbell.domain.sign_in_codes import ALPHABET, CODE_LENGTH, display_code, normalize_code
from dinnerbell.services import sign_in_codes
from dinnerbell.services.database import open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.sign_in_codes import THROTTLE_MAX_SECONDS, CodeThrottle, new_code
from tests.conftest import BASE_URL, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.web import htmx, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import SESSION_COOKIE, member, rows
from tests.test_web_app import switch

SAM = ("subject-sam", "sam@example.com")
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.1 Mobile/15E148 Safari/604.1"
)
MAC_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DIDNT_WORK = "That code didn't work."
ISSUED = re.compile(r'class="issued-code">([A-Z0-9-]+)</span>')


def device(client: TestClient, user_agent: str = IPHONE) -> TestClient:
    """Another browser on the same app, signed out, as a phone would be."""
    headers = {"Origin": BASE_URL, "User-Agent": user_agent}
    return TestClient(client.app, base_url=BASE_URL, headers=headers, follow_redirects=False)


def issued(html: str) -> str:
    match = ISSUED.search(html)
    assert match, "no code on the page"
    return match.group(1)


def my_code(client: TestClient) -> str:
    response = submit(client, "/preferences/sign-in-code")
    assert response.status_code == 200, page(response)
    return issued(response.text)


def admin_code(client: TestClient, member_id: int) -> str:
    response = submit(client, f"/admin/members/{member_id}/sign-in-code")
    assert response.status_code == 200, page(response)
    return issued(response.text)


def redeem(phone: TestClient, code: str) -> TestClient:
    """Both steps, as the member taps them."""
    first = phone.post("/auth/code", data={"code": code})
    assert first.status_code == 200 and "You're signing in as" in page(first), page(first)
    second = phone.post("/auth/code/continue", data={"code": code})
    assert second.status_code == 303, page(second)
    return phone


def audits(settings: Settings, action: str) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == action]


def codes(settings: Settings) -> list[SignInCodeRow]:
    return rows(settings, SignInCodeRow)


def sessions_of(settings: Settings, member_id: int) -> list[SessionRow]:
    return [s for s in rows(settings, SessionRow) if s.member_id == member_id]


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    return client


@pytest.fixture
def sam(admin: TestClient, migrated: Settings) -> int:
    assert invite(admin, SAM[1], "Sam").status_code == 303
    return int(member(migrated, SAM[1]).id)


@pytest.fixture
def on(admin: TestClient, sam: int) -> TestClient:
    switch(admin, True)
    return admin


# The rules ---------------------------------------------------------------------------------------


def test_a_code_has_ten_characters_without_look_alikes() -> None:
    assert not set("01ILOU") & set(ALPHABET) and len(set(ALPHABET)) == len(ALPHABET) == 30
    made = {new_code() for _ in range(50)}
    assert len(made) == 50
    assert all(len(c) == CODE_LENGTH and set(c) <= set(ALPHABET) for c in made)
    assert display_code("ABCDEFGHJK") == "ABCDE-FGHJK"
    assert normalize_code(" abcde-fghjk ") == "ABCDEFGHJK" == normalize_code("ABCDE FGHJK")
    for bad in ("", "ABCDEFGHJ", "ABCDEFGHJKM", "ABCDEFGHJ0", "ABCDE_FGHJK", "ÄBCDEFGHJK"):
        assert normalize_code(bad) is None, bad


@pytest.mark.parametrize(
    ("user_agent", "label"),
    [
        (IPHONE, "iPhone · Safari"),
        (MAC_CHROME, "Mac · Chrome"),
        (
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/120.0 Safari/604.1",
            "iPad · Chrome",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36",
            "Android · Chrome",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) SamsungBrowser/25.0 Chrome/121.0 Safari/537.36",
            "Android · Samsung Internet",
        ),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36 Edg/131.0", "Windows · Edge"),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0", "Linux · Firefox"),
        ("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) Chrome/131.0 Safari/537.36", "Chromebook · Chrome"),
        ("Mozilla/5.0 (Windows NT 10.0) OPR/115.0", "Windows · Opera"),
        ("curl/8.7.1", UNKNOWN_DEVICE),
        ("", UNKNOWN_DEVICE),
        (None, UNKNOWN_DEVICE),
        ("Firefox/1" + "x" * 5000, "Firefox"),
    ],
)
def test_a_device_label_is_coarse(user_agent: str | None, label: str) -> None:
    """Platform and browser only: no version, so the label isn't a fingerprint."""
    assert device_label(user_agent) == label
    assert not any(ch.isdigit() for ch in device_label(user_agent))


def test_the_throttle_grows_past_200_wrong_codes_an_hour_and_never_locks_out(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    throttle = CodeThrottle()
    for _ in range(200):
        throttle.failed(clock())
    assert (throttle.failures(clock()), throttle.delay(clock()), throttle.busy(clock())) == (200, 0.0, False)
    assert not [r for r in caplog.records if "slowing" in r.getMessage()]
    throttle.failed(clock())
    first = throttle.delay(clock())
    assert throttle.busy(clock()) and first > 0
    assert [r.levelno for r in caplog.records if "wrong sign-in codes" in r.getMessage()] == [logging.WARNING]
    for _ in range(20):
        throttle.failed(clock())
    assert throttle.delay(clock()) > first
    for _ in range(1000):
        throttle.failed(clock())
    assert throttle.delay(clock()) == THROTTLE_MAX_SECONDS
    assert [r.levelno for r in caplog.records if "wrong sign-in codes" in r.getMessage()] == [logging.WARNING]
    clock.advance(timedelta(minutes=59))
    assert throttle.busy(clock())
    clock.advance(timedelta(minutes=1))
    assert (throttle.failures(clock()), throttle.delay(clock()), throttle.busy(clock())) == (0, 0.0, False)


def test_the_throttle_counts_a_sliding_hour_in_bounded_buckets() -> None:
    clock = FakeClock()
    throttle = CodeThrottle(threshold=2)
    for _ in range(90):
        throttle.failed(clock())
        clock.advance(timedelta(minutes=1))
    assert throttle.failures(clock()) == 59 and len(throttle._buckets) == 59


def test_the_throttle_waits_with_the_sleep_it_was_given() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    clock = FakeClock()
    throttle = CodeThrottle(threshold=1, sleep=sleep)
    asyncio.run(throttle.wait(clock()))
    throttle.failed(clock())
    throttle.failed(clock())
    asyncio.run(throttle.wait(clock()))
    assert slept == [throttle.delay(clock())] and slept[0] > 0


# Off ---------------------------------------------------------------------------------------------


def test_off_every_code_and_device_route_is_the_usual_404(admin: TestClient, sam: int, migrated: Settings) -> None:
    phone = device(admin)
    assert phone.get("/auth/code").status_code == 404
    for path in ("/auth/code", "/auth/code/continue"):
        assert phone.post(path, data={"code": "ABCDE-FGHJK"}).status_code == 404, path
        assert submit(admin, path, {"code": "ABCDE-FGHJK"}).status_code == 404, path
    [mine] = sessions_of(migrated, member(migrated, "admin@example.com").id)
    for path in (
        "/preferences/sign-in-code",
        f"/preferences/devices/{mine.public_id}/sign-out",
        f"/admin/members/{sam}/sign-in-code",
        f"/admin/members/{sam}/sign-out",
    ):
        response = submit(admin, path)
        assert response.status_code == 404, path
        assert "That page doesn't exist." in page(response), path
    assert 'href="/auth/code"' not in phone.get("/").text
    assert 'id="devices"' not in admin.get("/preferences").text
    members = admin.get("/admin/members").text
    assert "/sign-in-code" not in members and "/sign-out" not in members
    assert codes(migrated) == [] and len(sessions_of(migrated, mine.member_id)) == 1


def test_codes_and_sessions_stay_when_the_switch_goes_off(on: TestClient, sam: int, migrated: Settings) -> None:
    """Spec 6.15: turning it off keeps sessions and codes, which work again when it's back on."""
    code = admin_code(on, sam)
    switch(on, False)
    assert len(codes(migrated)) == 1
    phone = device(on)
    assert phone.post("/auth/code", data={"code": code}).status_code == 404
    switch(on, True)
    redeem(phone, code)


# Making a code -----------------------------------------------------------------------------------


def test_a_member_makes_their_own_code_and_it_shows_once(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings, clock: FakeClock
) -> None:
    sign_in_as(on, provider, *SAM)
    before = on.get("/preferences")
    assert 'id="devices"' in before.text and 'id="issued-code"' not in before.text
    response = submit(on, "/preferences/sign-in-code")
    code = issued(response.text)
    assert "until 1:15 PM" in page(response)  # 18:00 UTC is 1:00 PM in Chicago, plus 15 minutes
    assert "This is the only time it's shown." in page(response)
    assert code not in on.get("/preferences").text  # never again
    [row] = codes(migrated)
    assert row.code_hash == hashlib.sha256(normalize_code(code).encode()).hexdigest()  # type: ignore[union-attr]
    assert (row.member_id, row.created_by, row.used_at) == (sam, sam, None)
    assert row.expires_at == clock.now + timedelta(minutes=15)
    [made] = audits(migrated, "member.sign_in_code_created")
    assert (made.actor_member_id, made.target) == (sam, f"member:{sam}")
    assert made.payload == {"expires_at": (clock.now + timedelta(minutes=15)).isoformat()}
    assert inline_code(response.text)[1] == []


def test_an_admin_makes_a_code_for_any_active_member_admins_included(
    on: TestClient, sam: int, migrated: Settings
) -> None:
    admin_id = member(migrated, "admin@example.com").id
    members = on.get("/admin/members").text
    assert f"/admin/members/{sam}/sign-in-code" in members and f"/admin/members/{admin_id}/sign-in-code" in members
    response = submit(on, f"/admin/members/{sam}/sign-in-code")
    assert "Sign-in code for Sam:" in page(response) and inline_code(response.text)[1] == []
    admin_code(on, admin_id)
    assert sorted((c.member_id, c.created_by) for c in codes(migrated)) == sorted(
        [(sam, admin_id), (admin_id, admin_id)]
    )
    made = audits(migrated, "member.sign_in_code_created")
    assert sorted(a.target for a in made) == sorted([f"member:{sam}", f"member:{admin_id}"])
    assert all(a.actor_member_id == admin_id for a in made)


def test_a_new_code_replaces_the_old_one_whoever_made_it(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    by_admin = admin_code(on, sam)
    sign_in_as(on, provider, *SAM)
    by_sam = my_code(on)
    assert len(codes(migrated)) == 1
    phone = device(on)
    refused = phone.post("/auth/code", data={"code": by_admin})
    assert refused.status_code == 400 and DIDNT_WORK in page(refused)
    redeem(phone, by_sam)


def test_guests_and_deactivated_members_get_no_code(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    assert submit(on, "/admin/guests", {"display_name": "Pat"}).status_code == 303
    pat = next(m for m in rows(migrated, MemberRow) if m.display_name == "Pat").id
    refused = submit(on, f"/admin/members/{pat}/sign-in-code")
    assert refused.status_code == 400 and "Guests can't sign in with a code." in page(refused)
    assert f"/admin/members/{pat}/sign-in-code" not in on.get("/admin/members").text
    assert submit(on, f"/admin/members/{sam}/deactivate").status_code == 303
    refused = submit(on, f"/admin/members/{sam}/sign-in-code")
    assert refused.status_code == 400 and "Reactivate them first." in page(refused)
    assert f"/admin/members/{sam}/sign-in-code" not in on.get("/admin/members").text
    assert submit(on, "/admin/members/9999/sign-in-code").status_code == 404
    assert codes(migrated) == [] and audits(migrated, "member.sign_in_code_created") == []


def test_only_an_admin_makes_a_code_for_someone_else(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    admin_id = member(migrated, "admin@example.com").id
    sign_in_as(on, provider, *SAM)
    assert submit(on, f"/admin/members/{admin_id}/sign-in-code").status_code == 403
    assert on.post("/preferences/sign-in-code").status_code == 403  # no CSRF token
    assert codes(migrated) == []

    async def as_a_guest() -> None:
        from dataclasses import replace

        from dinnerbell.db.repositories import MemberRepository

        database = open_database(migrated.async_database_url)
        try:
            async with database.sessionmaker() as db:
                sam_member = await MemberRepository(db).get(1, sam)
                assert sam_member is not None
                guest = replace(sam_member, role=Role.GUEST, email=None)
                with pytest.raises(Forbidden):
                    await sign_in_codes.create_my_code(db, guest, now=datetime.now(UTC))
                with pytest.raises(Forbidden):
                    await sign_in_codes.create_member_code(db, sam_member, admin_id, now=datetime.now(UTC))
        finally:
            await database.close()

    asyncio.run(as_a_guest())


# Redeeming ---------------------------------------------------------------------------------------


def test_signing_in_with_a_code_takes_two_steps_and_opens_an_ordinary_session(
    on: TestClient, sam: int, migrated: Settings, clock: FakeClock
) -> None:
    code = admin_code(on, sam)
    phone = device(on)
    form = phone.get("/auth/code")
    assert form.status_code == 200 and 'action="/auth/code"' in form.text and 'name="csrf_token"' not in form.text
    assert inline_code(form.text)[1] == []

    first = phone.post("/auth/code", data={"code": code.lower().replace("-", " ")})
    assert first.status_code == 200
    assert "You're signing in as Sam" in page(first) and 'action="/auth/code/continue"' in first.text
    assert inline_code(first.text)[1] == []
    assert codes(migrated)[0].used_at is None and sessions_of(migrated, sam) == []  # step 1 uses nothing

    second = phone.post("/auth/code/continue", data={"code": code})
    assert (second.status_code, second.headers["location"]) == (303, "/")
    cookie = second.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and f"Max-Age={90 * 24 * 3600}" in cookie
    assert "; Path=/; HttpOnly; SameSite=Lax; Secure" in cookie
    assert "Hi, Sam" in page(phone.get("/"))
    [session] = sessions_of(migrated, sam)
    assert session.expires_at == clock.now + timedelta(days=90) and session.device == "iPhone · Safari"
    assert codes(migrated)[0].used_at == clock.now
    [signed_in] = audits(migrated, "member.signed_in_with_code")
    assert (signed_in.actor_member_id, signed_in.target, signed_in.payload) == (
        sam,
        f"member:{sam}",
        {"device": "iPhone · Safari"},
    )

    again = device(on)
    for path in ("/auth/code", "/auth/code/continue"):
        refused = again.post(path, data={"code": code})  # used once
        assert refused.status_code == 400 and DIDNT_WORK in page(refused), path
    assert len(sessions_of(migrated, sam)) == 1


def test_every_failure_reads_the_same(on: TestClient, provider: FakeProvider, sam: int, migrated: Settings) -> None:
    """Wrong, malformed, used, expired, a guest's, and a deactivated member's: one message."""
    phone = device(on)
    answers = set()
    for typed in ("ABCDE-FGHJK", "nonsense", ""):
        for path in ("/auth/code", "/auth/code/continue"):
            response = phone.post(path, data={"code": typed})
            assert response.status_code == 400, (path, typed)
            answers.add(page(response).split('role="alert">', 1)[1].split("<", 1)[0])
    assert len(answers) == 1 and DIDNT_WORK in answers.pop()
    assert phone.post("/auth/code", data={"code": "A" * 33}).status_code == 400  # over the form's limit


def test_a_code_expires_after_15_minutes(on: TestClient, sam: int, migrated: Settings, clock: FakeClock) -> None:
    code = admin_code(on, sam)
    phone = device(on)
    clock.advance(timedelta(minutes=14, seconds=59))
    assert phone.post("/auth/code", data={"code": code}).status_code == 200
    clock.advance(timedelta(seconds=1))
    for path in ("/auth/code", "/auth/code/continue"):
        response = phone.post(path, data={"code": code})
        assert response.status_code == 400 and DIDNT_WORK in page(response), path
    assert sessions_of(migrated, sam) == [] and codes(migrated)[0].used_at is None


def test_a_member_deactivated_after_their_code_was_made_cant_use_it(
    on: TestClient, sam: int, migrated: Settings
) -> None:
    code = admin_code(on, sam)
    assert submit(on, f"/admin/members/{sam}/deactivate").status_code == 303
    phone = device(on)
    for path in ("/auth/code", "/auth/code/continue"):
        response = phone.post(path, data={"code": code})
        assert response.status_code == 400 and DIDNT_WORK in page(response), path
    assert sessions_of(migrated, sam) == [] and audits(migrated, "member.signed_in_with_code") == []


def test_a_guests_code_never_signs_in(on: TestClient, migrated: Settings, clock: FakeClock) -> None:
    """No route makes one, but if a row ever names a guest, both steps refuse it."""
    assert submit(on, "/admin/guests", {"display_name": "Pat"}).status_code == 303
    pat = next(m for m in rows(migrated, MemberRow) if m.display_name == "Pat").id
    code = "ABCDEFGHJK"
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO sign_in_code (household_id, member_id, code_hash, created_at, expires_at) "
                    "VALUES (1, :m, :h, :t, :e)"
                ),
                {
                    "m": pat,
                    "h": hashlib.sha256(code.encode()).hexdigest(),
                    "t": clock.now.replace(tzinfo=None).isoformat(sep=" "),
                    "e": (clock.now + timedelta(minutes=15)).replace(tzinfo=None).isoformat(sep=" "),
                },
            )
    finally:
        engine.dispose()
    phone = device(on)
    for path in ("/auth/code", "/auth/code/continue"):
        response = phone.post(path, data={"code": code})
        assert response.status_code == 400 and DIDNT_WORK in page(response), path
    assert sessions_of(migrated, pat) == []


def test_two_continues_at_once_sign_in_only_one_device(on: TestClient, sam: int, migrated: Settings) -> None:
    code = admin_code(on, sam)

    async def race() -> list[sign_in_codes.CodeSignIn | None]:
        database = open_database(migrated.async_database_url)
        now = FakeClock().now

        async def tap() -> sign_in_codes.CodeSignIn | None:
            async with database.sessionmaker() as db:
                return await sign_in_codes.redeem(db, code, now=now, replaces_token=None, user_agent=IPHONE)

        try:
            return list(await asyncio.gather(*(tap() for _ in range(4))))
        finally:
            await database.close()

    results = asyncio.run(race())
    assert [r is not None for r in results].count(True) == 1
    assert len(sessions_of(migrated, sam)) == 1 and len(audits(migrated, "member.signed_in_with_code")) == 1


def test_signing_in_replaces_the_session_the_device_had(on: TestClient, sam: int, migrated: Settings) -> None:
    """A device signed in as someone else, or carrying a planted cookie, gets a new session, and
    the old one ends (session fixation). A signed-in device sends its CSRF token too."""
    admin_id = member(migrated, "admin@example.com").id
    code = admin_code(on, sam)
    old_token = on.cookies[SESSION_COOKIE]
    assert on.post("/auth/code", data={"code": code}).status_code == 403  # signed in: CSRF token needed
    first = submit(on, "/auth/code", {"code": code})
    assert "You're signing in as Sam" in page(first)
    second = submit(on, "/auth/code/continue", {"code": code})
    assert second.status_code == 303
    assert sessions_of(migrated, admin_id) == [] and len(sessions_of(migrated, sam)) == 1
    assert on.cookies[SESSION_COOKIE] != old_token and "Hi, Sam" in page(on.get("/"))

    planted = device(on)
    planted.cookies.set(SESSION_COOKIE, "planted-token", domain="dinnerbell.test")
    redeem(planted, my_code(on))
    assert planted.cookies[SESSION_COOKIE] != "planted-token" and "Hi, Sam" in page(planted.get("/"))


def test_a_foreign_origin_is_refused(on: TestClient, sam: int, migrated: Settings) -> None:
    """The form has no session to bind a CSRF token to; OriginCheckMiddleware guards it."""
    code = admin_code(on, sam)
    phone = device(on)
    for path in ("/auth/code", "/auth/code/continue"):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}):
            assert phone.post(path, data={"code": code}, headers=headers).status_code == 403, (path, headers)
    no_origin = TestClient(on.app, base_url=BASE_URL, follow_redirects=False)
    assert no_origin.post("/auth/code/continue", data={"code": code}).status_code == 403
    assert codes(migrated)[0].used_at is None and sessions_of(migrated, sam) == []


def test_the_code_never_reaches_a_log_an_audit_row_or_a_url(
    on: TestClient, sam: int, migrated: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    code = admin_code(on, sam)
    plain = normalize_code(code)
    assert plain is not None
    phone = device(on)
    first = phone.post("/auth/code", data={"code": code})
    second = phone.post("/auth/code/continue", data={"code": code})
    phone.post("/auth/code", data={"code": code})  # a refusal too
    for record in caplog.records:
        line = record.getMessage() + repr(record.__dict__)
        assert code not in line and plain not in line, line
    for audit in rows(migrated, AuditRow):
        assert code not in repr(audit.payload) and plain not in repr(audit.payload)
        assert codes(migrated)[0].code_hash not in repr(audit.payload)
    for response in (first, second):
        assert code not in response.headers.get("location", "") and plain not in str(response.url)
    assert code not in phone.get("/").text


def test_wrong_codes_count_toward_the_per_address_limit(on: TestClient) -> None:
    """Every request under /auth/ counts, so 20 a minute from one address, wrong codes included,
    and the admin's own sign-in counted too."""
    phone = device(on)
    statuses = [
        phone.post(path, data={"code": "ABCDE-FGHJK"}).status_code
        for _ in range(12)
        for path in ("/auth/code", "/auth/code/continue")
    ]
    allowed = statuses.index(429)
    assert 0 < allowed <= 20 and set(statuses[:allowed]) == {400} and set(statuses[allowed:]) == {429}
    limited = phone.post("/auth/code", data={"code": "ABCDE-FGHJK"})
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_past_200_wrong_codes_an_hour_every_attempt_waits_and_the_admin_is_told(
    on: TestClient, sam: int, clock: FakeClock
) -> None:
    throttle: CodeThrottle = on.app.state.dinnerbell.code_throttle  # type: ignore[attr-defined]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    throttle.sleep = sleep
    assert 'id="code-throttle"' not in on.get("/admin/web-app").text
    for _ in range(200):
        throttle.failed(clock())
    code = admin_code(on, sam)
    phone = device(on)
    assert phone.post("/auth/code", data={"code": "ABCDE-FGHJK"}).status_code == 400
    assert slept == []  # the 201st wrong code is the first past the threshold
    notice = page(on.get("/admin/web-app")).split('id="code-throttle">', 1)[1].split("</p>", 1)[0]
    assert "wrong sign-in codes" in notice
    redeem(phone, code)  # slower, never locked out
    assert len(slept) == 2 and slept[1] >= slept[0] > 0
    clock.advance(timedelta(hours=1))
    assert 'id="code-throttle"' not in on.get("/admin/web-app").text


# The sign-in page --------------------------------------------------------------------------------


def test_the_sign_in_page_offers_a_code_only_while_on_and_first_in_the_app(on: TestClient) -> None:
    from dinnerbell.web.app import STATIC_DIR

    phone = device(on)
    html = phone.get("/").text
    assert 'href="/auth/login"' in html and '<a class="button block signin-code" href="/auth/code">' in html
    assert inline_code(html)[1] == []
    css = (STATIC_DIR / "app.css").read_text()
    standalone = css.split("@media (display-mode: standalone) {\n  .signin-options", 1)[1].split("}\n}", 1)[0]
    assert "order: -1" in standalone and "var(--accent)" in standalone
    switch(on, False)
    assert 'href="/auth/code"' not in phone.get("/").text


# Devices -----------------------------------------------------------------------------------------


def test_preferences_lists_my_devices_and_signs_one_out(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings, clock: FakeClock
) -> None:
    laptop = device(on, MAC_CHROME)
    assert sign_in(laptop, provider, claims={"sub": SAM[0], "email": SAM[1]}).status_code == 303
    clock.advance(timedelta(hours=2))
    phone = redeem(device(on), admin_code(on, sam))
    html = page(phone.get("/preferences"))
    cards = html.split('id="devices"', 1)[1].split("</section>", 1)[0]
    assert "iPhone · Safari" in cards and "Mac · Chrome" in cards
    assert cards.index("iPhone · Safari") < cards.index("Mac · Chrome")  # most recently seen first
    assert cards.count("This device") == 1
    assert cards.index("This device") < cards.index("Mac · Chrome")
    by_label = {s.device: s for s in sessions_of(migrated, sam)}
    for session in by_label.values():
        assert session.public_id not in (session.id,) and len(session.public_id) == 22
        assert session.id not in cards  # the token's hash never reaches a page

    signed_out = submit(phone, f"/preferences/devices/{by_label['Mac · Chrome'].public_id}/sign-out")
    assert (signed_out.status_code, signed_out.headers["location"]) == (303, "/preferences?notice=signed_out#devices")
    assert "That device is signed out." in page(phone.get("/preferences?notice=signed_out"))
    assert 'href="/auth/login"' in laptop.get("/").text  # the laptop is signed out
    assert [s.device for s in sessions_of(migrated, sam)] == ["iPhone · Safari"]

    mine = by_label["iPhone · Safari"].public_id
    response = submit(phone, f"/preferences/devices/{mine}/sign-out")
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert sessions_of(migrated, sam) == []


def test_a_member_cant_sign_out_someone_elses_device(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    [admins] = sessions_of(migrated, member(migrated, "admin@example.com").id)
    phone = redeem(device(on), admin_code(on, sam))
    for public_id in (admins.public_id, "no-such-device", "x" * 33, "bad.id"):
        assert submit(phone, f"/preferences/devices/{public_id}/sign-out").status_code == 404, public_id
    assert phone.post(f"/preferences/devices/{admins.public_id}/sign-out").status_code == 403  # no CSRF token
    assert len(sessions_of(migrated, admins.member_id)) == 1


def test_an_admin_signs_a_member_out_of_every_device(
    on: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    admin_id = member(migrated, "admin@example.com").id
    laptop = device(on, MAC_CHROME)
    sign_in(laptop, provider, claims={"sub": SAM[0], "email": SAM[1]})
    redeem(device(on), admin_code(on, sam))
    assert len(sessions_of(migrated, sam)) == 2
    members = on.get("/admin/members").text
    assert f"/admin/members/{sam}/sign-out" in members and f"/admin/members/{admin_id}/sign-out" not in members
    response = submit(on, f"/admin/members/{sam}/sign-out")
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"/admin/members?notice=signed_out#member-{sam}",
    )
    assert "Signed out of every device." in page(on.get("/admin/members?notice=signed_out"))
    assert sessions_of(migrated, sam) == [] and 'href="/auth/login"' in laptop.get("/").text
    [audit] = audits(migrated, "member.signed_out_everywhere")
    assert (audit.actor_member_id, audit.target, audit.payload) == (admin_id, f"member:{sam}", {"sessions": 2})
    assert submit(on, "/admin/members/9999/sign-out").status_code == 404
    sign_in_as(on, provider, *SAM)
    assert submit(on, f"/admin/members/{admin_id}/sign-out").status_code == 403


def test_reactivating_a_row_keeps_its_web_app_buttons(on: TestClient, sam: int) -> None:
    """The HTMX row an admin gets back has the same buttons as the page's."""
    assert submit(on, f"/admin/members/{sam}/deactivate").status_code == 303
    row = htmx(on, f"/admin/members/{sam}/reactivate", {})
    assert row.status_code == 200 and f"/admin/members/{sam}/sign-in-code" in row.text


def test_sign_in_with_slack_records_its_device_too(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    laptop = device(client, MAC_CHROME)
    sign_in(laptop, provider)
    [session] = rows(migrated, SessionRow)
    assert session.device == "Mac · Chrome" and session.public_id != session.id


def test_the_hourly_cleanup_deletes_spent_codes(on: TestClient, sam: int, migrated: Settings, clock: FakeClock) -> None:
    from dinnerbell.services.auth import purge_expired_sessions

    admin_id = member(migrated, "admin@example.com").id
    redeem(device(on), admin_code(on, sam))
    admin_code(on, admin_id)

    async def purge(now: datetime) -> None:
        database = open_database(migrated.async_database_url)
        try:
            async with database.sessionmaker() as db:
                await purge_expired_sessions(db, now=now)
        finally:
            await database.close()

    asyncio.run(purge(clock.now))
    assert [c.member_id for c in codes(migrated)] == [admin_id]  # Sam's was used
    asyncio.run(purge(clock.now + timedelta(minutes=15)))
    assert codes(migrated) == []


def test_services_refuse_while_off(migrated: Settings, client: TestClient) -> None:
    async def run() -> None:
        database = open_database(migrated.async_database_url)
        try:
            async with database.sessionmaker() as db:
                with pytest.raises(NotFound):
                    await sign_in_codes.peek(db, "ABCDE-FGHJK", now=datetime.now(UTC))
                with pytest.raises(NotFound):
                    await sign_in_codes.redeem(
                        db, "ABCDE-FGHJK", now=datetime.now(UTC), replaces_token=None, user_agent=None
                    )
        finally:
            await database.close()

    asyncio.run(run())
