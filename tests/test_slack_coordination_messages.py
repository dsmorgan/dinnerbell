# SPDX-License-Identifier: AGPL-3.0-or-later
"""The direct messages about coordinating and roles (#218): a bold headline, the place on its own
line, and short bullets, in one section so the spacing survives on a phone. The golden files pin
the exact words; these pin the structure, and the cases a golden file can't show."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack.blocks import MAX_SECTION
from dinnerbell.channels.slack.messages import Message, RenderContext, render
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import AssignmentRole
from tests.support.blockkit import check_message

CTX = RenderContext(samples.BASE_URL, samples.DIRECTORY)
COOK, PICKUP, ORDER, LEAD = (
    AssignmentRole.COOK,
    AssignmentRole.PICKUP,
    AssignmentRole.ORDER,
    AssignmentRole.COORDINATOR,
)


def body(message: Message) -> str:
    """The section's text: every one of these messages keeps its words in the first block."""
    check_message(message.payload())
    text = message.blocks[0]["text"]["text"]
    assert isinstance(text, str)
    return text


def sample(kind: str) -> str:
    text = samples.preview(kind)["blocks"][0]["text"]["text"]
    assert isinstance(text, str)
    return text


def assigned(roles: tuple[AssignmentRole, ...]) -> note.AssignmentMade:
    return note.AssignmentMade(samples.DECIDED, 31, samples.SAM, roles, samples.DECISION)


# Being picked at random -----------------------------------------------------------------------------


def test_picked_at_random_groups_what_you_can_do_by_when_you_can_do_it() -> None:
    event = replace(samples.VOTING, coordinator=samples.KIM)
    lines = body(render(note.CoordinatorPicked(event, samples.KIM), CTX)).split("\n")
    assert lines[0] == (
        "*You're coordinating dinner on Thursday, Oct 22.* Nobody had claimed it, so Dinnerbell picked you at random."
    )
    vote, decided, hand_off = lines.index("*While the vote is open:*"), lines.index("*After it's decided:*"), -3
    assert lines[vote - 1] == lines[decided - 1] == lines[hand_off - 1] == ""
    assert lines[vote + 1] == "• Tap ⋯ beside a pick to decide it, break a tie, or set pickup or delivery"
    assert lines[vote + 2] == "• Shuffle the proposals"
    assert lines[decided + 1] == "• Change the place, how you get it, and who does it"
    assert lines[hand_off] == "*Can't do it?* Hand it off on the call's page."
    assert lines[-1] == "Thanks for steering!"


@pytest.mark.parametrize(("minutes", "words"), [(1, "within 1 minute, or"), (15, "within 15 minutes, or")])
def test_picked_at_random_counts_the_minutes_to_choose(minutes: int, words: str) -> None:
    event = replace(samples.VOTING, coordinator=samples.KIM)
    text = body(render(note.CoordinatorPicked(event, samples.KIM, choose_minutes=minutes), CTX))
    assert f"{words} by the meal time if sooner. Otherwise, the first person who offered does it." in text


# Being given coordinating ---------------------------------------------------------------------------


def test_given_coordinating_says_who_gave_it_and_whom_to_tell_on_short_lines() -> None:
    assert sample("coordinating_given").split("\n") == [
        "*You're coordinating dinner on Thursday, Oct 22.*",
        "Alex gave it to you, so you're in.",
        "If it doesn't work for you, tell Alex.",
        "",
        "Thanks for steering!",
    ]


def test_given_coordinating_during_the_vote_says_you_joined_late() -> None:
    lines = sample("coordinating_given_late").split("\n")
    assert lines[2] == "The vote has started, so you joined late, and your vote counts."


# Choosing roles ---------------------------------------------------------------------------------------


def test_choosing_roles_is_a_headline_the_place_and_what_to_do() -> None:
    assert sample("roles_to_choose").split("\n") == [
        "*You're coordinating dinner on Thursday, Oct 22.*",
        "Sam's Tacos · Cook · Mexican",
        "",
        "Tap *Assign roles* to choose who cooks by *6:00 PM*. After that, the first person who offered does it.",
    ]


def test_choosing_roles_with_the_way_open_says_what_it_stays() -> None:
    assert sample("roles_to_choose_way_open").endswith(
        "Tap *Assign roles* to choose pickup or delivery, who orders, and who picks it up by *6:00 PM*. "
        "After that, it stays pickup, and the first people who offered do them."
    )


def test_choosing_roles_keeps_its_button() -> None:
    actions = samples.preview("roles_to_choose")["blocks"][1]["elements"]
    assert actions[0]["text"]["text"] == "Assign roles"


# Being assigned ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "words"),
    [
        ((COOK,), "cooking dinner"),
        ((PICKUP, ORDER), "picking up and ordering dinner"),
        ((PICKUP, ORDER, LEAD), "picking up, ordering, and coordinating dinner"),
    ],
)
def test_an_assignment_headline_reads_the_roles_as_a_serial_list(roles: tuple[AssignmentRole, ...], words: str) -> None:
    message = render(assigned(roles), CTX)
    headline = body(message).split("\n")[0]
    assert headline == f"*You're {words} on Thursday, Oct 22.*"
    assert words.removesuffix(" dinner") in message.text  # the notification's fallback says so too


def test_an_assignment_is_a_headline_the_place_bullets_and_thanks() -> None:
    assert sample("assignment_made").split("\n") == [
        "*You're cooking dinner on Thursday, Oct 22.*",
        "Sam's Tacos · Cook · Mexican",
        "",
        "• *When:* 6:45 PM",
        "• *Eating:* 3 people (1 guest)",
        "• *Dietary needs:* Vegetarian",
        "• *Also helping:* Alex (coordinating)",
        "",
        "Thanks for pitching in!",
    ]


def test_an_assignment_leaves_out_the_bullets_with_nothing_to_say() -> None:
    text = sample("assignment_made_three_roles")
    assert "*When:*" in text and "*Eating:*" in text
    assert "Dietary needs" not in text and "Also helping" not in text


def test_being_assigned_stands_as_its_own_paragraph_above_the_bullets() -> None:
    paragraphs = sample("assignment_made_assigned").split("\n\n")
    assert paragraphs[1] == (
        "Alex assigned you this. If it doesn't work for you, tell Alex, who can pick someone else with *Adjust* "
        "on the decision."
    )
    assert paragraphs[2].startswith("• *When:*")


def test_an_assignment_for_a_guest_names_the_guest_and_the_reader_and_who_to_tell() -> None:
    text = sample("assignment_made_for_guest")
    assert text.startswith("Hi Kim! *Your guest Pat is picking up dinner on Thursday, Oct 22.*\n")
    assert "Thanks for pitching in" not in text  # Kim isn't the one pitching in
    assigned_for = sample("assignment_made_assigned_for_guest")
    assert "Alex assigned Pat this. If it doesn't work for them, tell Alex, who can" in assigned_for
    assert "assigned you" not in assigned_for


def test_with_nobody_coordinating_being_assigned_says_to_tell_whoever_assigned_you() -> None:
    event = replace(samples.DECIDED, coordinator=None)
    made = note.AssignmentMade(event, 31, samples.SAM, (COOK,), samples.DECISION, assigned_by=samples.ALEX)
    assert "\n\nAlex assigned you this. If it doesn't work for you, tell Alex.\n\n" in body(render(made, CTX))


def test_a_host_who_assigned_their_own_guest_isnt_told_about_it() -> None:
    by_host = note.AssignmentMade(
        samples.DECIDED, 38, samples.PAT, (PICKUP,), samples.DECISION, via=samples.KIM, assigned_by=samples.KIM
    )
    text = body(render(by_host, CTX))
    assert "assigned" not in text and "tell" not in text
    assert text.startswith("Hi Kim! *Your guest Pat is picking up dinner")


def test_an_assignment_without_roles_still_reads_right() -> None:
    assert body(render(assigned(()), CTX)).startswith("*You're helping with dinner on Thursday, Oct 22.*\n")


def test_long_dietary_needs_and_helpers_stay_within_slacks_limit() -> None:
    decision = replace(
        samples.DECISION,
        dietary_notes=tuple(f"Need {n}" * 20 for n in range(60)),
        assignments=tuple(note.AssignmentInfo(COOK, note.PersonInfo(n + 100, "Long Name" * 8)) for n in range(40)),
    )
    made = note.AssignmentMade(samples.DECIDED, 31, samples.SAM, (ORDER,), decision)
    text = body(render(made, CTX))
    assert len(text) <= MAX_SECTION and text.startswith("*You're ordering dinner")


# Roles as a list, wherever a reader sees one ----------------------------------------------------------


def test_every_message_that_lists_your_roles_uses_a_serial_list() -> None:
    three = (PICKUP, ORDER, LEAD)
    words = "picking up, ordering, and coordinating"
    event, decision = samples.DECIDED, samples.DECISION
    for notification in (
        note.AssignmentEnded(event, samples.SAM, three, samples.ALEX, samples.ROLES_DUE),
        note.AssignmentCancelled(samples.CANCELLED, samples.SAM, three, decision, cancelled_by=samples.ALEX),
        note.RoleVacated(event, samples.ALEX, samples.SAM, three),
    ):
        assert words in body(render(notification, CTX)), notification.kind


def test_a_message_with_no_roles_says_helping() -> None:
    ended = note.AssignmentEnded(samples.DECIDED, samples.SAM, (), samples.ALEX, samples.ROLES_DUE)
    assert "you're no longer helping." in body(render(ended, CTX))
