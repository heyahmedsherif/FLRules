"""
FAR Scraper — fetches the Florida Administrative Register and extracts notices.

Site structure (from research):
  - Issue index:  /bigDoc/Default.asp             → lists issues by IID
  - Issue page:   /BigDoc/View_Faw.asp?IID=4616   → sections I–XIII
  - Section page: /BigDoc/View_Section.asp?Issue=4616&Section=6
                  → table with columns: View Text, Agency, Description, ID, Date
  - Notice page:  /Gateway/View_notice.asp?id=30743003
"""

import asyncio
import re
from dataclasses import dataclass, field

import httpx
import structlog
from bs4 import BeautifulSoup

from flrules.config import settings

log = structlog.get_logger()

# Sections most relevant to policy monitoring
SECTIONS_TO_MONITOR = {
    1: "Notices of Development / Negotiated Rulemaking",
    2: "Proposed Rules",
    3: "Notices of Changes / Corrections / Withdrawals",
    4: "Emergency Rules",
    6: "Notices of Meetings / Workshops / Hearings",
    12: "Miscellaneous",
    13: "Index of Rules Filed",
}


@dataclass
class ScrapedNotice:
    notice_id: int
    issue_iid: int
    section_number: int
    section_name: str
    agency_code: str
    description: str
    publish_date: str
    url: str
    full_text: str = ""


@dataclass
class ScrapedIssue:
    iid: int
    volume: str
    publish_date: str
    notices: list[ScrapedNotice] = field(default_factory=list)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=30.0,
        follow_redirects=True,
    )


async def fetch_latest_issue_ids(count: int = 5) -> list[dict]:
    """Get the most recent issue IDs from the FAR index page."""
    url = f"{settings.far_base_url}/bigDoc/Default.asp"
    async with _client() as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    issues = []

    # Links follow pattern: View_Faw.asp?IID=4616
    for link in soup.find_all("a", href=re.compile(r"View_Faw\.asp\?IID=(\d+)", re.I)):
        match = re.search(r"IID=(\d+)", link["href"])
        if match:
            iid = int(match.group(1))
            text = link.get_text(strip=True)
            issues.append({"iid": iid, "text": text})

    # Deduplicate and sort descending (most recent first)
    seen = set()
    unique = []
    for issue in issues:
        if issue["iid"] not in seen:
            seen.add(issue["iid"])
            unique.append(issue)

    return unique[:count]


async def scrape_section(
    client: httpx.AsyncClient, iid: int, section_num: int
) -> list[ScrapedNotice]:
    """Scrape all notices from a single section of a FAR issue."""
    section_name = SECTIONS_TO_MONITOR.get(section_num, f"Section {section_num}")
    url = f"{settings.far_base_url}/BigDoc/View_Section.asp?Issue={iid}&Section={section_num}"

    log.info("scraping_section", iid=iid, section=section_num, url=url)
    resp = await client.get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    notices = []

    # Notices are in table rows. Each row has: view icon, agency, description, ID, date.
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # Try to extract the notice ID from a link or cell text
        notice_id = None
        for cell in cells:
            # Look for numeric ID in cell text
            text = cell.get_text(strip=True)
            if text.isdigit() and len(text) > 5:
                notice_id = int(text)
                break

        if not notice_id:
            # Try extracting from link hrefs
            for link in row.find_all("a", href=True):
                match = re.search(r"[iI][dD]=(\d{5,})", link["href"])
                if match:
                    notice_id = int(match.group(1))
                    break

        if not notice_id:
            continue

        # Extract agency code and description from cells
        agency_code = ""
        description = ""
        publish_date = ""

        for i, cell in enumerate(cells):
            text = cell.get_text(strip=True)
            # Agency codes look like "40C", "59C", "5K" etc
            if re.match(r"^\d+[A-Z]?$", text) and len(text) <= 5:
                agency_code = text
            elif "Vol." in text or "/" in text and re.search(r"\d{1,2}/\d{1,2}/\d{4}", text):
                publish_date = text
            elif len(text) > 20 and not text.isdigit():
                description = text

        notice_url = f"{settings.far_base_url}/Gateway/View_notice.asp?id={notice_id}"

        notices.append(
            ScrapedNotice(
                notice_id=notice_id,
                issue_iid=iid,
                section_number=section_num,
                section_name=section_name,
                agency_code=agency_code,
                description=description,
                publish_date=publish_date,
                url=notice_url,
            )
        )

    log.info("section_scraped", iid=iid, section=section_num, notice_count=len(notices))
    return notices


async def fetch_notice_text(client: httpx.AsyncClient, notice_id: int) -> str:
    """Fetch the full text of a single notice."""
    url = f"{settings.far_base_url}/Gateway/View_notice.asp?id={notice_id}"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        # Extract main content area — typically in a content div or table
        # Remove nav/header/footer elements
        for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:10_000]  # cap at 10K chars
    except Exception as e:
        log.warning("notice_fetch_failed", notice_id=notice_id, error=str(e))
        return ""


async def scrape_issue(iid: int, fetch_full_text: bool = True) -> ScrapedIssue:
    """Scrape all monitored sections of a FAR issue."""
    issue = ScrapedIssue(iid=iid, volume="", publish_date="")

    async with _client() as client:
        # First get the issue page to extract volume/date metadata
        issue_url = f"{settings.far_base_url}/BigDoc/View_Faw.asp?IID={iid}"
        resp = await client.get(issue_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Extract volume and date from page text
        page_text = soup.get_text()
        vol_match = re.search(r"Vol[./]\s*(?:No\s*)?(\d+/\d+)", page_text)
        if vol_match:
            issue.volume = vol_match.group(1)

        date_match = re.search(
            r"((?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},\s+\d{4})",
            page_text,
        )
        if date_match:
            issue.publish_date = date_match.group(1)

        # Scrape each monitored section with polite delays
        for section_num in SECTIONS_TO_MONITOR:
            await asyncio.sleep(settings.request_delay_seconds)
            try:
                notices = await scrape_section(client, iid, section_num)
                issue.notices.extend(notices)
            except Exception as e:
                log.error("section_scrape_failed", iid=iid, section=section_num, error=str(e))

        # Optionally fetch full text for each notice
        if fetch_full_text:
            for notice in issue.notices:
                await asyncio.sleep(settings.request_delay_seconds)
                notice.full_text = await fetch_notice_text(client, notice.notice_id)

    log.info(
        "issue_scraped",
        iid=iid,
        volume=issue.volume,
        date=issue.publish_date,
        total_notices=len(issue.notices),
    )
    return issue
