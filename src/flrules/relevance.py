"""
Relevance filtering engine — scores notices against keyword/pattern rules.

Each rule has:
  - patterns: list of regex patterns (case-insensitive)
  - category: classification label
  - weight: how much a match contributes to the overall score
  - context: optional surrounding-text check to reduce false positives

A notice is flagged if its total relevance_score >= THRESHOLD.
"""

import re
from dataclasses import dataclass, field

import structlog

from flrules.scraper import ScrapedNotice

log = structlog.get_logger()

THRESHOLD = 0.01  # TEMP: lowered for email test — revert to 1.0


@dataclass
class RelevanceRule:
    category: str
    patterns: list[str]
    weight: float = 1.0
    context_patterns: list[str] = field(default_factory=list)  # must also match if provided


@dataclass
class MatchResult:
    matched: bool
    score: float
    matched_keywords: list[str]
    categories: list[str]
    summary_reason: str


# ────────────────────────────────────────────────────────────────
#  Rule definitions — organized by concern area
# ────────────────────────────────────────────────────────────────

RULES: list[RelevanceRule] = [
    # ── Domestic terrorism designations ──────────────────────────
    RelevanceRule(
        category="domestic_terrorism",
        patterns=[
            r"domestic\s+terror",
            r"terrorist\s+organi[sz]ation",
            r"organi[sz]ation\s+designation",
            r"designated\s+organi[sz]ation",
            r"terror\s+designation",
            r"material\s+support.*terror",
            r"foreign\s+terrorist",
        ],
        weight=3.0,
    ),
    # ── Cabinet meetings and executive actions ──────────────────
    RelevanceRule(
        category="cabinet_meeting",
        patterns=[
            r"cabinet\s+meeting",
            r"governor.*cabinet",
            r"executive\s+order",
            r"cabinet\s+agenda",
        ],
        weight=2.0,
    ),
    # ── Religious freedom / targeting ───────────────────────────
    RelevanceRule(
        category="religious_freedom",
        patterns=[
            r"religious\s+(?:freedom|liberty|organi[sz]ation|institution)",
            r"faith.based",
            r"house\s+of\s+worship",
            r"mosque",
            r"islamic\s+(?:center|school|organi[sz]ation|societ)",
            r"muslim",
            r"sharia",
            r"halal",
            r"hijab",
            r"religious\s+discrimination",
            r"religious\s+exemption",
        ],
        weight=3.0,
    ),
    # ── Immigration and refugees ────────────────────────────────
    RelevanceRule(
        category="immigration",
        patterns=[
            r"immigra(?:tion|nt)",
            r"refugee",
            r"asylum",
            r"deporta(?:tion|ble)",
            r"visa\s+(?:waiver|restriction|ban)",
            r"travel\s+ban",
            r"customs\s+(?:and\s+)?border",
            r"ice\s+(?:detain|enforce|cooperat)",
            r"sanctuary\s+(?:city|polic)",
            r"e-verify",
        ],
        weight=2.0,
    ),
    # ── Surveillance and law enforcement ────────────────────────
    RelevanceRule(
        category="surveillance",
        patterns=[
            r"surveillance\s+(?:program|authorit|technolog)",
            r"facial\s+recognition",
            r"biometric",
            r"data\s+(?:collection|sharing|retention).*(?:law\s+enforce|intelligen)",
            r"fusion\s+center",
            r"counter.?terror",
            r"counter.*extremis",
            r"cve\s+program",  # Countering Violent Extremism
            r"watch\s*list",
            r"no.fly\s+list",
            r"suspicious\s+activit(?:y|ies)\s+report",
        ],
        weight=2.5,
    ),
    # ── Education ───────────────────────────────────────────────
    RelevanceRule(
        category="education",
        patterns=[
            r"curriculum\s+(?:standard|review|change|ban)",
            r"(?:ban|restrict).*(?:book|text|material)",
            r"(?:diversity|dei|equity|inclusion).*(?:program|training|ban|restrict)",
            r"critical\s+race",
            r"divisive\s+concept",
            r"higher\s+education.*(?:accredit|standard|review)",
        ],
        weight=1.5,
    ),
    # ── Civil rights and discrimination ─────────────────────────
    RelevanceRule(
        category="civil_rights",
        patterns=[
            r"civil\s+rights",
            r"human\s+rights\s+commission",
            r"anti.discrimination",
            r"hate\s+crime",
            r"bias\s+incident",
            r"equal\s+(?:protection|opportunity)",
            r"profiling",
            r"stop.and.frisk",
        ],
        weight=2.0,
    ),
    # ── Non-profit / charity regulation ─────────────────────────
    RelevanceRule(
        category="nonprofit_regulation",
        patterns=[
            r"charit(?:y|able|ies)\s+(?:regulat|registr|report|solicit)",
            r"non.?profit\s+(?:regulat|registr|report|tax)",
            r"501\s*\(?\s*c\s*\)?\s*\(?\s*3",
            r"tax.?exempt\s+(?:status|organi[sz]ation|revoc)",
            r"foreign\s+(?:agent|donation|funding).*(?:regulat|restrict|ban|report)",
        ],
        weight=1.5,
    ),
    # ── Broad policy markers ────────────────────────────────────
    RelevanceRule(
        category="policy_general",
        patterns=[
            r"emergency\s+rule",
            r"rule\s+(?:amendment|repeal|adopt)",
            r"public\s+(?:hearing|comment\s+period)",
        ],
        weight=0.5,  # low weight — only flag when combined with other matches
    ),
]


def score_notice(notice: ScrapedNotice) -> MatchResult:
    """Score a single notice against all relevance rules."""
    searchable = f"{notice.description}\n{notice.full_text}".lower()
    total_score = 0.0
    matched_keywords: list[str] = []
    categories: set[str] = set()

    for rule in RULES:
        rule_matched = False
        for pattern in rule.patterns:
            if re.search(pattern, searchable, re.IGNORECASE):
                # If context_patterns are specified, check they also match
                if rule.context_patterns:
                    context_ok = any(
                        re.search(cp, searchable, re.IGNORECASE)
                        for cp in rule.context_patterns
                    )
                    if not context_ok:
                        continue

                matched_keywords.append(pattern)
                rule_matched = True

        if rule_matched:
            total_score += rule.weight
            categories.add(rule.category)

    is_match = total_score >= THRESHOLD
    cat_list = sorted(categories)
    reason = ""
    if is_match:
        reason = (
            f"Matched categories: {', '.join(cat_list)}. "
            f"Keywords: {', '.join(matched_keywords[:5])}."
        )

    return MatchResult(
        matched=is_match,
        score=total_score,
        matched_keywords=matched_keywords,
        categories=cat_list,
        summary_reason=reason,
    )


def filter_notices(notices: list[ScrapedNotice]) -> list[tuple[ScrapedNotice, MatchResult]]:
    """Filter a list of notices, returning only those above the relevance threshold."""
    results = []
    for notice in notices:
        result = score_notice(notice)
        if result.matched:
            log.info(
                "notice_matched",
                notice_id=notice.notice_id,
                score=result.score,
                categories=result.categories,
            )
            results.append((notice, result))
    log.info("filtering_complete", total=len(notices), matched=len(results))
    return results
