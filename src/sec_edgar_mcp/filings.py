"""Filing lists, readable text for 10-K/10-Q/8-K sections, and EDGAR full-text search."""

from __future__ import annotations

import re
import warnings
from collections import OrderedDict
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .sec_client import SecClient, SecError, archive_url


def _soup(html: str) -> BeautifulSoup:
    # Inline-XBRL filings are XHTML; the lenient HTML parser handles them fine, so skip bs4's warning.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------- filing lists


def recent_filings(subs: dict[str, Any]) -> list[dict[str, Any]]:
    recent = subs.get("filings", {}).get("recent", {})
    rows = []
    for i, accession in enumerate(recent.get("accessionNumber", [])):
        rows.append({
            "form": recent["form"][i],
            "filed": recent["filingDate"][i],
            "report_date": (recent.get("reportDate") or [None] * (i + 1))[i] or None,
            "accession": accession,
            "primary_document": recent["primaryDocument"][i],
            "description": (recent.get("primaryDocDescription") or [None] * (i + 1))[i] or None,
            "items": (recent.get("items") or [None] * (i + 1))[i] or None,
        })
    return rows


def list_filings(subs: dict[str, Any], cik: int, forms: list[str] | None, limit: int) -> dict[str, Any]:
    wanted = {f.upper() for f in forms} if forms else None
    rows = []
    for row in recent_filings(subs):
        if wanted and row["form"].upper() not in wanted:
            continue
        rows.append({**row, "url": archive_url(cik, row["accession"], row["primary_document"])})
        if len(rows) >= limit:
            break
    return {
        "company": subs.get("name"),
        "cik": cik,
        "tickers": subs.get("tickers"),
        "sic_description": subs.get("sicDescription"),
        "fiscal_year_end": subs.get("fiscalYearEnd"),
        "state_of_incorporation": subs.get("stateOfIncorporation"),
        "filings": rows,
        "notes": ["8-K 'items' codes: 2.02 = results, 4.01 = auditor change, 4.02 = restatement, "
                  "5.02 = director/officer change, 1.01 = material agreement."],
    }


# ---------------------------------------------------------------- html -> text

BLOCK_TAGS = ["p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"]


def html_to_text(html: str) -> str:
    soup = _soup(html)
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    for tag in soup.find_all(lambda t: t.name is not None and t.name.lower().endswith("header") and ":" in t.name):
        tag.decompose()  # inline XBRL header with hidden facts
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c not in ("$", "%", ")")]
        tr.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n" if cells else "\n"))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.append("\n")
    text = soup.get_text("")
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_text_cache: OrderedDict[str, str] = OrderedDict()


async def document_text(client: SecClient, cik: int, accession: str, filename: str) -> str:
    key = f"{cik}/{accession}/{filename}"
    if key in _text_cache:
        _text_cache.move_to_end(key)
        return _text_cache[key]
    raw = await client.archive_text(cik, accession, filename, cache=False)
    text = html_to_text(raw) if re.search(r"<html|<body|<div|<p[ >]", raw[:5000], re.I) else raw
    _text_cache[key] = text
    while len(_text_cache) > 12:
        _text_cache.popitem(last=False)
    return text


# ---------------------------------------------------------------- sections

SEP = r"\s*[\.\:\-–—]?\s*"


def _item(num: str, title: str = "") -> str:
    return rf"item\s*{num}(?![0-9a-z]){SEP}{title}"


SECTIONS: dict[str, dict[str, tuple[str, list[str]]]] = {
    "10-K": {
        "business": (_item("1", "business"), [_item("1a"), _item("1b"), _item("2", "properties")]),
        "risk_factors": (_item("1a", r"risk\s+factors"), [_item("1b"), _item("1c"), _item("2", "properties")]),
        "cybersecurity": (_item("1c", "cybersecurity"), [_item("2", "properties"), _item("3")]),
        "legal_proceedings": (_item("3", r"legal\s+proceedings"), [_item("4"), r"part\s+ii\b"]),
        "market_for_equity": (_item("5", "market"), [_item("6"), _item("7", "management")]),
        "mdna": (_item("7", r"management.{0,3}s\s+discussion"), [_item("7a"), _item("8", "financial")]),
        "market_risk": (_item("7a", "quantitative"), [_item("8", "financial")]),
        "financial_statements": (_item("8", r"financial\s+statements"), [_item("9", "changes"), _item("9a")]),
        "controls_and_procedures": (_item("9a", "controls"), [_item("9b"), _item("9c"), r"part\s+iii\b"]),
    },
    "10-Q": {
        "mdna": (_item("2", r"management.{0,3}s\s+discussion"), [_item("3", "quantitative"), _item("4", "controls")]),
        "market_risk": (_item("3", "quantitative"), [_item("4", "controls")]),
        "controls_and_procedures": (_item("4", "controls"), [r"part\s+ii\b", _item("1", "legal")]),
        "legal_proceedings": (_item("1", r"legal\s+proceedings"), [_item("1a"), _item("2", "unregistered")]),
        "risk_factors": (_item("1a", r"risk\s+factors"), [_item("2", "unregistered"), _item("3", "defaults"), _item("5")]),
        "other_information": (_item("5", r"other\s+information"), [_item("6", "exhibits")]),
    },
}
AUDITOR_RE = re.compile(r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm", re.I)


def extract_section(text: str, form: str, section: str) -> str | None:
    """Pick the longest start->end span; table-of-contents hits produce short spans."""
    base_form = "10-K" if form.upper().startswith("10-K") else "10-Q" if form.upper().startswith("10-Q") else form
    if section == "auditor_report":
        return extract_auditor_reports(text)
    spec = SECTIONS.get(base_form, {}).get(section)
    if spec is None:
        return None
    start_re = re.compile(spec[0], re.I)
    end_res = [re.compile(p, re.I) for p in spec[1]]
    best: tuple[int, int] | None = None
    for match in start_re.finditer(text):
        start = match.start()
        ends = [m.start() for r in end_res if (m := r.search(text, match.end()))]
        end = min(ends) if ends else len(text)
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
    if best is None or best[1] - best[0] < 300:
        return None
    return text[best[0]:best[1]].strip()


def extract_auditor_reports(text: str) -> str | None:
    starts = [m.start() for m in AUDITOR_RE.finditer(text)]
    reports = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:min(end, start + 15000)]
        if re.search(r"\bopinion", chunk[:4000], re.I) and len(chunk) > 800:
            reports.append(chunk.strip())
    if not reports:
        return None
    return "\n\n-----\n\n".join(reports[:3])


def available_sections(form: str) -> list[str]:
    base_form = "10-K" if form.upper().startswith("10-K") else "10-Q" if form.upper().startswith("10-Q") else form
    extra = ["auditor_report"] if base_form == "10-K" else []
    return ["full"] + list(SECTIONS.get(base_form, {})) + extra + (["exhibits"] if base_form in ("8-K", "6-K") else [])


# ---------------------------------------------------------------- read a filing

EXHIBIT_NAME_RE = re.compile(r"ex[a-z]*[-_.]?99|press|release|earnings", re.I)


def parse_index_page(html: str) -> list[dict[str, str]]:
    """Documents listed on a filing's -index.htm page: name, type (e.g. EX-99.1), description."""
    soup = _soup(html)
    docs = []
    for table in soup.find_all("table", class_="tableFile"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            link = cells[2].find("a")
            if link is None or not link.get("href"):
                continue
            name = link["href"].split("?doc=")[-1].rsplit("/", 1)[-1]
            docs.append({
                "name": name,
                "type": cells[3].get_text(strip=True),
                "description": cells[1].get_text(" ", strip=True),
            })
    return docs


async def exhibit_99_documents(client: SecClient, cik: int, accession: str) -> list[str]:
    """Press releases and other EX-99 exhibits of an 8-K."""
    try:
        page = await client.archive_text(cik, accession, f"{accession}-index.htm")
        docs = parse_index_page(page)
        names = [d["name"] for d in docs if d["type"].upper().startswith("EX-99")]
        if docs:
            return [n for n in names if n.lower().endswith((".htm", ".html", ".txt"))]
    except SecError:
        pass
    index = await client.filing_index(cik, accession)
    return [
        item["name"]
        for item in index.get("directory", {}).get("item", [])
        if EXHIBIT_NAME_RE.search(item.get("name", ""))
        and item.get("name", "").lower().endswith((".htm", ".html", ".txt"))
    ]


async def read_filing(
    client: SecClient,
    cik: int,
    *,
    form: str = "10-K",
    section: str = "full",
    accession: str | None = None,
    offset: int = 0,
    max_chars: int = 20000,
    find: str | None = None,
    context_chars: int = 1500,
    max_matches: int = 8,
) -> dict[str, Any]:
    subs = await client.submissions(cik)
    filings = recent_filings(subs)
    if accession:
        match = next((f for f in filings if f["accession"] == accession), None)
        if match is None:
            raise SecError(f"Accession {accession} is not among the company's recent filings.")
    else:
        wanted = form.upper()
        match = next((f for f in filings if f["form"].upper() == wanted), None)
        match = match or next((f for f in filings if f["form"].upper().startswith(wanted)), None)
        if match is None:
            raise SecError(f"No {form} found among the company's recent filings.")

    form_type = match["form"]
    if section not in available_sections(form_type):
        raise SecError(f"Unknown section '{section}' for {form_type}. Available: {', '.join(available_sections(form_type))}")

    parts: list[str] = []
    documents = []
    if section != "exhibits":
        main = await document_text(client, cik, match["accession"], match["primary_document"])
        documents.append(archive_url(cik, match["accession"], match["primary_document"]))
        parts.append(main)
    if form_type.upper() in ("8-K", "8-K/A", "6-K") and section in ("full", "exhibits"):
        for name in await exhibit_99_documents(client, cik, match["accession"]):
            if name == match["primary_document"]:
                continue
            parts.append(f"===== Exhibit: {name} =====\n" + await document_text(client, cik, match["accession"], name))
            documents.append(archive_url(cik, match["accession"], name))

    text = "\n\n".join(parts)
    if section not in ("full", "exhibits"):
        extracted = extract_section(text, form_type, section)
        if extracted is None:
            raise SecError(
                f"Could not locate '{section}' in this {form_type}. It may be incorporated by reference "
                "or use unusual headings; read section='full' with offset paging instead."
            )
        text = extracted

    header = {
        "company": subs.get("name"),
        "form": form_type,
        "filed": match["filed"],
        "report_date": match["report_date"],
        "accession": match["accession"],
        "section": section,
        "documents": documents,
        "total_chars": len(text),
    }
    if find:
        return {**header, **find_in_text(text, find, context_chars=context_chars, max_matches=max_matches)}

    max_chars = max(1000, min(max_chars, 60000))
    chunk = text[offset:offset + max_chars]
    next_offset = offset + len(chunk)
    return {
        "company": subs.get("name"),
        "form": form_type,
        "filed": match["filed"],
        "report_date": match["report_date"],
        "accession": match["accession"],
        "section": section,
        "documents": documents,
        "total_chars": len(text),
        "offset": offset,
        "next_offset": next_offset if next_offset < len(text) else None,
        "text": chunk,
    }


def find_in_text(text: str, query: str, *, context_chars: int = 1500, max_matches: int = 8) -> dict[str, Any]:
    """Snippets around every occurrence of query (case-insensitive, any whitespace between words).

    Nearby hits are merged into one snippet. Each snippet carries its offset so the caller can
    continue reading from there with offset paging.
    """
    words = query.split()
    if not words:
        return {"find": query, "total_matches": 0, "matches": []}
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.I)
    hits = [m.start() for m in pattern.finditer(text)]
    context_chars = max(200, min(context_chars, 6000))
    windows: list[list[int]] = []
    for pos in hits:
        start, end = max(0, pos - context_chars // 3), min(len(text), pos + context_chars)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
            windows[-1][2] += 1
        else:
            windows.append([start, end, 1])
    matches = [
        {"offset": start, "hits": count, "snippet": text[start:end]}
        for start, end, count in windows[: max(1, min(max_matches, 30))]
    ]
    return {
        "find": query,
        "total_matches": len(hits),
        "snippets_returned": len(matches),
        "more_snippets": len(windows) > len(matches),
        "matches": matches,
        "hint": "Read on from a snippet with offset=<its offset> and no find." if matches else
                "No matches. Try a shorter phrase or another word.",
    }


# ---------------------------------------------------------------- full-text search

DISPLAY_RE = re.compile(r"^(?P<name>.*?)\s*(?:\((?P<tickers>[^)]*)\))?\s*\(CIK (?P<cik>\d+)\)\s*$")


async def full_text_search(
    client: SecClient,
    query: str,
    *,
    forms: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query}
    if forms:
        params["category"] = "custom"
        params["forms"] = ",".join(f.upper() for f in forms)
    if start_date or end_date:
        # EDGAR only applies a date filter when both ends are present.
        params["dateRange"] = "custom"
        params["startdt"] = start_date or "2001-01-01"
        params["enddt"] = end_date or date.today().isoformat()
    if page:
        params["from"] = page * 100
    data = await client.full_text_search(params)
    hits = data.get("hits", {})
    total_info = hits.get("total", {})
    total = total_info.get("value")
    capped = total_info.get("relation") == "gte"

    results, companies = [], {}
    for hit in hits.get("hits", []):
        src = hit.get("_source", {})
        hit_id = hit.get("_id", "")
        accession, _, filename = hit_id.partition(":")
        names = src.get("display_names") or []
        ciks = src.get("ciks") or []
        parsed = DISPLAY_RE.match(names[0]) if names else None
        name = parsed.group("name").strip() if parsed else (names[0] if names else None)
        tickers = parsed.group("tickers") if parsed else None
        cik = int(ciks[0]) if ciks else None
        results.append({
            "company": name,
            "tickers": tickers,
            "cik": cik,
            "form": src.get("form") or (src.get("root_forms") or [None])[0],
            "filed": src.get("file_date"),
            "period_ending": src.get("period_ending"),
            "document": filename,
            "url": archive_url(cik, accession, filename) if cik and accession and filename else None,
        })
        if name:
            entry = companies.setdefault(name, {"company": name, "tickers": tickers, "cik": cik, "hits": 0})
            entry["hits"] += 1

    return {
        "query": query,
        "total_hits": f"{total}+" if capped else total,
        "date_range": [params.get("startdt"), params.get("enddt")] if "startdt" in params else None,
        "forms": params.get("forms"),
        "page": page,
        "hits_on_page": len(results),
        "companies": sorted(companies.values(), key=lambda c: c["hits"], reverse=True),
        "results": results,
        "notes": [
            'Use double quotes for exact phrases, e.g. "record backlog". Covers filings since 2001.',
            "The per-company counts cover this page of results only (100 hits per page).",
        ],
    }
