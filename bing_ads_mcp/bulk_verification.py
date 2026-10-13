"""Read back reviewed Campaign bulk fields. Other records stay explicitly unverified."""

from decimal import Decimal, InvalidOperation

from .mutations import verify_update

# https://learn.microsoft.com/en-us/advertising/bulk-service/campaign?view=bingads-13
CAMPAIGN_FIELDS = {
    "Campaign": "Name",
    "Status": "Status",
    "Budget": "DailyBudget",
    "Bid Adjustment": "AudienceAdsBidAdjustment",
    "Budget Type": "BudgetType",
    "Tracking Template": "TrackingUrlTemplate",
    "Final Url Suffix": "FinalUrlSuffix",
}


async def verify_campaign_rows(client, artifacts, scope, rows):
    campaigns, unchecked = [], []
    for index, row in enumerate(rows):
        if row.get("Type") == "Format Version":
            continue
        entity_id = row.get("Id") or ""
        if (
            row.get("Type") != "Campaign"
            or not entity_id.isdecimal()
            or int(entity_id) <= 0
            or row.get("Status") == "Deleted"
        ):
            unchecked.append(
                {"row_index": index, "reason": "Only existing Campaign updates are mapped."}
            )
            continue
        native = {"Id": entity_id}
        for column, value in row.items():
            if not value or column in {"Type", "Id", "Parent Id", "Client Id"}:
                continue
            if column not in CAMPAIGN_FIELDS or value == "delete_value":
                unchecked.append(
                    {"row_index": index, "column": column, "reason": "Not mapped for verification."}
                )
                continue
            if column in {"Budget", "Bid Adjustment"}:
                try:
                    number = Decimal(value)
                    if not number.is_finite():
                        raise InvalidOperation
                except InvalidOperation:
                    unchecked.append(
                        {"row_index": index, "column": column, "reason": "Invalid number."}
                    )
                    continue
            native[CAMPAIGN_FIELDS[column]] = value
        if len(native) > 1:
            campaigns.append(native)
    results = []
    # Match the documented campaign read limit; never resubmit uploaded records.
    for offset in range(0, len(campaigns), 100):
        results.append(
            await verify_update(
                client,
                artifacts,
                "update_campaigns",
                {"AccountId": scope.account_id, "Campaigns": campaigns[offset : offset + 100]},
                scope,
            )
        )
    state = (
        "mismatch"
        if any(r["status"] == "mismatch" for r in results)
        else (
            "matched"
            if results and not unchecked and all(r["status"] == "matched" for r in results)
            else "unverified"
        )
    )
    return {
        "status": state,
        "campaign_checks": results,
        "unchecked": unchecked,
        "guidance": "Also inspect every upload result row for provider errors. Read-back reflects "
        "current state and can lag; it does not prove causation. Unmapped records/fields need "
        "manual verification. No upload or mutation was repeated.",
    }
