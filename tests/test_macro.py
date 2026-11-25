from __future__ import annotations

import json
from datetime import date

import anyio
import httpx2
import pytest

from sec_edgar_mcp import macro
from sec_edgar_mcp.apiclient import ApiError, JsonApiClient

SECRET = "supersecretkey123"


def run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


def ok(payload, status=200):
    return httpx2.Response(status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})


# ------------------------------------------------------------------ fake FRED

def fred_handler(request: httpx2.Request) -> httpx2.Response:
    params = dict(request.url.params)
    if params.get("api_key") != SECRET:
        return ok({"error_code": 400, "error_message": f"Bad Request. api_key {params.get('api_key')} is not registered."}, 400)
    path = request.url.path
    if path.endswith("/series/search"):
        return ok({"count": 2, "seriess": [
            {"id": "TLDCCONS", "title": "Total Construction Spending: Data Center", "frequency_short": "M",
             "units_short": "Mil. of $", "observation_start": "2014-01-01", "observation_end": "2026-09-01",
             "popularity": 40, "seasonal_adjustment_short": "SAAR"},
            {"id": "OLDSERIES", "title": "Discontinued", "frequency_short": "A", "observation_end": "2010-01-01"},
        ]})
    if path.endswith("/series"):
        return ok({"seriess": [{"id": params["series_id"], "title": "Ten-year yield", "units": "Percent",
                                "frequency": "Monthly", "seasonal_adjustment": "Not Seasonally Adjusted",
                                "last_updated": "2026-11-01"}]})
    if path.endswith("/series/observations"):
        obs = [{"date": f"{y}-{m:02d}-01", "value": str(3 + (y - 2024) * 0.5 + m / 100)}
               for y in (2024, 2025, 2026) for m in range(1, 13) if (y, m) <= (2026, 10)]
        obs.insert(5, {"date": "2024-05-15", "value": "."})  # missing value marker
        return ok({"observations": obs})
    return httpx2.Response(404)


# ------------------------------------------------------------------ fake EIA

def eia_handler(request: httpx2.Request) -> httpx2.Response:
    params = request.url.params
    if params.get("api_key") != SECRET:
        return ok({"error": "API_KEY_INVALID", "code": 403}, 403)
    path = request.url.path
    if path.rstrip("/") == "/v2":
        return ok({"response": {"id": "", "name": "EIA", "routes": [{"id": "electricity", "name": "Electricity"}]}})
    if path.endswith("/electricity/retail-sales"):
        return ok({"response": {"id": "retail-sales", "name": "Retail sales",
                                "frequency": [{"id": "monthly"}, {"id": "annual"}],
                                "facets": [{"id": "stateid", "description": "State"}, {"id": "sectorid", "description": "Sector"}],
                                "data": {"sales": {"units": "million kilowatt hours"}, "price": {"units": "cents per kWh"}},
                                "startPeriod": "2001-01", "endPeriod": "2026-08"}})
    if path.endswith("/electricity/retail-sales/data"):
        assert params.get_list("facets[stateid][]") == ["VA", "TX"]
        assert params.get("sort[0][direction]") == "desc"
        rows = []
        for y in (2024, 2025, 2026):
            for m in range(1, 13):
                if (y, m) > (2026, 8):
                    continue
                # Virginia commercial grows 20% a year, Texas 5%
                rows.append({"period": f"{y}-{m:02d}", "stateid": "VA", "stateDescription": "Virginia",
                             "sectorid": "COM", "sales": str(1000 * 1.2 ** (y - 2024)), "sales-units": "million kilowatt hours"})
                rows.append({"period": f"{y}-{m:02d}", "stateid": "TX", "stateDescription": "Texas",
                             "sectorid": "COM", "sales": 3000 * 1.05 ** (y - 2024), "sales-units": "million kilowatt hours"})
        rows.sort(key=lambda r: r["period"], reverse=True)
        return ok({"response": {"total": str(len(rows)), "frequency": "monthly", "data": rows}})
    return httpx2.Response(404)


@pytest.fixture
def fred(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", SECRET)
    return JsonApiClient("FRED", "https://api.stlouisfed.org/fred", "FRED_API_KEY",
                         max_requests_per_second=1000, transport=httpx2.MockTransport(fred_handler))


@pytest.fixture
def eia(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", SECRET)
    return JsonApiClient("EIA", "https://api.eia.gov/v2", "EIA_API_KEY",
                         max_requests_per_second=1000, transport=httpx2.MockTransport(eia_handler))


# ------------------------------------------------------------------ tests

def test_fred_search(fred):
    result = run(macro.fred_search, fred, "data center construction")
    assert result["series"][0]["id"] == "TLDCCONS"
    assert result["series"][0]["observation_end"] == "2026-09-01"


def test_fred_series_summary(fred):
    result = run(macro.fred_series, fred, "dgs10", start="2024-01-01")
    s = result["summary"]
    assert result["id"] == "DGS10"
    assert s["latest_date"] == "2026-10-01" and s["year_ago_date"] == "2025-10-01"
    assert s["change_1y"] == pytest.approx(0.5)
    assert all(o["value"] is not None for o in result["observations"])  # '.' dropped
    assert result["source"].endswith("/DGS10")
    with pytest.raises(ApiError, match="units must be"):
        run(macro.fred_series, fred, "DGS10", units="bogus")


def test_eia_browse_and_electricity_sales(eia):
    top = run(macro.eia_browse, eia, "")
    assert top["child_routes"][0]["route"] == "electricity"
    leaf = run(macro.eia_browse, eia, "electricity/retail-sales")
    assert leaf["data_columns"]["sales"] == "million kilowatt hours"
    assert "stateid" in [f["id"] for f in leaf["facets"]]

    result = run(macro.electricity_sales, eia, states=["va", "TX"], months=12, today=date(2026, 11, 25))
    va, tx = result["states"]
    assert va["latest_month"] == "2026-08"
    assert va["latest_month_yoy_pct"] == 20.0
    assert va["trailing_12m_yoy_pct"] == pytest.approx(20.0, abs=0.1)
    assert tx["latest_month_yoy_pct"] == 5.0
    assert len(va["monthly"]) == 12
    assert result["unit"] == "million kilowatt hours"


def test_missing_key_and_key_never_leaks(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    client = JsonApiClient("FRED", "https://api.stlouisfed.org/fred", "FRED_API_KEY",
                           transport=httpx2.MockTransport(fred_handler))
    with pytest.raises(ApiError, match="FRED_API_KEY is not set"):
        run(macro.fred_search, client, "x")

    # Wrong key: the API echoes it back in its error message; we must redact it.
    monkeypatch.setenv("FRED_API_KEY", "wrong-key-987")
    client = JsonApiClient("FRED", "https://api.stlouisfed.org/fred", "FRED_API_KEY",
                           max_requests_per_second=1000, transport=httpx2.MockTransport(fred_handler))
    with pytest.raises(ApiError) as err:
        run(macro.fred_search, client, "x")
    assert "wrong-key-987" not in str(err.value) and "***" in str(err.value)
