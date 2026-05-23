#!/usr/bin/env python3
"""
Fetch Google Ads account data for the 80-check audit.

Runs GAQL queries for all audit categories (conversion tracking, wasted spend,
account structure, keywords/QS, ads/assets, settings, bidding/budget, PMax)
and outputs structured JSON consumed by the /ads google skill.

Usage:
    python fetch_google_ads.py
    python fetch_google_ads.py --customer-id 191-261-1776
    python fetch_google_ads.py --customer-id 191-261-1776 --output data.json
    python fetch_google_ads.py --check-auth

Auth (set one method via environment variables):

  Option A — OAuth2 (recommended):
    GOOGLE_ADS_DEVELOPER_TOKEN   required
    GOOGLE_ADS_CLIENT_ID
    GOOGLE_ADS_CLIENT_SECRET
    GOOGLE_ADS_REFRESH_TOKEN
    GOOGLE_ADS_LOGIN_CUSTOMER_ID  (manager/MCC account ID, digits only)

  Option B — Service account:
    GOOGLE_ADS_DEVELOPER_TOKEN   required
    GOOGLE_APPLICATION_CREDENTIALS  path to service account JSON key file
    GOOGLE_ADS_LOGIN_CUSTOMER_ID  (manager/MCC account ID, digits only)

  GOOGLE_ADS_CUSTOMER_ID  sets the default --customer-id (can override with CLI flag)

Output keys:
    meta             fetch timestamp, customer_id, date_range, errors
    campaigns        list of campaign objects with metrics
    ad_groups        list of ad group objects
    keywords         deduplicated keyword list with QS and metrics
    search_terms     top 1000 search terms by cost (last 30 days)
    ads              RSAs and other ad types with assets
    conversion_actions  all non-removed conversion actions
    shared_negative_lists  shared negative keyword lists
    campaign_negative_lists  per-campaign negative keyword count
    asset_groups     PMax asset groups with asset counts
    extensions       sitelinks, callouts, structured snippets per campaign
    audiences        audience segments applied to campaigns
    data_errors      per-query fetch failures with reasons
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from url_utils import sanitize_error

log = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────

def _normalise_customer_id(raw: str) -> str:
    """Strip dashes and spaces from a customer ID string."""
    return raw.replace("-", "").replace(" ", "").strip()


def _build_client(login_customer_id: str | None = None):
    """Build GoogleAdsClient from environment variables.

    Tries OAuth2 first; falls back to service account via
    GOOGLE_APPLICATION_CREDENTIALS.  Raises RuntimeError with actionable
    guidance if neither set of credentials is found.
    """
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        raise RuntimeError(
            "google-ads library not installed. Run: pip install google-ads"
        )

    dev_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if not dev_token:
        raise RuntimeError(
            "GOOGLE_ADS_DEVELOPER_TOKEN not set. "
            "Export it before running this script."
        )

    # Normalise login_customer_id (strip dashes if present)
    env_login = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
    if not login_customer_id and env_login:
        login_customer_id = _normalise_customer_id(env_login)

    # ── Option A: OAuth2 ──────────────────────────────────────────────────
    client_id = os.environ.get("GOOGLE_ADS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "").strip()

    if client_id and client_secret and refresh_token:
        config: dict[str, Any] = {
            "developer_token": dev_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "use_proto_plus": True,
        }
        if login_customer_id:
            config["login_customer_id"] = login_customer_id
        return GoogleAdsClient.load_from_dict(config)

    # ── Option B: Service account ─────────────────────────────────────────
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_file:
        if not os.path.isfile(key_file):
            raise RuntimeError(
                f"GOOGLE_APPLICATION_CREDENTIALS points to '{key_file}' "
                "but that file does not exist."
            )
        config = {
            "developer_token": dev_token,
            "json_key_file_path": key_file,
            "use_proto_plus": True,
        }
        impersonate = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip()
        if impersonate:
            config["impersonated_email"] = impersonate
        if login_customer_id:
            config["login_customer_id"] = login_customer_id
        return GoogleAdsClient.load_from_dict(config)

    # ── Neither found ─────────────────────────────────────────────────────
    raise RuntimeError(
        "No Google Ads credentials found. Set one of:\n\n"
        "  OAuth2 (recommended):\n"
        "    GOOGLE_ADS_CLIENT_ID=...\n"
        "    GOOGLE_ADS_CLIENT_SECRET=...\n"
        "    GOOGLE_ADS_REFRESH_TOKEN=...\n\n"
        "  Service account:\n"
        "    GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json\n\n"
        "Both methods also require GOOGLE_ADS_DEVELOPER_TOKEN.\n"
        "See ads/references/mcp-integration.md for setup instructions."
    )


def _run_query(
    service,
    customer_id: str,
    gaql: str,
    label: str,
    errors: list[dict],
) -> list:
    """Execute a GAQL query and return results as a list, recording failures."""
    try:
        stream = service.search_stream(customer_id=customer_id, query=gaql)
        rows = []
        for batch in stream:
            for row in batch.results:
                rows.append(row)
        return rows
    except Exception as exc:
        errors.append({"query": label, "error": sanitize_error(exc)})
        log.warning("Query '%s' failed: %s", label, sanitize_error(exc))
        return []


# ── GAQL query builders ───────────────────────────────────────────────────────

_CAMPAIGN_QUERY = """
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
  campaign.geo_target_type_setting.negative_geo_target_type,
  campaign.serving_status,
  campaign.primary_status,
  campaign.experiment_type,
  metrics.cost_micros,
  metrics.clicks,
  metrics.impressions,
  metrics.conversions,
  metrics.all_conversions,
  metrics.cost_per_conversion,
  metrics.ctr,
  metrics.average_cpc,
  metrics.search_budget_lost_impression_share,
  metrics.search_rank_lost_impression_share
FROM campaign
WHERE campaign.status = 'ENABLED'
  AND segments.date DURING LAST_30_DAYS
"""

_CAMPAIGN_SETTINGS_QUERY = """
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.advertising_channel_type,
  campaign.bidding_strategy_type,
  campaign.target_cpa.target_cpa_micros,
  campaign.target_roas.target_roas,
  campaign.maximize_conversions.target_cpa_micros,
  campaign.maximize_conversion_value.target_roas,
  campaign.labels,
  bidding_strategy.name,
  bidding_strategy.type
FROM campaign
WHERE campaign.status = 'ENABLED'
"""

_AD_GROUP_QUERY = """
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

# No segments.date to avoid per-day row explosion (see gaql-notes.md)
_KEYWORD_QUERY = """
SELECT
  ad_group_criterion.criterion_id,
  ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type,
  ad_group_criterion.status,
  ad_group_criterion.quality_info.quality_score,
  ad_group_criterion.quality_info.creative_quality_score,
  ad_group_criterion.quality_info.post_click_quality_score,
  ad_group_criterion.quality_info.search_predicted_ctr,
  ad_group_criterion.final_urls,
  ad_group_criterion.system_serving_status,
  campaign.id,
  campaign.name,
  campaign.bidding_strategy_type,
  ad_group.id,
  ad_group.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions,
  metrics.average_quality_score
FROM keyword_view
WHERE campaign.status = 'ENABLED'
  AND ad_group.status != 'REMOVED'
  AND ad_group_criterion.status != 'REMOVED'
"""

# LAST_30_DAYS only (LAST_90_DAYS not valid with DURING per gaql-notes.md)
# Can't filter campaign.status or ad_group.status here (INVALID_ARGUMENT)
_SEARCH_TERM_QUERY = """
SELECT
  search_term_view.search_term,
  search_term_view.resource_name,
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

_RSA_QUERY = """
SELECT
  ad_group_ad.ad.id,
  ad_group_ad.ad.name,
  ad_group_ad.ad.type,
  ad_group_ad.ad.responsive_search_ad.headlines,
  ad_group_ad.ad.responsive_search_ad.descriptions,
  ad_group_ad.ad.responsive_search_ad.path1,
  ad_group_ad.ad.responsive_search_ad.path2,
  ad_group_ad.ad.final_urls,
  ad_group_ad.status,
  ad_group_ad.ad_strength,
  ad_group_ad.policy_summary.approval_status,
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

_CONVERSION_ACTION_QUERY = """
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
  conversion_action.value_settings.always_use_default_value,
  conversion_action.click_through_lookback_window_days,
  conversion_action.view_through_lookback_window_days,
  conversion_action.include_in_conversions_metric,
  conversion_action.origin
FROM conversion_action
WHERE conversion_action.status != 'REMOVED'
"""

_SHARED_NEG_LIST_QUERY = """
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

_CAMPAIGN_NEG_LIST_QUERY = """
SELECT
  campaign_shared_set.campaign,
  campaign_shared_set.shared_set,
  campaign_shared_set.status,
  shared_set.name,
  shared_set.member_count
FROM campaign_shared_set
WHERE campaign_shared_set.status = 'ENABLED'
"""

_CAMPAIGN_NEGATIVE_KW_QUERY = """
SELECT
  campaign_criterion.campaign,
  campaign_criterion.keyword.text,
  campaign_criterion.keyword.match_type,
  campaign_criterion.type,
  campaign_criterion.negative
FROM campaign_criterion
WHERE campaign_criterion.type = 'KEYWORD'
  AND campaign_criterion.negative = TRUE
"""

_ASSET_GROUP_QUERY = """
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

_ASSET_GROUP_ASSET_QUERY = """
SELECT
  asset_group_asset.asset_group,
  asset_group_asset.field_type,
  asset_group_asset.status,
  asset.type,
  asset.name
FROM asset_group_asset
WHERE asset_group_asset.status != 'REMOVED'
"""

_EXTENSION_QUERY = """
SELECT
  campaign_extension_setting.campaign,
  campaign_extension_setting.extension_type,
  campaign_extension_setting.status,
  campaign_extension_setting.device
FROM campaign_extension_setting
WHERE campaign_extension_setting.status = 'ENABLED'
"""

_AUDIENCE_QUERY = """
SELECT
  campaign_audience_view.resource_name,
  ad_group_criterion.criterion_id,
  ad_group_criterion.type,
  ad_group_criterion.status,
  ad_group_criterion.bid_modifier,
  campaign.id,
  campaign.name,
  ad_group.id
FROM campaign_audience_view
WHERE campaign.status = 'ENABLED'
  AND ad_group_criterion.status != 'REMOVED'
"""

_CUSTOMER_MATCH_QUERY = """
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


# ── serialiser ────────────────────────────────────────────────────────────────

def _to_dict(proto_obj) -> dict:
    """Convert a proto-plus message to a plain dict."""
    from google.protobuf.json_format import MessageToDict  # type: ignore
    try:
        return MessageToDict(proto_obj._pb, preserving_proto_field_name=True)
    except Exception:
        # Fallback for non-proto objects
        return {}


def _row_to_dict(row) -> dict:
    """Flatten a GAQL result row to a nested dict."""
    d = {}
    for field in ("campaign", "ad_group", "ad_group_criterion", "ad_group_ad",
                  "search_term_view", "conversion_action", "shared_set",
                  "campaign_shared_set", "campaign_criterion", "asset_group",
                  "asset_group_asset", "asset", "campaign_extension_setting",
                  "ad_group_audience_view", "campaign_audience_view",
                  "user_list", "bidding_strategy", "metrics",
                  "campaign_audience_view"):
        obj = getattr(row, field, None)
        if obj is not None:
            d[field] = _to_dict(obj)
    return d


# ── dedup helpers ─────────────────────────────────────────────────────────────

def _dedup_keywords(rows: list) -> list[dict]:
    """Deduplicate keyword rows by (ad_group_id, keyword_text, match_type).

    The keyword_view + no-date segmentation approach still returns one row per
    keyword per date when historical slices differ; this ensures unique entries
    with aggregated metrics per gaql-notes.md.
    """
    seen: dict[tuple, dict] = {}
    for row in rows:
        d = _row_to_dict(row)
        crit = d.get("ad_group_criterion", {})
        kw = crit.get("keyword", {})
        ag = d.get("ad_group", {})
        key = (
            str(ag.get("id", "")),
            kw.get("text", "").lower(),
            kw.get("match_type", ""),
        )
        if key not in seen:
            seen[key] = d
        else:
            # Aggregate numeric metrics
            existing_m = seen[key].get("metrics", {})
            new_m = d.get("metrics", {})
            for metric in ("impressions", "clicks", "cost_micros", "conversions",
                           "all_conversions"):
                existing_m[metric] = (
                    existing_m.get(metric, 0) + new_m.get(metric, 0)
                )
            seen[key]["metrics"] = existing_m
    return list(seen.values())


# ── main fetch ────────────────────────────────────────────────────────────────

def fetch(customer_id: str) -> dict:
    """Fetch all audit data for *customer_id* and return as a plain dict."""
    customer_id = _normalise_customer_id(customer_id)
    client = _build_client()
    service = client.get_service("GoogleAdsService")

    errors: list[dict] = []
    ts = datetime.now(timezone.utc).isoformat()

    def q(gaql: str, label: str) -> list:
        return _run_query(service, customer_id, gaql, label, errors)

    # ── campaigns ─────────────────────────────────────────────────────────
    raw_campaigns = q(_CAMPAIGN_QUERY, "campaigns_metrics")
    raw_campaign_settings = q(_CAMPAIGN_SETTINGS_QUERY, "campaigns_settings")

    # Merge settings into campaign objects
    settings_by_id: dict[str, dict] = {}
    for row in raw_campaign_settings:
        d = _row_to_dict(row)
        cid = str(d.get("campaign", {}).get("id", ""))
        if cid:
            settings_by_id[cid] = d

    campaigns = []
    for row in raw_campaigns:
        d = _row_to_dict(row)
        cid = str(d.get("campaign", {}).get("id", ""))
        if cid in settings_by_id:
            # Merge bidding strategy and target info from settings query
            extra = settings_by_id[cid]
            d["campaign"].update({
                k: v for k, v in extra.get("campaign", {}).items()
                if k not in d.get("campaign", {})
            })
        campaigns.append(d)

    # ── ad groups ─────────────────────────────────────────────────────────
    ad_groups = [_row_to_dict(r) for r in q(_AD_GROUP_QUERY, "ad_groups")]

    # ── keywords (deduplicated) ────────────────────────────────────────────
    keywords = _dedup_keywords(q(_KEYWORD_QUERY, "keywords"))

    # ── search terms (filter removed in app layer per gaql-notes.md) ──────
    raw_st = q(_SEARCH_TERM_QUERY, "search_terms")
    search_terms = [_row_to_dict(r) for r in raw_st]

    # ── RSAs ──────────────────────────────────────────────────────────────
    ads = [_row_to_dict(r) for r in q(_RSA_QUERY, "rsa_ads")]

    # ── conversion actions ────────────────────────────────────────────────
    conversion_actions = [
        _row_to_dict(r) for r in q(_CONVERSION_ACTION_QUERY, "conversion_actions")
    ]

    # ── shared negative lists ──────────────────────────────────────────────
    shared_neg_lists = [
        _row_to_dict(r) for r in q(_SHARED_NEG_LIST_QUERY, "shared_neg_lists")
    ]
    campaign_neg_assignments = [
        _row_to_dict(r) for r in q(_CAMPAIGN_NEG_LIST_QUERY, "campaign_neg_list_assignments")
    ]
    campaign_neg_kws = [
        _row_to_dict(r) for r in q(_CAMPAIGN_NEGATIVE_KW_QUERY, "campaign_neg_keywords")
    ]

    # ── PMax asset groups ──────────────────────────────────────────────────
    raw_asset_groups = q(_ASSET_GROUP_QUERY, "asset_groups")
    raw_assets = q(_ASSET_GROUP_ASSET_QUERY, "asset_group_assets")

    # Count assets per asset_group by field_type
    asset_counts: dict[str, dict[str, int]] = {}
    for row in raw_assets:
        d = _row_to_dict(row)
        ag_resource = d.get("asset_group_asset", {}).get("asset_group", "")
        field_type = d.get("asset_group_asset", {}).get("field_type", "")
        if ag_resource not in asset_counts:
            asset_counts[ag_resource] = {}
        asset_counts[ag_resource][field_type] = (
            asset_counts[ag_resource].get(field_type, 0) + 1
        )

    asset_groups = []
    for row in raw_asset_groups:
        d = _row_to_dict(row)
        ag = d.get("asset_group", {})
        resource = ag.get("resource_name", "")
        d["asset_counts"] = asset_counts.get(resource, {})
        asset_groups.append(d)

    # ── extensions ────────────────────────────────────────────────────────
    extensions = [
        _row_to_dict(r) for r in q(_EXTENSION_QUERY, "extensions")
    ]

    # ── audience signals ──────────────────────────────────────────────────
    audiences = [
        _row_to_dict(r) for r in q(_AUDIENCE_QUERY, "audiences")
    ]

    # ── customer match lists ───────────────────────────────────────────────
    customer_match = [
        _row_to_dict(r) for r in q(_CUSTOMER_MATCH_QUERY, "customer_match")
    ]

    return {
        "meta": {
            "fetched_at": ts,
            "customer_id": customer_id,
            "date_range": "LAST_30_DAYS",
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
        description="Fetch Google Ads account data for audit analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--customer-id",
        default=os.environ.get("GOOGLE_ADS_CUSTOMER_ID", ""),
        help="Google Ads customer ID (dashes optional). "
             "Default: $GOOGLE_ADS_CUSTOMER_ID",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output file path. Use '-' for stdout (default).",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="Verify credentials and print the auth method; do not fetch data.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON output (default: true).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if args.check_auth:
        try:
            client = _build_client()
            # Determine which method was used
            if os.environ.get("GOOGLE_ADS_REFRESH_TOKEN"):
                method = "OAuth2"
            else:
                method = "Service account"
            print(json.dumps({"status": "ok", "auth_method": method}))
        except RuntimeError as exc:
            print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
            sys.exit(1)
        return

    if not args.customer_id:
        print(
            json.dumps({
                "error": "customer_id required",
                "hint": "Set GOOGLE_ADS_CUSTOMER_ID or pass --customer-id",
            }),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = fetch(args.customer_id)
    except RuntimeError as exc:
        print(json.dumps({"error": sanitize_error(exc)}), file=sys.stderr)
        sys.exit(1)

    indent = 2 if args.pretty else None
    output = json.dumps(data, indent=indent, default=str)

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
        print(
            json.dumps({
                "status": "ok",
                "file": args.output,
                "campaigns": len(data.get("campaigns", [])),
                "keywords": len(data.get("keywords", [])),
                "search_terms": len(data.get("search_terms", [])),
                "errors": len(data.get("data_errors", [])),
            })
        )


if __name__ == "__main__":
    main()
