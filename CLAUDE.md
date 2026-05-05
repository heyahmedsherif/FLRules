# FLRules Monitor

## Origin

This project was born from the question: *"Is there a bot we could set up to monitor the Florida Administrative Registrar to watch out for domestic terrorism organization designations and cabinet meetings?"* — referring to https://flrules.org/bigdoc/default.asp

Built for **CAIR-FL** (Council on American-Islamic Relations — Florida) at https://www.cairfl.org. The goal is to give CAIR-FL staff early warning when state agencies publish rules, emergency orders, or meeting notices that could affect Muslim communities in Florida. Operated by Intuitive Dataframe, LLC.

## What This Is

An automated monitoring system for the **Florida Administrative Register (FAR)**. It scrapes the FAR daily, scores notices against relevance categories (domestic terrorism designations, religious freedom, surveillance, immigration, civil rights, etc.), and alerts opted-in subscribers via SMS and email.

## Architecture

```
GitHub Actions Cron → scraper.py → relevance.py → pipeline.py → notifier.py
                                                      │               │
                                                   SQLite DB    Twilio SMS / SMTP Email
                                                      │
                                                static_site.py → GitHub Pages
```

**Web dashboard** (FastAPI + Google OAuth) available for self-hosted deployment.

## Key Components

| File | Purpose |
|------|---------|
| `src/flrules/scraper.py` | Polite crawl of FAR sections (2s delay, custom UA) |
| `src/flrules/relevance.py` | Keyword pattern matching with weighted categories, threshold=1.0 |
| `src/flrules/pipeline.py` | Orchestrator: scrape → filter → store → notify → build site |
| `src/flrules/notifier.py` | SMS (Twilio) + email (SMTP) + local JSON audit log |
| `src/flrules/web.py` | FastAPI dashboard with Google OAuth, admin panel |
| `src/flrules/auth.py` | Google OAuth 2.0 + role-based access (admin/member) |
| `src/flrules/static_site.py` | Generates self-contained HTML + chain.json for GitHub Pages |
| `src/flrules/cli.py` | CLI: `flrules run`, `scrape-test`, `subscribe`, `alerts`, etc. |
| `src/flrules/models.py` | SQLModel ORM: User, FARIssue, FARNotice, Alert, Subscriber |
| `src/flrules/config.py` | Pydantic settings from `.env` |
| `src/flrules/db.py` | Async SQLite engine + idempotent additive migrations |
| `src/flrules/archive.py` | Wayback Machine submit/availability/compare (opt-in via `ARCHIVE_ENABLED`) |
| `src/flrules/provenance.py` | Pure SHA-256 hash chain over substantive notice content (always on) |

## Integrity features

Three guards against post-scrape FAR manipulation:
- **Wayback archiving** — opt-in (`ARCHIVE_ENABLED=true`). Each notice URL → Save Page Now → snapshot stored on `FARNotice.wayback_url`.
- **Hash chain** — always on, no config. `content_hash` = SHA-256 of substantive content; `chain_hash` = SHA-256(prev_chain_hash || notice_id || content_hash). Chain head published to `site/chain.json` so external observers can pin it.
- **Disappearance detection** — implemented but **dormant** (off by default). Re-scrapes recent issues, alerts on missing notice IDs. Enable with `VERIFY_DISAPPEARANCES=true`. Skipped for now because legitimate withdrawals would trigger false positives that CAIR-FL doesn't have triage capacity for; revisit when needed.

## Deployment

- **Primary:** GitHub Actions cron (hourly weekdays, 2x weekends) → GitHub Pages static dashboard
- **DB persistence:** SQLite uploaded/downloaded as GitHub Actions artifact between runs
- **Web dashboard:** Optional self-hosted FastAPI (Docker or local)
- **Static site:** https://heyahmedsherif.github.io/FLRules/

## External Services

| Service | Purpose | Status |
|---------|---------|--------|
| flrules.org | Source data (FAR) | Working |
| Twilio | SMS alerts (+18779592390) | Pending toll-free verification resubmission |
| SMTP | Email alerts | Not configured yet |
| Google OAuth | Dashboard login | Configured |
| GitHub Pages | Static dashboard hosting | Working |
| GitHub Actions | Scheduled pipeline runs | Working |

## Current Status & Known Issues

- **Twilio SMS:** Toll-free verification for +18779592390 was rejected (reason: verbal consent script missing). Resubmitted 2026-04-14 with proper consent language and opt-in confirmation SMS. Awaiting approval.
- **Email:** Configured via Gmail SMTP (thatahmedsherif@gmail.com with app password). Works for now but will need to move to a custom domain (e.g. alerts@cairfl.org) or a service like Resend/Sendgrid for production.
- **Opt-in compliance:** Code updated 2026-04-14 to include "Reply STOP to unsubscribe" on all SMS and send opt-in confirmation SMS on subscribe.
- **Access control:** Currently any Google account can log in and get `member` role. For production, needs invite-only / admin-approval gate so only CAIR-FL staff can access. Plan: add `status` field to User model (approved/pending/denied), admin approval UI.

## Conventions

- **Async throughout:** All DB operations and HTTP calls are async (aiosqlite, httpx, AsyncSession)
- **Graceful degradation:** Missing Twilio/SMTP config logs warnings, doesn't crash
- **Polite scraping:** 2-second delay between requests, honest User-Agent, only public FAR sections
- **HTML rendering:** Web dashboard uses inline HTML in Python (no separate template files for most pages) — ruff long-line checks are disabled for web.py and static_site.py
- **Entry point:** `flrules` CLI via Click, registered in pyproject.toml

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest -v              # Run tests
ruff check src/ tests/ # Lint
flrules scrape-test    # Test scraper against live FAR (no DB, no notify)
flrules run --issues 3 --no-notify  # Dry run
```

## Relevance Categories (weights)

| Category | Weight | Triggers on |
|----------|--------|-------------|
| domestic_terrorism | 3.0 | Organization designations, material support rules |
| religious_freedom | 3.0 | Mosque, Islamic org, hijab, halal, sharia |
| surveillance | 2.5 | Fusion centers, biometrics, CVE, watchlists |
| cabinet_meeting | 2.0 | Governor/Cabinet agendas, executive orders |
| immigration | 2.0 | Refugee, asylum, travel ban, ICE cooperation |
| civil_rights | 2.0 | Hate crimes, profiling, discrimination |
| education | 1.5 | Curriculum bans, DEI restrictions, book bans |
| nonprofit_regulation | 1.5 | Charity registration, foreign agent rules |
| policy_general | 0.5 | Emergency rules, public comment periods |

Alert threshold: combined score >= 1.0
