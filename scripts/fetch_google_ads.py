#!/usr/bin/env python3
"""
Fetch Google Ads campaign data for audit and optimization analysis.

Uses the Google Ads REST API v20 with service account auth.

Usage:
    python fetch_google_ads.py --check-auth
    python fetch_google_ads.py --campaigns
    python fetch_google_ads.py --campaigns --days 90 --output campaigns.json
    python fetch_google_ads.py --ad-groups --campaign-id 123456789
    python fetch_google_ads.py --keywords --campaign-id 123456789
    python fetch_google_ads.py --ads --campaign-id 123456789

Auth env vars required:
    GOOGLE_SERVICE_ACCOUNT_JSON_B64   base64-encoded service account JSON
    GOOGLE_ADS_DEVELOPER_TOKEN        Google Ads developer token
    GOOGLE_ADS_CUSTOMER_ID            Client account ID (with or without dashes)
    GOOGLE_ADS_MANAGER_CUSTOMER_ID    MCC/manager account ID (optional)
"""

import argparse
import base64
import json
import os
import sys
import time

try:
    import jwt
    import requests
except ImportError:
    print(json.dumps({"error": "Missing deps. Run: pip install requests PyJWT cryptography"}))
    sys.exit(1)

from datetime import date, timedelta

API_VERSION = "v20"
BASE_URL = f"https://googleads.googleapis.com/{API_VERSION}"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict:
    """Read and validate required env vars. Returns config dict."""
    b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "")
    dev_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    customer_id = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
    manager_id = os.environ.get("GOOGLE_ADS_MANAGER_CUSTOMER_ID", "").replace("-", "")

    missing = []
    if not b64:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if not dev_token:
        missing.append("GOOGLE_ADS_DEVELOPER_TOKEN")
    if not customer_id:
        missing.append("GOOGLE_ADS_CUSTOMER_ID")

    sa_info = None
    errors = []
    if b64:
        try:
            sa_info = json.loads(base64.b64decode(b64))
        except Exception as e:
            errors.append(f"GOOGLE_SERVICE_ACCOUNT_JSON_B64 decode failed: {e}")

    return {
        "dev_token": dev_token,
        "customer_id": customer_id,
        "manager_id": manager_id,
        "sa_info": sa_info,
        "missing": missing,
        "errors": errors,
    }


def _get_access_token(sa_info: dict) -> str:
    """Exchange service account credentials for an OAuth2 access token."""
    now = int(time.time())
    payload = {
        "iss": sa_info["client_email"],
        "sub": sa_info["client_email"],
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
        "scope": ADS_SCOPE,
    }
    assertion = jwt.encode(payload, sa_info["private_key"], algorithm="RS256")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers(access_token: str, dev_token: str, manager_id: str) -> dict:
    h = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": dev_token,
        "Content-Type": "application/json",
    }
    if manager_id:
        h["login-customer-id"] = manager_id
    return h


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).strftime("%Y-%m-%d")


def _search(customer_id: str, query: str, headers: dict) -> list:
    """Run a GAQL query and return all result rows as dicts."""
    url = f"{BASE_URL}/customers/{customer_id}/googleAds:search"
    rows = []
    page_token = None
    while True:
        body = {"query": query}
        if page_token:
            body["pageToken"] = page_token
        resp = requests.post(url, json=body, headers=headers, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"API error {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        rows.extend(data.get("results", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return rows


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def check_auth() -> dict:
    """Verify credentials and make a lightweight API call."""
    cfg = _load_env()
    out = {
        "ok": False,
        "developer_token": bool(cfg["dev_token"]),
        "customer_id": cfg["customer_id"] or None,
        "manager_customer_id": cfg["manager_id"] or None,
        "service_account_email": cfg["sa_info"].get("client_email") if cfg["sa_info"] else None,
        "missing_vars": cfg["missing"],
        "errors": list(cfg["errors"]),
        "api_reachable": False,
    }

    if cfg["missing"] or cfg["errors"]:
        return out

    try:
        token = _get_access_token(cfg["sa_info"])
        h = _headers(token, cfg["dev_token"], cfg["manager_id"])
        rows = _search(cfg["customer_id"], """
            SELECT customer.id, customer.descriptive_name,
                   customer.currency_code, customer.time_zone, customer.status
            FROM customer
            LIMIT 1
        """, h)
        if rows:
            c = rows[0]["customer"]
            out["account"] = {
                "id": c.get("id"),
                "name": c.get("descriptiveName"),
                "currency": c.get("currencyCode"),
                "timezone": c.get("timeZone"),
                "status": c.get("status"),
            }
        out["api_reachable"] = True
        out["ok"] = True
    except Exception as e:
        out["errors"].append(str(e))

    return out


def fetch_campaigns(days: int = 30) -> dict:
    """Fetch active campaigns with performance metrics."""
    cfg = _load_env()
    if cfg["missing"] or cfg["errors"]:
        return {"ok": False, "error": "auth not configured", "missing": cfg["missing"]}

    try:
        token = _get_access_token(cfg["sa_info"])
        h = _headers(token, cfg["dev_token"], cfg["manager_id"])
        start = _days_ago(days)
        end = date.today().strftime("%Y-%m-%d")
        rows = _search(cfg["customer_id"], f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                campaign.bidding_strategy_type,
                campaign.start_date,
                campaign.end_date,
                campaign_budget.amount_micros,
                campaign_budget.delivery_method,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.ctr,
                metrics.average_cpc,
                metrics.search_impression_share
            FROM campaign
            WHERE campaign.status != 'REMOVED'
              AND segments.date BETWEEN '{start}' AND '{end}'
            ORDER BY metrics.cost_micros DESC
        """, h)

        campaigns = []
        for row in rows:
            c = row.get("campaign", {})
            m = row.get("metrics", {})
            b = row.get("campaignBudget", {})
            budget_micros = int(b.get("amountMicros", 0))
            campaigns.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "status": c.get("status"),
                "channel_type": c.get("advertisingChannelType"),
                "bidding_strategy": c.get("biddingStrategyType"),
                "start_date": c.get("startDate"),
                "end_date": c.get("endDate") or None,
                "daily_budget_usd": round(budget_micros / 1_000_000, 2),
                "delivery_method": b.get("deliveryMethod"),
                "metrics": {
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost_usd": round(int(m.get("costMicros", 0)) / 1_000_000, 2),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                    "conversion_value": round(float(m.get("conversionsValue", 0)), 2),
                    "ctr_pct": round(float(m.get("ctr", 0)) * 100, 2),
                    "avg_cpc_usd": round(int(m.get("averageCpc", 0)) / 1_000_000, 2),
                    "search_impression_share_pct": (
                        round(float(m.get("searchImpressionShare", 0)) * 100, 1)
                        if m.get("searchImpressionShare") else None
                    ),
                },
            })

        return {"ok": True, "customer_id": cfg["customer_id"], "days": days, "campaigns": campaigns}

    except Exception as e:
        return {"ok": False, "errors": [str(e)]}


def fetch_ad_groups(campaign_id: str) -> dict:
    """Fetch ad groups for a campaign with performance metrics."""
    cfg = _load_env()
    if cfg["missing"] or cfg["errors"]:
        return {"ok": False, "error": "auth not configured", "missing": cfg["missing"]}

    try:
        token = _get_access_token(cfg["sa_info"])
        h = _headers(token, cfg["dev_token"], cfg["manager_id"])
        rows = _search(cfg["customer_id"], f"""
            SELECT
                ad_group.id,
                ad_group.name,
                ad_group.status,
                ad_group.type,
                ad_group.cpc_bid_micros,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM ad_group
            WHERE campaign.id = {campaign_id}
              AND ad_group.status != 'REMOVED'
              AND segments.date DURING LAST_30_DAYS
            ORDER BY metrics.cost_micros DESC
        """, h)

        ad_groups = []
        for row in rows:
            ag = row.get("adGroup", {})
            m = row.get("metrics", {})
            ad_groups.append({
                "id": ag.get("id"),
                "name": ag.get("name"),
                "status": ag.get("status"),
                "type": ag.get("type"),
                "cpc_bid_usd": round(int(ag.get("cpcBidMicros", 0)) / 1_000_000, 2),
                "metrics": {
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost_usd": round(int(m.get("costMicros", 0)) / 1_000_000, 2),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                    "ctr_pct": round(float(m.get("ctr", 0)) * 100, 2),
                    "avg_cpc_usd": round(int(m.get("averageCpc", 0)) / 1_000_000, 2),
                },
            })

        return {"ok": True, "campaign_id": campaign_id, "ad_groups": ad_groups}

    except Exception as e:
        return {"ok": False, "errors": [str(e)]}


def fetch_keywords(campaign_id: str = "") -> dict:
    """Fetch keywords with quality scores and performance metrics."""
    cfg = _load_env()
    if cfg["missing"] or cfg["errors"]:
        return {"ok": False, "error": "auth not configured", "missing": cfg["missing"]}

    try:
        token = _get_access_token(cfg["sa_info"])
        h = _headers(token, cfg["dev_token"], cfg["manager_id"])
        campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
        rows = _search(cfg["customer_id"], f"""
            SELECT
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.quality_info.quality_score,
                ad_group_criterion.quality_info.search_predicted_ctr,
                ad_group_criterion.quality_info.creative_quality_score,
                ad_group_criterion.quality_info.post_click_quality_score,
                ad_group.name,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc,
                metrics.search_impression_share
            FROM keyword_view
            WHERE ad_group_criterion.status != 'REMOVED'
              AND segments.date DURING LAST_30_DAYS
              {campaign_filter}
            ORDER BY metrics.cost_micros DESC
            LIMIT 500
        """, h)

        keywords = []
        for row in rows:
            kw = row.get("adGroupCriterion", {})
            kw_data = kw.get("keyword", {})
            qi = kw.get("qualityInfo", {})
            m = row.get("metrics", {})
            keywords.append({
                "text": kw_data.get("text"),
                "match_type": kw_data.get("matchType"),
                "status": kw.get("status"),
                "ad_group": row.get("adGroup", {}).get("name"),
                "campaign": row.get("campaign", {}).get("name"),
                "quality_score": qi.get("qualityScore"),
                "expected_ctr": qi.get("searchPredictedCtr"),
                "ad_relevance": qi.get("creativeQualityScore"),
                "landing_page_exp": qi.get("postClickQualityScore"),
                "metrics": {
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost_usd": round(int(m.get("costMicros", 0)) / 1_000_000, 2),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                    "ctr_pct": round(float(m.get("ctr", 0)) * 100, 2),
                    "avg_cpc_usd": round(int(m.get("averageCpc", 0)) / 1_000_000, 2),
                    "search_impression_share_pct": (
                        round(float(m.get("searchImpressionShare", 0)) * 100, 1)
                        if m.get("searchImpressionShare") else None
                    ),
                },
            })

        return {"ok": True, "campaign_id": campaign_id or "all", "keywords": keywords}

    except Exception as e:
        return {"ok": False, "errors": [str(e)]}


def fetch_ads(campaign_id: str = "") -> dict:
    """Fetch ads with performance metrics."""
    cfg = _load_env()
    if cfg["missing"] or cfg["errors"]:
        return {"ok": False, "error": "auth not configured", "missing": cfg["missing"]}

    try:
        token = _get_access_token(cfg["sa_info"])
        h = _headers(token, cfg["dev_token"], cfg["manager_id"])
        campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
        rows = _search(cfg["customer_id"], f"""
            SELECT
                ad_group_ad.ad.id,
                ad_group_ad.ad.type,
                ad_group_ad.ad.name,
                ad_group_ad.ad.final_urls,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.status,
                ad_group_ad.ad_strength,
                ad_group.name,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM ad_group_ad
            WHERE ad_group_ad.status != 'REMOVED'
              AND segments.date DURING LAST_30_DAYS
              {campaign_filter}
            ORDER BY metrics.cost_micros DESC
            LIMIT 200
        """, h)

        ads = []
        for row in rows:
            ad_data = row.get("adGroupAd", {})
            ad = ad_data.get("ad", {})
            m = row.get("metrics", {})
            rsa = ad.get("responsiveSearchAd", {})
            ads.append({
                "id": ad.get("id"),
                "type": ad.get("type"),
                "name": ad.get("name"),
                "status": ad_data.get("status"),
                "ad_strength": ad_data.get("adStrength"),
                "final_urls": ad.get("finalUrls", []),
                "ad_group": row.get("adGroup", {}).get("name"),
                "campaign": row.get("campaign", {}).get("name"),
                "headlines": [a.get("text") for a in rsa.get("headlines", [])],
                "descriptions": [a.get("text") for a in rsa.get("descriptions", [])],
                "metrics": {
                    "impressions": int(m.get("impressions", 0)),
                    "clicks": int(m.get("clicks", 0)),
                    "cost_usd": round(int(m.get("costMicros", 0)) / 1_000_000, 2),
                    "conversions": round(float(m.get("conversions", 0)), 2),
                    "ctr_pct": round(float(m.get("ctr", 0)) * 100, 2),
                    "avg_cpc_usd": round(int(m.get("averageCpc", 0)) / 1_000_000, 2),
                },
            })

        return {"ok": True, "campaign_id": campaign_id or "all", "ads": ads}

    except Exception as e:
        return {"ok": False, "errors": [str(e)]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Google Ads data for audit analysis")
    parser.add_argument("--check-auth", action="store_true", help="Verify credentials and API access")
    parser.add_argument("--campaigns", action="store_true", help="Fetch all campaigns with metrics")
    parser.add_argument("--ad-groups", action="store_true", help="Fetch ad groups for a campaign")
    parser.add_argument("--keywords", action="store_true", help="Fetch keywords with quality scores")
    parser.add_argument("--ads", action="store_true", help="Fetch ads with creative details")
    parser.add_argument("--campaign-id", help="Filter by campaign ID")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--output", "-o", help="Write JSON output to file")

    args = parser.parse_args()

    if not any([args.check_auth, args.campaigns, args.ad_groups, args.keywords, args.ads]):
        parser.print_help()
        sys.exit(0)

    if args.check_auth:
        result = check_auth()
    elif args.campaigns:
        result = fetch_campaigns(days=args.days)
    elif args.ad_groups:
        if not args.campaign_id:
            print(json.dumps({"error": "--campaign-id required with --ad-groups"}))
            sys.exit(1)
        result = fetch_ad_groups(args.campaign_id)
    elif args.keywords:
        result = fetch_keywords(args.campaign_id or "")
    elif args.ads:
        result = fetch_ads(args.campaign_id or "")

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(output)

    if not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()
