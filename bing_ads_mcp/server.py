"""FastMCP entry point: a small tool surface over the complete API catalog."""

import argparse
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_session
from fastmcp.server.sessions import InvalidSession, create_session
from pydantic import Field

from . import content, conversions, mutations, transfers
from .artifacts import INLINE_BYTES, Artifacts
from .auth import Settings
from .catalog import describe_model, models, operation, operations
from .client import AdsClient, ApiError
from .entities import get_entities
from .models import EntityQuery, Id, Scope, Service
from .validation import normalized, operation_hints

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
JOB = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False}
CONTROL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
MutationSession = Annotated[
    str | None,
    Field(description="Session ID from unlock_mutation_tools; required to execute writes."),
]


def create_server(settings: Settings | None = None, client: AdsClient | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    artifacts = Artifacts(settings.export_dir)
    current = client

    @asynccontextmanager
    async def lifespan(server):
        nonlocal current
        if client is not None:
            yield
            return
        async with httpx.AsyncClient(follow_redirects=False) as http:
            current = AdsClient(settings, http)
            try:
                yield
            finally:
                current.close()
                current = None

    mcp = FastMCP(
        "Bing Ads",
        lifespan=lifespan,
        mask_error_details=True,
        instructions=(
            "Microsoft Advertising REST v13. Start with get_accounts. Filter list_operations by "
            "service/search, then use "
            "get_operation_schema for full native API access; inspect nested/derived models with "
            "get_model_schema. Use native PascalCase fields and decimal strings for 64-bit IDs. "
            "Writes start locked. After user authorization, call unlock_mutation_tools and pass "
            "its session_id on each write; call lock_mutation_tools with that ID when finished. "
            "write_api previews by default; dry_run=false sends a real change only with a valid "
            "unlocked session_id. Never reuse an unlock from another conversation. "
            "Local previews include advisory SDK checks. Use preflight=true "
            "to preview replacement diffs; removals need allow_remove=true. Supported updates read "
            "back by default; inspect verification before claiming success. Never blindly repeat "
            "a write after an uncertain outcome. "
            "Reports/bulk jobs submit once and resume by request ID. All returned provider fields "
            "and errors are retained except URL credentials. Analyze large local artifact files, "
            "or use read_json_artifact/read_csv_rows with filters, fields and numeric_fields. "
            "read_artifact supports UTF-8 or base64 bytes. Select report Columns for the task. "
            "Entity performance_time uses native ReportTime with explicit ReportTimeZone. "
            "Percentage values are percentage points; money is in account currency. This server "
            "uses the local signed-in user's permissions; it is not a shared multi-user gateway."
        ),
    )

    async def require_mutation_session(session_id: str | None) -> None:
        if settings.read_only:
            raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
        if session_id:
            try:
                await get_session(session_id)
                return
            except InvalidSession:
                pass
        raise ApiError(
            "Advertising writes are locked. Call unlock_mutation_tools after user authorization "
            "and pass its session_id with the write."
        )

    @mcp.tool(annotations=CONTROL)
    async def unlock_mutation_tools() -> dict:
        """Unlock writes after user authorization and return a new session_id.

        Pass that ID on each real write, then call lock_mutation_tools with the same ID.
        Each unlock is independent and lasts until locked or the server process exits.
        This workflow guard does not override BING_ADS_READ_ONLY or Microsoft permissions.
        """
        if settings.read_only:
            raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
        return {"mutation_tools_unlocked": True, "session_id": await create_session()}

    @mcp.tool(annotations=CONTROL)
    async def lock_mutation_tools(session_id: str) -> dict:
        """Revoke the session_id returned by unlock_mutation_tools; future writes need a new ID.

        Other independently unlocked sessions are unaffected. Already-started writes are not
        cancelled. Keep this ID private; it is a write capability on this local server.
        """
        try:
            session = await get_session(session_id)
        except InvalidSession:
            return {"mutation_tools_unlocked": False}
        await session.end()
        return {"mutation_tools_unlocked": False}

    @mcp.tool(annotations=READ)
    def list_operations(
        service: Service | None = None,
        search: str = "",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        details: bool = False,
    ) -> dict:
        """Find operations with case/separator-insensitive search; details=true adds routes/docs.

        Default entries are compact; use get_operation_schema for a selected operation.
        """
        found = [
            op.describe()
            if details
            else {"service": op.service, "operation": op.name, "effect": op.effect}
            for op in operations().values()
            if (service is None or op.service == service)
            and normalized(search) in normalized(op.name)
        ]
        return {
            "operations": found[offset : offset + limit],
            "total": len(found),
            "next_offset": offset + limit if offset + limit < len(found) else None,
        }

    @mcp.tool(annotations=READ)
    def get_operation_schema(service: Service, operation_name: str) -> dict:
        """Inspect the installed SDK request fields, effect, route and Microsoft documentation."""
        op = operation(service, operation_name)
        return artifacts.bounded(
            {
                **op.describe(),
                "schema": describe_model(service, op.model.__name__),
                **operation_hints(service, operation_name),
            }
        )

    @mcp.tool(annotations=READ)
    def list_models(
        service: Service,
        search: str = "",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """Discover SDK objects/enums, including every report request and column enum."""
        names = sorted(n for n in models(service) if normalized(search) in normalized(n))
        return {
            "models": names[offset : offset + limit],
            "total": len(names),
            "next_offset": offset + limit if offset + limit < len(names) else None,
        }

    @mcp.tool(annotations=READ)
    def get_model_schema(service: Service, model: str) -> dict:
        """Inspect native fields, enum values, and derived types without giant nested schemas."""
        return artifacts.bounded(describe_model(service, model))

    @mcp.tool(annotations=JOB)
    async def read_api(
        service: Service,
        operation_name: str,
        request: dict,
        scope: Scope | None = None,
        download_files: bool = False,
    ) -> dict:
        """Execute a native read or report/bulk-download job. Rejects every advertising write.

        All fields pass through to Microsoft unchanged; follow API pagination explicitly.
        Jobs are not retried. Prefer get_report/get_bulk_result for complete file delivery.
        download_files=true saves signed Microsoft response files before URL redaction, including
        offline conversion report files. Download errors retain the original API result/job ID.
        """
        if operation(service, operation_name).effect == "write":
            raise ApiError("This operation can change data. Use write_api.")
        output = await current.execute(service, operation_name, request, scope)
        if download_files:
            output["downloads"] = await transfers.download_response_files(
                current, artifacts, output
            )
        elif any(transfers.signed_urls(output)):
            output["download_hint"] = (
                "URL credentials are redacted. For native reads, download_files=true saves the "
                "files internally. Use get_report/get_bulk_result to poll existing jobs."
            )
        return artifacts.bounded(output)

    @mcp.tool(annotations=WRITE)
    async def write_api(
        service: Service,
        operation_name: str,
        request: dict,
        scope: Scope | None = None,
        dry_run: bool = True,
        session_id: MutationSession = None,
        verify: bool = True,
        allow_remove: bool = False,
        preflight: bool = False,
    ) -> dict:
        """Preview or execute any SDK write: campaigns, audiences, billing or insights.

        dry_run=true is a local preview only. dry_run=false requires the session_id returned
        by unlock_mutation_tools and sends exactly once. Inspect
        Inspect PartialErrors indices and null result slots. verify=true reads back campaign,
        ad group and keyword updates and negative-site sets. preflight=true adds read-only calls
        to replacement previews. Execution requires allow_remove=true for removals/empty site lists.
        """
        if not dry_run:
            await require_mutation_session(session_id)
        return artifacts.bounded(
            await mutations.execute(
                current,
                artifacts,
                service,
                operation_name,
                request,
                scope,
                dry_run=dry_run,
                verify=verify,
                allow_remove=allow_remove,
                preflight=preflight,
            )
        )

    @mcp.tool(annotations=JOB)
    async def run_report(scope: Scope, request: dict) -> dict:
        """Submit any native ReportRequest once. Use get_report with its ReportRequestId.

        Discover all Type/Columns/Filter/Scope options using list_models(reporting).
        For CSV row access set Format=Csv, FormatVersion=2.0, ExcludeReportHeader=true,
        ExcludeReportFooter=true, ExcludeColumnHeaders=false. Set Time.ReportTimeZone.
        ReturnOnlyCompleteData=true can reject today's/unprocessed data instead of guessing.
        """
        return artifacts.bounded(await transfers.submit_report(current, scope, request, artifacts))

    @mcp.tool(annotations=READ)
    async def get_report(
        scope: Scope, request_id: str, wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0
    ) -> dict:
        """Poll an existing report and save its complete download. Pending jobs keep their ID."""
        return artifacts.bounded(
            await transfers.poll_report(current, artifacts, scope, request_id, wait_seconds)
        )

    @mcp.tool(annotations=READ)
    async def get_bulk_result(scope: Scope, request_id: str, upload: bool = False) -> dict:
        """Poll bulk transfers and retrieve complete files, including per-record errors."""
        return artifacts.bounded(
            await transfers.poll_bulk(current, artifacts, scope, request_id, upload)
        )

    @mcp.tool(annotations=WRITE)
    async def upload_bulk_csv(
        scope: Scope,
        csv_content: str,
        dry_run: bool = True,
        session_id: MutationSession = None,
        verify: bool = True,
    ) -> dict:
        """Preview or upload native bulk CSV once; all entity types/columns pass through.

        Include the Format Version record (6.0). Upload result IDs/errors are retrieved with
        get_bulk_result(upload=true). Real uploads require session_id from unlock_mutation_tools.
        verify=true reads back supported Campaign updates when polled to completion; other records
        stay unverified. Never retry the whole file after partial failures.
        """
        if not dry_run:
            await require_mutation_session(session_id)
        return artifacts.bounded(
            await transfers.upload_bulk(current, artifacts, scope, csv_content, dry_run, verify)
        )

    @mcp.tool(annotations=READ)
    def list_content_operations() -> dict:
        """Discover Shopping Content v9.1 products, inventory and catalog operations."""
        return {
            "operations": [
                {"name": name, "method": spec[0], "resource": spec[1], "effect": spec[3]}
                for name, spec in content.OPERATIONS.items()
            ],
            "documentation": "https://learn.microsoft.com/en-us/advertising/shopping-content/reference",
            "note": "Native JSON bodies and query parameter names; only production is available.",
        }

    @mcp.tool(annotations=WRITE)
    async def content_api(
        operation_name: str,
        merchant_id: Id,
        scope: Scope | None = None,
        item_id: str | None = None,
        request: dict | None = None,
        query: dict | None = None,
        dry_run: bool = True,
        session_id: MutationSession = None,
    ) -> dict:
        """Execute Content reads or preview/write products, inventory and catalogs.

        Writes default to local preview; dry_run=false requires session_id from
        unlock_mutation_tools. Reads need no unlock. API-side product/
        inventory validation uses query={"dry-run":""} with dry_run=false. Catalogs have no
        API-side dry-run. Product inserts replace the whole offer: include every desired field.
        """
        if operation_name not in content.OPERATIONS:
            raise ApiError("Unknown Content operation. Use list_content_operations.")
        if not dry_run and content.OPERATIONS[operation_name][3] == "write":
            await require_mutation_session(session_id)
        return artifacts.bounded(
            await content.execute(
                current, operation_name, merchant_id, scope, item_id, request, query, dry_run
            )
        )

    @mcp.tool(annotations=READ)
    def read_artifact(
        artifact: str,
        offset: int = 0,
        max_bytes: int = 12000,
        encoding: Literal["base64", "utf-8"] = "base64",
    ) -> dict:
        """Read exact bytes or readable UTF-8. Follow next_offset; offsets always count bytes.

        Prefer read_json_artifact/read_csv_rows or local analysis for structured data.
        """
        return artifacts.read(artifact, offset, max_bytes, encoding)

    @mcp.tool(annotations=READ)
    def read_json_artifact(
        artifact: str, pointer: str = "", query: EntityQuery | None = None
    ) -> dict:
        """Read structured JSON without base64. pointer uses JSON Pointer, e.g. /data/Campaigns.

        Arrays support filters, sort, paging, fields and numeric_fields. Large objects list member
        pointers; choose one to inspect. Snapshots use /rows. Source files remain complete.
        """
        from .entities import filter_sort

        query = query or EntityQuery()
        if query.performance_time:
            raise ApiError("Artifact reads do not submit performance reports.")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "artifact": artifact,
                    "pointer": pointer,
                    "query": query.model_dump(exclude={"next_page_token", "page_size"}),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if query.next_page_token:
            return {
                "artifact": artifact,
                "pointer": pointer,
                "kind": "snapshot",
                **artifacts.entity_page(
                    None,
                    fingerprint,
                    query.page_size,
                    query.next_page_token,
                    fields=query.fields,
                    numeric_fields=query.numeric_fields,
                ),
            }
        value = artifacts.json_value(artifact, pointer)
        if isinstance(value, list):
            rows = value
            if (query.filters or query.sort_by) and any(not isinstance(r, dict) for r in rows):
                raise ApiError("Filtering/sorting requires object rows; null slots are retained.")
            rows = filter_sort(rows, query, metrics_nested=False)
            kind = "array"
        elif isinstance(value, dict) and len(json.dumps(value).encode()) > INLINE_BYTES - 2000:
            if query.filters or query.sort_by or query.fields is not None or query.numeric_fields:
                raise ApiError("Use a pointer to an array for query options.")
            rows = [
                {
                    "pointer": pointer + "/" + key.replace("~", "~0").replace("/", "~1"),
                    "type": "array"
                    if isinstance(item, list)
                    else "object"
                    if isinstance(item, dict)
                    else "scalar",
                    **({"size": len(item)} if isinstance(item, (dict, list)) else {}),
                }
                for key, item in value.items()
            ]
            kind = "object_members"
        else:
            if any(
                (
                    query.filters,
                    query.sort_by,
                    query.fields is not None,
                    query.numeric_fields,
                    query.next_page_token,
                )
            ):
                raise ApiError("Use a pointer to an array for query options.")
            encoded = json.dumps(value, ensure_ascii=False).encode()
            if len(encoded) > INLINE_BYTES - 2000:
                return {
                    "oversized_value": artifacts.save(encoded, "json"),
                    "guidance": "Use read_artifact(encoding=utf-8) or read the local path.",
                }
            return artifacts.bounded({"artifact": artifact, "pointer": pointer, "value": value})
        return {
            "artifact": artifact,
            "pointer": pointer,
            "kind": kind,
            **artifacts.entity_page(
                None if query.next_page_token else rows,
                fingerprint,
                query.page_size,
                query.next_page_token,
                fields=query.fields,
                numeric_fields=query.numeric_fields,
            ),
        }

    @mcp.tool(annotations=WRITE)
    async def submit_uet_events(
        scope: Scope,
        tag_id: Id,
        request: dict,
        dry_run: bool = True,
        session_id: MutationSession = None,
    ) -> dict:
        """Preview or submit native UET Conversions API events (data array, at most 1000).

        dry_run=false requires session_id from unlock_mutation_tools before retrieving/creating
        the tag auth key and sending events once. The key
        stays inside the server. Preserve eventId for deduplication and use Microsoft's
        required hashed identifiers/consent fields. Inspect eventsReceived and all indexed
        errors/warnings: HTTP 200 can include skipped events or removed optional fields.
        See https://learn.microsoft.com/en-us/advertising/guides/uet-conversion-api-integration
        for native eventType, eventTime, userData, customData and other payload fields.
        """
        if not dry_run:
            await require_mutation_session(session_id)
        return artifacts.bounded(await conversions.submit(current, scope, tag_id, request, dry_run))

    @mcp.tool(annotations=READ)
    def read_csv_rows(artifact: str, query: EntityQuery | None = None) -> dict:
        """Page complete CSV/TSV rows with stable snapshots. Source bytes stay in the artifact."""
        from .entities import filter_sort

        query = query or EntityQuery()
        if query.performance_time:
            raise ApiError("CSV rows already contain their requested report metrics.")
        rows = (
            None
            if query.next_page_token
            else filter_sort(transfers.csv_rows(artifacts, artifact), query, metrics_nested=False)
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "artifact": artifact,
                    "query": query.model_dump(exclude={"next_page_token", "page_size"}),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return artifacts.entity_page(
            rows,
            fingerprint,
            query.page_size,
            query.next_page_token,
            fields=query.fields,
            numeric_fields=query.numeric_fields,
        )

    # One explicit wrapper per entity keeps usable MCP signatures without a custom router.
    @mcp.tool(annotations=READ)
    async def get_accounts(query: EntityQuery | None = None, scope: Scope | None = None) -> dict:
        """List accessible accounts with customer IDs, filters, paging and optional performance."""
        return await get_entities(current, artifacts, "accounts", scope, query or EntityQuery())

    @mcp.tool(annotations=READ)
    async def get_campaigns(
        scope: Scope, query: EntityQuery | None = None, campaign_id: Id | None = None
    ) -> dict:
        """Campaign configuration and optional performance; all campaign types are included."""
        return await get_entities(
            current, artifacts, "campaigns", scope, query or EntityQuery(), campaign_id
        )

    @mcp.tool(annotations=READ)
    async def get_ad_groups(
        scope: Scope,
        query: EntityQuery | None = None,
        campaign_id: Id | None = None,
        ad_group_id: Id | None = None,
    ) -> dict:
        """Ad groups with campaign context, filters, sorting and optional performance."""
        return await get_entities(
            current, artifacts, "ad_groups", scope, query or EntityQuery(), campaign_id, ad_group_id
        )

    @mcp.tool(annotations=READ)
    async def get_ads(
        scope: Scope,
        query: EntityQuery | None = None,
        campaign_id: Id | None = None,
        ad_group_id: Id | None = None,
    ) -> dict:
        """Ads and complete creative fields, campaign/ad group context and optional performance."""
        return await get_entities(
            current, artifacts, "ads", scope, query or EntityQuery(), campaign_id, ad_group_id
        )

    @mcp.tool(annotations=READ)
    async def get_keywords(
        scope: Scope,
        query: EntityQuery | None = None,
        campaign_id: Id | None = None,
        ad_group_id: Id | None = None,
    ) -> dict:
        """Keywords, match types, bids, status and optional date-scoped performance."""
        return await get_entities(
            current, artifacts, "keywords", scope, query or EntityQuery(), campaign_id, ad_group_id
        )

    @mcp.tool(annotations=READ)
    async def get_asset_groups(
        scope: Scope, campaign_id: Id, query: EntityQuery | None = None
    ) -> dict:
        """Performance Max asset groups: search themes, assets and optional performance."""
        return await get_entities(
            current, artifacts, "asset_groups", scope, query or EntityQuery(), campaign_id
        )

    @mcp.tool(annotations=READ)
    async def get_audience_associations(
        scope: Scope,
        query: EntityQuery | None = None,
        campaign_id: Id | None = None,
        ad_group_id: Id | None = None,
    ) -> dict:
        """Campaign/ad group audience associations, names, bids and optional performance."""
        return await get_entities(
            current,
            artifacts,
            "audience_associations",
            scope,
            query or EntityQuery(),
            campaign_id,
            ad_group_id,
        )

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Microsoft Advertising MCP")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = create_server()
    if args.transport == "http":
        server.run(transport="http", host="127.0.0.1", port=args.port, show_banner=False)
    else:
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
