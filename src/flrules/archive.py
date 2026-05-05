"""
Archive — independent witness via the Internet Archive Wayback Machine.

Submits scraped FAR notice URLs to the Wayback Machine so we have a third-party,
timestamped copy of what flrules.org served at the moment we scraped it. Lets us
detect retroactive edits or per-bot content tampering.

Two APIs in use, both free and unauthenticated:
  - Save Page Now:  https://web.archive.org/save/<url>   (triggers a fresh snapshot)
  - Availability:   https://archive.org/wayback/available?url=<url>&timestamp=...

All functions fail-soft: any error returns an empty/None result and logs a warning.
The pipeline must never break because the archive is unreachable.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx
import structlog

from flrules.config import settings

log = structlog.get_logger()

SAVE_ENDPOINT = "https://web.archive.org/save/"
AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"
SNAPSHOT_BASE = "https://web.archive.org/web/"

# Tamper-detection threshold. A SequenceMatcher ratio below this between our scrape
# and the archive's copy of the same page gets flagged. 0.85 tolerates whitespace,
# minor markup, and ad/analytics noise but catches substantive content changes.
TAMPER_SIMILARITY_THRESHOLD = 0.85


@dataclass
class ArchiveResult:
    success: bool
    wayback_url: str = ""
    timestamp: str = ""  # YYYYMMDDHHMMSS as returned by the Wayback Machine
    error: str = ""


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=timeout,
        follow_redirects=True,
    )


async def submit_to_archive(url: str) -> ArchiveResult:
    """
    Ask the Wayback Machine to capture `url` right now.

    Returns ArchiveResult with success=False on any failure. The pipeline treats
    a failed submission as a missing field, not an error to propagate.
    """
    if not url:
        return ArchiveResult(success=False, error="empty_url")

    save_url = f"{SAVE_ENDPOINT}{url}"
    try:
        async with _client(timeout=60.0) as client:
            resp = await client.get(save_url)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        log.warning("archive_submit_network_error", url=url, error=str(e))
        return ArchiveResult(success=False, error=f"network: {e}")
    except Exception as e:  # pragma: no cover - defensive catchall
        log.warning("archive_submit_unexpected_error", url=url, error=str(e))
        return ArchiveResult(success=False, error=f"unexpected: {e}")

    # Save Page Now returns the snapshot URL in either the Content-Location header
    # or the final response URL after redirects. Both are stable patterns.
    location = resp.headers.get("content-location", "") or resp.headers.get("Content-Location", "")
    final_url = str(resp.url)

    snapshot_url = ""
    timestamp = ""

    if location.startswith("/web/"):
        snapshot_url = f"https://web.archive.org{location}"
    elif final_url.startswith(SNAPSHOT_BASE):
        snapshot_url = final_url

    if snapshot_url:
        ts_match = re.search(r"/web/(\d{14})/", snapshot_url)
        if ts_match:
            timestamp = ts_match.group(1)

    if not snapshot_url:
        log.warning(
            "archive_submit_no_snapshot",
            url=url,
            status=resp.status_code,
            location=location,
            final_url=final_url,
        )
        return ArchiveResult(
            success=False, error=f"no_snapshot_url (status={resp.status_code})"
        )

    log.info("archive_submitted", url=url, snapshot=snapshot_url, timestamp=timestamp)
    return ArchiveResult(success=True, wayback_url=snapshot_url, timestamp=timestamp)


async def check_availability(url: str, timestamp: str = "") -> ArchiveResult:
    """
    Look up an existing Wayback snapshot for `url`, optionally near `timestamp`.

    Useful when Save Page Now is rate-limited — we can still record the most recent
    pre-existing snapshot rather than triggering a fresh one.
    """
    if not url:
        return ArchiveResult(success=False, error="empty_url")

    params: dict[str, str] = {"url": url}
    if timestamp:
        params["timestamp"] = timestamp

    try:
        async with _client(timeout=15.0) as client:
            resp = await client.get(AVAILABILITY_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
        log.warning("archive_availability_error", url=url, error=str(e))
        return ArchiveResult(success=False, error=str(e))
    except ValueError as e:  # JSON decode error
        log.warning("archive_availability_bad_json", url=url, error=str(e))
        return ArchiveResult(success=False, error=f"bad_json: {e}")

    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available"):
        return ArchiveResult(success=False, error="no_snapshot")

    return ArchiveResult(
        success=True,
        wayback_url=closest.get("url", ""),
        timestamp=closest.get("timestamp", ""),
    )


def _normalize_text(text: str) -> str:
    """Collapse whitespace so trivial reflows don't flag as tampering."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


async def compare_with_snapshot(scraped_text: str, snapshot_url: str) -> tuple[bool, float]:
    """
    Fetch the Wayback snapshot and compare its visible text to `scraped_text`.

    Returns (tamper_detected, similarity_ratio). On any error the call returns
    (False, 0.0) — we don't want spurious tamper alerts when the archive is just
    unreachable. The caller can distinguish "low similarity" (tampered) from
    "could not check" (similarity == 0.0 with tamper_detected == False) using
    its own logging.
    """
    if not scraped_text or not snapshot_url:
        return (False, 0.0)

    try:
        from bs4 import BeautifulSoup

        async with _client(timeout=30.0) as client:
            resp = await client.get(snapshot_url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        snapshot_text = soup.get_text(separator="\n", strip=True)
    except Exception as e:
        log.warning("archive_compare_fetch_failed", snapshot_url=snapshot_url, error=str(e))
        return (False, 0.0)

    a = _normalize_text(scraped_text)
    b = _normalize_text(snapshot_text)
    if not a or not b:
        return (False, 0.0)

    ratio = SequenceMatcher(None, a, b).ratio()
    tampered = ratio < TAMPER_SIMILARITY_THRESHOLD
    if tampered:
        log.warning(
            "archive_tamper_suspected",
            snapshot_url=snapshot_url,
            similarity=round(ratio, 3),
            threshold=TAMPER_SIMILARITY_THRESHOLD,
        )
    return (tampered, ratio)
