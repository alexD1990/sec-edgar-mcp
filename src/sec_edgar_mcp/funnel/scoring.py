"""Stage 2 stops and code-set scores. Mirrors stockdocs/method/stage2-light.md and scoring.md.

Every score is an integer 1-10 with the reasons that moved it, so the fact sheet can show why.
"""

from __future__ import annotations

from typing import Any

from .params import Params


def _clamp(value: float) -> int:
    return int(max(1, min(10, round(value))))


def _band(value: float, bands: list[tuple[float, int]], below: int) -> int:
    """First band whose threshold value is <= x (bands sorted high to low)."""
    for threshold, points in bands:
        if value >= threshold:
            return points
    return below


# ------------------------------------------------------------------ stops

def stops(m: dict[str, Any], p: Params) -> list[str]:
    out = []
    cap = m.get("market_cap")
    if cap is None:
        out.append("S0 market cap unknown (no price or share count)")
    elif not p.min_market_cap_musd <= cap <= p.max_market_cap_musd:
        out.append(f"S0 market cap {cap}M outside {p.min_market_cap_musd:g}-{p.max_market_cap_musd:g}M")
    known, positive = m.get("growth_quarters_known", 0), m.get("growth_quarters_positive", 0)
    growth = m.get("revenue_growth_ttm_pct")
    if known >= 3 and positive < p.min_growth_quarters:
        out.append(f"S1 growth in only {positive} of the last {known} quarters")
    if growth is not None and growth < p.min_ttm_growth_pct:
        out.append(f"S1 TTM revenue growth {growth}% < {p.min_ttm_growth_pct}%")
    if m.get("operating_income_ttm") is not None and m["operating_income_ttm"] <= 0:
        out.append(f"S2 TTM operating income {m['operating_income_ttm']}M <= 0")
    share = m.get("one_off_share")
    if m.get("one_off_quarters") and (share is None or share > p.max_one_off_share):
        out.append(f"S3 one-off quarters {', '.join(m['one_off_quarters'])} carry "
                   f"{'most' if share is None else f'{round(100 * share)}%'} of TTM net income")
    if m.get("ocf_ttm") is not None and m["ocf_ttm"] < 0:
        out.append(f"S4 TTM operating cash flow {m['ocf_ttm']}M < 0")
    dil = m.get("dilution_pct")
    if dil is not None and dil > p.max_dilution_pct and not m.get("share_count_jump"):
        out.append(f"S5 diluted shares +{dil}% in a year")
    serious = [f for f in m.get("filing_flags", []) if f["flag"] in ("4.02", "NT")]
    if serious:
        out.append("S6 " + "; ".join(f"{f['what']} {f['filed']}" for f in serious))
    pe, peg = m.get("pe"), m.get("peg")
    if pe is not None and pe > p.max_pe and (peg is None or peg > p.max_peg):
        out.append(f"S7 P/E {pe} with PEG {peg}")
    return out


# ------------------------------------------------------------------ scores

def growth_score(m: dict[str, Any]) -> tuple[int, list[str]]:
    g = m.get("revenue_growth_ttm_pct")
    if g is None:
        return 3, ["no TTM growth"]
    base = _band(g, [(60, 9), (40, 8), (30, 7), (20, 6), (15, 5)], 3)
    why = [f"TTM growth {g}% -> {base}"]
    score = base
    if m.get("growth_all_above_20"):
        score += 1
        why.append("+1 growth >= 20% in all of the last 4 quarters")
    latest = m.get("growth_latest_pct")
    if latest is not None and g > 0 and latest < g / 2:
        score -= 2
        why.append(f"-2 latest quarter {latest}% is under half of TTM growth")
    acquisitions = [f for f in m.get("filing_flags", []) if f["flag"] == "2.01" and f.get("within_12m")]
    if acquisitions:
        score -= 1
        why.append(f"-1 acquisition (8-K 2.01 {acquisitions[0]['filed']}): growth may be bought")
    return _clamp(score), why


def profitability_score(m: dict[str, Any]) -> tuple[int, list[str]]:
    margin = m.get("operating_margin_ttm_pct")
    if margin is None:
        return 3, ["no TTM operating margin"]
    if margin <= 0:
        return 1, [f"TTM operating margin {margin}%"]
    score = _band(margin, [(20, 8), (10, 7), (5, 6)], 4)
    why = [f"TTM operating margin {margin}% -> {score}"]
    ratio = m.get("ocf_to_net_income")
    if ratio is not None:
        if ratio >= 1.0:
            score += 2
            why.append(f"+2 OCF/net income {ratio}")
        elif ratio < 0.5:
            score -= 2
            why.append(f"-2 OCF/net income {ratio}")
    else:
        why.append("OCF/net income unknown")
    if m.get("one_off_quarters"):
        score -= 1
        why.append("-1 one-off quarter in TTM")
    if m.get("ocf_ttm") is not None and m["ocf_ttm"] < 0:
        score = min(score, 3)
        why.append("max 3: negative TTM OCF")
    return _clamp(score), why


def strength_score(m: dict[str, Any]) -> tuple[int, list[str]]:
    net_cash, ebit = m.get("net_cash"), m.get("operating_income_ttm")
    if net_cash is None:
        return 3, ["no balance sheet"]
    if net_cash >= 0:
        score, why = 8, [f"net cash {net_cash}M -> 8"]
    elif not ebit or ebit <= 0:
        score, why = 2, [f"net debt {-net_cash}M and no operating profit -> 2"]
    else:
        lev = round(-net_cash / ebit, 1)
        score = 7 if lev <= 1 else 6 if lev <= 2 else 4 if lev <= 3 else 2
        why = [f"net debt / EBIT {lev} -> {score}"]
    dil = m.get("dilution_pct")
    if dil is not None and not m.get("share_count_jump"):
        if dil > 10:
            score -= 2
            why.append(f"-2 dilution {dil}%")
        elif dil > 5:
            score -= 1
            why.append(f"-1 dilution {dil}%")
    if m.get("debt_due_12m") and m.get("cash") is not None and m["debt_due_12m"] > m["cash"]:
        score -= 2
        why.append(f"-2 debt due within 12 months {m['debt_due_12m']}M > cash {m['cash']}M")
    if any(f["flag"] == "issuance" and f.get("within_6m") for f in m.get("filing_flags", [])):
        score -= 1
        why.append("-1 share registration/prospectus in the last 6 months")
    return _clamp(score), why


def valuation_score(m: dict[str, Any]) -> tuple[int, list[str]]:
    pe = m.get("pe")
    if pe is None:
        return 2, ["no meaningful P/E"]
    score = _band(-pe, [(-10, 9), (-15, 8), (-20, 7), (-25, 6), (-35, 4)], 2)
    why = [f"P/E {pe} -> {score}"]
    peg = m.get("peg")
    if peg is not None:
        if peg <= 0.5:
            score += 1
            why.append(f"+1 PEG {peg}")
        elif peg > 1.5:
            score -= 1
            why.append(f"-1 PEG {peg}")
    ps = m.get("ps")
    if ps is not None and ps > 10:
        score = min(score, 4)
        why.append(f"max 4: P/S {ps}")
    return _clamp(score), why


def insider_score(trades: dict[str, Any]) -> tuple[int, list[str]]:
    buyers = trades.get("open_market_purchases", {}).get("insiders", [])
    discretionary = sum(
        t.get("value_usd") or 0 for t in trades.get("transactions", [])
        if t.get("code") == "S" and not t.get("rule_10b5_1_plan")
    )
    bought = trades.get("open_market_purchases", {}).get("value_usd", 0)
    if len(buyers) >= 3:
        return 9, [f"{len(buyers)} insiders bought (${bought:,.0f})"]
    if len(buyers) == 2:
        return 8, [f"2 insiders bought (${bought:,.0f})"]
    if len(buyers) == 1:
        return 7, [f"1 insider bought (${bought:,.0f})"]
    if discretionary > 10_000_000:
        return 2, [f"discretionary sales ${discretionary:,.0f}, no buying"]
    if discretionary > 1_000_000:
        return 3, [f"discretionary sales ${discretionary:,.0f}, no buying"]
    return 5, ["no open-market buying, no large discretionary sales"]


def total_score(scores: dict[str, float], p: Params) -> float:
    weights = p.weights
    return round(sum(scores[k] * weights[k] for k in weights) / sum(weights.values()), 1)


def score_company(m: dict[str, Any], p: Params, insider: tuple[int, list[str]] | None = None) -> dict[str, Any]:
    parts = {
        "growth": growth_score(m),
        "profitability": profitability_score(m),
        "strength": strength_score(m),
        "valuation": valuation_score(m),
    }
    scores = {k: v[0] for k, v in parts.items()}
    why = {k: v[1] for k, v in parts.items()}
    scores["risk"] = p.placeholder_risk
    why["risk"] = ["set in stage 3"]
    if insider:
        scores["insider"], why["insider"] = insider
    else:
        scores["insider"], why["insider"] = p.placeholder_insider, ["not checked (outside insider_top)"]
    return {"scores": scores, "why": why, "total": total_score(scores, p)}
