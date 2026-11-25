"""Small JSON API client for key-based public data APIs (FRED, EIA).

The API key is read from the environment (or .env) on every call and is never included in
results or error messages.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any

import anyio
import httpx2
from mcp.server.mcpserver.exceptions import ToolError

Params = list[tuple[str, str]]


class ApiError(ToolError):
    """An API error the caller can read (the key is redacted)."""


class JsonApiClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        key_env: str,
        *,
        key_param: str = "api_key",
        max_requests_per_second: float = 5.0,
        cache_entries: int = 128,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.key_param = key_param
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(30.0, connect=10.0), follow_redirects=True, transport=transport
        )
        self._interval = 1.0 / max_requests_per_second
        self._next_slot = 0.0
        self._lock = anyio.Lock()
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cache_entries = cache_entries

    def _key(self) -> str:
        key = os.environ.get(self.key_env, "").strip()
        if not key:
            raise ApiError(
                f"{self.key_env} is not set. Add the line {self.key_env}=<your key> to the .env file in the "
                "project folder, then restart the Claude app so the server reloads."
            )
        return key

    def _redact(self, text: str, key: str) -> str:
        return text.replace(key, "***") if key else text

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await anyio.sleep(wait)

    async def get_json(self, path: str, params: Params, *, ttl: float = 3600) -> Any:
        cache_key = f"{path}?{sorted(params)}"
        hit = self._cache.get(cache_key)
        if hit and hit[0] > time.monotonic():
            self._cache.move_to_end(cache_key)
            return hit[1]

        key = self._key()
        url = f"{self.base_url}/{path.lstrip('/')}"
        full_params = [*params, (self.key_param, key)]
        last_error = ""
        for attempt in range(3):
            await self._throttle()
            try:
                response = await self._client.get(url, params=full_params)
            except httpx2.TransportError as exc:
                last_error = type(exc).__name__
                await anyio.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}"
                await anyio.sleep(2.0 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
                message = ""
                if isinstance(data, dict):
                    message = str(data.get("error_message") or data.get("error") or data.get("message") or "")
                message = message or response.text[:300]
                raise ApiError(
                    f"{self.name} error (HTTP {response.status_code}) for {path}: {self._redact(message, key)}"
                )
            self._cache[cache_key] = (time.monotonic() + ttl, data)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_entries:
                self._cache.popitem(last=False)
            return data
        raise ApiError(f"{self.name} did not answer for {path} ({last_error}). Try again shortly.")

    async def aclose(self) -> None:
        await self._client.aclose()


_clients: dict[str, JsonApiClient] = {}

CONFIG = {
    "fred": ("FRED", "https://api.stlouisfed.org/fred", "FRED_API_KEY"),
    "eia": ("EIA", "https://api.eia.gov/v2", "EIA_API_KEY"),
}


def get_api_client(which: str) -> JsonApiClient:
    if which not in _clients:
        name, base, env = CONFIG[which]
        _clients[which] = JsonApiClient(name, base, env)
    return _clients[which]


def set_api_client(which: str, client: JsonApiClient | None) -> None:
    if client is None:
        _clients.pop(which, None)
    else:
        _clients[which] = client
