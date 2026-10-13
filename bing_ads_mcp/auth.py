"""OAuth setup and refresh. Tokens stay on disk, never in MCP tool arguments/results."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit

import httpx
from dotenv import load_dotenv

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
REDIRECT_URI = "http://localhost"


def oauth_scope(environment: str) -> str:
    if environment not in {"production", "sandbox"}:
        raise ValueError("Environment must be production or sandbox.")
    host = "si.ads.microsoft.com" if environment == "sandbox" else "ads.microsoft.com"
    return f"https://{host}/msads.manage offline_access"


@dataclass(frozen=True)
class Settings:
    client_id: str = ""
    developer_token: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    environment: str = "production"
    read_only: bool = False
    token_file: Path = Path(".bing-ads/token.json")
    export_dir: Path = Path(".bing-ads/exports")

    @classmethod
    def from_env(cls):
        load_dotenv(dotenv_path=Path.cwd() / ".env")
        environment = os.getenv("BING_ADS_ENVIRONMENT", "production")
        read_only = os.getenv("BING_ADS_READ_ONLY", "false").lower()
        if environment not in {"production", "sandbox"}:
            raise ValueError("BING_ADS_ENVIRONMENT must be production or sandbox.")
        if read_only not in {"true", "false"}:
            raise ValueError("BING_ADS_READ_ONLY must be true or false.")
        return cls(
            client_id=os.getenv("BING_ADS_CLIENT_ID", ""),
            developer_token=os.getenv("BING_ADS_DEVELOPER_TOKEN", ""),
            client_secret=os.getenv("BING_ADS_CLIENT_SECRET", ""),
            refresh_token=os.getenv("BING_ADS_REFRESH_TOKEN", ""),
            environment=environment,
            read_only=read_only == "true",
            token_file=Path(os.getenv("BING_ADS_TOKEN_FILE", ".bing-ads/token.json")),
            export_dir=Path(os.getenv("BING_ADS_EXPORT_DIR", ".bing-ads/exports")),
        )


def save_token(settings: Settings, refresh_token: str) -> None:
    """Replace atomically, with owner-only permissions; never overwrite .env."""
    path = settings.token_file
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {
                    "client_id": settings.client_id,
                    "environment": settings.environment,
                    "refresh_token": refresh_token,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OAuth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http
        self._lock = asyncio.Lock()
        self._access = ""
        self._expires = 0.0
        self._refresh = settings.refresh_token
        self._loaded = False
        self._pending = False

    async def exchange(self, fields: dict) -> dict:
        payload = {
            **fields,
            "client_id": self.settings.client_id,
            "scope": oauth_scope(self.settings.environment),
        }
        if self.settings.client_secret:
            payload["client_secret"] = self.settings.client_secret
        try:
            response = await self.http.post(f"{AUTHORITY}/token", data=payload, timeout=30)
            body = response.json()
        except httpx.HTTPError:
            raise ValueError(
                "Could not reach the Microsoft token endpoint. Check network connectivity."
            ) from None
        except ValueError:
            raise ValueError(
                "Microsoft's token endpoint returned an invalid JSON response."
            ) from None
        if (
            not response.is_success
            or not isinstance(body, dict)
            or not isinstance(body.get("access_token"), str)
            or not body["access_token"]
        ):
            description = str(body.get("error_description", "")) if isinstance(body, dict) else ""
            codes = ", ".join(sorted(set(re.findall(r"AADSTS[0-9]+", description))))
            # Microsoft can echo request values in errors. Redact before truncating
            # so a clipped secret cannot escape replacement.
            credentials = [
                payload.get(name, "")
                for name in ("code", "code_verifier", "refresh_token", "client_secret")
            ] + [self.settings.developer_token, self.settings.refresh_token, self._access]
            for value in sorted(set(filter(None, credentials)), key=len, reverse=True):
                for encoded in (quote(value, safe=""), quote(value), quote_plus(value), value):
                    description = description.replace(encoded, "[REDACTED]")
            description = " ".join(description.split())[:2000]
            request = f"client_id={self.settings.client_id}"
            if fields.get("redirect_uri"):
                request += f"; redirect_uri={fields['redirect_uri']}"
            raise ValueError(
                f"OAuth authentication failed (HTTP {response.status_code}; "
                f"{codes or 'no error code'}).\n"
                f"Microsoft: {description or 'No error description returned.'}\n"
                f"Request: {request}\n"
                "Run bing-ads-login again after correcting the reported issue."
            )
        if body.get("refresh_token") is not None and (
            not isinstance(body["refresh_token"], str) or not body["refresh_token"]
        ):
            raise ValueError("OAuth returned an invalid refresh token. Restart login.")
        try:
            if int(body.get("expires_in", 3600)) <= 0:
                raise ValueError
        except TypeError, ValueError:
            raise ValueError("OAuth returned an invalid token expiration.") from None
        return body

    async def access_token(self) -> str:
        async with self._lock:
            if self._pending:
                # A previous save failed. Persist the rotated token before using it,
                # while keeping the original access-token expiration unchanged.
                save_token(self.settings, self._refresh)
                self._pending = False
            if self._access and time.monotonic() < self._expires:
                return self._access
            if not self.settings.client_id:
                raise ValueError("Set BING_ADS_CLIENT_ID and run bing-ads-login.")
            if not self._loaded and self.settings.token_file.exists():
                try:
                    saved = json.loads(self.settings.token_file.read_text())
                    if (
                        saved["client_id"] != self.settings.client_id
                        or saved["environment"] != self.settings.environment
                    ):
                        raise ValueError
                    self._refresh = saved["refresh_token"]
                    if not isinstance(self._refresh, str) or not self._refresh:
                        raise ValueError
                except ValueError, KeyError, TypeError, OSError:
                    raise ValueError(
                        "Token file is invalid for this app/environment. Run bing-ads-login."
                    ) from None
            self._loaded = True
            if not self._refresh:
                raise ValueError("Run bing-ads-login or set BING_ADS_REFRESH_TOKEN.")
            body = await self.exchange(
                {"grant_type": "refresh_token", "refresh_token": self._refresh}
            )
            self._refresh = body.get("refresh_token") or self._refresh
            self._access = body["access_token"]
            self._expires = time.monotonic() + max(0, int(body.get("expires_in", 3600)) - 120)
            # Persist the replacement before any advertising request can be sent.
            self._pending = True
            save_token(self.settings, self._refresh)
            self._pending = False
            return self._access


def authorization_request(
    client_id: str, environment: str = "production", redirect_uri: str = REDIRECT_URI
) -> tuple[str, str, str]:
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = urlencode(
        {
            "client_id": client_id,
            "scope": oauth_scope(environment),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "prompt": "select_account",
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORITY}/authorize?{query}", state, verifier


def response_code(uri: str, state: str, redirect_uri: str = REDIRECT_URI) -> str:
    parsed, expected = urlsplit(uri), urlsplit(redirect_uri)
    query = parse_qs(parsed.query)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise ValueError("Unexpected OAuth callback address.")
    if query.get("state") != [state] or len(query.get("code", [])) != 1:
        raise ValueError("OAuth state/code mismatch. Restart login.")
    return query["code"][0]


def browser_authorization(settings: Settings, timeout: float = 300) -> tuple[str, str, str]:
    """Receive the PKCE callback on this computer; never ask users to copy a code URL."""
    outcome = {}
    state = ""

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # The request path contains the authorization code.

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid_state = query.get("state") == [state]
            if urlsplit(self.path).path != "/" or not valid_state:
                self.send_error(400, "Unexpected callback; continue sign-in in the original tab.")
                return
            if query.get("error"):
                outcome["error"] = "Microsoft sign-in was declined or failed. Run login again."
            else:
                try:
                    outcome["code"] = response_code(
                        redirect_uri + self.path, state, redirect_uri + "/"
                    )
                except ValueError:
                    self.send_error(400, "Invalid callback. Restart login.")
                    return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><title>Bing Ads MCP</title>"
                b"<script>history.replaceState(null,'','/');</script>"
                b"<p>Sign-in response received. You can close this tab. "
                b"Check your Terminal for the result.</p>"
            )

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://localhost:{server.server_port}"
        url, state, verifier = authorization_request(
            settings.client_id, settings.environment, redirect_uri
        )
        print("Opening Microsoft sign-in. Complete it in your browser.", flush=True)
        print(
            "Waiting for the browser callback (up to 5 minutes). No pasting is needed.", flush=True
        )
        print(f"If the browser does not open, use this sign-in link:\n{url}\n", flush=True)
        webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
        if not outcome:
            raise ValueError(
                "Sign-in timed out. Register http://localhost under Mobile and desktop "
                "applications in Azure, then run login again."
            )
        if "error" in outcome:
            raise ValueError(outcome["error"])
        return outcome["code"], verifier, redirect_uri


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign in to Microsoft Advertising locally.")
    parser.add_argument(
        "--mcp-config", type=Path, help="Read BingAds env settings from your local Claude .mcp.json"
    )
    args = parser.parse_args()
    if args.mcp_config:
        try:
            config = json.loads(args.mcp_config.read_text())["mcpServers"]["BingAds"]["env"]
            for name, value in config.items():
                if name.startswith("BING_ADS_") and isinstance(value, str):
                    if "${" in value:
                        raise ValueError("Use literal values in this local MCP config for login.")
                    os.environ[name] = value
        except OSError, KeyError, TypeError, json.JSONDecodeError:
            raise SystemExit("Cannot read mcpServers.BingAds.env from this MCP config.") from None
    settings = Settings.from_env()
    if not settings.client_id:
        raise SystemExit("Set BING_ADS_CLIENT_ID in your MCP config or .env first.")
    try:
        code, verifier, redirect_uri = browser_authorization(settings)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Login failed: {exc}") from None
    print("Sign-in received. Exchanging authorization code for tokens...", flush=True)

    async def login():
        async with httpx.AsyncClient(follow_redirects=False) as http:
            body = await OAuth(settings, http).exchange(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                }
            )
            if not body.get("refresh_token"):
                raise ValueError(
                    "No refresh token returned. Grant offline_access and sign in again."
                )
            save_token(settings, body["refresh_token"])

    try:
        asyncio.run(login())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Login failed: {exc}") from None
    print(f"Signed in. Refresh token saved privately to {settings.token_file}.")


if __name__ == "__main__":
    main()
