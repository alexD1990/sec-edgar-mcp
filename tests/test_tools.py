from __future__ import annotations

import json
from datetime import date

import anyio
import httpx2
import pytest

from fake_sec import ACME, OTC_CO, SHRINK, SWITCHER, transport
from sec_edgar_mcp import filings, financials, insiders, screener
from sec_edgar_mcp.sec_client import SecClient, SecError, set_client


@pytest.fixture
def client():
    c = SecClient("Test Runner test@example.com", max_requests_per_second=1000, transport=transport())
    set_client(c)
    filings._text_cache.clear()
    yield c
    set_client(None)


def run(coro_fn, *args, **kwargs):
    return anyio.run(lambda: coro_fn(*args, **kwargs))


# ------------------------------------------------------------------ client


def test_user_agent_required():
    with pytest.raises(SecError):
        SecClient("no email here")


# ------------------------------------------------------------------ screener


def test_default_period_uses_filing_lag():
    assert screener.default_period(date(2026, 11, 25)) == "CY2026Q3"
    assert screener.default_period(date(2026, 11, 10)) == "CY2026Q2"
    assert screener.default_period(date(2026, 2, 1)) == "CY2025Q3"
    assert screener.previous_year("CY2026Q3") == "CY2025Q3"
    assert screener.previous_year("CY2025") == "CY2024"
    with pytest.raises(SecError):
        screener.previous_year("2025-Q3")


def test_screen_filters_and_tag_fallback(client):
    result = run(screener.screen, client, period="CY2026Q3", min_revenue_growth_pct=25)
    tickers = [r["ticker"] for r in result["results"]]
    assert "ACME" in tickers  # +100%, profitable
    assert "SWTH" in tickers  # found via the ASC 606 revenue tag (+50%)
    assert "TPSC" not in tickers  # OTC excluded by default
    assert "SHRK" not in tickers  # shrinking
    assert all(r["cik"] != 9999999 for r in result["results"])  # unlisted
    acme = next(r for r in result["results"] if r["ticker"] == "ACME")
    assert acme["revenue_musd"] == 300.0  # 'Revenues' beats the bogus 606 value
    assert acme["revenue_growth_pct"] == 100.0
    assert acme["gross_margin_pct"] == 40.0
    assert acme["net_income_growth_pct"] == 200.0
    swth = next(r for r in result["results"] if r["ticker"] == "SWTH")
    assert swth["net_income_growth_pct"] is None  # from a loss: growth is meaningless
    assert tickers[0] == "ACME"  # sorted by growth

    with_otc = run(screener.screen, client, period="CY2026Q3", include_otc=True)
    assert "TPSC" in [r["ticker"] for r in with_otc["results"]]

    capped = run(screener.screen, client, period="CY2026Q3", max_revenue_musd=200)
    assert [r["ticker"] for r in capped["results"]] == ["SWTH"]


def test_screen_missing_period(client):
    with pytest.raises(SecError, match="No revenue data"):
        run(screener.screen, client, period="CY2030Q1")


# ------------------------------------------------------------------ financials


def test_financials_quarters_annual_and_balance(client):
    facts = run(client.company_facts, ACME)
    result = financials.summarize_financials(facts, years=5, quarters=8)

    annual = {r["period_end"]: r for r in result["annual"]}
    assert annual["2025-12-31"]["revenue_musd"] == 700.0
    assert annual["2024-12-31"]["revenue_musd"] == 400.0  # older SalesRevenueNet tag
    assert annual["2025-12-31"]["revenue_growth_yoy_pct"] == 75.0

    quarters = {r["period_end"]: r for r in result["quarterly"]}
    assert "2025-06-30" in quarters and quarters["2025-06-30"]["revenue_musd"] == 140.0  # YTD ignored
    assert quarters["2025-09-30"]["revenue_musd"] == 155.0  # restatement wins
    q4 = quarters["2025-12-31"]
    assert q4["revenue_musd"] == 700 - 120 - 140 - 155
    assert "derived" in q4["note"].lower() or "Q4" in q4["note"]
    q3 = quarters["2026-09-30"]
    assert q3["revenue_growth_yoy_pct"] == round(100 * (300 - 155) / 155, 1)
    assert q3["net_margin_pct"] == 10.0
    assert q3["eps_diluted"] == 0.6

    balance = result["latest_balance_sheet"]
    assert balance["inventory"] == {"value_musd": 140.0, "as_of": "2026-09-30"}
    assert balance["shares_outstanding"]["value_m"] == 50.0


# ------------------------------------------------------------------ insiders


def test_insider_trades_dedupes_joint_owners(client):
    result = run(insiders.insider_trades, client, ACME, days=90, today=date(2026, 11, 25))
    assert result["form4_filings_read"] == 4  # the 2024 filing is outside the window
    sells = result["open_market_sales"]
    assert sells["transactions"] == 1  # reported by both spouses, counted once
    assert sells["value_usd"] == 4_000_000
    buys = result["open_market_purchases"]
    assert buys["transactions"] == 1 and buys["value_usd"] == 51_000
    assert result["net_open_market_usd"] == 51_000 - 4_000_000
    sale = next(t for t in result["transactions"] if t["code"] == "S")
    assert sale["also_reported_by"]
    assert {t["code"] for t in result["transactions"]} == {"P", "S"}
    everything = run(insiders.insider_trades, client, ACME, days=90, include_all_codes=True, today=date(2026, 11, 25))
    assert {t["code"] for t in everything["transactions"]} == {"P", "S", "F", "A"}


def test_parse_form4_flags_plan():
    from fake_sec import FORM4

    parsed = insiders.parse_form4(FORM4["form4a.xml"])
    assert parsed["rule_10b5_1_plan"] is True
    assert parsed["owners"][0]["roles"] == ["CEO"]


# ------------------------------------------------------------------ filings


def test_read_10k_sections(client):
    full = run(filings.read_filing, client, ACME, form="10-K", section="full")
    assert "HIDDEN XBRL" not in full["text"]
    assert "Item 1A. | Risk Factors | 10" in full["text"]  # table rows kept on one line

    risks = run(filings.read_filing, client, ACME, form="10-K", section="risk_factors")
    assert risks["text"].startswith("Item 1A. Risk Factors")
    assert "63% of our components" in risks["text"]
    assert "Unresolved Staff Comments" not in risks["text"]

    auditor = run(filings.read_filing, client, ACME, form="10-K", section="auditor_report")
    assert "Adverse Opinion" in auditor["text"]
    assert auditor["text"].count("Report of Independent Registered Public Accounting Firm") == 2

    page = run(filings.read_filing, client, ACME, form="10-K", section="full", max_chars=1000)
    assert page["next_offset"] == 1000
    page2 = run(filings.read_filing, client, ACME, form="10-K", section="full", offset=1000, max_chars=1000)
    assert page2["offset"] == 1000 and page2["text"] != page["text"]

    with pytest.raises(SecError, match="Unknown section"):
        run(filings.read_filing, client, ACME, form="10-K", section="exhibits")


def test_read_8k_includes_press_release(client):
    result = run(filings.read_filing, client, ACME, form="8-K")
    assert "record revenue of $300 million" in result["text"]
    assert any("ex991" in d for d in result["documents"])


def test_list_filings_filter(client):
    subs = run(client.submissions, ACME)
    result = filings.list_filings(subs, ACME, ["10-K", "8-K"], 10)
    assert [f["form"] for f in result["filings"]] == ["8-K", "10-K"]
    assert result["filings"][0]["items"] == "2.02,9.01"


def test_full_text_search(client):
    result = run(filings.full_text_search, client, '"record revenue"', forms=["8-K"], start_date="2026-01-01")
    assert result["total_hits"] == 2
    first = result["results"][0]
    assert first["company"] == "Acme Robotics Inc."
    assert first["tickers"] == "ACME, ACME-W"
    assert first["cik"] == ACME
    assert first["url"].endswith("/000126000046/acme-ex991.htm")


# ------------------------------------------------------------------ MCP over HTTP


async def _mcp_post(http, payload, token="secret", session=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["mcp-session-id"] = session
    return await http.post("http://testserver/mcp", content=json.dumps(payload), headers=headers)


def test_http_app_end_to_end(client):
    from sec_edgar_mcp.server import build_http_app

    async def scenario():
        app = build_http_app("secret")
        inner = app.app  # Starlette app inside the auth middleware
        async with inner.router.lifespan_context(inner):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                health = await http.get("http://testserver/health")
                assert health.status_code == 200

                denied = await _mcp_post(http, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")
                assert denied.status_code == 401

                init = await _mcp_post(http, {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}},
                })
                assert init.status_code == 200, init.text
                version = init.json()["result"]["protocolVersion"]

                async def call(i, method, params):
                    r = await http.post("http://testserver/mcp", content=json.dumps(
                        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}), headers={
                        "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        "Authorization": "Bearer secret", "mcp-protocol-version": version})
                    assert r.status_code == 200, r.text
                    return r.json()

                listed = await call(2, "tools/list", {})
                names = {t["name"] for t in listed["result"]["tools"]}
                assert {"screen_companies", "get_financials", "get_insider_trades", "read_filing",
                        "search_filings", "get_filings", "lookup_company"} <= names

                called = await call(3, "tools/call", {"name": "screen_companies", "arguments": {"period": "CY2026Q3"}})
                result = called["result"]
                assert not result.get("isError"), result
                payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                assert payload["results"][0]["ticker"] == "ACME"

                err = await call(4, "tools/call", {"name": "get_financials", "arguments": {"company": "NOPE-NOT-REAL"}})
                assert err["result"]["isError"] is True
                assert "No EDGAR company" in err["result"]["content"][0]["text"]

    anyio.run(scenario)


def test_cache_is_bounded_by_bytes():
    c = SecClient("Test Runner test@example.com", cache_max_bytes=300, transport=transport())
    c._cache_put("a", 1, 60, 100)
    c._cache_put("b", 2, 60, 90)
    c._cache_put("huge", 3, 60, 200)  # > a third of the budget: never cached
    assert c._cache_get("huge") is None
    c._cache_put("c", 4, 60, 95)
    c._cache_put("d", 5, 60, 95)  # pushes total over 300 -> oldest ("a") evicted
    assert c._cache_get("a") is None and c._cache_get("d") == 5
    assert c._cache_bytes <= 300
