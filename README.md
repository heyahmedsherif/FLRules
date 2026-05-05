# FLRules Monitor

**Florida Administrative Register monitoring system for civil rights organizations.**

Automatically monitors the [Florida Administrative Register](https://flrules.org/bigdoc/default.asp) for rule changes, proposed regulations, emergency rules, and meeting notices relevant to Muslim civil rights — then alerts opted-in members via email and SMS.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    GITHUB ACTIONS CRON                        │
│              (hourly weekdays / 2x weekends)                 │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      PIPELINE                                │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐ │
│  │ Scraper  │──▶│ Relevance│──▶│  Store   │──▶│ Notifier │ │
│  │          │   │ Filter   │   │ (SQLite) │   │          │ │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘ │
│       │                                         │      │     │
│       ▼                                         ▼      ▼     │
│  flrules.org                               Resend  Twilio    │
│  (polite crawl                             (email) (SMS)     │
│   2s delay)                                                  │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                   WEB DASHBOARD                              │
│          FastAPI • stats • alerts • subscribers              │
│               (optional, self-hosted)                        │
└──────────────────────────────────────────────────────────────┘
```

## What It Monitors

The FAR publishes daily. The scraper checks these sections:

| Section | Content | Why It Matters |
|---------|---------|----------------|
| I | Development notices | Early warning of new rules |
| II | Proposed rules | Active rulemaking |
| III | Changes/corrections/withdrawals | Rule modifications |
| IV | Emergency rules | Immediate-effect rules |
| VI | Meetings/workshops/hearings | Public participation opportunities |
| XII | Miscellaneous | Catch-all for designations, orders |
| XIII | Index of rules filed | Final rules tracker |

## Relevance Categories

Notices are scored against these categories (configurable in `src/flrules/relevance.py`):

- **domestic_terrorism** — organization designations, material support rules (weight: 3.0)
- **religious_freedom** — mosque, Islamic org, hijab, halal, faith-based policies (weight: 3.0)
- **surveillance** — fusion centers, biometrics, CVE programs, watchlists (weight: 2.5)
- **cabinet_meeting** — Governor/Cabinet agendas, executive orders (weight: 2.0)
- **immigration** — refugee, asylum, deportation, travel ban, ICE cooperation (weight: 2.0)
- **civil_rights** — hate crimes, profiling, discrimination, equal protection (weight: 2.0)
- **education** — curriculum bans, DEI restrictions, book bans (weight: 1.5)
- **nonprofit_regulation** — charity registration, tax-exempt status, foreign agent rules (weight: 1.5)
- **policy_general** — emergency rules, public comment periods (weight: 0.5)

A notice triggers an alert when its combined score >= 1.0.

## Quick Start

```bash
# Clone and set up
cd /path/to/FLRules
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env with your API keys

# Initialize database
flrules init-database

# Test scrape (no notifications, no DB)
flrules scrape-test

# Full pipeline dry run
flrules run --issues 3 --no-notify

# Full pipeline with notifications
flrules run --issues 3 --notify

# Add a subscriber
flrules subscribe --email someone@example.com --name "Ahmed"

# View alerts
flrules alerts

# Start web dashboard
uvicorn flrules.web:app --reload
```

## Deployment Options

### Option A: GitHub Actions (Recommended for small teams)

The included `.github/workflows/monitor.yml` runs the pipeline on a cron schedule. Set your API keys as GitHub repository secrets. The SQLite DB is uploaded as an artifact after each run.

**Pros**: Zero infrastructure, free for public repos, built-in scheduling.
**Cons**: No persistent DB between runs (use artifact download/upload), no dashboard.

### Option B: Railway / Render / Fly.io

```bash
# Railway (one-click deploy)
railway up

# Or Docker
docker build -t flrules .
docker run -p 8000:8000 --env-file .env -v ./data:/app/data flrules
```

**Pros**: Persistent storage, dashboard accessible, low cost ($5-7/mo).

### Option C: Local Cron (Simplest)

```bash
# Add to crontab -e
0 * * * * cd /path/to/FLRules && .venv/bin/flrules run --issues 3 --notify
```

## API Keys Setup

### Resend (Email)
1. Sign up at [resend.com](https://resend.com) — free tier: 3,000 emails/month
2. Add and verify your sending domain
3. Create an API key → `RESEND_API_KEY`

### Twilio (SMS)
1. Sign up at [twilio.com](https://www.twilio.com) — trial includes test credits
2. Get a phone number → `TWILIO_FROM_NUMBER`
3. Copy Account SID and Auth Token

## Legal and Ethical Considerations

### Web Scraping Compliance
- **robots.txt**: The FAR site has minimal restrictions. Our monitored paths (`/bigDoc/`, `/BigDoc/View_Section.asp`, `/Gateway/View_notice.asp`) are **not blocked**.
- **Crawl delay**: We use a 2-second delay between requests (configurable). The site specifies no crawl delay, but we add one to be respectful.
- **User-Agent**: We identify ourselves honestly as `FLRulesMonitor/0.1`.
- **Public records**: The Florida Administrative Register is a public government document required by Florida Statute 120.55. Monitoring it is a protected civic activity.

### Data Privacy
- Subscriber data (emails, phone numbers) is stored locally in SQLite — never sent to third parties except for the delivery services (Resend, Twilio).
- No tracking pixels or analytics in notification emails.
- Subscribers can unsubscribe at any time via the admin dashboard or by contacting the org.
- Consider adding a privacy policy page if deploying publicly.

### Rate Limiting
- Default: 1 poll/hour during business hours, 2 polls/day on weekends
- ~7 sections × ~15 notices avg = ~100 page loads per run at 2s intervals = ~3 min per run
- Well within reasonable use for a government transparency site

## Integrity Verification

Three features guard against the FAR being altered after we have already scraped a notice:

### Wayback Machine archiving (opt-in)
Each new notice URL is submitted to the Internet Archive's [Save Page Now](https://web.archive.org/save/) API, producing a third-party, timestamped copy on infrastructure we don't control. The snapshot URL is stored alongside the notice. If flrules.org ever serves different content than the archive, we have an independent witness.

Enable with `ARCHIVE_ENABLED=true` in your environment.

### Cryptographic chain of custody (always on)
Every notice we ingest is hashed (SHA-256 of its substantive content) and chained to the previous notice: `chain_hash = SHA256(prev_chain_hash || notice_id || content_hash)`. Tampering with any earlier notice invalidates every subsequent chain hash, so notice history cannot be quietly rewritten.

The current chain head is published to `site/chain.json` on every run. External observers (journalists, lawyers, auditors) can pin the head at a moment in time and later verify we did not retroactively edit history.

### Disappearance detection (dormant — opt-in when ready)
Re-scrapes the most recently stored issues and creates an alert if any previously-seen notice ID is no longer visible on the live site. Off by default because legitimate withdrawals will trigger it and require human triage.

Enable with `VERIFY_DISAPPEARANCES=true`. Tune via `VERIFY_RECENT_ISSUES` (default 1) and `VERIFY_INTERVAL_HOURS` (default 24).

## Development

```bash
# Run tests
pytest -v

# Lint
ruff check src/ tests/

# Type check
mypy src/

# Format
ruff format src/ tests/
```

## Tuning the Relevance Filter

Edit `src/flrules/relevance.py` to:
- Add/remove keyword patterns
- Adjust category weights
- Change the `THRESHOLD` (default: 1.0)
- Add `context_patterns` to reduce false positives

The `scrape-test` CLI command is useful for testing changes against live data without storing results or sending notifications.
