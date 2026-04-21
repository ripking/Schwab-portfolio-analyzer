"""PostgreSQL persistence layer for portfolio snapshots."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import psycopg2
from psycopg2.extensions import connection as PGConnection
from psycopg2.extras import execute_values

DEFAULT_DB_NAME = "PortfolioDB"
DEFAULT_DB_PORT = "5432"

POSITION_COLUMNS = (
    "snapshot_date",
    "account_number",
    "account_type",
    "symbol",
    "company_name",
    "asset_type",
    "quantity",
    "avg_cost",
    "cost_basis",
    "last_price",
    "market_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "day_pnl",
    "day_pnl_pct",
)

PRIMARY_KEY = ("snapshot_date", "account_number", "symbol")

CREATE_TABLE_SQL = """
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
"""


def _db_config() -> dict[str, str]:
    host = os.environ.get("PORTFOLIO_DB_HOST")
    user = os.environ.get("PORTFOLIO_DB_USER")
    password = os.environ.get("PORTFOLIO_DB_PASSWORD", "")
    if not host or not user:
        raise RuntimeError(
            "PORTFOLIO_DB_HOST and PORTFOLIO_DB_USER must be set in .env"
        )
    return {
        "host": host,
        "port": os.environ.get("PORTFOLIO_DB_PORT", DEFAULT_DB_PORT),
        "user": user,
        "password": password,
        "dbname": os.environ.get("PORTFOLIO_DB_NAME", DEFAULT_DB_NAME),
    }


def get_db_connection() -> PGConnection:
    cfg = _db_config()
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["dbname"],
        connect_timeout=10,
    )


def init_db(conn: PGConnection) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()


def upsert_positions(positions: Iterable[dict[str, Any]], conn: PGConnection) -> int:
    rows = [tuple(p.get(col) for col in POSITION_COLUMNS) for p in positions]
    if not rows:
        return 0
    non_key_cols = [c for c in POSITION_COLUMNS if c not in PRIMARY_KEY]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_key_cols)
    col_list = ", ".join(POSITION_COLUMNS)
    pk_list = ", ".join(PRIMARY_KEY)
    sql = (
        f"INSERT INTO positions ({col_list}) VALUES %s "
        f"ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause}"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
        affected = cur.rowcount
    conn.commit()
    return affected if affected >= 0 else len(rows)


def get_snapshot_gaps(conn: PGConnection, lookback_days: int = 30) -> list[str]:
    """Return weekday dates in the lookback window with no snapshot row."""
    today = date.today()
    start = today - timedelta(days=lookback_days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT snapshot_date FROM positions "
            "WHERE snapshot_date >= %s AND snapshot_date <= %s",
            (start, today),
        )
        existing = {row[0] for row in cur.fetchall()}

    gaps: list[str] = []
    day = start
    while day <= today:
        # Schwab markets are closed on weekends; only flag weekday gaps.
        if day.weekday() < 5 and day not in existing:
            gaps.append(day.isoformat())
        day += timedelta(days=1)
    return gaps


def test_db_connection(print_fn=print) -> bool:
    try:
        cfg = _db_config()
    except RuntimeError as exc:
        print_fn(f"✗ DB config error: {exc}")
        return False
    try:
        conn = get_db_connection()
    except Exception as exc:  # noqa: BLE001 — connection errors vary by driver
        print_fn(
            f"✗ Cannot connect to {cfg['dbname']} at {cfg['host']}:{cfg['port']} — {exc}"
        )
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        print_fn(f"✓ Connected to {cfg['dbname']} at {cfg['host']}:{cfg['port']}")
        return True
    except Exception as exc:  # noqa: BLE001
        print_fn(f"✗ Query failed on {cfg['dbname']}: {exc}")
        return False
    finally:
        conn.close()


def get_latest_snapshot_date(conn: PGConnection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_date) FROM positions")
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return row[0].isoformat() if isinstance(row[0], date) else str(row[0])


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"${sign}{abs(value):,.2f}"


def _fmt_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):,.2f}%"


def _fmt_qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _fmt_account_mask(account_number: str) -> str:
    digits = "".join(ch for ch in account_number if ch.isdigit())
    tail = digits[-4:] if digits else account_number[-4:]
    return f"...{tail}"


def _format_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def export_markdown(date_str: str, conn: PGConnection) -> str | None:
    """Return a markdown report for the given snapshot_date, or None if empty."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(POSITION_COLUMNS)} FROM positions "
            f"WHERE snapshot_date = %s",
            (date_str,),
        )
        rows = cur.fetchall()

    if not rows:
        return None

    records = [dict(zip(POSITION_COLUMNS, row)) for row in rows]
    for record in records:
        for key in (
            "quantity",
            "avg_cost",
            "cost_basis",
            "last_price",
            "market_value",
            "unrealized_pnl",
            "unrealized_pnl_pct",
            "day_pnl",
            "day_pnl_pct",
        ):
            record[key] = float(record.get(key) or 0.0)

    accounts: dict[str, dict[str, Any]] = {}
    for rec in records:
        acct_key = rec["account_number"]
        bucket = accounts.setdefault(
            acct_key,
            {
                "account_number": acct_key,
                "account_type": rec.get("account_type"),
                "positions": [],
            },
        )
        bucket["positions"].append(rec)

    formatted_date = _format_date(date_str)
    lines: list[str] = [f"# Portfolio Snapshot — {formatted_date}", ""]

    portfolio_market_value = 0.0
    portfolio_cost_basis = 0.0
    portfolio_unrealized_pnl = 0.0
    portfolio_day_pnl = 0.0
    position_count = 0

    for acct_key in sorted(accounts.keys()):
        acct = accounts[acct_key]
        positions = sorted(
            acct["positions"], key=lambda p: p["market_value"], reverse=True
        )
        acct_mv = sum(p["market_value"] for p in positions)
        acct_cb = sum(p["cost_basis"] for p in positions)
        acct_upnl = sum(p["unrealized_pnl"] for p in positions)
        acct_dpnl = sum(p["day_pnl"] for p in positions)
        acct_upnl_pct = (acct_upnl / acct_cb * 100.0) if acct_cb else 0.0

        portfolio_market_value += acct_mv
        portfolio_cost_basis += acct_cb
        portfolio_unrealized_pnl += acct_upnl
        portfolio_day_pnl += acct_dpnl
        position_count += len(positions)

        account_type = acct.get("account_type") or "UNKNOWN"
        mask = _fmt_account_mask(acct["account_number"])
        lines.append(f"## Account: {mask} ({account_type})")
        lines.append("")
        lines.append(
            "| Company | Ticker | Last Price | Shares | Position Size | "
            "Avg Cost | Cost Basis | Unrealized P&L | P&L % | Day P&L |"
        )
        lines.append(
            "|---------|--------|------------|--------|---------------|"
            "----------|------------|----------------|-------|---------|"
        )
        for p in positions:
            lines.append(
                "| {company} | {ticker} | {last} | {shares} | {mv} | "
                "{avg} | {cb} | {upnl} | {upnl_pct} | {dpnl} |".format(
                    company=(p.get("company_name") or "").replace("|", "\\|"),
                    ticker=p["symbol"],
                    last=_fmt_money(p["last_price"]),
                    shares=_fmt_qty(p["quantity"]),
                    mv=_fmt_money(p["market_value"]),
                    avg=_fmt_money(p["avg_cost"]),
                    cb=_fmt_money(p["cost_basis"]),
                    upnl=_fmt_signed_money(p["unrealized_pnl"]),
                    upnl_pct=_fmt_signed_pct(p["unrealized_pnl_pct"]),
                    dpnl=_fmt_signed_money(p["day_pnl"]),
                )
            )
        lines.append("")
        lines.append(f"**Account Total Market Value:** {_fmt_money(acct_mv)}")
        lines.append(
            f"**Account Unrealized P&L:** {_fmt_signed_money(acct_upnl)} "
            f"({_fmt_signed_pct(acct_upnl_pct)})"
        )
        lines.append(f"**Account Day P&L:** {_fmt_signed_money(acct_dpnl)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    portfolio_upnl_pct = (
        (portfolio_unrealized_pnl / portfolio_cost_basis * 100.0)
        if portfolio_cost_basis
        else 0.0
    )

    lines.append("## Portfolio Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total Market Value | {_fmt_money(portfolio_market_value)} |")
    lines.append(f"| Total Cost Basis | {_fmt_money(portfolio_cost_basis)} |")
    lines.append(
        f"| Total Unrealized P&L | {_fmt_signed_money(portfolio_unrealized_pnl)} |"
    )
    lines.append(
        f"| Total Unrealized P&L % | {_fmt_signed_pct(portfolio_upnl_pct)} |"
    )
    lines.append(f"| Total Day P&L | {_fmt_signed_money(portfolio_day_pnl)} |")
    lines.append(f"| Snapshot Date | {formatted_date} |")
    lines.append(f"| Accounts | {len(accounts)} |")
    lines.append(f"| Positions | {position_count} |")
    lines.append("")

    return "\n".join(lines)
