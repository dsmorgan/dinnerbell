# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badge art, the Stats page, and Admin > Badges (M4 slice 3, #15)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, BadgeAwardRow, HouseholdRow
from dinnerbell.domain.badges import (
    BADGES,
    DEFAULT_LADDERS,
    LADDERS,
    TIERS,
    Announcement,
    Ladder,
    Tier,
    ladders_setting,
)
from dinnerbell.domain.contributions import Kind
from dinnerbell.services import badge_art
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.web import submit
from tests.test_auth_flow import member, rows
from tests.test_events_web import sign_in_as
from tests.test_logo import STATIC, committed_svgs, svg_problems

KEYS = [b.key.value for b in BADGES]
EVERY_BADGE_AND_TIER = [(k, t) for k in KEYS for t in TIERS]
CSS = (STATIC / "app.css").read_text()


# The art -------------------------------------------------------------------------------------------


def ids(value: object) -> str:
    return str(getattr(value, "value", value))


@pytest.mark.parametrize(("key", "tier"), EVERY_BADGE_AND_TIER, ids=ids)
def test_every_badge_and_tier_draws_with_a_name(key: str, tier: Tier) -> None:
    svg = str(badge_art.badge_svg(key, tier))
    name = badge_art.badge_name(key)
    assert f'aria-label="{name}, {tier.label}"' in svg and 'role="img"' in svg
    assert "style=" not in svg and "<script" not in svg
    ElementTree.fromstring(svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1))  # noqa: S314
    sparkles = tier in (Tier.GOLD, Tier.DIAMOND)
    assert ("badge-tw" in svg) is sparkles and ("badge-sheen" in svg) is sparkles
    assert svg.count('class="badge-tw') == {Tier.GOLD: 3, Tier.DIAMOND: 4}.get(tier, 0)


@pytest.mark.parametrize("key", KEYS)
def test_a_locked_badge_is_soft_and_says_so(key: str) -> None:
    svg = str(badge_art.badge_svg(key, Tier.DIAMOND, locked=True))
    assert f'aria-label="{badge_art.badge_name(key)}, not earned yet"' in svg
    assert "badge-svg-locked" in svg and "badge-tw" not in svg and "badge-sheen" not in svg


def test_a_decorative_badge_is_hidden_from_assistive_technology() -> None:
    svg = str(badge_art.badge_svg("chef", Tier.GOLD, decorative=True))
    assert 'aria-hidden="true"' in svg and "aria-label" not in svg and 'role="img"' not in svg


def test_tiers_differ_in_outline_not_only_in_color() -> None:
    shapes = {t: badge_art.TIER_ART[t].shape for t in TIERS}
    assert shapes[Tier.FIRST] == "ribbon" and shapes[Tier.DIAMOND] == "gem"
    assert {shapes[t] for t in (Tier.BRONZE, Tier.SILVER, Tier.GOLD)} == {"medal"}
    fills = {badge_art.TIER_ART[t].fill for t in TIERS}
    assert len(fills) == 5


def test_there_is_an_icon_for_every_badge_and_a_frame_for_every_tier() -> None:
    assert set(badge_art.ICONS) == set(KEYS)
    for key in KEYS:
        assert (STATIC / "badges" / "icons" / f"{key}.svg").read_text() == badge_art.icon_file(key)
    for tier in TIERS:
        assert (STATIC / "badges" / "frames" / f"{tier.value}.svg").read_text() == badge_art.frame_file(tier)
    on_disk = {p.name for p in (STATIC / "badges").rglob("*") if p.is_file()}
    assert on_disk == {f"{k}.svg" for k in KEYS} | {f"{t.value}.svg" for t in TIERS}


def test_the_badge_files_are_covered_by_the_svg_allowlist() -> None:
    covered = {p.relative_to(STATIC).as_posix() for p in committed_svgs() if "badges" in p.parts}
    assert len(covered) == len(KEYS) + len(TIERS)
    assert not [p for p in covered if svg_problems((STATIC / p).read_text())]


@pytest.mark.parametrize(("key", "tier"), EVERY_BADGE_AND_TIER, ids=ids)
def test_the_standalone_svg_for_slack_is_inert_and_still(key: str, tier: Tier) -> None:
    text = badge_art.standalone_svg(key, tier)
    assert svg_problems(text) == []
    assert "class=" not in text and "animate" not in text and "<style" not in text
    assert text.count('<path d="M0 -7') == {Tier.GOLD: 3, Tier.DIAMOND: 4}.get(tier, 0)  # the stars are drawn in


def test_the_badge_files_are_served_as_images(client: TestClient) -> None:
    for name in ("badges/frames/gold.svg", "badges/icons/chef.svg"):
        response = client.get(f"/static/{name}")
        assert (response.status_code, response.headers["content-type"]) == (200, "image/svg+xml")


def test_the_css_sparkles_without_inline_styles_and_stops_for_reduced_motion() -> None:
    assert "@keyframes badge-twinkle" in CSS and "@keyframes badge-sheen" in CSS
    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)", CSS.index("badge-sheen")) :]
    assert re.search(r"\.badge-tw,\s*\.badge-sheen\s*\{\s*animation:\s*none", reduced)
    assert ".badge-sheen { opacity: 0; }" in reduced
    assert "var(--" in CSS[CSS.index(".badge-row.locked") :][:80]  # the locked title follows the theme (dark mode)


# Stats page ------------------------------------------------------------------------------------------


NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)  # the FakeClock default


def award(
    settings: Settings,
    member_id: int,
    badge: str,
    tier: Tier,
    *,
    ago: timedelta = timedelta(hours=1),
    announcement: Announcement = Announcement.ANNOUNCED,
    at: datetime | None = None,
) -> None:
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            household = db.query(HouseholdRow).one()
            db.add(
                BadgeAwardRow(
                    household_id=household.id,
                    member_id=member_id,
                    badge_key=badge,
                    tier=tier.value,
                    awarded_at=at or NOW - ago,
                    announcement=announcement.value,
                )
            )
            db.commit()
    finally:
        engine.dispose()


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    return client


@pytest.fixture
def house(admin: TestClient, migrated: Settings) -> Settings:
    for email, name in (("kim@example.com", "Kim"), ("lee@example.com", "Lee")):
        assert invite(admin, email, name).status_code == 303
    return migrated


class Structure(HTMLParser):
    """Headings, images without text, and form controls without names: the structure axe checks first."""

    def __init__(self) -> None:
        super().__init__()
        self.headings: list[int] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "svg" and "aria-hidden" not in a and "aria-label" not in a:
            self.problems.append("svg without a name")
        if tag == "progress" and "aria-label" not in a:
            self.problems.append("progress without a name")
        if tag == "img" and "alt" not in a:
            self.problems.append("img without alt")


def check_structure(html: str) -> None:
    parsed = Structure()
    parsed.feed(html)
    assert parsed.problems == []
    assert parsed.headings.count(1) == 1
    assert all(b - a <= 1 for a, b in zip(parsed.headings, parsed.headings[1:], strict=False)), parsed.headings


def test_a_member_with_no_badges_sees_all_thirteen_softly(house: Settings, client: TestClient) -> None:
    html = client.get("/stats").text
    assert html.count('class="badge-row locked"') == 13
    assert html.count("Not earned yet") == 13
    assert "Meals cooked: 0 of 1 for First." in page(client.get("/stats"))
    assert "Newly earned" not in html and "Lately earned" in html and "No new badges yet" in html
    assert inline_code(html)[1] == []
    check_structure(html)


def test_a_member_sees_tiers_progress_and_new_badges(house: Settings, client: TestClient) -> None:
    me = member(house, "admin@example.com").id
    award(house, me, "chef", Tier.FIRST, ago=timedelta(days=30))
    award(house, me, "chef", Tier.BRONZE, ago=timedelta(days=1))
    award(house, me, "scout", Tier.FIRST, ago=timedelta(days=2), announcement=Announcement.SKIPPED)
    html = page(client.get("/stats"))
    assert 'Chef<span class="badge-tier"> · Bronze' in html
    assert 'aria-label="Chef: 0 of 50 meals cooked for Silver"' in html
    new = html[html.index("Newly earned") : html.index("Your badges")]
    assert "Chef · Bronze" in new and "Chef · First" not in new  # a month old
    assert "Scout" not in new  # earned quietly
    assert 'Scout<span class="badge-tier"> · First' in html  # but it is held
    assert html.count('class="badge-row locked"') == 11
    check_structure(html)


def test_the_household_view_lists_recent_badges_by_recency_without_ranking(house: Settings, client: TestClient) -> None:
    ids = {n: member(house, f"{n.lower()}@example.com").id for n in ("Kim", "Lee")}
    award(house, ids["Lee"], "chef", Tier.FIRST, ago=timedelta(hours=3))
    award(house, ids["Kim"], "scout", Tier.FIRST, ago=timedelta(hours=2))
    award(house, ids["Kim"], "scout", Tier.BRONZE, ago=timedelta(hours=1))
    award(
        house, ids["Lee"], "explorer", Tier.FIRST, ago=timedelta(hours=1, minutes=30), announcement=Announcement.SKIPPED
    )
    html = page(client.get("/stats"))
    lately = html[html.index("Lately earned") :]
    assert lately.index("Scout · Bronze") < lately.index("Scout · First") < lately.index("Chef · First")
    assert "Explorer" not in lately  # quiet awards are not news
    assert "4 badges earned together" in html
    assert "Newest first." in lately
    for word in ("rank", "leaderboard", "most", "top "):
        assert word not in lately.lower().replace("top tier", "")


def test_a_member_never_sees_another_members_private_standing(
    house: Settings, provider: FakeProvider, client: TestClient
) -> None:
    kim, lee = (member(house, f"{n}@example.com").id for n in ("kim", "lee"))
    for tier in TIERS[:3]:
        award(house, lee, "chef", tier)
    sign_in_as(client, provider, "kim@example.com")
    assert kim
    html = page(client.get("/stats"))
    # Lee's badges are household news, with a name; Lee's progress and counts are not shown.
    assert "Lee" in html and "Chef · Silver" in html
    assert 'class="badge-row locked"' in html and html.count("badge-row") == 13
    own = html[html.index('id="badges-title"') : html.index('id="counts-title"')]
    assert "Lee" not in own and 'Chef<span class="badge-tier"> · Silver' not in own


def test_the_stats_page_needs_a_sign_in(client: TestClient) -> None:
    assert client.get("/stats").status_code in (302, 303, 401)


def test_the_navigation_links_the_stats_page(house: Settings, client: TestClient) -> None:
    assert '<a href="/stats"' in client.get("/").text and "Stats</a>" in client.get("/").text
    assert 'aria-current="page">Stats' in client.get("/stats").text


# Admin > Badges ----------------------------------------------------------------------------------------


FREQUENT = DEFAULT_LADDERS[Ladder.FREQUENT]


def form(*counts: int | str, ladder: Ladder = Ladder.FREQUENT) -> dict[str, str]:
    """The whole form: ``counts`` for ``ladder`` and the defaults for the other two."""
    shown: dict[Ladder, tuple[int | str, ...]] = dict(DEFAULT_LADDERS) | {ladder: counts}
    return {
        f"threshold_{lad.value}_{t.value}": str(n) for lad in LADDERS for t, n in zip(TIERS, shown[lad], strict=True)
    }


def reset(client: TestClient) -> Any:
    return submit(client, "/admin/badges/reset", {})


def audits(settings: Settings) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == "household.badge_thresholds_updated"]


def test_the_badges_tab_shows_the_three_ladders_and_explains_the_rules(admin: TestClient) -> None:
    html = admin.get("/admin/badges").text
    for ladder in LADDERS:
        for tier, n in zip(TIERS, DEFAULT_LADDERS[ladder], strict=True):
            assert f'name="threshold_{ladder.value}_{tier.value}" value="{n}"' in html
        assert f"<h2>{ladder.label}</h2>" in html
    for badge in BADGES:  # each ladder lists its badges
        assert badge.name in html
    assert "Bright Idea, Good Host" in html and "Scout, Explorer, On a Roll" in html
    assert 'action="/admin/badges/reset"' in html and "Reset to defaults" in html
    assert 'aria-current="page">Badges' in html
    assert "Lowering a count awards the tier to anyone who already has the count, quietly" in html
    assert "Raising a count takes nothing away" in html
    check_structure(html)


def test_saving_new_counts_changes_the_ladder_and_audits(admin: TestClient, migrated: Settings) -> None:
    saved = submit(admin, "/admin/badges", form(1, 2, 4, 8, 16, ladder=Ladder.RARE))
    assert (saved.status_code, saved.headers["location"]) == (303, "/admin/badges?notice=saved")
    assert "Badge counts saved." in admin.get("/admin/badges?notice=saved").text
    wanted = ladders_setting(DEFAULT_LADDERS) | {"rare": [1, 2, 4, 8, 16]}
    assert rows(migrated, HouseholdRow)[0].settings["badge_thresholds"] == wanted
    (audit,) = audits(migrated)
    assert audit.payload["from"] == ladders_setting(DEFAULT_LADDERS) and audit.payload["to"] == wanted
    html = admin.get("/admin/badges").text
    assert 'name="threshold_rare_gold" value="8"' in html and 'name="threshold_frequent_gold" value="100"' in html


def test_saving_the_form_untouched_records_nothing(admin: TestClient, migrated: Settings) -> None:
    assert submit(admin, "/admin/badges", form(*FREQUENT)).status_code == 303
    assert audits(migrated) == [] and "badge_thresholds" not in rows(migrated, HouseholdRow)[0].settings
    submit(admin, "/admin/badges", form(2, 4, 8, 16, 32))
    assert submit(admin, "/admin/badges", form(2, 4, 8, 16, 32)).status_code == 303
    assert len(audits(migrated)) == 1


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ((1, 10, 10, 100, 200), "Each tier needs a higher count"),
        ((10, 5, 50, 100, 200), "Each tier needs a higher count"),
        ((0, 10, 50, 100, 200), "The First count must be a whole number from 1 to 10000."),
        ((1, 10, 50, 100, 10001), "The Diamond count must be a whole number from 1 to 10000."),
        ((1, "ten", 50, 100, 200), "The Bronze count must be a whole number."),
        ((1, "", 50, 100, 200), "The Bronze count must be a whole number."),
        ((1, "-5", 50, 100, 200), "The Bronze count must be a whole number."),
        ((1, "2.5", 50, 100, 200), "The Bronze count must be a whole number."),
        ((1, "1e3", 50, 100, 200), "The Bronze count must be a whole number."),
        ((1, "9" * 12, 50, 100, 200), "The Bronze count must be a whole number."),
    ],
)
def test_bad_counts_are_refused_and_shown_again(
    admin: TestClient, migrated: Settings, counts: Any, message: str
) -> None:
    response = submit(admin, "/admin/badges", form(*counts))
    assert response.status_code == 400 and message in page(response)
    assert audits(migrated) == [] and "badge_thresholds" not in rows(migrated, HouseholdRow)[0].settings
    for name, value in form(*counts).items():
        if value and "<" not in value:
            assert f'name="{name}" value="{value}"' in response.text


def test_a_bad_count_names_its_ladder(admin: TestClient, migrated: Settings) -> None:
    response = submit(admin, "/admin/badges", form(1, 5, 5, 50, 100, ladder=Ladder.OCCASIONAL))
    assert response.status_code == 400 and "Occasional ladder: Each tier needs a higher count" in page(response)
    response = submit(admin, "/admin/badges", form(1, "x", 10, 25, 50, ladder=Ladder.RARE))
    assert response.status_code == 400 and "Rare ladder: The Bronze count must be a whole number." in page(response)
    assert audits(migrated) == [] and "badge_thresholds" not in rows(migrated, HouseholdRow)[0].settings


def test_a_missing_field_is_refused_not_a_crash(admin: TestClient) -> None:
    response = submit(admin, "/admin/badges", {"threshold_frequent_first": "1"})
    assert response.status_code == 400


def test_reset_puts_back_the_defaults_and_audits(admin: TestClient, migrated: Settings) -> None:
    assert reset(admin).status_code == 303
    assert resets(migrated) == []  # nothing to change, nothing recorded
    submit(admin, "/admin/badges", form(2, 4, 8, 16, 32))
    done = reset(admin)
    assert (done.status_code, done.headers["location"]) == (303, "/admin/badges?notice=reset")
    assert "Badge counts reset to the defaults." in admin.get("/admin/badges?notice=reset").text
    assert rows(migrated, HouseholdRow)[0].settings["badge_thresholds"] == ladders_setting(DEFAULT_LADDERS)
    (audit,) = resets(migrated)
    assert audit.payload["from"]["frequent"] == [2, 4, 8, 16, 32]
    assert audit.payload["to"] == ladders_setting(DEFAULT_LADDERS)
    assert 'name="threshold_frequent_first" value="1"' in admin.get("/admin/badges").text


def resets(settings: Settings) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == "household.badge_thresholds_reset"]


def test_only_admins_reach_the_badges_form(house: Settings, provider: FakeProvider, client: TestClient) -> None:
    sign_in_as(client, provider, "kim@example.com")
    assert client.get("/admin/badges").status_code == 403
    assert submit(client, "/admin/badges", form(1, 2, 3, 4, 5)).status_code == 403
    assert reset(client).status_code == 403
    assert (
        audits(house) == [] and resets(house) == [] and "badge_thresholds" not in rows(house, HouseholdRow)[0].settings
    )


def test_the_badges_form_checks_csrf_and_origin(admin: TestClient, migrated: Settings) -> None:
    body = form(1, 2, 3, 4, 5)
    assert admin.post("/admin/badges", data=body).status_code == 403
    assert admin.post("/admin/badges", data=body | {"csrf_token": "0" * 64}).status_code == 403
    foreign = admin.post(
        "/admin/badges", data=body | {"csrf_token": csrf_from(admin)}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert audits(migrated) == []


def test_the_reset_checks_csrf_and_origin(admin: TestClient, migrated: Settings) -> None:
    submit(admin, "/admin/badges", form(2, 4, 8, 16, 32))
    assert admin.post("/admin/badges/reset").status_code == 403
    assert admin.post("/admin/badges/reset", data={"csrf_token": "0" * 64}).status_code == 403
    foreign = admin.post(
        "/admin/badges/reset", data={"csrf_token": csrf_from(admin)}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert resets(migrated) == []
    assert rows(migrated, HouseholdRow)[0].settings["badge_thresholds"]["frequent"] == [2, 4, 8, 16, 32]


# Review fixes ------------------------------------------------------------------------------------------


def test_household_labels_have_no_you() -> None:
    from dinnerbell.web.routes.stats import HOUSEHOLD_LABELS, KIND_LABELS

    assert set(HOUSEHOLD_LABELS) == set(KIND_LABELS)
    assert not [label for label in HOUSEHOLD_LABELS.values() if re.search(r"\byou", label, re.I)]
    assert HOUSEHOLD_LABELS[Kind.GAVE_FEEDBACK] == "Feedback given"


def test_the_household_view_lists_food_critic_like_any_badge_without_feedback_content(
    house: Settings, provider: FakeProvider, client: TestClient
) -> None:
    kim = member(house, "kim@example.com").id
    award(house, kim, "food_critic", Tier.FIRST, ago=timedelta(hours=1))
    award(house, kim, "chef", Tier.FIRST, ago=timedelta(hours=2))
    html = page(client.get("/stats"))  # signed in as the admin
    lately = html[html.index("Lately earned") :]
    assert "Chef · First" in lately and "Food Critic · First" in lately
    assert "2 badges earned together" in html
    sign_in_as(client, provider, "kim@example.com")
    mine = page(client.get("/stats"))
    assert "Food Critic<span" in mine[: mine.index("Lately earned")]
    assert "Food Critic · First" in mine[mine.index("Lately earned") :]
    assert "loved" not in html.lower() and "not again" not in html.lower()  # FR-49: never what anyone said


def test_no_page_repeats_an_id(house: Settings, client: TestClient) -> None:
    me = member(house, "admin@example.com").id
    for key in ("chef", "scout", "explorer"):
        award(house, me, key, Tier.GOLD)
        award(house, me, key, Tier.DIAMOND)
    html = client.get("/stats").text
    found = re.findall(r'\bid="([^"]+)"', html)
    assert len(found) == len(set(found)), sorted(i for i in found if found.count(i) > 1)
    assert sum(i.startswith("badge-") for i in found) >= 6


def test_a_hostile_member_name_is_escaped_on_the_stats_page(house: Settings, admin: TestClient) -> None:
    from dinnerbell.db.models import MemberRow

    kim = member(house, "kim@example.com")
    engine = create_engine(house.sync_database_url)
    with Session(engine) as db:
        db.get(MemberRow, kim.id).display_name = "<b>x</b>"  # type: ignore[union-attr]
        db.commit()
    engine.dispose()
    award(house, kim.id, "chef", Tier.FIRST)
    html = admin.get("/stats").text
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


@pytest.mark.parametrize("junk", [" " * 40 + "x", "9" * 40, "\uff11\uff12", "²", "1 " * 20])
def test_adversarial_count_input_is_refused_before_conversion(
    admin: TestClient, migrated: Settings, junk: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Work done, not elapsed time: the check must reject junk before int() ever sees it.
    from dinnerbell.web.routes import stats

    def guarded(text: str) -> int:
        assert text != junk.strip(), "int() ran on input the check should have refused"
        return int(text)

    monkeypatch.setattr(stats, "int", guarded, raising=False)
    response = submit(admin, "/admin/badges", form(1, junk, 50, 100, 200))
    assert response.status_code == 400 and "must be a whole number" in page(response)
    assert audits(migrated) == []
