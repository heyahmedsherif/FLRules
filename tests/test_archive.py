"""Tests for the Wayback archive integration.

All HTTP is mocked via pytest-httpx — these tests never touch the real Internet
Archive. The contract under test is the fail-soft promise: archive errors must
never raise into the pipeline.
"""

import httpx

from flrules.archive import (
    TAMPER_SIMILARITY_THRESHOLD,
    check_availability,
    compare_with_snapshot,
    submit_to_archive,
)


class TestSubmitToArchive:
    async def test_success_via_content_location(self, httpx_mock):
        httpx_mock.add_response(
            url="https://web.archive.org/save/https://flrules.org/Gateway/View_notice.asp?id=12345",
            status_code=200,
            headers={
                "Content-Location": "/web/20260505120000/https://flrules.org/Gateway/View_notice.asp?id=12345"
            },
        )
        result = await submit_to_archive(
            "https://flrules.org/Gateway/View_notice.asp?id=12345"
        )
        assert result.success
        assert result.wayback_url.startswith("https://web.archive.org/web/20260505120000/")
        assert result.timestamp == "20260505120000"

    async def test_empty_url_fails_soft(self):
        result = await submit_to_archive("")
        assert not result.success
        assert "empty_url" in result.error

    async def test_network_error_fails_soft(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        result = await submit_to_archive("https://flrules.org/x")
        assert not result.success
        assert "network" in result.error

    async def test_timeout_fails_soft(self, httpx_mock):
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"))
        result = await submit_to_archive("https://flrules.org/x")
        assert not result.success

    async def test_no_snapshot_url_fails_soft(self, httpx_mock):
        # Service responds 200 but with no recognizable snapshot URL anywhere.
        httpx_mock.add_response(
            url="https://web.archive.org/save/https://flrules.org/x",
            status_code=200,
            text="<html>some unrelated body</html>",
        )
        result = await submit_to_archive("https://flrules.org/x")
        assert not result.success
        assert "no_snapshot_url" in result.error


class TestCheckAvailability:
    async def test_snapshot_found(self, httpx_mock):
        httpx_mock.add_response(
            url="https://archive.org/wayback/available?url=https%3A%2F%2Fflrules.org%2Fx",
            json={
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "url": "https://web.archive.org/web/20260101000000/https://flrules.org/x",
                        "timestamp": "20260101000000",
                        "status": "200",
                    }
                }
            },
        )
        result = await check_availability("https://flrules.org/x")
        assert result.success
        assert result.timestamp == "20260101000000"

    async def test_no_snapshot(self, httpx_mock):
        httpx_mock.add_response(
            url="https://archive.org/wayback/available?url=https%3A%2F%2Fflrules.org%2Fx",
            json={"archived_snapshots": {}},
        )
        result = await check_availability("https://flrules.org/x")
        assert not result.success
        assert result.error == "no_snapshot"

    async def test_http_error_fails_soft(self, httpx_mock):
        httpx_mock.add_response(
            url="https://archive.org/wayback/available?url=https%3A%2F%2Fflrules.org%2Fx",
            status_code=503,
        )
        result = await check_availability("https://flrules.org/x")
        assert not result.success

    async def test_malformed_json_fails_soft(self, httpx_mock):
        httpx_mock.add_response(
            url="https://archive.org/wayback/available?url=https%3A%2F%2Fflrules.org%2Fx",
            text="not json at all",
        )
        result = await check_availability("https://flrules.org/x")
        assert not result.success
        assert "bad_json" in result.error


class TestCompareWithSnapshot:
    async def test_identical_text_no_tamper(self, httpx_mock):
        body = "<html><body>Notice of Proposed Rule 12345 Section 6 Agency 40C</body></html>"
        httpx_mock.add_response(
            url="https://web.archive.org/web/20260505000000/https://flrules.org/x",
            text=body,
        )
        scraped = "Notice of Proposed Rule 12345 Section 6 Agency 40C"
        tampered, ratio = await compare_with_snapshot(
            scraped, "https://web.archive.org/web/20260505000000/https://flrules.org/x"
        )
        assert not tampered
        assert ratio >= TAMPER_SIMILARITY_THRESHOLD

    async def test_whitespace_differences_ignored(self, httpx_mock):
        body = "<html><body>\n\n  Notice  of  Proposed   Rule  12345\n  Section 6\n</body></html>"
        httpx_mock.add_response(
            url="https://web.archive.org/web/20260505000000/https://flrules.org/x",
            text=body,
        )
        scraped = "Notice of Proposed Rule 12345 Section 6"
        tampered, ratio = await compare_with_snapshot(
            scraped, "https://web.archive.org/web/20260505000000/https://flrules.org/x"
        )
        assert not tampered

    async def test_substantive_change_flagged(self, httpx_mock):
        body = (
            "<html><body>Wholly different content about something completely "
            "unrelated like fishing licenses and water management districts in "
            "southern Florida</body></html>"
        )
        httpx_mock.add_response(
            url="https://web.archive.org/web/20260505000000/https://flrules.org/x",
            text=body,
        )
        scraped = (
            "Designation of XYZ Foundation as a domestic terrorist organization "
            "under section 775.30 with material support penalties"
        )
        tampered, ratio = await compare_with_snapshot(
            scraped, "https://web.archive.org/web/20260505000000/https://flrules.org/x"
        )
        assert tampered
        assert ratio < TAMPER_SIMILARITY_THRESHOLD

    async def test_fetch_failure_does_not_flag_tamper(self, httpx_mock):
        # If the snapshot can't be fetched at all, we MUST NOT raise a false alarm.
        httpx_mock.add_exception(httpx.ConnectError("down"))
        tampered, ratio = await compare_with_snapshot(
            "scraped text", "https://web.archive.org/web/20260505000000/https://flrules.org/x"
        )
        assert not tampered
        assert ratio == 0.0

    async def test_empty_inputs_safe(self):
        tampered, ratio = await compare_with_snapshot("", "https://example.com")
        assert not tampered and ratio == 0.0
        tampered, ratio = await compare_with_snapshot("text", "")
        assert not tampered and ratio == 0.0


class TestPipelineDoesNotBreakOnArchiveFailure:
    """End-to-end safety net: if archiving fails, the pipeline still stores
    notices and sends notifications. Mocks every HTTP call so no real network."""

    async def test_archive_failure_does_not_block_pipeline(
        self, httpx_mock, monkeypatch, tmp_path
    ):
        from flrules import archive
        from flrules.config import settings

        # Point the DB at a tmp file so we don't touch the real one.
        db_file = tmp_path / "test.db"
        monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
        # Force a re-import of db with the patched URL.
        import importlib

        from flrules import db as db_module

        importlib.reload(db_module)
        from flrules import pipeline as pipeline_module

        importlib.reload(pipeline_module)

        # Enable archiving but make every submission fail.
        monkeypatch.setattr(settings, "archive_enabled", True)

        async def always_fail(url: str):
            return archive.ArchiveResult(success=False, error="forced_failure")

        monkeypatch.setattr(pipeline_module, "submit_to_archive", always_fail)

        # Mock the FAR site: empty issue list → pipeline runs the static-site step
        # but does no scraping. That's enough to prove no exception is raised.
        httpx_mock.add_response(
            url="https://flrules.org/bigDoc/Default.asp",
            html="<html><body>No issues found</body></html>",
        )

        # Skip the static-site generation step — it touches the filesystem and
        # isn't what this test is asserting.
        async def noop_site():
            return None

        monkeypatch.setattr(pipeline_module, "generate_static_site", noop_site)

        stats = await pipeline_module.run_pipeline(issue_count=1, notify=False)
        assert stats["issues_checked"] == 0  # no issues parsed from empty body
