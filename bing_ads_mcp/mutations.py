"""Small, explicit read-back plans and replacement guards over the native executor."""

import json
from decimal import Decimal, InvalidOperation

from .client import ApiError
from .entities import call, flags
from .privacy import safe_output

# Request collection, ID field, read operation, parent fields. No guessed routes.
UPDATES = {
    "update_campaigns": ("Campaigns", "Id", "get_campaigns_by_ids", ("AccountId",)),
    "update_ad_groups": ("AdGroups", "Id", "get_ad_groups_by_ids", ("CampaignId",)),
    "update_keywords": ("Keywords", "Id", "get_keywords_by_ids", ("AdGroupId", "AssetGroupId")),
}
REPLACEMENTS = {
    "set_negative_sites_to_campaigns": (
        "CampaignNegativeSites",
        "CampaignId",
        "get_negative_sites_by_campaign_ids",
        ("AccountId",),
    ),
    "set_negative_sites_to_ad_groups": (
        "AdGroupNegativeSites",
        "AdGroupId",
        "get_negative_sites_by_ad_group_ids",
        ("CampaignId",),
    ),
}


async def read_back(client, name, body, scope):
    collection, id_field, reader, parents = (UPDATES | REPLACEMENTS)[name]
    requested = body.get(collection)
    if (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(row, dict) or not row.get(id_field) for row in requested)
    ):
        raise ApiError("Read-back requires entity IDs in every request item.")
    ids = [str(row[id_field]) for row in requested]
    if len(ids) != len(set(ids)):
        raise ApiError("Read-back requires unique entity IDs.")
    plural_ids = {"Campaigns": "CampaignIds", "AdGroups": "AdGroupIds", "Keywords": "KeywordIds"}
    body_read = {
        **{key: body[key] for key in parents if key in body},
        plural_ids.get(collection, id_field + "s"): ids,
    }
    if reader == "get_campaigns_by_ids":
        body_read["CampaignType"] = flags("CampaignType")
    pages, rows = [], []
    for offset in range(0, len(ids), 100):
        data = await call(
            client,
            "campaign",
            reader,
            {**body_read, plural_ids.get(collection, id_field + "s"): ids[offset : offset + 100]},
            scope,
        )
        if not isinstance(data.get(collection), list):
            raise ApiError(
                "Microsoft did not return the requested collection.", provider_response=data
            )
        pages.append(data)
        rows.extend(data[collection])
    actual = {str(row[id_field]): row for row in rows if isinstance(row, dict) and id_field in row}
    if len(actual) != sum(isinstance(r, dict) and id_field in r for r in rows):
        raise ApiError("Read-back returned duplicate entity IDs.", provider_response=pages)
    return requested, actual, pages[0] if len(pages) == 1 else {"pages": pages}


async def replacement_diff(client, name, body, scope):
    requested, actual, _ = await read_back(client, name, body, scope)
    id_field = REPLACEMENTS[name][1]
    diffs = []
    for row in requested:
        entity_id = str(row[id_field])
        if entity_id not in actual:
            raise ApiError("Cannot safely replace sites: current entity was not returned.")
        desired = row.get("NegativeSites")
        existing = actual[entity_id].get("NegativeSites")
        # A returned entity with a null list has no directly assigned sites. A null
        # entity above is an incomplete read, and must not be treated as an empty list.
        if not isinstance(desired, list) or not all(isinstance(s, str) for s in desired):
            raise ApiError("NegativeSites must be an explicit list of strings; [] clears it.")
        if existing is not None and (
            not isinstance(existing, list) or not all(isinstance(s, str) for s in existing)
        ):
            raise ApiError("Cannot safely replace sites: unexpected current list shape.")
        old, new = set(existing or []), set(desired)
        diffs.append(
            {
                "id": entity_id,
                "add": sorted(new - old),
                "remove": sorted(old - new),
                "unchanged_count": len(old & new),
                "clears_list": not desired,
            }
        )
    return diffs


def compare(requested, actual, path=""):
    """Compare supplied fields only. Null update semantics are field-specific."""
    result = []
    for key, wanted in requested.items():
        if not path and key == "Id":
            continue
        field = f"{path}.{key}" if path else key
        present = isinstance(actual, dict) and key in actual
        observed = actual.get(key) if present else None
        if isinstance(wanted, dict) and isinstance(observed, dict):
            result.extend(compare(wanted, observed, field))
            continue
        equal = type(wanted) is type(observed) and wanted == observed
        if (
            type(wanted) in (int, float)
            or (isinstance(wanted, str) and key in {"DailyBudget", "AudienceAdsBidAdjustment"})
        ) and type(observed) in (int, float, str):
            try:
                equal = Decimal(str(wanted)) == Decimal(str(observed))
            except InvalidOperation:
                pass
        if (
            key == "NegativeSites"
            and isinstance(wanted, list)
            and (observed is None or isinstance(observed, list))
        ):
            equal = set(wanted) == set(observed or [])
        status = (
            "not_checked"
            if wanted is None
            else "not_returned"
            if not present
            else "matched"
            if equal
            else "mismatch"
        )
        result.append({"field": field, "requested": wanted, "actual": observed, "status": status})
    return result


async def verify_update(client, artifacts, name, body, scope):
    try:
        requested, actual, data = await read_back(client, name, body, scope)
        id_field = (UPDATES | REPLACEMENTS)[name][1]
        checks = [
            {"id": str(row[id_field]), "fields": compare(row, actual.get(str(row[id_field])))}
            for row in requested
        ]
        statuses = {f["status"] for item in checks for f in item["fields"]}
        return {
            "status": "mismatch"
            if "mismatch" in statuses
            else "matched"
            if statuses == {"matched"}
            else "unverified",
            "entities": checks,
            "read_back": artifacts.save(json.dumps(safe_output(data)).encode(), "json"),
        }
    except Exception as exc:
        # Never hide a completed mutation because its subsequent read failed.
        return {
            "status": "unverified",
            "error": exc.details if isinstance(exc, ApiError) else {"message": "Read-back failed."},
        }


async def execute(
    client,
    artifacts,
    service,
    name,
    body,
    scope,
    *,
    dry_run=True,
    verify=True,
    allow_remove=False,
    preflight=False,
):
    preview = client.preview(service, name, body, scope)
    replacement = service == "campaign" and name in REPLACEMENTS
    diffs = None
    if replacement and (preflight or not dry_run):
        diffs = await replacement_diff(client, name, body, scope)
        if not dry_run and not allow_remove and any(d["remove"] or d["clears_list"] for d in diffs):
            raise ApiError(
                "Replacement would remove exclusions or clear a list. Review the diff and "
                "set allow_remove=true only when those removals are intended.",
                replacement_diff=diffs,
                sent=False,
            )
    if dry_run:
        return {**preview, **({"replacement_diff": diffs} if diffs is not None else {})}
    output = await client.execute(service, name, body, scope)
    if diffs is not None:
        output["replacement_diff"] = diffs
    verification = {"status": "disabled" if not verify else "not_supported"}
    if verify and service == "campaign" and name in UPDATES | REPLACEMENTS:
        verification = await verify_update(client, artifacts, name, body, scope)
        if verification["status"] == "mismatch" and output["status"] == "returned":
            output["provider_status"] = output["status"]
            output["status"] = "verification_mismatch"
    verification["guidance"] = (
        "Read-back checks current state, not causation. A mismatch may reflect propagation delay "
        "or normalization. Re-read before deciding; no mutation was retried. "
        "Null update fields need operation-specific verification."
    )
    output["verification"] = verification
    return output
