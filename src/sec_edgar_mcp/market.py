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
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .apiclient import ApiError, JsonApiClient
from .filings import recent_filings
from .financials import summarize_financials
from .sec_client import SecClient, SecError, archive_url

CHANGE_LOOKBACK_SLACK_DAYS = 10


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
    start = end - timedelta(days=days)
    # A few extra days so the bar on or before the start date is always there (weekends, holidays).
    fetched = await daily_bars(client, ticker, start - timedelta(days=CHANGE_LOOKBACK_SLACK_DAYS), end)
    bars = [b for b in fetched if b["date"] >= start.isoformat()] or fetched

    closes = [b["close"] for b in bars]
    last = closes[-1]
    last_day = date.fromisoformat(bars[-1]["date"])

    def change_over(calendar_days: int) -> float | None:
        """Change since the last close on or before (last day - calendar_days)."""
        target = last_day - timedelta(days=calendar_days)
        earlier = [b for b in fetched if b["date"] <= target.isoformat()]
        if not earlier or (target - date.fromisoformat(earlier[-1]["date"])).days > CHANGE_LOOKBACK_SLACK_DAYS:
            return None  # not enough history (recent listing or a shorter window)
        base = earlier[-1]["close"]
        return round(100.0 * (last / base - 1), 1) if base else None

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
        "change_pct compares with the last close on or before the same calendar date 1/3/6/12 months back.",
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
            "1m": change_over(30), "3m": change_over(91), "6m": change_over(182), "1y": change_over(365),
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

SHARES_FACT = "dei:entitycommonstocksharesoutstanding"
_IX_FACT_RE = re.compile(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", re.I | re.S)
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_CONTEXT_RE = re.compile(
    r"<(?:[\w-]+:)?context\b[^>]*?\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:[\w-]+:)?context>", re.I | re.S
)
_MEMBER_RE = re.compile(r"<(?:[\w-]+:)?explicitMember\b[^>]*>\s*([^<\s]+)\s*<", re.I)
_INSTANT_RE = re.compile(r"<(?:[\w-]+:)?instant>\s*([^<\s]+)\s*<", re.I)
_COVER_FORMS = ("10-Q", "10-K")
_cover_cache: OrderedDict[str, dict[str, Any] | None] = OrderedDict()


def _ix_number(inner: str, attrs: dict[str, str]) -> float | None:
    """The value of one inline-XBRL ix:nonFraction element (format, scale and sign applied)."""
    text = re.sub(r"<[^>]+>", "", inner)
    text = re.sub(r"&[#\w]+;", " ", text).strip()
    fmt = attrs.get("format", "").lower()
    if "zero" in fmt or "numwordsen" in fmt or text in ("-", "—", "–"):
        return 0.0
    if "commadecimal" in fmt:  # 1.234.567,00
        text = text.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        text = text.replace(",", "").replace(" ", "")
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    try:
        value *= 10 ** int(attrs.get("scale", "0") or 0)
    except ValueError:
        pass
    return -value if attrs.get("sign") == "-" else value


def _class_label(members: list[str]) -> str:
    if not members:
        return "all classes"
    return ", ".join(m.split(":")[-1].removesuffix("Member") for m in members)


def parse_cover_shares(html: str) -> dict[str, Any] | None:
    """Shares outstanding per class from the cover page of an inline-XBRL 10-Q/10-K.

    SEC's companyfacts API drops facts that carry a dimension, and the per-class cover-page counts
    (StatementClassOfStockAxis) are exactly such facts. The filing itself has them all.
    """
    contexts: dict[str, dict[str, Any]] = {}
    for cid, body in _CONTEXT_RE.findall(html):
        instant = _INSTANT_RE.search(body)
        contexts[cid] = {"members": _MEMBER_RE.findall(body), "instant": instant.group(1) if instant else None}

    values: dict[str, float] = {}
    for attr_text, inner in _IX_FACT_RE.findall(html):
        attrs = {k.lower(): (a if a else b) for k, a, b in _ATTR_RE.findall(attr_text)}
        if attrs.get("name", "").lower() != SHARES_FACT:
            continue
        ctx = attrs.get("contextref")
        value = _ix_number(inner, attrs)
        if ctx and value is not None:
            values.setdefault(ctx, value)  # the same fact is sometimes shown twice
    if not values:
        return None

    # Per-class values win; a value without a dimension is then a total and would double count.
    per_class = {c: v for c, v in values.items() if contexts.get(c, {}).get("members")}
    chosen = per_class or values
    classes = sorted(
        ({"class": _class_label(contexts.get(c, {}).get("members", [])), "shares_m": round(v / 1e6, 2)}
         for c, v in chosen.items()),
        key=lambda r: -r["shares_m"],
    )
    as_of = next((contexts[c]["instant"] for c in chosen if contexts.get(c, {}).get("instant")), None)
    return {
        "shares_m": round(sum(chosen.values()) / 1e6, 2),
        "per_class_m": [c["shares_m"] for c in classes],
        "classes": classes,
        "as_of": as_of,
    }


async def cover_page_shares(client: SecClient, cik: int) -> dict[str, Any] | None:
    """Per-class shares outstanding from the latest 10-Q or 10-K, read from the filing itself."""
    subs = await client.submissions(cik)
    filing = next((f for f in recent_filings(subs) if f["form"].upper() in _COVER_FORMS), None)
    if filing is None:
        return None
    key = f"{cik}/{filing['accession']}"
    if key not in _cover_cache:
        try:
            html = await client.archive_text(cik, filing["accession"], filing["primary_document"], cache=False)
        except SecError:
            return None
        _cover_cache[key] = parse_cover_shares(html)
        while len(_cover_cache) > 128:
            _cover_cache.popitem(last=False)
    parsed = _cover_cache[key]
    if parsed is None:
        return None
    return {
        **parsed,
        "accession": filing["accession"],
        "form": filing["form"],
        "filed": filing["filed"],
        "url": archive_url(cik, filing["accession"], filing["primary_document"]),
    }


def shares_all_classes(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Fallback: shares outstanding from companyfacts (dei), summed over the values of the latest filing.

    companyfacts leaves out per-class (dimensional) facts, so for many Class A/B companies this
    returns one class or nothing: prefer cover_page_shares.
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


def valuation(
    quote: dict[str, Any], facts: dict[str, Any], cover: dict[str, Any] | None = None
) -> dict[str, Any]:
    """cover: result of cover_page_shares (per-class counts read from the filing). Without it the
    companyfacts cover-page values are used, which can miss share classes."""
    price = quote["price"]
    summary = summarize_financials(facts, years=1, quarters=6)
    ttm = trailing_four_quarters(summary["quarterly"])
    balance = summary["latest_balance_sheet"]
    notes: list[str] = []

    shares = cover
    share_source = f"cover page of {cover['form']} filed {cover['filed']} (all classes, inline XBRL)" if cover else None
    if shares is None:
        shares = shares_all_classes(facts)
        share_source = "cover page via companyfacts (may miss share classes)" if shares else None
    shares_m = shares["shares_m"] if shares else None
    latest_diluted = summary["quarterly"][0].get("diluted_shares_m") if summary["quarterly"] else None
    if shares_m is None and latest_diluted:
        shares_m = latest_diluted
        share_source = "diluted weighted shares, latest quarter (no cover-page count)"
    elif shares_m and latest_diluted and not 0.6 <= shares_m / latest_diluted <= 1.6:
        notes.append(
            f"Cover-page shares ({shares_m}M) and diluted weighted shares last quarter ({latest_diluted}M) differ a "
            "lot: a reverse split, restructuring, large issuance or a share class missing from EPS. Check the filing."
        )
    market_cap = None if shares_m is None else round(price * shares_m, 1)

    out: dict[str, Any] = {
        "price": price,
        "shares_outstanding_m": shares_m,
        "shares_source": share_source,
        "shares_detail": shares,
        "diluted_shares_latest_quarter_m": latest_diluted,
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
        out["eps_basis"] = "sum of reported diluted EPS, last four quarters"
        if ttm.get("net_income_musd") is not None and latest_diluted:
            # Summed EPS breaks across splits and IPOs (a derived Q4 mixes share counts); net income
            # over the latest diluted share count is on the same basis as today's price.
            eps = round(ttm["net_income_musd"] / latest_diluted, 2)
            out["eps_basis"] = "net income last four quarters / diluted shares latest quarter"
        out["eps_ttm"] = eps
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
