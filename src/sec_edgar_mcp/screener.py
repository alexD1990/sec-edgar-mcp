"""Screen every SEC filer at once using XBRL 'frames'.

A frame is one concept (e.g. Revenues) for all companies in one calendar period
(e.g. CY2025Q3). Comparing two frames a year apart gives growth for the whole
market in a handful of requests.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import anyio

from .companies import ticker_map
from .market import lookup_market
from .sec_client import SecClient, SecError

REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
NET_INCOME_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")
GROSS_PROFIT_CONCEPTS = ("GrossProfit",)

PERIOD_RE = re.compile(r"^CY(\d{4})(Q[1-4])?$")
SORT_KEYS = ("revenue_growth", "net_margin", "net_income_growth", "revenue", "gross_margin", "pe", "ps", "market_cap")
ASCENDING_SORTS = {"pe", "ps", "market_cap"}  # cheapest / smallest first


def default_period(today: date | None = None, lag_days: int = 50) -> str:
    """Latest calendar quarter that ended at least `lag_days` ago (10-Qs are due 40-45 days after)."""
    cutoff = (today or date.today()) - timedelta(days=lag_days)
    quarter_ends = [(4, 12, 31), (3, 9, 30), (2, 6, 30), (1, 3, 31)]
    for quarter, month, day in quarter_ends:
        if date(cutoff.year, month, day) <= cutoff:
            return f"CY{cutoff.year}Q{quarter}"
    return f"CY{cutoff.year - 1}Q4"


def previous_year(period: str) -> str:
    match = PERIOD_RE.match(period)
    if not match:
        raise SecError(f"Period must look like CY2025Q3 (a quarter) or CY2024 (a full year), got '{period}'.")
    year, quarter = match.groups()
    return f"CY{int(year) - 1}{quarter or ''}"


async def merged_frame(client: SecClient, concepts: tuple[str, ...], period: str) -> dict[int, dict[str, Any]]:
    """Fetch several concepts for one period; the first concept with a value wins per company."""
    results: dict[str, list[dict[str, Any]]] = {}

    async def fetch(concept: str) -> None:
        results[concept] = await client.frame(concept, "USD", period)

    async with anyio.create_task_group() as tg:
        for concept in concepts:
            tg.start_soon(fetch, concept)

    merged: dict[int, dict[str, Any]] = {}
    for concept in concepts:
        for row in results.get(concept, []):
            merged.setdefault(row["cik"], {**row, "concept": concept})
    return merged


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return round(100.0 * (current - previous) / previous, 1)


async def screen(
    client: SecClient,
    *,
    period: str | None = None,
    min_revenue_growth_pct: float = 25.0,
    min_revenue_musd: float = 20.0,
    max_revenue_musd: float | None = None,
    min_prev_revenue_musd: float = 10.0,
    require_profitable: bool = True,
    min_net_margin_pct: float | None = None,
    min_gross_margin_pct: float | None = None,
    include_otc: bool = False,
    sort_by: str = "revenue_growth",
    limit: int = 50,
    market: dict[str, dict[str, Any]] | None = None,
    max_pe: float | None = None,
    min_market_cap_musd: float | None = None,
    max_market_cap_musd: float | None = None,
) -> dict[str, Any]:
    """market: price/market-cap table keyed by ticker (market.market_caps). When given, rows get
    an annualized P/E and P/S and the valuation filters apply."""
    period = period or default_period()
    prior = previous_year(period)
    annual_factor = 4 if "Q" in period else 1
    if sort_by not in SORT_KEYS:
        raise SecError(f"sort_by must be one of {', '.join(SORT_KEYS)}")

    frames: dict[str, dict[int, dict[str, Any]]] = {}

    async def load(key: str, concepts: tuple[str, ...], p: str) -> None:
        frames[key] = await merged_frame(client, concepts, p)

    async with anyio.create_task_group() as tg:
        tg.start_soon(load, "rev", REVENUE_CONCEPTS, period)
        tg.start_soon(load, "rev_prev", REVENUE_CONCEPTS, prior)
        tg.start_soon(load, "ni", NET_INCOME_CONCEPTS, period)
        tg.start_soon(load, "ni_prev", NET_INCOME_CONCEPTS, prior)
        tg.start_soon(load, "gp", GROSS_PROFIT_CONCEPTS, period)

    tickers = await ticker_map(client)
    if not frames["rev"]:
        raise SecError(f"No revenue data published for {period} yet. Try an earlier period.")

    rows = []
    for cik, rev_row in frames["rev"].items():
        revenue = rev_row["val"]
        if revenue < min_revenue_musd * 1e6:
            continue
        if max_revenue_musd is not None and revenue > max_revenue_musd * 1e6:
            continue
        prev_row = frames["rev_prev"].get(cik)
        if prev_row is None or prev_row["val"] < min_prev_revenue_musd * 1e6:
            continue  # growth from a tiny base (e.g. a one-off licence fee) is noise
        growth = _growth(revenue, prev_row["val"])
        if growth is None or growth < min_revenue_growth_pct:
            continue
        net_income = frames["ni"].get(cik, {}).get("val")
        if require_profitable and (net_income is None or net_income <= 0):
            continue
        net_margin = None if net_income is None else round(100.0 * net_income / revenue, 1)
        if min_net_margin_pct is not None and (net_margin is None or net_margin < min_net_margin_pct):
            continue
        gross = frames["gp"].get(cik, {}).get("val")
        gross_margin = None if gross is None else round(100.0 * gross / revenue, 1)
        if min_gross_margin_pct is not None and (gross_margin is None or gross_margin < min_gross_margin_pct):
            continue
        listing = tickers.get(cik)
        if listing is None:
            continue  # not exchange-listed (funds, private filers, delisted)
        exchange = listing.get("exchange")
        if not include_otc and (exchange is None or exchange.upper() == "OTC"):
            continue
        prev_ni = frames["ni_prev"].get(cik, {}).get("val")
        valuation: dict[str, Any] = {}
        if market is not None:
            quote = lookup_market(market, listing.get("ticker"))
            cap = (quote or {}).get("market_cap_musd")
            if cap is None and (max_pe is not None or min_market_cap_musd is not None
                                or max_market_cap_musd is not None):
                continue
            if min_market_cap_musd is not None and (cap is None or cap < min_market_cap_musd):
                continue
            if max_market_cap_musd is not None and (cap is None or cap > max_market_cap_musd):
                continue
            yearly = annual_factor * revenue / 1e6
            yearly_ni = None if net_income is None else annual_factor * net_income / 1e6
            pe = round(cap / yearly_ni, 1) if cap and yearly_ni and yearly_ni > 0 else None
            if max_pe is not None and (pe is None or pe > max_pe):
                continue
            valuation = {
                "price": (quote or {}).get("price"),
                "market_cap_musd": cap,
                "pe_annualized": pe,
                "ps_annualized": round(cap / yearly, 2) if cap and yearly > 0 else None,
                "sector": (quote or {}).get("sector"),
                "industry": (quote or {}).get("industry"),
            }
        rows.append({
            "ticker": listing.get("ticker"),
            "name": listing.get("name") or rev_row.get("entityName"),
            "exchange": exchange,
            "cik": cik,
            "period_end": rev_row.get("end"),
            "revenue_musd": round(revenue / 1e6, 1),
            "revenue_prev_year_musd": round(prev_row["val"] / 1e6, 1),
            "revenue_growth_pct": growth,
            "gross_margin_pct": gross_margin,
            "net_income_musd": None if net_income is None else round(net_income / 1e6, 1),
            "net_margin_pct": net_margin,
            "net_income_growth_pct": _growth(net_income, prev_ni),
            "accession": rev_row.get("accn"),
            **valuation,
        })

    key_map = {
        "revenue_growth": "revenue_growth_pct",
        "net_margin": "net_margin_pct",
        "net_income_growth": "net_income_growth_pct",
        "revenue": "revenue_musd",
        "gross_margin": "gross_margin_pct",
        "pe": "pe_annualized",
        "ps": "ps_annualized",
        "market_cap": "market_cap_musd",
    }
    field = key_map[sort_by]
    if sort_by in ASCENDING_SORTS:
        rows.sort(key=lambda r: (r.get(field) is None, r.get(field) or 0))
    else:
        rows.sort(key=lambda r: (r.get(field) is not None, r.get(field) or 0), reverse=True)

    result = {
        "period": period,
        "compared_with": prior,
        "filters": {
            "min_revenue_growth_pct": min_revenue_growth_pct,
            "min_revenue_musd": min_revenue_musd,
            "max_revenue_musd": max_revenue_musd,
            "min_prev_revenue_musd": min_prev_revenue_musd,
            "require_profitable": require_profitable,
            "min_net_margin_pct": min_net_margin_pct,
            "min_gross_margin_pct": min_gross_margin_pct,
            "include_otc": include_otc,
            "max_pe": max_pe,
            "min_market_cap_musd": min_market_cap_musd,
            "max_market_cap_musd": max_market_cap_musd,
        },
        "with_valuation": market is not None,
        "companies_with_revenue_data": len(frames["rev"]),
        "matches": len(rows),
        "results": rows[:limit],
        "notes": [
            "Quarter periods (CYyyyyQn) compare one quarter with the same quarter a year earlier.",
            "SEC maps fiscal periods to the nearest calendar period, so period_end can differ by a few weeks.",
            "A recent period can be incomplete while companies are still filing.",
            "Huge growth from a tiny base or one-off items is common: verify with get_financials and the filings.",
        ],
    }
    if market is None:
        result["notes"].append("No valuation: set with_valuation (or a P/E or market-cap filter) to add it.")
    else:
        result["notes"].append(
            f"pe_annualized and ps_annualized use {'one quarter x 4' if annual_factor == 4 else 'the full year'} "
            "and Nasdaq's market cap, which can count only one share class. Rough screen: confirm with get_quote."
        )
    return result
