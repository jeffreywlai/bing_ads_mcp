"""The twelve documented Shopping Content v9.1 operations, outside the v13 SDK."""

from urllib.parse import quote

import httpx

from .client import AdsClient, ApiError, has_errors, result
from .models import Scope, identifier

BASE = "https://content.api.bingads.microsoft.com/shopping/v9.1/bmc"
# method, resource, suffix, effect. All bodies/query fields pass through unchanged.
OPERATIONS = {
    "list_products": ("GET", "products", "", "read"),
    "get_product": ("GET", "products", "/{id}", "read"),
    "insert_product": ("POST", "products", "", "write"),
    "delete_product": ("DELETE", "products", "/{id}", "write"),
    "batch_products": ("POST", "products", "/batch", "write"),
    "batch_inventory": ("POST", "inventory", "/batch", "write"),
    "update_inventory": ("POST", "inventory", "/online/products/{id}", "write"),
    "list_catalogs": ("GET", "catalogs", "", "read"),
    "get_catalog": ("GET", "catalogs", "/{id}", "read"),
    "insert_catalog": ("POST", "catalogs", "", "write"),
    "update_catalog": ("PUT", "catalogs", "/{id}", "write"),
    "delete_catalog": ("DELETE", "catalogs", "/{id}", "write"),
}


async def execute(
    client: AdsClient,
    name: str,
    merchant_id: str,
    scope: Scope | None,
    item_id: str | None = None,
    body: dict | None = None,
    query: dict | None = None,
    dry_run: bool = True,
) -> dict:
    if name not in OPERATIONS:
        raise ApiError("Unknown Content operation. Use list_content_operations.")
    if client.settings.environment != "production":
        raise ApiError(
            "Shopping Content has no sandbox endpoint. Use production credentials and "
            "the documented dry-run query parameter for products/inventory validation."
        )
    method, resource, suffix, effect = OPERATIONS[name]
    if "{id}" in suffix and not item_id:
        raise ApiError("item_id is required for this Content operation.")
    if item_id in {".", ".."}:
        raise ApiError("Invalid item_id.")
    url = f"{BASE}/{identifier(merchant_id)}/{resource}" + suffix.replace(
        "{id}", quote(item_id or "", safe="")
    )
    if effect == "write" and dry_run:
        return {
            "dry_run": True,
            "sent": False,
            "operation": name,
            "method": method,
            "url": url,
            "request": body,
            "query": query,
            "scope": scope.model_dump() if scope else None,
            "validation": "Local preview only; no Microsoft API call.",
        }
    if effect == "write" and client.settings.read_only:
        raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
    headers = await client.headers(scope, legacy=True)
    try:
        response = await client.http.request(
            method,
            url,
            headers=headers,
            json=body,
            params=query,
            timeout=60,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise ApiError(
            "Content request transport failed; no retry attempted.", unknown=effect == "write"
        ) from None
    output = result(
        response.status_code,
        dict(response.headers),
        response.content,
        effect=effect,
        secrets=(
            headers["AuthenticationToken"],
            client.settings.developer_token,
            client.settings.client_secret,
            client.settings.refresh_token,
        ),
    )
    if any(has_errors(e) for e in output["data"].get("entries", [])):
        output["status"] = "partial_failure"
    return {"operation": name, "effect": effect, "dry_run": False, "sent": True, **output}
