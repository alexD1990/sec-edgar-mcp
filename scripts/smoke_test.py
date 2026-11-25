"""Quick live check against the real SEC EDGAR. Run after installing:

    export SEC_USER_AGENT="Ola Nordmann ola@example.com"
    python scripts/smoke_test.py            # defaults to SMCI
    python scripts/smoke_test.py NVDA

Each step prints OK or the error, so you can see which tool needs attention.
"""

from __future__ import annotations

import os
import sys
import time

import anyio

from sec_edgar_mcp import filings, financials, insiders, macro, market, screener
from sec_edgar_mcp.apiclient import get_api_client
from sec_edgar_mcp.companies import resolve_company
from sec_edgar_mcp.config import load_env
from sec_edgar_mcp.sec_client import get_client


async def main(ticker: str) -> None:
    client = get_client()

    async def step(name, fn):
        start = time.monotonic()
        try:
            summary = await fn()
            print(f"OK   {name:<28} {time.monotonic() - start:5.1f}s  {summary}")
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
            print(f"FAIL {name:<28} {type(exc).__name__}: {exc}")

    info = await resolve_company(client, ticker)
    cik = info["cik"]
    print(f"{ticker} -> {info['name']} (CIK {cik}, {info['exchange']})\n")

    async def fin():
        r = financials.summarize_financials(await client.company_facts(cik), years=3, quarters=4)
        q = r["quarterly"][0] if r["quarterly"] else {}
        return f"latest quarter {q.get('period_end')}: revenue {q.get('revenue_musd')}m, growth {q.get('revenue_growth_yoy_pct')}%"

    async def ins():
        r = await insiders.insider_trades(client, cik, days=90)
        return (f"{r['form4_filings_read']} Form 4s, buys ${r['open_market_purchases']['value_usd']:,.0f}, "
                f"sells ${r['open_market_sales']['value_usd']:,.0f}")

    async def risks():
        r = await filings.read_filing(client, cik, form="10-K", section="risk_factors", max_chars=200)
        return f"{r['total_chars']:,} chars from 10-K filed {r['filed']}"

    async def auditor():
        r = await filings.read_filing(client, cik, form="10-K", section="auditor_report", max_chars=200)
        return f"{r['total_chars']:,} chars"

    async def eightk():
        r = await filings.read_filing(client, cik, form="8-K", max_chars=200)
        return f"8-K filed {r['filed']}, {len(r['documents'])} document(s)"

    async def search():
        r = await filings.full_text_search(client, '"material weakness"', forms=["10-K"], start_date="2026-01-01")
        return f"{r['total_hits']} hits, top company: {r['companies'][0]['company'] if r['companies'] else '-'}"

    async def scr():
        r = await screener.screen(client, min_revenue_growth_pct=30, max_revenue_musd=2000, limit=5)
        top = ", ".join(f"{x['ticker']} (+{x['revenue_growth_pct']}%)" for x in r["results"])
        return f"{r['period']}: {r['matches']} matches of {r['companies_with_revenue_data']}; top: {top}"

    await step("get_financials", fin)
    await step("get_insider_trades", ins)
    await step("read_filing risk_factors", risks)
    await step("read_filing auditor_report", auditor)
    await step("read_filing 8-K", eightk)
    await step("search_filings", search)
    await step("screen_companies", scr)

    print()
    if os.environ.get("FRED_API_KEY"):
        async def fred_series():
            r = await macro.fred_series(get_api_client("fred"), "DGS10")
            return f"10-year Treasury {r['summary'].get('latest_value')}% on {r['summary'].get('latest_date')}"

        async def fred_search():
            r = await macro.fred_search(get_api_client("fred"), "construction spending power", limit=3)
            return ", ".join(f"{s['id']} ({s['observation_end']})" for s in r["series"]) or "no hits"

        await step("fred_series DGS10", fred_series)
        await step("fred_search", fred_search)
    else:
        print("SKIP FRED: no FRED_API_KEY in environment or .env")

    if os.environ.get("EIA_API_KEY"):
        async def eia_sales():
            r = await macro.electricity_sales(get_api_client("eia"), states=["VA", "TX", "US"], months=12)
            return "; ".join(
                f"{s['state']} {s.get('latest_month')} TTM {s.get('trailing_12m_yoy_pct')}%" for s in r["states"]
            )

        await step("eia_electricity_sales", eia_sales)
    else:
        print("SKIP EIA: no EIA_API_KEY in environment or .env")

    print()
    if not os.environ.get("MASSIVE_API_KEY"):
        print("SKIP prices: no MASSIVE_API_KEY in environment or .env")
        return
    massive, finnhub = get_api_client("massive"), get_api_client("finnhub")
    print("(Massive free plan allows 5 requests a minute, so the price steps take about a minute)")

    def quote_step(symbol):
        async def run_quote():
            sym_info = await resolve_company(client, symbol)
            q = await market.best_quote(massive, finnhub, sym_info["ticker"])
            v = market.valuation(q, await client.company_facts(sym_info["cik"]))
            classes = (v.get("shares_detail") or {}).get("per_class_m")
            return (f"${q['price']} ({q.get('price_time')}, {q.get('price_source')}), "
                    f"52w {q.get('week52_low')}-{q.get('week52_high')}, cap {v['market_cap_musd']}m, "
                    f"classes {classes}, P/E {v['pe_ttm']}, P/S {v['ps_ttm']}")
        return run_quote

    async def history():
        r = await market.price_history(massive, info["ticker"], days=365)
        return (f"{r['trading_days']} days {r['from']}..{r['to']}, 1y {r['change_pct']['1y']}%, "
                f"{r['below_high_pct']}% below high {r['high']['price']} ({r['high']['date']})")

    async def closes():
        r = await market.all_closes(massive)
        return f"{len(r['prices'])} symbols on {r['date']}; SMCI {market.lookup_price(r['prices'], 'SMCI')}"

    async def scr_val():
        table = await market.all_closes(massive)
        r = await screener.screen(client, min_revenue_growth_pct=30, max_revenue_musd=500, prices=table,
                                  max_pe=20, max_market_cap_musd=5000, sort_by="pe", limit=5)
        top = ", ".join(f"{x['ticker']} (P/E {x['pe_annualized']}, +{x['revenue_growth_pct']}%)" for x in r["results"])
        return f"{r['matches']} matches; cheapest: {top}"

    await step(f"get_quote {ticker}", quote_step(ticker))
    await step("get_quote FPS (Up-C)", quote_step("FPS"))
    await step("get_quote GOOGL (3 classes)", quote_step("GOOGL"))
    await step("get_price_history", history)
    await step("all_closes (bulk)", closes)
    await step("screen with valuation", scr_val)
    if os.environ.get("FINNHUB_API_KEY"):
        async def fh():
            q = await market.finnhub_quote(finnhub, ticker)
            return f"${q['price']} at {q['price_time']}"
        await step("finnhub_quote (real time)", fh)
    else:
        print("SKIP Finnhub: no FINNHUB_API_KEY (optional real-time price)")


if __name__ == "__main__":
    load_env()
    anyio.run(main, sys.argv[1] if len(sys.argv) > 1 else "SMCI")
