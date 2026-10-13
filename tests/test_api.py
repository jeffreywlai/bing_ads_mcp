import inspect
import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from urllib3 import HTTPResponse
from urllib3.exceptions import ReadTimeoutError

from bing_ads_mcp.auth import Settings
from bing_ads_mcp.catalog import SERVICES, describe_model, models, operation, operations
from bing_ads_mcp.client import AdsClient, ApiError
from bing_ads_mcp.models import Scope
from bing_ads_mcp.policy import JOBS, READS
from bing_ads_mcp.server import create_server

SCOPE = Scope(customer_id="123", account_id="456")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    (
        "write_api",
        {
            "service": "campaign",
            "operation_name": "update_campaigns",
            "request": {"AccountId": "456", "Campaigns": [{"Id": "789", "Status": "Paused"}]},
        },
    ),
    (
        "upload_bulk_csv",
        {
            "csv_content": (
                "Type,Status,Id,Parent Id,Name\nFormat Version,,,,6.0\nCampaign,Paused,789,456,\n"
            )
        },
    ),
    ("content_api", {"operation_name": "insert_product", "merchant_id": "123", "request": {}}),
    ("submit_uet_events", {"tag_id": "123", "request": {"data": [{"eventId": "test-event"}]}}),
]


def test_catalog_covers_every_public_sdk_operation():
    discovered = operations()
    assert Counter(o.service for o in discovered.values()) == {
        "campaign": 194,
        "ad_insight": 33,
        "reporting": 2,
        "bulk": 6,
        "customer": 39,
        "billing": 16,
    }
    for service, (cls, _, _) in SERVICES.items():
        expected = {
            n
            for n, f in inspect.getmembers(cls, inspect.isfunction)
            if not n.startswith("_")
            and not n.endswith(("_with_http_info", "_without_preload_content"))
        }
        assert {n for s, n in discovered if s == service} == expected
    assert READS | JOBS <= {f"{s}.{n}" for s, n in discovered}
    for s, n in discovered:
        op = operation(s, n)
        schema = describe_model(s, op.model.__name__)
        assert isinstance(schema, dict)
        assert op.path.startswith("/")
        assert "v13" in op.path


def test_write_classification_includes_deceptive_get_and_insight_actions():
    for service, name in [
        ("campaign", "get_uet_tag_auth_key"),
        ("ad_insight", "apply_recommendations"),
        ("ad_insight", "dismiss_recommendations"),
        ("ad_insight", "tag_recommendations"),
        ("ad_insight", "set_auto_apply_opt_in_status"),
        ("bulk", "upload_entity_records"),
        ("billing", "redeem_coupon"),
        ("customer", "update_user_roles"),
    ]:
        assert operation(service, name).effect == "write"
    assert operation("reporting", "submit_generate_report").effect == "job"


def test_polymorphic_schema_and_flag_wire_encoding():
    schema = describe_model("campaign", "Ad")
    assert "ResponsiveSearchAd" in schema["derived_models"]
    assert "Headlines" in describe_model("campaign", "ResponsiveSearchAd")["properties"]
    assert (
        "CampaignPerformanceReportRequest"
        in describe_model("reporting", "ReportRequest")["derived_models"]
    )
    flag = describe_model("campaign", "CampaignType")
    assert flag["type"] == "string"
    assert "PerformanceMax" in flag["values"]
    assert len(models("reporting")) > 100


@pytest.fixture
async def api(tmp_path):
    settings = Settings(
        client_id="app",
        developer_token="developer-secret",
        refresh_token="refresh",
        token_file=tmp_path / "token.json",
        export_dir=tmp_path / "exports",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"access_token": "access-secret", "refresh_token": "rotated"}
            )
        )
    ) as http:
        client = AdsClient(settings, http)
        yield client
        client.close()


def transport(api, monkeypatch, service, body, status=200):
    sent = []

    def request(method, url, **kwargs):
        sent.append((method, url, kwargs))
        assert kwargs["timeout"].connect_timeout == 10
        return HTTPResponse(
            body=json.dumps(body).encode(),
            status=status,
            headers={"TrackingId": "trace", "Content-Type": "application/json"},
        )

    pool = api.clients[service].rest_client.pool_manager
    assert pool.connection_pool_kw["retries"].total == 0
    monkeypatch.setattr(pool, "request", request)
    return sent


async def test_native_json_retains_derived_fields_null_slots_and_future_fields(api, monkeypatch):
    response = {
        "AdIds": ["9007199254740993", None],
        "PartialErrors": [{"Index": 1, "Code": 123, "Message": "Invalid headline"}],
        "FutureResponse": {"x": True},
    }
    sent = transport(api, monkeypatch, "campaign", response)
    payload = {
        "AdGroupId": "88",
        "Ads": [
            {
                "Type": "ResponsiveSearch",
                "Headlines": [{"Asset": {"Type": "TextAsset", "Text": "New headline"}}],
                "FutureField": None,
            },
            None,
        ],
    }
    got = await api.execute("campaign", "add_ads", payload, SCOPE)
    assert got["data"] == response
    assert got["status"] == "partial_failure"
    assert len(sent) == 1
    assert json.loads(sent[0][2]["body"]) == payload
    assert sent[0][2]["headers"]["CustomerAccountId"] == "456"
    assert sent[0][2]["headers"]["CustomerId"] == "123"
    assert sent[0][2]["headers"]["Authorization"] == "Bearer access-secret"


async def test_dry_run_never_authenticates_and_scope_mismatch_fails_first(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("network"))
    ) as h:
        api = AdsClient(Settings(read_only=True), h)
        preview = await api.execute(
            "campaign", "update_campaigns", {"AccountId": "456"}, SCOPE, dry_run=True
        )
        assert preview["sent"] is False
        with pytest.raises(ApiError, match="match scope"):
            await api.execute("campaign", "update_campaigns", {"AccountId": "789"}, SCOPE)
        with pytest.raises(ApiError, match="disabled"):
            await api.execute("campaign", "update_campaigns", {}, SCOPE)
        api.close()


async def test_no_retry_on_uncertain_write(api, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise ReadTimeoutError(None, None, "sensitive transport diagnostic")

    monkeypatch.setattr(api.clients["campaign"].rest_client.pool_manager, "request", timeout)
    with pytest.raises(ApiError) as error:
        await api.execute("campaign", "update_campaigns", {}, SCOPE)
    assert len(calls) == 1
    assert error.value.details["mutation_outcome_unknown"] is True
    assert "sensitive" not in str(error.value)


async def test_error_preserves_provider_message_but_redacts_credentials(api, monkeypatch):
    sent = transport(
        api,
        monkeypatch,
        "campaign",
        {"Errors": [{"Code": 106, "Message": "Permission denied; access-secret developer-secret"}]},
        403,
    )
    with pytest.raises(ApiError) as error:
        await api.execute("campaign", "get_campaigns_by_account_id", {}, SCOPE)
    assert len(sent) == 1
    assert "Permission denied" in str(error.value)
    assert "access-secret" not in str(error.value)
    assert "developer-secret" not in str(error.value)
    assert error.value.details["tracking_id"] == "trace"


async def test_null_error_slots_are_preserved_without_reporting_a_failure(api, monkeypatch):
    response = {"CampaignIds": ["42"], "PartialErrors": [None], "Errors": [None]}
    transport(api, monkeypatch, "campaign", response)
    output = await api.execute("campaign", "add_campaigns", {}, SCOPE)
    assert output["status"] == "returned"
    assert output["data"] == response


async def test_real_fastmcp_client_discovery_validation_and_write_guard(api):
    async with Client(create_server(api.settings, api)) as mcp:
        names = {t.name for t in await mcp.list_tools()}
        assert {
            "get_accounts",
            "get_campaigns",
            "get_ad_groups",
            "get_ads",
            "get_keywords",
            "get_asset_groups",
            "get_audience_associations",
            "write_api",
            "unlock_mutation_tools",
            "lock_mutation_tools",
        } <= names
        assert len(names) == 24
        preview = await mcp.call_tool(
            "write_api",
            {
                "service": "campaign",
                "operation_name": "update_campaigns",
                "request": {},
                "scope": SCOPE.model_dump(),
            },
        )
        assert preview.data["dry_run"] is True
        rejected = await mcp.call_tool(
            "read_api",
            {
                "service": "campaign",
                "operation_name": "get_uet_tag_auth_key",
                "request": {},
                "scope": SCOPE.model_dump(),
            },
            raise_on_error=False,
        )
        assert rejected.is_error
        invalid = await mcp.call_tool(
            "get_campaigns",
            {"scope": {"customer_id": "123", "account_id": 0}},
            raise_on_error=False,
        )
        assert invalid.is_error
        unknown = await mcp.call_tool(
            "get_model_schema",
            {"service": "reporting", "model": "Misspelled"},
            raise_on_error=False,
        )
        assert unknown.is_error
        assert "Use list_models" in unknown.content[0].text


@pytest.mark.parametrize("tool,arguments", MUTATIONS)
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_every_mutation_requires_unlock_and_stops_after_relock(
    api, monkeypatch, tool, arguments, mode
):
    token = AsyncMock(return_value="access-secret")
    monkeypatch.setattr(api.oauth, "access_token", token)
    campaign_calls = transport(api, monkeypatch, "campaign", {"UetTagAuthKey": "uet-secret"})
    bulk_calls = transport(
        api,
        monkeypatch,
        "bulk",
        {"UploadUrl": "https://bulk.api.bingads.microsoft.com/upload", "RequestId": "job"},
    )
    http_calls = []

    def respond(request):
        http_calls.append(request)
        return httpx.Response(200, json={"eventsReceived": 1})

    arguments = {"scope": SCOPE.model_dump(), **arguments}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(api, "http", http)
        async with Client(create_server(api.settings, api), mode=mode) as mcp:
            preview = await mcp.call_tool(tool, arguments)
            assert preview.data["sent"] is False
            for session in ({}, {"session_id": "unminted-session"}):
                denied = await mcp.call_tool(
                    tool, {**arguments, "dry_run": False, **session}, raise_on_error=False
                )
                assert denied.is_error
                assert "writes are locked" in denied.content[0].text
            token.assert_not_called()
            assert not campaign_calls and not bulk_calls and not http_calls

            unlocked = await mcp.call_tool("unlock_mutation_tools", {})
            assert unlocked.data["mutation_tools_unlocked"] is True
            session_id = unlocked.data["session_id"]
            request = {**arguments, "dry_run": False, "session_id": session_id}
            executed = await mcp.call_tool(tool, request)
            assert executed.data["sent"] is True
            expected_calls = 2 if tool in {"upload_bulk_csv", "submit_uet_events"} else 1
            assert len(campaign_calls) + len(bulk_calls) + len(http_calls) == expected_calls
            token_calls = token.call_count

            for _ in range(2):
                locked = await mcp.call_tool("lock_mutation_tools", {"session_id": session_id})
                assert locked.data == {"mutation_tools_unlocked": False}
            denied = await mcp.call_tool(tool, request, raise_on_error=False)
            assert denied.is_error
            assert "writes are locked" in denied.content[0].text
            assert token.call_count == token_calls
            assert len(campaign_calls) + len(bulk_calls) + len(http_calls) == expected_calls


@pytest.mark.parametrize("tool,arguments", MUTATIONS)
async def test_read_only_cannot_be_overridden_by_unlock(api, monkeypatch, tool, arguments):
    from dataclasses import replace

    settings = replace(api.settings, read_only=True)
    monkeypatch.setattr(api, "settings", settings)
    token = AsyncMock(side_effect=AssertionError("Blocked writes must not authenticate"))
    monkeypatch.setattr(api.oauth, "access_token", token)
    async with Client(create_server(settings, api)) as mcp:
        denied = await mcp.call_tool("unlock_mutation_tools", {}, raise_on_error=False)
        assert denied.is_error
        assert "BING_ADS_READ_ONLY" in denied.content[0].text
        arguments = {"scope": SCOPE.model_dump(), **arguments}
        preview = await mcp.call_tool(tool, arguments)
        assert preview.data["sent"] is False
        denied = await mcp.call_tool(
            tool,
            {**arguments, "dry_run": False, "session_id": "cannot-override-read-only"},
            raise_on_error=False,
        )
        assert denied.is_error
        assert "BING_ADS_READ_ONLY" in denied.content[0].text
        token.assert_not_called()


async def test_unlock_is_explicit_independent_and_does_not_survive_server_restart(api, monkeypatch):
    sent = transport(api, monkeypatch, "campaign", {})
    request = {"scope": SCOPE.model_dump(), **MUTATIONS[0][1], "dry_run": False}
    server = create_server(api.settings, api)
    async with Client(server) as first, Client(server) as second:
        one = (await first.call_tool("unlock_mutation_tools", {})).data["session_id"]
        denied = await second.call_tool("write_api", request, raise_on_error=False)
        assert denied.is_error and not sent
        two = (await second.call_tool("unlock_mutation_tools", {})).data["session_id"]
        assert one != two
        await first.call_tool("lock_mutation_tools", {"session_id": one})
        denied = await first.call_tool(
            "write_api", {**request, "session_id": one}, raise_on_error=False
        )
        assert denied.is_error and not sent
        executed = await second.call_tool("write_api", {**request, "session_id": two})
        assert executed.data["sent"] is True and len(sent) == 1
    async with Client(create_server(api.settings, api)) as restarted:
        denied = await restarted.call_tool(
            "write_api", {**request, "session_id": two}, raise_on_error=False
        )
        assert denied.is_error and len(sent) == 1


async def test_reads_and_report_jobs_work_while_locked_and_read_api_cannot_bypass_it(
    api, monkeypatch
):
    campaign_calls = transport(api, monkeypatch, "campaign", {"Campaigns": []})
    report_calls = transport(api, monkeypatch, "reporting", {"ReportRequestId": "report"})
    async with Client(create_server(api.settings, api)) as mcp:
        read = await mcp.call_tool(
            "read_api",
            {
                "service": "campaign",
                "operation_name": "get_campaigns_by_account_id",
                "request": {"AccountId": "456", "CampaignType": "Search"},
                "scope": SCOPE.model_dump(),
            },
        )
        assert read.data["data"] == {"Campaigns": []}
        report = await mcp.call_tool(
            "run_report",
            {
                "scope": SCOPE.model_dump(),
                "request": {"Type": "CampaignPerformanceReportRequest"},
            },
        )
        assert report.data["data"]["ReportRequestId"] == "report"
        for service, name in operations():
            if operation(service, name).effect == "write":
                rejected = await mcp.call_tool(
                    "read_api",
                    {
                        "service": service,
                        "operation_name": name,
                        "request": {},
                        "scope": SCOPE.model_dump(),
                    },
                    raise_on_error=False,
                )
                assert rejected.is_error
                assert "Use write_api" in rejected.content[0].text
        assert len(campaign_calls) == len(report_calls) == 1


@pytest.mark.parametrize("dry_run", [False, True])
async def test_content_reads_work_while_locked(api, monkeypatch, dry_run):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"resources": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(api, "http", http)
        async with Client(create_server(api.settings, api)) as mcp:
            result = await mcp.call_tool(
                "content_api",
                {"operation_name": "list_products", "merchant_id": "123", "dry_run": dry_run},
            )
            assert result.data["data"] == {"resources": []}
            assert len(sent) == 1 and sent[0].method == "GET"


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_mutation_session_survives_separate_stdio_requests(tmp_path, mode):
    script = (
        "from pathlib import Path\n"
        "from bing_ads_mcp.auth import Settings\n"
        "from bing_ads_mcp.server import create_server\n"
        "create_server(Settings(export_dir=Path('exports'))).run(show_banner=False)\n"
    )
    transport = StdioTransport(
        sys.executable,
        ["-c", script],
        env={"PYTHONPATH": str(PROJECT_ROOT)},
        cwd=str(tmp_path),
    )
    request = {"scope": SCOPE.model_dump(), **MUTATIONS[0][1], "dry_run": False}
    async with Client(transport, mode=mode) as mcp:
        denied = await mcp.call_tool("write_api", request, raise_on_error=False)
        assert denied.is_error and "writes are locked" in denied.content[0].text
        session_id = (await mcp.call_tool("unlock_mutation_tools", {})).data["session_id"]
        request["session_id"] = session_id
        # No credentials: reaching this error proves the unlock survived a new request,
        # without allowing this protocol test to contact Microsoft.
        permitted = await mcp.call_tool("write_api", request, raise_on_error=False)
        assert permitted.is_error and "Set BING_ADS_DEVELOPER_TOKEN" in permitted.content[0].text
        await mcp.call_tool("lock_mutation_tools", {"session_id": session_id})
        denied = await mcp.call_tool("write_api", request, raise_on_error=False)
        assert denied.is_error and "writes are locked" in denied.content[0].text


@pytest.mark.parametrize(
    "service", ["campaign", "ad_insight", "customer", "billing", "reporting", "bulk"]
)
def test_catalog_includes_independently_documented_public_operations(service):
    reference = json.loads(Path(__file__).with_name("public_operations.json").read_text())
    documented = reference["services"][service]["operations"]
    exposed = {
        name.replace("_", "").lower() for (family, name) in operations() if family == service
    }
    missing = [name for name in documented if name.lower() not in exposed]
    assert not missing, f"Public API operations missing from {service}: {missing}"
