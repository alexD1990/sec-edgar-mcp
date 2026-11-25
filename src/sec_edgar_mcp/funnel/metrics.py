"""Stage 2 metrics for one company, computed only from its own XBRL facts, its filing list and a price.

Pure functions (no network), so every rule can be tested. See stockdocs/method/stage2-light.md.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..filings import recent_filings
from ..financials import (
    DURATION_METRICS,
    INSTANT_METRICS,
    Metric,
    _facts_for,
    debt_tags_on,
    extract_instants,
    summarize_financials,
)
from ..market import shares_all_classes
from ..sec_client import archive_url
from .params import Params


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _near(a: date, b: date, tolerance_days: int) -> bool:
    return abs((a - b).days) <= tolerance_days


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return round(100.0 * (new / old - 1), 1)


def _m(value: float | None) -> float | None:
    return None if value is None else round(value / 1e6, 2)


# ------------------------------------------------------------------ trailing twelve months

def all_periods(facts: dict[str, Any], metric: Metric) -> list[tuple[date, date, float]]:
    """Every duration value of any length (quarters, year-to-date, years), merged across concepts.
    Higher-priority concepts win; the latest filing wins for the same period."""
    merged: dict[tuple[str, str], float] = {}
    for concept in metric.concepts:
        per_concept: dict[tuple[str, str], dict[str, Any]] = {}
        for fact in _facts_for(facts, metric, concept):
            if "start" not in fact:
                continue
            key = (fact["start"], fact["end"])
            current = per_concept.get(key)
            if current is None or fact.get("filed", "") > current.get("filed", ""):
                per_concept[key] = fact
        for key, fact in per_concept.items():
            merged.setdefault(key, fact["val"])
    return [(_d(s), _d(e), v) for (s, e), v in merged.items()]


def ttm_from_periods(periods: list[tuple[date, date, float]], end: date) -> tuple[float | None, str | None]:
    """Twelve months ending at `end`: a reported 12-month value, or year-to-date + last fiscal year
    - the same year-to-date a year earlier. Cash-flow items are reported year-to-date in 10-Qs,
    so this is the only way to get a trailing figure for them."""
    for start, stop, value in periods:
        if _near(stop, end, 7) and 340 <= (stop - start).days <= 390:
            return value, "12 months reported"
    ytd = [p for p in periods if _near(p[1], end, 7) and 80 <= (p[1] - p[0]).days <= 300]
    for start, stop, value in sorted(ytd, key=lambda p: p[1] - p[0], reverse=True):
        length = (stop - start).days
        full_year = [p for p in periods if 340 <= (p[1] - p[0]).days <= 390 and _near(p[1], start - timedelta(days=1), 10)]
        prior = [p for p in periods if _near(p[1], stop - timedelta(days=365), 14) and abs((p[1] - p[0]).days - length) <= 14]
        if full_year and prior:
            return value + full_year[0][2] - prior[0][2], "year-to-date + last fiscal year - prior year-to-date"
    return None, None


def ttm_from_quarters(rows: list[dict[str, Any]], end: date, key: str) -> float | None:
    """Sum of four consecutive quarter rows (newest first) ending at `end`, in USD millions."""
    for i, row in enumerate(rows):
        if _near(_d(row["period_end"]), end, 14):
            window = rows[i:i + 4]
            if len(window) < 4 or any(r.get(key) is None for r in window):
                return None
            span = (_d(window[0]["period_end"]) - _d(window[3]["period_end"])).days
            return round(sum(r[key] for r in window), 2) if 250 <= span <= 300 else None
    return None


def ttm(facts: dict[str, Any], rows: list[dict[str, Any]], name: str, row_key: str, end: date) -> float | None:
    """TTM in USD millions: from reported periods first, else from four quarter rows."""
    value, _ = ttm_from_periods(all_periods(facts, DURATION_METRICS[name]), end)
    if value is not None:
        return _m(value)
    return ttm_from_quarters(rows, end, row_key)


# ------------------------------------------------------------------ balance sheet

def _instant_near(facts: dict[str, Any], name: str, when: date, tolerance: int = 20) -> float | None:
    values = extract_instants(facts, INSTANT_METRICS[name])
    best = min(values, key=lambda e: abs((_d(e) - when).days), default=None)
    if best is None or not _near(_d(best), when, tolerance):
        return None
    return values[best]["val"]


def debt_estimate(tags: dict[str, float]) -> tuple[float, float, list[str]]:
    """(total interest-bearing debt, debt due within 12 months, tags used), USD millions.

    Companies tag debt in many overlapping ways. Rules: a combined total wins; otherwise long-term debt
    (which normally includes the current part and convertibles) plus short-term borrowings and lines of
    credit; only when no long-term-debt tag exists are notes, convertibles and long-term credit lines added.
    The result is an estimate and is labelled as such in the fact sheet.
    """
    used: list[str] = []

    def take(*names: str) -> float:
        total = 0.0
        for n in names:
            if n in tags:
                total += tags[n]
                used.append(n)
        return total

    if "DebtLongtermAndShorttermCombinedAmount" in tags:
        total = take("DebtLongtermAndShorttermCombinedAmount")
    else:
        if "LongTermDebt" in tags:
            total = take("LongTermDebt")
        elif "LongTermDebtNoncurrent" in tags:
            total = take("LongTermDebtNoncurrent", "LongTermDebtCurrent")
        else:
            # No long-term total: add the pieces (a current portion alone does not include convertibles).
            total = take("LongTermDebtCurrent", "LongTermNotesPayable", "NotesPayable", "ConvertibleNotesPayable",
                         "ConvertibleLongTermNotesPayable", "ConvertibleDebtNoncurrent", "LongTermLineOfCredit")
        total += take("ShortTermBorrowings", "LineOfCredit")
    if "DebtCurrent" in tags:
        due = tags["DebtCurrent"]
    else:
        due = sum(tags.get(n, 0.0) for n in ("LongTermDebtCurrent", "ShortTermBorrowings", "LineOfCredit",
                                              "ConvertibleNotesPayableCurrent"))
    return round(total, 2), round(due, 2), used


# ------------------------------------------------------------------ filings

FILING_FLAGS = {
    "4.01": "auditor change",
    "4.02": "restatement (non-reliance)",
    "2.01": "acquisition completed",
    "5.02": "director/officer change",
}


def filing_flags(subs: dict[str, Any], cik: int, today: date, months: int) -> list[dict[str, Any]]:
    cutoff = (today - timedelta(days=round(months * 30.44))).isoformat()
    found = []
    for f in recent_filings(subs):
        if f["filed"] < cutoff:
            continue
        form = (f["form"] or "").upper()
        url = archive_url(cik, f["accession"], f["primary_document"])
        if form.startswith("8-K"):
            items = [i.strip() for i in (f["items"] or "").split(",")]
            for code, label in FILING_FLAGS.items():
                if code in items:
                    found.append({"flag": code, "what": label, "filed": f["filed"], "accession": f["accession"], "url": url})
        elif form in ("NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"):
            found.append({"flag": "NT", "what": f"late filing ({form})", "filed": f["filed"], "accession": f["accession"], "url": url})
        elif form in ("S-1", "S-3", "S-3ASR") or form.startswith("424B"):
            found.append({"flag": "issuance", "what": f"registration/prospectus ({form})", "filed": f["filed"],
                          "accession": f["accession"], "url": url})
    for flag in found:
        age = (today - _d(flag["filed"])).days
        flag["within_12m"], flag["within_6m"] = age <= 365, age <= 183
    return found


# ------------------------------------------------------------------ valuation

def set_valuation(m: dict[str, Any]) -> None:
    """Market cap, P/E, P/S, EV, EV/EBIT and PEG from price, shares and TTM figures (in place).

    EPS = TTM net income / latest diluted shares, so it is on the same basis as today's price even
    across splits and IPOs (summing reported quarterly EPS is not)."""
    price, shares = m.get("price"), m.get("shares_outstanding")
    ni, diluted = m.get("net_income_ttm"), m.get("diluted_shares")
    per_share = diluted or shares  # some companies (FRD, partnerships) tag no quarterly diluted count
    m["eps_ttm"] = round(ni / per_share, 3) if ni is not None and per_share else None
    m["eps_basis"] = (
        "TTM net income / diluted shares latest quarter" if diluted
        else "TTM net income / shares outstanding (no diluted share count reported)" if shares else None
    )
    m["market_cap"] = round(price * shares, 1) if price and shares else None
    m["pe"] = round(price / m["eps_ttm"], 1) if price and m["eps_ttm"] and m["eps_ttm"] > 0 else None
    m["ps"] = round(m["market_cap"] / m["revenue_ttm"], 2) if m["market_cap"] and m.get("revenue_ttm") else None
    m["ev"] = round(m["market_cap"] - m["net_cash"], 1) if m["market_cap"] is not None and m.get("net_cash") is not None else None
    ebit = m.get("operating_income_ttm")
    m["ev_ebit"] = round(m["ev"] / ebit, 1) if m["ev"] is not None and ebit and ebit > 0 else None
    g = m.get("revenue_growth_ttm_pct")
    m["peg"] = round(m["pe"] / g, 2) if m["pe"] and g and g > 0 else None


# ------------------------------------------------------------------ the analysis

def analyze(
    facts: dict[str, Any], subs: dict[str, Any], *, cik: int, price: float | None, today: date, params: Params
) -> dict[str, Any]:
    """Every stage 2 figure for one company. Money in USD millions, shares in millions."""
    summary = summarize_financials(facts, years=3, quarters=12)
    rows = [r for r in summary["quarterly"] if r.get("revenue_musd") is not None]
    m: dict[str, Any] = {"cik": cik, "company": facts.get("entityName"), "data_gaps": []}
    if not rows:
        m["data_gaps"].append("no quarterly revenue")
        return m

    latest = rows[0]
    end = _d(latest["period_end"])
    m["latest_quarter_end"] = latest["period_end"]
    m["latest_quarter_source"] = latest.get("source_accession")
    year_ago_row = next((r for r in rows if _near(_d(r["period_end"]), end - timedelta(days=365), 14)), None)
    year_ago_end = _d(year_ago_row["period_end"]) if year_ago_row else end - timedelta(days=365)

    # ---- growth by quarter (last four, newest first)
    last4 = rows[:4]
    m["quarters"] = [
        {"end": r["period_end"], "revenue": r.get("revenue_musd"), "growth_pct": r.get("revenue_growth_yoy_pct"),
         "operating_income": r.get("operating_income_musd"), "net_income": r.get("net_income_musd"),
         "derived_q4": "note" in r}
        for r in last4
    ]
    growths = [q["growth_pct"] for q in m["quarters"] if q["growth_pct"] is not None]
    m["growth_quarters_positive"] = sum(1 for g in growths if g > 0)
    m["growth_quarters_known"] = len(growths)
    m["growth_all_above_20"] = len(growths) == 4 and all(g >= 20 for g in growths)
    m["growth_latest_pct"] = latest.get("revenue_growth_yoy_pct")

    # ---- trailing twelve months
    m["revenue_ttm"] = ttm(facts, rows, "revenue", "revenue_musd", end)
    m["revenue_ttm_prior"] = ttm(facts, rows, "revenue", "revenue_musd", year_ago_end)
    m["revenue_growth_ttm_pct"] = _pct(m["revenue_ttm"], m["revenue_ttm_prior"])
    if m["revenue_growth_ttm_pct"] is None and growths:
        m["revenue_growth_ttm_pct"] = round(sum(growths) / len(growths), 1)
        m["data_gaps"].append("TTM revenue growth estimated as the mean of quarterly growth")
    all_rows = summary["quarterly"]
    m["operating_income_ttm"] = ttm(facts, all_rows, "operating_income", "operating_income_musd", end)
    m["net_income_ttm"] = ttm(facts, all_rows, "net_income", "net_income_musd", end)
    m["ocf_ttm"] = ttm(facts, all_rows, "operating_cash_flow", "operating_cash_flow_musd", end)
    capex = ttm(facts, all_rows, "capex", "capex_musd", end)
    m["fcf_ttm"] = None if m["ocf_ttm"] is None or capex is None else round(m["ocf_ttm"] - capex, 2)
    for key in ("operating_income_ttm", "net_income_ttm", "ocf_ttm"):
        if m[key] is None:
            m["data_gaps"].append(f"{key} missing")
    m["operating_margin_ttm_pct"] = (
        round(100 * m["operating_income_ttm"] / m["revenue_ttm"], 1)
        if m["operating_income_ttm"] is not None and m["revenue_ttm"] else None
    )
    m["ocf_to_net_income"] = (
        round(m["ocf_ttm"] / m["net_income_ttm"], 2)
        if m["ocf_ttm"] is not None and m["net_income_ttm"] and m["net_income_ttm"] > 0 else None
    )

    # ---- one-off quarters: profit far above operating profit
    flagged = []
    for q in m["quarters"]:
        ni, op = q["net_income"], q["operating_income"]
        if ni is not None and op is not None and ni > 0 and (op <= 0 or ni > params.non_operating_factor * op):
            q["one_off"] = True
            flagged.append(q)
    m["one_off_quarters"] = [q["end"] for q in flagged]
    m["one_off_share"] = (
        round(sum(q["net_income"] for q in flagged) / m["net_income_ttm"], 2)
        if flagged and m["net_income_ttm"] and m["net_income_ttm"] > 0 else (0.0 if not flagged else None)
    )

    # ---- shares and dilution (diluted weighted average, latest quarter vs a year earlier)
    m["diluted_shares"] = latest.get("diluted_shares_m")
    prior_shares = year_ago_row.get("diluted_shares_m") if year_ago_row else None
    m["dilution_pct"] = _pct(m["diluted_shares"], prior_shares)
    m["share_count_jump"] = bool(m["dilution_pct"] is not None and (m["dilution_pct"] > 100 or m["dilution_pct"] < -50))
    if m["share_count_jump"]:
        m["data_gaps"].append("diluted share count changed more than 2x in a year (split, IPO or restructuring)")
    cover = shares_all_classes(facts)
    m["shares_outstanding"] = cover["shares_m"] if cover else m["diluted_shares"]
    m["shares_source"] = "companyfacts cover page (may miss share classes)" if cover else "diluted shares"

    # ---- working capital vs revenue
    bal_date = _d(summary["balance_sheet_date"]) if summary["balance_sheet_date"] else end
    for name in ("inventory", "receivables"):
        now_v, then_v = _instant_near(facts, name, bal_date), _instant_near(facts, name, bal_date - timedelta(days=365))
        m[f"{name}_growth_pct"] = _pct(now_v, then_v)

    # ---- balance sheet
    cash = _instant_near(facts, "cash", bal_date, 5) or 0.0
    investments = _instant_near(facts, "short_term_investments", bal_date, 5) or 0.0
    m["balance_date"] = summary["balance_sheet_date"]
    m["cash"] = _m(cash + investments)
    debt, due, used = debt_estimate(summary["debt_tags_on_balance_date"])
    m["debt"], m["debt_due_12m"], m["debt_tags_used"] = debt, due, used
    m["net_cash"] = round(m["cash"] - debt, 2)

    m["price"] = price
    set_valuation(m)

    # ---- filings
    m["filing_flags"] = filing_flags(subs, cik, today, params.filing_flag_months)
    m["sic"] = int(subs["sic"]) if str(subs.get("sic") or "").isdigit() else None
    m["industry"] = subs.get("sicDescription")
    latest_10k = next((f for f in recent_filings(subs) if (f["form"] or "").upper() in ("10-K", "20-F", "40-F")), None)
    latest_10q = next((f for f in recent_filings(subs) if (f["form"] or "").upper() == "10-Q"), None)
    m["sources"] = {
        label: {"form": f["form"], "filed": f["filed"], "accession": f["accession"],
                "url": archive_url(cik, f["accession"], f["primary_document"])}
        for label, f in (("latest_10k", latest_10k), ("latest_10q", latest_10q)) if f
    }
    return m
