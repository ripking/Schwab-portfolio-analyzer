"""Schwab OAuth token manager.

Tokens are persisted to ~/.schwab_tracker/tokens.json with mode 0600. Access
tokens expire after 30 minutes, refresh tokens after 7 days — get_valid_token
refreshes on demand with a 5-minute safety buffer.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

OAUTH_BASE = "https://api.schwabapi.com/v1/oauth"
AUTHORIZE_URL = f"{OAUTH_BASE}/authorize"
TOKEN_URL = f"{OAUTH_BASE}/token"

DEFAULT_REDIRECT_URI = "https://127.0.0.1"
TOKEN_DIR = Path.home() / ".schwab_tracker"
TOKEN_FILE = TOKEN_DIR / "tokens.json"

ACCESS_TOKEN_TTL_SEC = 30 * 60
REFRESH_TOKEN_TTL_SEC = 7 * 24 * 60 * 60
REFRESH_SAFETY_BUFFER_SEC = 5 * 60


class TokenExpiredError(Exception):
    """Raised when the Schwab refresh token has expired or been revoked."""


class AuthConfigError(Exception):
    """Raised when required OAuth config is missing from the environment."""


def _client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("SCHWAB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SCHWAB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise AuthConfigError(
            "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET must be set in .env"
        )
    return client_id, client_secret


def _redirect_uri() -> str:
    return os.environ.get("SCHWAB_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _save_tokens(payload: dict[str, Any]) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    tmp_path.replace(TOKEN_FILE)
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)


def _load_tokens() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise TokenExpiredError(
            "Refresh token expired or revoked. Run: schwab-tracker auth login"
        )
    return json.loads(TOKEN_FILE.read_text())


def _now() -> float:
    return time.time()


def _build_record(token_response: dict[str, Any], *, refresh_issued_at: float | None) -> dict[str, Any]:
    now = _now()
    expires_in = int(token_response.get("expires_in", ACCESS_TOKEN_TTL_SEC))
    refresh_anchor = refresh_issued_at if refresh_issued_at is not None else now
    return {
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "token_type": token_response.get("token_type", "Bearer"),
        "scope": token_response.get("scope"),
        "id_token": token_response.get("id_token"),
        "access_token_issued_at": now,
        "access_token_expires_at": now + expires_in,
        "refresh_token_issued_at": refresh_anchor,
        "refresh_token_expires_at": refresh_anchor + REFRESH_TOKEN_TTL_SEC,
        "last_refreshed_at": now,
    }


def _extract_code(redirect_url: str) -> str:
    parsed = urlparse(redirect_url.strip())
    query = parse_qs(parsed.query)
    codes = query.get("code")
    if not codes:
        raise ValueError(
            "No `code` parameter found in the pasted redirect URL. "
            "Make sure you pasted the full URL from your browser address bar."
        )
    return codes[0]


def build_authorize_url() -> str:
    client_id, _ = _client_credentials()
    redirect_uri = _redirect_uri()
    return (
        f"{AUTHORIZE_URL}"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
    )


def initial_auth(input_fn=input, print_fn=print) -> dict[str, Any]:
    """Run the one-time OAuth authorization flow and persist tokens."""
    client_id, client_secret = _client_credentials()
    redirect_uri = _redirect_uri()
    authorize_url = build_authorize_url()

    print_fn("")
    print_fn("Open the following URL in a browser, approve access, then paste")
    print_fn("the FULL redirect URL from your browser's address bar back here.")
    print_fn("")
    print_fn(authorize_url)
    print_fn("")
    pasted = input_fn("Paste redirect URL: ").strip()
    code = _extract_code(pasted)

    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({response.status_code}): {response.text}"
        )
    record = _build_record(response.json(), refresh_issued_at=_now())
    _save_tokens(record)
    return record


def refresh_tokens() -> str:
    """Refresh the access token using the stored refresh token."""
    client_id, client_secret = _client_credentials()
    tokens = _load_tokens()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise TokenExpiredError(
            "Refresh token expired or revoked. Run: schwab-tracker auth login"
        )

    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if response.status_code in (400, 401):
        raise TokenExpiredError(
            "Refresh token expired or revoked. Run: schwab-tracker auth login"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed ({response.status_code}): {response.text}"
        )

    refresh_issued_at = tokens.get("refresh_token_issued_at", _now())
    record = _build_record(response.json(), refresh_issued_at=refresh_issued_at)
    _save_tokens(record)
    return record["access_token"]


def get_valid_token() -> str:
    """Return a valid access token, refreshing if expired or near expiry."""
    tokens = _load_tokens()
    expires_at = float(tokens.get("access_token_expires_at", 0))
    if expires_at - _now() > REFRESH_SAFETY_BUFFER_SEC:
        return tokens["access_token"]
    return refresh_tokens()


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_refresh_token_hours_remaining() -> float:
    tokens = _load_tokens()
    expires_at = float(tokens.get("refresh_token_expires_at", 0))
    return max(0.0, (expires_at - _now()) / 3600.0)


def token_status(print_fn=print) -> dict[str, Any]:
    tokens = _load_tokens()
    access_exp = float(tokens.get("access_token_expires_at", 0))
    refresh_exp = float(tokens.get("refresh_token_expires_at", 0))
    last_refreshed = float(tokens.get("last_refreshed_at", 0))

    hours_remaining = max(0.0, (refresh_exp - _now()) / 3600.0)
    days = int(hours_remaining // 24)
    hours = hours_remaining - days * 24

    print_fn(f"Access token expires:  {_fmt_ts(access_exp)}")
    print_fn(f"Refresh token expires: {_fmt_ts(refresh_exp)}")
    print_fn(f"Refresh remaining:     {days}d {hours:.1f}h")
    print_fn(f"Last refreshed:        {_fmt_ts(last_refreshed)}")
    if hours_remaining <= 24:
        print_fn("⚠️  Less than 24 hours remaining — run: schwab-tracker auth login")

    return {
        "access_token_expires_at": access_exp,
        "refresh_token_expires_at": refresh_exp,
        "last_refreshed_at": last_refreshed,
        "hours_remaining": hours_remaining,
    }
