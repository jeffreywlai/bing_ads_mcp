import base64
import json
from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from bing_ads_mcp import mutations, reporting
from bing_ads_mcp.artifacts import INLINE_BYTES, Artifacts
from bing_ads_mcp.auth import Settings
from bing_ads_mcp.catalog import operation
from bing_ads_mcp.client import AdsClient, ApiError
from bing_ads_mcp.models import Scope
from bing_ads_mcp.privacy import safe_output
from bing_ads_mcp.server import create_server
from bing_ads_mcp.transfers import poll_bulk, poll_report, submit_report
from bing_ads_mcp.validation import request_issues
from bing_ads_mcp.views import numeric

SCOPE = Scope(customer_id="1", account_id="2")


@pytest.fixture
async def client(tmp_path):
    def no_network(request):
        raise AssertionError("These tests must not contact Microsoft.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as http:
        client = AdsClient(Settings(export_dir=tmp_path), http)
        yield client
        client.close()


async def test_discovery_compact_details_normalized_and_documented_constraints(client):
    async with Client(create_server(client.settings, client)) as mcp:
        compact = (await mcp.call_tool("list_operations", {})).data
        detailed = (await mcp.call_tool("list_operations", {"details": True})).data
        assert compact["total"] == detailed["total"] == 290
        assert compact["next_offset"] == detailed["next_offset"] == 50
        for brief, full in zip(compact["operations"], detailed["operations"], strict=True):
            assert brief == {k: full[k] for k in ("service", "operation", "effect")}
        assert len(json.dumps(compact)) < len(json.dumps(detailed)) * 0.3
        for query in ("NegativeKeyword", "negative_keyword", "NEGATIVE KEYWORD"):
            found = (await mcp.call_tool("list_operations", {"search": query})).data
            assert found["total"] > 0
        found = (
            await mcp.call_tool("list_operations", {"search": "AddNegativeKeywordsToEntities"})
        ).data
        assert found["total"] == 1
        found = (
            await mcp.call_tool("list_models", {"service": "campaign", "search": "conversion goal"})
        ).data
        assert "ConversionGoal" in found["models"]
        schema = (
            await mcp.call_tool(
                "get_operation_schema",
                {"service": "campaign", "operation_name": "get_offline_conversion_reports"},
            )
        ).data
        assert schema["documented_required"] == ["StartDateUtc", "EndDateUtc"]


def test_utf8_chunks_preserve_multibyte_boundaries_and_exact_bytes(tmp_path):
    store = Artifacts(tmp_path)
    text = "é💡日本語\n" * 100
    name = store.save(text.encode(), "json")["artifact"]
    offset, chunks = 0, []
    while offset is not None:
        page = store.read(name, offset, 11, "utf-8")
        chunks.append(page["content"])
        offset = page["next_offset"]
    assert "".join(chunks) == text
    assert base64.b64decode(store.read(name)["content"]) == text.encode()
    with pytest.raises(ApiError):
        store.read(name, 1, 11, "utf-8")
    with pytest.raises(ApiError):
        store.read(name, 0, 1, "utf-8")


async def test_json_projection_pages_preserve_full_snapshot_and_native_nulls(client, tmp_path):
    store = Artifacts(tmp_path)
    rows = [
        {
            "Id": str(9007199254740993 + i),
            "Name": "夏" * 500,
            "Performance": {"Spend": "4,716.21", "Ctr": "5.47%", "Missing": "--"},
            "Future": [None, {"precise": "0.123456789012345678901"}],
        }
        for i in range(120)
    ]
    original = {"data": {"Campaigns": rows}, "PartialErrors": [None], "future": {"x/y~z": None}}
    name = store.save(json.dumps(original).encode(), "json")["artifact"]
    query = {
        "fields": ["Id", "Performance.Spend"],
        "page_size": 31,
        "numeric_fields": ["Performance.Spend", "Performance.Ctr", "Performance.Missing"],
    }
    async with Client(create_server(client.settings, client)) as mcp:
        root = (await mcp.call_tool("read_json_artifact", {"artifact": name})).data
        assert root["kind"] == "object_members"
        assert any(r["pointer"] == "/data" for r in root["Entities"])
        arguments = {"artifact": name, "pointer": "/data/Campaigns", "query": query}
        ids = []
        while True:
            page = (await mcp.call_tool("read_json_artifact", arguments)).data
            ids.extend(r["Id"] for r in page["Entities"])
            assert page["numeric_values"][0] == {
                "Performance.Spend": 4716.21,
                "Performance.Ctr": 5.47,
                "Performance.Missing": None,
            }
            assert set(page["Entities"][0]) == {"Id", "Performance"}
            assert store.json_value(page["snapshot_artifact"], "/rows") == rows
            if not page["NextPageToken"]:
                break
            arguments["query"] = {**query, "next_page_token": page["NextPageToken"]}
            invalid = await mcp.call_tool(
                "read_json_artifact",
                {**arguments, "query": {**arguments["query"], "fields": ["Name"]}},
                raise_on_error=False,
            )
            assert invalid.is_error
        assert ids == [r["Id"] for r in rows]
        assert store.json_value(name) == original
        null = (
            await mcp.call_tool(
                "read_json_artifact", {"artifact": name, "pointer": "/future/x~1y~0z"}
            )
        ).data
        assert null["value"] is None
        nulls = store.save(b'[null,{"Id":"1"},null]', "json")["artifact"]
        page = (await mcp.call_tool("read_json_artifact", {"artifact": nulls})).data
        assert page["Entities"] == [None, {"Id": "1"}, None]


async def test_csv_projection_filter_numeric_view_does_not_change_source(client, tmp_path):
    store = Artifacts(tmp_path)
    raw = b'Id,Spend,Ctr,Name\n001,"4,716.21",5.47%,keep\n002,--,<10%,omit\n'
    name = store.save(raw, "csv")["artifact"]
    async with Client(create_server(client.settings, client)) as mcp:
        got = (
            await mcp.call_tool(
                "read_csv_rows",
                {
                    "artifact": name,
                    "query": {
                        "fields": ["Id", "Spend"],
                        "numeric_fields": ["Spend", "Ctr"],
                        "filters": {"Spend": {"IsGreaterThan": 4000}},
                    },
                },
            )
        ).data
        assert got["Entities"] == [{"Id": "001", "Spend": "4,716.21"}]
        assert got["numeric_values"] == [{"Spend": 4716.21, "Ctr": 5.47}]
        assert store.path(name).read_bytes() == raw
        invalid = await mcp.call_tool(
            "read_csv_rows",
            {"artifact": name, "query": {"fields": ["Spned"]}},
            raise_on_error=False,
        )
        assert invalid.is_error and "Spned" in invalid.content[0].text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5.47%", 5.47),
        ("4,716.21", 4716.21),
        ("--", None),
        ("<10%", None),
        (None, None),
        ("", None),
        ("1,23", None),
        ("NaN", None),
        (True, None),
        ("9007199254740993", "9007199254740993"),
        ("0.123456789012345678901", "0.123456789012345678901"),
        ("-2.50", -2.5),
    ],
)
def test_numeric_precision_and_missingness(value, expected):
    assert numeric(value) == expected


def test_advisory_validation_catches_typos_without_mutating_future_or_derived_fields(client):
    payload = {
        "AccountId": "2",
        "Campaigns": [
            {
                "Id": "5",
                "DailyBudgett": 3,
                "DailyBudget": "wrong",
                "AudienceAdsBidAdjustmnt": -100,
                "Future": [None],
            }
        ],
    }
    before = json.dumps(payload)
    preview = client.preview("campaign", "update_campaigns", payload, SCOPE)
    issues = {i["path"]: i for i in preview["warnings"]}
    assert "DailyBudget" in issues["Campaigns[0].DailyBudgett"]["suggestions"]
    assert (
        "AudienceAdsBidAdjustment" in issues["Campaigns[0].AudienceAdsBidAdjustmnt"]["suggestions"]
    )
    assert "Campaigns[0].DailyBudget" in issues
    assert json.dumps(payload) == before
    assert preview["request"] == payload
    op = operation("campaign", "add_ads")
    issues = request_issues(
        "campaign",
        op.model,
        {
            "AdGroupId": 3,
            "Ads": [
                {
                    "Type": "ResponsiveSearch",
                    "Headlines": [{"Asset": {"Type": "TextAsset", "Text": "ok"}}],
                },
                None,
            ],
        },
    )
    assert issues == []
    request = {"CampaignId": "4", "AdGroups": [{"Id": "5", "AudienceAdsBidAdjustment": -100}]}
    missing_flag = client.preview("campaign", "update_ad_groups", request, SCOPE)
    assert any(w["path"] == "UpdateAudienceAdsBidAdjustment" for w in missing_flag["warnings"])
    enabled = client.preview(
        "campaign", "update_ad_groups", {**request, "UpdateAudienceAdsBidAdjustment": True}, SCOPE
    )
    assert all(w["path"] != "UpdateAudienceAdsBidAdjustment" for w in enabled["warnings"])


async def test_documented_invalid_requests_fail_before_authentication(client, monkeypatch):
    headers = AsyncMock(side_effect=AssertionError("Must not authenticate"))
    monkeypatch.setattr(client, "headers", headers)
    for name, payload, message in [
        ("get_negative_keywords_by_entity_ids", {"EntityIds": ["1", "2"]}, "at most one"),
        ("get_offline_conversion_reports", {}, "StartDateUtc"),
    ]:
        with pytest.raises(ApiError, match=message):
            await client.execute("campaign", name, payload, SCOPE)
    headers.assert_not_called()
    preview = client.preview(
        "campaign",
        "get_conversion_goals_by_ids",
        {"ConversionGoalTypes": "Url, InStoreTransaction"},
        SCOPE,
    )
    assert "InStoreTransaction" in preview["hints"][0]
    assert preview["request"]["ConversionGoalTypes"] == "Url, InStoreTransaction"


@pytest.mark.parametrize("observed,state", [(0, "mismatch"), (-100, "matched"), (None, "mismatch")])
async def test_write_readback_flags_noops_without_retrying(
    client, tmp_path, monkeypatch, observed, state
):
    calls = []

    async def execute(service, name, body, scope):
        calls.append((name, body))
        if name == "update_campaigns":
            return {"status": "returned", "sent": True, "data": {"PartialErrors": []}}
        return {
            "status": "returned",
            "data": {
                "Campaigns": [
                    {
                        "Id": "5",
                        "AudienceAdsBidAdjustment": observed,
                        "Name": "kept",
                        "Future": [None],
                    }
                ]
            },
        }

    monkeypatch.setattr(client, "execute", execute)
    out = await mutations.execute(
        client,
        Artifacts(tmp_path),
        "campaign",
        "update_campaigns",
        {"AccountId": "2", "Campaigns": [{"Id": "5", "AudienceAdsBidAdjustment": -100}]},
        SCOPE,
        dry_run=False,
    )
    assert out["verification"]["status"] == state
    assert [n for n, _ in calls] == ["update_campaigns", "get_campaigns_by_ids"]
    assert out["data"] == {"PartialErrors": []}
    assert out["status"] == ("verification_mismatch" if state == "mismatch" else "returned")
    raw = Artifacts(tmp_path).json_value(out["verification"]["read_back"]["artifact"])
    assert raw["Campaigns"][0]["Future"] == [None]


async def test_failed_readback_preserves_completed_mutation_and_partial_errors(
    client, tmp_path, monkeypatch
):
    execute = AsyncMock(
        side_effect=[
            {"sent": True, "status": "partial_failure", "data": {"PartialErrors": [{"Index": 1}]}},
            ApiError("read unavailable"),
        ]
    )
    monkeypatch.setattr(client, "execute", execute)
    out = await mutations.execute(
        client,
        Artifacts(tmp_path),
        "campaign",
        "update_ad_groups",
        {"CampaignId": "4", "AdGroups": [{"Id": "5", "Status": "Paused"}]},
        SCOPE,
        dry_run=False,
    )
    assert out["sent"] and out["status"] == "partial_failure"
    assert out["data"]["PartialErrors"] == [{"Index": 1}]
    assert out["verification"]["status"] == "unverified"
    assert execute.await_count == 2


@pytest.mark.parametrize(
    "name,collection,id_key,parent",
    [
        ("set_negative_sites_to_campaigns", "CampaignNegativeSites", "CampaignId", "AccountId"),
        ("set_negative_sites_to_ad_groups", "AdGroupNegativeSites", "AdGroupId", "CampaignId"),
    ],
)
async def test_replacement_preflight_removal_guard_and_empty_lists(
    client, tmp_path, monkeypatch, name, collection, id_key, parent
):
    existing = ["old.example", "keep.example"]
    calls = []

    async def execute(service, operation, body, scope):
        nonlocal existing
        calls.append(operation)
        if operation == name:
            existing = body[collection][0]["NegativeSites"]
            return {"sent": True, "status": "returned", "data": {"PartialErrors": []}}
        return {
            "status": "returned",
            "data": {collection: [{id_key: "5", "NegativeSites": existing}]},
        }

    monkeypatch.setattr(client, "execute", execute)
    body = {
        parent: "2",
        collection: [{id_key: "5", "NegativeSites": ["keep.example", "new.example"]}],
    }
    store = Artifacts(tmp_path)
    preview = await mutations.execute(client, store, "campaign", name, body, SCOPE)
    assert not calls and "replaces" in preview["hints"][0]
    preview = await mutations.execute(client, store, "campaign", name, body, SCOPE, preflight=True)
    assert preview["replacement_diff"][0]["remove"] == ["old.example"]
    with pytest.raises(ApiError, match="allow_remove"):
        await mutations.execute(client, store, "campaign", name, body, SCOPE, dry_run=False)
    assert name not in calls
    out = await mutations.execute(
        client, store, "campaign", name, body, SCOPE, dry_run=False, allow_remove=True
    )
    assert out["verification"]["status"] == "matched"
    assert calls.count(name) == 1
    existing.clear()
    body[collection][0]["NegativeSites"] = []
    with pytest.raises(ApiError, match="allow_remove"):
        await mutations.execute(client, store, "campaign", name, body, SCOPE, dry_run=False)


async def test_signed_url_download_uses_original_but_never_echoes_credentials(
    client, tmp_path, monkeypatch
):
    original = "https://reports.blob.core.windows.net/data.csv?sv=1&sig=private%2Bsignature&se=soon"
    monkeypatch.setattr(
        client,
        "execute",
        AsyncMock(
            return_value={
                "data": {"RequestStatus": "Completed", "ResultFileUrl": original, "Future": [None]}
            }
        ),
    )
    seen = []

    def download(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"Id,Spend\n1,2\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as http:
        monkeypatch.setattr(client, "http", http)
        out = await poll_bulk(client, Artifacts(tmp_path), SCOPE, "job")
    assert seen == [original]
    assert "private%2Bsignature" not in json.dumps(out)
    assert "sv=1" in out["provider_response"]["ResultFileUrl"]
    assert out["provider_response"]["Future"] == [None]
    error = ApiError("failed", provider_response={"url": original})
    assert "private" not in str(error)
    bounded = Artifacts(tmp_path).bounded({"url": original, "text": "x" * INLINE_BYTES})
    assert (
        "private"
        not in Artifacts(tmp_path).path(bounded["complete_result"]["artifact"]).read_text()
    )
    assert "secret" not in safe_output(
        "https://reports.blob.core.windows.net/file?%73IG=secret&ok=1#access_token=secret"
    )


def test_retention_warns_without_clipping_and_does_not_guess_unknown_types():
    request = {
        "Type": "CampaignPerformanceReportRequest",
        "Aggregation": "Monthly",
        "Time": {
            "CustomDateRangeStart": {"Year": 2022, "Month": 11, "Day": 1},
            "CustomDateRangeEnd": {"Year": 2026, "Month": 10, "Day": 12},
            "ReportTimeZone": "PacificTimeUSCanadaTijuana",
        },
    }
    context = reporting.request_context(request, today=date(2026, 10, 13))
    assert context["retention"]["approximate_earliest_date"] == "2023-10-13"
    assert context["warnings"] and context["requested_time"] == request["Time"]
    assert context["effective_range_verified"] is False
    unknown = reporting.request_context({"Type": "FutureReportRequest"})
    assert unknown["retention"]["known"] is False
    leap = reporting.request_context(
        {"Type": "CampaignPerformanceReportRequest"}, today=date(2024, 2, 29)
    )
    assert leap["retention"]["approximate_earliest_date"] == "2021-02-28"


async def test_report_context_survives_restart_and_coverage_is_observed_not_guaranteed(
    client, tmp_path, monkeypatch
):
    execute = AsyncMock(
        side_effect=[
            {"data": {"ReportRequestId": "job"}},
            {
                "data": {
                    "ReportRequestStatus": {
                        "Status": "Success",
                        "ReportDownloadUrl": "https://reports.blob.core.windows.net/r.csv",
                    }
                }
            },
        ]
    )
    monkeypatch.setattr(client, "execute", execute)
    request = {
        "Type": "CampaignPerformanceReportRequest",
        "Aggregation": "Monthly",
        "Time": {"CustomDateRangeStart": {"Year": 2022, "Month": 1, "Day": 1}},
    }
    await submit_report(client, SCOPE, request, Artifacts(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"TimePeriod,Spend\n2023-10,5.50\n2023-11,6.50\n")
        )
    ) as http:
        monkeypatch.setattr(client, "http", http)
        got = await poll_report(client, Artifacts(tmp_path), SCOPE, "job")
    context = got["report_context"]
    assert context["requested_time"] == request["Time"]
    assert context["effective_range_verified"] is False
    assert context["observed_time_periods"][0]["first_time_period"] == "2023-10"
    assert context["observed_time_periods"][0]["rows_with_time_period"] == 2
    other = reporting.coverage(Artifacts(tmp_path), "sandbox", SCOPE, "job", [])
    assert other["requested_time"] is None


async def test_bulk_poll_reads_campaign_state_and_marks_other_records_unverified(
    client, tmp_path, monkeypatch
):
    from bing_ads_mcp.transfers import upload_bulk

    calls = []

    async def execute(service, name, body, scope):
        calls.append(name)
        if name == "get_bulk_upload_url":
            return {
                "data": {
                    "RequestId": "bulk-job",
                    "UploadUrl": "https://bulk.api.bingads.microsoft.com/upload",
                }
            }
        if name == "get_bulk_upload_status":
            return {
                "data": {
                    "RequestStatus": "Completed",
                    "ResultFileUrl": "https://reports.blob.core.windows.net/r.csv?sig=bulk-secret",
                }
            }
        assert name == "get_campaigns_by_ids"
        return {
            "status": "returned",
            "data": {"Campaigns": [{"Id": "5", "AudienceAdsBidAdjustment": 0, "DailyBudget": 2.5}]},
        }

    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(
        client, "headers", AsyncMock(return_value={"Content-Type": "application/json"})
    )
    uploads = []

    def respond(request):
        if request.method == "POST":
            uploads.append(request.content)
            return httpx.Response(200)
        return httpx.Response(200, content=b"Type,Id,Bid Adjustment,Error\nCampaign,5,0,\n")

    csv_text = (
        "Type,Id,Parent Id,Name,Bid Adjustment,Budget\nFormat Version,,,6.0,,\n"
        "Campaign,5,2,,-100,2.50\nKeyword,-1,6,new,,\n"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(client, "http", http)
        await upload_bulk(client, Artifacts(tmp_path), SCOPE, csv_text, dry_run=False)
        result = await poll_bulk(client, Artifacts(tmp_path), SCOPE, "bulk-job", upload=True)
    assert len(uploads) == 1
    assert calls == ["get_bulk_upload_url", "get_bulk_upload_status", "get_campaigns_by_ids"]
    assert result["status"] == "Completed"
    assert result["verification"]["status"] == "mismatch"
    checks = result["verification"]["campaign_checks"][0]["entities"][0]["fields"]
    assert {c["field"]: c["status"] for c in checks} == {
        "AudienceAdsBidAdjustment": "mismatch",
        "DailyBudget": "matched",
    }
    assert result["verification"]["unchecked"]
    assert "bulk-secret" not in json.dumps(result)


async def test_verify_opt_out_sends_once_and_unknown_fields_remain_unverified(
    client, tmp_path, monkeypatch
):
    execute = AsyncMock(return_value={"status": "returned", "sent": True, "data": {}})
    monkeypatch.setattr(client, "execute", execute)
    body = {"AccountId": "2", "Campaigns": [{"Id": "5", "DailyBudgett": 3}]}
    result = await mutations.execute(
        client,
        Artifacts(tmp_path),
        "campaign",
        "update_campaigns",
        body,
        SCOPE,
        dry_run=False,
        verify=False,
    )
    assert result["verification"]["status"] == "disabled" and execute.await_count == 1
    execute.side_effect = [
        {"status": "returned", "sent": True, "data": {}},
        {"status": "returned", "data": {"Campaigns": [{"Id": "5", "DailyBudget": 2}]}},
    ]
    result = await mutations.execute(
        client, Artifacts(tmp_path), "campaign", "update_campaigns", body, SCOPE, dry_run=False
    )
    assert result["verification"]["status"] == "unverified"
    assert result["verification"]["entities"][0]["fields"][0]["status"] == "not_returned"


async def test_incomplete_preflight_never_sends_a_replacement_even_with_allow_remove(
    client, tmp_path, monkeypatch
):
    execute = AsyncMock(
        return_value={"status": "returned", "data": {"CampaignNegativeSites": [None]}}
    )
    monkeypatch.setattr(client, "execute", execute)
    with pytest.raises(ApiError, match="not returned"):
        await mutations.execute(
            client,
            Artifacts(tmp_path),
            "campaign",
            "set_negative_sites_to_campaigns",
            {"AccountId": "2", "CampaignNegativeSites": [{"CampaignId": "5", "NegativeSites": []}]},
            SCOPE,
            dry_run=False,
            allow_remove=True,
        )
    assert execute.await_count == 1
    assert execute.await_args.args[1] == "get_negative_sites_by_campaign_ids"


def test_empty_site_list_matches_microsoft_null_list():
    checks = mutations.compare({"NegativeSites": []}, {"NegativeSites": None})
    assert checks[0]["status"] == "matched"


async def test_oversized_json_scalar_has_finite_utf8_retrieval(client, tmp_path):
    store = Artifacts(tmp_path)
    text = "é" * INLINE_BYTES
    artifact = store.save(json.dumps({"message": text}).encode(), "json")["artifact"]
    async with Client(create_server(client.settings, client)) as mcp:
        got = (
            await mcp.call_tool("read_json_artifact", {"artifact": artifact, "pointer": "/message"})
        ).data
        offset, chunks = 0, []
        while offset is not None:
            page = (
                await mcp.call_tool(
                    "read_artifact",
                    {
                        "artifact": got["oversized_value"]["artifact"],
                        "offset": offset,
                        "encoding": "utf-8",
                    },
                )
            ).data
            chunks.append(page["content"])
            offset = page["next_offset"]
    assert json.loads("".join(chunks)) == text


async def test_native_signed_file_delivery_survives_redaction_without_resubmitting(
    client, tmp_path, monkeypatch
):
    output = {
        "status": "returned",
        "data": {
            "OfflineConversionReports": [
                {
                    "FileUrl": "https://reports.blob.core.windows.net/report.csv?sig=hidden-signature",
                    "SuccessCount": 2,
                    "Future": [None],
                }
            ]
        },
    }
    execute = AsyncMock(return_value=output)
    monkeypatch.setattr(client, "execute", execute)
    urls = []

    def respond(request):
        urls.append(str(request.url))
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"Id,Status\n1,Success\n2,Success\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(client, "http", http)
        async with Client(create_server(client.settings, client)) as mcp:
            result = await mcp.call_tool(
                "read_api",
                {
                    "service": "campaign",
                    "operation_name": "get_offline_conversion_reports",
                    "scope": SCOPE.model_dump(),
                    "request": {
                        "StartDateUtc": "2026-10-01T00:00:00Z",
                        "EndDateUtc": "2026-10-02T00:00:00Z",
                    },
                    "download_files": True,
                },
            )
    assert execute.await_count == 1 and len(urls) == 1
    assert "hidden-signature" in urls[0] and "hidden-signature" not in result.content[0].text
    assert result.data["data"]["OfflineConversionReports"][0]["Future"] == [None]
    file = result.data["downloads"][0]["files"][0]
    assert Artifacts(tmp_path).path(file["artifact"]).read_bytes().endswith(b"2,Success\n")


async def test_bulk_verification_and_large_readback_use_bounded_read_batches(
    client, tmp_path, monkeypatch
):
    calls = []

    async def execute(service, name, body, scope):
        assert name == "get_campaigns_by_ids"
        calls.append(body)
        return {
            "status": "returned",
            "data": {
                "Campaigns": [{"Id": value, "Status": "Paused"} for value in body["CampaignIds"]],
                "Future": [None],
            },
        }

    monkeypatch.setattr(client, "execute", execute)
    out = await mutations.verify_update(
        client,
        Artifacts(tmp_path),
        "update_campaigns",
        {
            "AccountId": "2",
            "Campaigns": [{"Id": str(i), "Status": "Paused"} for i in range(1, 206)],
        },
        SCOPE,
    )
    assert out["status"] == "matched"
    assert [len(call["CampaignIds"]) for call in calls] == [100, 100, 5]
    assert len(out["entities"]) == 205
    data = Artifacts(tmp_path).json_value(out["read_back"]["artifact"])
    assert len(data["pages"]) == 3 and all(p["Future"] == [None] for p in data["pages"])


def test_wrong_collection_type_still_returns_advisory_diagnostics(client):
    preview = client.preview("campaign", "update_ad_groups", {"AdGroups": 12}, SCOPE)
    assert preview["warnings"] == [{"path": "AdGroups", "issue": "Expected an array."}]
    assert preview["request"] == {"AdGroups": 12}
    assert "password" not in safe_output("HTTPS://name:password@example.com/a?SIG=campaign")
