"""Seven entity workflows using the same API executor and Reporting service.

Fetch complete configuration, join date-scoped reports, then filter/sort/page a
saved snapshot. An inline page size never limits source data.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation

from .artifacts import Artifacts
from .catalog import models, operation
from .client import AdsClient, ApiError
from .models import EntityQuery, Scope
from .reporting import request_context
from .transfers import csv_rows, poll_report, submit_report

REPORTS = {
    "accounts": ("Account", "AccountId"),
    "campaigns": ("Campaign", "CampaignId"),
    "ad_groups": ("AdGroup", "AdGroupId"),
    "ads": ("Ad", "AdId"),
    "keywords": ("Keyword", "KeywordId"),
    "asset_groups": ("AssetGroup", "AssetGroupId"),
    "audience_associations": ("Audience", "AssociationId"),
}
METRICS = {
    "Spend": "Spend",
    "Impressions": "Impressions",
    "Clicks": "Clicks",
    "ClickThroughRate": "Ctr",
    "AverageCostPerClick": "AverageCpc",
    "AverageCostPerThousandImpressions": "AverageCpm",
    "Conversions": "ConversionsQualified",
    "ClickConversionRate": "ConversionRate",
    "Revenue": "Revenue",
    "ReturnOnAdSpend": "ReturnOnAdSpend",
    "TopImpressionRate": "TopImpressionRatePercent",
    "AbsoluteTopImpressionRate": "AbsoluteTopImpressionRatePercent",
    "CPA": "CostPerConversion",
    "CostPerInstall": "CostPerInstall",
}
OPERATORS = {
    "IsEqualTo",
    "IsNotEqualTo",
    "IsGreaterThan",
    "IsGreaterThanOrEqualTo",
    "IsLessThan",
    "IsLessThanOrEqualTo",
    "IsEqualToAny",
    "Contains",
    "StartsWith",
}


def flags(name: str) -> str:
    return ", ".join(m.to_json() for m in models("campaign")[name] if m.to_json() != "None")


async def call(
    client: AdsClient, service: str, name: str, body: dict, scope: Scope | None = None
) -> dict:
    # Ask for every SDK-known opt-in field, so convenience reads keep new properties.
    model = operation(service, name).model
    field = getattr(model, "model_fields", {}).get("return_additional_fields")
    if field:
        from typing import get_args

        enum = next(t for t in get_args(field.annotation) if t is not type(None))
        body = {
            "ReturnAdditionalFields": ", ".join(m.to_json() for m in enum if m.to_json() != "None"),
            **body,
        }
    output = await client.execute(service, name, body, scope)
    if output["status"] == "partial_failure":
        raise ApiError(
            "Entity retrieval was incomplete. Inspect the provider errors.",
            provider_response=output,
        )
    return output["data"]


async def accounts(client: AdsClient, scope: Scope | None) -> list[dict]:
    if scope:
        result = await call(
            client, "customer", "get_account", {"AccountId": scope.account_id}, scope
        )
        return [result["Account"]] if result.get("Account") else []
    user = await call(client, "customer", "get_user", {})
    predicates = [{"Field": "UserId", "Operator": "Equals", "Value": str(user["User"]["Id"])}]
    rows, seen = [], set()
    index = 0
    while True:
        body = await call(
            client,
            "customer",
            "search_accounts",
            {"Predicates": predicates, "PageInfo": {"Index": index, "Size": 1000}},
        )
        page = body.get("Accounts") or []
        ids = {str(row["Id"]) for row in page}
        if page and ids <= seen:
            raise ApiError(
                "Account pagination repeated a page; results cannot be treated as complete."
            )
        rows.extend(page)
        seen.update(ids)
        if len(page) < 1000:
            return rows
        index += 1


async def configuration(
    client: AdsClient,
    kind: str,
    scope: Scope | None,
    campaign_id: str | None,
    ad_group_id: str | None,
) -> list[dict]:
    if kind == "accounts":
        return await accounts(client, scope)
    if scope is None:
        raise ApiError("An explicit customer/account scope is required.")
    if kind == "asset_groups" and not campaign_id:
        raise ApiError("campaign_id is required for asset groups.")
    if campaign_id:
        campaigns = (
            await call(
                client,
                "campaign",
                "get_campaigns_by_ids",
                {
                    "AccountId": scope.account_id,
                    "CampaignIds": [campaign_id],
                    "CampaignType": flags("CampaignType"),
                },
                scope,
            )
        ).get("Campaigns") or []
    else:
        campaigns = (
            await call(
                client,
                "campaign",
                "get_campaigns_by_account_id",
                {"AccountId": scope.account_id, "CampaignType": flags("CampaignType")},
                scope,
            )
        ).get("Campaigns") or []
    campaigns = [dict(c, CampaignId=c["Id"], CampaignName=c.get("Name")) for c in campaigns if c]
    if kind == "campaigns":
        return campaigns
    rows = []
    for campaign in campaigns:
        cid = str(campaign["Id"])
        parent = {
            "CampaignId": cid,
            "CampaignName": campaign.get("Name"),
            "CampaignType": campaign.get("CampaignType"),
        }
        if kind == "asset_groups":
            groups = (
                await call(
                    client,
                    "campaign",
                    "get_asset_groups_by_campaign_id",
                    {"CampaignId": cid},
                    scope,
                )
            ).get("AssetGroups") or []
            rows.extend({**g, **parent, "AssetGroupId": g["Id"]} for g in groups if g)
            continue
        if kind == "audience_associations":
            criteria = (
                await call(
                    client,
                    "campaign",
                    "get_campaign_criterions_by_ids",
                    {"CampaignId": cid, "CampaignCriterionIds": None, "CriterionType": "Audience"},
                    scope,
                )
            ).get("CampaignCriterions") or []
            rows.extend(association(c, parent, "Campaign") for c in criteria if c)
        groups = (
            await call(
                client, "campaign", "get_ad_groups_by_campaign_id", {"CampaignId": cid}, scope
            )
        ).get("AdGroups") or []
        for group in groups:
            if not group or (ad_group_id and str(group["Id"]) != ad_group_id):
                continue
            gid = str(group["Id"])
            ancestors = {**parent, "AdGroupId": gid, "AdGroupName": group.get("Name")}
            if kind == "ad_groups":
                rows.append({**group, **ancestors})
            elif kind in {"ads", "keywords"}:
                request = {"AdGroupId": gid}
                if kind == "ads":
                    # Microsoft's AdType reference reserves Image for future use.
                    request["AdTypes"] = [
                        m.value for m in models("campaign")["AdType"] if m.value != "Image"
                    ]
                children = (
                    await call(client, "campaign", f"get_{kind}_by_ad_group_id", request, scope)
                ).get("Ads" if kind == "ads" else "Keywords") or []
                key = "AdId" if kind == "ads" else "KeywordId"
                rows.extend({**c, **ancestors, key: c["Id"]} for c in children if c)
            elif kind == "audience_associations":
                criteria = (
                    await call(
                        client,
                        "campaign",
                        "get_ad_group_criterions_by_ids",
                        {
                            "AdGroupId": gid,
                            "AdGroupCriterionIds": None,
                            "CriterionType": "Audience",
                        },
                        scope,
                    )
                ).get("AdGroupCriterions") or []
                rows.extend(association(c, ancestors, "AdGroup") for c in criteria if c)
    if kind == "audience_associations":
        audiences = (
            await call(
                client,
                "campaign",
                "get_audiences_by_ids",
                {"AudienceIds": None, "Type": flags("AudienceType")},
                scope,
            )
        ).get("Audiences") or []
        names = {str(a["Id"]): a.get("Name") for a in audiences if a}
        for row in rows:
            row["AudienceName"] = names.get(str(row.get("AudienceId")))
    return rows


def association(criterion: dict, parent: dict, level: str) -> dict:
    audience = criterion.get("Criterion") or {}
    return {
        **criterion,
        **parent,
        "Level": level,
        "AssociationId": criterion["Id"],
        "AudienceId": audience.get("AudienceId"),
        "AudienceType": audience.get("AudienceType"),
        "BidAdjustment": (criterion.get("CriterionBid") or {}).get("BidAdjustment"),
    }


def number(value):
    try:
        number = Decimal(str(value).replace(",", "").removesuffix("%"))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def field_value(row: dict, field: str):
    if field in row:
        return row[field]
    if field in METRICS:
        return (row.get("Performance") or {}).get(field)
    value = row
    for part in field.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def matches(value, operator, wanted) -> bool:
    if operator == "IsEqualToAny":
        if not isinstance(wanted, list):
            raise ApiError("IsEqualToAny requires an array.")
        return any(matches(value, "IsEqualTo", item) for item in wanted)
    if value is None and wanted is not None:
        return False
    if operator in {"Contains", "StartsWith"}:
        return isinstance(value, str) and (
            str(wanted).casefold() in value.casefold()
            if operator == "Contains"
            else value.casefold().startswith(str(wanted).casefold())
        )
    a, b = number(value), number(wanted)
    left, right = (a, b) if a is not None and b is not None else (value, wanted)
    if operator == "IsEqualTo":
        return left == right
    if operator == "IsNotEqualTo":
        return left != right
    if a is None or b is None:
        return False
    return {
        "IsGreaterThan": a > b,
        "IsGreaterThanOrEqualTo": a >= b,
        "IsLessThan": a < b,
        "IsLessThanOrEqualTo": a <= b,
    }[operator]


def filter_sort(rows: list[dict], query: EntityQuery, *, metrics_nested: bool = True) -> list[dict]:
    fields = set(query.filters) | ({query.sort_by} if query.sort_by else set())
    for field in fields:
        if metrics_nested and field in METRICS and not query.performance_time:
            raise ApiError("Metric filters/sorting require performance_time.")
        if rows and field not in METRICS and all(field_value(row, field) is None for row in rows):
            raise ApiError(
                f"Field {field} was not returned; inspect entity fields before filtering."
            )
    for field, conditions in query.filters.items():
        if set(conditions) - OPERATORS:
            raise ApiError("Unsupported filter operator.", allowed=sorted(OPERATORS))
        rows = [
            r
            for r in rows
            if all(matches(field_value(r, field), op, v) for op, v in conditions.items())
        ]
    if query.sort_by:
        field = query.sort_by
        present = [r for r in rows if field_value(r, field) is not None]
        missing = [r for r in rows if field_value(r, field) is None]

        def key(row):
            v = field_value(row, field)
            n = number(v)
            return (0, n) if n is not None else (1, str(v))

        rows = sorted(present, key=key, reverse=query.descending) + missing
    return rows


async def performance(
    client: AdsClient,
    artifacts: Artifacts,
    kind: str,
    rows: list[dict],
    scope: Scope | None,
    query: EntityQuery,
) -> dict | None:
    if not query.performance_time:
        return None
    if not query.performance_time.get("ReportTimeZone"):
        raise ApiError("performance_time must include Microsoft's ReportTimeZone enum name.")
    family, id_field = REPORTS[kind]
    available = {m.value for m in models("reporting")[family + "PerformanceReportColumn"]}
    mapping = {k: v for k, v in METRICS.items() if v in available}
    # Asset group reports still use Conversions; do not request a nonexistent field.
    if "ConversionsQualified" not in available and "Conversions" in available:
        mapping["Conversions"] = "Conversions"
    scopes = (
        {scope.account_id: scope}
        if scope
        else {
            str(r["Id"]): Scope(customer_id=r["ParentCustomerId"], account_id=r["Id"]) for r in rows
        }
    )
    report_ids = dict(query.performance_report_ids)
    data = {}
    pending = False
    for account_id, account_scope in scopes.items():
        if account_id not in report_ids:
            request = {
                "Type": family + "PerformanceReportRequest",
                "Format": "Csv",
                "FormatVersion": "2.0",
                "ExcludeReportHeader": True,
                "ExcludeReportFooter": True,
                "ExcludeColumnHeaders": False,
                "ReturnOnlyCompleteData": query.return_only_complete_data,
                "Aggregation": "Summary",
                "Scope": {"AccountIds": [account_id]},
                "Time": query.performance_time,
                "Columns": [id_field, *mapping.values()],
            }
            sent = await submit_report(client, account_scope, request, artifacts)
            report_ids[account_id] = sent["data"]["ReportRequestId"]
        response = await poll_report(client, artifacts, account_scope, report_ids[account_id])
        if response["status"] == "Pending":
            pending = True
            continue
        for file in response["files"]:
            for item in csv_rows(artifacts, file["artifact"]):
                key = (account_id, str(item[id_field]))
                if key in data:
                    raise ApiError(
                        "Report returned multiple rows per entity; use the full report "
                        "to analyze its segments. No metric rows were discarded.",
                        request_id=report_ids[account_id],
                        files=response["files"],
                    )
                data[key] = {public: item.get(native) for public, native in mapping.items()}
    if pending:
        return {
            "status": "Pending",
            "query": {**query.model_dump(), "performance_report_ids": report_ids},
            "guidance": "Call this entity tool again with the returned query and same scope/IDs. "
            "Report IDs resume existing jobs without resubmitting.",
        }
    for row in rows:
        aid = scope.account_id if scope else str(row["Id"])
        row["Performance"] = data.get((aid, str(row.get(id_field, row["Id"]))))
        row["UnavailableMetrics"] = sorted(set(METRICS) - set(mapping))
    return None


async def get_entities(
    client: AdsClient,
    artifacts: Artifacts,
    kind: str,
    scope: Scope | None,
    query: EntityQuery,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
) -> dict:
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "kind": kind,
                "environment": client.settings.environment,
                "scope": scope.model_dump() if scope else None,
                "campaign_id": campaign_id,
                "ad_group_id": ad_group_id,
                "query": query.model_dump(
                    exclude={"next_page_token", "page_size", "performance_report_ids"}
                ),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    context = (
        request_context(
            {
                "Type": REPORTS[kind][0] + "PerformanceReportRequest",
                "Time": query.performance_time,
                "Aggregation": "Summary",
            }
        )
        if query.performance_time
        else None
    )
    notices = {"report_context": context} if context and context["warnings"] else {}
    if query.next_page_token:
        return {
            **notices,
            **artifacts.entity_page(
                None,
                fingerprint,
                query.page_size,
                query.next_page_token,
                fields=query.fields,
                numeric_fields=query.numeric_fields,
            ),
        }
    rows = await configuration(client, kind, scope, campaign_id, ad_group_id)
    pending = await performance(client, artifacts, kind, rows, scope, query)
    if pending:
        return {**notices, **pending}
    return {
        **notices,
        **artifacts.entity_page(
            filter_sort(rows, query),
            fingerprint,
            query.page_size,
            fields=query.fields,
            numeric_fields=query.numeric_fields,
        ),
    }
