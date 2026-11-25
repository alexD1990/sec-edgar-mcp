"""Macro and energy context: FRED (St. Louis Fed) and EIA (US Energy Information Administration)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .apiclient import ApiError, JsonApiClient

# ---------------------------------------------------------------- FRED

FRED_UNITS = {
    "lin": "levels",
    "chg": "change",
    "ch1": "change from a year ago",
    "pch": "percent change",
    "pc1": "percent change from a year ago",
    "pca": "compounded annual rate of change",
    "log": "natural log",
}


def _num(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fred_search(client: JsonApiClient, text: str, limit: int = 10) -> dict[str, Any]:
    data = await client.get_json(
        "series/search",
        [("search_text", text), ("limit", str(limit)), ("order_by", "popularity"),
         ("sort_order", "desc"), ("file_type", "json")],
        ttl=24 * 3600,
    )
    series = [
        {
            "id": s.get("id"),
            "title": s.get("title"),
            "frequency": s.get("frequency_short") or s.get("frequency"),
            "units": s.get("units_short") or s.get("units"),
            "seasonal_adjustment": s.get("seasonal_adjustment_short"),
            "observation_start": s.get("observation_start"),
            "observation_end": s.get("observation_end"),
            "last_updated": s.get("last_updated"),
            "popularity": s.get("popularity"),
        }
        for s in data.get("seriess", [])
    ]
    return {"query": text, "count": data.get("count"), "series": series,
            "notes": ["Pass an id to fred_series. Prefer series whose observation_end is recent."]}


def _year_ago(points: list[tuple[str, float]]) -> tuple[str, float] | None:
    latest = date.fromisoformat(points[-1][0])
    target = latest - timedelta(days=365)
    best, best_gap = None, 45
    for d, v in points:
        gap = abs((date.fromisoformat(d) - target).days)
        if gap <= best_gap:
            best, best_gap = (d, v), gap
    return best


async def fred_series(
    client: JsonApiClient,
    series_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    units: str = "lin",
    frequency: str | None = None,
    max_points: int = 120,
) -> dict[str, Any]:
    if units not in FRED_UNITS:
        raise ApiError(f"units must be one of {', '.join(FRED_UNITS)}")
    series_id = series_id.strip().upper()
    meta = await client.get_json("series", [("series_id", series_id), ("file_type", "json")], ttl=24 * 3600)
    info = (meta.get("seriess") or [{}])[0]
    start = start or (date.today() - timedelta(days=365 * 5)).isoformat()
    params = [("series_id", series_id), ("file_type", "json"), ("observation_start", start),
              ("units", units), ("sort_order", "asc")]
    if end:
        params.append(("observation_end", end))
    if frequency:
        params.append(("frequency", frequency))
    data = await client.get_json("series/observations", params, ttl=3600)
    points = [(o["date"], v) for o in data.get("observations", []) if (v := _num(o.get("value"))) is not None]

    summary: dict[str, Any] = {"observations": len(points)}
    if points:
        latest_date, latest = points[-1]
        summary.update({"latest_date": latest_date, "latest_value": latest})
        if len(points) > 1:
            summary["previous_date"], summary["previous_value"] = points[-2]
        ago = _year_ago(points)
        if ago and units == "lin":
            summary["year_ago_date"], summary["year_ago_value"] = ago
            summary["change_1y"] = round(latest - ago[1], 4)
            if ago[1]:
                summary["change_1y_pct"] = round(100 * (latest - ago[1]) / abs(ago[1]), 2)
        values = [v for _, v in points]
        summary["min_in_window"], summary["max_in_window"] = min(values), max(values)

    shown = points[-max_points:]
    return {
        "id": series_id,
        "title": info.get("title"),
        "units": f"{info.get('units')} ({FRED_UNITS[units]})" if units != "lin" else info.get("units"),
        "frequency": info.get("frequency"),
        "seasonal_adjustment": info.get("seasonal_adjustment"),
        "last_updated": info.get("last_updated"),
        "window": [start, end or (points[-1][0] if points else None)],
        "summary": summary,
        "observations": [{"date": d, "value": v} for d, v in shown],
        "truncated": len(points) > len(shown),
        "source": f"https://fred.stlouisfed.org/series/{series_id}",
    }


# ---------------------------------------------------------------- EIA


async def eia_browse(client: JsonApiClient, route: str = "") -> dict[str, Any]:
    route = route.strip("/")
    data = await client.get_json(route or "", [], ttl=24 * 3600)
    resp = data.get("response", {})
    out: dict[str, Any] = {"route": route or "(root)", "name": resp.get("name"), "description": resp.get("description")}
    if resp.get("routes"):
        out["child_routes"] = [
            {"route": f"{route}/{r.get('id')}".strip("/"), "name": r.get("name")} for r in resp["routes"]
        ]
    if resp.get("frequency"):
        out["frequencies"] = [f.get("id") for f in resp["frequency"]]
        out["facets"] = [{"id": f.get("id"), "description": f.get("description")} for f in resp.get("facets", [])]
        out["data_columns"] = {k: (v or {}).get("units") for k, v in (resp.get("data") or {}).items()}
        out["period"] = [resp.get("startPeriod"), resp.get("endPeriod")]
        out["notes"] = ["Leaf route: query it with eia_data(route, data_columns, frequency, facets)."]
    return out


async def eia_data(
    client: JsonApiClient,
    route: str,
    *,
    data_columns: list[str],
    frequency: str,
    facets: dict[str, list[str]] | None = None,
    start: str | None = None,
    end: str | None = None,
    length: int = 500,
) -> dict[str, Any]:
    route = route.strip("/")
    params: list[tuple[str, str]] = [("frequency", frequency)]
    params += [(f"data[{i}]", col) for i, col in enumerate(data_columns)]
    for facet, values in (facets or {}).items():
        params += [(f"facets[{facet}][]", v) for v in values]
    if start:
        params.append(("start", start))
    if end:
        params.append(("end", end))
    params += [("sort[0][column]", "period"), ("sort[0][direction]", "desc"),
               ("offset", "0"), ("length", str(max(1, min(length, 5000))))]
    data = await client.get_json(f"{route}/data", params, ttl=6 * 3600)
    resp = data.get("response", {})
    rows = resp.get("data", [])
    for row in rows:
        for col in data_columns:
            if col in row:
                row[col] = _num(row[col])
    return {
        "route": route,
        "frequency": resp.get("frequency", frequency),
        "total_rows": _num(resp.get("total")),
        "rows_returned": len(rows),
        "rows": rows,
        "warnings": data.get("warnings") or resp.get("warnings") or [],
        "source": f"https://www.eia.gov/opendata/browser/{route}",
    }


SECTORS = {"RES": "residential", "COM": "commercial", "IND": "industrial", "TRA": "transportation",
           "OTH": "other", "ALL": "all sectors"}


def _shift_month(period: str, months: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


async def electricity_sales(
    client: JsonApiClient,
    *,
    states: list[str],
    sector: str = "COM",
    months: int = 24,
    today: date | None = None,
) -> dict[str, Any]:
    """Monthly retail electricity sales by state, with trailing-12-month growth."""
    sector = sector.upper()
    if sector not in SECTORS:
        raise ApiError(f"sector must be one of {', '.join(SECTORS)}")
    states = [s.strip().upper() for s in states if s.strip()]
    if not states:
        raise ApiError("Give at least one state code, e.g. ['VA', 'TX'] or ['US'] for the national total.")
    now = today or date.today()
    start = _shift_month(f"{now.year:04d}-{now.month:02d}", -(months + 14))
    result = await eia_data(
        client, "electricity/retail-sales", data_columns=["sales"], frequency="monthly",
        facets={"stateid": states, "sectorid": [sector]}, start=start, length=5000,
    )
    by_state: dict[str, dict[str, float]] = {}
    names: dict[str, str] = {}
    unit = None
    for row in result["rows"]:
        if row.get("sales") is None:
            continue
        sid = row.get("stateid")
        by_state.setdefault(sid, {})[row["period"]] = row["sales"]
        names[sid] = row.get("stateDescription") or sid
        unit = unit or row.get("sales-units")

    out_states = []
    for sid in states:
        series = by_state.get(sid, {})
        if not series:
            out_states.append({"state": sid, "error": "no data returned"})
            continue
        periods = sorted(series)
        latest = periods[-1]
        entry: dict[str, Any] = {"state": sid, "name": names.get(sid), "latest_month": latest,
                                 "latest_value": series[latest]}
        same_month_last_year = series.get(_shift_month(latest, -12))
        if same_month_last_year:
            entry["latest_month_yoy_pct"] = round(100 * (series[latest] / same_month_last_year - 1), 1)
        last12 = [_shift_month(latest, -i) for i in range(12)]
        prev12 = [_shift_month(latest, -12 - i) for i in range(12)]
        if all(p in series for p in last12 + prev12):
            ttm, prev = sum(series[p] for p in last12), sum(series[p] for p in prev12)
            entry["trailing_12m"] = round(ttm, 1)
            entry["trailing_12m_yoy_pct"] = round(100 * (ttm / prev - 1), 1)
        entry["monthly"] = [{"period": p, "value": series[p]} for p in periods[-months:]]
        out_states.append(entry)

    return {
        "sector": f"{sector} ({SECTORS[sector]})",
        "unit": unit,
        "states": out_states,
        "notes": [
            "Data centers are mostly counted in the commercial (COM) sector; Virginia (VA) and Texas (TX) "
            "have the largest data-center clusters.",
            "EIA retail sales lag about 2-3 months. Use trailing_12m_yoy_pct for the trend; single months "
            "swing with weather.",
        ],
        "source": result["source"],
    }
