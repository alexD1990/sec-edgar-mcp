from __future__ import annotations

import json
from datetime import date, timedelta

import anyio
import httpx2
import pytest

from fake_sec import fact, transport
from sec_edgar_mcp import market, screener
from sec_edgar_mcp.apiclient import ApiError, JsonApiClient
from sec_edgar_mcp.sec_client import SecClient, set_client

SECRET = "finnhubsecret999"


def run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


def ok(payload, status=200):
    return httpx2.Response(status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})


def nasdaq_ok(data):
    return ok({"data": data, "message": None, "status": {"rCode": 200, "bCodeMessage": None}})


# ------------------------------------------------------------------ fake Nasdaq

def history_rows(today: date, n: int = 300):
    rows, day, price = [], today, 30.0
    while len(rows) < n:
        if day.weekday() < 5:
            rows.append({"date": day.strftime("%m/%d/%Y"), "close": f"${price:,.2f}", "volume": "1,000,000",
                         "open": "$1", "high": f"${price * 1.01:,.2f}", "low": f"${price * 0.99:,.2f}"})
            price = price / 1.002  # walking back in time: prices were lower before
        day -= timedelta(days=1)
    rows[150]["close"], rows[150]["high"] = "$60.00", "$66.00"  # the peak
    return rows


def nasdaq_handler(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if "User-Agent" not in request.headers or "Mozilla" not in request.headers["User-Agent"]:
        return httpx2.Response(403)
    if path.endswith("/quote/BLOCKED/info") or path.endswith("/quote/BLOCKED/summary"):
        return ok({"data": None, "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists"}]}})
    if path.endswith("/quote/UPCO/info"):
        return nasdaq_ok({"symbol": "UPCO", "companyName": "Up-C Holdings Class A", "marketStatus": "Closed",
                          "primaryData": {"lastSalePrice": "$20.00", "percentageChange": "-1.5%",
                                          "lastTradeTimestamp": "Nov 24, 2026", "isRealTime": False},
                          "keyStats": {"fiftyTwoWeekHighLow": {"label": "52 Week Range:", "value": "12.50 - 66.00"}}})
    if path.endswith("/quote/UPCO/summary"):
        return nasdaq_ok({"summaryData": {
            "MarketCap": {"label": "Market Cap", "value": "2,000,000,000"},  # Class A only
            "PERatio": {"label": "P/E Ratio", "value": 25.0},
            "OneYrTarget": {"label": "1 Year Target", "value": "$40.00"},
            "AverageVolume": {"label": "Average Volume", "value": "3,000,000"},
            "Sector": {"label": "Sector", "value": "Industrials"},
        }})
    if path.endswith("/quote/UPCO/historical"):
        return nasdaq_ok({"symbol": "UPCO", "tradesTable": {"rows": history_rows(date(2026, 11, 24))}})
    if path.endswith("/screener/stocks"):
        assert request.url.params["download"] == "true"
        return nasdaq_ok({"rows": [
            {"symbol": "ACME", "lastsale": "$48.00", "marketCap": "2400000000.00", "sector": "Technology"},
            {"symbol": "SWTH", "lastsale": "$9.60", "marketCap": "480000000.00", "sector": "Industrials"},
            {"symbol": "SHRK", "lastsale": "$1.00", "marketCap": "", "sector": ""},
        ]})
    return httpx2.Response(404)


def finnhub_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.params.get("token") != SECRET:
        return ok({"error": f"Invalid API key {request.url.params.get('token')}"}, 401)
    return ok({"c": 19.5, "d": -0.3, "dp": -1.5, "h": 20, "l": 19, "o": 19.8, "pc": 19.8, "t": 1795651200})


@pytest.fixture
def nasdaq():
    return JsonApiClient("Nasdaq", "https://api.nasdaq.com/api", None, headers=market_headers(),
                         max_requests_per_second=1000, transport=httpx2.MockTransport(nasdaq_handler))


@pytest.fixture
def finnhub():
    return JsonApiClient("Finnhub", "https://finnhub.io/api/v1", "FINNHUB_API_KEY", key_param="token",
                         max_requests_per_second=1000, transport=httpx2.MockTransport(finnhub_handler))


def market_headers():
    from sec_edgar_mcp.apiclient import NASDAQ_HEADERS
    return NASDAQ_HEADERS


# ------------------------------------------------------------------ fake SEC facts for an Up-C company

def quarter(start, end, val, filed, fp, fy=2026):
    return fact(start, end, val, filed, fy=fy, fp=fp, accn=f"0002-{end}")


UPCO_FACTS = {
    "cik": 2000001,
    "entityName": "Up-C Holdings",
    "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            fact(None, "2026-07-31", 90_000_000, "2026-08-05", accn="0002-old"),
            fact(None, "2026-10-31", 100_000_000, "2026-11-05", accn="0002-new"),  # Class A
            fact(None, "2026-10-31", 30_000_000, "2026-11-05", accn="0002-new"),   # Class B
        ]}}},
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                quarter("2025-10-01", "2025-12-31", 200e6, "2026-02-20", "Q4", 2025),
                quarter("2026-01-01", "2026-03-31", 220e6, "2026-05-05", "Q1"),
                quarter("2026-04-01", "2026-06-30", 260e6, "2026-08-05", "Q2"),
                quarter("2026-07-01", "2026-09-30", 320e6, "2026-11-05", "Q3"),
            ]}},
            "NetIncomeLoss": {"units": {"USD": [
                quarter("2025-10-01", "2025-12-31", 10e6, "2026-02-20", "Q4", 2025),
                quarter("2026-01-01", "2026-03-31", 15e6, "2026-05-05", "Q1"),
                quarter("2026-04-01", "2026-06-30", 20e6, "2026-08-05", "Q2"),
                quarter("2026-07-01", "2026-09-30", 25e6, "2026-11-05", "Q3"),
            ]}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": [
                quarter("2025-10-01", "2025-12-31", 0.10, "2026-02-20", "Q4", 2025),
                quarter("2026-01-01", "2026-03-31", 0.15, "2026-05-05", "Q1"),
                quarter("2026-04-01", "2026-06-30", 0.20, "2026-08-05", "Q2"),
                quarter("2026-07-01", "2026-09-30", 0.25, "2026-11-05", "Q3"),
            ]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                fact(None, "2026-09-30", 300e6, "2026-11-05"),
            ]}},
        },
    },
}


# ------------------------------------------------------------------ tests

def test_number_parsing():
    assert market.num("$1,234.50") == 1234.5
    assert market.num("-2.3%") == -2.3
    assert market.num("N/A") is None
    assert market.num("") is None
    assert market.num_range("$66.00/$12.50") == (12.5, 66.0)
    assert market.num_range("12.50 - 66.00") == (12.5, 66.0)
    assert market.nasdaq_symbol("BRK-B") == "BRK.B"


def test_share_classes_are_summed_from_the_latest_cover_page():
    shares = market.shares_all_classes(UPCO_FACTS)
    assert shares["shares_m"] == 130.0
    assert shares["per_class_m"] == [100.0, 30.0]
    assert shares["as_of"] == "2026-10-31"


def test_quote_and_valuation_for_up_c(nasdaq):
    quote = run(market.nasdaq_quote, nasdaq, "UPCO")
    assert quote["price"] == 20.0
    assert (quote["week52_low"], quote["week52_high"]) == (12.5, 66.0)
    assert quote["market_cap_nasdaq_musd"] == 2000.0
    assert quote["analyst_target_1y"] == 40.0

    val = market.valuation(quote, UPCO_FACTS)
    assert val["market_cap_musd"] == 2600.0           # 130M shares, both classes
    assert val["trailing_12m"]["eps_diluted"] == 0.7
    assert val["pe_ttm"] == round(20 / 0.7, 1)        # price over reported diluted EPS
    assert val["ps_ttm"] == round(2600 / 1000, 1)
    assert any("differs from Nasdaq" in n for n in val["notes"])
    assert any("2 share classes" in n for n in val["notes"])


def test_missing_quarter_gives_no_trailing_figures():
    rows = [{"period_end": "2026-09-30", "revenue_musd": 1}, {"period_end": "2026-06-30", "revenue_musd": 1},
            {"period_end": "2026-03-31", "revenue_musd": 1}, {"period_end": "2025-09-30", "revenue_musd": 1}]
    assert market.trailing_four_quarters(rows) is None


def test_finnhub_fallback_and_key_never_leaks(nasdaq, finnhub, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(ApiError) as err:
        run(market.best_quote, nasdaq, finnhub, "BLOCKED")
    assert "Symbol not exists" in str(err.value) and "FINNHUB_API_KEY" in str(err.value)

    monkeypatch.setenv("FINNHUB_API_KEY", SECRET)
    quote = run(market.best_quote, nasdaq, finnhub, "BLOCKED")
    assert quote["price"] == 19.5 and "nasdaq_error" in quote

    monkeypatch.setenv("FINNHUB_API_KEY", "wrong-" + SECRET)
    finnhub._cache.clear()
    with pytest.raises(ApiError) as err:
        run(market.best_quote, nasdaq, finnhub, "BLOCKED")
    assert SECRET not in str(err.value)


def test_price_history_statistics(nasdaq):
    hist = run(market.price_history, nasdaq, "UPCO", days=500, max_points=40, today=date(2026, 11, 24))
    assert hist["trading_days"] == 300
    assert hist["to"] == "2026-11-24" and hist["last_close"] == 30.0
    assert hist["high"]["price"] == 66.0
    assert hist["below_high_pct"] == round(100 * (30 / 66 - 1), 1)
    assert hist["change_pct"]["1m"] > 0
    assert len(hist["closes"]) <= 40 and hist["closes"][-1]["date"] == "2026-11-24"
    assert hist["annualized_volatility_pct"] > 0


def test_screen_with_valuation(nasdaq):
    client = SecClient("Test Runner test@example.com", max_requests_per_second=1000, transport=transport())
    set_client(client)
    try:
        caps = run(market.market_caps, nasdaq)
        assert caps["ACME"]["market_cap_musd"] == 2400.0 and caps["SHRK"]["market_cap_musd"] is None

        result = run(screener.screen, client, period="CY2026Q3", market=caps, sort_by="pe")
        by_ticker = {r["ticker"]: r for r in result["results"]}
        assert by_ticker["ACME"]["pe_annualized"] == 20.0      # 2400 / (30 x 4)
        assert by_ticker["SWTH"]["pe_annualized"] == 10.0      # 480 / (12 x 4)
        assert [r["ticker"] for r in result["results"]][:2] == ["SWTH", "ACME"]  # cheapest first

        cheap = run(screener.screen, client, period="CY2026Q3", market=caps, max_pe=15)
        assert [r["ticker"] for r in cheap["results"]] == ["SWTH"]

        small = run(screener.screen, client, period="CY2026Q3", market=caps, max_market_cap_musd=1000)
        assert [r["ticker"] for r in small["results"]] == ["SWTH"]

        plain = run(screener.screen, client, period="CY2026Q3")
        assert "pe_annualized" not in plain["results"][0] and not plain["with_valuation"]
    finally:
        set_client(None)
