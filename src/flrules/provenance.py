"""
Provenance — cryptographic chain of every FAR notice we have ever observed.

Each notice gets two hashes:

  content_hash  = SHA-256 of the normalized observable content of the notice.
                  Stable across cosmetic whitespace/markup changes, sensitive to
                  any substantive change to agency, description, body, or section.

  chain_hash    = SHA-256(prev_chain_hash || notice_id || content_hash)
                  Each notice's chain hash depends on every notice before it.
                  Tampering with any earlier notice invalidates every subsequent
                  chain hash, so the chain cannot be quietly rewritten.

The current chain head is published in the static site (chain.json) so any
outside observer — a journalist, an academic, a court — can pin the head at a
moment in time and later verify that we did not retroactively edit history.

This module is pure: no I/O, no side effects, no async. Easy to test, easy to
audit, no failure modes other than bad inputs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Sentinel for the very first notice in the chain. SHA-256 of the literal string
# "FLRules Monitor genesis" so the chain has a deterministic, publishable origin.
GENESIS_PREV_HASH = hashlib.sha256(b"FLRules Monitor genesis").hexdigest()


@dataclass(frozen=True)
class ChainEntry:
    notice_id: int
    content_hash: str
    prev_chain_hash: str
    chain_hash: str


def _normalize(text: str) -> str:
    """Canonicalize whitespace and case so trivial reformatting does not change
    the hash. Anything that survives this normalization is considered substantive."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def compute_content_hash(
    *,
    notice_id: int,
    section_number: int,
    agency_code: str,
    description: str,
    full_text: str,
) -> str:
    """Hash the substantive content of a notice. The fields chosen are exactly
    those that, if altered by the publisher, would change the meaning of the
    notice. Cosmetic fields (fetched_at, URL formatting, publish_date string
    formatting) are intentionally excluded so reformatting does not flag as
    tampering."""
    parts = [
        str(notice_id),
        str(section_number),
        _normalize(agency_code),
        _normalize(description),
        _normalize(full_text),
    ]
    payload = "\x1f".join(parts).encode("utf-8")  # 0x1F = ASCII unit separator
    return hashlib.sha256(payload).hexdigest()


def compute_chain_hash(prev_chain_hash: str, notice_id: int, content_hash: str) -> str:
    """Link a notice into the chain. The notice_id is included so two notices
    with identical content (rare but possible) still produce different chain
    hashes."""
    payload = f"{prev_chain_hash}\x1f{notice_id}\x1f{content_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


def build_entry(
    *,
    notice_id: int,
    section_number: int,
    agency_code: str,
    description: str,
    full_text: str,
    prev_chain_hash: str,
) -> ChainEntry:
    """Convenience: compute both hashes and return a ChainEntry."""
    ch = compute_content_hash(
        notice_id=notice_id,
        section_number=section_number,
        agency_code=agency_code,
        description=description,
        full_text=full_text,
    )
    return ChainEntry(
        notice_id=notice_id,
        content_hash=ch,
        prev_chain_hash=prev_chain_hash,
        chain_hash=compute_chain_hash(prev_chain_hash, notice_id, ch),
    )


def verify_chain(entries: list[ChainEntry]) -> tuple[bool, int]:
    """Verify that a chain (in order) is internally consistent.

    Returns (ok, first_bad_index). When ok is True, first_bad_index is -1.
    When ok is False, first_bad_index is the position of the first entry whose
    chain_hash does not match the recomputed value or whose prev pointer does
    not match the previous entry's chain_hash."""
    expected_prev = GENESIS_PREV_HASH
    for i, entry in enumerate(entries):
        if entry.prev_chain_hash != expected_prev:
            return (False, i)
        recomputed = compute_chain_hash(
            entry.prev_chain_hash, entry.notice_id, entry.content_hash
        )
        if recomputed != entry.chain_hash:
            return (False, i)
        expected_prev = entry.chain_hash
    return (True, -1)
