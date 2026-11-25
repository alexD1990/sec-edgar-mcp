"""Industry (SIC code) of a company and the sector groups the screener leaves out by default.

Low P/E and fast growth mean something different for a bank, an insurer, a REIT, a fund or a
shipowner (book value, float, NAV and freight rates drive those), and biotech revenue is often
one-off milestone payments. The screener is built for operating companies, so these groups are
left out unless asked for.

SEC frames carry no industry, so the SIC code comes from each company's submissions file. Only the
companies that pass every other screening filter are looked up, and the result is kept for the
life of the process (SIC codes rarely change).
"""

from __future__ import annotations

from typing import Any

import anyio

from .sec_client import SecClient, SecError

# group -> (description, inclusive SIC ranges)
SECTOR_GROUPS: dict[str, tuple[str, list[tuple[int, int]]]] = {
    "funds_trusts": (
        "Funds, ETFs, commodity pools, unit and royalty trusts, SPACs and asset-backed issuers "
        "(SIC 6189, 6221, 6720-6797, 6799)",
        [(6189, 6189), (6221, 6221), (6720, 6797), (6799, 6799)],
    ),
    "banks_finance": (
        "Banks, savings institutions, lenders, brokers and asset managers (SIC 6000-6299 except the above)",
        [(6000, 6188), (6190, 6220), (6222, 6299)],
    ),
    "insurance": ("Insurance carriers and agents (SIC 6300-6499)", [(6300, 6499)]),
    "real_estate": ("REITs and real-estate operators (SIC 6500-6599, 6798)", [(6500, 6599), (6798, 6798)]),
    "shipping": ("Water transportation and shipping (SIC 4400-4499)", [(4400, 4499)]),
    "biotech_pharma": (
        "Pharmaceuticals, biotech, diagnostics and biological research (SIC 2833-2836, 8731)",
        [(2833, 2836), (8731, 8731)],
    ),
}
SECTOR_NAMES = tuple(SECTOR_GROUPS)
DEFAULT_EXCLUDED_SECTORS = SECTOR_NAMES

_industry_cache: dict[int, dict[str, Any]] = {}


def sector_group(sic: int | None) -> str | None:
    """The sector group a SIC code belongs to, or None for an operating company / unknown code."""
    if sic is None:
        return None
    for name, (_, ranges) in SECTOR_GROUPS.items():
        if any(low <= sic <= high for low, high in ranges):
            return name
    return None


def check_sectors(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    unknown = [n for n in names if n not in SECTOR_GROUPS]
    if unknown:
        raise SecError(f"Unknown sector group(s): {', '.join(unknown)}. Choose from: {', '.join(SECTOR_NAMES)}.")
    return tuple(dict.fromkeys(names))


async def company_industry(client: SecClient, cik: int) -> dict[str, Any]:
    """{"sic": 3571, "industry": "Electronic Computers", "sector_group": None} from the submissions file."""
    if cik in _industry_cache:
        return _industry_cache[cik]
    try:
        subs = await client.submissions(cik, cache=False)
    except SecError:
        return {"sic": None, "industry": None, "sector_group": None}  # not cached: may work next time
    try:
        sic = int(subs.get("sic")) if subs.get("sic") not in (None, "") else None
    except (TypeError, ValueError):
        sic = None
    info = {"sic": sic, "industry": subs.get("sicDescription") or None, "sector_group": sector_group(sic)}
    _industry_cache[cik] = info
    return info


async def industries(client: SecClient, ciks: list[int]) -> dict[int, dict[str, Any]]:
    """Industry for many companies at once (the client keeps to SEC's rate limit)."""
    out: dict[int, dict[str, Any]] = {}

    async def one(cik: int) -> None:
        out[cik] = await company_industry(client, cik)

    async with anyio.create_task_group() as tg:
        for cik in dict.fromkeys(ciks):
            tg.start_soon(one, cik)
    return out
