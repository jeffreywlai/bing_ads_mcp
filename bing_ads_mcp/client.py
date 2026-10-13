"""One explicit REST boundary. No arbitrary URLs, shared mutable scope, or write retries."""

import asyncio
import json
import re

import httpx

from .auth import OAuth, Settings
from .models import Scope

SERVICES = {
    "campaign": ("campaign", "CampaignManagement"),
    "customer": ("clientcenter", "CustomerManagement"),
    "reporting": ("reporting", "Reporting"),
}


class ApiError(ValueError):
    """A sanitized, actionable error suitable for an MCP result."""

    def __init__(self, message: str, *, unknown: bool = False, **details):
        self.details = {"message": message, "mutation_outcome_unknown": unknown, **details}
        super().__init__(json.dumps(self.details))


def error_details(body: object) -> list[dict]:
    """Keep codes and batch indices; provider messages can echo tokens and input data."""
    result = []
    if isinstance(body, list):
        for item in body:
            result.extend(error_details(item))
    elif isinstance(body, dict):
        if "Code" in body or "ErrorCode" in body:
            result.append({
                key: value for key, value in body.items()
                if key in {"Code", "ErrorCode", "Index", "FieldPath"}
                and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,120}", str(value))
            })
        else:
            for key in ("Errors", "OperationErrors", "BatchErrors", "PartialErrors", "Error", "ApiFault"):
                result.extend(error_details(body.get(key)))
    return result[:100]


class AdsClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http
        self.oauth = OAuth(settings, http)

    async def request(
        self, service: str, path: str, body: dict, *, scope: Scope | None = None,
        method: str = "POST", mutation: bool = False, retry: bool = True,
    ) -> dict:
        if mutation and self.settings.read_only:
            raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
        if not self.settings.developer_token:
            raise ApiError("Set BING_ADS_DEVELOPER_TOKEN before making API calls.")
        if scope and "AccountId" in body and str(body["AccountId"]) != scope.account_id:
            raise ApiError("AccountId does not match the account scope.")
        host, namespace = SERVICES[service]
        sandbox = ".sandbox" if self.settings.environment == "sandbox" else ""
        url = f"https://{host}.api{sandbox}.bingads.microsoft.com/{namespace}/v13/{path}"
        headers = {
            "Authorization": f"Bearer {await self.oauth.access_token()}",
            "DeveloperToken": self.settings.developer_token,
        }
        if scope:
            headers.update(CustomerId=scope.customer_id, CustomerAccountId=scope.account_id)
        attempts = 3 if retry and not mutation else 1
        for attempt in range(attempts):
            try:
                response = await self.http.request(method, url, json=body, headers=headers, timeout=30)
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ApiError(
                    "Transport failed. Read current entity state before retrying a write.",
                    unknown=mutation, retryable=not mutation,
                ) from None
            try:
                data = response.json()
            except ValueError:
                data = None
            transient = response.status_code in {408, 429, 500, 502, 503, 504}
            errors = error_details(data)
            throttled = any(str(e.get("Code")) == "117" for e in errors)
            if transient and not errors and attempt + 1 < attempts:
                delay = response.headers.get("Retry-After", "")
                if not delay or (delay.isdigit() and int(delay) <= 10):
                    await asyncio.sleep(int(delay) if delay.isdigit() else 2**attempt)
                    continue
            if response.is_error or not isinstance(data, dict) or any(
                data.get(key) for key in ("Errors", "OperationErrors", "BatchErrors", "ApiFault")
            ):
                raise ApiError(
                    "Microsoft Advertising request failed; inspect error codes and TrackingId.",
                    unknown=mutation and (transient or not isinstance(data, dict)),
                    http_status=response.status_code, errors=errors,
                    retryable=(transient or throttled) and not mutation,
                    retry_after_seconds=60 if throttled else None,
                    tracking_id=response.headers.get("TrackingId"),
                )
            return {**data, "TrackingId": response.headers.get("TrackingId") or data.get("TrackingId")}
        raise AssertionError("unreachable")

    async def change(self, path: str, body: dict, scope: Scope, dry_run: bool) -> dict:
        if self.settings.read_only:
            raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
        if dry_run:
            return {
                "dry_run": True, "applied": False, "scope": scope.model_dump(),
                "method": "PUT", "operation": path, "request": body,
                "validation": "Local validation only; Microsoft has not validated this request.",
            }
        data = await self.request("campaign", path, body, scope=scope, method="PUT", mutation=True)
        if "PartialErrors" not in data:
            raise ApiError("Write response is missing PartialErrors. Verify entity state.", unknown=True)
        errors = error_details(data["PartialErrors"])
        return {
            "dry_run": False, "all_succeeded": not bool(data["PartialErrors"]),
            "partial_errors": errors, "tracking_id": data["TrackingId"],
            "guidance": "Failed indices refer to the submitted array. Re-read before retrying.",
        }
