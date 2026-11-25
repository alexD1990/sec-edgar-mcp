"""Stage 1 and 2 of the funnel: metrics, stops, scores and an end-to-end run on the fake SEC."""

from __future__ import annotations

import csv
from datetime import date

import anyio
import pytest

from fake_sec import ACME, fact, transport
from sec_edgar_mcp import financials, market
from sec_edgar_mcp.funnel import metrics, scoring
from sec_edgar_mcp.funnel.params import Params
from sec_edgar_mcp.funnel.run import run_stage1, run_stage2
from sec_edgar_mcp.sec_client import SecClient, set_client

TODAY = date(2026, 11, 25)
QUARTERS = {
    2024: [("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30"), ("2024-07-01", "2024-09-30")],
    2025: [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30")],
    2026: [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30"), ("2026-07-01", "2026-09-30")],
}


def flow(by_year: dict[int, list[float]], full_years: dict[int, float]) -> list[dict]:
    """Quarterly 10-Q facts plus full-year 10-K facts (values in USD millions)."""
    out = []
    for year, values in by_year.items():
        for (start, end), v in zip(QUARTERS[year], values):
            out.append(fact(start, end, v * 1e6, f"{end[:4]}-{end[5:7]}-28", fy=year, fp="Q", accn=f"q-{end}"))
    for year, v in full_years.items():
        out.append(fact(f"{year}-01-01", f"{year}-12-31", v * 1e6, f"{year + 1}-02-20", form="10-K", fy=year, fp="FY",
                        accn=f"k-{year}"))
    return out


def grow_facts(net_income_2025: float = 45.0) -> dict:
    """A company growing ~40% a year with a clean balance sheet. With net_income_2025=70 its derived
    Q4 2025 (43) is far above operating profit (17): a one-off like AMSC's tax benefit."""
    return {
        "cik": 42, "entityName": "Grow Corp",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                fact(None, "2026-10-31", 20e6, "2026-11-05", accn="q-2026-09-30")]}}},
            "us-gaap": {
                "Revenues": {"units": {"USD": flow(
                    {2024: [80, 85, 90], 2025: [100, 110, 120], 2026: [140, 150, 170]}, {2024: 350, 2025: 460})}},
                "OperatingIncomeLoss": {"units": {"USD": flow(
                    {2024: [6, 6, 7], 2025: [10, 11, 12], 2026: [15, 16, 18]}, {2024: 26, 2025: 50})}},
                "NetIncomeLoss": {"units": {"USD": flow(
                    {2024: [5, 5, 6], 2025: [8, 9, 10], 2026: [12, 12, 14]}, {2024: 22, 2025: net_income_2025})}},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                    fact("2025-07-01", "2025-09-30", 19e6, "2025-10-28", accn="q-2025-09-30"),
                    fact("2026-07-01", "2026-09-30", 20e6, "2026-10-28", accn="q-2026-09-30"),
                ]}},
                # Cash flow is reported year-to-date in 10-Qs
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                    fact("2025-01-01", "2025-09-30", 50e6, "2025-10-28"),
                    fact("2025-01-01", "2025-12-31", 70e6, "2026-02-20", form="10-K"),
                    fact("2026-01-01", "2026-09-30", 60e6, "2026-10-28"),
                ]}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
                    fact("2025-01-01", "2025-09-30", 5e6, "2025-10-28"),
                    fact("2025-01-01", "2025-12-31", 8e6, "2026-02-20", form="10-K"),
                    fact("2026-01-01", "2026-09-30", 6e6, "2026-10-28"),
                ]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [fact(None, "2026-09-30", 100e6, "2026-10-28")]}},
                "LongTermDebt": {"units": {"USD": [fact(None, "2026-09-30", 30e6, "2026-10-28")]}},
            },
        },
    }


def subs(filings: list[tuple[str, str, str]] = ()) -> dict:
    """Submissions file with (form, filed, items) rows."""
    rows = list(filings) + [("10-Q", "2026-10-28", "")]
    return {
        "sic": "3571", "sicDescription": "Electronic Computers",
        "filings": {"recent": {
            "accessionNumber": [f"0042-26-{i:06d}" for i in range(len(rows))],
            "filingDate": [r[1] for r in rows],
            "reportDate": [r[1] for r in rows],
            "form": [r[0] for r in rows],
            "primaryDocument": ["doc.htm"] * len(rows),
            "primaryDocDescription": [""] * len(rows),
            "items": [r[2] for r in rows],
        }},
    }


def analyze(facts=None, sub=None, price=42.0, params=None):
    return metrics.analyze(facts or grow_facts(), sub or subs(), cik=42, price=price, today=TODAY,
                           params=params or Params())


# ------------------------------------------------------------------ metrics

def test_ttm_from_year_to_date_cash_flow():
    periods = metrics.all_periods(grow_facts(), financials.DURATION_METRICS["operating_cash_flow"])
    value, how = metrics.ttm_from_periods(periods, date(2026, 9, 30))
    assert value == 80e6 and how.startswith("year-to-date")       # 60 + 70 - 50


def test_metrics_for_a_clean_grower():
    m = analyze()
    assert [q["growth_pct"] for q in m["quarters"]] == [41.7, 36.4, 40.0, 36.8]
    assert m["growth_all_above_20"] and m["growth_quarters_positive"] == 4
    assert (m["revenue_ttm"], m["revenue_ttm_prior"], m["revenue_growth_ttm_pct"]) == (590.0, 425.0, 38.8)
    assert (m["operating_income_ttm"], m["net_income_ttm"]) == (66.0, 56.0)   # Q4 2025 derived from the year
    assert (m["ocf_ttm"], m["fcf_ttm"], m["ocf_to_net_income"]) == (80.0, 71.0, 1.43)
    assert m["operating_margin_ttm_pct"] == 11.2 and m["one_off_quarters"] == []
    assert m["dilution_pct"] == 5.3
    assert (m["cash"], m["debt"], m["net_cash"]) == (100.0, 30.0, 70.0)
    assert (m["eps_ttm"], m["pe"], m["peg"], m["market_cap"], m["ps"]) == (2.8, 15.0, 0.39, 840.0, 1.42)
    assert scoring.stops(m, Params()) == []

    s = scoring.score_company(m, Params())
    assert s["scores"] == {"growth": 8, "profitability": 9, "strength": 7, "valuation": 9, "risk": 5.0, "insider": 5.0}
    assert s["total"] == 7.5


def test_one_off_quarter_stops_the_company():
    m = analyze(grow_facts(net_income_2025=70.0))
    assert m["one_off_quarters"] == ["2025-12-31"]                     # 43 net vs 17 operating
    assert m["net_income_ttm"] == 81.0 and m["one_off_share"] == 0.53
    assert any(s.startswith("S3") for s in scoring.stops(m, Params()))


def test_eps_is_robust_to_a_split():
    """Summing reported EPS across a 4:1 split gave CLMB a P/E of 8.7; net income / current shares does not."""
    facts = grow_facts()
    facts["facts"]["us-gaap"]["EarningsPerShareDiluted"] = {"units": {"USD/shares": [
        fact("2025-01-01", "2025-12-31", 9.0, "2026-02-20", form="10-K"),  # pre-split full year
    ]}}
    assert analyze(facts)["eps_ttm"] == 2.8


def test_filing_flags_stops_and_scores():
    sub = subs([("8-K", "2026-06-01", "2.01,9.01"), ("NT 10-Q", "2026-05-16", ""), ("S-3", "2026-09-01", ""),
                ("8-K", "2023-01-01", "4.02")])                       # outside 24 months: ignored
    m = analyze(sub=sub)
    assert [f["flag"] for f in m["filing_flags"]] == ["2.01", "NT", "issuance"]
    assert any(s.startswith("S6") for s in scoring.stops(m, Params()))
    assert scoring.growth_score(m)[0] == 7                             # -1 for the acquisition
    assert scoring.strength_score(m)[0] == 6                           # -1 for the S-3


def test_expensive_and_shrinking_companies_stop():
    m = analyze(price=1000.0)                                          # P/E 357, PEG 9
    assert any(s.startswith("S7") for s in scoring.stops(m, Params()))
    m = analyze()
    m["revenue_growth_ttm_pct"], m["growth_quarters_positive"] = 10.0, 2
    assert {s[:2] for s in scoring.stops(m, Params())} == {"S1"}


def test_size_stop_and_eps_without_diluted_shares():
    facts = grow_facts()
    del facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]   # like FRD
    m = analyze(facts)
    assert m["eps_ttm"] == 2.8 and m["pe"] == 15.0                     # 56 / 20M shares outstanding
    assert m["eps_basis"].endswith("(no diluted share count reported)")
    assert not any(s.startswith("S0") for s in scoring.stops(m, Params()))
    big = analyze(price=300.0)                                         # 6 000M market cap
    assert any(s.startswith("S0 market cap 6000") for s in scoring.stops(big, Params()))
    m["market_cap"] = None
    assert any(s.startswith("S0 market cap unknown") for s in scoring.stops(m, Params()))


def test_debt_estimate_rules():
    # A revolver (line of credit) counts as debt due within 12 months
    assert metrics.debt_estimate({"LongTermDebt": 100, "LongTermDebtCurrent": 10, "LineOfCredit": 5})[:2] == (105, 15)
    assert metrics.debt_estimate({"LongTermLineOfCredit": 101.67, "LongTermNotesPayable": 3.49})[0] == 105.16  # FRD
    assert metrics.debt_estimate({"ConvertibleNotesPayable": 2000, "LongTermDebtCurrent": 100})[:2] == (2100, 100)
    assert metrics.debt_estimate({"DebtLongtermAndShorttermCombinedAmount": 50, "LongTermDebt": 40})[0] == 50


def test_insider_scores():
    buy = {"open_market_purchases": {"insiders": ["A", "B"], "value_usd": 100}, "transactions": []}
    assert scoring.insider_score(buy)[0] == 8
    sale = {"code": "S", "value_usd": 12e6, "rule_10b5_1_plan": False}
    planned = {"code": "S", "value_usd": 50e6, "rule_10b5_1_plan": True}
    none = {"open_market_purchases": {"insiders": [], "value_usd": 0}}
    assert scoring.insider_score({**none, "transactions": [sale]})[0] == 2
    assert scoring.insider_score({**none, "transactions": [planned]})[0] == 5   # 10b5-1 sales say little


def test_proxy_statement_facts_are_ignored():
    facts = grow_facts()
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        fact("2025-07-01", "2026-06-30", 112e6, "2026-09-18", form="DEF 14A"))  # QNST-style pay-vs-performance
    rows = financials.summarize_financials(facts)["annual"]
    assert all(r["period_end"] != "2026-06-30" for r in rows)


def test_params_override():
    p = Params()
    p.set("min_revenue_growth_pct=25")
    p.set("exclude_sectors=insurance,shipping")
    p.set("require_profitable=false")
    assert (p.min_revenue_growth_pct, p.exclude_sectors, p.require_profitable) == (25.0, ["insurance", "shipping"], False)
    with pytest.raises(ValueError):
        p.set("nope=1")


# ------------------------------------------------------------------ end to end on the fake SEC

def test_stage1_and_stage2_write_the_run(tmp_path):
    client = SecClient("Test Runner test@example.com", max_requests_per_second=1000, transport=transport())
    set_client(client)
    market._cover_cache.clear()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "README.md").write_text("## Logg\n| Dato | Steg 1 | Steg 2 | Steg 3 | Merknad |\n|---|---|---|---|---|\n")
    run_dir = runs / TODAY.isoformat()
    prices = {"date": "2026-11-23", "prices": {"ACME": 48.0, "SWTH": 9.6, "GAIN": 5.0, "GINS": 10.0}}
    p = Params(period="CY2026Q3")
    try:
        one = anyio.run(lambda: run_stage1(client, prices, p, run_dir, TODAY))
        assert one["matches"] == 2
        with (run_dir / "stage1.csv").open() as f:
            assert [r["ticker"] for r in csv.DictReader(f)] == ["ACME", "SWTH"]   # others fail size/market-cap filters
        assert (run_dir / "params.json").is_file() and (run_dir / "stage1_excluded.json").is_file()

        (run_dir / "sheets").mkdir()
        (run_dir / "sheets" / "OLD.md").write_text("from an earlier run")
        two = anyio.run(lambda: run_stage2(client, p, run_dir, TODAY))
        assert not (run_dir / "sheets" / "OLD.md").exists()
        assert two["advance"] == ["ACME"]
        with (run_dir / "stage2.csv").open() as f:
            rows = {r["ticker"]: r for r in csv.DictReader(f)}
        assert rows["ACME"]["advance"] == "yes" and rows["ACME"]["s_insider"] == "7"   # one insider bought
        assert rows["ACME"]["insider_checked"] == "yes"
        assert rows["SWTH"]["stop"].startswith("error")                 # no companyfacts in the fake
        sheet = (run_dir / "sheets" / "ACME.md").read_text()
        assert "tallark steg 2" in sheet and "cover page 10-Q" in sheet
        assert "| ACME" in (run_dir / "stage2_summary.md").read_text()
        assert f"| {TODAY.isoformat()} | 2 | 1 |" in (runs / "README.md").read_text()
    finally:
        set_client(None)
