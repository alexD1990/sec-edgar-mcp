"""Resolve tickers, names and CIK numbers to EDGAR companies."""

from __future__ import annotations

from typing import Any

from .sec_client import SecClient, SecError


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


async def search_companies(client: SecClient, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Exact ticker or CIK first, then name matches (prefix before substring)."""
    query = query.strip()
    if not query:
        return []
    companies = await client.tickers()

    if query.isdigit():
        cik = int(query)
        return [c for c in companies if c["cik"] == cik][:limit]

    ticker = query.upper().replace(".", "-")
    exact = [c for c in companies if (c.get("ticker") or "").upper() == ticker]
    if exact:
        return exact[:limit]

    q = _norm(query)
    prefix, contains = [], []
    for c in companies:
        name = _norm(c.get("name") or "")
        if name.startswith(q):
            prefix.append(c)
        elif q in name:
            contains.append(c)
    # one row per CIK (companies with several share classes appear more than once)
    seen: set[int] = set()
    result = []
    for c in prefix + contains:
        if c["cik"] not in seen:
            seen.add(c["cik"])
            result.append(c)
    return result[:limit]


async def resolve_company(client: SecClient, query: str) -> dict[str, Any]:
    """Return exactly one company or raise a helpful error."""
    matches = await search_companies(client, query, limit=5)
    if not matches:
        if query.strip().isdigit():
            # Not exchange-listed, but still a valid filer: allow raw CIK lookups.
            return {"cik": int(query), "name": None, "ticker": None, "exchange": None}
        raise SecError(f"No EDGAR company matches '{query}'. Try the ticker symbol or use lookup_company.")
    first = matches[0]
    if len({m["cik"] for m in matches}) > 1 and (first.get("ticker") or "").upper() != query.strip().upper():
        options = ", ".join(f"{m['name']} ({m['ticker']})" for m in matches)
        raise SecError(f"'{query}' is ambiguous: {options}. Use the ticker.")
    return first


async def ticker_map(client: SecClient) -> dict[int, dict[str, Any]]:
    """CIK -> first listed share class (used to attach tickers to screening results)."""
    mapping: dict[int, dict[str, Any]] = {}
    for c in await client.tickers():
        mapping.setdefault(c["cik"], c)
    return mapping
