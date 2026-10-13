"""OAuth setup and refresh. Tokens stay on disk, never in MCP tool arguments/results."""

import asyncio
import base64
import getpass
import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from dotenv import load_dotenv

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"

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
            json.dump({
                "client_id": settings.client_id,
                "environment": settings.environment,
                "refresh_token": refresh_token,
            }, stream)
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

    async def exchange(self, fields: dict) -> dict:
        payload = {
            **fields, "client_id": self.settings.client_id,
            "scope": oauth_scope(self.settings.environment),
        }
        if self.settings.client_secret:
            payload["client_secret"] = self.settings.client_secret
        try:
            response = await self.http.post(f"{AUTHORITY}/token", data=payload, timeout=30)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("OAuth request failed. Check connectivity and run bing-ads-login.") from None
        if response.is_error or not isinstance(body, dict) or not body.get("access_token"):
            raise ValueError("OAuth authentication failed. Check the app settings or run bing-ads-login.")
        return body

    async def access_token(self) -> str:
        async with self._lock:
            if self._access and time.monotonic() < self._expires:
                return self._access
            if not self.settings.client_id:
                raise ValueError("Set BING_ADS_CLIENT_ID and run bing-ads-login.")
            if self.settings.token_file.exists():
                try:
                    saved = json.loads(self.settings.token_file.read_text())
                    if (
                        saved["client_id"] != self.settings.client_id
                        or saved["environment"] != self.settings.environment
                    ):
                        raise ValueError
                    self._refresh = saved["refresh_token"]
                except (ValueError, KeyError, TypeError, OSError):
                    raise ValueError(
                        "Token file is invalid for this app/environment. Run bing-ads-login."
                    ) from None
            if not self._refresh:
                raise ValueError("Run bing-ads-login or set BING_ADS_REFRESH_TOKEN.")
            body = await self.exchange({"grant_type": "refresh_token", "refresh_token": self._refresh})
            self._refresh = body.get("refresh_token") or self._refresh
            # Persist the replacement before any advertising request can be sent.
            save_token(self.settings, self._refresh)
            self._access = body["access_token"]
            self._expires = time.monotonic() + max(0, int(body.get("expires_in", 3600)) - 120)
            return self._access


def authorization_request(
    client_id: str, environment: str = "production"
) -> tuple[str, str, str]:
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = urlencode({
        "client_id": client_id, "scope": oauth_scope(environment), "response_type": "code",
        "redirect_uri": REDIRECT_URI, "state": state,
        "code_challenge": challenge.decode(), "code_challenge_method": "S256",
    })
    return f"{AUTHORITY}/authorize?{query}", state, verifier


def response_code(uri: str, state: str) -> str:
    parsed, expected = urlsplit(uri), urlsplit(REDIRECT_URI)
    query = parse_qs(parsed.query)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("Paste the full redirect URL from the Microsoft sign-in page.")
    if query.get("state") != [state] or len(query.get("code", [])) != 1:
        raise ValueError("OAuth state/code mismatch. Restart login.")
    return query["code"][0]


def main() -> None:
    settings = Settings.from_env()
    if not settings.client_id:
        raise SystemExit("Set BING_ADS_CLIENT_ID in .env first.")
    url, state, verifier = authorization_request(settings.client_id, settings.environment)
    print(f"Sign in and consent at:\n{url}\n")
    webbrowser.open(url)
    code = response_code(getpass.getpass("Paste the full redirect URL (hidden): "), state)

    async def login():
        async with httpx.AsyncClient(follow_redirects=False) as http:
            body = await OAuth(settings, http).exchange({
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
            })
            if not body.get("refresh_token"):
                raise ValueError("No refresh token returned. Grant offline_access and sign in again.")
            save_token(settings, body["refresh_token"])

    asyncio.run(login())
    print(f"Signed in. Refresh token saved privately to {settings.token_file}.")


if __name__ == "__main__":
    main()
