import base64
import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from bing_ads_mcp import content, entities
from bing_ads_mcp.artifacts import INLINE_BYTES, Artifacts
from bing_ads_mcp.auth import Settings
from bing_ads_mcp.catalog import models
from bing_ads_mcp.client import ApiError
from bing_ads_mcp.entities import filter_sort, get_entities
from bing_ads_mcp.models import EntityQuery, Scope
from bing_ads_mcp.transfers import csv_rows, download, poll_report, upload_bulk

SCOPE = Scope(customer_id="1", account_id="2")


def archive_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    return buf.getvalue()


def test_complete_artifacts_chunking_and_private_permissions(tmp_path):
    store = Artifacts(tmp_path)
    original = {"rows": [{"text": "é" * 300} for _ in range(200)], "status": "returned"}
    notice = store.bounded(original)
    assert notice["inline_complete"] is False
    name = notice["complete_result"]["artifact"]
    assert store.path(name).stat().st_mode & 0o777 == 0o600
    output, offset = b"", 0
    while offset is not None:
        page = store.read(name, offset, 77)
        output += base64.b64decode(page["content"])
        offset = page["next_offset"]
    assert json.loads(output) == original
    with pytest.raises(ApiError):
        store.read("../../.env")


def test_stable_pagination_never_skips_large_or_filtered_rows(tmp_path):
    store = Artifacts(tmp_path)
    rows = [{"Id": str(i), "x": "x" * (INLINE_BYTES if i == 2 else 500)} for i in range(5)]
    page = store.entity_page(rows, "same-query", 3)
    ids = []
    while True:
        for row in page["Entities"]:
            if "oversized_entity" in row:
                row = json.loads(store.path(row["oversized_entity"]["artifact"]).read_text())
            ids.append(row["Id"])
        token = page["NextPageToken"]
        if not token:
            break
        with pytest.raises(ApiError):
            store.entity_page(None, "wrong-query", 3, token)
        page = store.entity_page(None, "same-query", 3, token)
    assert ids == [str(i) for i in range(5)]


async def test_download_preserves_every_zip_member_and_never_sends_auth(tmp_path):
    files = {
        "../../escape.csv": b'Id,Name\r\n1,"quoted, value"\r\n',
        "second.csv": b"Id,Name\n2,second\n",
        "report.xml": b"<?xml version='1.0'?><report><row id='3'/></report>",
    }
    seen = []

    def respond(request):
        seen.append(request)
        assert "Authorization" not in request.headers
        assert "DeveloperToken" not in request.headers
        return httpx.Response(200, content=archive_bytes(files))

    store = Artifacts(tmp_path / "exports")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await download(
            http, store, "https://reports.blob.core.windows.net/file?sig=secret"
        )
    assert result["complete"]
    assert [store.path(f["artifact"]).read_bytes() for f in result["files"]] == list(files.values())
    assert not (tmp_path / "escape.csv").exists()
    assert csv_rows(store, result["files"][0]["artifact"])[0]["Name"] == "quoted, value"
    assert result["files"][2]["artifact"].endswith(".xml")
    with pytest.raises(ApiError, match="CSV/TSV"):
        csv_rows(store, result["files"][2]["artifact"])


async def test_report_pending_resume_and_no_data(tmp_path):
    calls = []
    responses = ["Pending", "Success"]

    async def execute(service, name, body, scope):
        calls.append((name, body))
        return {
            "data": {"ReportRequestStatus": {"Status": responses.pop(0), "ReportDownloadUrl": None}}
        }

    client = SimpleNamespace(execute=execute)
    store = Artifacts(tmp_path)
    assert (await poll_report(client, store, SCOPE, "existing"))["status"] == "Pending"
    assert (await poll_report(client, store, SCOPE, "existing"))["no_data"] is True
    assert calls == [("poll_generate_report", {"ReportRequestId": "existing"})] * 2


async def test_content_native_fields_encoding_partial_errors_and_no_redirects(tmp_path):
    seen = []

    async def headers(*args, **kwargs):
        return {"AuthenticationToken": "secret", "DeveloperToken": "dev"}

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "batchId": 1,
                        "errors": {"errors": [{"reason": "invalid", "message": "bad price"}]},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SimpleNamespace(
            settings=Settings(developer_token="dev"), http=http, headers=headers
        )
        payload = {"entries": [{"batchId": 1, "method": "insert", "product": {"future": [None]}}]}
        result = await content.execute(client, "batch_products", "1", SCOPE, body=payload)
        assert result["sent"] is False
        assert not seen
        result = await content.execute(
            client, "batch_products", "1", SCOPE, body=payload, dry_run=False
        )
        assert result["status"] == "partial_failure"
        assert json.loads(seen[0].content) == payload
        assert "authorization" not in seen[0].headers
        assert len(content.OPERATIONS) == 12


def test_filters_sort_numerically_and_do_not_treat_missing_metrics_as_zero():
    rows = [
        {"Id": "1", "Performance": {"Spend": "20.00"}},
        {"Id": "2", "Performance": {"Spend": "100.00"}},
        {"Id": "3", "Performance": None},
    ]
    query = EntityQuery(
        performance_time={"ReportTimeZone": "PacificTimeUSCanadaTijuana"},
        sort_by="Spend",
        descending=True,
        filters={"Spend": {"IsGreaterThan": 10}},
    )
    assert [r["Id"] for r in filter_sort(rows, query)] == ["2", "1"]
    nonzero = query.model_copy(update={"filters": {"Spend": {"IsNotEqualTo": 0}}})
    assert [r["Id"] for r in filter_sort(rows, nonzero)] == ["2", "1"]
    raw = [{"Spend": "20"}, {"Spend": "100"}]
    assert filter_sort(raw, EntityQuery(sort_by="Spend"), metrics_nested=False) == raw


class EntityClient:
    settings = Settings()

    def __init__(self):
        self.calls = []

    async def execute(self, service, name, body, scope=None, **kwargs):
        self.calls.append((service, name, body, scope))
        mapping = {
            "get_user": {"User": {"Id": "99"}},
            "get_account": {"Account": {"Id": "2", "ParentCustomerId": "1", "Name": "Account"}},
            "search_accounts": {
                "Accounts": [{"Id": "2", "ParentCustomerId": "1", "Name": "Account"}]
            },
            "get_campaigns_by_account_id": {
                "Campaigns": [{"Id": "10", "Name": "Campaign", "CampaignType": "Search"}]
            },
            "get_campaigns_by_ids": {
                "Campaigns": [{"Id": "10", "Name": "Campaign", "CampaignType": "PerformanceMax"}]
            },
            "get_ad_groups_by_campaign_id": {"AdGroups": [{"Id": "20", "Name": "Group"}]},
            "get_ads_by_ad_group_id": {
                "Ads": [
                    {"Id": "30", "Type": "ResponsiveSearch", "Headlines": [{"Text": "Preserved"}]}
                ]
            },
            "get_keywords_by_ad_group_id": {
                "Keywords": [{"Id": "40", "Text": "Keyword", "MatchType": "Exact"}]
            },
            "get_asset_groups_by_campaign_id": {
                "AssetGroups": [{"Id": "50", "SearchThemes": ["theme"]}]
            },
            "get_campaign_criterions_by_ids": {
                "CampaignCriterions": [{"Id": "60", "Criterion": {"AudienceId": "70"}}]
            },
            "get_ad_group_criterions_by_ids": {
                "AdGroupCriterions": [{"Id": "61", "Criterion": {"AudienceId": "70"}}]
            },
            "get_audiences_by_ids": {"Audiences": [{"Id": "70", "Name": "Audience"}]},
        }
        return {"status": "returned", "data": mapping[name]}


@pytest.mark.parametrize(
    "kind",
    [
        "accounts",
        "campaigns",
        "ad_groups",
        "ads",
        "keywords",
        "asset_groups",
        "audience_associations",
    ],
)
async def test_seven_workflows_keep_native_fields_and_reuse_stable_pages(tmp_path, kind):
    client = EntityClient()
    store = Artifacts(tmp_path)
    campaign = "10" if kind == "asset_groups" else None
    page = await get_entities(client, store, kind, SCOPE, EntityQuery(page_size=1), campaign)
    assert page["Entities"]
    if kind == "accounts":
        assert [name for _, name, _, _ in client.calls] == ["get_account"]
    if kind == "ads":
        assert page["Entities"][0]["Headlines"] == [{"Text": "Preserved"}]
        request = next(
            body for _, name, body, _ in client.calls if name == "get_ads_by_ad_group_id"
        )
        assert "Image" not in request["AdTypes"]
        assert "ResponsiveSearch" in request["AdTypes"]
    if kind == "asset_groups":
        assert page["Entities"][0]["SearchThemes"] == ["theme"]
    if kind == "audience_associations":
        assert page["TotalCount"] == 2
        count = len(client.calls)
        next_page = await get_entities(
            client,
            store,
            kind,
            SCOPE,
            EntityQuery(page_size=1, next_page_token=page["NextPageToken"]),
        )
        assert len(client.calls) == count
        assert next_page["Entities"][0]["Level"] == "AdGroup"
        assert next_page["Entities"][0]["AudienceName"] == "Audience"


async def test_bulk_preview_no_auth_and_uncertain_upload_has_resume_id(tmp_path):
    store = Artifacts(tmp_path)

    async def headers(*args, **kwargs):
        return {"AuthenticationToken": "secret", "Content-Type": "application/json"}

    calls = []

    async def execute(*args, **kwargs):
        calls.append(1)
        return {
            "data": {
                "UploadUrl": "https://bulk.api.bingads.microsoft.com/upload",
                "RequestId": "resume",
            }
        }

    def timeout(request):
        raise httpx.ReadTimeout("failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = SimpleNamespace(settings=Settings(), http=http, execute=execute, headers=headers)
        csv = "Type,Status,Id,Parent Id,Name\nFormat Version,,,,6.0\nCampaign,Paused,123,2,\n"
        assert (await upload_bulk(client, store, SCOPE, csv))["sent"] is False
        assert not calls
        with pytest.raises(ApiError) as error:
            await upload_bulk(client, store, SCOPE, csv, dry_run=False)
        assert error.value.details["request_id"] == "resume"
        assert error.value.details["mutation_outcome_unknown"]
        assert len(calls) == 1


async def test_bulk_missing_version_fails_before_creating_job(tmp_path):
    client = SimpleNamespace(settings=Settings())
    with pytest.raises(ApiError, match="Format Version"):
        await upload_bulk(
            client,
            Artifacts(tmp_path),
            SCOPE,
            "Type,Name\nCampaign,first\nCampaign,second\n",
            dry_run=False,
        )


@pytest.mark.parametrize("kind", entities.REPORTS)
async def test_entity_performance_resumes_report_and_keeps_supported_metrics(
    tmp_path, monkeypatch, kind
):
    client, store = EntityClient(), Artifacts(tmp_path)
    family, id_column = entities.REPORTS[kind]
    ids = {
        "accounts": "2",
        "campaigns": "10",
        "ad_groups": "20",
        "ads": "30",
        "keywords": "40",
        "asset_groups": "50",
        "audience_associations": "60",
    }
    metric = "Conversions" if kind == "asset_groups" else "ConversionsQualified"
    report = store.save(f"{id_column},Spend,{metric}\n{ids[kind]},5.50,2\n".encode(), "csv")
    submissions, polls = [], []

    async def submit(client, scope, request):
        assert scope == SCOPE
        # Validate convenience requests against the independently generated SDK model.
        models("reporting")[family + "PerformanceReportRequest"].model_validate(request)
        submissions.append(request)
        return {"data": {"ReportRequestId": "existing-report"}}

    async def poll(client, artifacts, scope, request_id):
        polls.append(request_id)
        return (
            {"status": "Pending"} if len(polls) == 1 else {"status": "Success", "files": [report]}
        )

    monkeypatch.setattr(entities, "submit_report", submit)
    monkeypatch.setattr(entities, "poll_report", poll)
    query = EntityQuery(
        performance_time={
            "PredefinedTime": "LastSevenDays",
            "ReportTimeZone": "PacificTimeUSCanadaTijuana",
        }
    )
    campaign = "10" if kind == "asset_groups" else None
    pending = await get_entities(client, store, kind, SCOPE, query, campaign)
    assert pending["status"] == "Pending"
    complete = await get_entities(
        client, store, kind, SCOPE, EntityQuery(**pending["query"]), campaign
    )
    assert len(submissions) == 1
    assert polls == ["existing-report", "existing-report"]
    assert complete["Entities"][0]["Performance"]["Spend"] == "5.50"
    assert complete["Entities"][0]["Performance"]["Conversions"] == "2"
    if kind == "audience_associations":
        assert complete["Entities"][1]["Performance"] is None
