import asyncio
import io
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit

import httpx
import pytest

from bing_ads_mcp import auth


def test_browser_callback_without_copying_code(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(auth.webbrowser, "open", opened.append)

    class FakeServer:
        server_port = 49152

        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler = handler
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            self.calls += 1
            params = parse_qs(urlsplit(opened[0]).query)
            assert params["redirect_uri"] == ["http://localhost:49152"]
            assert params["code_challenge_method"] == ["S256"]
            assert params["prompt"] == ["select_account"]
            handler = self.handler.__new__(self.handler)
            state = "bad" if self.calls == 1 else params["state"][0]
            handler.path = "/?" + urlencode({"code": "sensitive-code", "state": state})
            handler.wfile = io.BytesIO()
            handler.send_response = lambda *args: None
            handler.send_error = lambda *args: None
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.do_GET()
            assert b"sensitive-code" not in handler.wfile.getvalue()

    monkeypatch.setattr(auth, "HTTPServer", FakeServer)
    code, verifier, redirect = auth.browser_authorization(auth.Settings(client_id="app"))
    assert code == "sensitive-code"
    assert len(verifier) >= 43
    assert redirect == "http://localhost:49152"
    assert "sensitive-code" not in capsys.readouterr().out


def test_callback_rejects_wrong_state_and_duplicate_code():
    with pytest.raises(ValueError):
        auth.response_code("http://localhost?code=secret&state=other", "expected")
    with pytest.raises(ValueError):
        auth.response_code("http://localhost?code=a&code=b&state=s", "s")


def test_login_sends_exact_redirect_and_pkce_then_saves_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".mcp.json"
    token_file = tmp_path / "token.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "BingAds": {
                        "env": {
                            "BING_ADS_CLIENT_ID": "app-from-mcp-config",
                            "BING_ADS_CLIENT_SECRET": "",
                            "BING_ADS_ENVIRONMENT": "production",
                            "BING_ADS_TOKEN_FILE": str(token_file),
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["bing-ads-login", "--mcp-config", str(config)])
    # main imports the local MCP environment; restore it after the test.
    for name in ("CLIENT_ID", "CLIENT_SECRET", "ENVIRONMENT", "TOKEN_FILE"):
        monkeypatch.setenv(f"BING_ADS_{name}", "")
    captured = {}
    code = "sensitive+code/with=encoding"

    def browser(settings):
        redirect = "http://localhost:61999"
        url, state, verifier = auth.authorization_request(settings.client_id, redirect_uri=redirect)
        captured.update(query=parse_qs(urlsplit(url).query), verifier=verifier)
        callback = redirect + "/?" + urlencode({"code": code, "state": state})
        return auth.response_code(callback, state, redirect + "/"), verifier, redirect

    def respond(request):
        assert str(request.url) == f"{auth.AUTHORITY}/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        fields = parse_qs(request.content.decode())
        assert fields["redirect_uri"] == captured["query"]["redirect_uri"]
        assert fields["client_id"] == captured["query"]["client_id"] == ["app-from-mcp-config"]
        assert fields["code"] == [code]
        assert fields["code_verifier"] == [captured["verifier"]]
        assert fields["grant_type"] == ["authorization_code"]
        assert "client_secret" not in fields
        return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})

    monkeypatch.setattr(auth, "browser_authorization", browser)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: client)
    auth.main()
    assert json.loads(token_file.read_text())["refresh_token"] == "refresh"
    output = capsys.readouterr().out
    assert "Signed in." in output
    assert code not in output
    assert captured["verifier"] not in output


async def test_oauth_error_keeps_provider_diagnostic_and_redacts_credentials():
    secrets = ["code+secret/one=", "pkce-secret", "refresh-secret", "client-secret", "dev-secret"]
    description = (
        "AADSTS50011: The redirect URI 'http://localhost:61999' specified in the request "
        "does not match the redirect URIs configured for the application 'test-app'. "
        "Trace ID: 11111111-1111-1111-1111-111111111111\r\n"
        + " ".join(encoded(secret) for secret in secrets for encoded in (str, quote, quote_plus))
    )
    settings = auth.Settings(
        client_id="test-app", client_secret=secrets[3], developer_token=secrets[4]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error_description": description})
        )
    ) as http:
        with pytest.raises(ValueError) as error:
            await auth.OAuth(settings, http).exchange(
                {
                    "code": secrets[0],
                    "code_verifier": secrets[1],
                    "refresh_token": secrets[2],
                    "redirect_uri": "http://localhost:61999",
                }
            )
    message = str(error.value)
    assert "AADSTS50011" in message
    assert "does not match the redirect URIs" in message
    assert "Trace ID: 11111111-1111-1111-1111-111111111111" in message
    assert "Request: client_id=test-app; redirect_uri=http://localhost:61999" in message
    assert "[REDACTED]" in message
    for secret in secrets:
        assert secret not in message
        assert quote(secret) not in message
        assert quote_plus(secret) not in message


@pytest.mark.parametrize(
    "environment,host", [("production", "ads.microsoft.com"), ("sandbox", "si.ads.microsoft.com")]
)
async def test_refresh_rotation_concurrency_and_permissions(tmp_path, environment, host):
    seen = []

    def respond(request):
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200, json={"access_token": "access", "refresh_token": "rotated", "expires_in": 3600}
        )

    settings = auth.Settings(
        client_id="app",
        refresh_token="initial",
        environment=environment,
        token_file=tmp_path / "token.json",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        oauth = auth.OAuth(settings, http)
        assert await asyncio.gather(*[oauth.access_token() for _ in range(5)]) == ["access"] * 5
    assert len(seen) == 1
    assert seen[0]["scope"] == [f"https://{host}/msads.manage offline_access"]
    assert json.loads(settings.token_file.read_text())["refresh_token"] == "rotated"
    assert settings.token_file.stat().st_mode & 0o777 == 0o600


async def test_failed_save_never_reuses_old_refresh_token(tmp_path, monkeypatch):
    settings = auth.Settings(client_id="app", token_file=tmp_path / "token.json")
    auth.save_token(settings, "old")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh"}
        )

    save = auth.save_token

    def fail(*args):
        raise OSError("disk error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        oauth = auth.OAuth(settings, http)
        monkeypatch.setattr(auth, "save_token", fail)
        with pytest.raises(OSError):
            await oauth.access_token()
        monkeypatch.setattr(auth, "save_token", save)
        assert await oauth.access_token() == "new-access"
    assert len(calls) == 1
    assert json.loads(settings.token_file.read_text())["refresh_token"] == "new-refresh"


async def test_saved_token_cannot_cross_environments(tmp_path):
    production = auth.Settings(client_id="app", token_file=tmp_path / "token.json")
    auth.save_token(production, "secret")
    sandbox = auth.Settings(
        client_id="app", environment="sandbox", token_file=production.token_file
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("network"))
    ) as http:
        with pytest.raises(ValueError, match="environment"):
            await auth.OAuth(sandbox, http).access_token()


async def test_failed_token_save_does_not_extend_expired_access_token(tmp_path, monkeypatch):
    settings = auth.Settings(client_id="app", token_file=tmp_path / "token.json")
    auth.save_token(settings, "old")
    clock = [100.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    exchanges = []

    def respond(request):
        exchanges.append(parse_qs(request.content.decode())["refresh_token"][0])
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{len(exchanges)}",
                "refresh_token": f"refresh-{len(exchanges)}",
                "expires_in": 3600,
            },
        )

    save = auth.save_token

    def fail(*args):
        raise OSError("disk error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        oauth = auth.OAuth(settings, http)
        monkeypatch.setattr(auth, "save_token", fail)
        with pytest.raises(OSError):
            await oauth.access_token()
        clock[0] += 4000
        monkeypatch.setattr(auth, "save_token", save)
        assert await oauth.access_token() == "access-2"
    assert exchanges == ["old", "refresh-1"]
    assert json.loads(settings.token_file.read_text())["refresh_token"] == "refresh-2"
