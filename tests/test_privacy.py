import json

import pytest

from bing_ads_mcp.artifacts import INLINE_BYTES, Artifacts
from bing_ads_mcp.client import ApiError
from bing_ads_mcp.privacy import safe_output
from bing_ads_mcp.transfers import signed_urls


def test_advertising_urls_survive_inline_results_files_snapshots_and_errors(tmp_path):
    urls = [
        "https://shop.example/item?code=SKU123&key=category&token=channel&sig=brand#auth=label",
        "https://login.microsoftonline.com.shop.example/?code=SUMMER",
        "https://shop.example/?next=https%3A%2F%2Flogin.microsoftonline.com&code=SUMMER",
        "https://[unfinished?code=SUMMER",
    ]
    row = {"Id": "001", "FinalUrls": urls, "Future": [None, {"text": urls[0]}]}
    original = {"data": {"Ads": [row]}, "PartialErrors": [None]}
    store = Artifacts(tmp_path)
    assert store.bounded(original) == original
    oversized = {**original, "large": "x" * INLINE_BYTES}
    descriptor = store.bounded(oversized)["complete_result"]
    assert store.json_value(descriptor["artifact"]) == oversized
    page = store.entity_page([row, row], "urls", 1)
    assert page["Entities"] == [row]
    assert store.json_value(page["snapshot_artifact"], "/rows") == [row, row]
    last = store.entity_page(None, "urls", 1, page["NextPageToken"])
    assert last["Entities"] == [row] and last["NextPageToken"] is None
    projected = store.entity_page([row], "projected", 1, fields=["Id"])
    assert projected["Entities"] == [{"Id": "001"}]
    assert store.json_value(projected["snapshot_artifact"], "/rows") == [row]
    assert (
        ApiError("provider error", provider_response=original).details["provider_response"]
        == original
    )


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://reports.blob.core.windows.net/file?sv=1&%73IG=hidden&sp=r",
        "https://bulk.api.bingads.microsoft.com/file?token=hidden&ok=1",
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?code=hidden&prompt=login",
        "https://login.live.com/oauth20_desktop.srf#access_token=hidden",
        "https://name:hidden@shop.example/?code=SUMMER",
    ],
)
def test_credential_urls_remain_redacted_without_mutating_provider_data(url):
    original = {"url": url, "future": [None, url]}
    safe = safe_output(original)
    assert "hidden" not in json.dumps(safe)
    assert original == {"url": url, "future": [None, url]}
    if "SUMMER" in url:
        assert safe["url"] == "https://[REDACTED]@shop.example/?code=SUMMER"


def test_only_microsoft_transfer_urls_are_download_candidates():
    transfer = "https://reports.blob.core.windows.net/file?sig=hidden"
    result = {
        "FileUrl": transfer,
        "DestinationUrl": "https://shop.example/?code=SUMMER&key=category&sig=brand",
        "LoginUrl": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?code=hidden",
        "OtherUrl": "https://name:hidden@shop.example/file",
        "UploadUrl": "https://bulk.api.bingads.microsoft.com/file?sig=hidden",
    }
    assert list(signed_urls(result)) == [("/FileUrl", transfer)]
