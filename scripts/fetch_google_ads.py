#!/usr/bin/env python3
"""
Fetch Google Ads account data for the 80-check audit.

Hits the Google Ads REST API v20 directly (no google-ads Python library).
Auth uses a service account — the same pattern as the internal Express API.

Usage:
    python fetch_google_ads.py
    python fetch_google_ads.py --customer-id 191-261-1776
    python fetch_google_ads.py --customer-id 191-261-1776 --output data.json
    python fetch_google_ads.py --check-auth

Required environment variables:
    GOOGLE_ADS_DEVELOPER_TOKEN    developer token
    GOOGLE_SERVICE_ACCOUNT_EMAIL  service account email
    GOOGLE_PRIVATE_KEY            PEM string  OR  full service-account JSON string
    GOOGLE_ADS_CUSTOMER_ID        target account (no hyphens, or 191-261-1776 style)

Optional:
    GOOGLE_ADS_MANAGER_CUSTOMER_ID  MCC/manager account ID (digits only)

Output JSON keys:
    meta                  fetch timestamp, customer_id, date_range, error count
    campaigns             campaign metrics + impression share
    ad_groups             ad group structure and metrics
    keywords              deduplicated keywords with QS and metrics
    search_terms          top 1000 search terms by cost (last 30 days)
    ads                   RSAs with headlines, descriptions, ad_strength
    conversion_actions    all non-removed conversion actions
    shared_negative_lists account-level shared negative keyword lists
    campaign_neg_list_assignments  which lists are assigned to which campaigns
    campaign_negative_keywords     campaign-level negative keyword criteria
    asset_groups          PMax asset groups with per-type asset counts
    extensions            campaign extension settings (sitelinks, callouts, etc.)
    audiences             campaign audience segments
    customer_match_lists  CRM-based user lists
    data_errors           per-query failures with reasons
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests

from url_utils import sanitize_error

log = logging.getLogger(__name__)

_ADS_API_BASE = "https://googleads.googleapis.com/v20"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"


# ── auth ──────────────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    """Obtain a short-lived OAuth2 access token via service account credentials.

    Reads GOOGLE_SERVICE_ACCOUNT_EMAIL and GOOGLE_PRIVATE_KEY from env.
    GOOGLE_PRIVATE_KEY may be:
      - A PEM string (starts with -----BEGIN)
      - A full service-account JSON string (starts with {)
    """
    from google.oauth2 import service_account
    import google.auth.transport.requests as g_requests

    sa_email = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip()
    raw_key = os.environ.get("GOOGLE_PRIVATE_KEY", "").strip()

    if not sa_email or not raw_key:
        missing = []
        if not sa_email:
            missing.append("GOOGLE_SERVICE_ACCOUNT_EMAIL")
        if not raw_key:
            missing.append("GOOGLE_PRIVATE_KEY")
        raise RuntimeError(
            f"Missing env vars: {', '.join(missing)}\n\n"
            "GOOGLE_PRIVATE_KEY should be either:\n"
            "  • A PEM string:  -----BEGIN RSA PRIVATE KEY-----\\n...\n"
            "  • A full service account JSON string:  {\"type\":\"service_account\",...}\n\n"
            "Download the JSON key from GCP → IAM & Admin → Service Accounts,\n"
            "then export it: export GOOGLE_PRIVATE_KEY=$(cat key.json)"
        )

    # Build service_account_info dict from PEM or full JSON
    if raw_key.lstrip().startswith("{"):
        try:
            sa_info = json.loads(raw_key)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"GOOGLE_PRIVATE_KEY looks like JSON but failed to parse: {exc}"
            )
    else:
        # PEM key — construct the minimum required dict
        sa_info = {
            "type": "service_account",
            "client_email": sa_email,
            "private_key": raw_key.replace("\\n", "\n"),
            "token_uri": _TOKEN_URI,
        }

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=[_ADWORDS_SCOPE]
    )
    creds.refresh(g_requests.Request())
    return creds.token


def _build_headers(access_token: str, manager_id: str | None = None) -> dict:
    dev_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if not dev_token:
        raise RuntimeError("GOOGLE_ADS_DEVELOPER_TOKEN not set.")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": dev_token,
        "Content-Type": "application/json",
    }
    if manager_id:
        headers["login-customer-id"] = manager_id.replace("-", "")
    return headers


def _normalise_id(raw: str) -> str:
    return raw.replace("-", "").replace(" ", "").strip()


# ── REST query runner ─────────────────────────────────────────────────────────

def _gaql(
    customer_id: str,
    gaql: str,
    headers: dict,
    label: str,
    errors: list,
    page_size: int = 10_000,
) -> list[dict]:
    """POST a GAQL query; page through results; return list of row dicts."""
    url = f"{_ADS_API_BASE}/customers/{customer_id}/googleAds:searchStream"
    rows: list[dict] = []
    try:
        resp = requests.post(
            url,
            headers=headers,
            json={"query": gaql},
            timeout=60,
        )
        resp.raise_for_status()
        # searchStream returns newline-delimited JSON chunks
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            rows.extend(chunk.get("results", []))
    except Exception as exc:
        errors.append({"query": label, "error": sanitize_error(exc)})
        log.warning("Query '%s' failed: %s", label, sanitize_error(exc))
    return rows


# ── impression-share helper ───────────────────────────────────────────────────

def _is_val(v: Any) -> float | None:
    """Convert Google's IS value to float or None.

    Google returns 0.0–1.0 floats but also "--" (string) when the metric is
    below the reporting threshold.
    """
    if v is None or v == "--":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ── GAQL strings ──────────────────────────────────────────────────────────────

# Impression share included here — the critical gap in the upstream Express API
_Q_CAMPAIGNS = """
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.advertising_channel_type,
  campaign.advertising_channel_sub_type,
  campaign.bidding_strategy_type,
  campaign.budget_amount_micros,
  campaign.network_settings.target_google_search,
  campaign.network_settings.target_search_network,
  campaign.network_settings.target_content_network,
  campaign.network_settings.target_partner_search_network,
  campaign.geo_target_type_setting.positive_geo_target_type,
  campaign.serving_status,
  campaign.primary_status,
  campaign.target_cpa.target_cpa_micros,
  campaign.target_roas.target_roas,
  metrics.cost_micros,
  metrics.clicks,
  metrics.impressions,
  metrics.conversions,
  metrics.all_conversions,
  metrics.cost_per_conversion,
  metrics.ctr,
  metrics.average_cpc,
  metrics.search_impression_share,
  metrics.search_budget_lost_impression_share,
  metrics.search_rank_lost_impression_share,
  metrics.search_absolute_top_impression_share,
  metrics.search_top_impression_share,
  metrics.content_impression_share,
  metrics.content_budget_lost_impression_share,
  metrics.content_rank_lost_impression_share
FROM campaign
WHERE campaign.status = 'ENABLED'
  AND segments.date DURING LAST_30_DAYS
"""

_Q_AD_GROUPS = """
SELECT
  ad_group.id,
  ad_group.name,
  ad_group.status,
  ad_group.type,
  campaign.id,
  campaign.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions
FROM ad_group
WHERE campaign.status = 'ENABLED'
  AND ad_group.status != 'REMOVED'
  AND segments.date DURING LAST_30_DAYS
"""

# No segments.date — avoids per-day row explosion (gaql-notes.md)
_Q_KEYWORDS = """
SELECT
  ad_group_criterion.criterion_id,
  ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type,
  ad_group_criterion.status,
  ad_group_criterion.quality_info.quality_score,
  ad_group_criterion.quality_info.creative_quality_score,
  ad_group_criterion.quality_info.post_click_quality_score,
  ad_group_criterion.quality_info.search_predicted_ctr,
  ad_group_criterion.system_serving_status,
  campaign.id,
  campaign.name,
  campaign.bidding_strategy_type,
  ad_group.id,
  ad_group.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions
FROM keyword_view
WHERE campaign.status = 'ENABLED'
  AND ad_group.status != 'REMOVED'
  AND ad_group_criterion.status != 'REMOVED'
"""

# Can't filter campaign.status / ad_group.status in search_term_view (INVALID_ARGUMENT)
_Q_SEARCH_TERMS = """
SELECT
  search_term_view.search_term,
  campaign.id,
  campaign.name,
  ad_group.id,
  ad_group.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions,
  metrics.ctr,
  metrics.average_cpc
FROM search_term_view
WHERE segments.date DURING LAST_30_DAYS
ORDER BY metrics.cost_micros DESC
LIMIT 1000
"""

_Q_RSAS = """
SELECT
  ad_group_ad.ad.id,
  ad_group_ad.ad.type,
  ad_group_ad.ad.responsive_search_ad.headlines,
  ad_group_ad.ad.responsive_search_ad.descriptions,
  ad_group_ad.ad.responsive_search_ad.path1,
  ad_group_ad.ad.responsive_search_ad.path2,
  ad_group_ad.ad.final_urls,
  ad_group_ad.status,
  ad_group_ad.ad_strength,
  campaign.id,
  campaign.name,
  ad_group.id,
  ad_group.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions,
  metrics.ctr
FROM ad_group_ad
WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
  AND ad_group_ad.status != 'REMOVED'
  AND campaign.status = 'ENABLED'
  AND segments.date DURING LAST_30_DAYS
"""

_Q_CONVERSION_ACTIONS = """
SELECT
  conversion_action.id,
  conversion_action.name,
  conversion_action.type,
  conversion_action.status,
  conversion_action.category,
  conversion_action.primary_for_goal,
  conversion_action.counting_type,
  conversion_action.attribution_model_settings.attribution_model,
  conversion_action.value_settings.default_value,
  conversion_action.include_in_conversions_metric,
  conversion_action.origin
FROM conversion_action
WHERE conversion_action.status != 'REMOVED'
"""

_Q_SHARED_NEG_LISTS = """
SELECT
  shared_set.id,
  shared_set.name,
  shared_set.type,
  shared_set.member_count,
  shared_set.status,
  shared_set.reference_count
FROM shared_set
WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
  AND shared_set.status != 'REMOVED'
"""

_Q_CAMPAIGN_NEG_ASSIGNMENTS = """
SELECT
  campaign_shared_set.campaign,
  campaign_shared_set.shared_set,
  campaign_shared_set.status,
  shared_set.name,
  shared_set.member_count
FROM campaign_shared_set
WHERE campaign_shared_set.status = 'ENABLED'
"""

_Q_CAMPAIGN_NEG_KWS = """
SELECT
  campaign_criterion.campaign,
  campaign_criterion.keyword.text,
  campaign_criterion.keyword.match_type,
  campaign_criterion.negative
FROM campaign_criterion
WHERE campaign_criterion.type = 'KEYWORD'
  AND campaign_criterion.negative = TRUE
"""

_Q_ASSET_GROUPS = """
SELECT
  asset_group.id,
  asset_group.name,
  asset_group.status,
  asset_group.ad_strength,
  asset_group.resource_name,
  campaign.id,
  campaign.name
FROM asset_group
WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
  AND campaign.status = 'ENABLED'
  AND asset_group.status != 'REMOVED'
"""

_Q_ASSET_GROUP_ASSETS = """
SELECT
  asset_group_asset.asset_group,
  asset_group_asset.field_type,
  asset_group_asset.status,
  asset.type
FROM asset_group_asset
WHERE asset_group_asset.status != 'REMOVED'
"""

_Q_EXTENSIONS = """
SELECT
  campaign_extension_setting.campaign,
  campaign_extension_setting.extension_type,
  campaign_extension_setting.status
FROM campaign_extension_setting
WHERE campaign_extension_setting.status = 'ENABLED'
"""

_Q_AUDIENCES = """
SELECT
  ad_group_criterion.criterion_id,
  ad_group_criterion.type,
  ad_group_criterion.status,
  campaign.id,
  campaign.name,
  ad_group.id
FROM campaign_audience_view
WHERE campaign.status = 'ENABLED'
  AND ad_group_criterion.status != 'REMOVED'
"""

_Q_CUSTOMER_MATCH = """
SELECT
  user_list.id,
  user_list.name,
  user_list.type,
  user_list.size_for_display,
  user_list.size_for_search,
  user_list.membership_status,
  user_list.match_rate_percentage
FROM user_list
WHERE user_list.type = 'CRM_BASED'
  AND user_list.membership_status = 'OPEN'
"""


# ── keyword dedup ─────────────────────────────────────────────────────────────

def _dedup_keywords(rows: list[dict]) -> list[dict]:
    """Deduplicate by (ad_group_id, keyword_text, match_type), sum metrics."""
    seen: dict[tuple, dict] = {}
    for row in rows:
        ag_id = str(row.get("adGroup", {}).get("id", ""))
        kw = row.get("adGroupCriterion", {}).get("keyword", {})
        key = (ag_id, kw.get("text", "").lower(), kw.get("matchType", ""))
        if key not in seen:
            seen[key] = row
        else:
            em = seen[key].setdefault("metrics", {})
            nm = row.get("metrics", {})
            for m in ("impressions", "clicks", "costMicros", "conversions"):
                em[m] = em.get(m, 0) + _to_num(nm.get(m, 0))
    return list(seen.values())


def _to_num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── asset group asset count ───────────────────────────────────────────────────

def _count_assets(asset_rows: list[dict]) -> dict[str, dict[str, int]]:
    """Return {asset_group_resource: {field_type: count}}."""
    counts: dict[str, dict[str, int]] = {}
    for row in asset_rows:
        aga = row.get("assetGroupAsset", {})
        ag_res = aga.get("assetGroup", "")
        ft = aga.get("fieldType", "")
        counts.setdefault(ag_res, {})
        counts[ag_res][ft] = counts[ag_res].get(ft, 0) + 1
    return counts


# ── impression share normalisation ───────────────────────────────────────────

def _normalise_is(campaigns: list[dict]) -> list[dict]:
    """Convert IS float values and "--" strings to float|None in each row."""
    is_fields = [
        "searchImpressionShare",
        "searchBudgetLostImpressionShare",
        "searchRankLostImpressionShare",
        "searchAbsoluteTopImpressionShare",
        "searchTopImpressionShare",
        "contentImpressionShare",
        "contentBudgetLostImpressionShare",
        "contentRankLostImpressionShare",
    ]
    for row in campaigns:
        m = row.get("metrics", {})
        for f in is_fields:
            if f in m:
                m[f] = _is_val(m[f])
    return campaigns


# ── main fetch ────────────────────────────────────────────────────────────────

def fetch(customer_id: str) -> dict:
    customer_id = _normalise_id(customer_id)
    manager_id = _normalise_id(
        os.environ.get("GOOGLE_ADS_MANAGER_CUSTOMER_ID", "")
    ) or None

    token = _get_access_token()
    headers = _build_headers(token, manager_id)
    errors: list[dict] = []
    ts = datetime.now(timezone.utc).isoformat()

    def q(gaql: str, label: str) -> list[dict]:
        return _gaql(customer_id, gaql, headers, label, errors)

    campaigns = _normalise_is(q(_Q_CAMPAIGNS, "campaigns"))
    ad_groups = q(_Q_AD_GROUPS, "ad_groups")
    keywords = _dedup_keywords(q(_Q_KEYWORDS, "keywords"))
    search_terms = q(_Q_SEARCH_TERMS, "search_terms")
    ads = q(_Q_RSAS, "rsa_ads")
    conversion_actions = q(_Q_CONVERSION_ACTIONS, "conversion_actions")
    shared_neg_lists = q(_Q_SHARED_NEG_LISTS, "shared_neg_lists")
    campaign_neg_assignments = q(_Q_CAMPAIGN_NEG_ASSIGNMENTS, "campaign_neg_assignments")
    campaign_neg_kws = q(_Q_CAMPAIGN_NEG_KWS, "campaign_neg_keywords")
    asset_groups_raw = q(_Q_ASSET_GROUPS, "asset_groups")
    asset_rows = q(_Q_ASSET_GROUP_ASSETS, "asset_group_assets")
    extensions = q(_Q_EXTENSIONS, "extensions")
    audiences = q(_Q_AUDIENCES, "audiences")
    customer_match = q(_Q_CUSTOMER_MATCH, "customer_match")

    asset_counts = _count_assets(asset_rows)
    asset_groups = [
        {**row, "assetCounts": asset_counts.get(
            row.get("assetGroup", {}).get("resourceName", ""), {}
        )}
        for row in asset_groups_raw
    ]

    return {
        "meta": {
            "fetched_at": ts,
            "customer_id": customer_id,
            "manager_id": manager_id,
            "date_range": "LAST_30_DAYS",
            "api_version": "v20",
            "total_errors": len(errors),
        },
        "campaigns": campaigns,
        "ad_groups": ad_groups,
        "keywords": keywords,
        "search_terms": search_terms,
        "ads": ads,
        "conversion_actions": conversion_actions,
        "shared_negative_lists": shared_neg_lists,
        "campaign_neg_list_assignments": campaign_neg_assignments,
        "campaign_negative_keywords": campaign_neg_kws,
        "asset_groups": asset_groups,
        "extensions": extensions,
        "audiences": audiences,
        "customer_match_lists": customer_match,
        "data_errors": errors,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Google Ads account data (REST API v20, service account auth).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--customer-id",
        default=os.environ.get("GOOGLE_ADS_CUSTOMER_ID", ""),
        help="Google Ads customer ID (dashes optional). Default: $GOOGLE_ADS_CUSTOMER_ID",
    )
    parser.add_argument(
        "--output", default="-",
        help="Output file path. Use '-' for stdout (default).",
    )
    parser.add_argument(
        "--check-auth", action="store_true",
        help="Verify credentials only; do not fetch data.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)

    if args.check_auth:
        try:
            _get_access_token()
            print(json.dumps({"status": "ok", "auth_method": "service_account"}))
        except Exception as exc:
            print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
            sys.exit(1)
        return

    if not args.customer_id:
        print(json.dumps({
            "error": "customer_id required",
            "hint": "Set GOOGLE_ADS_CUSTOMER_ID or pass --customer-id",
        }), file=sys.stderr)
        sys.exit(1)

    try:
        data = fetch(args.customer_id)
    except RuntimeError as exc:
        print(json.dumps({"error": sanitize_error(exc)}), file=sys.stderr)
        sys.exit(1)

    output = json.dumps(data, indent=2, default=str)
    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
        print(json.dumps({
            "status": "ok",
            "file": args.output,
            "campaigns": len(data.get("campaigns", [])),
            "keywords": len(data.get("keywords", [])),
            "search_terms": len(data.get("search_terms", [])),
            "errors": len(data.get("data_errors", [])),
        }))


if __name__ == "__main__":
    main()
