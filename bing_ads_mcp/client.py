"""One authenticated execution boundary for every v13 API operation."""

import asyncio
import json

import httpx
from fastmcp.exceptions import ToolError
from openapi_client.api_client import ApiClient
from openapi_client.configuration import Configuration
from openapi_client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from .auth import OAuth, Settings
from .catalog import SERVICES, operation
from .models import Scope
from .privacy import safe_output
from .validation import operation_hints, request_constraints, request_issues


class ApiError(ToolError):
    def __init__(self, message: str, *, unknown: bool = False, **details):
        self.details = safe_output(
            {"message": message, "mutation_outcome_unknown": unknown, **details}
        )
        super().__init__(json.dumps(self.details, ensure_ascii=False))


def redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    return value


def has_errors(value) -> bool:
    if isinstance(value, dict):
        return any(
            any(v) if isinstance(v, list) else bool(v)
            for k, v in value.items()
            if k in {"PartialErrors", "BatchErrors", "OperationErrors", "Errors", "errors"}
        )
    return False


def result(
    status: int, headers: dict, raw: bytes, *, effect: str, secrets=(), partial: bool = False
) -> dict:
    """Keep raw provider fields, nested partial errors, indices, and tracking IDs."""
    tracking = next(
        (v for k, v in headers.items() if k.lower() in {"trackingid", "webrequestactivityid"}), None
    )
    try:
        body = json.loads(raw) if raw else {}
    except ValueError, UnicodeError:
        raise ApiError(
            "Microsoft returned a non-JSON response. Verify state before retrying.",
            unknown=effect != "read",
            http_status=status,
            tracking_id=tracking,
        ) from None
    body = redact(body, secrets)
    if (
        not 200 <= status < 300
        or not isinstance(body, dict)
        or not partial
        and any(
            any(body[k]) if isinstance(body.get(k), list) else bool(body.get(k))
            for k in ("OperationErrors", "Errors", "error", "ApiFault", "ApplicationFault")
        )
    ):
        raise ApiError(
            "Microsoft Advertising rejected or could not complete the request.",
            unknown=effect != "read" and (status >= 500 or status in {408, 429}),
            http_status=status,
            tracking_id=tracking,
            provider_response=body,
        )
    return {
        "status": "partial_failure" if partial or has_errors(body) else "returned",
        "data": body,
        "tracking_id": tracking,
        "mutation_outcome_unknown": False,
    }


class AdsClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http
        self.oauth = OAuth(settings, http)
        self.clients = {}
        for service, (_, host, _) in SERVICES.items():
            sandbox = ".sandbox" if settings.environment == "sandbox" else ""
            config = Configuration(
                host=f"https://{host}.api{sandbox}.bingads.microsoft.com", retries=0, debug=False
            )
            self.clients[service] = ApiClient(config)

    def close(self):
        for client in self.clients.values():
            client.rest_client.pool_manager.clear()

    async def headers(self, scope: Scope | None, *, legacy: bool = False) -> dict:
        if not self.settings.developer_token:
            raise ApiError("Set BING_ADS_DEVELOPER_TOKEN before making API calls.")
        try:
            token = await self.oauth.access_token()
        except (ValueError, OSError) as exc:
            raise ApiError(
                str(exc)
                if isinstance(exc, ValueError)
                else "Could not persist OAuth tokens. Check token file permissions."
            ) from None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "DeveloperToken": self.settings.developer_token,
        }
        headers["AuthenticationToken" if legacy else "Authorization"] = (
            token if legacy else f"Bearer {token}"
        )
        if scope:
            headers.update(CustomerId=scope.customer_id, CustomerAccountId=scope.account_id)
        return headers

    def preview(self, service: str, name: str, body: dict, scope: Scope | None) -> dict:
        op = operation(service, name)
        if service in {"campaign", "ad_insight", "reporting", "bulk"} and scope is None:
            raise ApiError("This service requires an explicit customer/account scope.")
        if (
            scope
            and body.get("AccountId") is not None
            and str(body["AccountId"]) != scope.account_id
        ):
            raise ApiError("AccountId must match scope.account_id.")
        try:
            json.dumps(body, allow_nan=False)
        except TypeError, ValueError:
            raise ApiError("Request must contain finite JSON values.") from None
        if problem := request_constraints(service, name, body):
            raise ApiError(problem, **operation_hints(service, name))
        issues = request_issues(service, op.model, body)
        groups = body.get("AdGroups")
        groups = groups if isinstance(groups, list) else []
        if name == "update_ad_groups" and body.get("UpdateAudienceAdsBidAdjustment") is not True:
            if any(isinstance(row, dict) and "AudienceAdsBidAdjustment" in row for row in groups):
                issues.append(
                    {
                        "path": "UpdateAudienceAdsBidAdjustment",
                        "issue": "Set this flag to true to update AudienceAdsBidAdjustment; "
                        "Microsoft otherwise ignores the field.",
                    }
                )
        campaigns = body.get("Campaigns")
        campaigns = campaigns if isinstance(campaigns, list) else []
        if name in {"update_campaigns", "update_ad_groups"} and any(
            isinstance(row, dict) and "AudienceAdsBidAdjustment" in row
            for row in campaigns + groups
        ):
            issues.append(
                {
                    "path": "AudienceAdsBidAdjustment",
                    "issue": "Read back this field: accepted-but-unchanged updates were observed "
                    "in a user session. This is not confirmed as a universal API restriction.",
                }
            )
        return {
            **op.describe(),
            "environment": self.settings.environment,
            "scope": scope.model_dump() if scope else None,
            "request": body,
            "dry_run": True,
            "sent": False,
            "validation": "Local SDK field/type checks are advisory. Microsoft has not "
            "validated this request. Native fields are sent unchanged.",
            **({"warnings": issues} if issues else {}),
            **operation_hints(service, name),
        }

    async def execute(
        self,
        service: str,
        name: str,
        body: dict,
        scope: Scope | None = None,
        *,
        dry_run: bool = False,
    ) -> dict:
        preview = self.preview(service, name, body, scope)
        op = operation(service, name)
        if dry_run:
            return preview
        if op.effect == "write" and self.settings.read_only:
            raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
        headers = await self.headers(scope)
        client = self.clients[service]

        def send():
            params = client.param_serialize(
                op.method, op.path, header_params=headers, body=body, auth_settings=[]
            )
            response = client.call_api(*params, _request_timeout=(10.0, 60.0))
            try:
                raw = response.read()
                # Some SDK versions set .data and return None from read().
                return response.status, dict(response.getheaders()), response.data or raw or b""
            finally:
                response.response.close()
                response.response.release_conn()

        try:
            status, response_headers, raw = await asyncio.to_thread(send)
        except HTTPError, ApiException, OSError:
            raise ApiError(
                "Transport failed. No automatic retry was attempted. "
                "Read current state before repeating a write or job submission.",
                unknown=op.effect != "read",
                retryable=op.effect == "read",
            ) from None
        auth = headers["Authorization"].removeprefix("Bearer ")
        try:
            output = result(
                status,
                response_headers,
                raw,
                effect=op.effect,
                secrets=(
                    auth,
                    self.settings.developer_token,
                    self.settings.client_secret,
                    self.settings.refresh_token,
                ),
            )
        except ApiError as exc:
            hints = operation_hints(service, name)
            if hints:
                details = dict(exc.details)
                message = details.pop("message")
                unknown = details.pop("mutation_outcome_unknown")
                raise ApiError(message, unknown=unknown, **details, **hints) from None
            raise
        return {
            "service": service,
            "operation": name,
            "effect": op.effect,
            "dry_run": False,
            "sent": True,
            **output,
            **({"warnings": preview["warnings"]} if "warnings" in preview else {}),
            **operation_hints(service, name),
        }
