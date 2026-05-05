"""Tests for the cryptographic provenance chain.

The chain's value depends on three properties:
  - Determinism: same inputs always produce the same hash.
  - Sensitivity: any substantive change produces a different hash.
  - Linkage: tampering with any earlier entry is detectable downstream.

These are pure functions, so the tests are pure too — no mocks, no I/O.
"""

from flrules.provenance import (
    GENESIS_PREV_HASH,
    build_entry,
    compute_chain_hash,
    compute_content_hash,
    verify_chain,
)


class TestContentHash:
    def test_deterministic(self):
        kw = dict(
            notice_id=12345,
            section_number=2,
            agency_code="40C",
            description="Notice of Proposed Rule",
            full_text="Body text",
        )
        assert compute_content_hash(**kw) == compute_content_hash(**kw)

    def test_whitespace_insensitive(self):
        a = compute_content_hash(
            notice_id=1, section_number=2, agency_code="40C",
            description="Notice  of   Proposed Rule\n",
            full_text="Body text",
        )
        b = compute_content_hash(
            notice_id=1, section_number=2, agency_code="40C",
            description="notice of proposed rule",
            full_text="body text",
        )
        assert a == b

    def test_substantive_change_changes_hash(self):
        a = compute_content_hash(
            notice_id=1, section_number=2, agency_code="40C",
            description="Designate Foundation X as terrorist org",
            full_text="",
        )
        b = compute_content_hash(
            notice_id=1, section_number=2, agency_code="40C",
            description="Designate Foundation Y as terrorist org",
            full_text="",
        )
        assert a != b

    def test_section_change_changes_hash(self):
        kw = dict(notice_id=1, agency_code="40C", description="x", full_text="")
        assert compute_content_hash(section_number=2, **kw) != compute_content_hash(
            section_number=4, **kw
        )

    def test_known_vector(self):
        # If anyone ever changes the normalization or separator scheme, this
        # test will catch it — published chain hashes would silently break.
        h = compute_content_hash(
            notice_id=1,
            section_number=2,
            agency_code="40C",
            description="hello",
            full_text="world",
        )
        # Recomputed once and pinned. Format: SHA256("1\x1f2\x1f40c\x1fhello\x1fworld")
        import hashlib
        expected = hashlib.sha256(b"1\x1f2\x1f40c\x1fhello\x1fworld").hexdigest()
        assert h == expected


class TestChainHash:
    def test_links_in_order(self):
        e1 = build_entry(
            notice_id=1, section_number=2, agency_code="x",
            description="first", full_text="", prev_chain_hash=GENESIS_PREV_HASH,
        )
        e2 = build_entry(
            notice_id=2, section_number=2, agency_code="x",
            description="second", full_text="", prev_chain_hash=e1.chain_hash,
        )
        ok, idx = verify_chain([e1, e2])
        assert ok
        assert idx == -1

    def test_genesis_required(self):
        # An entry whose prev pointer is not the genesis hash must fail
        # verification when treated as the start of the chain.
        e1 = build_entry(
            notice_id=1, section_number=2, agency_code="x",
            description="first", full_text="", prev_chain_hash="bad_prev",
        )
        ok, idx = verify_chain([e1])
        assert not ok
        assert idx == 0

    def test_tampered_middle_entry_detected(self):
        e1 = build_entry(
            notice_id=1, section_number=2, agency_code="x",
            description="first", full_text="", prev_chain_hash=GENESIS_PREV_HASH,
        )
        e2 = build_entry(
            notice_id=2, section_number=2, agency_code="x",
            description="second", full_text="", prev_chain_hash=e1.chain_hash,
        )
        e3 = build_entry(
            notice_id=3, section_number=2, agency_code="x",
            description="third", full_text="", prev_chain_hash=e2.chain_hash,
        )
        # Replace e2's content_hash with something forged.
        from flrules.provenance import ChainEntry
        forged = ChainEntry(
            notice_id=e2.notice_id,
            content_hash="forged" + "0" * 58,
            prev_chain_hash=e2.prev_chain_hash,
            chain_hash=e2.chain_hash,  # unchanged on purpose
        )
        ok, idx = verify_chain([e1, forged, e3])
        assert not ok
        assert idx == 1  # the forged entry's chain_hash no longer matches its content

    def test_two_notices_with_same_content_distinct_hashes(self):
        # The notice_id is included in the chain hash specifically so that
        # accidentally identical content does not produce identical chain hashes.
        a = compute_chain_hash(GENESIS_PREV_HASH, 1, "samehash")
        b = compute_chain_hash(GENESIS_PREV_HASH, 2, "samehash")
        assert a != b
