"""Turn EDGAR 'companyfacts' (all XBRL facts a company ever filed) into clean
annual and quarterly key figures.

Companies tag the same number differently (and change tags over the years), so each
metric has a list of candidate US-GAAP concepts in priority order. For every period we
take the first concept that has a value, and the most recently filed value wins
(so restatements replace originals).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class Metric:
    concepts: tuple[str, ...]
    unit: str = "USD"
    kind: str = "duration"  # "duration" (flows) or "instant" (balance sheet)
    additive: bool = True  # can Q4 be derived as FY - Q1 - Q2 - Q3?
    approx_q4: bool = False  # not additive, but FY - Q1 - Q2 - Q3 is a usable estimate (EPS)
    taxonomy: str = "us-gaap"


DURATION_METRICS: dict[str, Metric] = {
    "revenue": Metric((
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    )),
    "cost_of_revenue": Metric(("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")),
    "gross_profit": Metric(("GrossProfit",)),
    "research_and_development": Metric(("ResearchAndDevelopmentExpense",)),
    "operating_income": Metric(("OperatingIncomeLoss",)),
    "net_income": Metric(("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss")),
    "eps_diluted": Metric(
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"),
        unit="USD/shares",
        additive=False,
        approx_q4=True,
    ),
    "diluted_shares": Metric(("WeightedAverageNumberOfDilutedSharesOutstanding",), unit="shares", additive=False),
    "operating_cash_flow": Metric((
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    )),
    "capex": Metric(("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")),
}

INSTANT_METRICS: dict[str, Metric] = {
    "cash": Metric(
        ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        kind="instant",
    ),
    "inventory": Metric(("InventoryNet",), kind="instant"),
    "current_assets": Metric(("AssetsCurrent",), kind="instant"),
    "current_liabilities": Metric(("LiabilitiesCurrent",), kind="instant"),
    "total_assets": Metric(("Assets",), kind="instant"),
    "total_liabilities": Metric(("Liabilities",), kind="instant"),
    "long_term_debt": Metric(("LongTermDebt", "LongTermDebtNoncurrent"), kind="instant"),
    "convertible_debt": Metric((
        "ConvertibleNotesPayable",
        "ConvertibleDebtNoncurrent",
        "ConvertibleLongTermNotesPayable",
        "ConvertibleNotesPayableCurrent",
    ), kind="instant"),
    "short_term_debt": Metric(
        ("DebtCurrent", "ShortTermBorrowings", "LongTermDebtCurrent", "LineOfCredit"), kind="instant"
    ),
    "stockholders_equity": Metric(
        ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        kind="instant",
    ),
    "shares_outstanding": Metric(
        ("EntityCommonStockSharesOutstanding",), unit="shares", kind="instant", taxonomy="dei"
    ),
}

QUARTER_DAYS = (80, 100)
ANNUAL_DAYS = (340, 390)


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _duration_days(fact: dict[str, Any]) -> int | None:
    if "start" not in fact:
        return None
    return (_d(fact["end"]) - _d(fact["start"])).days


def _facts_for(facts: dict[str, Any], metric: Metric, concept: str) -> list[dict[str, Any]]:
    node = facts.get("facts", {}).get(metric.taxonomy, {}).get(concept)
    if not node:
        return []
    return node.get("units", {}).get(metric.unit, [])


def _with_origin(fact: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    fact["orig_fy"] = origin.get("orig_fy", origin.get("fy"))
    fact["orig_fp"] = origin.get("orig_fp", origin.get("fp"))
    fact["orig_filed"] = origin.get("orig_filed", origin.get("filed", ""))
    return fact


def extract_durations(facts: dict[str, Any], metric: Metric) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """Return {'annual': {(start,end): fact}, 'quarterly': {...}} merged across concepts."""
    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = {"annual": {}, "quarterly": {}}
    for concept in metric.concepts:
        per_concept: dict[str, dict[tuple[str, str], dict[str, Any]]] = {"annual": {}, "quarterly": {}}
        for fact in _facts_for(facts, metric, concept):
            days = _duration_days(fact)
            if days is None:
                continue
            if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
                bucket = "annual"
            elif QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
                bucket = "quarterly"
            else:
                continue  # year-to-date (6/9 month) values
            key = (fact["start"], fact["end"])
            current = per_concept[bucket].get(key)
            filed = fact.get("filed", "")
            if current is None:
                per_concept[bucket][key] = _with_origin({**fact, "concept": concept}, fact)
                continue
            # Value: the latest filing wins (restatements). Fiscal year/period labels: the
            # earliest filing wins, because later filings repeat old periods as comparatives
            # tagged with the *new* filing's fiscal year.
            origin = current if current["orig_filed"] <= filed else fact
            if filed > current.get("filed", ""):
                current = {**fact, "concept": concept}
            per_concept[bucket][key] = _with_origin(current, origin)
        # Higher-priority concepts win; lower ones only fill periods that are missing.
        for bucket in ("annual", "quarterly"):
            existing_ends = {end for (_, end) in out[bucket]}
            for key, fact in per_concept[bucket].items():
                if key[1] not in existing_ends:
                    out[bucket][key] = fact
    return out


def derive_missing_q4(annual: dict, quarterly: dict) -> dict[tuple[str, str], dict[str, Any]]:
    """Companies usually don't tag Q4 on its own; derive it as FY minus Q1-Q3."""
    derived: dict[tuple[str, str], dict[str, Any]] = {}
    quarter_ends = {_d(end) for (_, end) in quarterly}
    for (a_start, a_end), a_fact in annual.items():
        start, end = _d(a_start), _d(a_end)
        if any(abs((qe - end).days) <= 5 for qe in quarter_ends):
            continue
        inside = [
            (k, f) for k, f in quarterly.items() if _d(k[0]) >= start - timedelta(days=5) and _d(k[1]) < end
        ]
        if len(inside) != 3:
            continue
        last_q_end = max(_d(k[1]) for k, _ in inside)
        q4_start = last_q_end + timedelta(days=1)
        if not QUARTER_DAYS[0] <= (end - q4_start).days <= QUARTER_DAYS[1]:
            continue
        value = a_fact["val"] - sum(f["val"] for _, f in inside)
        derived[(q4_start.isoformat(), a_end)] = {
            "start": q4_start.isoformat(),
            "end": a_end,
            "val": value,
            "filed": a_fact.get("filed"),
            "form": a_fact.get("form"),
            "accn": a_fact.get("accn"),
            "orig_fy": a_fact.get("orig_fy"),
            "orig_fp": "Q4",
            "derived": True,
        }
    return derived


def extract_instants(facts: dict[str, Any], metric: Metric) -> dict[str, dict[str, Any]]:
    """{end_date: fact}, merged across concepts, latest filing wins."""
    out: dict[str, dict[str, Any]] = {}
    for concept in metric.concepts:
        per_concept: dict[str, dict[str, Any]] = {}
        for fact in _facts_for(facts, metric, concept):
            if "start" in fact:
                continue
            current = per_concept.get(fact["end"])
            if current is None or fact.get("filed", "") > current.get("filed", ""):
                per_concept[fact["end"]] = {**fact, "concept": concept}
        for end, fact in per_concept.items():
            out.setdefault(end, fact)
    return out


def _money(value: float | None) -> float | None:
    return None if value is None else round(value / 1e6, 2)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round(100.0 * (current - previous) / abs(previous), 1)


def _find_year_ago(rows: dict[str, dict[str, Any]], end: str) -> dict[str, Any] | None:
    target = _d(end) - timedelta(days=365)
    best, best_gap = None, 21
    for other_end, row in rows.items():
        gap = abs((_d(other_end) - target).days)
        if gap < best_gap:
            best, best_gap = row, gap
    return best


def _build_rows(per_metric: dict[str, dict[tuple[str, str], dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name, periods in per_metric.items():
        for (start, end), fact in periods.items():
            row = rows.setdefault(end, {"period_start": start, "period_end": end, "_raw": {}})
            row["_raw"][name] = fact["val"]
            if fact.get("derived"):
                row["derived_from_annual"] = True
            if fact.get("approx"):
                row.setdefault("_approx", []).append(name)
            if fact.get("orig_fy") and "fiscal_year" not in row:
                row["fiscal_year"] = fact.get("orig_fy")
                row["fiscal_period"] = fact.get("orig_fp")
            row.setdefault("source_form", fact.get("form"))
            row.setdefault("source_accession", fact.get("accn"))
    return rows


def _finish_rows(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    finished = []
    for end in sorted(rows, reverse=True):
        row = rows[end]
        raw = row["_raw"]
        prev = _find_year_ago(rows, end)
        prev_raw = prev["_raw"] if prev else {}
        gross = raw.get("gross_profit")
        if gross is None and raw.get("revenue") is not None and raw.get("cost_of_revenue") is not None:
            gross = raw["revenue"] - raw["cost_of_revenue"]
        fcf = None
        if raw.get("operating_cash_flow") is not None and raw.get("capex") is not None:
            fcf = raw["operating_cash_flow"] - raw["capex"]
        out = {
            "period_start": row["period_start"],
            "period_end": end,
            "fiscal_year": row.get("fiscal_year"),
            "fiscal_period": row.get("fiscal_period"),
            "revenue_musd": _money(raw.get("revenue")),
            "revenue_growth_yoy_pct": _growth(raw.get("revenue"), prev_raw.get("revenue")),
            "gross_profit_musd": _money(gross),
            "gross_margin_pct": _pct(gross, raw.get("revenue")),
            "operating_income_musd": _money(raw.get("operating_income")),
            "operating_margin_pct": _pct(raw.get("operating_income"), raw.get("revenue")),
            "net_income_musd": _money(raw.get("net_income")),
            "net_margin_pct": _pct(raw.get("net_income"), raw.get("revenue")),
            "net_income_growth_yoy_pct": _growth(raw.get("net_income"), prev_raw.get("net_income")),
            "eps_diluted": raw.get("eps_diluted"),
            "diluted_shares_m": None if raw.get("diluted_shares") is None else round(raw["diluted_shares"] / 1e6, 2),
            "research_and_development_musd": _money(raw.get("research_and_development")),
            "operating_cash_flow_musd": _money(raw.get("operating_cash_flow")),
            "capex_musd": _money(raw.get("capex")),
            "free_cash_flow_musd": _money(fcf),
            "source_form": row.get("source_form"),
            "source_accession": row.get("source_accession"),
        }
        if row.get("derived_from_annual"):
            out["note"] = "Q4 derived as full year minus Q1-Q3"
            if row.get("_approx"):
                out["note"] += (
                    f"; {', '.join(row['_approx'])} approximate (share count changes during the year), "
                    "check the earnings release for the reported figure"
                )
        finished.append({k: v for k, v in out.items() if v is not None})
    return finished


DEBT_TAG_RE = re.compile(r"debt|notespayable|borrowing|lineofcredit|loanspayable", re.I)
STALE_DAYS = 100


def debt_tags_on(facts: dict[str, Any], as_of: str) -> dict[str, float]:
    """Every US-GAAP debt-like tag the company reported on one balance-sheet date (USD millions).

    Companies use many different debt tags; this raw list makes sure no borrowing is missed
    even when it is not one of the standard concepts above.
    """
    found: dict[str, tuple[str, float]] = {}
    for concept, node in facts.get("facts", {}).get("us-gaap", {}).items():
        if not DEBT_TAG_RE.search(concept) or "FairValue" in concept or "Issuance" in concept:
            continue
        for fact in node.get("units", {}).get("USD", []):
            if "start" in fact or fact.get("end") != as_of:
                continue
            filed = fact.get("filed", "")
            if concept not in found or filed > found[concept][0]:
                found[concept] = (filed, fact["val"])
    return {concept: round(val / 1e6, 2) for concept, (_, val) in sorted(found.items())}


def summarize_financials(facts: dict[str, Any], years: int = 5, quarters: int = 8) -> dict[str, Any]:
    annual_by_metric: dict[str, dict] = {}
    quarterly_by_metric: dict[str, dict] = {}
    concepts_used: dict[str, list[str]] = {}

    for name, metric in DURATION_METRICS.items():
        periods = extract_durations(facts, metric)
        quarterly = dict(periods["quarterly"])
        if metric.additive or metric.approx_q4:
            derived = derive_missing_q4(periods["annual"], periods["quarterly"])
            if metric.approx_q4:
                for fact in derived.values():
                    fact["val"] = round(fact["val"], 2)
                    fact["approx"] = True
            quarterly.update(derived)
        annual_by_metric[name] = periods["annual"]
        quarterly_by_metric[name] = quarterly
        used = sorted({f.get("concept") for f in list(periods["annual"].values()) + list(periods["quarterly"].values())} - {None})
        if used:
            concepts_used[name] = used

    annual = _finish_rows(_build_rows(annual_by_metric))[:years]
    quarterly = _finish_rows(_build_rows(quarterly_by_metric))[:quarters]

    balance: dict[str, Any] = {}
    for name, metric in INSTANT_METRICS.items():
        values = extract_instants(facts, metric)
        if not values:
            continue
        latest_end = max(values)
        fact = values[latest_end]
        value = fact["val"]
        balance[name] = {
            "value_m" if metric.unit == "shares" else "value_musd": round(value / 1e6, 2),
            "as_of": latest_end,
            "concept": fact.get("concept"),
        }

    # The balance-sheet date is the latest date among USD items (shares are often dated later).
    usd_dates = [v["as_of"] for v in balance.values() if "value_musd" in v]
    balance_date = max(usd_dates) if usd_dates else None
    stale = []
    if balance_date:
        for name, item in balance.items():
            if "value_musd" in item and (_d(balance_date) - _d(item["as_of"])).days > STALE_DAYS:
                item["stale"] = True
                stale.append(name)

    notes = [
        "Money in USD millions. Growth is year-over-year vs. the same period one year earlier.",
        "Figures come straight from the company's XBRL filings; check source_accession for the filing.",
        "Stock price, market cap and P/E: use get_quote (shares_outstanding here is one class only).",
        "Debt is tagged in many ways: read debt_tags_on_balance_date for the full picture, "
        "and the balance sheet in the filing before stating total debt.",
    ]
    if stale:
        notes.append(
            f"STALE: {', '.join(stale)} not reported on {balance_date}; the company probably switched "
            "to another tag. Do not use these values as current."
        )

    return {
        "company": facts.get("entityName"),
        "cik": facts.get("cik"),
        "annual": annual,
        "quarterly": quarterly,
        "balance_sheet_date": balance_date,
        "latest_balance_sheet": balance,
        "debt_tags_on_balance_date": debt_tags_on(facts, balance_date) if balance_date else {},
        "xbrl_concepts_used": concepts_used,
        "notes": notes,
    }
