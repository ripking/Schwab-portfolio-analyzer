"""Discord webhook notifications. Alert failures never propagate."""

from __future__ import annotations

import os
from typing import Iterable

import requests

WEBHOOK_TIMEOUT_SEC = 10


def send_discord_alert(message: str) -> None:
    """Send a message to Discord; fall back to stdout on any failure."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(message)
        return
    try:
        requests.post(
            webhook_url,
            json={"content": message},
            timeout=WEBHOOK_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 — never raise from alerting
        print(f"[discord alert failed: {exc}]")
        print(message)


def _format_command(env_var: str, fallback: str) -> str:
    """Render a command for Discord — fenced code block if customized, inline otherwise."""
    custom = os.environ.get(env_var, "").strip()
    if custom:
        return f"```\n{custom}\n```"
    return f"`{fallback}`"


def check_and_alert_reauth(hours_remaining: float) -> None:
    if hours_remaining > 24:
        return
    command = _format_command("REAUTH_COMMAND", "schwab-tracker auth login")
    send_discord_alert(
        "⚠️ Schwab re-auth required\n"
        "Refresh token expires in less than 24 hours.\n"
        "SSH into Unraid and run:\n"
        f"{command}"
    )


def alert_pull_failure(error_message: str) -> None:
    command = _format_command("PULL_COMMAND", "schwab-tracker pull")
    send_discord_alert(
        "❌ Schwab portfolio pull failed\n"
        f"Error: {error_message}\n"
        "Check Unraid and run:\n"
        f"{command}"
    )


def alert_snapshot_gaps(gap_dates: Iterable[str]) -> None:
    gaps = list(gap_dates)
    if not gaps:
        return
    send_discord_alert(
        "⚠️ Missing portfolio snapshots detected\n"
        f"Gaps in last 30 days: {', '.join(gaps)}\n"
        "These dates cannot be backfilled from the Schwab API."
    )
