"""Quick live check against the real SEC EDGAR. Run after installing:

    export SEC_USER_AGENT="Ola Nordmann ola@example.com"
    python scripts/smoke_test.py            # defaults to SMCI
    python scripts/smoke_test.py NVDA

Each step prints OK or the error, so you can see which tool needs attention.
"""

from __future__ import annotations

import sys
import time

import anyio

from sec_edgar_mcp import filings, financials, insiders, screener
from sec_edgar_mcp.companies import resolve_company
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


if __name__ == "__main__":
    anyio.run(main, sys.argv[1] if len(sys.argv) > 1 else "SMCI")
