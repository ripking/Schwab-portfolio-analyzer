"""Fetch positions from Schwab Trader API and enrich them with live quotes."""

from __future__ import annotations

from datetime import date
from typing import Any

import requests

from .auth import TokenExpiredError

ACCOUNTS_URL = "https://api.schwabapi.com/trader/v1/accounts"
QUOTES_URL = "https://api.schwabapi.com/marketdata/v1/quotes"

QUOTE_BATCH_SIZE = 500
REQUEST_TIMEOUT_SEC = 60


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def _raise_for_status(response: requests.Response, context: str) -> None:
    if response.status_code == 401:
        raise TokenExpiredError(
            "Refresh token expired or revoked. Run: schwab-tracker auth login"
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"{context} failed ({response.status_code}): {response.text[:500]}"
        )


def _fetch_accounts(access_token: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            ACCOUNTS_URL,
            headers=_auth_headers(access_token),
            params={"fields": "positions"},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Schwab accounts request errored: {exc}") from exc
    _raise_for_status(response, "Schwab accounts fetch")
    return response.json() or []


def _fetch_quotes(access_token: str, symbols: list[str]) -> dict[str, Any]:
    if not symbols:
        return {}
    quotes: dict[str, Any] = {}
    for start in range(0, len(symbols), QUOTE_BATCH_SIZE):
        batch = symbols[start : start + QUOTE_BATCH_SIZE]
        try:
            response = requests.get(
                QUOTES_URL,
                headers=_auth_headers(access_token),
                params={"symbols": ",".join(batch)},
                timeout=REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Schwab quotes request errored: {exc}") from exc
        _raise_for_status(response, "Schwab quotes fetch")
        payload = response.json() or {}
        if isinstance(payload, dict):
            quotes.update(payload)
    return quotes


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quote_fields(quote: dict[str, Any] | None) -> dict[str, Any]:
    if not quote:
        return {
            "company_name": None,
            "last_price": 0.0,
            "net_change": 0.0,
            "net_change_pct": 0.0,
        }
    reference = quote.get("reference") or {}
    regular = quote.get("regular") or {}
    quote_block = quote.get("quote") or {}

    description = (
        reference.get("description")
        or quote.get("description")
        or quote_block.get("description")
    )
    last_price = (
        quote_block.get("lastPrice")
        if quote_block.get("lastPrice") is not None
        else regular.get("regularMarketLastPrice")
    )
    net_change = (
        quote_block.get("netChange")
        if quote_block.get("netChange") is not None
        else regular.get("regularMarketNetChange")
    )
    net_change_pct = (
        quote_block.get("netPercentChange")
        if quote_block.get("netPercentChange") is not None
        else regular.get("regularMarketPercentChange")
    )
    return {
        "company_name": description,
        "last_price": _as_float(last_price),
        "net_change": _as_float(net_change),
        "net_change_pct": _as_float(net_change_pct),
    }


def _extract_account(account_wrapper: dict[str, Any]) -> dict[str, Any]:
    account = account_wrapper.get("securitiesAccount") or account_wrapper
    return {
        "account_number": str(account.get("accountNumber", "")),
        "account_type": account.get("type"),
        "positions": account.get("positions", []) or [],
    }


def _position_symbol(instrument: dict[str, Any]) -> str:
    return (
        instrument.get("symbol")
        or instrument.get("cusip")
        or ""
    )


def _position_asset_type(instrument: dict[str, Any]) -> str:
    asset_type = (instrument.get("assetType") or instrument.get("type") or "").upper()
    if asset_type in ("OPTION", "OPT"):
        return "OPTION"
    return asset_type or "EQUITY"


def _position_quantity(position: dict[str, Any]) -> float:
    long_qty = _as_float(position.get("longQuantity"))
    short_qty = _as_float(position.get("shortQuantity"))
    return long_qty - short_qty


def get_all_positions(access_token: str) -> list[dict[str, Any]]:
    """Return the current flattened position snapshot across all accounts."""
    accounts_payload = _fetch_accounts(access_token)
    accounts = [_extract_account(a) for a in accounts_payload]

    symbols: list[str] = []
    seen: set[str] = set()
    for account in accounts:
        for position in account["positions"]:
            instrument = position.get("instrument") or {}
            symbol = _position_symbol(instrument)
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

    quotes = _fetch_quotes(access_token, symbols)

    snapshot_date = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    for account in accounts:
        for position in account["positions"]:
            instrument = position.get("instrument") or {}
            symbol = _position_symbol(instrument)
            if not symbol:
                continue
            asset_type = _position_asset_type(instrument)
            quantity = _position_quantity(position)
            avg_cost = _as_float(position.get("averagePrice"))
            cost_basis = quantity * avg_cost

            quote = _quote_fields(quotes.get(symbol))
            company_name = (
                quote["company_name"]
                or instrument.get("description")
                or None
            )
            last_price = quote["last_price"]

            if asset_type == "OPTION":
                market_value = quantity * last_price * 100
            else:
                market_value = quantity * last_price

            unrealized_pnl = market_value - cost_basis
            unrealized_pnl_pct = (
                (unrealized_pnl / cost_basis * 100.0) if cost_basis else 0.0
            )
            day_pnl = _as_float(position.get("currentDayProfitLoss"))
            day_pnl_pct = _as_float(position.get("currentDayProfitLossPercentage"))

            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "account_number": account["account_number"],
                    "account_type": account["account_type"],
                    "symbol": symbol,
                    "company_name": company_name,
                    "asset_type": asset_type,
                    "quantity": quantity,
                    "avg_cost": avg_cost,
                    "cost_basis": cost_basis,
                    "last_price": last_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "day_pnl": day_pnl,
                    "day_pnl_pct": day_pnl_pct,
                }
            )
    return rows
