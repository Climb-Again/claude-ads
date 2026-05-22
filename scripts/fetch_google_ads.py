#!/usr/bin/env python3
"""
Fetch Google Ads impression share, quality score, and budget metrics for IS optimization.

Usage:
    python fetch_google_ads.py
    python fetch_google_ads.py --query campaign_is --days 14
    python fetch_google_ads.py --query all --output gads-data.json

Queries:
    campaign_is      Campaign-level IS + budget/rank lost IS
    keyword_qs       Keyword Quality Scores + per-keyword IS
    budget_util      Budget utilization + recommended budgets
    auction_insights Competitor auction overlap + outranking share
    bid_strategies   Bidding strategy config + conversion volume
    search_terms     Search term harvest + IS by search term
    all              Run all queries (default)

Required env vars:
    GOOGLE_ADS_DEVELOPER_TOKEN
    GOOGLE_ADS_CUSTOMER_ID          (no hyphens, e.g. 1234567890)
    GOOGLE_ADS_MANAGER_CUSTOMER_ID  (optional, for MCC)
    GOOGLE_SERVICE_ACCOUNT_JSON     (full service account JSON string, OR use the two below)
    GOOGLE_SERVICE_ACCOUNT_EMAIL    (service account email)
    GOOGLE_PRIVATE_KEY              (PEM key — use \\n for newlines in env files)
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

try:
    import requests
except ImportError:
    print("Error: requests required. pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    from google.oauth2 import service_account
    import google.auth.transport.requests as google_requests
except ImportError:
    print("Error: google-auth required. pip install google-auth", file=sys.stderr)
    sys.exit(1)

from url_utils import sanitize_error

API_VERSION = "v20"
ADS_API_BASE = "https://googleads.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/adwords"]


def _load_credentials():
    """Build service_account.Credentials from env vars."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            info = json.loads(sa_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    email = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL")
    private_key = os.environ.get("GOOGLE_PRIVATE_KEY")
    if not email or not private_key:
        raise ValueError(
            "Missing credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON, or both "
            "GOOGLE_SERVICE_ACCOUNT_EMAIL and GOOGLE_PRIVATE_KEY."
        )
    info = {
        "type": "service_account",
        "client_email": email,
        "private_key": private_key.replace("\\n", "\n"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _get_access_token(credentials) -> str:
    credentials.refresh(google_requests.Request())
    return credentials.token


def _build_date_range(days: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _safe_float(val):
    """Google returns '--' (string) when a metric is below reportable threshold."""
    if val is None or val == "--":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _run_query(access_token: str, customer_id: str, manager_id: str | None, query: str) -> list:
    url = f"{ADS_API_BASE}/{API_VERSION}/customers/{customer_id}/googleAds:search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "Content-Type": "application/json",
    }
    if manager_id:
        headers["login-customer-id"] = manager_id

    try:
        resp = requests.post(url, headers=headers, json={"query": query}, timeout=60)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except requests.exceptions.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.response.json()
        except Exception:
            pass
        raise RuntimeError(
            f"Google Ads API error {exc.response.status_code}: {error_body}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Request failed: {sanitize_error(exc)}") from exc


QUERIES = {
    "campaign_is": lambda s, e: f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.search_impression_share,
          metrics.search_budget_lost_impression_share,
          metrics.search_rank_lost_impression_share,
          metrics.search_absolute_top_impression_share,
          metrics.search_top_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """,

    "keyword_qs": lambda s, e: f"""
        SELECT
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.quality_info.quality_score,
          ad_group_criterion.quality_info.search_predicted_ctr,
          ad_group_criterion.quality_info.ad_relevance,
          ad_group_criterion.quality_info.landing_page_experience,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.search_impression_share,
          metrics.search_rank_lost_impression_share,
          metrics.search_budget_lost_impression_share
        FROM keyword_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND ad_group_criterion.status = 'ENABLED'
        ORDER BY metrics.impressions DESC
        LIMIT 500
    """,

    "budget_util": lambda s, e: f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign_budget.id,
          campaign_budget.name,
          campaign_budget.amount_micros,
          campaign_budget.has_recommended_budget,
          campaign_budget.recommended_budget_amount_micros,
          campaign_budget.type,
          metrics.cost_micros,
          metrics.search_budget_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """,

    "auction_insights": lambda s, e: f"""
        SELECT
          campaign.name,
          auction_insight.domain,
          metrics.auction_insight_search_overlap_rate,
          metrics.auction_insight_search_outranking_share,
          metrics.auction_insight_search_position_above_rate,
          metrics.auction_insight_search_top_impression_percentage,
          metrics.auction_insight_search_absolute_top_impression_percentage
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.auction_insight_search_overlap_rate DESC
    """,

    "bid_strategies": lambda s, e: f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.bidding_strategy_type,
          campaign.target_cpa.target_cpa_micros,
          campaign.target_roas.target_roas,
          campaign.maximize_conversions.target_cpa_micros,
          campaign.maximize_conversion_value.target_roas,
          campaign.manual_cpc.enhanced_cpc_enabled,
          metrics.conversions,
          metrics.conversion_value,
          metrics.cost_micros,
          metrics.average_cpc
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """,

    "search_terms": lambda s, e: f"""
        SELECT
          campaign.name,
          ad_group.name,
          search_term_view.search_term,
          search_term_view.status,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.search_impression_share
        FROM search_term_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
          AND campaign.status = 'ENABLED'
          AND search_term_view.status != 'EXCLUDED'
        ORDER BY metrics.impressions DESC
        LIMIT 500
    """,
}


def fetch_data(query_name: str, days: int) -> dict:
    creds = _load_credentials()
    access_token = _get_access_token(creds)
    customer_id = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
    manager_id = os.environ.get("GOOGLE_ADS_MANAGER_CUSTOMER_ID", "").replace("-", "") or None

    if not customer_id:
        raise ValueError("GOOGLE_ADS_CUSTOMER_ID env var is required.")

    start, end = _build_date_range(days)
    output: dict = {"date_range": {"start": start, "end": end, "days": days}, "queries": {}}

    names = list(QUERIES.keys()) if query_name == "all" else [query_name]
    for name in names:
        rows = _run_query(access_token, customer_id, manager_id, QUERIES[name](start, end))
        output["queries"][name] = rows

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Google Ads IS and quality metrics for optimization."
    )
    parser.add_argument(
        "--query",
        choices=[*QUERIES.keys(), "all"],
        default="all",
        help="Which data to fetch (default: all)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback window in days (default: 30)",
    )
    parser.add_argument("--output", "-o", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        print("Error: GOOGLE_ADS_DEVELOPER_TOKEN env var is required.", file=sys.stderr)
        sys.exit(1)

    try:
        result = fetch_data(args.query, args.days)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {sanitize_error(exc)}", file=sys.stderr)
        sys.exit(1)

    json_output = json.dumps(result, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(json_output)


if __name__ == "__main__":
    main()
