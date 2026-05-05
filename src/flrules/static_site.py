"""
Static site generator — builds an index.html dashboard for GitHub Pages.

After each pipeline run, call generate_static_site() to produce a self-contained
HTML file in the `site/` directory. GitHub Actions deploys this to Pages.
"""

import json
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import func, select

from flrules.db import async_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber

log = structlog.get_logger()

SITE_DIR = Path(__file__).resolve().parent.parent.parent / "site"

# Category metadata: (color, label, svg_icon)
# Icons are minimal 16x16 SVG path data
CATEGORY_META = {
    "domestic_terrorism": ("#ef4444", "Domestic Terrorism", "M12 9a1 1 0 01-1 1H9v2a1 1 0 01-2 0v-2H5a1 1 0 010-2h2V6a1 1 0 012 0v2h2a1 1 0 011 1z"),
    "cabinet_meeting": ("#f59e0b", "Cabinet Meeting", "M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"),
    "religious_freedom": ("#8b5cf6", "Religious Freedom", "M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"),
    "immigration": ("#06b6d4", "Immigration", "M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z"),
    "surveillance": ("#ec4899", "Surveillance", "M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"),
    "civil_rights": ("#10b981", "Civil Rights", "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"),
    "education": ("#6366f1", "Education", "M12 14l9-5-9-5-9 5 9 5z M12 14l6.16-3.422a12.083 12.083 0 01.665 6.479A11.952 11.952 0 0012 20.055a11.952 11.952 0 00-6.824-2.998 12.078 12.078 0 01.665-6.479L12 14z"),
    "nonprofit_regulation": ("#64748b", "Nonprofit Reg.", "M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"),
    "policy_general": ("#94a3b8", "Policy General", "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"),
}

# ── Inline SVG icons for stat cards ──────────────────────
ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
ICON_DOC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>'
ICON_FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>'
ICON_USERS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>'

# ── App logo: document + shield ──────────────────────────
LOGO_SVG = """<svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="4" y="2" width="20" height="28" rx="3" fill="#1e40af" opacity="0.15"/>
<rect x="6" y="4" width="20" height="28" rx="3" fill="#2563eb" opacity="0.3"/>
<rect x="8" y="6" width="20" height="28" rx="3" fill="white"/>
<line x1="12" y1="13" x2="24" y2="13" stroke="#cbd5e1" stroke-width="1.5" stroke-linecap="round"/>
<line x1="12" y1="17" x2="22" y2="17" stroke="#cbd5e1" stroke-width="1.5" stroke-linecap="round"/>
<line x1="12" y1="21" x2="20" y2="21" stroke="#cbd5e1" stroke-width="1.5" stroke-linecap="round"/>
<line x1="12" y1="25" x2="18" y2="25" stroke="#cbd5e1" stroke-width="1.5" stroke-linecap="round"/>
<circle cx="27" cy="27" r="9" fill="#2563eb"/>
<path d="M27 21.5c-3 0-5.5 1.5-5.5 1.5s0 5 1.5 7c1.5 2 4 3 4 3s2.5-1 4-3c1.5-2 1.5-7 1.5-7s-2.5-1.5-5.5-1.5z" fill="white" opacity="0.95"/>
<path d="M25 27l1.5 1.5 3-3" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

# ── Florida state outline (simplified, decorative) ──────
FLORIDA_SVG = """<svg class="fl-outline" viewBox="0 0 200 300" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M50 10 L140 10 L145 15 L150 10 L170 12 L175 18 L180 15 L185 20 L180 30 L175 35 L178 45 L175 55 L170 60 L168 70 L165 80 L162 90 L158 100 L155 110 L150 120 L148 130 L145 140 L140 155 L135 170 L130 180 L125 190 L118 200 L112 210 L108 220 L105 230 L102 235 L98 240 L94 250 L90 258 L85 265 L78 272 L70 278 L62 282 L55 284 L50 280 L48 272 L50 265 L55 258 L52 250 L48 242 L42 235 L38 228 L35 220 L33 210 L30 195 L28 180 L25 165 L22 150 L20 135 L18 120 L17 105 L18 90 L20 75 L25 60 L30 50 L35 40 L40 30 L45 20 Z" fill="currentColor" opacity="0.04"/>
</svg>"""

# ── Favicon as data URI ──────────────────────────────────
FAVICON_SVG = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='2' y='4' width='18' height='24' rx='3' fill='%232563eb'/%3E%3Ccircle cx='23' cy='23' r='9' fill='%23ef4444'/%3E%3Cpath d='M23 18v6M23 26v1' stroke='white' stroke-width='2' stroke-linecap='round'/%3E%3C/svg%3E"


async def generate_static_site():
    """Query the DB and write a static HTML dashboard to site/index.html.
    Also publishes site/chain.json — the cryptographic chain head — so any
    outside observer can pin the head at a moment in time and later verify
    we did not retroactively edit notice history."""
    await init_db()
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_session() as session:
        alert_count = await session.scalar(select(func.count(Alert.id))) or 0
        notice_count = await session.scalar(select(func.count(FARNotice.id))) or 0
        issue_count = await session.scalar(select(func.count(FARIssue.id))) or 0
        sub_count = await session.scalar(select(func.count(Subscriber.id))) or 0

        result = await session.execute(
            select(Alert).order_by(Alert.created_at.desc()).limit(50)
        )
        alerts = result.scalars().all()

        result = await session.execute(
            select(FARNotice).order_by(FARNotice.fetched_at.desc()).limit(30)
        )
        recent_notices = result.scalars().all()

        result = await session.execute(
            select(FARIssue).order_by(FARIssue.fetched_at.desc()).limit(1)
        )
        latest_issue = result.scalar_one_or_none()

        # Publish the current chain head so external observers can audit. We
        # write this even when the chain is empty so the file exists with a
        # known shape from the very first deploy.
        head_result = await session.execute(
            select(FARNotice.notice_id, FARNotice.chain_hash, FARNotice.fetched_at)
            .where(FARNotice.chain_hash != "")
            .order_by(FARNotice.id.desc())
            .limit(1)
        )
        head_row = head_result.first()
        chained_count = await session.scalar(
            select(func.count(FARNotice.id)).where(FARNotice.chain_hash != "")
        ) or 0
        chain_head_payload = {
            "chain_head": head_row[1] if head_row else "",
            "head_notice_id": head_row[0] if head_row else None,
            "head_fetched_at": (
                head_row[2].isoformat() + "Z" if head_row and head_row[2] else None
            ),
            "chained_notice_count": chained_count,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    chain_path = SITE_DIR / "chain.json"
    chain_path.write_text(json.dumps(chain_head_payload, indent=2))
    log.info("chain_head_published", path=str(chain_path), **chain_head_payload)

    now = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    last_issue_date = latest_issue.publish_date if latest_issue else "No scans yet"

    # Build alert cards
    alert_cards = ""
    for a in alerts:
        ts = a.created_at.strftime("%b %d, %Y") if isinstance(a.created_at, datetime) else str(a.created_at)
        cats = " ".join(_badge_html(c.strip()) for c in a.category.split(","))
        score_class = "score-high" if a.relevance_score >= 5 else "score-med" if a.relevance_score >= 2 else "score-low"
        notified_icon = '<span class="status-sent">Sent</span>' if a.notified else '<span class="status-pending">Pending</span>'
        alert_cards += f"""<div class="alert-card">
<div class="alert-header">
<div class="alert-cats">{cats}</div>
<div class="alert-meta">
<span class="{score_class}">Score: {a.relevance_score:.1f}</span>
{notified_icon}
</div>
</div>
<p class="alert-summary">{_esc(a.summary[:300])}</p>
<div class="alert-footer">
<span class="alert-date">{ts}</span>
<span class="alert-notice">Notice #{a.notice_id}</span>
</div>
</div>
"""

    # Build notice rows
    notice_rows = ""
    for n in recent_notices:
        section_badge = f'<span class="section-badge">{_esc(n.section_name[:30])}</span>'
        notice_rows += f"""<tr>
<td class="date-cell">{_esc(n.publish_date)}</td>
<td>{section_badge}</td>
<td class="agency-cell">{_esc(n.agency_code)}</td>
<td><a href="{_esc(n.url)}" target="_blank" rel="noopener">{_esc(n.description[:140])}</a></td>
</tr>
"""

    # Category legend with SVG icons
    legend_items = ""
    for cat_key, (color, label, icon_path) in CATEGORY_META.items():
        icon = f'<svg class="legend-icon" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="{icon_path}"/></svg>'
        legend_items += f'<span class="legend-item">{icon}{label}</span>'

    # Dump alerts JSON
    alerts_json = json.dumps(
        [
            {
                "notice_id": a.notice_id,
                "category": a.category,
                "score": a.relevance_score,
                "summary": a.summary,
                "created_at": str(a.created_at),
            }
            for a in alerts
        ],
        indent=2,
    )
    (SITE_DIR / "alerts.json").write_text(alerts_json)

    empty_alerts_html = """<div class="empty-state">
<svg class="empty-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
<h3>All clear — no alerts triggered</h3>
<p>The system is actively monitoring the Florida Administrative Register.<br>
Alerts will appear here when relevant rule changes are detected.</p>
</div>"""

    empty_notices_html = """<div class="empty-state">
<svg class="empty-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
<h3>No notices scanned yet</h3>
<p>Notices from the Florida Administrative Register will appear here after the first pipeline run.</p>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLRules Monitor</title>
<link rel="icon" href="{FAVICON_SVG}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-hover: #f1f5f9;
  --border: #e2e8f0;
  --border-light: #f1f5f9;
  --text: #0f172a;
  --text-secondary: #475569;
  --text-muted: #94a3b8;
  --accent: #2563eb;
  --accent-light: #dbeafe;
  --radius: 12px;
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,.07), 0 2px 4px -2px rgba(0,0,0,.05);
  --shadow-lg: 0 10px 15px -3px rgba(0,0,0,.07), 0 4px 6px -4px rgba(0,0,0,.05);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}}

/* ── Header ──────────────────────────────── */
.header {{
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 50%, #172554 100%);
  color: white;
  padding: 2.5rem 2rem 3rem;
  position: relative;
  overflow: hidden;
}}
.header::after {{
  content: '';
  position: absolute;
  top: 0; right: 0; bottom: 0; left: 0;
  background: radial-gradient(ellipse at 80% 50%, rgba(37,99,235,0.15) 0%, transparent 60%);
  pointer-events: none;
}}
.fl-outline {{
  position: absolute;
  right: -20px;
  top: -30px;
  width: 280px;
  height: 380px;
  color: white;
  opacity: 1;
  pointer-events: none;
}}
.header-inner {{
  max-width: 1200px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}
.header-brand {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.25rem;
}}
.header-brand h1 {{
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}}
.header .tagline {{
  color: #94a3b8;
  font-size: 0.875rem;
  font-weight: 400;
  margin-left: 0.15rem;
}}
.header-meta {{
  display: flex;
  gap: 1.5rem;
  margin-top: 1rem;
  flex-wrap: wrap;
}}
.header-meta span {{
  font-size: 0.75rem;
  color: #64748b;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}}
.dot {{
  width: 7px; height: 7px;
  border-radius: 50%;
  display: inline-block;
  animation: pulse 2s infinite;
}}
.dot-active {{ background: #22c55e; }}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.5; }}
}}

/* ── Main Content ────────────────────────── */
.container {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 1.5rem 2rem 3rem;
}}

/* ── Stat Cards ──────────────────────────── */
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin: -2.25rem 0 2rem;
  position: relative;
  z-index: 1;
}}
.stat {{
  background: var(--surface);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border-light);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  display: flex;
  align-items: center;
  gap: 1rem;
}}
.stat:hover {{
  transform: translateY(-3px);
  box-shadow: 0 12px 24px -6px rgba(0,0,0,.1);
}}
.stat-icon {{
  width: 44px; height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}}
.stat-icon svg {{ width: 22px; height: 22px; }}
.stat:nth-child(1) .stat-icon {{ background: #fef2f2; color: #ef4444; }}
.stat:nth-child(2) .stat-icon {{ background: #eff6ff; color: #2563eb; }}
.stat:nth-child(3) .stat-icon {{ background: #f5f3ff; color: #8b5cf6; }}
.stat:nth-child(4) .stat-icon {{ background: #ecfdf5; color: #10b981; }}
.stat-text .num {{
  font-size: 1.75rem;
  font-weight: 700;
  line-height: 1.1;
}}
.stat:nth-child(1) .num {{ color: #ef4444; }}
.stat:nth-child(2) .num {{ color: #2563eb; }}
.stat:nth-child(3) .num {{ color: #8b5cf6; }}
.stat:nth-child(4) .num {{ color: #10b981; }}
.stat-text .lbl {{
  font-size: 0.75rem;
  color: var(--text-muted);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-top: 0.1rem;
}}

/* ── Section Headers ─────────────────────── */
.section-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 2.5rem 0 1rem;
}}
.section-header h2 {{
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}}
.section-header h2 svg {{ width: 20px; height: 20px; color: var(--text-muted); }}
.section-header .count {{
  font-size: 0.75rem;
  color: var(--text-muted);
  background: var(--border-light);
  padding: 0.2rem 0.65rem;
  border-radius: 999px;
  font-weight: 500;
}}

/* ── Category Legend ──────────────────────── */
.legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem 1rem;
  margin-bottom: 1.25rem;
  padding: 0.75rem 1rem;
  background: var(--surface);
  border-radius: var(--radius);
  border: 1px solid var(--border-light);
}}
.legend-item {{
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-weight: 500;
}}
.legend-icon {{
  width: 14px; height: 14px;
  flex-shrink: 0;
}}

/* ── Alert Cards ─────────────────────────── */
.alerts-grid {{
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}}
.alert-card {{
  background: var(--surface);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  box-shadow: var(--shadow);
  border: 1px solid var(--border-light);
  border-left: 3px solid var(--accent);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}}
.alert-card:hover {{
  box-shadow: var(--shadow-md);
  border-left-color: #1d4ed8;
}}
.alert-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.6rem;
  flex-wrap: wrap;
  gap: 0.5rem;
}}
.alert-cats {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}}
.alert-meta {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
}}
.alert-summary {{
  font-size: 0.875rem;
  color: var(--text-secondary);
  line-height: 1.55;
  margin-bottom: 0.75rem;
}}
.alert-footer {{
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: var(--text-muted);
  padding-top: 0.6rem;
  border-top: 1px solid var(--border-light);
}}

/* ── Badges ──────────────────────────────── */
.badge {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.2rem 0.65rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.01em;
}}
.badge svg {{ width: 11px; height: 11px; stroke: white; fill: none; stroke-width: 2; }}
.score-high {{ color: #ef4444; font-weight: 700; font-size: 0.8rem; }}
.score-med {{ color: #f59e0b; font-weight: 600; font-size: 0.8rem; }}
.score-low {{ color: #64748b; font-weight: 600; font-size: 0.8rem; }}
.status-sent {{
  font-size: 0.7rem; color: #10b981; font-weight: 600;
  background: #ecfdf5; padding: 0.15rem 0.55rem; border-radius: 999px;
  display: inline-flex; align-items: center; gap: 0.25rem;
}}
.status-sent::before {{ content: ''; width: 5px; height: 5px; border-radius: 50%; background: #10b981; }}
.status-pending {{
  font-size: 0.7rem; color: #f59e0b; font-weight: 600;
  background: #fffbeb; padding: 0.15rem 0.55rem; border-radius: 999px;
  display: inline-flex; align-items: center; gap: 0.25rem;
}}
.status-pending::before {{ content: ''; width: 5px; height: 5px; border-radius: 50%; background: #f59e0b; }}

/* ── Table ────────────────────────────────── */
.table-wrap {{
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  border: 1px solid var(--border-light);
  overflow: hidden;
}}
table {{ width: 100%; border-collapse: collapse; }}
th {{
  background: var(--border-light);
  padding: 0.7rem 1rem; text-align: left;
  font-size: 0.7rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--text-secondary);
}}
td {{
  padding: 0.7rem 1rem; font-size: 0.85rem;
  border-top: 1px solid var(--border-light); vertical-align: top;
}}
tr:hover td {{ background: var(--surface-hover); }}
.date-cell {{ white-space: nowrap; color: var(--text-muted); font-size: 0.8rem; }}
.agency-cell {{ font-family: 'SF Mono','Fira Code',monospace; font-size: 0.8rem; color: var(--text-secondary); }}
.section-badge {{
  font-size: 0.7rem; color: var(--accent); background: var(--accent-light);
  padding: 0.15rem 0.5rem; border-radius: 999px; font-weight: 500; white-space: nowrap;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* ── Empty States ────────────────────────── */
.empty-state {{
  text-align: center; padding: 3.5rem 2rem;
  background: var(--surface); border-radius: var(--radius);
  border: 1px dashed var(--border);
}}
.empty-svg {{
  width: 48px; height: 48px;
  color: var(--text-muted); opacity: 0.4;
  margin-bottom: 1rem;
}}
.empty-state h3 {{
  font-size: 1rem; font-weight: 600;
  color: var(--text-secondary); margin-bottom: 0.35rem;
}}
.empty-state p {{
  font-size: 0.85rem; color: var(--text-muted); line-height: 1.6;
}}

/* ── Footer ──────────────────────────────── */
footer {{
  margin-top: 3rem; padding: 1.5rem 0;
  border-top: 1px solid var(--border);
  font-size: 0.75rem; color: var(--text-muted);
  display: flex; justify-content: space-between;
  flex-wrap: wrap; gap: 0.5rem;
}}
footer a {{ color: var(--text-secondary); }}

/* ── Responsive ──────────────────────────── */
@media (max-width: 768px) {{
  .header {{ padding: 1.5rem 1rem 2rem; }}
  .fl-outline {{ width: 150px; height: 200px; right: -10px; }}
  .container {{ padding: 1rem; }}
  .stats {{ grid-template-columns: repeat(2, 1fr); margin-top: -1.5rem; }}
  .stat {{ padding: 1rem; gap: 0.75rem; }}
  .stat-icon {{ width: 36px; height: 36px; }}
  .stat-icon svg {{ width: 18px; height: 18px; }}
  .stat-text .num {{ font-size: 1.4rem; }}
  .alert-header {{ flex-direction: column; align-items: flex-start; }}
  .section-header {{ flex-direction: column; align-items: flex-start; gap: 0.5rem; }}
  .legend {{ gap: 0.4rem 0.75rem; }}
  footer {{ flex-direction: column; }}
  .table-wrap {{ overflow-x: auto; }}
}}
@media (max-width: 480px) {{
  .stats {{ grid-template-columns: 1fr; }}
  .header-brand h1 {{ font-size: 1.25rem; }}
}}
</style>
</head>
<body>

<div class="header">
{FLORIDA_SVG}
<div class="header-inner">
  <div class="header-brand">
    {LOGO_SVG}
    <h1>FLRules Monitor</h1>
  </div>
  <p class="tagline">Florida Administrative Register &mdash; Civil Rights Alert System</p>
  <div class="header-meta">
    <span><span class="dot dot-active"></span> Monitoring active</span>
    <span>Last scan: {last_issue_date}</span>
    <span>Updated: {now}</span>
  </div>
</div>
</div>

<div class="container">

<div class="stats">
  <div class="stat">
    <div class="stat-icon">{ICON_ALERT}</div>
    <div class="stat-text"><div class="num">{alert_count}</div><div class="lbl">Alerts Flagged</div></div>
  </div>
  <div class="stat">
    <div class="stat-icon">{ICON_DOC}</div>
    <div class="stat-text"><div class="num">{notice_count}</div><div class="lbl">Notices Scanned</div></div>
  </div>
  <div class="stat">
    <div class="stat-icon">{ICON_FOLDER}</div>
    <div class="stat-text"><div class="num">{issue_count}</div><div class="lbl">Issues Processed</div></div>
  </div>
  <div class="stat">
    <div class="stat-icon">{ICON_USERS}</div>
    <div class="stat-text"><div class="num">{sub_count}</div><div class="lbl">Subscribers</div></div>
  </div>
</div>

<div class="section-header">
  <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>Flagged Alerts</h2>
  <span class="count">{alert_count} total</span>
</div>
<div class="legend">{legend_items}</div>

<div class="alerts-grid">
{alert_cards if alert_cards else empty_alerts_html}
</div>

<div class="section-header">
  <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>Recent Notices Scanned</h2>
  <span class="count">{len(recent_notices)} shown</span>
</div>

{f'<div class="table-wrap"><table><thead><tr><th>Published</th><th>Section</th><th>Agency</th><th>Description</th></tr></thead><tbody>{notice_rows}</tbody></table></div>' if notice_rows else empty_notices_html}

<footer>
  <span>Data from the <a href="https://flrules.org/bigdoc/default.asp">Florida Administrative Register</a>. Alerts generated via keyword relevance scoring.</span>
  <span><a href="alerts.json">alerts.json</a></span>
</footer>

</div>
</body>
</html>"""

    (SITE_DIR / "index.html").write_text(html)
    log.info("static_site_generated", path=str(SITE_DIR / "index.html"))


def _badge_html(category: str) -> str:
    """Generate a colored badge with mini icon for a category."""
    color, label, icon_path = CATEGORY_META.get(
        category, ("#94a3b8", category.replace("_", " ").title(), "")
    )
    icon = ""
    if icon_path:
        icon = f'<svg viewBox="0 0 24 24"><path d="{icon_path}"/></svg>'
    return f'<span class="badge" style="background:{color}">{icon}{label}</span>'


def _esc(text: str) -> str:
    """Minimal HTML escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
