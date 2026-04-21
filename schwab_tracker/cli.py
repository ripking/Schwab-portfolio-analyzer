"""Click-based command-line interface for schwab-tracker."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from . import auth as auth_mod
from . import notifications
from . import positions as positions_mod
from . import storage

ENV_FILE = Path.cwd() / ".env"

MASKED_KEYS = {
    "SCHWAB_CLIENT_SECRET",
    "PORTFOLIO_DB_PASSWORD",
    "DISCORD_WEBHOOK_URL",
}

CONFIG_KEYS = (
    "SCHWAB_CLIENT_ID",
    "SCHWAB_CLIENT_SECRET",
    "PORTFOLIO_DB_HOST",
    "PORTFOLIO_DB_PORT",
    "PORTFOLIO_DB_USER",
    "PORTFOLIO_DB_PASSWORD",
    "PORTFOLIO_DB_NAME",
    "DISCORD_WEBHOOK_URL",
)


def _load_env() -> None:
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
    else:
        load_dotenv(override=False)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _append_env(updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value
    existing.update({k: v for k, v in updates.items() if v is not None})
    lines = [f"{key}={existing[key]}" for key in existing]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)


@click.group()
def cli() -> None:
    """Schwab portfolio tracker — nightly snapshot pipeline."""
    _load_env()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@cli.group()
def auth() -> None:
    """Manage Schwab OAuth tokens."""


@auth.command("login")
def auth_login() -> None:
    """Run the OAuth flow and persist new tokens."""
    try:
        record = auth_mod.initial_auth()
    except auth_mod.AuthConfigError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)
    access_exp = datetime.utcfromtimestamp(record["access_token_expires_at"])
    refresh_exp = datetime.utcfromtimestamp(record["refresh_token_expires_at"])
    click.echo("✓ Tokens saved")
    click.echo(f"  Access token expires:  {access_exp:%Y-%m-%d %H:%M:%S UTC}")
    click.echo(f"  Refresh token expires: {refresh_exp:%Y-%m-%d %H:%M:%S UTC}")


@auth.command("status")
def auth_status() -> None:
    """Print token expiry and age."""
    try:
        auth_mod.token_status(print_fn=click.echo)
    except auth_mod.TokenExpiredError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)


@auth.command("refresh")
def auth_refresh() -> None:
    """Force an access-token refresh."""
    try:
        auth_mod.refresh_tokens()
    except auth_mod.TokenExpiredError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)
    auth_mod.token_status(print_fn=click.echo)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@cli.group()
def config() -> None:
    """Inspect environment configuration."""


@config.command("show")
def config_show() -> None:
    """Print current env values (secrets masked) and test the DB."""
    click.echo(f"Env file: {ENV_FILE if ENV_FILE.exists() else '(not found)'}")
    for key in CONFIG_KEYS:
        value = os.environ.get(key, "")
        display = _mask(value) if key in MASKED_KEYS else value
        click.echo(f"  {key}={display}")
    click.echo("")
    storage.test_db_connection(print_fn=click.echo)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@cli.group()
def db() -> None:
    """Manage the PortfolioDB schema and connectivity."""


@db.command("init")
def db_init() -> None:
    """Create the positions table if it does not exist."""
    try:
        conn = storage.get_db_connection()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"✗ Cannot connect to PortfolioDB: {exc}", err=True)
        sys.exit(1)
    try:
        storage.init_db(conn)
    finally:
        conn.close()
    click.echo("✓ positions table ready")


@db.command("test")
def db_test() -> None:
    """Probe the database connection."""
    ok = storage.test_db_connection(print_fn=click.echo)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@cli.command("pull")
def pull() -> None:
    """Fetch positions from Schwab and upsert into PortfolioDB."""
    try:
        token = auth_mod.get_valid_token()
        rows = positions_mod.get_all_positions(token)

        conn = storage.get_db_connection()
        try:
            storage.upsert_positions(rows, conn)
            gaps = storage.get_snapshot_gaps(conn)
        finally:
            conn.close()

        if gaps:
            notifications.alert_snapshot_gaps(gaps)

        try:
            hours_remaining = auth_mod.get_refresh_token_hours_remaining()
            notifications.check_and_alert_reauth(hours_remaining)
        except Exception as exc:  # noqa: BLE001 — alerting must not fail the pull
            click.echo(f"[reauth check skipped: {exc}]", err=True)

        account_count = len({r["account_number"] for r in rows})
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        click.echo(
            f"✓ {len(rows)} positions across {account_count} accounts "
            f"upserted to PortfolioDB — {timestamp}"
        )
    except Exception as exc:  # noqa: BLE001 — broadcast + re-raise
        notifications.alert_pull_failure(str(exc))
        click.echo(f"✗ Pull failed: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@cli.command("export")
@click.option(
    "--date",
    "date_str",
    default=None,
    help="Snapshot date (YYYY-MM-DD). Defaults to the most recent snapshot.",
)
@click.option(
    "--output",
    "output_dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True),
    help="Output directory. Defaults to current working directory.",
)
def export(date_str: str | None, output_dir: str | None) -> None:
    """Write a markdown portfolio report for a snapshot date."""
    try:
        conn = storage.get_db_connection()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"✗ Cannot connect to PortfolioDB: {exc}", err=True)
        sys.exit(1)
    try:
        if date_str is None:
            date_str = storage.get_latest_snapshot_date(conn)
            if not date_str:
                click.echo("✗ No snapshots found in PortfolioDB", err=True)
                sys.exit(1)
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            click.echo(f"✗ --date must be YYYY-MM-DD (got {date_str})", err=True)
            sys.exit(1)

        markdown = storage.export_markdown(date_str, conn)
    finally:
        conn.close()

    if markdown is None:
        click.echo(f"✗ No positions found for {date_str}", err=True)
        sys.exit(1)

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"portfolio_{date_str}.md"
    out_path.write_text(markdown)
    click.echo(str(out_path.resolve()))


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


CRON_INSTRUCTIONS = """
Cron (Unraid User Scripts plugin):
  1. Install the "User Scripts" plugin from Community Apps if needed.
  2. Add a new script named `schwab-portfolio-pull` with this body:

     #!/bin/bash
     export PATH="/usr/local/bin:/usr/bin:/bin"
     cd /mnt/user/appdata/schwab-portfolio-tracker
     source .env
     /usr/local/bin/schwab-tracker pull >> /mnt/user/appdata/schwab-portfolio-tracker/pull.log 2>&1

  3. Set a custom cron schedule: `0 18 * * 1-5` (6pm weekdays).
  4. Log file: /mnt/user/appdata/schwab-portfolio-tracker/pull.log
"""


@cli.command("setup")
@click.pass_context
def setup(ctx: click.Context) -> None:
    """Interactive first-run wizard."""
    click.echo("Schwab portfolio tracker — setup wizard")
    click.echo("---------------------------------------")
    click.echo(f"Writing config to {ENV_FILE}")
    click.echo("")

    updates: dict[str, str] = {}
    updates["SCHWAB_CLIENT_ID"] = click.prompt("SCHWAB_CLIENT_ID", type=str)
    updates["SCHWAB_CLIENT_SECRET"] = click.prompt(
        "SCHWAB_CLIENT_SECRET", type=str, hide_input=True
    )
    updates["PORTFOLIO_DB_HOST"] = click.prompt(
        "PORTFOLIO_DB_HOST", default="localhost"
    )
    updates["PORTFOLIO_DB_PORT"] = click.prompt("PORTFOLIO_DB_PORT", default="5432")
    updates["PORTFOLIO_DB_USER"] = click.prompt("PORTFOLIO_DB_USER", default="postgres")
    updates["PORTFOLIO_DB_PASSWORD"] = click.prompt(
        "PORTFOLIO_DB_PASSWORD", hide_input=True, default="", show_default=False
    )
    updates["PORTFOLIO_DB_NAME"] = click.prompt(
        "PORTFOLIO_DB_NAME", default="PortfolioDB"
    )
    updates["DISCORD_WEBHOOK_URL"] = click.prompt(
        "DISCORD_WEBHOOK_URL (enter to skip)", default="", show_default=False
    )

    _append_env(updates)
    for key, value in updates.items():
        os.environ[key] = value
    click.echo(f"✓ Wrote {ENV_FILE}")
    click.echo("")

    click.echo("Initializing database schema…")
    ctx.invoke(db_init)
    click.echo("")

    click.echo("Starting Schwab OAuth login…")
    ctx.invoke(auth_login)
    click.echo("")

    click.echo("Running a test pull…")
    try:
        token = auth_mod.get_valid_token()
        rows = positions_mod.get_all_positions(token)
        account_count = len({r["account_number"] for r in rows})
        click.echo(
            f"✓ Test fetch: {account_count} accounts, {len(rows)} positions"
        )
        conn = storage.get_db_connection()
        try:
            storage.upsert_positions(rows, conn)
        finally:
            conn.close()
        click.echo("✓ Snapshot persisted to PortfolioDB")
    except Exception as exc:  # noqa: BLE001
        click.echo(f"✗ Test pull failed: {exc}", err=True)
        sys.exit(1)

    click.echo("")
    click.echo(CRON_INSTRUCTIONS)


if __name__ == "__main__":
    cli()
