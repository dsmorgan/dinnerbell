# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push below the database (#138, spec 6.15): which kinds push and to whom, what a push says,
the push service allowlist, and the adapter's encryption (RFC 8291), VAPID keys and token
(RFC 8292), and request (RFC 8030). The queue and the drainer are in ``test_push``."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from joserfc import jwt
from joserfc.jwk import ECKey

from dinnerbell.channels.webpush import encryption, sender, vapid
from dinnerbell.domain import notifications as note
from dinnerbell.domain import push
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.events import AssignmentRole, EventState, ProposalSource
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, NotifyVia, Role
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)  # 3:00 PM in Chicago
EVENT = note.EventInfo(
    7, 1, Slot.DINNER, date(2026, 10, 22), EventState.COLLECTING, 2, "/events/7", "America/Chicago",
    NOW + timedelta(minutes=90), vote_deadline=NOW + timedelta(minutes=135),
)  # fmt: skip
KIM, SAM, LEE = note.PersonInfo(3, "Kim"), note.PersonInfo(2, "Sam"), note.PersonInfo(4, "Lee")
ITEM = note.ItemInfo(1, "Siam Garden", ItemKind.RESTAURANT)
PICK = note.ProposalInfo(1, 1, ITEM, Mode.PICKUP, (), ProposalSource.SYSTEM)
DECISION = note.DecisionInfo(
    PICK, NOW + timedelta(hours=3, minutes=30), 2, (note.ParticipantInfo(KIM), note.ParticipantInfo(SAM)), ()
)


def person(**values: object) -> Member:
    base = Member(3, 1, "Kim", "kim@example.com", Role.MEMBER, Capabilities(), active=True)
    return replace(base, **values)  # type: ignore[arg-type]


# What pushes ------------------------------------------------------------------------------------


def test_every_kind_is_classified_on_purpose() -> None:
    """A new notification kind fails here until someone decides whether it pushes."""
    assert set(note.NOTIFICATION_TYPES) == push.PUSHED_KINDS | push.NOT_PUSHED_KINDS
    assert not push.PUSHED_KINDS & push.NOT_PUSHED_KINDS
    assert not push.PUSHED_KINDS & note.UPDATE_KINDS  # edits to Slack messages never push
    assert {"call_opened", "proposals_ready", "decision_made", "event_cancelled"} == push.CHANNEL_PUSHES
    for kind in push.DIRECT_PUSHES:
        cls = note.NOTIFICATION_TYPES[kind]
        assert "recipient_id" in dir(cls) and kind not in push.CHANNEL_PUSHES, kind
    for kind in ("badges_earned", "monthly_recap", "call_updated", "guest_invited"):
        assert not push.pushes(kind)


def test_a_direct_message_pushes_to_its_recipient_and_a_guests_to_the_inviter() -> None:
    reminder = note.ResponseReminder(EVENT, KIM)
    assert push.audience(reminder) == (KIM.id,)
    pat = note.PersonInfo(99, "Pat")
    made = note.AssignmentMade(EVENT, 1, pat, (AssignmentRole.PICKUP,), DECISION, via=SAM)
    assert push.audience(made) == (SAM.id,)  # the member answering for the guest, as Slack's
    assert push.audience(note.MaybeResolved(EVENT, KIM)) == ()  # not a kind that pushes


def test_channel_posts_push_to_the_members_they_concern() -> None:
    assert push.audience(note.CallOpened(EVENT, (KIM, SAM, LEE))) == (KIM.id, SAM.id, LEE.id)
    assert push.audience(note.ProposalsReady(EVENT, (), participants=(SAM, KIM))) == (SAM.id, KIM.id)
    assert push.audience(note.DecisionMade(EVENT, DECISION)) == (KIM.id, SAM.id)
    off = note.EventCancelled(EVENT, (KIM,), cancelled_by=SAM, audience=(KIM, SAM, LEE))
    assert push.audience(off) == (KIM.id, LEE.id)  # not whoever called it off
    assert push.audience(note.EventCancelled(EVENT, (KIM, SAM))) == (KIM.id, SAM.id)  # a row from before audience
    assert push.audience(note.CallAbandoned(EVENT, note.EndReason.TOO_FEW)) == ()  # type: ignore[attr-defined]


def test_only_active_members_on_the_web_app_or_both_want_pushes() -> None:
    assert push.wants_push(person(), "call_opened")  # no Slack user: the web app
    assert push.wants_push(person(slack_user_id="U03KIM", notify_via=NotifyVia.BOTH), "call_opened")
    assert not push.wants_push(person(slack_user_id="U03KIM"), "call_opened")  # Slack by default
    assert not push.wants_push(person(slack_user_id="U03KIM", notify_via=NotifyVia.SLACK), "call_opened")
    assert not push.wants_push(person(active=False), "call_opened")
    assert not push.wants_push(person(role=Role.GUEST, email=None), "call_opened")


def test_muted_reminders_dont_push_but_everything_else_does() -> None:
    muted = person(mute_dm_reminders=True)
    assert not push.wants_push(muted, "response_reminder")
    assert not push.wants_push(muted, "feedback_reminder")
    assert push.wants_push(muted, "assignment_made") and push.wants_push(muted, "call_opened")


@pytest.mark.parametrize(
    ("kind", "state", "run", "stale"),
    [
        ("call_opened", EventState.COLLECTING, 2, None),
        ("call_opened", EventState.VOTING, 2, push.MOVED_ON),
        ("call_opened", EventState.COLLECTING, 3, push.STARTED_AGAIN),
        ("call_opened", None, None, push.STARTED_AGAIN),  # discarded
        ("proposals_ready", EventState.VOTING, 2, None),
        ("proposals_ready", EventState.DECIDED, 2, push.MOVED_ON),
        ("decision_made", EventState.DECIDED, 2, None),
        ("decision_made", EventState.DONE, 2, push.MOVED_ON),
        ("decision_made", EventState.CANCELLED, 2, note.SUPERSEDED_BY_CALL_OFF),
        ("event_cancelled", EventState.CANCELLED, 2, None),
        ("event_cancelled", EventState.COLLECTING, 3, push.STARTED_AGAIN),  # started again since
    ],
)
def test_a_channel_posts_push_goes_stale_with_its_step(
    kind: str, state: EventState | None, run: int | None, stale: str | None
) -> None:
    assert push.channel_post_stale(kind, EVENT, state, run) == stale


# Subscriptions ----------------------------------------------------------------------------------

P256DH = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"

KNOWN = (
    "https://fcm.googleapis.com/fcm/send/abc:APA91b",
    "https://fcm.googleapis.com/wp/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
    "https://web.push.apple.com/QGuQyavXutnMH",
    "https://web.push.apple.com:443/QGuQ",
    "https://wns2-par02p.notify.windows.com/w/?token=BQYAAA%3d%3d",
)
UNKNOWN = (
    "http://fcm.googleapis.com/fcm/send/abc",  # not HTTPS
    "https://fcm.googleapis.com:8443/fcm/send/abc",  # another port
    "https://user:pass@fcm.googleapis.com/fcm/send/abc",
    "https://fcm.googleapis.com.example.com/x",
    "https://evil-fcm.googleapis.com/x",
    "https://push.apple.com/x",  # the bare domain isn't a push host
    "https://notapple.com/x",
    "https://xpush.apple.com/x",
    "https://169.254.169.254/latest/meta-data",
    "https://localhost/x",
    "https://dinnerbell.test/admin",
    "https://notify.windows.com.attacker.example/x",
    "https://fcm.googleapis.com/x#frag",
    "https://fcm.googleapis.com/a b",
    "https://fcm.googleapis.com/é",
    "https://fcm.googleapis.com/" + "a" * push.MAX_ENDPOINT_LENGTH,
    "javascript:alert(1)",
    "",
    "https://[::1]/x",
    "https://fcm.googleapis.com:notaport/x",
)


@pytest.mark.parametrize("endpoint", KNOWN)
def test_known_push_services_are_allowed(endpoint: str) -> None:
    assert push.known_push_service(endpoint)
    assert push.check_subscription(endpoint, P256DH, AUTH + "==") == push.SubscriptionKeys(endpoint, P256DH, AUTH)


@pytest.mark.parametrize("endpoint", UNKNOWN)
def test_anything_else_is_refused_so_dinnerbell_never_posts_there(endpoint: str) -> None:
    assert not push.known_push_service(endpoint)
    with pytest.raises(push.InvalidSubscription, match="push service"):
        push.check_subscription(endpoint, P256DH, AUTH)


@pytest.mark.parametrize(
    ("p256dh", "auth"), [("", AUTH), (P256DH, ""), (P256DH[:-4], AUTH), (P256DH, AUTH[:-2]), ("a+b/", AUTH)]
)
def test_keys_that_cant_work_are_refused(p256dh: str, auth: str) -> None:
    with pytest.raises(push.InvalidSubscription, match="keys"):
        push.check_subscription(KNOWN[0], p256dh, auth)


# What a push says -------------------------------------------------------------------------------


def payload(notification: note.Notification, now: datetime = NOW) -> dict[str, object]:
    data = json.loads(push.message(notification, now).payload("https://dinnerbell.test"))
    assert set(data) == {"web_push", "notification"} and data["web_push"] == 8030
    found: dict[str, object] = data["notification"]
    return found


def test_a_push_names_the_meal_the_time_and_what_to_do_and_opens_the_call() -> None:
    opened = payload(note.CallOpened(EVENT, (KIM, SAM)))
    assert opened == {
        "title": "Dinner: are you in?",
        "body": "Answer by 4:30 PM.",
        "navigate": "https://dinnerbell.test/events/7",
        "lang": "en",
        "dir": "ltr",
    }
    assert payload(note.ProposalsReady(EVENT, ()))["body"] == "Voting closes at 5:15 PM."
    assert payload(note.DecisionMade(EVENT, DECISION))["body"] == "Siam Garden, at 6:30 PM."
    made = payload(note.AssignmentMade(EVENT, 1, KIM, (AssignmentRole.PICKUP, AssignmentRole.COORDINATOR), DECISION))
    assert made["title"] == "Dinner: you're picking up and coordinating"
    tomorrow = payload(note.ResponseReminder(EVENT, KIM), NOW - timedelta(days=1))
    assert tomorrow["body"] == "Answer by Thursday 4:30 PM."
    joined = payload(note.GuestJoined(1, KIM, note.PersonInfo(9, "Pat"), "web"))
    assert joined["navigate"] == "https://dinnerbell.test/admin/members"


def test_a_push_never_names_anyones_answer_or_note() -> None:
    off = note.EventCancelled(EVENT, (KIM,), cancelled_by=SAM, note="Zq7x sick", audience=(KIM, SAM))
    every = [
        note.CallOpened(EVENT, (KIM, SAM), roster=(note.RosterEntry(SAM, note.Status.IN, note="Zq7x"),)),  # type: ignore[attr-defined]
        note.ProposalsReady(EVENT, (PICK,), participants=(KIM, SAM)),
        note.DecisionMade(EVENT, DECISION, locked_by=SAM),
        off,
        note.RoleVacated(EVENT, KIM, note.PersonInfo(9, "Pat"), (AssignmentRole.COOK,)),
        note.CoordinatingGiven(EVENT, KIM, SAM, NOW),
        note.AssignmentEnded(EVENT, KIM, (AssignmentRole.COOK,), SAM, NOW),
        note.RolesToChoose(EVENT, KIM, DECISION, NOW + timedelta(minutes=15)),
        note.CoordinatorPicked(EVENT, KIM),
        note.FeedbackReminder(EVENT, KIM, "Siam Garden", date(2026, 10, 29)),
    ]
    for notification in every:
        assert notification.kind in push.PUSHED_KINDS
        text = json.dumps(payload(notification))
        assert "Sam" not in text and "Zq7x" not in text and "Pat" not in text, notification.kind


def test_a_long_payload_is_cut_to_fit_one_record() -> None:
    long = note.FeedbackReminder(EVENT, KIM, "Tacos " * 2000, date(2026, 10, 29))
    body = push.message(long, NOW).payload("https://dinnerbell.test")
    assert len(body) <= push.MAX_PAYLOAD_BYTES <= encryption.MAX_PLAINTEXT
    assert json.loads(body)["notification"]["body"].endswith("…")


def test_ttl_lasts_until_the_next_deadline_within_a_minute_and_a_day() -> None:
    assert push.ttl(note.CallOpened(EVENT, ()), NOW) == 90 * 60
    assert push.ttl(note.CallOpened(EVENT, ()), NOW + timedelta(minutes=90)) == 45 * 60  # the vote's
    assert push.ttl(note.ResponseReminder(EVENT, KIM), NOW + timedelta(minutes=89, seconds=50)) == 60
    assert push.ttl(note.DecisionMade(EVENT, DECISION), NOW + timedelta(hours=3)) == 30 * 60
    assert push.ttl(note.GuestJoined(1, KIM, KIM, "web"), NOW) == 24 * 3600
    assert push.ttl(note.CallOpened(EVENT, ()), NOW + timedelta(days=2)) == 24 * 3600  # nothing ahead


def test_urgency_and_one_topic_per_call_run() -> None:
    assert push.urgency("call_opened") == "high"
    assert {push.urgency(k) for k in push.PUSHED_KINDS - {"call_opened"}} == {"normal"}
    topic = push.topic_for(note.CallOpened(EVENT, ()))
    assert topic is not None and len(topic) <= 32
    assert set(topic) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert push.topic_for(note.EventCancelled(EVENT)) == topic  # a call off replaces "are you in?"
    assert push.topic_for(note.CallOpened(replace(EVENT, run=3), ())) != topic
    assert push.topic_for(note.GuestJoined(1, KIM, KIM, "web")) is None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, push.Outcome.SENT),
        (404, push.Outcome.GONE),
        (410, push.Outcome.GONE),
        (401, push.Outcome.REJECTED),
        (403, push.Outcome.REJECTED),
        (413, push.Outcome.FAILED),
        (400, push.Outcome.FAILED),
        (429, push.Outcome.RETRY),
        (500, push.Outcome.RETRY),
        (503, push.Outcome.RETRY),
    ],
)
def test_each_answer_from_the_push_service(status: int, outcome: push.Outcome) -> None:
    assert push.outcome(status) is outcome


# RFC 8291 ---------------------------------------------------------------------------------------


def test_encryption_matches_the_rfc_8291_example() -> None:
    """RFC 8291 section 5 and appendix A: the same keys and salt give the same bytes."""
    server = ec.derive_private_key(
        int.from_bytes(encryption.b64decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1()
    )
    body = encryption.encrypt(
        b"When I grow up, I want to be a watermelon",
        P256DH,
        AUTH,
        salt=encryption.b64decode("DGv6ra1nlYgDCS1FRnbzlw"),
        private_key=server,
    )
    header = (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    ciphertext = "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    assert vapid.b64url(body[: encryption.HEADER_SIZE]) == header
    assert vapid.b64url(body[encryption.HEADER_SIZE :]) == ciphertext


def decrypt(body: bytes, receiver: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """The user agent's side of RFC 8291, to check a round trip with fresh keys."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    def mac(key: bytes, data: bytes) -> bytes:
        return hmac.new(key, data, hashlib.sha256).digest()

    salt, as_public = body[:16], body[21:86]
    ua_public = receiver.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = receiver.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = mac(mac(auth, shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = mac(salt, ikm)
    cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    padded = AESGCM(cek).decrypt(nonce, body[86:], None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


def test_a_fresh_push_decrypts_and_never_repeats_its_salt_or_key() -> None:
    from cryptography.hazmat.primitives import serialization

    browser = ec.generate_private_key(ec.SECP256R1())
    point = browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    message = push.message(note.CallOpened(EVENT, ()), NOW).payload("https://dinnerbell.test")
    first = encryption.encrypt(message, vapid.b64url(point), vapid.b64url(auth))
    second = encryption.encrypt(message, vapid.b64url(point), vapid.b64url(auth))
    assert decrypt(first, browser, auth) == message == decrypt(second, browser, auth)
    assert first[:16] != second[:16] and first[21:86] != second[21:86]
    assert len(first) <= encryption.RECORD_SIZE
    with pytest.raises(ValueError, match="at most"):
        encryption.encrypt(b"x" * (encryption.MAX_PLAINTEXT + 1), vapid.b64url(point), vapid.b64url(auth))


# VAPID ------------------------------------------------------------------------------------------

SECRET = "test-secret-key-that-is-long-enough-to-pass-validation"


def test_the_key_pair_comes_from_the_secret_key_alone() -> None:
    first, again = vapid.VapidKeys.from_secret(SECRET), vapid.VapidKeys.from_secret(SECRET)
    assert first.public_key == again.public_key
    assert vapid.VapidKeys.from_secret(SECRET + "x").public_key != first.public_key
    point = base64.urlsafe_b64decode(first.public_key + "=")
    assert len(point) == 65 and point[0] == 4  # uncompressed P-256, as applicationServerKey takes
    assert first.private_key.private_numbers().private_value == vapid.private_value(SECRET)


@pytest.mark.parametrize("secret", [SECRET, "", "a", "é" * 40, "z" * 500])
def test_the_private_key_is_always_in_range(secret: str) -> None:
    value = vapid.private_value(secret)
    assert 1 <= value <= vapid.P256_ORDER - 1
    assert value == vapid.private_value(secret)


def test_the_reduction_maps_the_edges_into_range() -> None:
    """48 bytes reduced mod (n - 1) plus 1: 0 gives 1, and n - 2 gives n - 1."""
    n = vapid.P256_ORDER
    assert 0 % (n - 1) + 1 == 1 and (n - 2) % (n - 1) + 1 == n - 1 and (n - 1) % (n - 1) + 1 == 1


def test_the_token_names_the_push_service_expires_in_12_hours_and_never_an_email() -> None:
    keys = vapid.VapidKeys.from_secret(SECRET)
    token = keys.token("https://web.push.apple.com/QGuQyavXutnMH?x=1", "https://dinnerbell.test", NOW)
    decoded = jwt.decode(token, ECKey.import_key(keys.private_key.public_key()))
    assert decoded.header == {"typ": "JWT", "alg": "ES256"}
    assert decoded.claims == {
        "aud": "https://web.push.apple.com",
        "exp": int((NOW + timedelta(hours=12)).timestamp()),
        "sub": "https://dinnerbell.test",
    }
    header = keys.authorization("https://fcm.googleapis.com/fcm/send/a", "https://dinnerbell.test", NOW)
    assert header.startswith("vapid t=") and header.endswith(f", k={keys.public_key}")


# The request ------------------------------------------------------------------------------------


async def test_the_sender_posts_one_encrypted_push_with_the_rfc_8030_headers() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    keys = vapid.VapidKeys.from_secret(SECRET)
    client = sender.new_client(httpx.MockTransport(answer))
    target = push.PushTarget(5, "https://fcm.googleapis.com/fcm/send/abc", P256DH, AUTH)
    response = await sender.WebPushSender(keys, "https://dinnerbell.test", client, FakeClock(NOW)).send(
        target, b'{"web_push":8030}', ttl=600, urgency="high", topic="abc"
    )
    await client.aclose()
    assert response == push.PushResponse(429, 120.0)
    (request,) = seen
    assert request.method == "POST" and str(request.url) == target.endpoint
    assert (request.headers["TTL"], request.headers["Urgency"], request.headers["Topic"]) == ("600", "high", "abc")
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["Authorization"].startswith("vapid t=")
    assert len(request.content) == encryption.HEADER_SIZE + len(b'{"web_push":8030}') + 17


async def test_the_sender_never_follows_a_redirect_or_posts_off_the_allowlist() -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://169.254.169.254/"})

    keys = vapid.VapidKeys.from_secret(SECRET)
    client = sender.new_client(httpx.MockTransport(answer))
    push_sender = sender.WebPushSender(keys, "https://dinnerbell.test", client, FakeClock(NOW))
    target = push.PushTarget(5, "https://fcm.googleapis.com/fcm/send/abc", P256DH, AUTH)
    assert (await push_sender.send(target, b"{}", ttl=60, urgency="normal", topic=None)).status == 307
    assert seen == [target.endpoint]
    with pytest.raises(sender.UnknownPushService, match="subscription 6"):
        await push_sender.send(
            push.PushTarget(6, "https://dinnerbell.test/x", P256DH, AUTH), b"{}", ttl=60, urgency="normal", topic=None
        )
    assert seen == [target.endpoint]
    await client.aclose()


def test_retry_after_is_seconds_or_a_date() -> None:
    assert sender.retry_after("30", NOW) == 30.0
    assert sender.retry_after("Thu, 22 Oct 2026 20:02:00 GMT", NOW) == 120.0
    assert sender.retry_after("Thu, 22 Oct 2026 19:00:00 GMT", NOW) == 0.0
    assert sender.retry_after("soon", NOW) is None and sender.retry_after(None, NOW) is None
