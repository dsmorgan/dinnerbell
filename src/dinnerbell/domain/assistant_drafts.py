# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's drafts and candidates (ADR 0009, spec 6.14, **Tools** and **Drafts**).

A draft is a change the assistant prepared for one member: a proposal, an offer, new catalog
items, one of the call actions a member takes in public anyway (#251): an answer, a vote, a
suggestion, a role, taking back an offer, inviting a guest or answering for one, or starting a
call, or a change to the member's own data, in private only (#253): a level on Preferences,
feedback on a meal, or away days. It says what **Confirm** will do, in lines worked
out here with the same rules the services apply, and lasts ``DRAFT_MINUTES``. Only its member confirms it, at most once.

Candidates are what ranking's hard filters let through for a voting call. Their reasons come
from a snapshot with no preferences, no hints, and pseudonyms in place of names
(``reasons_snapshot``), so no reason can say who likes what (FR-29).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from ipaddress import ip_address
from typing import Any

from dinnerbell.domain.catalog import MAX_URL_LENGTH, MODE_ORDER, CatalogItem, ItemKind, Mode, name_key
from dinnerbell.domain.catalog_export import dump_document
from dinnerbell.domain.catalog_file import (
    FORMAT_VERSION,
    HomeMealEntry,
    LocatedEntry,
    ParsedCatalog,
    RestaurantEntry,
    TagSection,
)
from dinnerbell.domain.events import Event, EventState, RoleOffer, Status
from dinnerbell.domain.fetch_policy import FetchError, Target, parse_target
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.offers import OfferPath, offer_path
from dinnerbell.domain.ranking import (
    MODE_COMPONENTS,
    GroupPreferences,
    RankingSnapshot,
    RankItem,
    components,
    explain,
)

DRAFT_MINUTES = 30
"""How long a draft waits for **Confirm** (spec 6.14, **Drafts**)."""
MAX_OPEN_DRAFTS = 10
"""Drafts one member may have waiting at once, so a runaway conversation can't pile them up."""

DOING = {Mode.COOK: "cook it", Mode.PICKUP: "pick it up", Mode.DELIVERY: "order it"}
"""What whoever takes a mode's role does, as the offer service words it."""
STRIPPED_FIELDS = frozenset({"cooks", "active"})
"""Fields a catalog draft never carries: cooks are other people's emails, and a draft only adds
items people can pick."""


class DraftKind(StrEnum):
    PROPOSAL = "proposal"
    OFFER = "offer"
    CATALOG = "catalog"
    ANSWER = "answer"
    VOTE = "vote"
    SUGGESTION = "suggestion"
    ROLE = "role"
    OFFER_OFF = "offer_off"
    GUEST = "guest"
    GUEST_ANSWER = "guest_answer"
    CALL = "call"
    PREFERENCE = "preference"
    FEEDBACK = "feedback"
    AWAY = "away"


CALL_ACTIONS = frozenset(
    {
        DraftKind.ANSWER,
        DraftKind.VOTE,
        DraftKind.SUGGESTION,
        DraftKind.ROLE,
        DraftKind.OFFER_OFF,
        DraftKind.GUEST,
        DraftKind.GUEST_ANSWER,
        DraftKind.CALL,
    }
)
"""The call actions (#251): each confirms through one call use case, on the draft's call, or
for ``CALL``, the slot and day it starts."""

OWN_DATA = frozenset({DraftKind.PREFERENCE, DraftKind.FEEDBACK, DraftKind.AWAY})
"""The member's own data (#253): drafted only in a private conversation, and each confirms
through the use case the Preferences page or the feedback form calls."""


@dataclass(frozen=True, slots=True)
class Draft:
    """A stored draft. ``payload`` holds what the service needs, by kind:

    - proposal: ``item_id``, ``mode``, ``join_late``
    - offer: ``item_id``, ``mode``, ``slot``, ``day``, ``at`` (``HH:MM``), ``capacity``,
      ``outcome``
    - catalog: ``content`` (the cleaned catalog file) and ``dropped`` (names left out)
    - answer: ``status``, and ``join_late`` for an In while voting
    - vote: ``proposal_id``, ``vote`` (False takes it back), and ``join_late``
    - suggestion: ``item_id``
    - role: ``role``, ``offered`` (False gives it up), and ``join_late``
    - offer_off: nothing; the offer is ``event_id``
    - guest: ``name`` as the member wrote it, and ``needs`` (dietary tag IDs, for a new guest);
      Confirm picks the saved guest by that name, or a new one
    - guest_answer: ``guest_id``, ``name``, and ``status`` or ``role`` with ``offered``
    - call: ``slot`` and ``day``; ``event_id`` is the called-off call it starts again, if any
    - preference: ``target_type``, ``target_id``, ``name``, ``level``, and ``before``, the
      level it changes
    - feedback: ``meal_id``, ``item_id``, the answers (``rating``, ``issues``, ``issue_note``,
      ``cook_thanks``, ``cook_note``), and ``before``, when the member's feedback it replaces
      was saved, or None
    - away: ``first`` and ``last``, or ``clear``, and ``before``, the window it changes, or None

    ``event_id`` and ``run`` are the call the draft was made against, where there is one."""

    id: int
    household_id: int
    member_id: int
    conversation_key: str
    kind: DraftKind
    payload: Mapping[str, Any]
    title: str
    lines: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    event_id: int | None = None
    run: int | None = None
    used_at: datetime | None = None
    """When a Confirm or a Cancel used it."""
    confirmed_at: datetime | None = None
    """When a Confirm used it; None for a draft cancelled, or still waiting."""

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def expires_at(now: datetime) -> datetime:
    return now + timedelta(minutes=DRAFT_MINUTES)


def day_words(day: date, today: date) -> str:
    """``day`` as a member reads it: "today", "tomorrow", or "Thu, Oct 22"."""
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{day:%a, %b} {day.day}"


# What Confirm does -------------------------------------------------------------------------------


def proposal_lines(*, slot: Slot, when: str, join_late: bool, volunteers: bool, mode: Mode) -> tuple[str, ...]:
    """What confirming a proposal does (spec 6.14, **Drafts**): it's added to the vote, the
    member joins late when they aren't in (``calls.add_proposal`` with ``join_late``), and they
    take the role the mode needs when they can and haven't offered it yet (``picks.volunteer``)."""
    lines = [f"It's added to {when}'s {slot.value} vote as a proposal."]
    if join_late:
        lines.append("You'll join late.")
    if volunteers:
        lines.append(f"You'll be down to {DOING[mode]}.")
    return tuple(lines)


class OfferOutcome(StrEnum):
    """What posting an offer does to its slot's call that day (FR-5): ``offer_path``, with a
    cooking offer for a day that has no call yet told apart, because it keeps that call from
    opening (``offers.replaces_call``)."""

    JOIN = "join"
    REPLACE = "replace"
    HOLD = "hold"
    ALONE = "alone"


def offer_outcome(call: Event | None, mode: Mode) -> OfferOutcome:
    path = offer_path(call, mode)
    if path is OfferPath.ATTACH:
        return OfferOutcome.JOIN
    if path is OfferPath.REPLACE:
        return OfferOutcome.REPLACE
    if call is None and mode is Mode.COOK:
        return OfferOutcome.HOLD
    return OfferOutcome.ALONE


@dataclass(frozen=True, slots=True)
class OfferPlan:
    """What an offer will do, as Confirm checks it again (``offers.post_offer``'s ``expect``):
    the outcome, the call's ID, run, and state, and, for a join that takes over the place's
    pick in another mode (``offers._attach``), that pick's mode. ``scheduled`` says whether the
    slot has a standing call that day, for a hold."""

    outcome: OfferOutcome
    call_id: int | None = None
    run: int | None = None
    state: EventState | None = None
    takes_over: Mode | None = None
    scheduled: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "call_id": self.call_id,
            "run": self.run,
            "state": self.state.value if self.state else None,
            "takes_over": self.takes_over.value if self.takes_over else None,
            "scheduled": self.scheduled,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> OfferPlan:
        state, takes_over = raw.get("state"), raw.get("takes_over")
        return cls(
            outcome=OfferOutcome(str(raw["outcome"])),
            call_id=raw.get("call_id"),
            run=raw.get("run"),
            state=EventState(str(state)) if state else None,
            takes_over=Mode(str(takes_over)) if takes_over else None,
            scheduled=bool(raw.get("scheduled", True)),
        )

    def same(self, other: OfferPlan) -> bool:
        """Whether ``other`` does what this one said, ignoring ``scheduled``. That only words a
        hold, and it can change while the outcome doesn't, such as when the day's answer window
        passes or Admin turns the slot's schedule off; either way the offer still holds the slot."""
        return (self.outcome, self.call_id, self.run, self.state, self.takes_over) == (
            other.outcome,
            other.call_id,
            other.run,
            other.state,
            other.takes_over,
        )


def offer_lines(plan: OfferPlan, *, slot: Slot, when: str, mode: Mode, item: str) -> tuple[str, ...]:
    """What confirming an offer does, one of four (spec 6.14, **Drafts**), then the role the
    offerer takes. ``when`` is the day in words, and ``item`` the item's name."""
    if plan.outcome is OfferOutcome.JOIN and plan.takes_over is not None:
        after = "and you're in" if plan.state is EventState.COLLECTING else "and its votes stay"
        first = (
            f"It changes {when}'s {slot.value} pick {item} from {plan.takes_over.label.lower()} to "
            f"{mode.label.lower()}. It becomes your pick, {after}."
        )
    elif plan.outcome is OfferOutcome.JOIN:
        stage = "voting" if plan.state is EventState.VOTING else "collecting answers"
        first = f"It joins {when}'s {slot.value} call, which is {stage}, as a pick, and you're in."
    elif plan.outcome is OfferOutcome.REPLACE:
        first = f"It replaces {when}'s scheduled {slot.value} call, which won't open."
    elif plan.outcome is OfferOutcome.HOLD and plan.scheduled:
        first = f"It keeps {when}'s {slot.value} call from opening while your offer is on."
    elif plan.outcome is OfferOutcome.HOLD:
        first = f"No one can start a {slot.value} call for {when} while your offer is on."
    else:
        first = "It's posted as an offer of its own."
    return (first, f"You'll {DOING[mode]}.")


# What Confirm does for a call action (#251) -----------------------------------------------------

ROLE_DOING = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pick up", RoleOffer.ORDER: "order"}
"""What a role offer says the member will do, as the call's roster words it."""
LATE_VOTE_WAITS = "Your votes show, and count once the coordinator says so."
LATE_VOTE_COUNTS = "Your votes count, since answers closed early on your maybe."


def _late(when: str, slot: Slot, vote_counts: bool) -> tuple[str, str]:
    return (f"You'll join {when}'s {slot.value} call late.", LATE_VOTE_COUNTS if vote_counts else LATE_VOTE_WAITS)


def answer_lines(
    status: Status,
    *,
    slot: Slot,
    when: str,
    join_late: bool = False,
    vote_counts: bool = False,
    usual_offers: bool = False,
    withdraws_roles: bool = False,
    coordinating: bool = False,
) -> tuple[str, ...]:
    """What confirming an answer does: ``calls.respond``, or ``calls.join_late`` for an In
    while voting (``join_late``), whose votes count at once only for a maybe the early close
    turned out (``vote_counts``). ``usual_offers``: answering In turns on the offers set on
    Preferences (``with_usual_offers``). ``withdraws_roles`` and ``coordinating``: an Out
    withdraws the member's role offers, and ends their coordinating (``drops_coordinating``)."""
    if join_late:
        lines = list(_late(when, slot, vote_counts))
    else:
        word = {Status.IN: "in for", Status.OUT: "out of", Status.MAYBE: "a maybe for"}[status]
        lines = [f"You'll be {word} {when}'s {slot.value} call."]
    if usual_offers:
        lines.append("Your usual offers from Preferences turn on.")
    if withdraws_roles:
        lines.append("Your role offers are withdrawn.")
    if coordinating:
        lines.append("You'll stop coordinating it.")
    return tuple(lines)


def vote_lines(
    name: str, mode: Mode, *, vote: bool, join_late: bool = False, vote_counts: bool = True
) -> tuple[str, ...]:
    """What confirming a vote does (``calls.vote``, ``calls.unvote``): someone who isn't in joins
    late first, as **I'm in (late)** and vote does; a late joiner's vote counts only once the
    coordinator says so, unless ``vote_counts``."""
    pick = f"{name} ({mode.label.lower()})"
    if not vote:
        return (f"It takes back your vote for {pick}.",)
    lines = [f"It adds your vote for {pick}."]
    if join_late:
        lines.append("You'll join the call late first.")
    if not vote_counts:
        lines.append(LATE_VOTE_WAITS)
    return tuple(lines)


def suggestion_lines(name: str, *, slot: Slot, when: str, counts_in: bool) -> tuple[str, ...]:
    """What confirming a suggestion does (``calls.save_details``): it joins the member's
    suggestions, and their eating times, note, and other suggestions stay. Details counts in
    someone who hasn't answered (``counts_in``)."""
    lines = [
        f"It adds {name} to your suggestions for {when}'s {slot.value} call.",
        "Your eating times, note, and other suggestions stay as they are.",
    ]
    if counts_in:
        lines.append("You'll be in, as saving Details does.")
    return tuple(lines)


def role_lines(
    role: RoleOffer,
    *,
    offered: bool,
    slot: Slot,
    when: str,
    join_late: bool = False,
    vote_counts: bool = False,
    counts_in: bool = False,
) -> tuple[str, ...]:
    """What confirming a role does (``calls.offer_role``): offering one counts in someone who
    isn't (``counts_in``), unless they said maybe; while voting, someone who isn't in joins
    late first (``join_late``, ``calls.join_late``)."""
    doing = ROLE_DOING[role]
    if not offered:
        return (f"You'll no longer be down to {doing} for {when}'s {slot.value} call.",)
    lines = [*_late(when, slot, vote_counts)] if join_late else []
    lines.append(f"You'll be down to {doing} for {when}'s {slot.value} call.")
    if counts_in:
        lines.append("You'll be in.")
    return tuple(lines)


def offer_off_lines(*, item: str, slot: Slot, when: str) -> tuple[str, ...]:
    """What confirming taking back an offer does (``calls.cancel_call``, as its offerer)."""
    return (
        f"It calls off your offer of {item} for {when}'s {slot.value}.",
        "Dinnerbell tells everyone who joined it.",
    )


def invite_lines(name: str, *, needs: Iterable[str], slot: Slot, when: str) -> tuple[str, ...]:
    """What confirming a guest's invitation does (``guests.invite``), the same whether the
    household has a saved guest by that name, and whether they have Slack: the household's saved
    guest by that name, or else a new guest, saved first with ``needs``, the dietary tags the
    member named. A guest with Slack is told and answers themselves; one without is in at once,
    and the inviter answers for them."""
    named = list(needs)
    saved = "If no saved guest has that name, Dinnerbell saves them as a new guest"
    lines = [
        f"It invites {name} to {when}'s {slot.value} call.",
        f"{saved}, with these dietary needs: {_list(named)}. A saved guest keeps their own." if named else f"{saved}.",
        "A guest on Slack is told and answers themselves; anyone else is in at once, and you answer for them.",
    ]
    return tuple(lines)


def guest_answer_lines(
    name: str,
    *,
    status: Status | None,
    role: RoleOffer | None,
    offered: bool,
    withdraws_roles: bool,
    slot: Slot,
    when: str,
) -> tuple[str, ...]:
    """What confirming an answer for a guest does (``guests.answer_for``): in or out, or a role
    offered or withdrawn for them, which counts them in. An out withdraws their role offers."""
    call = f"{when}'s {slot.value} call"
    if status is Status.OUT:
        return (
            f"{name} will be out of {call}.",
            *(["Roles offered for them are withdrawn."] if withdraws_roles else []),
        )
    if status is not None:
        return (f"{name} will be in for {call}.",)
    doing = ROLE_DOING[role or RoleOffer.COOK]
    if not offered:
        return (f"{name} will no longer be down to {doing} for {call}.",)
    return (f"{name} will be down to {doing} for {call}, and in.",)


def call_lines(*, slot: Slot, when: str, now: bool, restart: bool) -> tuple[str, ...]:
    """What confirming an ad-hoc call does (``calls.start_call``, FR-3): today's opens now; a
    later day's opens at that day's usual time; a called-off call starts again as a new run.
    Whoever starts it may call it off until it's decided (``check_can_cancel``)."""
    if restart:
        first = f"It starts the called-off {slot.value} call for {when} again."
    elif now:
        first = f"It starts {when}'s {slot.value} call now, and Dinnerbell posts it."
    else:
        first = f"It starts a {slot.value} call for {when}, which opens at the usual time."
    return (first, "You'll be its starter, so you can call it off until it's decided.")


# Candidates --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Narrowing:
    """How a member narrowed a candidate search. Each is optional: 0, "", or None means any."""

    not_eaten_in_days: int = 0
    cuisine: str = ""
    mode: Mode | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    item: RankItem
    modes: tuple[Mode, ...]
    """Every way it passes the filters, in ``MODE_ORDER``."""
    reasons: tuple[str, ...]
    for_meal: bool


def reasons_snapshot(
    snapshot: RankingSnapshot, *, labels: Mapping[int, str], suggesters: Mapping[int, tuple[int, ...]]
) -> RankingSnapshot:
    """``snapshot`` with nothing a reason could reveal about a person: no preferences, so no
    favorite sentences (``SOFT_PREFERENCE`` is 0); no hints; and members as the conversation's
    pseudonyms. ``suggesters`` holds member IDs by item, and ``labels`` each member's label."""
    return replace(
        snapshot,
        preferences=GroupPreferences(),
        hints={},
        member_names=dict(labels),
        suggesters={item: tuple(labels[m] for m in ids if m in labels) for item, ids in suggesters.items()},
    )


def _best_mode(item: RankItem, modes: Iterable[Mode], snapshot: RankingSnapshot) -> Mode:
    """Ranking's choice among an item's modes (step 4): the mode's own components, then
    ``MODE_ORDER``."""

    def key(mode: Mode) -> tuple[float, int]:
        parts = components(item, mode, snapshot)
        return (-round(sum(parts[c] for c in MODE_COMPONENTS), 6), MODE_ORDER.index(mode))

    return min(modes, key=key)


def candidate_reasons(
    item: RankItem, modes: Iterable[Mode], clean: RankingSnapshot, *, for_meal: bool
) -> tuple[str, ...]:
    """Reasons for ``item`` from ``clean`` (``reasons_snapshot``): variety, novelty, suggestions,
    the default mode, volunteer fit, and, for an item usually had at other meals, meal fit."""
    mode = _best_mode(item, modes, clean)
    return explain(item, mode, components(item, mode, clean), clean, for_meal=for_meal)


def narrowed(
    pairs: Iterable[tuple[RankItem, Mode]],
    narrowing: Narrowing,
    *,
    last_eaten_days: Mapping[int, int],
    cuisine_names: Mapping[int, str],
) -> dict[int, tuple[RankItem, tuple[Mode, ...]]]:
    """``pairs`` that pass ``narrowing``, grouped by item, each with its modes in order."""
    cuisine = name_key(narrowing.cuisine)
    found: dict[int, tuple[RankItem, list[Mode]]] = {}
    for item, mode in pairs:
        if narrowing.mode is not None and mode is not narrowing.mode:
            continue
        days = last_eaten_days.get(item.id)
        if narrowing.not_eaten_in_days and days is not None and days < narrowing.not_eaten_in_days:
            continue
        if cuisine and not any(cuisine in name_key(cuisine_names.get(t, "")) for t in item.cuisine_ids):
            continue
        found.setdefault(item.id, (item, []))[1].append(mode)
    return {i: (item, tuple(m for m in MODE_ORDER if m in modes)) for i, (item, modes) in found.items()}


# Catalog drafts ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanCatalog:
    """A catalog draft: the file to import, the names it adds, and the ones it left out because
    the catalog has them already."""

    text: str
    added: tuple[str, ...]
    dropped: tuple[str, ...]
    tags: Mapping[str, list[str]] = field(default_factory=dict)


def _entry_document(entry: RestaurantEntry | HomeMealEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude_unset=True, exclude=set(STRIPPED_FIELDS))


def matching_names(parsed: ParsedCatalog, items: Iterable[CatalogItem]) -> frozenset[tuple[ItemKind, str]]:
    """The (kind, name key) of each entry in ``parsed`` that names an item ``items`` has, in
    either state: an import would change that item, and a draft only adds."""
    existing = {(item.kind, item.key) for item in items}
    return frozenset(
        (located.kind, name_key(located.entry.name))
        for located in parsed.entries
        if (located.kind, name_key(located.entry.name)) in existing
    )


def clean_catalog(parsed: ParsedCatalog, items: Iterable[CatalogItem]) -> CleanCatalog:
    """``parsed`` as a draft (spec 6.14, **Tools**): entries naming an item the catalog has
    (same kind, ignoring case) are dropped and listed, and ``cooks`` and ``active`` are stripped.
    The caller checks ``parsed.errors`` first."""
    matches = matching_names(parsed, items)
    sections: dict[str, list[dict[str, Any]]] = {"restaurants": [], "home_meals": []}
    added: list[str] = []
    dropped: list[str] = []
    for located in parsed.entries:
        entry = located.entry
        if (located.kind, name_key(entry.name)) in matches:
            dropped.append(entry.name)
            continue
        added.append(entry.name)
        sections["restaurants" if located.kind is ItemKind.RESTAURANT else "home_meals"].append(_entry_document(entry))
    document: dict[str, Any] = {"version": FORMAT_VERSION}
    tags = parsed.tags.model_dump(mode="json", exclude_none=True) if parsed.tags is not None else {}
    if tags:
        document["tags"] = tags
    document.update({name: entries for name, entries in sections.items() if entries})
    return CleanCatalog(text=dump_document(document), added=tuple(added), dropped=tuple(dropped), tags=tags)


MAX_LINK_ITEMS = 20
"""The most entries one menu link drafts: a menu page is usually one place, a list page a few."""
LINK_DROPPED_FIELDS = frozenset({"url", "order_url", "notes"})
"""Fields a link draft never takes from the drafting call. A page can plant a link that looks like
its own site's and leads anywhere, through a shortener, a redirect endpoint, or someone else's page
on a shared host, so Dinnerbell picks a lone entry's link itself (``entry_link``). Notes are free
text a page could fill with anything, such as a phone number to call with a card. Members add
links and notes themselves."""


@dataclass(frozen=True, slots=True)
class LinkCatalog:
    """What a drafting call read from a page, kept to what an injected page can't misuse:
    ``parsed`` holds the first ``MAX_LINK_ITEMS`` entries, with no link or notes the page wrote,
    and only the declared tags those entries use. ``links_dropped`` counts the links the page
    wrote, and ``items_cut`` the entries past the limit."""

    parsed: ParsedCatalog
    links_dropped: int = 0
    items_cut: int = 0


def _on_host(target: Target, host: str) -> bool:
    if target.host == host:
        return True
    return target.address is None and not _is_address(host) and target.host.endswith(f".{host}")


def _is_address(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


def entry_link(link: str, final_url: str) -> str | None:
    """The one link Dinnerbell gives a lone entry from ``link``, the member's own, which the
    fetch reached at ``final_url``: ``final_url`` when its host is ``link``'s or a subdomain of
    it, such as ``example.com`` moving to ``www.example.com``, and otherwise ``link`` exactly as
    the member gave it, such as a shortener's. None for a link the catalog can't hold."""
    try:
        given = parse_target(link)
        final = parse_target(final_url)
    except FetchError:
        return None
    chosen = final_url if _on_host(final, given.host) else link
    return chosen if len(chosen) <= MAX_URL_LENGTH else None


def _from_page(entry: RestaurantEntry | HomeMealEntry) -> tuple[Any, int]:
    fields = entry.model_dump(mode="json", exclude_unset=True)
    links = sum(1 for name in ("url", "order_url") if fields.get(name) is not None)
    for name in LINK_DROPPED_FIELDS:
        fields.pop(name, None)
    return type(entry).model_validate(fields), links


def _used_tags(tags: TagSection | None, entries: Iterable[LocatedEntry]) -> TagSection | None:
    if tags is None:
        return None
    used = {name_key(tag) for located in entries for tag in [*located.entry.cuisine, *(located.entry.tags or [])]}
    kept = {
        kind: [tag for tag in names if name_key(tag) in used]
        for kind, names in tags.model_dump(exclude_none=True).items()
    }
    kept = {kind: names for kind, names in kept.items() if names}
    return TagSection.model_validate(kept) if kept else None


def from_link(parsed: ParsedCatalog, *, url: str | None = None, max_items: int = MAX_LINK_ITEMS) -> LinkCatalog:
    """``parsed``, a drafting call's answer, as ``LinkCatalog`` keeps it. ``url``, from
    ``entry_link``, becomes the ``url`` of a lone entry: a menu link's draft is usually that one
    place. No entry ever gets an ``order_url``. The caller checks ``parsed.errors`` first;
    entries that parsed validate again unchanged. Raises ``ValueError`` only if that ever stops
    holding."""
    kept: list[LocatedEntry] = []
    links_dropped = 0
    for located in parsed.entries[:max_items]:
        entry, dropped = _from_page(located.entry)
        links_dropped += dropped
        kept.append(LocatedEntry(located.location, entry))
    if url is not None and len(kept) == 1:
        only = kept[0]
        fields = only.entry.model_dump(mode="json", exclude_unset=True) | {"url": url}
        kept[0] = LocatedEntry(only.location, type(only.entry).model_validate(fields))
    clean = ParsedCatalog(tags=_used_tags(parsed.tags, kept), entries=tuple(kept), errors=())
    return LinkCatalog(clean, links_dropped=links_dropped, items_cut=max(0, len(parsed.entries) - max_items))


def catalog_lines(clean: CleanCatalog) -> tuple[str, ...]:
    lines = [f"Opens the import preview to add {_list(clean.added)}; it changes no item the catalog has."]
    if clean.dropped:
        lines.append(f"Left out, already in the catalog: {_list(clean.dropped)}.")
    return tuple(lines)


def _list(names: Iterable[str]) -> str:
    shown = list(names)
    if len(shown) > 8:
        shown = [*shown[:8], f"{len(shown) - 8} more"]
    if len(shown) <= 2:
        return " and ".join(shown)
    return f"{', '.join(shown[:-1])}, and {shown[-1]}"
