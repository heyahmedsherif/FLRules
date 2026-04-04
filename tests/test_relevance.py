"""Tests for the relevance filtering engine."""

from flrules.relevance import THRESHOLD, score_notice
from flrules.scraper import ScrapedNotice


def _make_notice(description: str = "", full_text: str = "") -> ScrapedNotice:
    return ScrapedNotice(
        notice_id=1,
        issue_iid=1,
        section_number=2,
        section_name="Proposed Rules",
        agency_code="99",
        description=description,
        publish_date="4/3/2026",
        url="https://example.com",
        full_text=full_text,
    )


class TestDomesticTerrorism:
    def test_direct_match(self):
        n = _make_notice(description="Designation of domestic terrorist organizations")
        r = score_notice(n)
        assert r.matched
        assert "domestic_terrorism" in r.categories
        assert r.score >= THRESHOLD

    def test_material_support(self):
        n = _make_notice(full_text="Prohibiting material support for terrorist activities")
        r = score_notice(n)
        assert r.matched
        assert "domestic_terrorism" in r.categories

    def test_foreign_terrorist(self):
        n = _make_notice(description="Foreign terrorist organization list update")
        r = score_notice(n)
        assert r.matched


class TestCabinetMeeting:
    def test_cabinet_meeting(self):
        n = _make_notice(description="Governor and Cabinet Meeting Agenda for April")
        r = score_notice(n)
        assert r.matched
        assert "cabinet_meeting" in r.categories

    def test_executive_order(self):
        n = _make_notice(description="Executive Order 2026-42")
        r = score_notice(n)
        assert r.matched


class TestReligiousFreedom:
    def test_mosque(self):
        n = _make_notice(full_text="Zoning permit application for new mosque construction")
        r = score_notice(n)
        assert r.matched
        assert "religious_freedom" in r.categories

    def test_islamic_organization(self):
        n = _make_notice(description="Registration of Islamic Society of Central Florida")
        r = score_notice(n)
        assert r.matched

    def test_hijab(self):
        n = _make_notice(full_text="Dress code policy amendments regarding hijab in schools")
        r = score_notice(n)
        assert r.matched

    def test_sharia(self):
        n = _make_notice(description="Proposed rule regarding sharia law application")
        r = score_notice(n)
        assert r.matched


class TestImmigration:
    def test_refugee(self):
        n = _make_notice(description="Refugee resettlement program modifications")
        r = score_notice(n)
        assert r.matched
        assert "immigration" in r.categories

    def test_travel_ban(self):
        n = _make_notice(full_text="Implementation of travel ban provisions")
        r = score_notice(n)
        assert r.matched


class TestSurveillance:
    def test_fusion_center(self):
        n = _make_notice(description="Florida Fusion Center operational updates")
        r = score_notice(n)
        assert r.matched
        assert "surveillance" in r.categories

    def test_counter_extremism(self):
        n = _make_notice(full_text="Countering violent extremism program grants")
        r = score_notice(n)
        assert r.matched


class TestCivilRights:
    def test_hate_crime(self):
        n = _make_notice(description="Hate crime reporting requirements amendment")
        r = score_notice(n)
        assert r.matched
        assert "civil_rights" in r.categories

    def test_profiling(self):
        n = _make_notice(full_text="Racial and religious profiling prevention guidelines")
        r = score_notice(n)
        assert r.matched


class TestNonRelevant:
    def test_water_management(self):
        n = _make_notice(description="Water management district boundary adjustment")
        r = score_notice(n)
        assert not r.matched

    def test_fishing_license(self):
        n = _make_notice(description="Recreational fishing license fee schedule update")
        r = score_notice(n)
        assert not r.matched

    def test_nursing_home_beds(self):
        n = _make_notice(
            description="Community Nursing Home Beds Fixed Need Pool determination"
        )
        r = score_notice(n)
        assert not r.matched


class TestMultiCategoryMatch:
    def test_combined_score(self):
        n = _make_notice(
            description="Cabinet meeting on domestic terrorism designations",
            full_text="Review of organizations for terrorist designation by Governor and Cabinet",
        )
        r = score_notice(n)
        assert r.matched
        assert r.score >= 5.0  # cabinet_meeting (2) + domestic_terrorism (3)
        assert "cabinet_meeting" in r.categories
        assert "domestic_terrorism" in r.categories
