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
from .market import lookup_price
from .sec_client import SecClient, SecError
from .sectors import DEFAULT_EXCLUDED_SECTORS, SECTOR_GROUPS, check_sectors, industries

REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
NET_INCOME_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")
GROSS_PROFIT_CONCEPTS = ("GrossProfit",)
OPERATING_INCOME_CONCEPTS = ("OperatingIncomeLoss",)
# Companies without dilutive securities tag the combined concept only (EOSE, NFE).
EPS_CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")

PERIOD_RE = re.compile(r"^CY(\d{4})(Q[1-4])?$")
SORT_KEYS = ("revenue_growth", "net_margin", "net_income_growth", "revenue", "gross_margin", "pe", "ps", "market_cap")
ASCENDING_SORTS = {"pe", "ps", "market_cap"}  # cheapest / smallest first

# Sanity limits: beyond these the figures are almost always one-off gains, tagging errors or a fund.
MAX_PLAUSIBLE_NET_MARGIN_PCT = 60.0
MIN_PLAUSIBLE_PE = 3.0
NON_OPERATING_FACTOR = 1.5  # net income above 1.5 x operating income = profit mostly from outside the business
FLAG_TEXT = {
    "profit_mostly_non_operating": "net income is positive but operating income is negative or far lower "
                                   "(gains on debt, derivatives, asset sales or tax items)",
    "net_margin_over_60pct": "net margin above 60% (one-off gain, tax benefit or fair-value change)",
    "negative_gross_margin": "negative gross margin",
    "loss_per_share_despite_profit": "net income positive but EPS zero or negative (gains excluded from "
                                     "diluted EPS or attributable to others)",
    "pe_below_3": "P/E below 3 (almost always a one-off gain or a data problem)",
}


def default_period(today: date | None = None, lag_days: int = 50) -> str:
    """Latest calendar quarter that ended at least `lag_days` ago (10-Qs are due 40-45 days after)."""
    cutoff = (today or date.today()) - timedelta(days=lag_days)
    quarter_ends = [(4, 12, 31), (3, 9, 30), (2, 6, 30), (1, 3, 31)]
    for quarter, month, day in quarter_ends:
        if date(cutoff.year, month, day) <= cutoff:
            return f"CY{cutoff.year}Q{quarter}"
    return f"CY{cutoff.year - 1}Q4"


def next_quarter_instant(period: str) -> str:
    """Cover-page share counts are dated after the period ends, so SEC files them under the next quarter."""
    match = PERIOD_RE.match(period)
    if not match:
        raise SecError(f"Period must look like CY2025Q3 (a quarter) or CY2024 (a full year), got '{period}'.")
    year, quarter = int(match.group(1)), match.group(2)
    if quarter is None or quarter == "Q4":
        return f"CY{year + 1}Q1I"
    return f"CY{year}Q{int(quarter[1]) + 1}I"


def previous_year(period: str) -> str:
    match = PERIOD_RE.match(period)
    if not match:
        raise SecError(f"Period must look like CY2025Q3 (a quarter) or CY2024 (a full year), got '{period}'.")
    year, quarter = match.groups()
    return f"CY{int(year) - 1}{quarter or ''}"


def _quarter_parts(period: str) -> tuple[int, int | None]:
    match = PERIOD_RE.match(period)
    if not match:
        raise SecError(f"Period must look like CY2025Q3 (a quarter) or CY2024 (a full year), got '{period}'.")
    year, quarter = match.groups()
    return int(year), (int(quarter[1]) if quarter else None)


def trailing_quarters(period: str) -> list[tuple[int, int]]:
    """The four calendar quarters ending with `period`, newest first."""
    year, quarter = _quarter_parts(period)
    if quarter is None:
        raise SecError("trailing_quarters needs a quarter period such as CY2026Q3.")
    out = []
    for _ in range(4):
        out.append((year, quarter))
        year, quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    return out


def eps_periods(period: str) -> set[str]:
    """Frames needed for trailing EPS: four quarters, plus the full year and Q1-Q3 to derive a missing Q4
    (most companies report Q4 only inside the 10-K's full year)."""
    if _quarter_parts(period)[1] is None:
        return {period}
    needed = set()
    for year, quarter in trailing_quarters(period):
        needed.add(f"CY{year}Q{quarter}")
        if quarter == 4:
            needed.update({f"CY{year}", f"CY{year}Q1", f"CY{year}Q2", f"CY{year}Q3"})
    return needed


def trailing_eps(cik: int, eps: dict[str, dict[int, dict[str, Any]]], period: str) -> tuple[float | None, str | None]:
    """Diluted EPS for the twelve months ending with `period`, and how it was built."""
    def val(p: str) -> float | None:
        row = eps.get(p, {}).get(cik)
        return None if row is None else row["val"]

    if _quarter_parts(period)[1] is None:
        value = val(period)
        return (value, "annual") if value is not None else (None, None)

    values, derived = [], False
    for year, quarter in trailing_quarters(period):
        value = val(f"CY{year}Q{quarter}")
        if value is None and quarter == 4:
            full, parts = val(f"CY{year}"), [val(f"CY{year}Q{i}") for i in (1, 2, 3)]
            if full is not None and all(p is not None for p in parts):
                value, derived = full - sum(parts), True
        values.append(value)

    latest = values[0]
    known = [v for v in values if v is not None]
    if latest is None:
        return None, None
    if len(known) == 4:
        return round(sum(known), 4), "ttm (Q4 derived from full year)" if derived else "ttm"
    if len(known) == 3:  # non-calendar fiscal year: its fiscal Q4 has no quarter frame
        return round(sum(known) * 4 / 3, 4), "3 quarters annualized"
    return round(latest * 4, 4), "1 quarter x 4"


async def merged_frame(
    client: SecClient, concepts: tuple[str, ...], period: str, unit: str = "USD"
) -> dict[int, dict[str, Any]]:
    """Fetch several concepts for one period; the first concept with a value wins per company."""
    results: dict[str, list[dict[str, Any]]] = {}

    async def fetch(concept: str) -> None:
        results[concept] = await client.frame(concept, unit, period)

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
    prices: dict[str, Any] | None = None,
    max_pe: float | None = None,
    min_market_cap_musd: float | None = None,
    max_market_cap_musd: float | None = None,
    exclude_suspect: bool = True,
    exclude_sectors: list[str] | tuple[str, ...] | None = None,
    keep_unknown_market_cap: bool = False,
) -> dict[str, Any]:
    """prices: {"date": ..., "prices": {ticker: close}} from market.all_closes. When given, rows get
    market cap, a trailing P/E and an annualized P/S, and the valuation filters apply.

    exclude_suspect drops rows whose figures fail a sanity check (see FLAG_TEXT); they are listed
    under excluded_suspect instead. With exclude_suspect=False they stay in, with their flags.

    exclude_sectors: sector groups from sectors.SECTOR_GROUPS to leave out (None = all of them:
    funds, banks/finance, insurance, real estate, shipping, biotech/pharma; [] = none). When any group
    is excluded, every remaining match gets its SIC code and industry."""
    sectors_out = check_sectors(DEFAULT_EXCLUDED_SECTORS if exclude_sectors is None else exclude_sectors)
    period = period or default_period()
    prior = previous_year(period)
    annual_factor = 4 if "Q" in period else 1
    if sort_by not in SORT_KEYS:
        raise SecError(f"sort_by must be one of {', '.join(SORT_KEYS)}")

    frames: dict[str, dict[int, dict[str, Any]]] = {}

    async def load(key: str, concepts: tuple[str, ...], p: str) -> None:
        frames[key] = await merged_frame(client, concepts, p)

    eps_frames: dict[str, dict[int, dict[str, Any]]] = {}
    share_frames: dict[str, dict[int, dict[str, Any]]] = {}

    async def load_eps(p: str) -> None:
        eps_frames[p] = await merged_frame(client, EPS_CONCEPTS, p, unit="USD-per-shares")

    async def load_shares(p: str) -> None:
        rows = await client.frame("EntityCommonStockSharesOutstanding", "shares", p, taxonomy="dei")
        share_frames[p] = {row["cik"]: row for row in rows}

    async with anyio.create_task_group() as tg:
        tg.start_soon(load, "rev", REVENUE_CONCEPTS, period)
        tg.start_soon(load, "rev_prev", REVENUE_CONCEPTS, prior)
        tg.start_soon(load, "ni", NET_INCOME_CONCEPTS, period)
        tg.start_soon(load, "ni_prev", NET_INCOME_CONCEPTS, prior)
        tg.start_soon(load, "gp", GROSS_PROFIT_CONCEPTS, period)
        tg.start_soon(load, "op", OPERATING_INCOME_CONCEPTS, period)
        for p in (eps_periods(period) if prices is not None else {period}):
            tg.start_soon(load_eps, p)
        if prices is not None:
            tg.start_soon(load_shares, f"{period}I" if "Q" in period else f"{period}Q4I")
            tg.start_soon(load_shares, next_quarter_instant(period))

    tickers = await ticker_map(client)
    if not frames["rev"]:
        raise SecError(f"No revenue data published for {period} yet. Try an earlier period.")

    rows = []
    excluded: list[dict[str, Any]] = []
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
        operating = frames["op"].get(cik, {}).get("val")
        quarter_eps = (eps_frames.get(period, {}).get(cik) or {}).get("val")

        flags = []
        if net_income is not None and net_income > 0:
            if operating is not None and (operating <= 0 or net_income > NON_OPERATING_FACTOR * operating):
                flags.append("profit_mostly_non_operating")
            if quarter_eps is not None and quarter_eps <= 0:
                flags.append("loss_per_share_despite_profit")
        if net_margin is not None and net_margin > MAX_PLAUSIBLE_NET_MARGIN_PCT:
            flags.append("net_margin_over_60pct")
        if gross_margin is not None and gross_margin < 0:
            flags.append("negative_gross_margin")

        valuation: dict[str, Any] = {}
        if prices is not None:
            price = lookup_price(prices["prices"], listing.get("ticker"))
            if price is None and (max_pe is not None or min_market_cap_musd is not None
                                  or max_market_cap_musd is not None):
                continue
            share_row = next(  # latest instant first
                (rows_[cik] for _, rows_ in sorted(share_frames.items(), reverse=True) if cik in rows_), None
            )
            shares = share_row["val"] if share_row else None
            cap = round(price * shares / 1e6, 1) if price and shares else None
            # SEC frames lack a share count for many companies; the funnel keeps those and checks
            # the size in stage 2 from the company's own filings (keep_unknown_market_cap).
            unknown_ok = cap is None and keep_unknown_market_cap
            if min_market_cap_musd is not None and not unknown_ok and (cap is None or cap < min_market_cap_musd):
                continue
            if max_market_cap_musd is not None and not unknown_ok and (cap is None or cap > max_market_cap_musd):
                continue
            # P/E only from reported EPS: price and EPS are per share, so the share count (one class in
            # frames) cannot distort it, and gains that diluted EPS excludes stay out.
            eps, pe_basis = trailing_eps(cik, eps_frames, period)
            pe = round(price / eps, 1) if price and eps is not None and eps > 0 else None
            if max_pe is not None and (pe is None or pe > max_pe):
                continue
            if pe is not None and pe < MIN_PLAUSIBLE_PE:
                flags.append("pe_below_3")
            yearly = annual_factor * revenue / 1e6
            valuation = {
                "price": price,
                "market_cap_musd": cap,
                "pe": pe,
                "pe_basis": pe_basis,
                "eps_12m": eps,
                "ps_annualized": round(cap / yearly, 2) if cap and yearly > 0 else None,
            }
        if flags and exclude_suspect:
            excluded.append({"ticker": listing.get("ticker"), "name": listing.get("name") or rev_row.get("entityName"),
                             "flags": flags})
            continue
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
            "operating_income_musd": None if operating is None else round(operating / 1e6, 1),
            "accession": rev_row.get("accn"),
            **valuation,
            **({"flags": flags} if flags else {}),
        })

    by_sector: dict[str, list[str]] = {}
    if sectors_out and rows:
        info = await industries(client, [r["cik"] for r in rows])
        kept = []
        for r in rows:
            ind = info.get(r["cik"], {})
            r["sic"], r["industry"] = ind.get("sic"), ind.get("industry")
            group = ind.get("sector_group")
            if group in sectors_out:
                by_sector.setdefault(group, []).append(r["ticker"])
            else:
                kept.append(r)
        rows = kept

    key_map = {
        "revenue_growth": "revenue_growth_pct",
        "net_margin": "net_margin_pct",
        "net_income_growth": "net_income_growth_pct",
        "revenue": "revenue_musd",
        "gross_margin": "gross_margin_pct",
        "pe": "pe",
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
            "exclude_suspect": exclude_suspect,
            "exclude_sectors": list(sectors_out),
        },
        "excluded_by_sector": {
            group: {"count": len(tickers), "tickers": tickers[:15], "what": SECTOR_GROUPS[group][0]}
            for group, tickers in sorted(by_sector.items(), key=lambda kv: -len(kv[1]))
        },
        "excluded_suspect": {
            "count": len(excluded),
            "companies": excluded[:30],
            "flag_meanings": {k: v for k, v in FLAG_TEXT.items()
                              if any(k in e["flags"] for e in excluded) or any(k in r.get("flags", []) for r in rows)},
        },
        "with_valuation": prices is not None,
        "prices_from": None if prices is None else prices.get("date"),
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
    if prices is None:
        result["notes"].append("No valuation: set with_valuation (or a P/E or market-cap filter) to add it.")
    else:
        result["notes"].append(
            f"Valuation uses closing prices from {prices.get('date')}. pe = price / diluted EPS of the last twelve "
            "months; pe_basis says how that EPS was built (a missing Q4 is derived from the full year, a "
            "non-calendar fiscal year gives 3 quarters annualized). ps_annualized uses "
            f"{'one quarter x 4' if annual_factor == 4 else 'the full year'}. "
            "market_cap_musd counts the share class SEC frames report (one class for Class A/B companies), "
            "so it can be too low: confirm with get_quote."
        )
    if by_sector:
        result["notes"].append(
            f"{sum(len(t) for t in by_sector.values())} companies in excluded sector groups were left out (see "
            "excluded_by_sector); pass exclude_sectors=[] or a shorter list to include them."
        )
    if excluded:
        result["notes"].append(
            f"{len(excluded)} companies failed a sanity check and were left out (see excluded_suspect). "
            "Run with exclude_suspect=false to see them with their flags."
        )
    return result
