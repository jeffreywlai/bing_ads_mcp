"""Redact URL credentials at the output boundary, after internal downloads use them."""

import re
from urllib.parse import unquote_plus

SECRET_PARAMETERS = {
    "sig",
    "signature",
    "access_token",
    "refresh_token",
    "token",
    "code",
    "client_secret",
    "api_key",
    "apikey",
    "key",
    "auth",
    "authorization",
}


def safe_output(value):
    if isinstance(value, str):

        def url(match):
            return re.sub(
                r"([?&#])([^=&?#]+)=([^&#]*)",
                lambda m: (
                    m[1] + m[2] + "=[REDACTED]"
                    if unquote_plus(m[2]).casefold() in SECRET_PARAMETERS
                    else m[0]
                ),
                re.sub(r"(https?://)[^/?#]*@", r"\1[REDACTED]@", match[0], flags=re.IGNORECASE),
            )

        return re.sub(r"https?://[^\s<>\"']+", url, value, flags=re.IGNORECASE)
    if isinstance(value, list):
        return [safe_output(item) for item in value]
    if isinstance(value, dict):
        return {k: safe_output(v) for k, v in value.items()}
    return value
