---
name: ads-optimize
description: "Google Ads impression share optimization. Fetches live IS, QS, and budget data via API v20, runs 44-check analysis via optimize-google agent, and delivers a prioritized action plan ranked by expected IS lift. Use when user wants to increase impressions, improve impression share, grow exposure, fix lost impression share, or run a weekly optimization pass."
user-invokable: false
tested_date: 2026-05-22
tested_with: claude-code v2.x
---

# Google Ads IS Optimization

Runs a focused optimization pass targeting impression share growth. Fetches live
data via Google Ads API v20 when credentials are present; falls back to pasted
data otherwise. Spawns the `optimize-google` agent for analysis. Outputs
`is-optimization-YYYY-MM-DD.md` with actions ranked by expected IS lift.

## Scheduling

Run weekly via the `/loop` skill:

```
/loop 7d /ads optimize
```

Each run saves a dated report so you can track week-over-week IS improvement.

## Process

### Step 1 — Context Intake

If not already known, collect:
1. **Monthly Google Ads spend** (approximate)
2. **Business type** (SaaS, e-commerce, local service, etc.)
3. **Primary campaign types** (Search, PMax, Shopping, Display)

Skip this step if context was already provided earlier in the session.

### Step 2 — Data Collection

**API mode** (when `GOOGLE_ADS_DEVELOPER_TOKEN` and `GOOGLE_ADS_CUSTOMER_ID` are set):

```bash
python ~/.claude/skills/ads/scripts/fetch_google_ads.py --query all --days 30 --output /tmp/gads-is.json
```

Parse the resulting JSON and pass it to the `optimize-google` agent.

**Manual mode** (no credentials):

Ask the user to provide any of the following (more = better recommendations):

1. Campaign IS report (Search IS, Budget Lost IS, Rank Lost IS per campaign) — **required**
2. Keyword quality scores (QS, Expected CTR, Ad Relevance, Landing Page Experience)
3. Budget utilization (actual daily spend vs daily budget per campaign)
4. Auction Insights export
5. Bid strategy settings per campaign

### Step 3 — IS Optimization Analysis

Spawn `optimize-google` agent via Task tool (`context: fork`) passing:
- Raw data (JSON from API or pasted user data)
- Business type and monthly spend
- Date range
- Output filename: `is-optimization-{today}.md`

### Step 4 — Summary

After the agent completes, read `is-optimization-{today}.md` and present:
- IS Snapshot (current IS + budget/rank split)
- Top 3 actions with expected IS lift

## API Credentials Setup

Set these environment variables. Add to `~/.claude/.env` or your project `.env`:

```
GOOGLE_ADS_DEVELOPER_TOKEN=your_developer_token
GOOGLE_ADS_CUSTOMER_ID=1234567890
GOOGLE_ADS_MANAGER_CUSTOMER_ID=9876543210   # MCC only — omit if not using MCC

# Full service account JSON (recommended):
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","client_email":"...","private_key":"..."}

# OR split form:
GOOGLE_SERVICE_ACCOUNT_EMAIL=ads-reader@project.iam.gserviceaccount.com
GOOGLE_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...
```

The service account must be granted **Read-only** access in the Google Ads account:
Google Ads UI → Admin → Access & Security → Users → grant access to the service account email.

## Output

- `is-optimization-YYYY-MM-DD.md` — full ranked action plan (primary deliverable)
- Console: IS snapshot table + top 3 immediate actions
