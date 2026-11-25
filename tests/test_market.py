from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import anyio
import httpx2
import pytest

from fake_sec import fact, transport
from sec_edgar_mcp import market, screener
from sec_edgar_mcp.apiclient import ApiError, JsonApiClient
from sec_edgar_mcp.sec_client import SecClient, set_client

MASSIVE_KEY = "massivesecret123"
FINNHUB_KEY = "finnhubsecret999"


def run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


def ok(payload, status=200):
    return httpx2.Response(status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})


# ------------------------------------------------------------------ fake Massive

def bars(last_day: date, n: int = 300):
    out, day, price = [], last_day, 30.0
    while len(out) < n:
        if day.weekday() < 5:
            ms = int(datetime(day.year, day.month, day.day, 4, tzinfo=timezone.utc).timestamp() * 1000)
            out.append({"t": ms, "o": price, "h": price * 1.01, "l": price * 0.99, "c": price, "v": 1_000_000})
            price = price / 1.002  # walking back in time: prices were lower before
        day -= timedelta(days=1)
    out[150]["c"], out[150]["h"] = 60.0, 66.0  # the peak
    return out[::-1]


def massive_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.params.get("apiKey") != MASSIVE_KEY:
        return ok({"status": "ERROR", "error": f"Unknown API Key {request.url.params.get('apiKey')}"}, 401)
    path = request.url.path
    if path.startswith("/v2/aggs/ticker/UPCO/range/1/day/"):
        start, end = path.split("/")[-2:]
        rows = [b for b in bars(date(2026, 11, 24))
                if start <= datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).date().isoformat() <= end]
        return ok({"ticker": "UPCO", "status": "OK", "resultsCount": len(rows), "results": rows})
    if path.startswith("/v2/aggs/ticker/"):
        return ok({"ticker": "X", "status": "OK", "resultsCount": 0})
    if path == "/v2/aggs/grouped/locale/us/market/stocks/2026-11-24":
        return ok({"status": "NOT_AUTHORIZED", "message": "Attempted to request today's data before end of day."}, 403)
    if path == "/v2/aggs/grouped/locale/us/market/stocks/2026-11-23":
        return ok({"status": "OK", "resultsCount": 3, "results": [
            {"T": "ACME", "c": 48.0}, {"T": "SWTH", "c": 9.6}, {"T": "SHRK", "c": 1.0},
        ]})
    return httpx2.Response(404)


def finnhub_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.params.get("token") != FINNHUB_KEY:
        return ok({"error": f"Invalid API key {request.url.params.get('token')}"}, 401)
    return ok({"c": 31.5, "d": 1.5, "dp": 5.0, "h": 32, "l": 30, "o": 30, "pc": 30.0, "t": 1795651200})


@pytest.fixture
def massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", MASSIVE_KEY)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    return JsonApiClient("Massive", "https://api.massive.com", "MASSIVE_API_KEY", key_param="apiKey",
                         max_requests_per_second=1000, transport=httpx2.MockTransport(massive_handler))


@pytest.fixture
def finnhub():
    return JsonApiClient("Finnhub", "https://finnhub.io/api/v1", "FINNHUB_API_KEY", key_param="token",
                         max_requests_per_second=1000, transport=httpx2.MockTransport(finnhub_handler))


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

def test_symbols():
    assert market.massive_symbol("BRK-B") == "BRK.B"
    assert market.lookup_price({"BRK-B": 1.0}, "BRK.B") == 1.0


def test_share_classes_are_summed_from_the_latest_cover_page():
    shares = market.shares_all_classes(UPCO_FACTS)
    assert shares["shares_m"] == 130.0
    assert shares["per_class_m"] == [100.0, 30.0]
    assert shares["as_of"] == "2026-10-31"


def test_quote_and_valuation_for_up_c(massive, finnhub):
    quote = run(market.best_quote, massive, finnhub, "UPCO", today=date(2026, 11, 25))
    assert quote["price"] == 30.0 and quote["price_time"] == "2026-11-24 close"
    assert quote["week52_high"] == 66.0
    assert quote["below_52w_high_pct"] == round(100 * (30 / 66 - 1), 1)

    val = market.valuation(quote, UPCO_FACTS)
    assert val["market_cap_musd"] == 3900.0           # 130M shares x $30, both classes
    assert val["trailing_12m"]["eps_diluted"] == 0.7
    assert val["pe_ttm"] == round(30 / 0.7, 1)        # price over reported diluted EPS
    assert val["ps_ttm"] == round(3900 / 1000, 1)
    assert any("2 share classes" in n for n in val["notes"])


def test_finnhub_adds_real_time_price(massive, finnhub, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", FINNHUB_KEY)
    quote = run(market.best_quote, massive, finnhub, "UPCO", today=date(2026, 11, 25))
    assert quote["price"] == 31.5 and quote["price_source"] == "Finnhub (real time)"
    assert quote["week52_high"] == 66.0                # range still from Massive


def test_missing_keys_and_keys_never_leak(massive, finnhub, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY")
    with pytest.raises(ApiError) as err:
        run(market.best_quote, massive, finnhub, "UPCO")
    assert "MASSIVE_API_KEY" in str(err.value)

    monkeypatch.setenv("MASSIVE_API_KEY", "wrong-" + MASSIVE_KEY)
    with pytest.raises(ApiError) as err:
        run(market.best_quote, massive, finnhub, "UPCO")
    assert MASSIVE_KEY not in str(err.value)


def test_unknown_ticker(massive, finnhub):
    with pytest.raises(ApiError) as err:
        run(market.best_quote, massive, finnhub, "NOPE", today=date(2026, 11, 25))
    assert "no daily prices" in str(err.value)


def test_missing_quarter_gives_no_trailing_figures():
    rows = [{"period_end": "2026-09-30", "revenue_musd": 1}, {"period_end": "2026-06-30", "revenue_musd": 1},
            {"period_end": "2026-03-31", "revenue_musd": 1}, {"period_end": "2025-09-30", "revenue_musd": 1}]
    assert market.trailing_four_quarters(rows) is None


def test_price_history_statistics(massive):
    hist = run(market.price_history, massive, "UPCO", days=500, max_points=40, today=date(2026, 11, 25))
    assert hist["trading_days"] == 300
    assert hist["to"] == "2026-11-24" and hist["last_close"] == 30.0
    assert hist["high"]["price"] == 66.0
    assert hist["below_high_pct"] == round(100 * (30 / 66 - 1), 1)
    assert hist["change_pct"]["1m"] > 0
    assert len(hist["closes"]) <= 40 and hist["closes"][-1]["date"] == "2026-11-24"
    assert hist["annualized_volatility_pct"] > 0


def test_price_history_one_year_window_has_a_1y_change(massive):
    """365 calendar days can hold exactly 252 trading days; 1y must still be computed."""
    hist = run(market.price_history, massive, "UPCO", days=365, today=date(2026, 11, 25))
    assert hist["from"] == "2025-11-24" and hist["to"] == "2026-11-24"
    closes = {datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).date().isoformat(): b["c"]
              for b in bars(date(2026, 11, 24))}
    assert hist["change_pct"]["1y"] == round(100 * (30.0 / closes["2025-11-24"] - 1), 1)
    assert hist["change_pct"]["1m"] == round(100 * (30.0 / closes["2026-10-23"] - 1), 1)  # 10-25 is a Sunday

    short = run(market.price_history, massive, "UPCO", days=100, today=date(2026, 11, 25))
    assert short["change_pct"]["1y"] is None and short["change_pct"]["3m"] is not None


def test_cover_page_shares_from_inline_xbrl():
    from fake_sec import TEN_Q_HTML

    parsed = market.parse_cover_shares(TEN_Q_HTML)
    assert parsed["shares_m"] == 50.0                   # 42 + 8; the undimensioned total is not added
    assert parsed["classes"] == [{"class": "CommonClassA", "shares_m": 42.0},
                                 {"class": "CommonClassB", "shares_m": 8.0}]  # scale=6 applied, duplicate ignored
    assert parsed["as_of"] == "2026-10-31"
    only_total = (
        '<xbrli:context id="t"><xbrli:period><xbrli:instant>2026-10-31</xbrli:instant></xbrli:period></xbrli:context>'
        '<ix:nonFraction name="dei:EntityCommonStockSharesOutstanding" contextRef="t" scale="3">1,500</ix:nonFraction>'
    )
    single = market.parse_cover_shares(only_total)
    assert single["shares_m"] == 1.5 and single["classes"] == [{"class": "all classes", "shares_m": 1.5}]
    assert market.parse_cover_shares("<html>no xbrl</html>") is None


def test_cover_page_shares_reads_latest_10q_and_feeds_valuation():
    from fake_sec import ACME

    client = SecClient("Test Runner test@example.com", max_requests_per_second=1000, transport=transport())
    market._cover_cache.clear()
    cover = run(market.cover_page_shares, client, ACME)
    assert cover["shares_m"] == 50.0 and cover["form"] == "10-Q" and cover["accession"] == "0001-26-000047"
    assert cover["url"].endswith("/acme-20260930.htm")

    val = market.valuation({"price": 10.0}, UPCO_FACTS, cover)
    assert val["market_cap_musd"] == 500.0              # the cover from the filing wins over companyfacts
    assert val["shares_source"].startswith("cover page of 10-Q filed 2026-11-05")


def test_all_closes_steps_back_to_a_released_day(massive):
    closes = run(market.all_closes, massive, today=date(2026, 11, 25))
    assert closes["date"] == "2026-11-23"              # 11-24 not released yet on the free plan
    assert closes["prices"]["ACME"] == 48.0


def test_next_quarter_instant():
    assert screener.next_quarter_instant("CY2026Q3") == "CY2026Q4I"
    assert screener.next_quarter_instant("CY2026Q4") == "CY2027Q1I"
    assert screener.next_quarter_instant("CY2025") == "CY2026Q1I"


def test_screen_with_valuation(massive):
    client = SecClient("Test Runner test@example.com", max_requests_per_second=1000, transport=transport())
    set_client(client)
    try:
        prices = run(market.all_closes, massive, today=date(2026, 11, 25))

        result = run(screener.screen, client, period="CY2026Q3", prices=prices, sort_by="pe")
        by_ticker = {r["ticker"]: r for r in result["results"]}
        assert by_ticker["ACME"]["market_cap_musd"] == 2400.0   # newest share count (50M) x $48
        # Trailing EPS: Q3 0.60 + Q2 0.55 + Q1 0.50 + Q4 derived 1.20 - 0.75 = 2.10
        assert by_ticker["ACME"]["eps_12m"] == 2.1
        assert by_ticker["ACME"]["pe"] == round(48 / 2.1, 1)
        assert by_ticker["ACME"]["pe_basis"] == "ttm (Q4 derived from full year)"
        assert by_ticker["SWTH"]["market_cap_musd"] == 480.0    # count from the Q3 frame
        assert by_ticker["SWTH"]["pe"] == 10.0                  # 9.6 / (0.24 x 4): only one quarter known
        assert by_ticker["SWTH"]["pe_basis"] == "1 quarter x 4"
        assert [r["ticker"] for r in result["results"]][:2] == ["SWTH", "ACME"]  # cheapest first
        assert result["prices_from"] == "2026-11-23"
        assert "GAIN" not in by_ticker                          # profit from a gain: left out

        cheap = run(screener.screen, client, period="CY2026Q3", prices=prices, max_pe=15)
        assert [r["ticker"] for r in cheap["results"]] == ["SWTH"]

        small = run(screener.screen, client, period="CY2026Q3", prices=prices, max_market_cap_musd=1000)
        assert [r["ticker"] for r in small["results"]] == ["SWTH"]

        plain = run(screener.screen, client, period="CY2026Q3")
        assert "pe" not in plain["results"][0] and not plain["with_valuation"]
    finally:
        set_client(None)
