"""Native UET Conversions API events; tag credentials stay inside the server."""

import json

import httpx

from .client import AdsClient, ApiError, has_errors, result
from .models import Scope, identifier

DOCUMENTATION = (
    "https://learn.microsoft.com/en-us/advertising/guides/"
    "uet-conversion-api-integration?view=bingads-13"
)


async def submit(
    client: AdsClient, scope: Scope, tag_id: str, request: dict, dry_run: bool = True
) -> dict:
    tag_id = identifier(tag_id)
    if client.settings.environment != "production":
        raise ApiError("UET event submission uses the production Conversions API endpoint.")
    events = request.get("data")
    if not isinstance(events, list) or not 1 <= len(events) <= 1000:
        raise ApiError("The native data array must contain between 1 and 1000 events.")
    try:
        json.dumps(request, allow_nan=False)
    except TypeError, ValueError:
        raise ApiError("Request must contain finite JSON values.") from None
    url = f"https://capi.uet.microsoft.com/v1/{tag_id}/events"
    if dry_run:
        return {
            "dry_run": True,
            "sent": False,
            "method": "POST",
            "url": url,
            "scope": scope.model_dump(),
            "request": request,
            "event_count": len(events),
            "validation": "Local preview only; no Microsoft API call.",
            "authentication": "Execution retrieves the UET tag key, creating it if absent.",
            "documentation": DOCUMENTATION,
        }
    if client.settings.read_only:
        raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
    # The installed SDK and the operation reference agree on these field names.
    # The CAPI guide's TagId/TagAuthKey example differs; never guess by retrying.
    credentials = await client.execute(
        "campaign", "get_uet_tag_auth_key", {"UetTagId": tag_id}, scope
    )
    data = credentials["data"]
    key = data.get("UetTagAuthKey")
    if has_errors(data) or not isinstance(key, str) or not key:
        raise ApiError("Could not obtain a UET tag key. No conversion events were submitted.")
    try:
        response = await client.http.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=request,
            timeout=60,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise ApiError(
            "UET event submission transport failed; no retry attempted. "
            "Preserve eventId values when reconciling an uncertain outcome.",
            unknown=True,
        ) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    received = body.get("eventsReceived") if isinstance(body, dict) else None
    # CAPI can return HTTP 200 with an error object after accepting some events
    # or removing invalid optional fields. Preserve the full indexed diagnostic.
    partial = (
        response.status_code == 200
        and isinstance(received, int)
        and received > 0
        and bool(body.get("error"))
    )
    return {
        "tag_id": tag_id,
        "dry_run": False,
        "sent": True,
        **result(
            response.status_code,
            dict(response.headers),
            response.content,
            effect="write",
            secrets=(key,),
            partial=partial,
        ),
    }
