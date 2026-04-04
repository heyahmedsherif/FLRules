"""Tests for the scraper module (unit tests with mocked HTTP)."""

import pytest

from flrules.scraper import ScrapedNotice, fetch_latest_issue_ids

MOCK_INDEX_HTML = """
<html><body>
<a href="View_Faw.asp?IID=4616">April 3, 2026</a>
<a href="View_Faw.asp?IID=4615">April 2, 2026</a>
<a href="View_Faw.asp?IID=4614">April 1, 2026</a>
<a href="View_Faw.asp?IID=4613">March 31, 2026</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_latest_issue_ids(httpx_mock):
    httpx_mock.add_response(url="https://flrules.org/bigDoc/Default.asp", html=MOCK_INDEX_HTML)
    issues = await fetch_latest_issue_ids(count=3)
    assert len(issues) == 3
    assert issues[0]["iid"] == 4616
    assert issues[2]["iid"] == 4614


def test_scraped_notice_dataclass():
    n = ScrapedNotice(
        notice_id=12345,
        issue_iid=4616,
        section_number=6,
        section_name="Meetings",
        agency_code="40C",
        description="Test meeting notice",
        publish_date="4/3/2026",
        url="https://flrules.org/Gateway/View_notice.asp?id=12345",
    )
    assert n.notice_id == 12345
    assert n.full_text == ""
