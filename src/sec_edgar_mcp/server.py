"""The MCP server: tool definitions, optional bearer-token auth and the HTTP app."""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .companies import resolve_company, search_companies
from .filings import full_text_search, list_filings
from .filings import read_filing as read_filing_text
from .financials import summarize_financials
from .apiclient import get_api_client
from .insiders import insider_trades
from .macro import eia_browse as eia_browse_route
from .macro import eia_data as eia_data_query
from .macro import electricity_sales, fred_search as fred_search_series, fred_series as fred_series_data
from .screener import screen
from .sec_client import get_client

INSTRUCTIONS = """\
Free, primary-source data straight from SEC EDGAR for US-listed companies.

Typical research flow:
1. screen_companies to find candidates (growth, profitability, size) across all filers.
2. get_financials for the trend of one company (annual + quarterly, margins, cash flow, balance sheet).
3. get_insider_trades for open-market buying/selling by officers and directors.
4. read_filing for the text: risk factors, MD&A, auditor report, legal proceedings, 8-K press releases.
5. search_filings for phrases across all companies' filings (e.g. "record backlog", "material weakness").

Macro and energy context (needs FRED_API_KEY / EIA_API_KEY in .env):
- fred_search + fred_series for interest rates, inflation, construction spending and other US macro series.
- eia_electricity_sales for power demand by state and sector (a proxy for data-center build-out);
  eia_browse + eia_data for any other EIA dataset.

There are no stock prices in SEC data; valuation (P/E, market cap) needs a price from another source.
Always cite the accession number or URL that each figure comes from.
"""

mcp = MCPServer(
    "sec-edgar",
    title="SEC EDGAR research",
    instructions=INSTRUCTIONS,
    version=__version__,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
Company = Annotated[str, Field(description="Ticker (e.g. SMCI), company name, or CIK number.")]


@mcp.tool(annotations=READ_ONLY)
async def lookup_company(
    query: Annotated[str, Field(description="Ticker, part of the company name, or CIK.")],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find US-listed companies on EDGAR by ticker, name or CIK. Returns cik, name, ticker and exchange."""
    matches = await search_companies(get_client(), query, limit)
    return {"query": query, "matches": matches}


@mcp.tool(annotations=READ_ONLY)
async def screen_companies(
    period: Annotated[
        str | None,
        Field(description="Calendar period: a quarter like CY2026Q3 or a full year like CY2025. "
                          "Default: latest quarter that ended 50+ days ago."),
    ] = None,
    min_revenue_growth_pct: Annotated[float, Field(description="Minimum revenue growth vs. same period a year earlier, in %.")] = 25.0,
    min_revenue_musd: Annotated[float, Field(description="Minimum revenue in the period, USD millions.")] = 20.0,
    max_revenue_musd: Annotated[float | None, Field(description="Maximum revenue in the period, USD millions (use to stay in small caps).")] = None,
    min_prev_revenue_musd: Annotated[
        float, Field(description="Minimum revenue in the comparison period, USD millions. Filters out huge % growth from a tiny base.")
    ] = 10.0,
    require_profitable: Annotated[bool, Field(description="Only companies with positive net income in the period.")] = True,
    min_net_margin_pct: Annotated[float | None, Field(description="Minimum net margin in %.")] = None,
    min_gross_margin_pct: Annotated[float | None, Field(description="Minimum gross margin in %.")] = None,
    include_otc: Annotated[bool, Field(description="Include OTC-traded companies (default: only exchange-listed).")] = False,
    sort_by: Literal["revenue_growth", "net_margin", "net_income_growth", "revenue", "gross_margin"] = "revenue_growth",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Screen ALL SEC filers at once on growth and profitability using XBRL frames.

    Compares one period with the same period a year earlier and returns matching
    exchange-listed companies with revenue, growth, margins and net income.
    Use it to find candidates, then verify each one with get_financials and read_filing.
    """
    return await screen(
        get_client(),
        period=period,
        min_revenue_growth_pct=min_revenue_growth_pct,
        min_revenue_musd=min_revenue_musd,
        max_revenue_musd=max_revenue_musd,
        min_prev_revenue_musd=min_prev_revenue_musd,
        require_profitable=require_profitable,
        min_net_margin_pct=min_net_margin_pct,
        min_gross_margin_pct=min_gross_margin_pct,
        include_otc=include_otc,
        sort_by=sort_by,
        limit=limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_financials(
    company: Company,
    years: Annotated[int, Field(ge=1, le=15)] = 5,
    quarters: Annotated[int, Field(ge=0, le=20)] = 8,
) -> dict[str, Any]:
    """Key figures for one company from its XBRL filings: annual and quarterly revenue, growth,
    gross/operating/net margins, EPS, operating and free cash flow, plus the latest balance sheet
    (cash, inventory, debt, equity, shares outstanding)."""
    client = get_client()
    info = await resolve_company(client, company)
    facts = await client.company_facts(info["cik"])
    result = summarize_financials(facts, years=years, quarters=quarters)
    result["ticker"] = info.get("ticker")
    result["exchange"] = info.get("exchange")
    return result


@mcp.tool(annotations=READ_ONLY)
async def get_insider_trades(
    company: Company,
    days: Annotated[int, Field(ge=1, le=730, description="Look-back window in days.")] = 90,
    include_all_codes: Annotated[
        bool, Field(description="Also list grants, tax withholding, exercises etc. (default: only open-market buys and sells).")
    ] = False,
    max_filings: Annotated[
        int, Field(ge=10, le=400, description="Most Form 4 filings to read (newest first). Large companies file many.")
    ] = 150,
) -> dict[str, Any]:
    """Insider transactions from Form 4 filings: open-market purchases and sales by officers,
    directors and 10% owners, with totals, prices, holdings after the trade and Rule 10b5-1 flags."""
    client = get_client()
    info = await resolve_company(client, company)
    return await insider_trades(
        client, info["cik"], days=days, include_all_codes=include_all_codes, max_filings=max_filings
    )


@mcp.tool(annotations=READ_ONLY)
async def get_filings(
    company: Company,
    forms: Annotated[
        list[str] | None, Field(description="Form types to include, e.g. ['10-K', '10-Q', '8-K']. Default: all.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List a company's recent filings (newest first) with dates, accession numbers, 8-K item codes and links."""
    client = get_client()
    info = await resolve_company(client, company)
    subs = await client.submissions(info["cik"])
    return list_filings(subs, info["cik"], forms, limit)


@mcp.tool(annotations=READ_ONLY)
async def read_filing(
    company: Company,
    form: Annotated[str, Field(description="Form type: 10-K, 10-Q, 8-K, DEF 14A, S-1 ...")] = "10-K",
    section: Annotated[
        str,
        Field(description=(
            "'full', or a section. 10-K: business, risk_factors, cybersecurity, legal_proceedings, "
            "market_for_equity, mdna, market_risk, financial_statements, controls_and_procedures, auditor_report. "
            "10-Q: mdna, market_risk, controls_and_procedures, legal_proceedings, risk_factors, other_information. "
            "8-K: full (main document + press-release exhibits) or exhibits."
        )),
    ] = "full",
    accession: Annotated[str | None, Field(description="A specific filing (from get_filings). Default: the latest of this form.")] = None,
    offset: Annotated[int, Field(ge=0, description="Character offset for paging through long text (use next_offset).")] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=60000)] = 20000,
    find: Annotated[
        str | None,
        Field(description=(
            "Search inside the chosen document/section instead of paging: returns snippets around each "
            "match with their offsets, e.g. find='Commitments and Contingencies' or 'export control'."
        )),
    ] = None,
    context_chars: Annotated[int, Field(ge=200, le=6000, description="Snippet length when using find.")] = 1500,
) -> dict[str, Any]:
    """Read the text of a filing or one section of it, as clean plain text with paging.

    Good for finding red flags: auditor_report (adverse opinions, going concern), controls_and_procedures
    (material weaknesses), risk_factors (customer/supplier concentration), legal_proceedings, and 8-K
    earnings press releases. Use find to jump straight to a note or topic in a long document.
    """
    client = get_client()
    info = await resolve_company(client, company)
    return await read_filing_text(
        client, info["cik"], form=form, section=section, accession=accession, offset=offset,
        max_chars=max_chars, find=find, context_chars=context_chars,
    )


@mcp.tool(annotations=READ_ONLY)
async def search_filings(
    query: Annotated[str, Field(description='Words or "exact phrase" to find, e.g. "record backlog" or "going concern".')],
    forms: Annotated[list[str] | None, Field(description="Limit to form types, e.g. ['8-K'] or ['10-K', '10-Q'].")] = None,
    start_date: Annotated[str | None, Field(description="Earliest filing date, YYYY-MM-DD.")] = None,
    end_date: Annotated[str | None, Field(description="Latest filing date, YYYY-MM-DD.")] = None,
    page: Annotated[int, Field(ge=0, le=99, description="Result page (100 hits per page).")] = 0,
) -> dict[str, Any]:
    """Full-text search across all EDGAR filings since 2001. Returns matching documents and a
    per-company hit count, which is useful for spotting which companies talk about a theme."""
    return await full_text_search(
        get_client(), query, forms=forms, start_date=start_date, end_date=end_date, page=page
    )


# ---------------------------------------------------------------- macro and energy


@mcp.tool(annotations=READ_ONLY)
async def fred_search(
    query: Annotated[str, Field(description="Words describing the series, e.g. 'data center construction spending', '10-year treasury'.")],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find FRED (St. Louis Fed) data series by keyword. Returns series ids, titles, frequency and how
    current each series is. Use the id with fred_series."""
    return await fred_search_series(get_api_client("fred"), query, limit)


@mcp.tool(annotations=READ_ONLY)
async def fred_series(
    series_id: Annotated[str, Field(description="FRED series id, e.g. DGS10, FEDFUNDS, CPIAUCSL, TTLCONS.")],
    start: Annotated[str | None, Field(description="First date, YYYY-MM-DD. Default: five years ago.")] = None,
    end: Annotated[str | None, Field(description="Last date, YYYY-MM-DD. Default: latest.")] = None,
    units: Annotated[
        Literal["lin", "chg", "ch1", "pch", "pc1", "pca", "log"],
        Field(description="lin = levels, pc1 = % change from a year ago, pch = % change from previous period."),
    ] = "lin",
    frequency: Annotated[
        str | None, Field(description="Aggregate to a lower frequency: d, w, m, q or a. Default: native.")
    ] = None,
    max_points: Annotated[int, Field(ge=1, le=1000)] = 120,
) -> dict[str, Any]:
    """Observations for one FRED series with a summary: latest value, value a year earlier, 1-year change
    and the min/max in the window."""
    return await fred_series_data(
        get_api_client("fred"), series_id, start=start, end=end, units=units, frequency=frequency,
        max_points=max_points,
    )


@mcp.tool(annotations=READ_ONLY)
async def eia_electricity_sales(
    states: Annotated[
        list[str], Field(description="Two-letter state codes, or 'US' for the national total, e.g. ['VA', 'TX', 'US'].")
    ],
    sector: Annotated[
        Literal["COM", "IND", "RES", "ALL", "TRA", "OTH"],
        Field(description="COM = commercial (where data centers are counted), IND = industrial, ALL = total."),
    ] = "COM",
    months: Annotated[int, Field(ge=6, le=120, description="Months of history to show.")] = 24,
) -> dict[str, Any]:
    """Monthly retail electricity sales from EIA by state and sector, with year-over-year growth of the
    trailing 12 months. Fast-rising commercial sales in Virginia or Texas indicate data-center build-out."""
    return await electricity_sales(get_api_client("eia"), states=states, sector=sector, months=months)


@mcp.tool(annotations=READ_ONLY)
async def eia_browse(
    route: Annotated[str, Field(description="EIA API route, e.g. '' (top), 'electricity', 'electricity/retail-sales'.")] = "",
) -> dict[str, Any]:
    """Explore the EIA data catalogue: child routes, or for a dataset its frequencies, facets and data columns."""
    return await eia_browse_route(get_api_client("eia"), route)


@mcp.tool(annotations=READ_ONLY)
async def eia_data(
    route: Annotated[str, Field(description="Dataset route from eia_browse, e.g. 'electricity/retail-sales'.")],
    data_columns: Annotated[list[str], Field(description="Columns to return, e.g. ['sales', 'price'].")],
    frequency: Annotated[str, Field(description="e.g. monthly, quarterly, annual, hourly.")],
    facets: Annotated[
        dict[str, list[str]] | None,
        Field(description="Filters, e.g. {'stateid': ['VA'], 'sectorid': ['COM']}."),
    ] = None,
    start: Annotated[str | None, Field(description="First period, e.g. 2023-01.")] = None,
    end: Annotated[str | None, Field(description="Last period.")] = None,
    length: Annotated[int, Field(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    """Query any EIA dataset (newest first). Use eia_browse first to find routes, facets and columns."""
    return await eia_data_query(
        get_api_client("eia"), route, data_columns=data_columns, frequency=frequency, facets=facets,
        start=start, end=end, length=length,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__})


# ---------------------------------------------------------------- auth + app


class BearerTokenMiddleware:
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on everything except /health.

    Pure ASGI (not BaseHTTPMiddleware) so streaming responses are untouched.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(token: str | None = None) -> ASGIApp:
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",  # behind a hosting proxy; DNS-rebinding protection is for localhost servers
    )
    token = token if token is not None else os.environ.get("MCP_AUTH_TOKEN")
    if token:
        app = BearerTokenMiddleware(app, token)
    return app
