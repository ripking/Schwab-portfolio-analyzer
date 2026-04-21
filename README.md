# schwab-portfolio-tracker

A data-pipeline CLI that fetches current positions from the Schwab Trader API
every weekday evening and upserts them into a PostgreSQL database
(`PortfolioDB`) running on Unraid. One optional on-demand command generates a
markdown report from any historical snapshot date already in the database.

## What this tool does

- Nightly pull of current positions across every Schwab account via the
  Trader API.
- Enrichment with current market quotes (company name, last price, day change)
  in a single batch request.
- Idempotent upsert into a single `positions` table keyed by
  `(snapshot_date, account_number, symbol)`.
- Manual, SSH-driven OAuth flow for the weekly refresh-token renewal.
- Discord alerts for upcoming re-auth, pull failures, and missed snapshots.
- On-demand markdown export of a historical snapshot.

## What this tool does NOT do

- No analytics, charting, reporting web UI, or REST API.
- No historical backfill — Schwab does not expose historical position data,
  so missed pulls remain gaps forever.
- No file output except the on-demand `export` command.

## Prerequisites

- Python 3.10+
- A Schwab developer app in **Ready To Use** status with a registered
  redirect URI (defaults to `https://127.0.0.1`).
- A PostgreSQL instance reachable from the Unraid host.
- A database named `PortfolioDB` created **before** running setup:

  ```sql
  CREATE DATABASE "PortfolioDB";
  ```

## Install on Unraid

1. Open the Unraid terminal (via the UI or SSH).
2. Install `pip` if it is not already present:

   ```bash
   python3 -m ensurepip
   ```

3. Clone the repo into the Unraid `appdata` share:

   ```bash
   git clone https://github.com/YOUR_USERNAME/schwab-portfolio-tracker.git \
       /mnt/user/appdata/schwab-portfolio-tracker
   ```

4. Install the package:

   ```bash
   cd /mnt/user/appdata/schwab-portfolio-tracker
   pip3 install -e .
   ```

5. Run the setup wizard:

   ```bash
   schwab-tracker setup
   ```

## First-run setup

`schwab-tracker setup` is an interactive wizard that:

1. Prompts for Schwab OAuth credentials and writes them to `.env`.
2. Prompts for PostgreSQL connection details and writes them to `.env`.
3. Prompts for an optional Discord webhook URL.
4. Creates the `positions` table.
5. Runs the Schwab OAuth login flow.
6. Performs a test pull and prints the account and position counts found.
7. Prints the Unraid cron setup instructions.

## CLI reference

| Command                        | Description                                                                 |
|--------------------------------|-----------------------------------------------------------------------------|
| `schwab-tracker setup`         | Interactive first-run wizard (env + DB init + OAuth + test pull).           |
| `schwab-tracker auth login`    | Run the OAuth authorization flow and persist new tokens.                    |
| `schwab-tracker auth status`   | Print token ages and expiry, warn if under 24 hours remain.                 |
| `schwab-tracker auth refresh`  | Force an access-token refresh.                                              |
| `schwab-tracker config show`   | Print env values (secrets masked) and probe the DB.                         |
| `schwab-tracker db init`       | Create the `positions` table if it does not exist.                          |
| `schwab-tracker db test`       | Probe the database connection.                                              |
| `schwab-tracker pull`          | Full nightly pipeline: fetch positions, upsert, alert on gaps / re-auth.    |
| `schwab-tracker export`        | Write a markdown snapshot report. See below.                                |

### `schwab-tracker export`

| Option     | Default                                   | Description                          |
|------------|-------------------------------------------|--------------------------------------|
| `--date`   | Most recent `snapshot_date` in the table. | Snapshot date in `YYYY-MM-DD` form.  |
| `--output` | Current working directory.                | Output directory for the markdown.   |

The file is written as `portfolio_YYYY-MM-DD.md` and the full path is echoed
to stdout.

## Re-auth procedure (weekly)

Schwab refresh tokens expire every **7 days**. The nightly `pull` posts a
Discord alert at 6:00 PM whenever fewer than 24 hours remain on the refresh
token. Re-auth is manual:

1. SSH into the Unraid host from any machine that has a browser.
2. Run:

   ```bash
   schwab-tracker auth login
   ```

3. Copy the printed OAuth URL and open it in your local browser.
4. Approve access in the Schwab UI.
5. The browser will redirect to your registered redirect URI (typically
   `https://127.0.0.1/...`). The page may not load — that is expected.
6. Copy the **full redirect URL** from the browser's address bar and paste it
   back into the terminal prompt.
7. New tokens are written to `~/.schwab_tracker/tokens.json` (mode `600`).

> ⚠️ **Weekly re-auth window.** Schwab refresh tokens expire every 7 days.
> The tool alerts via Discord at the 6 PM pull when fewer than 24 hours
> remain. If the window closes, the next pull will fail and you must run
> `schwab-tracker auth login` before snapshots resume.

## Cron setup (Unraid User Scripts plugin)

1. Install the **User Scripts** plugin from Unraid Community Apps if it is
   not already installed.
2. Create a new script named `schwab-portfolio-pull` with this body:

   ```bash
   #!/bin/bash
   export PATH="/usr/local/bin:/usr/bin:/bin"
   cd /mnt/user/appdata/schwab-portfolio-tracker
   source .env
   /usr/local/bin/schwab-tracker pull >> /mnt/user/appdata/schwab-portfolio-tracker/pull.log 2>&1
   ```

3. Set the schedule to the custom cron expression:

   ```
   0 18 * * 1-5
   ```

   (6:00 PM, Monday through Friday.)

4. Log output is appended to
   `/mnt/user/appdata/schwab-portfolio-tracker/pull.log`.

## Gap detection

After each successful pull, the tool checks the last 30 days of
`snapshot_date` values and flags any missing **weekday** dates. Any gaps
found are broadcast via Discord:

```
⚠️ Missing portfolio snapshots detected
Gaps in last 30 days: 2026-04-16, 2026-04-17, ...
These dates cannot be backfilled from the Schwab API.
```

Missed dates are informational only — Schwab does not expose historical
position data, so gaps are permanent.

## PostgreSQL schema

```sql
CREATE TABLE IF NOT EXISTS positions (
    snapshot_date       DATE NOT NULL,
    account_number      TEXT NOT NULL,
    account_type        TEXT,
    symbol              TEXT NOT NULL,
    company_name        TEXT,
    asset_type          TEXT,
    quantity            NUMERIC,
    avg_cost            NUMERIC,
    cost_basis          NUMERIC,
    last_price          NUMERIC,
    market_value        NUMERIC,
    unrealized_pnl      NUMERIC,
    unrealized_pnl_pct  NUMERIC,
    day_pnl             NUMERIC,
    day_pnl_pct         NUMERIC,
    PRIMARY KEY (snapshot_date, account_number, symbol)
);
```

Notes:
- `symbol` holds the full OCC symbol for options positions.
- For options, `market_value = quantity * last_price * 100`.
- `company_name` comes from the `description` field on the quotes endpoint.

## Environment variables

Required keys (written to `.env` by the setup wizard):

```
SCHWAB_CLIENT_ID=
SCHWAB_CLIENT_SECRET=
PORTFOLIO_DB_HOST=localhost
PORTFOLIO_DB_PORT=5432
PORTFOLIO_DB_USER=postgres
PORTFOLIO_DB_PASSWORD=
PORTFOLIO_DB_NAME=PortfolioDB
DISCORD_WEBHOOK_URL=
```

Secrets live in `.env` only; the file is excluded from git via `.gitignore`.

## Token storage

Tokens are persisted to `~/.schwab_tracker/tokens.json` with mode `600`. The
file is never committed — it is covered by `.gitignore` both as `tokens.json`
and by being outside the repository.

## Operational troubleshooting

- **`db test` fails** — confirm the Unraid host can reach the Postgres host
  and port, and that `PortfolioDB` exists.
- **Pull fails with 401** — your refresh token has expired or been revoked.
  Run `schwab-tracker auth login`.
- **No Discord alerts arrive** — confirm `DISCORD_WEBHOOK_URL` is set
  correctly; alert failures intentionally never break the pull.
