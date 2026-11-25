"""Stock prices and valuation.

Prices come from Massive (formerly Polygon.io). The free Stocks Basic plan gives end-of-day bars
(the previous trading day), two years of history and the closing price of every US stock in one
request, at 5 requests per minute. With FINNHUB_API_KEY set, get_quote also shows Finnhub's
real-time last price. Valuation is computed from the price and the company's own SEC filings, so
share classes and earnings are the ones the company reported.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .apiclient import ApiError, JsonApiClient
from .financials import summarize_financials


class MarketError(ApiError):
    """A market-data error the caller can read."""


def massive_symbol(ticker: str) -> str:
    """SEC writes class shares as BRK-B; Massive uses BRK.B."""
    return ticker.upper().replace("-", ".").replace("/", ".")


def _ticker_key(symbol: str) -> str:
    return symbol.upper().replace("/", "-").replace(".", "-").strip()


def _has_key(env: str) -> bool:
    return bool(os.environ.get(env, "").strip())


def _bar_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


# ------------------------------------------------------------------ Massive

async def daily_bars(client: JsonApiClient, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
    symbol = massive_symbol(ticker)
    data = await client.get_json(
        f"v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}",
        [("adjusted", "true"), ("sort", "asc"), ("limit", "50000")],
        ttl=3 * 3600,
    )
    bars = [
        {"date": _bar_date(r["t"]), "open": r.get("o"), "high": r.get("h"), "low": r.get("l"),
         "close": r["c"], "volume": r.get("v")}
        for r in (data or {}).get("results") or []
        if r.get("c") is not None and r.get("t") is not None
    ]
    if not bars:
        raise MarketError(f"Massive has no daily prices for {symbol} between {start} and {end}.")
    return bars


async def finnhub_quote(client: JsonApiClient, ticker: str) -> dict[str, Any]:
    data = await client.get_json("quote", [("symbol", ticker.upper())], ttl=60)
    price = (data or {}).get("c")
    if not price:
        raise MarketError(f"Finnhub has no price for {ticker}.")
    stamp = data.get("t")
    return {
        "price": float(price),
        "change_pct": data.get("dp"),
        "previous_close": data.get("pc"),
        "price_time": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="minutes") if stamp else None,
    }


async def best_quote(
    massive: JsonApiClient, finnhub: JsonApiClient, ticker: str, today: date | None = None
) -> dict[str, Any]:
    """Last close and 52-week range from Massive; real-time price from Finnhub when it has a key."""
    end = (today or date.today()) - timedelta(days=1)  # the free plan refuses the current day
    quote: dict[str, Any] = {"symbol": massive_symbol(ticker)}
    errors = []

    if _has_key("MASSIVE_API_KEY"):
        try:
            bars = await daily_bars(massive, ticker, end - timedelta(days=371), end)
            year = [b for b in bars if b["date"] >= (end - timedelta(days=365)).isoformat()]
            last = bars[-1]
            quote.update({
                "price": last["close"],
                "price_time": f"{last['date']} close",
                "price_source": "Massive (end of day)",
                "previous_close": bars[-2]["close"] if len(bars) > 1 else None,
                "week52_high": max(b["high"] or b["close"] for b in year),
                "week52_low": min(b["low"] or b["close"] for b in year),
                "average_volume_20d": round(sum(b["volume"] or 0 for b in bars[-20:]) / len(bars[-20:])),
                "history_starts": bars[0]["date"],
            })
            if quote["previous_close"]:
                quote["change_pct"] = round(100 * (last["close"] / quote["previous_close"] - 1), 2)
        except ApiError as exc:
            errors.append(str(exc))

    if _has_key("FINNHUB_API_KEY"):
        try:
            live = await finnhub_quote(finnhub, ticker)
            quote.update({
                "price": live["price"],
                "price_time": live["price_time"],
                "price_source": "Finnhub (real time)",
                "change_pct": live["change_pct"],
                "previous_close": live["previous_close"],
            })
        except ApiError as exc:
            errors.append(str(exc))

    if "price" not in quote:
        if not errors:
            raise MarketError(
                "No price source configured. Add MASSIVE_API_KEY=<key> (free Stocks Basic plan at massive.com) "
                "to the .env file, then restart the Claude app."
            )
        raise MarketError("No price: " + " / ".join(errors))
    if errors:
        quote["warnings"] = errors
    if quote.get("week52_high"):
        quote["below_52w_high_pct"] = round(100 * (quote["price"] / quote["week52_high"] - 1), 1)
    return quote


async def price_history(
    client: JsonApiClient, ticker: str, days: int = 365, max_points: int = 60, today: date | None = None
) -> dict[str, Any]:
    if not _has_key("MASSIVE_API_KEY"):
        raise MarketError("Price history needs MASSIVE_API_KEY in .env (free Stocks Basic plan at massive.com).")
    end = (today or date.today()) - timedelta(days=1)  # the free plan refuses the current day
    bars = await daily_bars(client, ticker, end - timedelta(days=days), end)

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
    symbol = massive_symbol(ticker)
    notes = [
        f"closes is sampled every {step} trading day(s) to keep the answer short; statistics use every day.",
        "Prices are split-adjusted daily closes; the free plan ends at the previous trading day.",
    ]
    if days > 730:
        notes.append("The free Massive plan keeps about two years of history.")
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
        "notes": notes,
        "source": f"https://massive.com/quote/{symbol}",
    }


async def all_closes(client: JsonApiClient, today: date | None = None) -> dict[str, Any]:
    """Closing price of every US stock on the latest trading day the free plan has (one request)."""
    if not _has_key("MASSIVE_API_KEY"):
        raise MarketError("Valuation in the screener needs MASSIVE_API_KEY in .env (free plan at massive.com).")
    day = (today or date.today()) - timedelta(days=1)
    tried = []
    while len(tried) < 4:
        if day.weekday() >= 5:
            day -= timedelta(days=1)
            continue
        tried.append(day.isoformat())
        try:
            data = await client.get_json(
                f"v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}", [("adjusted", "true")], ttl=6 * 3600
            )
        except ApiError as exc:
            if "403" not in str(exc) and "NOT_AUTHORIZED" not in str(exc):
                raise
            data = None  # day not released yet on the free plan
        results = (data or {}).get("results") or []
        if results:
            prices = {_ticker_key(r["T"]): r["c"] for r in results if r.get("T") and r.get("c")}
            return {"date": day.isoformat(), "prices": prices}
        day -= timedelta(days=1)
    raise MarketError(f"Massive returned no market-wide prices for {', '.join(tried)}.")


def lookup_price(prices: dict[str, float], ticker: str | None) -> float | None:
    return prices.get(_ticker_key(ticker)) if ticker else None


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
        "cash_musd": (balance.get("cash") or {}).get("value_musd"),
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
    if shares and len(shares["per_class_m"]) > 1:
        notes.append(
            f"{len(shares['per_class_m'])} share classes summed. P/E uses reported diluted EPS, which is the "
            "consistent measure when some classes do not share in net income (Up-C)."
        )
    out["notes"] = notes
    return out
