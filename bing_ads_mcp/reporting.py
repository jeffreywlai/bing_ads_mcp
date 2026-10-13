"""Report request context and conservative coverage information; never clip requests."""

import calendar
import csv
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta

RETENTION_DOC = (
    "https://learn.microsoft.com/en-us/advertising/guides/report-data-retention-time-periods"
    "?view=bingads-13"
)
# Reviewed rows from Microsoft's table. Unknown reports remain explicitly unknown.
STANDARD = {
    "Account",
    "Ad",
    "AdDynamicText",
    "AdGroup",
    "Audience",
    "Campaign",
    "Combination",
    "DestinationUrl",
    "Keyword",
    "Conversion",
    "ProductDimension",
    "ProductPartition",
    "ProductPartitionUnit",
    "DSAAutoTarget",
    "DSACategory",
}
SHORT_HOURLY = {"PublisherUsage": 2, "SearchQuery": 1, "ProductSearchQuery": 1, "DSASearchQuery": 1}


def context_name(environment, scope, request_id, namespace="report"):
    key = json.dumps([namespace, environment, scope.model_dump(), request_id], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:32] + ".json"


def request_context(request, *, today=None):
    today = today or datetime.now(UTC).date()
    time = request.get("Time") or {}
    time_fields = time if isinstance(time, dict) else {}
    family = request.get("Type", "").removesuffix("PerformanceReportRequest")
    aggregation = request.get("Aggregation", "Summary")
    months = days = None
    if aggregation in {"Hourly", "Daily", "Weekly", "Monthly", "Yearly", "Summary"}:
        if family in STANDARD | SHORT_HOURLY.keys():
            months = SHORT_HOURLY.get(family, 6) if aggregation == "Hourly" else 36
        elif family == "Asset" and aggregation != "Hourly":
            days = 30
        elif request.get("Type") in {
            "SearchCampaignChangeHistoryReportRequest",
            "ShareOfVoiceReportRequest",
        }:
            months = 6
    earliest = None
    if months:
        y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
        earliest = date(y, m + 1, min(today.day, calendar.monthrange(y, m + 1)[1]))
    elif days:
        earliest = today - timedelta(days=days)
    start = None
    try:
        custom = time_fields.get("CustomDateRangeStart") or {}
        start = date(custom["Year"], custom["Month"], custom["Day"])
    except KeyError, TypeError, ValueError:
        pass
    warnings = []
    if earliest and start and start < earliest:
        warnings.append(
            "Requested start precedes Microsoft's documented retention window. Older data may "
            "be unavailable and the first monthly/yearly bucket may be partial. The request "
            "was not shortened. ReturnOnlyCompleteData does not recover expired history."
        )
    return {
        "requested_time": time,
        "aggregation": aggregation,
        "report_type": request.get("Type"),
        "retention": {
            "known": earliest is not None,
            "months": months,
            "days": days,
            "approximate_earliest_date": earliest.isoformat() if earliest else None,
            "as_of_utc": today.isoformat(),
            "documentation": RETENTION_DOC,
        },
        "warnings": warnings,
        "effective_range_verified": False,
    }


def remember(artifacts, environment, scope, request_id, context):
    artifacts.save(
        json.dumps(context).encode(),
        "json",
        name=context_name(environment, scope, request_id),
    )


def coverage(artifacts, environment, scope, request_id, files):
    try:
        context = artifacts.json_value(context_name(environment, scope, request_id))
    except Exception:
        context = {"requested_time": None, "effective_range_verified": False}
    observed = []
    for file in files:
        artifact = file["artifact"]
        if not artifact.endswith((".csv", ".tsv")):
            continue
        count, first, last = 0, None, None
        try:
            with artifacts.path(artifact).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(
                    stream, delimiter="\t" if artifact.endswith(".tsv") else ","
                )
                if not reader.fieldnames or "TimePeriod" not in reader.fieldnames:
                    continue
                for row in reader:
                    period = row.get("TimePeriod") or ""
                    if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2}(?:\|\d{1,2})?)?)?", period):
                        count += 1
                        first = min(first, period) if first else period
                        last = max(last, period) if last else period
            observed.append(
                {
                    "artifact": artifact,
                    "rows_with_time_period": count,
                    "first_time_period": first,
                    "last_time_period": last,
                }
            )
        except OSError, UnicodeError, csv.Error:
            observed.append({"artifact": artifact, "inspection": "unavailable"})
    return {
        **context,
        "observed_time_periods": observed,
        "coverage_note": "Observed report buckets are not proof of complete date coverage; "
        "missing activity, retention and timezone boundaries can affect them. "
        "The retention cutoff is approximate; inspect Microsoft's report metadata where present.",
    }
