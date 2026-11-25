"""Insider transactions from Form 4 filings (officers, directors, 10% owners)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any

import anyio

from .sec_client import SecClient, SecError, archive_url

TRANSACTION_CODES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant or award",
    "M": "exercise/conversion of derivative",
    "X": "exercise of in-the-money option",
    "C": "conversion of derivative",
    "F": "shares withheld to pay tax",
    "G": "gift",
    "D": "disposition to the issuer",
    "J": "other",
    "W": "inheritance",
    "I": "discretionary transaction",
    "K": "equity swap",
    "V": "voluntary report",
    "L": "small acquisition",
    "U": "tender of shares",
    "E": "expiration of short derivative",
    "H": "expiration of long derivative",
    "O": "exercise of out-of-the-money derivative",
    "Z": "voting trust deposit/withdrawal",
}


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    value = node.find("value")
    text = (value.text if value is not None else node.text) or ""
    text = text.strip()
    return text or None


def _flag(el: ET.Element | None, path: str) -> bool:
    return (_text(el, path) or "").lower() in ("1", "true", "yes")


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_form4(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text.strip().encode())
    owners = []
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        roles = []
        if _flag(rel, "isDirector"):
            roles.append("director")
        if _flag(rel, "isOfficer"):
            roles.append(_text(rel, "officerTitle") or "officer")
        if _flag(rel, "isTenPercentOwner"):
            roles.append("10% owner")
        if _flag(rel, "isOther"):
            roles.append(_text(rel, "otherText") or "other")
        owners.append({"name": _text(owner, "reportingOwnerId/rptOwnerName"), "roles": roles})

    transactions = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = _num(_text(tx, "transactionAmounts/transactionShares"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
        transactions.append({
            "date": _text(tx, "transactionDate"),
            "code": code,
            "type": TRANSACTION_CODES.get(code or "", "unknown"),
            "acquired_or_disposed": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
            "shares": shares,
            "price": price,
            "value_usd": round(shares * price, 2) if shares is not None and price else None,
            "shares_owned_after": _num(_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
            "direct_or_indirect": _text(tx, "ownershipNature/directOrIndirectOwnership"),
            "security": _text(tx, "securityTitle"),
        })

    return {
        "issuer_ticker": _text(root, "issuer/issuerTradingSymbol"),
        "period_of_report": _text(root, "periodOfReport"),
        "owners": owners,
        "rule_10b5_1_plan": _flag(root, "aff10b5One"),
        "transactions": transactions,
        "derivative_transactions": len(root.findall("derivativeTable/derivativeTransaction")),
    }


def _raw_xml_name(primary_document: str) -> str:
    # EDGAR lists the rendered version (xslF345X05/form4.xml); the raw XML sits one level up.
    return primary_document.split("/", 1)[1] if primary_document.startswith("xsl") and "/" in primary_document else primary_document


async def insider_trades(
    client: SecClient,
    cik: int,
    *,
    days: int = 90,
    max_filings: int = 60,
    include_all_codes: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    subs = await client.submissions(cik)
    recent = subs.get("filings", {}).get("recent", {})
    cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()

    candidates = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in ("4", "4/A"):
            continue
        filed = recent["filingDate"][i]
        if filed < cutoff:
            continue
        candidates.append((recent["accessionNumber"][i], filed, recent["primaryDocument"][i]))
    truncated = len(candidates) > max_filings
    candidates = candidates[:max_filings]

    parsed: list[tuple[str, str, dict[str, Any]]] = []
    errors: list[str] = []

    async def fetch(accession: str, filed: str, primary: str) -> None:
        try:
            xml_text = await client.archive_text(cik, accession, _raw_xml_name(primary))
            parsed.append((accession, filed, parse_form4(xml_text)))
        except (SecError, ET.ParseError) as exc:
            errors.append(f"{accession}: {exc}")

    async with anyio.create_task_group() as tg:
        for accession, filed, primary in candidates:
            tg.start_soon(fetch, accession, filed, primary)

    rows: list[dict[str, Any]] = []
    seen: dict[tuple, dict[str, Any]] = {}
    for accession, filed, form in sorted(parsed, key=lambda p: p[1], reverse=True):
        owner_names = "; ".join(o["name"] or "?" for o in form["owners"])
        roles = sorted({r for o in form["owners"] for r in o["roles"]})
        for tx in form["transactions"]:
            key = (tx["date"], tx["code"], tx["shares"], tx["price"], tx["shares_owned_after"])
            if key in seen:
                # Same trade reported by several joint owners (e.g. spouses, family trusts).
                seen[key]["also_reported_by"].append(owner_names)
                continue
            row = {
                **tx,
                "insider": owner_names,
                "roles": roles,
                "rule_10b5_1_plan": form["rule_10b5_1_plan"],
                "filed": filed,
                "accession": accession,
                "url": archive_url(cik, accession),
                "also_reported_by": [],
            }
            seen[key] = row
            rows.append(row)

    def summarize(code: str) -> dict[str, Any]:
        selected = [r for r in rows if r["code"] == code]
        return {
            "transactions": len(selected),
            "shares": sum(r["shares"] or 0 for r in selected),
            "value_usd": round(sum(r["value_usd"] or 0 for r in selected), 2),
            "insiders": sorted({r["insider"] for r in selected}),
        }

    buys, sells = summarize("P"), summarize("S")
    code_counts: dict[str, int] = {}
    for r in rows:
        code_counts[r["code"] or "?"] = code_counts.get(r["code"] or "?", 0) + 1

    shown = rows if include_all_codes else [r for r in rows if r["code"] in ("P", "S")]
    for r in shown:
        if not r["also_reported_by"]:
            r.pop("also_reported_by")

    return {
        "cik": cik,
        "company": subs.get("name"),
        "window_days": days,
        "form4_filings_read": len(parsed),
        "more_filings_not_read": truncated,
        "open_market_purchases": buys,
        "open_market_sales": sells,
        "net_open_market_usd": round(buys["value_usd"] - sells["value_usd"], 2),
        "transactions_by_code": {f"{c} ({TRANSACTION_CODES.get(c, 'unknown')})": n for c, n in code_counts.items()},
        "transactions": shown,
        "errors": errors,
        "notes": [
            "Open-market purchases (code P) are the strongest signal; grants (A), tax withholding (F) and "
            "exercises (M) are compensation mechanics.",
            "Sales under a Rule 10b5-1 plan were scheduled in advance and say less about the insider's view.",
            "Identical trades reported by several joint owners are counted once (see also_reported_by).",
        ],
    }
