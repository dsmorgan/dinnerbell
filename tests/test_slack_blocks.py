# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit rendering (spec 13.10, "Channel" row): golden files for every notification, modal,
and Home tab sample, Slack's limits, escaping, and truncation.

The golden files live in tests/golden/slack/. After an intended change to a message, rewrite
them and review the diff:

    DINNERBELL_UPDATE_GOLDEN=1 uv run pytest tests/test_slack_blocks.py
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from dinnerbell.channels.slack import blocks as bk
from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack.messages import MAX_PROPOSALS, RenderContext, render
from dinnerbell.channels.slack.views import HomeLinks, home_view
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.events import EventState, ProposalSource, RoleOffer, Status
from dinnerbell.domain.meals import Slot
from dinnerbell.main import main
from dinnerbell.services.event_views import TodayView
from tests.support.blockkit import check_message, check_view

GOLDEN = Path(__file__).parent / "golden" / "slack"
UPDATE = os.environ.get("DINNERBELL_UPDATE_GOLDEN") == "1"
CTX = RenderContext(samples.BASE_URL, samples.DIRECTORY)


def as_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("kind", samples.KINDS)
def test_golden(kind: str) -> None:
    payload = samples.preview(kind)
    if kind in samples.VIEWS:
        check_view(payload)
    else:
        check_message(payload)
    path = GOLDEN / f"{kind}.json"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(as_json(payload))
    assert path.exists(), f"no golden file for {kind}; run with DINNERBELL_UPDATE_GOLDEN=1"
    assert path.read_text() == as_json(payload), f"{kind} changed; review, then DINNERBELL_UPDATE_GOLDEN=1"


LABELLED = ("call_opened", "proposals_ready", "decision_made", "response_reminder", "home", "home_empty")


def labelled(kind: str) -> dict[str, object]:
    """Sample ``kind`` from an instance labelled TEST (ADR 0006)."""
    if kind in samples.VIEWS:
        today = TodayView(samples.DAY, [samples.COLLECTING_VIEW] if kind == "home" else [], tuple(Slot))
        return home_view(today, HomeLinks(samples.BASE_URL), label="TEST")
    return render(samples.NOTIFICATIONS[kind](), replace(CTX, label="TEST")).payload()


@pytest.mark.parametrize("kind", LABELLED)
def test_labelled_golden(kind: str) -> None:
    payload = labelled(kind)
    if kind in samples.VIEWS:
        check_view(payload)
    else:
        check_message(payload)
    path = GOLDEN / "labelled" / f"{kind}.json"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(as_json(payload))
    assert path.exists(), f"no labelled golden file for {kind}; run with DINNERBELL_UPDATE_GOLDEN=1"
    assert path.read_text() == as_json(payload), f"labelled {kind} changed; review, then DINNERBELL_UPDATE_GOLDEN=1"
    unlabelled = samples.preview(kind)
    if kind in samples.VIEWS:
        assert payload["blocks"][1:] == unlabelled["blocks"][1:]  # only the header differs
    else:
        assert payload == {**unlabelled, "blocks": [bk.instance_banner("TEST"), *unlabelled["blocks"]]}


def test_every_notification_kind_has_a_sample_and_no_stale_goldens() -> None:
    sampled = {samples.NOTIFICATIONS[k]().kind for k in samples.NOTIFICATIONS}
    assert sampled == set(note.NOTIFICATION_TYPES)
    stale = {p.stem for p in GOLDEN.glob("*.json")} - set(samples.KINDS)
    assert stale == set()
    assert {p.stem for p in (GOLDEN / "labelled").glob("*.json")} == set(LABELLED)


def test_direct_messages_and_channel_posts_differ_where_they_should() -> None:
    reminder = samples.preview("response_reminder")
    assert "Hi Kim!" in reminder["blocks"][0]["text"]["text"]
    assert "<@" not in json.dumps(reminder)  # a DM never pings anyone
    opened = samples.preview("call_opened")
    assert "<@U01ALEX> <@U03KIM> Lee <@U02SAM>" in opened["blocks"][1]["text"]["text"]  # Lee has no Slack user
    assignment = samples.preview("assignment_made")
    assert "You're *cooking*" in assignment["blocks"][0]["text"]["text"]


def test_links_point_at_the_event_page() -> None:
    payload = json.dumps(samples.preview("decision_made"))
    assert "https://dinnerbell.example.com/events/7" in payload
    assert samples.preview("call_updated", "https://food.home.test")["blocks"][-1]["elements"][-1]["url"] == (
        "https://food.home.test/events/7"
    )


def test_buttons_follow_the_state() -> None:
    def action_ids(kind: str) -> list[str]:
        payload = samples.preview(kind)
        return [e["action_id"] for b in payload["blocks"] if b["type"] == "actions" for e in b["elements"]]

    assert action_ids("call_updated") == [
        "respond_in",
        "respond_maybe",
        "respond_out",
        "offer_cook",
        "offer_pickup",
        "offer_order",
        "coordinate",
        "details",
        "link_event",
    ]
    assert action_ids("call_updated_voting") == ["link_event"]  # Alex coordinates already
    assert action_ids("proposals_updated") == ["vote", "lock"] * 3 + ["add_proposal", "link_event"]
    assert action_ids("proposals_updated_closed") == ["link_event"]
    assert action_ids("decision_made") == ["mark_done", "link_event"]
    assert action_ids("decision_made_by_vote") == ["mark_done", "link_item", "link_order", "link_event"]
    assert action_ids("decision_updated_done") == ["link_event"]


# Escaping and limits -------------------------------------------------------------------------------


def test_user_text_is_escaped() -> None:
    evil = note.PersonInfo(9, "<!channel> & <@U0BOSS>")
    entry = note.RosterEntry(evil, Status.IN, note="<https://evil.example|click> *bold*", suggestions=("<!here>",))
    message = render(note.CallUpdated(samples.event(), (entry,), (), 1), CTX)
    check_message(message.payload())
    text = json.dumps(message.payload())
    assert "<!channel>" not in text and "<!here>" not in text and "<https://evil" not in text
    assert "&lt;!channel&gt; &amp; &lt;@U0BOSS&gt;" in text
    proposal = replace(samples.PROPOSALS[2])
    assert "Taqueria &lt;Lupita&gt; &amp; Co" in json.dumps(
        render(note.ProposalsReady(samples.VOTING, (proposal,)), CTX).payload()
    )


def test_a_huge_roster_and_long_names_stay_within_slacks_limits() -> None:
    long = "N" * 80
    roster = tuple(
        note.RosterEntry(
            note.PersonInfo(i, f"{long}{i}"),
            Status.IN,
            guests=20,
            guest_needs=("Vegetarian", "Gluten-free"),
            eat_from=time(17, 0),
            eat_until=time(21, 0),
            offers=tuple(RoleOffer),
            suggestions=("S" * 100,) * 3,
            note="& <" * 66,
        )
        for i in range(60)
    )
    event = replace(samples.event(), coordinator=note.PersonInfo(1, long))
    message = render(note.CallUpdated(event, roster, roster and (), head_count=1260), CTX)
    check_message(message.payload())
    assert message.blocks[2]["text"]["text"].endswith("…and more on the web.")


def test_more_proposals_than_the_cap_are_listed_on_the_web() -> None:
    item = note.ItemInfo(None, "P" * 100, ItemKind.RESTAURANT, url="https://x.example/" + "a" * 2900)
    proposals = tuple(
        note.ProposalInfo(100 + i, i, item, Mode.PICKUP, ("R" * 300,) * 5, ProposalSource.MEMBER) for i in range(14)
    )
    message = render(note.ProposalsUpdated(samples.VOTING, proposals, 3), CTX)
    check_message(message.payload())
    sections = [b for b in message.blocks if b.get("block_id", "").startswith("proposal-") and b["type"] == "section"]
    assert len(sections) == MAX_PROPOSALS
    assert any("4 more picks" in json.dumps(b) for b in message.blocks)


def test_a_cancelled_decision_strikes_the_item() -> None:
    message = render(note.DecisionUpdated(samples.event(EventState.CANCELLED), samples.DECISION), CTX)
    assert message.blocks[0]["text"]["text"].startswith("~Sam's Tacos~")


def test_clip_mrkdwn_never_splits_a_link_or_an_escape() -> None:
    assert bk.clip_mrkdwn("abc <@U0123> def", 9) == "abc …"
    assert bk.clip_mrkdwn("abc &amp; def", 7) == "abc …"
    assert bk.clip_mrkdwn("short", 10) == "short"
    assert bk.clip("abcdef", 4) == "abc…"


def test_join_lines_stops_before_the_limit() -> None:
    assert bk.join_lines(["a", "b"]) == "a\nb"
    joined = bk.join_lines(["x" * 50] * 10, limit=120, more="…more")
    assert joined.endswith("…more") and len(joined) <= 120


def test_safe_url_only_allows_http() -> None:
    assert bk.safe_url("javascript:alert(1)") is None
    assert bk.safe_url("https://a.example/x|y>z") == "https://a.example/x%7Cy%3Ez"
    assert bk.safe_url("https://a.example/" + "a" * 3000) is None
    assert bk.safe_url(None) is None
    assert bk.link("ftp://x.example", "<b>") == "&lt;b&gt;"


def test_times_and_days() -> None:
    assert [bk.clock(time(h, m)) for h, m in ((0, 5), (9, 0), (12, 0), (17, 15), (23, 59))] == [
        "12:05 AM",
        "9:00 AM",
        "12:00 PM",
        "5:15 PM",
        "11:59 PM",
    ]
    assert bk.local_clock(datetime(2026, 10, 22, 22, 15, tzinfo=UTC), "America/Chicago") == "5:15 PM"
    assert bk.day_label(date(2026, 1, 4)) == "Sunday, Jan 4"


def test_preview_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["slack", "preview"]) == 0
    assert "call_opened" in capsys.readouterr().out.split()
    assert main(["slack", "preview", "decision_made", "--base-url", "https://x.example/"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == samples.preview("decision_made", "https://x.example")
    assert main(["slack", "preview", "nope"]) == 2
    assert "Unknown kind" in capsys.readouterr().err


def test_plain_entries_and_bare_notifications() -> None:
    bare = note.RosterEntry(samples.KIM, Status.IN)
    scheduled = render(note.CallUpdated(samples.event(EventState.SCHEDULED), (bare,), (), 1), CTX)
    assert scheduled.blocks[1]["text"]["text"] == "This call opens soon."
    assert "• <@U03KIM>\n" in scheduled.blocks[2]["text"]["text"] + "\n"
    proposal = replace(samples.PROPOSALS[0], reasons=())
    picks = render(note.ProposalsUpdated(samples.VOTING, (proposal,), 2), CTX)
    assert picks.blocks[1]["text"]["text"].splitlines()[1] == "2 votes: <@U01ALEX>, <@U02SAM>"
    cancelled = render(note.EventCancelled(samples.event(EventState.CANCELLED)), CTX)
    assert cancelled.blocks[0]["text"]["text"] == "An admin cancelled the dinner call for Thursday, Oct 22."
    for message in (scheduled, picks, cancelled):
        check_message(message.payload())


def test_views_for_an_empty_catalog_and_a_busy_day() -> None:
    from dinnerbell.channels.slack.views import HomeLinks, details_modal, home_view, proposal_modal
    from dinnerbell.domain.meals import Slot
    from dinnerbell.services.event_views import Permissions, TodayView

    empty = replace(samples.COLLECTING_VIEW, items=(), dietary_tags=(), cuisine_tags=())
    details = details_modal(empty, samples.ORIGIN)
    proposal = proposal_modal(empty, samples.ORIGIN)
    for view in (details, proposal):
        check_view(view)
    assert {b.get("block_id") for b in details["blocks"]} >= {"guests", "suggest_text"}
    assert not {"guest_needs", "suggest_item"} & {b.get("block_id") for b in details["blocks"]}
    assert [b.get("block_id") for b in proposal["blocks"][1:]] == ["mode", "free_text"]

    voting = replace(samples.VOTING_VIEW, mine=None, can=Permissions())
    lunch = replace(
        samples.COLLECTING_VIEW, mine=None, event=replace(samples.COLLECTING_VIEW.event, id=8, slot=Slot.LUNCH)
    )
    home = home_view(TodayView(samples.DAY, [voting, lunch], tuple(Slot)), HomeLinks(samples.BASE_URL))
    check_view(home)
    text = json.dumps(home)
    assert "Voting is open in the channel thread." in text and "Are you in? Tap below." in text
    assert "start_call" not in text  # both calls exist already
