# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web **Assistant** page (ADR 0009, spec 6.14): gating, one private conversation per
member, sending with and without HTMX, Conversation hours 0, draft cards with Confirm and
Cancel, the add-only catalog import, and reply text that never becomes HTML. The model is
``FakeModel``; the browser is the real test client with HTTPS, CSRF, and Origin checks."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.app_factory import create_app
from dinnerbell.config import SLACK_ISSUER, Settings
from dinnerbell.db.models import AssistantConversationRow, AssistantDraftRow, CatalogItemRow, EventRow, MemberRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventRuleViolation, EventState, IllegalTransition, Proposal, ProposalSource
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.offers import OfferPath
from dinnerbell.services.assistant import drafts
from dinnerbell.services.assistant.call_drafts import ActionConfirmed
from dinnerbell.services.assistant.conversation import FAILED
from dinnerbell.services.assistant.drafts import EXPIRED, NO_SUCH_DRAFT, USED, OfferConfirmed, ProposalConfirmed
from dinnerbell.services.database import open_database
from dinnerbell.services.household import current_household
from dinnerbell.services.offers import Posted
from dinnerbell.web.routes import assistant_page
from tests.conftest import BASE_URL, csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, failure, tool_call, usage
from tests.support.fake_oidc import FakeProvider
from tests.support.links import MENU_LINK, link_draft
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import rows

KEY = "sk-ant-test-not-a-real-key"
HX = {"HX-Request": "true"}
SLACK_TEAM = "T-HOUSE"
SLACK_ADMIN = {"sub": "subject-admin", "https://slack.com/team_id": SLACK_TEAM, "https://slack.com/user_id": "U01ADMIN"}
SETTINGS_ON = {"enabled": "on", "cap_dollars": "5", "conversation_hours": "24"}
CATALOG = """version: 1
restaurants:
  - name: Noodle Bar
    cuisine: [Chinese]
    notes: From the draft
home_meals:
  - name: Lentil Soup
    cuisine: [Indian]
"""


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY}


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock, settings: Settings) -> FakeProvider:
    """The configured provider: Sign in with Slack for the guest test, the fake issuer otherwise."""
    return FakeProvider(key=signing_key, clock=clock, issuer=settings.oidc_issuer)


@pytest.fixture
def client(migrated: Settings, clock: FakeClock, provider: FakeProvider, model: FakeModel) -> Iterator[TestClient]:
    """The app with the fake model in place of Anthropic."""
    app = create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app), model_client=model)
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False) as test_client:
        yield test_client


def as_who(client: TestClient, provider: FakeProvider, who: str) -> TestClient:
    """Sign in as ``who`` ("admin", "kim", or "jo") in a fresh browser."""
    client.cookies.clear()
    email = f"{who}@example.com"
    response = sign_in(client, provider, claims={"sub": f"subject-{who}", "email": email})
    assert response.status_code == 303, page(response)
    return client


def set_assistant(client: TestClient, **values: str) -> None:
    response = submit(client, "/admin/assistant", SETTINGS_ON | values)
    assert response.status_code == 303, page(response)


def furnish(client: TestClient, provider: FakeProvider, *, switch: bool = True, **values: str) -> TestClient:
    """The admin invites Kim and Jo, adds Siam Garden, and turns the assistant on."""
    as_who(client, provider, "admin")
    assert invite(client, "kim@example.com", "Kim").status_code == 303
    assert invite(client, "jo@example.com", "Jo").status_code == 303
    assert add_item(client, "Siam Garden").status_code == 303
    if switch:
        set_assistant(client, **values)
    return client


def send(client: TestClient, text: str, **headers: str) -> httpx.Response:
    return client.post("/assistant", data={"message": text, "csrf_token": csrf_from(client)}, headers=headers)


def draft_ids(html: str) -> list[int]:
    return [int(n) for n in re.findall(r'id="draft-(\d+)"', html)]


def offer_tool(name: str = "Siam Garden") -> dict[str, Any]:
    return {"name": name, "mode": "pickup", "slot": "dinner", "day": "today", "time": "", "spare_servings": 2}


def member_id(settings: Settings, name: str) -> int:
    return next(m.id for m in rows(settings, MemberRow) if m.display_name == name)


ROUTES = (
    ("post", "/assistant"),
    ("post", "/assistant/start-over"),
    ("post", "/assistant/drafts/1/confirm"),
    ("post", "/assistant/drafts/1/cancel"),
)


def closed(client: TestClient) -> None:
    """No link, and every route, a POST with a good CSRF token included, is a 404."""
    assert 'href="/assistant"' not in client.get("/").text
    assert client.get("/assistant").status_code == 404
    token = csrf_from(client)
    for _, path in ROUTES:
        response = client.post(path, data={"message": "Hi", "csrf_token": token})
        assert response.status_code == 404, path


# Gating ----------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=False)
def test_with_the_flag_off_there_is_no_link_and_no_page(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider, switch=False)
    closed(client)
    closed(as_who(client, provider, "kim"))
    assert model.requests == []


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("settings_overrides", [{}])
def test_without_the_key_there_is_no_link_and_no_page(
    client: TestClient, provider: FakeProvider, settings: Settings
) -> None:
    assert settings.anthropic_api_key is None
    furnish(client, provider, switch=False)
    closed(client)
    closed(as_who(client, provider, "kim"))


@pytest.mark.flag("assistant", on=True)
def test_with_the_switch_off_there_is_no_link_and_no_page(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider, switch=False)
    closed(client)
    closed(as_who(client, provider, "kim"))
    # Turned on, then off again: closed again, and the Admin tab is still there.
    set_assistant(as_who(client, provider, "admin"))
    assert 'href="/assistant"' in client.get("/").text
    submit(client, "/admin/assistant", {"cap_dollars": "5", "conversation_hours": "24"})
    closed(client)
    assert model.requests == []


@pytest.mark.flag("assistant", on=True)
def test_with_everything_set_members_and_admins_see_the_link(client: TestClient, provider: FakeProvider) -> None:
    furnish(client, provider)
    for who in ("admin", "kim"):
        as_who(client, provider, who)
        assert 'href="/assistant"' in client.get("/catalog").text
        shown = client.get("/assistant")
        assert shown.status_code == 200
        assert '<a href="/assistant" aria-current="page">Assistant</a>' in shown.text


@pytest.mark.flag("assistant", on=True)
def test_a_deactivated_member_is_signed_out_and_never_answered(
    client: TestClient, provider: FakeProvider, migrated: Settings, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    token = csrf_from(client)
    kim_cookies = dict(client.cookies)
    as_who(client, provider, "admin")
    kim = member_id(migrated, "Kim")
    assert submit(client, f"/admin/members/{kim}/deactivate").status_code == 303
    client.cookies.clear()
    client.cookies.update(kim_cookies)
    assert client.get("/assistant").status_code == 303
    response = client.post("/assistant", data={"message": "Hi", "csrf_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert model.requests == [] and rows(migrated, AssistantConversationRow) == []


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    "settings_overrides", [{"anthropic_api_key": KEY, "slack_team_id": SLACK_TEAM, "oidc_issuer": SLACK_ISSUER}]
)
def test_a_guest_is_refused_and_never_answered(
    client: TestClient, provider: FakeProvider, migrated: Settings, model: FakeModel
) -> None:
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    set_assistant(client)
    assert submit(client, "/admin/guests", {"display_name": "Pat"}).status_code == 303
    guest_id = int(re.findall(r'id="guest-(\d+)"', client.get("/admin/members").text)[-1])
    assert submit(client, f"/admin/guests/{guest_id}/slack", {"slack_user_id": "U07PAT"}).status_code == 303
    client.cookies.clear()
    claims = {
        "sub": "sub-U07PAT",
        "email": "u07pat@elsewhere.example",
        "https://slack.com/team_id": SLACK_TEAM,
        "https://slack.com/user_id": "U07PAT",
    }
    assert sign_in(client, provider, claims=claims).status_code == 303
    assert 'href="/assistant"' not in client.get("/").text
    assert client.get("/assistant").status_code == 403
    token = csrf_from(client)
    for _, path in ROUTES:
        assert client.post(path, data={"message": "Hi", "csrf_token": token}).status_code == 403, path
    assert model.requests == [] and rows(migrated, AssistantConversationRow) == []


@pytest.mark.flag("assistant", on=True)
def test_sending_needs_the_csrf_token(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    assert client.post("/assistant", data={"message": "Hi"}).status_code == 403
    assert client.post("/assistant", data={"message": "Hi", "csrf_token": "wrong"}).status_code == 403
    assert model.requests == []


# Sending ---------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
def test_a_message_gets_a_reply_with_htmx_and_history_survives_a_reload(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    empty = page(client.get("/assistant"))
    assert "No messages yet." in empty and 'role="log"' in empty and 'aria-live="polite"' in empty
    assert "keeps this conversation for 24 hours" in empty
    model.script = [answer("Siam Garden is open tonight, Member 1.")]
    response = send(client, "What Thai places do we have?", **HX)
    assert response.status_code == 200 and "HX-Reswap" not in response.headers
    fragment = page(response)
    assert "<html" not in fragment
    assert "What Thai places do we have?" in fragment
    assert "Siam Garden is open tonight, Kim." in fragment  # names put back for display
    assert "Kim" not in model.sent  # and never sent
    model.script = [answer("Try Siam Garden.")]
    send(client, "Something cheaper?", **HX)
    reloaded = page(client.get("/assistant"))
    turns = re.findall(r'<p class="turn-text">(.*?)</p>', reloaded, re.S)
    assert turns == [
        "What Thai places do we have?",
        "Siam Garden is open tonight, Kim.",
        "Something cheaper?",
        "Try Siam Garden.",
    ]
    assert reloaded.count("turn turn-mine") == 2 and reloaded.count("turn turn-reply") == 2
    # The follow-up went out with the earlier exchange.
    assert "What Thai places do we have?" in str(model.requests[-1]["messages"])
    [row] = rows(migrated, AssistantConversationRow)
    assert row.key == f"web:{member_id(migrated, 'Kim')}" and row.private


@pytest.mark.flag("assistant", on=True)
def test_without_javascript_a_kept_exchange_redirects_and_shows_on_the_page(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [answer("We have Siam Garden.")]
    response = send(client, "What Thai places do we have?")
    assert response.status_code == 303 and response.headers["location"] == "/assistant#ask"
    shown = page(client.get("/assistant"))
    assert "What Thai places do we have?" in shown and "We have Siam Garden." in shown
    # The form posts without HTMX too, and HTMX gives it a longer wait than other requests.
    assert 'method="post" action="/assistant"' in shown and '"timeout": 120000' in shown
    assert 'hx-disabled-elt="find button"' in shown and "data-reset-on-success" in shown
    assert '<label class="field">\n      <span>Message</span>\n      <textarea name="message"' in shown
    # Enter sends from a keyboard, and the help line says so (#285).
    assert "data-enter-sends" in shown and "Enter sends and Shift+Enter starts a new line" in shown
    script = client.get("/static/app.js").text
    assert '"data-enter-sends"' in script and "requestSubmit" in script and "isComposing" in script
    assert "event.shiftKey" in script and "(pointer: coarse)" in script


@pytest.mark.flag("assistant", on=True)
def test_the_cores_messages_show_as_they_are_and_arent_kept(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [failure(maybe_billed=False)]
    shown = send(client, "What Thai places do we have?")
    assert shown.status_code == 200 and FAILED in page(shown)  # shown once, not kept
    assert "What Thai places do we have?" in page(shown)
    assert FAILED not in page(client.get("/assistant"))
    assert rows(migrated, AssistantConversationRow) == []
    long = send(client, "x" * 4_001, **HX)
    assert "That message is too long for me." in page(long)
    # Line breaks arrive as CRLF; well past the service's limit, the reply still says so.
    longer = send(client, "line\r\n" * 2_500, **HX)
    assert longer.status_code == 200 and "That message is too long for me." in page(longer)
    assert 'maxlength="4000"' in client.get("/assistant").text


@pytest.mark.flag("assistant", on=True)
def test_turned_off_while_sending_is_a_404_so_the_text_stays(
    client: TestClient, provider: FakeProvider, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def switched_off(*_args: Any, **_kwargs: Any) -> None:
        return None  # what converse returns once the switch is off

    monkeypatch.setattr(assistant_page, "converse", switched_off)
    assert send(client, "Hello", **HX).status_code == 404
    assert send(client, "Hello").status_code == 404


@pytest.mark.flag("assistant", on=True)
def test_at_the_cap_the_reply_says_so(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider, cap_dollars="1")
    as_who(client, provider, "kim")
    model.script = [answer("A costly answer.", parts=(usage(input_tokens=900_000, output_tokens=0),))]
    assert "A costly answer." in page(send(client, "Hello", **HX))  # billed past the $1 cap
    at_cap = page(send(client, "Hello again", **HX))
    assert "reached this month's spending limit" in at_cap and "Hello again" in at_cap
    assert len(model.requests) == 1


@pytest.mark.flag("assistant", on=True)
def test_an_empty_message_sends_nothing(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    assert send(client, "   ", **HX).status_code == 204
    response = send(client, "\r\n")
    assert response.status_code == 303 and response.headers["location"] == "/assistant"
    assert model.requests == []


@pytest.mark.flag("assistant", on=True)
def test_start_over_deletes_the_conversation_and_cancels_its_drafts(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Here's a draft.")]
    send(client, "I'll pick up Siam Garden tonight", **HX)
    assert "Start over" in page(client.get("/assistant"))
    response = submit(client, "/assistant/start-over")
    assert response.status_code == 303 and response.headers["location"] == "/assistant?notice=cleared"
    shown = page(client.get("/assistant?notice=cleared"))
    assert "Started over." in shown and "No messages yet." in shown
    assert "pick up Siam Garden" not in shown and draft_ids(shown) == []
    assert rows(migrated, AssistantConversationRow) == []
    [draft] = rows(migrated, AssistantDraftRow)
    assert draft.used_at is not None


@pytest.mark.flag("assistant", on=True)
def test_one_member_never_sees_anothers_conversation(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Kim's secret plan is drafted.")]
    send(client, "Plan a surprise dinner for Jo", **HX)
    [kims_draft] = draft_ids(page(client.get("/assistant")))
    as_who(client, provider, "jo")
    shown = page(client.get("/assistant"))
    assert "surprise" not in shown and "secret plan" not in shown and draft_ids(shown) == []
    model.script = [answer("Hello, Jo.")]
    send(client, "Hi", **HX)
    assert "surprise" not in str(model.requests[-1])  # Kim's history never goes out for Jo
    theirs = htmx(client, f"/assistant/drafts/{kims_draft}/confirm", {})
    assert theirs.status_code == 409 and NO_SUCH_DRAFT in page(theirs)
    # Jo starting over leaves Kim's conversation alone.
    submit(client, "/assistant/start-over")
    keys = {row.key for row in rows(migrated, AssistantConversationRow)}
    assert keys == {f"web:{member_id(migrated, 'Kim')}"}
    as_who(client, provider, "kim")
    assert "surprise" in page(client.get("/assistant"))


# Conversation hours 0 --------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
def test_at_hours_zero_nothing_is_kept_and_only_the_latest_exchange_shows(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider, conversation_hours="0")
    as_who(client, provider, "kim")
    assert "Nothing is kept" in page(client.get("/assistant"))
    model.script = [answer("First answer."), answer("Second answer."), answer("Third answer.")]
    first = send(client, "First question")
    assert first.status_code == 200 and "First answer." in page(first)
    second = page(send(client, "Second question"))
    assert "Second answer." in second and "First" not in second and "Nothing is kept" in second
    third = send(client, "Third question", **HX)
    assert third.headers["HX-Reswap"] == "innerHTML" and "Third answer." in page(third)
    assert "First" not in page(client.get("/assistant")) and "Third" not in page(client.get("/assistant"))
    assert rows(migrated, AssistantConversationRow) == []
    assert "Start over" not in page(client.get("/assistant"))


@pytest.mark.flag("assistant", on=True)
def test_at_hours_zero_waiting_drafts_stay_on_the_page(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider, conversation_hours="0")
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Drafted."), answer("Anything else?")]
    send(client, "I'll pick up Siam Garden", **HX)
    later = page(send(client, "Thanks", **HX))  # replaces the log, so it carries the waiting card
    assert len(draft_ids(later)) == 1 and "Anything else?" in later
    assert len(draft_ids(page(client.get("/assistant")))) == 1


# Draft cards -----------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
def test_an_offer_draft_card_confirms_with_htmx(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Here's a draft for you.")]
    fragment = page(send(client, "I'll pick up Siam Garden tonight", **HX))
    [draft] = draft_ids(fragment)
    assert "Offer to pick up Siam Garden" in fragment and "Confirm will:" in fragment
    assert f'hx-post="/assistant/drafts/{draft}/confirm"' in fragment
    on_load = page(client.get("/assistant"))
    assert draft_ids(on_load) == [draft] and "Offer to pick up Siam Garden" in on_load
    assert rows(migrated, EventRow) == []
    confirmed = htmx(client, f"/assistant/drafts/{draft}/confirm", {})
    assert confirmed.status_code == 200
    card = page(confirmed)
    assert "<html" not in card and f'id="draft-{draft}"' in card
    assert "Confirmed. Your dinner offer for today is posted." in card and "Confirm</button>" not in card
    [event] = rows(migrated, EventRow)
    assert f'href="/events/{event.id}"' in card
    assert draft_ids(page(client.get("/assistant"))) == []  # used, so no longer waiting
    again = htmx(client, f"/assistant/drafts/{draft}/confirm", {})
    assert again.status_code == 409 and USED in page(again)
    assert len(rows(migrated, EventRow)) == 1


@pytest.mark.flag("assistant", on=True)
def test_a_preference_draft_confirms_with_htmx_and_links_to_preferences(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    """#253: the member's own data has no call, so the card links to Preferences instead."""
    furnish(client, provider)
    as_who(client, provider, "kim")
    level = {"what": "", "name": "Siam Garden", "level": "2"}
    model.script = [tool_call("draft_preference", level), answer("Here's a draft.")]
    fragment = page(send(client, "I love Siam Garden", **HX))
    [draft] = draft_ids(fragment)
    assert "Set Siam Garden to +2" in fragment
    assert "Your level for the restaurant Siam Garden on Preferences goes from 0 to +2." in fragment
    confirmed = htmx(client, f"/assistant/drafts/{draft}/confirm", {})
    assert confirmed.status_code == 200
    card = page(confirmed)
    assert "Confirmed. Your level for Siam Garden is +2 now." in card and 'href="/preferences">Open Preferences' in card
    assert 'value="2" checked' in page(client.get("/preferences"))


@pytest.mark.flag("assistant", on=True)
def test_cancel_works_with_and_without_htmx(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Drafted.")]
    send(client, "Offer Siam Garden", **HX)
    [draft] = draft_ids(page(client.get("/assistant")))
    cancelled = htmx(client, f"/assistant/drafts/{draft}/cancel", {})
    assert cancelled.status_code == 200 and "Cancelled. Nothing changed." in page(cancelled)
    assert draft_ids(page(client.get("/assistant"))) == []
    refused = submit(client, f"/assistant/drafts/{draft}/confirm")
    assert refused.status_code == 409 and USED in page(refused) and "<html" in refused.text
    assert rows(migrated, EventRow) == []
    again = submit(client, f"/assistant/drafts/{draft}/cancel")
    assert again.status_code == 409 and USED in page(again)


@pytest.mark.flag("assistant", on=True)
def test_confirm_without_javascript_shows_the_result_on_the_page(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Drafted.")]
    send(client, "Offer Siam Garden")
    [draft] = draft_ids(page(client.get("/assistant")))
    confirmed = submit(client, f"/assistant/drafts/{draft}/confirm")
    assert confirmed.status_code == 200 and "<html" in confirmed.text
    shown = '<p class="alert assistant-outcome ok" role="status">Confirmed. Your dinner offer for today is posted.'
    assert shown in page(confirmed)
    assert len(rows(migrated, EventRow)) == 1


@pytest.mark.flag("assistant", on=True)
def test_an_expired_draft_and_another_members_draft_are_not_found(
    client: TestClient, provider: FakeProvider, model: FakeModel, clock: FakeClock, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Drafted.")]
    send(client, "Offer Siam Garden", **HX)
    [draft] = draft_ids(page(client.get("/assistant")))
    as_who(client, provider, "jo")
    for path in ("confirm", "cancel"):
        theirs = htmx(client, f"/assistant/drafts/{draft}/{path}", {})
        assert theirs.status_code == 409 and NO_SUCH_DRAFT in page(theirs)
        assert "Offer to pick up" not in page(theirs)  # nothing of Kim's draft shows
    missing = submit(client, "/assistant/drafts/999999/confirm")
    assert missing.status_code == 409 and NO_SUCH_DRAFT in page(missing)
    as_who(client, provider, "kim")
    clock.advance(timedelta(minutes=31))
    expired = htmx(client, f"/assistant/drafts/{draft}/confirm", {})
    assert expired.status_code == 409 and EXPIRED in page(expired)
    assert rows(migrated, EventRow) == []
    assert client.post("/assistant/drafts/0/confirm", data={"csrf_token": csrf_from(client)}).status_code == 404


@pytest.mark.flag("assistant", on=True)
def test_a_refusal_from_the_service_shows_as_it_is_and_keeps_the_card(
    client: TestClient, provider: FakeProvider, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_offer", offer_tool()), answer("Drafted.")]
    send(client, "Offer Siam Garden", **HX)
    [draft] = draft_ids(page(client.get("/assistant")))

    async def refuse(*_args: Any) -> Any:
        raise EventRuleViolation("Voting on that call closed at 5:00 PM.")

    monkeypatch.setattr(drafts, "confirm_draft", refuse)
    refused = htmx(client, f"/assistant/drafts/{draft}/confirm", {})
    card = page(refused)
    assert refused.status_code == 409
    assert '<p class="alert error" role="alert">Voting on that call closed at 5:00 PM.' in card
    assert "Confirm</button>" in card and "Cancel</button>" in card  # still waiting, so Cancel works

    async def lost_a_race(*_args: Any) -> Any:
        raise IllegalTransition(EventState.DONE, EventState.VOTING)

    monkeypatch.setattr(drafts, "confirm_draft", lost_a_race)
    assert htmx(client, f"/assistant/drafts/{draft}/confirm", {}).status_code == 409


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    ("result", "said"),
    [
        (ProposalConfirmed(7, Proposal(3, 7, Mode.PICKUP, 1, ProposalSource.MEMBER)), "Your proposal is on the call."),
        (
            OfferConfirmed(Posted(OfferPath.ATTACH, 7, Slot.DINNER, date(2026, 10, 22), "Siam Garden")),
            "Siam Garden is a pick on today's dinner call",
        ),
        (
            OfferConfirmed(Posted(OfferPath.REPLACE, 7, Slot.DINNER, date(2026, 10, 23), "Siam Garden")),
            "replaces that day's scheduled dinner call",
        ),
        (ActionConfirmed(7, "Confirmed. You're in for today's dinner call."), "You're in for today's dinner call."),
        (ActionConfirmed(7, "Confirmed. Your offer is called off.", offer=True), "Open the offer"),
    ],
)
def test_each_confirmed_result_says_what_happened_and_links_it(
    client: TestClient,
    provider: FakeProvider,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
    said: str,
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def confirmed(*_args: Any) -> Any:
        return result

    monkeypatch.setattr(drafts, "confirm_draft", confirmed)
    card = page(htmx(client, "/assistant/drafts/5/confirm", {}))
    assert said in card and 'href="/events/7"' in card


@pytest.mark.flag("assistant", on=True)
def test_a_catalog_confirm_opens_an_add_only_preview_that_never_updates_a_match(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [tool_call("draft_catalog_items", {"catalog_file": CATALOG}), answer("Drafted two items.")]
    fragment = page(send(client, "Add Noodle Bar and lentil soup", **HX))
    [draft] = draft_ids(fragment)
    assert "Add 2 items to the catalog" in fragment
    assert f'hx-post="/assistant/drafts/{draft}/confirm"' not in fragment  # a whole page, never a swap
    assert f'action="/assistant/drafts/{draft}/confirm"' in fragment
    # A matching item appears after the draft.
    assert add_item(client, "Noodle Bar", cuisines="Chinese").status_code == 303
    preview = submit(client, f"/assistant/drafts/{draft}/confirm")
    assert preview.status_code == 200
    shown = page(preview)
    assert "This import comes from an assistant draft, so it only adds new items." in shown
    assert '<a href="/catalog" aria-current="page">Catalog</a>' in shown  # the nav says Catalog
    assert 'aria-current="page">Assistant' not in shown
    assert "Add 1 item" in shown and "Leave 1 matching item" in shown and "Update" not in shown
    content = re.search(r'<textarea name="content"[^>]*>\n?(.*?)</textarea>', shown, re.S)
    token = re.search(r'name="token" value="([^"]+)"', preview.text)
    assert content and token
    # Apply carries only the token, not the page's add_only field: the token's mark still holds.
    applied = submit(client, "/catalog/import/apply", {"content": content.group(1), "token": token.group(1)})
    assert applied.status_code == 303 and applied.headers["location"].startswith("/catalog?notice=imported&created=1")
    items = {item.name: item for item in rows(migrated, CatalogItemRow)}
    assert "Lentil Soup" in items and items["Noodle Bar"].notes is None


@pytest.mark.flag("assistant", on=True)
def test_a_link_drafts_confirm_opens_its_preview_with_only_what_dinnerbell_kept(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def drafted() -> int:
        database = open_database(migrated.async_database_url)
        try:
            async with database.sessionmaker() as db:
                household = await current_household(db)
                kim = next(m for m in await MemberRepository(db).list(household.id) if m.email == "kim@example.com")
                return (await link_draft(db, kim, now=clock())).view.id
        finally:
            await database.close()

    draft = asyncio.run(drafted())
    assert f'action="/assistant/drafts/{draft}/confirm"' in page(client.get("/assistant"))
    preview = submit(client, f"/assistant/drafts/{draft}/confirm")
    assert preview.status_code == 200
    shown = page(preview)
    assert "only adds new items" in shown and "Add 1 item" in shown and "Basil House" in shown
    assert MENU_LINK in shown and "pay.example.net" not in shown and "Pay ahead" not in shown
    content = re.search(r'<textarea name="content"[^>]*>\n?(.*?)</textarea>', shown, re.S)
    token = re.search(r'name="token" value="([^"]+)"', preview.text)
    assert content and token
    applied = submit(client, "/catalog/import/apply", {"content": content.group(1), "token": token.group(1)})
    assert applied.status_code == 303 and applied.headers["location"].startswith("/catalog?notice=imported&created=1")
    basil = next(row for row in rows(migrated, CatalogItemRow) if row.name == "Basil House")
    assert basil.url == MENU_LINK and basil.order_url is None and basil.notes is None


# Escaping --------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
def test_reply_text_is_escaped_and_never_linked(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    nasty = (
        '<script>alert("hi")</script> <b>bold</b> <a href="https://evil.example/a">click</a>\n'
        "[the menu](https://evil.example/menu) and https://evil.example/raw **strong**"
    )
    model.script = [answer(nasty)]
    for response in (send(client, "<i>my</i> question https://mine.example", **HX), client.get("/assistant")):
        raw = response.text
        assert "<script>alert" not in raw and "<b>bold</b>" not in raw and "<i>my</i>" not in raw
        assert "&lt;script&gt;alert(&#34;hi&#34;)&lt;/script&gt;" in raw or "&lt;script&gt;alert(&quot;hi&quot;)" in raw
        assert 'href="https://evil.example' not in raw and 'href="https://mine.example' not in raw
        assert "<strong>strong</strong>" not in raw
        assert "[the menu](https://evil.example/menu)" in page(response)


@pytest.mark.flag("assistant", on=True)
def test_draft_titles_and_lines_are_escaped(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    assert add_item(client, "<b>Pho</b> Place").status_code == 303
    model.script = [tool_call("draft_offer", offer_tool("<b>Pho</b> Place")), answer("Drafted.")]
    fragment = send(client, "Offer pho", **HX).text
    assert "<b>Pho</b>" not in fragment and "&lt;b&gt;Pho&lt;/b&gt; Place" in fragment
