import json
from types import SimpleNamespace

import httpx
import pytest

from bing_ads_mcp import conversions
from bing_ads_mcp.auth import Settings
from bing_ads_mcp.client import ApiError
from bing_ads_mcp.models import Scope

SCOPE = Scope(customer_id="1", account_id="2")
PAYLOAD = {
    "data": [{"eventType": "custom", "eventId": "stable-id", "FutureField": [None]}],
    "continueOnValidationError": True,
}


async def test_uet_preview_and_readonly_never_retrieve_or_create_key():
    async def forbidden(*args, **kwargs):
        pytest.fail("No credentials or requests may be made for a preview or blocked write.")

    client = SimpleNamespace(settings=Settings(read_only=True), execute=forbidden)
    preview = await conversions.submit(client, SCOPE, "123", PAYLOAD)
    assert preview["sent"] is False
    assert preview["request"] == PAYLOAD
    assert "creating it if absent" in preview["authentication"]
    with pytest.raises(ApiError, match="disabled"):
        await conversions.submit(client, SCOPE, "123", PAYLOAD, dry_run=False)
    with pytest.raises(ApiError, match="1000"):
        await conversions.submit(client, SCOPE, "123", {"data": [{}] * 1001}, dry_run=False)


@pytest.mark.parametrize("partial", [False, True])
async def test_uet_uses_sdk_key_keeps_native_fields_and_preserves_partial_response(partial):
    key_calls, event_calls = [], []
    provider_response = {"eventsReceived": 1, "FutureResponse": {"x": [None]}}
    if partial:
        provider_response["error"] = {
            "code": "ValidationWarning",
            "details": [{"index": 0, "isWarning": True, "propertyName": "data[0].userData.em"}],
        }

    async def execute(service, name, request, scope):
        key_calls.append((service, name, request, scope))
        return {"data": {"UetTagAuthKey": "private-key", "PartialErrors": []}}

    def respond(request):
        event_calls.append(request)
        assert str(request.url) == "https://capi.uet.microsoft.com/v1/123/events"
        assert request.headers["Authorization"] == "Bearer private-key"
        assert "DeveloperToken" not in request.headers
        assert json.loads(request.content) == PAYLOAD
        return httpx.Response(200, json=provider_response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SimpleNamespace(settings=Settings(), execute=execute, http=http)
        output = await conversions.submit(client, SCOPE, "123", PAYLOAD, dry_run=False)
    assert key_calls == [("campaign", "get_uet_tag_auth_key", {"UetTagId": "123"}, SCOPE)]
    assert len(event_calls) == 1
    assert output["data"] == provider_response
    assert output["status"] == ("partial_failure" if partial else "returned")
    assert "private-key" not in json.dumps(output)


@pytest.mark.parametrize("failure", ["timeout", "redirect", "validation"])
async def test_uet_never_retries_or_follows_redirects_and_redacts_key(failure):
    calls = []

    async def execute(*args, **kwargs):
        return {"data": {"UetTagAuthKey": "private-key"}}

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-key")
        return httpx.Response(
            302 if failure == "redirect" else 400,
            headers={"Location": "https://example.invalid"},
            json={"error": {"code": "ValidationError", "message": "private-key"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SimpleNamespace(settings=Settings(), execute=execute, http=http)
        with pytest.raises(ApiError) as error:
            await conversions.submit(client, SCOPE, "123", PAYLOAD, dry_run=False)
    assert len(calls) == 1
    assert "private-key" not in str(error.value)
    assert error.value.details["mutation_outcome_unknown"] is (failure == "timeout")
