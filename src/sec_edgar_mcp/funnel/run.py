"""Run stage 1 and stage 2 and write the results to <stockdocs>/runs/<date>/."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import anyio

from ..config import PROJECT_ROOT
from ..insiders import insider_trades
from ..market import cover_page_shares
from ..screener import screen
from ..sec_client import SecClient
from .metrics import analyze, set_valuation
from .params import Params
from .scoring import insider_score, score_company, stops

STAGE1_COLUMNS = [
    "ticker", "name", "exchange", "cik", "sic", "industry", "period_end", "revenue_musd", "revenue_prev_year_musd",
    "revenue_growth_pct", "gross_margin_pct", "operating_income_musd", "net_income_musd", "net_margin_pct",
    "price", "market_cap_musd", "accession",
]
STAGE2_COLUMNS = [
    "rank", "advance", "ticker", "name", "cik", "industry", "total", "s_growth", "s_profitability", "s_strength",
    "s_valuation", "s_risk", "s_insider", "stop", "price", "market_cap", "pe", "peg", "ps", "ev_ebit",
    "revenue_ttm", "revenue_growth_ttm_pct", "growth_latest_pct", "growth_quarters_positive",
    "operating_margin_ttm_pct", "net_income_ttm", "ocf_ttm", "ocf_to_net_income", "fcf_ttm", "one_off_quarters",
    "dilution_pct", "cash", "debt", "net_cash", "inventory_growth_pct", "receivables_growth_pct", "flags",
    "insider_checked", "data_gaps", "latest_quarter_end",
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def git_version() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


# ------------------------------------------------------------------ stage 1

async def run_stage1(client: SecClient, prices: dict[str, Any], p: Params, run_dir: Path, today: date) -> dict[str, Any]:
    log("Steg 1: screening av alle selskaper (SEC frames) ...")
    result = await screen(
        client,
        period=p.period,
        min_revenue_growth_pct=p.min_revenue_growth_pct,
        min_revenue_musd=p.min_revenue_musd,
        max_revenue_musd=p.max_revenue_musd,
        min_prev_revenue_musd=p.min_prev_revenue_musd,
        require_profitable=p.require_profitable,
        include_otc=p.include_otc,
        prices=prices,
        min_market_cap_musd=p.min_market_cap_musd,
        max_market_cap_musd=p.max_market_cap_musd,
        exclude_suspect=True,
        exclude_sectors=p.exclude_sectors,
        keep_unknown_market_cap=True,
        sort_by="revenue_growth",
        limit=100_000,
    )
    rows = result["results"]
    run_dir.mkdir(parents=True, exist_ok=True)
    write_csv(run_dir / "stage1.csv", rows, STAGE1_COLUMNS)
    (run_dir / "stage1_excluded.json").write_text(json.dumps({
        "excluded_by_sector": result["excluded_by_sector"],
        "excluded_suspect": result["excluded_suspect"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "params.json").write_text(json.dumps({
        "date": today.isoformat(), "period": result["period"], "compared_with": result["compared_with"],
        "prices_from": result["prices_from"], "sec_edgar_mcp_version": git_version(),
        "params": json.loads(p.to_json()),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "period": result["period"], "companies_with_revenue_data": result["companies_with_revenue_data"],
        "matches": len(rows), "excluded_suspect": result["excluded_suspect"]["count"],
        "excluded_by_sector": sum(v["count"] for v in result["excluded_by_sector"].values()),
    }
    log(f"Steg 1 ferdig: {summary['matches']} treff av {summary['companies_with_revenue_data']} "
        f"({result['period']}); utelatt: {summary['excluded_suspect']} sanity, {summary['excluded_by_sector']} bransje.")
    unknown = sum(1 for r in rows if r.get("market_cap_musd") is None)
    if unknown:
        log(f"  {unknown} av treffene mangler markedsverdi i SEC frames; størrelsen sjekkes i steg 2.")
    if not 100 <= len(rows) <= 400:
        log("Merk: treffantallet er utenfor 100-400. Vurder å justere terskler i stockdocs/method/stage1-screening.md.")
    return summary


# ------------------------------------------------------------------ stage 2

def _flatten(m: dict[str, Any], s: dict[str, Any], stop_list: list[str]) -> dict[str, Any]:
    row = {k: m.get(k) for k in STAGE2_COLUMNS if k in m}
    row.update({
        "ticker": m.get("ticker"), "name": m.get("company") or m.get("name"), "total": s["total"],
        **{f"s_{k}": v for k, v in s["scores"].items()},
        "stop": " | ".join(stop_list),
        "one_off_quarters": ", ".join(m.get("one_off_quarters", [])),
        "flags": "; ".join(f"{f['flag']} {f['filed']}" for f in m.get("filing_flags", [])),
        "data_gaps": "; ".join(m.get("data_gaps", [])),
        "insider_checked": "yes" if m.get("insider") else "no",
    })
    return row


async def cover_shares(client: SecClient, m: dict[str, Any]) -> None:
    """Replace the share count with all classes from the latest 10-Q/10-K cover page (in place)."""
    try:
        cover = await cover_page_shares(client, m["cik"])
    except Exception as exc:  # noqa: BLE001
        m.setdefault("data_gaps", []).append(f"cover-page shares failed: {exc}")
        return
    m["share_classes"] = cover.get("classes") if cover else None
    if cover:
        m["shares_outstanding"] = cover["shares_m"]
        m["shares_source"] = f"cover page {cover['form']} {cover['filed']} ({cover['accession']})"
        set_valuation(m)


async def run_stage2(client: SecClient, p: Params, run_dir: Path, today: date) -> dict[str, Any]:
    candidates = read_csv(run_dir / "stage1.csv")
    log(f"Steg 2: lett analyse av {len(candidates)} selskaper ...")
    results: list[dict[str, Any]] = []
    done = 0
    limiter = anyio.Semaphore(p.concurrency)

    async def one(c: dict[str, str]) -> None:
        nonlocal done
        cik = int(c["cik"])
        async with limiter:
            try:
                facts = await client.company_facts(cik, cache=False)
                subs = await client.submissions(cik, cache=False)
                m = analyze(facts, subs, cik=cik, price=_num(c.get("price")), today=today, params=p)
            except Exception as exc:  # noqa: BLE001 - one bad company must not stop the run
                m = {"cik": cik, "company": c.get("name"), "error": f"{type(exc).__name__}: {exc}",
                     "data_gaps": ["analysis failed"]}
        m["ticker"] = c.get("ticker")
        results.append(m)
        done += 1
        if done % 25 == 0 or done == len(candidates):
            log(f"  {done}/{len(candidates)}")

    async with anyio.create_task_group() as tg:
        for c in candidates:
            tg.start_soon(one, c)

    def evaluate(m: dict[str, Any]) -> None:
        m["_stops"] = [f"error: {m['error']}"] if m.get("error") else stops(m, p)
        m["_score"] = score_company(m, p, m.get("insider"))

    # Companies without a share count in their XBRL: read the cover page now, so the size stop works.
    no_size = [m for m in results if not m.get("error") and m.get("price") and m.get("market_cap") is None]
    if no_size:
        log(f"Steg 2: aksjetall fra forsiden for {len(no_size)} selskaper uten aksjetall i XBRL ...")
        async with anyio.create_task_group() as tg:
            for m in no_size:
                tg.start_soon(cover_shares, client, m)

    for m in results:
        evaluate(m)

    # Insider trades and cover-page share counts only for the best candidates (many requests each).
    ranked = sorted((m for m in results if not m["_stops"]), key=lambda m: m["_score"]["total"], reverse=True)
    top = ranked[:p.insider_top]
    log(f"Steg 2: innsidehandel og aksjetall fra forsiden for {len(top)} selskaper ...")
    insider_limiter = anyio.Semaphore(3)

    async def deepen(m: dict[str, Any]) -> None:
        async with insider_limiter:
            try:
                trades = await insider_trades(client, m["cik"], days=p.insider_days, max_filings=100, today=today)
                m["insider"] = insider_score(trades)
                m["insider_detail"] = {
                    "buyers": trades["open_market_purchases"]["insiders"],
                    "bought_usd": trades["open_market_purchases"]["value_usd"],
                    "sold_usd": trades["open_market_sales"]["value_usd"],
                    "more_filings_not_read": trades["more_filings_not_read"],
                }
            except Exception as exc:  # noqa: BLE001
                m.setdefault("data_gaps", []).append(f"insider trades failed: {exc}")
            if "share_classes" not in m:
                await cover_shares(client, m)

    async with anyio.create_task_group() as tg:
        for m in top:
            tg.start_soon(deepen, m)
    for m in top:
        evaluate(m)  # P/E-dependent stops and scores again with the corrected share count

    ranked = sorted(results, key=lambda m: (bool(m["_stops"]), -m["_score"]["total"]))
    passed = [m for m in ranked if not m["_stops"]][:p.advance]
    passed_ciks = {m["cik"] for m in passed}
    rows = []
    for i, m in enumerate(ranked, 1):
        row = _flatten(m, m["_score"], m["_stops"])
        row["rank"], row["advance"] = i, "yes" if m["cik"] in passed_ciks else ""
        rows.append(row)
    write_csv(run_dir / "stage2.csv", rows, STAGE2_COLUMNS)

    sheets = run_dir / "sheets"
    sheets.mkdir(exist_ok=True)
    for rank, m in enumerate(passed, 1):
        (sheets / f"{m['ticker']}.md").write_text(fact_sheet(m, rank, today), encoding="utf-8")
    (run_dir / "stage2_summary.md").write_text(summary_markdown(ranked, passed, today), encoding="utf-8")
    append_log(run_dir, len(candidates), [m["ticker"] for m in passed])

    stopped = sum(1 for m in results if m["_stops"])
    log(f"Steg 2 ferdig: {len(passed)} går videre, {stopped} stoppet. Se {run_dir / 'stage2_summary.md'}")
    return {"analysed": len(results), "stopped": stopped, "advance": [m["ticker"] for m in passed]}


# ------------------------------------------------------------------ markdown output

def _f(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "–"
    if isinstance(value, float):
        text = f"{value:,.2f}" if abs(value) < 10 else f"{value:,.1f}"
        return f"{text}{suffix}".replace(",", " ")
    return f"{value}{suffix}"


def fact_sheet(m: dict[str, Any], rank: int, today: date) -> str:
    s = m["_score"]
    lines = [
        f"# {m.get('company')} ({m.get('ticker')}) – tallark steg 2",
        f"Kjørt {today.isoformat()} · rangering {rank} · bransje: {m.get('industry') or '–'} (SIC {m.get('sic') or '–'})",
        "",
        "Alle tall er regnet av kode (sec_edgar_mcp.funnel) fra SEC XBRL. Beløp i USD millioner. "
        "Steg 3 skal ikke regne dem ut på nytt.",
        "",
        "## Verdsettelse",
        "| Kurs | Markedsverdi | P/E (TTM) | PEG | P/S (TTM) | EV/EBIT | Aksjer (mill.) |",
        "|---|---|---|---|---|---|---|",
        f"| {_f(m.get('price'))} | {_f(m.get('market_cap'))} | {_f(m.get('pe'))} | {_f(m.get('peg'))} | "
        f"{_f(m.get('ps'))} | {_f(m.get('ev_ebit'))} | {_f(m.get('shares_outstanding'))} |",
        f"Aksjetall: {m.get('shares_source')}. EPS TTM = {_f(m.get('eps_ttm'))} ({m.get('eps_basis') or 'ikke beregnet'}).",
        "",
        "## Siste tolv måneder (TTM, slutt " + _f(m.get("latest_quarter_end")) + ")",
        "| Omsetning | Vekst | Driftsmargin | Driftsresultat | Nettoresultat | OCF | OCF/netto | FCF |",
        "|---|---|---|---|---|---|---|---|",
        f"| {_f(m.get('revenue_ttm'))} | {_f(m.get('revenue_growth_ttm_pct'), ' %')} | "
        f"{_f(m.get('operating_margin_ttm_pct'), ' %')} | {_f(m.get('operating_income_ttm'))} | "
        f"{_f(m.get('net_income_ttm'))} | {_f(m.get('ocf_ttm'))} | {_f(m.get('ocf_to_net_income'))} | {_f(m.get('fcf_ttm'))} |",
        "",
        "## Kvartaler (nyeste først)",
        "| Kvartal slutt | Omsetning | Vekst år/år | Driftsresultat | Nettoresultat | Merknad |",
        "|---|---|---|---|---|---|",
    ]
    for q in m.get("quarters", []):
        note = ", ".join(n for n in ("engangspost?" if q.get("one_off") else "", "Q4 avledet" if q.get("derived_q4") else "") if n)
        lines.append(f"| {q['end']} | {_f(q['revenue'])} | {_f(q['growth_pct'], ' %')} | {_f(q['operating_income'])} | "
                     f"{_f(q['net_income'])} | {note} |")
    lines += [
        "",
        "## Balanse og kapital (" + _f(m.get("balance_date")) + ")",
        f"- Kontanter + kortsiktige plasseringer: {_f(m.get('cash'))}",
        f"- Rentebærende gjeld (estimat, tagger: {', '.join(m.get('debt_tags_used') or []) or 'ingen'}): {_f(m.get('debt'))}; "
        f"forfaller innen 12 mnd: {_f(m.get('debt_due_12m'))}. **Bekreft i balansen før gjeld brukes i en konklusjon.**",
        f"- Netto kontanter (+) / gjeld (−): {_f(m.get('net_cash'))}",
        f"- Utvanning (utvannede aksjer år/år): {_f(m.get('dilution_pct'), ' %')}",
        f"- Varelager år/år: {_f(m.get('inventory_growth_pct'), ' %')} · Kundefordringer år/år: "
        f"{_f(m.get('receivables_growth_pct'), ' %')} · Omsetning TTM år/år: {_f(m.get('revenue_growth_ttm_pct'), ' %')}",
        "",
        "## Rapportflagg (siste 24 mnd)",
    ]
    flags = m.get("filing_flags") or []
    lines += [f"- {f['flag']} {f['what']}, {f['filed']}: {f['url']}" for f in flags] or ["- Ingen"]
    detail = m.get("insider_detail")
    lines += ["", "## Innsidere (siste 180 dager, åpent marked)"]
    if detail:
        lines.append(f"- Kjøpere: {', '.join(detail['buyers']) or 'ingen'} · kjøpt ${detail['bought_usd']:,.0f} · "
                     f"solgt ${detail['sold_usd']:,.0f}" + (" · ikke alle Form 4 lest" if detail["more_filings_not_read"] else ""))
    else:
        lines.append("- Ikke sjekket i steg 2.")
    lines += [
        "",
        "## Kodepoeng (risiko settes i steg 3)",
        "| Vekst | Lønnsomhet | Finansiell styrke | Verdsettelse | Risiko | Innsidere | Totalt foreløpig |",
        "|---|---|---|---|---|---|---|",
        "| " + " | ".join(str(s["scores"][k]) for k in ("growth", "profitability", "strength", "valuation", "risk", "insider"))
        + f" | {s['total']} |",
        "",
    ]
    names = {"growth": "Vekst", "profitability": "Lønnsomhet", "strength": "Finansiell styrke",
             "valuation": "Verdsettelse", "insider": "Innsidere"}
    lines += [f"- {names[k]}: {'; '.join(s['why'][k])}" for k in names]
    if m.get("data_gaps"):
        lines += ["", "## Datahull", *[f"- {g}" for g in m["data_gaps"]]]
    lines += ["", "## Kilder"]
    for label, src in (m.get("sources") or {}).items():
        lines.append(f"- {label}: {src['form']} {src['filed']}, {src['accession']}: {src['url']}")
    lines.append(f"- Siste kvartal i XBRL: {m.get('latest_quarter_source')}")
    return "\n".join(lines) + "\n"


def summary_markdown(ranked: list[dict[str, Any]], passed: list[dict[str, Any]], today: date) -> str:
    lines = [
        f"# Steg 2 – {today.isoformat()}",
        "",
        f"{len(ranked)} analysert, {len(passed)} går videre til steg 3. Foreløpig totalpoeng har risiko = 5.",
        "",
        "## Videre til steg 3",
        "| # | Ticker | Navn | Bransje | Totalt | Vekst TTM | P/E | PEG | Netto kontanter | Innsidere |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, m in enumerate(passed, 1):
        lines.append(f"| {i} | {m['ticker']} | {m.get('company')} | {m.get('industry') or '–'} | {m['_score']['total']} | "
                     f"{_f(m.get('revenue_growth_ttm_pct'), ' %')} | {_f(m.get('pe'))} | {_f(m.get('peg'))} | "
                     f"{_f(m.get('net_cash'))} | {m['_score']['scores']['insider']} |")
    counts: dict[str, int] = {}
    for m in ranked:
        for stop in m["_stops"]:
            key = stop.split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
    lines += ["", "## Stoppårsaker (antall selskaper)", *[f"- {k}: {v}" for k, v in sorted(counts.items())]]
    return "\n".join(lines) + "\n"


def append_log(run_dir: Path, analysed: int, advanced: list[str]) -> None:
    readme = run_dir.parent / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8")
    try:
        stage1 = len(read_csv(run_dir / "stage1.csv"))
    except OSError:
        stage1 = analysed
    line = f"| {run_dir.name} | {stage1} | {len(advanced)} | | {', '.join(advanced[:8])}{' …' if len(advanced) > 8 else ''} |"
    if line not in text:
        readme.write_text(text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
