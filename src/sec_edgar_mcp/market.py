"""Stock prices and valuation.

Prices come from Nasdaq's public quote API (no key, covers NYSE, Nasdaq and NYSE American).
When Nasdaq does not answer and FINNHUB_API_KEY is set, Finnhub's free quote endpoint is used
for the last price. Valuation is computed from the price and the company's own SEC filings, so
share classes and earnings are the ones the company reported.
"""

from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import anyio

from .apiclient import ApiError, JsonApiClient
from .financials import summarize_financials

NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


class MarketError(ApiError):
    """A market-data error the caller can read."""


# ------------------------------------------------------------------ parsing helpers

def num(value: Any) -> float | None:
    """Parse '$1,234.50', '-2.3%', '+0.35', 'N/A', '' or a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    match = NUM_RE.search(str(value).replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def num_range(value: Any) -> tuple[float | None, float | None]:
    """'$66.00/$12.50' or '12.50 - 66.00' -> (low, high)."""
    numbers = [float(m.replace(",", "")) for m in NUM_RE.findall(str(value or "").replace("$", ""))]
    if len(numbers) < 2:
        return None, None
    return min(numbers[:2]), max(numbers[:2])


def nasdaq_symbol(ticker: str) -> str:
    """SEC writes class shares as BRK-B; Nasdaq's API uses BRK.B."""
    return ticker.upper().replace("-", ".").replace("/", ".")


def _ticker_key(symbol: str) -> str:
    return symbol.upper().replace("/", "-").replace(".", "-").strip()


def _nasdaq_data(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MarketError(f"Nasdaq returned no data for {what}.")
    status = payload.get("status") or {}
    data = payload.get("data")
    if status.get("rCode") not in (None, 200) or not data:
        messages = [m.get("errorMessage") or str(m) for m in (status.get("bCodeMessage") or []) if m]
        detail = "; ".join(messages) or "no data"
        raise MarketError(f"Nasdaq has no data for {what}: {detail}")
    return data


# ------------------------------------------------------------------ Nasdaq quote

async def nasdaq_quote(client: JsonApiClient, ticker: str) -> dict[str, Any]:
    symbol = nasdaq_symbol(ticker)
    payloads: dict[str, Any] = {}

    async def fetch(kind: str) -> None:
        payloads[kind] = await client.get_json(f"quote/{symbol}/{kind}", [("assetclass", "stocks")], ttl=60)

    async with anyio.create_task_group() as tg:
        tg.start_soon(fetch, "info")
        tg.start_soon(fetch, "summary")

    info = _nasdaq_data(payloads["info"], symbol)
    primary = info.get("primaryData") or {}
    price = num(primary.get("lastSalePrice"))
    if price is None:
        raise MarketError(f"Nasdaq returned no price for {symbol}.")
    key_stats = info.get("keyStats") or {}
    low52, high52 = num_range((key_stats.get("fiftyTwoWeekHighLow") or {}).get("value"))

    summary_raw: dict[str, Any] = {}
    try:
        summary_raw = _nasdaq_data(payloads["summary"], symbol).get("summaryData") or {}
    except MarketError:
        pass
    summary = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in summary_raw.items()}
    if high52 is None:
        low52, high52 = num_range(summary.get("FiftTwoWeekHighLow") or summary.get("FiftyTwoWeekHighLow"))

    return {
        "symbol": info.get("symbol") or symbol,
        "name": info.get("companyName"),
        "price": price,
        "change_pct": num(primary.get("percentageChange")),
        "price_time": primary.get("lastTradeTimestamp"),
        "real_time": primary.get("isRealTime"),
        "market_status": info.get("marketStatus"),
        "previous_close": num(summary.get("PreviousClose")),
        "week52_low": low52,
        "week52_high": high52,
        "volume": num(summary.get("ShareVolume")),
        "average_volume": num(summary.get("AverageVolume")),
        "market_cap_nasdaq_musd": None if num(summary.get("MarketCap")) is None
        else round(num(summary.get("MarketCap")) / 1e6, 1),
        "pe_nasdaq": num(summary.get("PERatio")),
        "forward_pe_nasdaq": num(summary.get("ForwardPE1Yr")),
        "analyst_target_1y": num(summary.get("OneYrTarget")),
        "exchange": summary.get("Exchange"),
        "sector": summary.get("Sector"),
        "industry": summary.get("Industry"),
        "source": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}",
    }


async def finnhub_quote(client: JsonApiClient, ticker: str) -> dict[str, Any]:
    data = await client.get_json("quote", [("symbol", ticker.upper())], ttl=60)
    price = num((data or {}).get("c"))
    if not price:
        raise MarketError(f"Finnhub has no price for {ticker}.")
    stamp = data.get("t")
    return {
        "symbol": ticker.upper(),
        "price": price,
        "change_pct": num(data.get("dp")),
        "previous_close": num(data.get("pc")),
        "price_time": datetime.fromtimestamp(stamp).isoformat(timespec="minutes") if stamp else None,
        "source": "https://finnhub.io",
        "note": "Finnhub fallback: no 52-week range, market cap or analyst target.",
    }


async def best_quote(nasdaq: JsonApiClient, finnhub: JsonApiClient, ticker: str) -> dict[str, Any]:
    try:
        return await nasdaq_quote(nasdaq, ticker)
    except ApiError as first:
        if not os.environ.get("FINNHUB_API_KEY", "").strip():
            raise MarketError(
                f"{first} Set FINNHUB_API_KEY in .env (free at finnhub.io) to fall back to Finnhub."
            ) from first
        try:
            quote = await finnhub_quote(finnhub, ticker)
        except ApiError as second:
            raise MarketError(f"No price: {first} / {second}") from second
        quote["nasdaq_error"] = str(first)
        return quote


# ------------------------------------------------------------------ SEC side of valuation

def shares_all_classes(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Shares outstanding from the latest cover page, summed over share classes.

    Companies with several classes (Class A/B, Up-C structures) report one cover-page value per
    class in the same filing; a single value would understate the market cap.
    """
    node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    entries = (node or {}).get("units", {}).get("shares", [])
    if not entries:
        return None
    latest = max(entries, key=lambda f: (f.get("filed", ""), f.get("end", "")))
    same = [f for f in entries if f.get("accn") == latest.get("accn") and f.get("end") == latest.get("end")]
    values = sorted({f["val"] for f in same}, reverse=True)
    return {
        "shares_m": round(sum(values) / 1e6, 2),
        "per_class_m": [round(v / 1e6, 2) for v in values],
        "as_of": latest.get("end"),
        "accession": latest.get("accn"),
    }


def trailing_four_quarters(quarterly: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum the latest four consecutive quarters (rows are newest first)."""
    rows = [r for r in quarterly if r.get("revenue_musd") is not None or r.get("net_income_musd") is not None][:4]
    if len(rows) < 4:
        return None
    span = (date.fromisoformat(rows[0]["period_end"]) - date.fromisoformat(rows[3]["period_end"])).days
    if not 250 <= span <= 300:
        return None  # a quarter is missing, so four rows do not make a year

    def total(key: str) -> float | None:
        values = [r.get(key) for r in rows]
        return None if any(v is None for v in values) else round(sum(values), 2)

    return {
        "period_end": rows[0]["period_end"],
        "quarters": [r["period_end"] for r in rows],
        "revenue_musd": total("revenue_musd"),
        "net_income_musd": total("net_income_musd"),
        "eps_diluted": total("eps_diluted"),
        "operating_cash_flow_musd": total("operating_cash_flow_musd"),
        "free_cash_flow_musd": total("free_cash_flow_musd"),
        "estimated_q4": any(r.get("note") for r in rows),
    }


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 1)


def valuation(quote: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    price = quote["price"]
    summary = summarize_financials(facts, years=1, quarters=6)
    ttm = trailing_four_quarters(summary["quarterly"])
    shares = shares_all_classes(facts)
    balance = summary["latest_balance_sheet"]
    notes: list[str] = []

    share_source = "cover page (all classes)"
    shares_m = shares["shares_m"] if shares else None
    if shares_m is None and summary["quarterly"]:
        shares_m = summary["quarterly"][0].get("diluted_shares_m")
        share_source = "diluted weighted shares, latest quarter (no cover-page count)"
    market_cap = None if shares_m is None else round(price * shares_m, 1)

    cash = (balance.get("cash") or {}).get("value_musd")
    out: dict[str, Any] = {
        "price": price,
        "shares_outstanding_m": shares_m,
        "shares_source": share_source,
        "shares_detail": shares,
        "market_cap_musd": market_cap,
        "trailing_12m": ttm,
        "pe_ttm": None,
        "ps_ttm": None,
        "price_to_fcf_ttm": None,
        "cash_musd": cash,
        "balance_sheet_date": summary["balance_sheet_date"],
    }
    if ttm:
        eps = ttm.get("eps_diluted")
        out["pe_ttm"] = _ratio(price, eps)
        out["ps_ttm"] = _ratio(market_cap, ttm.get("revenue_musd"))
        out["price_to_fcf_ttm"] = _ratio(market_cap, ttm.get("free_cash_flow_musd"))
        if eps is not None and eps <= 0:
            notes.append("Trailing EPS is zero or negative, so there is no meaningful P/E.")
        if ttm.get("estimated_q4"):
            notes.append("One quarter in the trailing 12 months is derived from the annual report.")
    else:
        notes.append("Could not build four consecutive quarters, so no trailing P/E or P/S: use get_financials.")

    nasdaq_cap = quote.get("market_cap_nasdaq_musd")
    if market_cap and nasdaq_cap and abs(market_cap - nasdaq_cap) / market_cap > 0.15:
        notes.append(
            f"Market cap from SEC share count ({market_cap:,.0f}M) differs from Nasdaq's ({nasdaq_cap:,.0f}M). "
            "Usually several share classes (Nasdaq counts one) or a stale share count: check shares_detail."
        )
    if shares and len(shares["per_class_m"]) > 1:
        notes.append(
            f"{len(shares['per_class_m'])} share classes summed. P/E uses reported diluted EPS, which is the "
            "consistent measure when some classes do not share in net income (Up-C)."
        )
    out["notes"] = notes
    return out


# ------------------------------------------------------------------ price history

def _parse_date(text: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


async def price_history(
    client: JsonApiClient, ticker: str, days: int = 365, max_points: int = 60, today: date | None = None
) -> dict[str, Any]:
    symbol = nasdaq_symbol(ticker)
    end = today or date.today()
    start = end - timedelta(days=days)
    payload = await client.get_json(
        f"quote/{symbol}/historical",
        [("assetclass", "stocks"), ("fromdate", start.isoformat()), ("todate", end.isoformat()), ("limit", "9999")],
        ttl=3600,
    )
    data = _nasdaq_data(payload, symbol)
    table = data.get("tradesTable") or {}
    raw_rows = table.get("rows") or []
    bars = []
    for row in raw_rows:
        day = _parse_date(row.get("date", ""))
        close = num(row.get("close"))
        if day is None or close is None:
            continue
        bars.append({"date": day.isoformat(), "close": close, "high": num(row.get("high")),
                     "low": num(row.get("low")), "volume": num(row.get("volume"))})
    bars.sort(key=lambda b: b["date"])
    if not bars:
        raise MarketError(f"Nasdaq returned no price history for {symbol}.")

    closes = [b["close"] for b in bars]
    last = closes[-1]

    def change_over(trading_days: int) -> float | None:
        if len(closes) <= trading_days:
            return None
        return round(100.0 * (last / closes[-1 - trading_days] - 1), 1)

    high_bar = max(bars, key=lambda b: b["high"] or b["close"])
    low_bar = min(bars, key=lambda b: b["low"] or b["close"])
    high = high_bar["high"] or high_bar["close"]
    low = low_bar["low"] or low_bar["close"]
    daily = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    volatility = None
    if len(daily) > 10:
        mean = sum(daily) / len(daily)
        volatility = round(100 * math.sqrt(sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)) * math.sqrt(252), 1)
    volumes = [b["volume"] for b in bars[-20:] if b["volume"] is not None]

    step = max(1, math.ceil(len(bars) / max_points))
    sampled = bars[::-1][::step][::-1]  # always keep the latest bar
    return {
        "symbol": symbol,
        "from": bars[0]["date"],
        "to": bars[-1]["date"],
        "trading_days": len(bars),
        "last_close": last,
        "change_pct": {
            "1m": change_over(21), "3m": change_over(63), "6m": change_over(126), "1y": change_over(252),
            "whole_window": round(100.0 * (last / closes[0] - 1), 1),
        },
        "high": {"price": high, "date": high_bar["date"]},
        "low": {"price": low, "date": low_bar["date"]},
        "below_high_pct": round(100.0 * (last / high - 1), 1),
        "above_low_pct": round(100.0 * (last / low - 1), 1),
        "annualized_volatility_pct": volatility,
        "average_volume_20d": round(sum(volumes) / len(volumes)) if volumes else None,
        "closes": [{"date": b["date"], "close": b["close"]} for b in sampled],
        "notes": [
            f"closes is sampled every {step} trading day(s) to keep the answer short; statistics use every day.",
            "If the window starts before the listing date, the history starts at the IPO.",
        ],
        "source": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}/historical",
    }


# ------------------------------------------------------------------ whole market

async def market_caps(client: JsonApiClient) -> dict[str, dict[str, Any]]:
    """Price and market cap for every US-listed stock in one request, keyed by SEC-style ticker."""
    payload = await client.get_json(
        "screener/stocks",
        [("tableonly", "true"), ("limit", "25"), ("offset", "0"), ("download", "true")],
        ttl=900,
    )
    data = _nasdaq_data(payload, "the stock screener")
    rows = data.get("rows") or (data.get("table") or {}).get("rows") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        if not symbol:
            continue
        cap = num(row.get("marketCap"))
        out[_ticker_key(symbol)] = {
            "price": num(row.get("lastsale")),
            "market_cap_musd": None if not cap else round(cap / 1e6, 1),
            "sector": row.get("sector") or None,
            "industry": row.get("industry") or None,
            "ipo_year": row.get("ipoyear") or None,
        }
    if not out:
        raise MarketError("Nasdaq's stock screener returned no rows.")
    return out


def lookup_market(caps: dict[str, dict[str, Any]], ticker: str | None) -> dict[str, Any] | None:
    return caps.get(_ticker_key(ticker)) if ticker else None
