"""Thin async client for SEC EDGAR with rate limiting, retries and an in-memory cache.

SEC fair-access rules: at most 10 requests per second, and every request must carry a
User-Agent that identifies you (name + email). See https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any

import anyio
import httpx2
from mcp.server.mcpserver.exceptions import ToolError

DATA_BASE = "https://data.sec.gov"
WWW_BASE = "https://www.sec.gov"
EFTS_BASE = "https://efts.sec.gov/LATEST"

# Cache lifetimes (seconds)
TTL_TICKERS = 24 * 3600
TTL_FACTS = 6 * 3600
TTL_FRAMES = 12 * 3600
TTL_SUBMISSIONS = 30 * 60
TTL_DOCUMENT = 7 * 24 * 3600  # filed documents never change
TTL_SEARCH = 30 * 60


class SecError(ToolError):
    """An error we can explain to the caller; the MCP client sees the message."""


class SecClient:
    def __init__(
        self,
        user_agent: str,
        *,
        max_requests_per_second: float = 8.0,
        cache_entries: int = 256,
        cache_max_bytes: int = 60_000_000,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise SecError(
                "SEC_USER_AGENT must be set to something like 'Ola Nordmann ola@example.com'. "
                "SEC rejects requests without a contact e-mail in the User-Agent."
            )
        self._client = httpx2.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx2.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )
        self._interval = 1.0 / max_requests_per_second
        self._next_slot = 0.0
        self._lock = anyio.Lock()
        # key -> (expires, value, size in bytes of the raw response)
        self._cache: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self._cache_entries = cache_entries
        self._cache_max_bytes = cache_max_bytes
        self._cache_bytes = 0

    # ------------------------------------------------------------------ internals
    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await anyio.sleep(wait)

    def _cache_get(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if item is None:
            return None
        expires, value, _size = item
        if expires < time.monotonic():
            self._cache_drop(key)
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_drop(self, key: str) -> None:
        item = self._cache.pop(key, None)
        if item is not None:
            self._cache_bytes -= item[2]

    def _cache_put(self, key: str, value: Any, ttl: float, size: int) -> None:
        # Large company files (tens of MB) would crowd out everything else on a small server.
        if size > self._cache_max_bytes // 3:
            return
        self._cache_drop(key)
        self._cache[key] = (time.monotonic() + ttl, value, size)
        self._cache_bytes += size
        while self._cache and (len(self._cache) > self._cache_entries or self._cache_bytes > self._cache_max_bytes):
            oldest = next(iter(self._cache))
            self._cache_drop(oldest)

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx2.Response:
        last_error: Exception | None = None
        for attempt in range(4):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params)
            except httpx2.TransportError as exc:  # network hiccup
                last_error = exc
                await anyio.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = SecError(f"SEC returned HTTP {response.status_code} for {url}")
                await anyio.sleep(2.0 * (attempt + 1))
                continue
            if response.status_code == 404:
                raise SecError(f"Not found on EDGAR: {url}")
            if response.status_code == 403:
                raise SecError(
                    "SEC refused the request (HTTP 403). Check that SEC_USER_AGENT contains a real "
                    "name and e-mail, and that the server is not exceeding 10 requests/second."
                )
            response.raise_for_status()
            return response
        raise SecError(f"Giving up on {url}: {last_error}")

    # ------------------------------------------------------------------ public API
    async def get_json(
        self, url: str, *, ttl: float, params: dict[str, Any] | None = None, cache: bool = True
    ) -> Any:
        key = f"json:{url}?{sorted((params or {}).items())}"
        cached = self._cache_get(key) if cache else None
        if cached is not None:
            return cached
        response = await self._request(url, params)
        data = response.json()
        if cache:
            self._cache_put(key, data, ttl, len(response.content))
        return data

    async def get_text(self, url: str, *, ttl: float, cache: bool = True) -> str:
        key = f"text:{url}"
        cached = self._cache_get(key) if cache else None
        if cached is not None:
            return cached
        response = await self._request(url)
        text = response.text
        if cache:
            self._cache_put(key, text, ttl, len(response.content))
        return text

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ endpoints
    async def tickers(self) -> list[dict[str, Any]]:
        """All exchange-listed companies known to EDGAR: cik, name, ticker, exchange."""
        raw = await self.get_json(f"{WWW_BASE}/files/company_tickers_exchange.json", ttl=TTL_TICKERS)
        fields = raw["fields"]
        return [dict(zip(fields, row)) for row in raw["data"]]

    async def company_facts(self, cik: int) -> dict[str, Any]:
        return await self.get_json(f"{DATA_BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json", ttl=TTL_FACTS)

    async def submissions(self, cik: int, *, cache: bool = True) -> dict[str, Any]:
        """cache=False for bulk lookups (e.g. the SIC code of every screening match), so hundreds of
        filing histories do not push the frames out of the cache."""
        return await self.get_json(f"{DATA_BASE}/submissions/CIK{cik:010d}.json", ttl=TTL_SUBMISSIONS, cache=cache)

    async def frame(self, concept: str, unit: str, period: str, taxonomy: str = "us-gaap") -> list[dict[str, Any]]:
        """One XBRL concept for every reporting company in one period (e.g. CY2025Q3, CY2024)."""
        url = f"{DATA_BASE}/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
        try:
            data = await self.get_json(url, ttl=TTL_FRAMES)
        except SecError as exc:
            if "Not found" in str(exc):
                return []
            raise
        return data.get("data", [])

    async def filing_index(self, cik: int, accession: str) -> dict[str, Any]:
        acc = accession.replace("-", "")
        return await self.get_json(f"{WWW_BASE}/Archives/edgar/data/{cik}/{acc}/index.json", ttl=TTL_DOCUMENT)

    async def archive_text(self, cik: int, accession: str, filename: str, *, cache: bool = True) -> str:
        acc = accession.replace("-", "")
        url = f"{WWW_BASE}/Archives/edgar/data/{cik}/{acc}/{filename}"
        return await self.get_text(url, ttl=TTL_DOCUMENT, cache=cache)

    async def full_text_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.get_json(f"{EFTS_BASE}/search-index", ttl=TTL_SEARCH, params=params)


def archive_url(cik: int, accession: str, filename: str | None = None) -> str:
    acc = accession.replace("-", "")
    base = f"{WWW_BASE}/Archives/edgar/data/{cik}/{acc}"
    return f"{base}/{filename}" if filename else f"{base}/{accession}-index.htm"


_client: SecClient | None = None


def get_client() -> SecClient:
    """Process-wide client, created on first use from the SEC_USER_AGENT env var."""
    global _client
    if _client is None:
        _client = SecClient(os.environ.get("SEC_USER_AGENT", ""))
    return _client


def set_client(client: SecClient | None) -> None:
    """Swap the process-wide client (used by tests)."""
    global _client
    _client = client
