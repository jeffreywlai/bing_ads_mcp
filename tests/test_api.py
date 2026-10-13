import inspect
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from urllib3 import HTTPResponse
from urllib3.exceptions import ReadTimeoutError

from bing_ads_mcp.auth import Settings
from bing_ads_mcp.catalog import SERVICES, describe_model, models, operation, operations
from bing_ads_mcp.client import AdsClient, ApiError
from bing_ads_mcp.models import Scope
from bing_ads_mcp.policy import JOBS, READS
from bing_ads_mcp.server import create_server

SCOPE = Scope(customer_id="123", account_id="456")


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
        } <= names
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
