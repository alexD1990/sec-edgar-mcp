"""Thresholds for stage 1 and 2. Defaults mirror stockdocs/method/stage1-screening.md,
stage2-light.md and scoring.md. Override one value on the command line with --set key=value."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..sectors import DEFAULT_EXCLUDED_SECTORS


@dataclass
class Params:
    # ---- stage 1 (screen_companies)
    period: str | None = None                 # e.g. CY2026Q3; None = latest quarter 50+ days old
    min_revenue_musd: float = 10.0            # latest quarter
    max_revenue_musd: float = 500.0
    min_prev_revenue_musd: float = 5.0        # same quarter a year earlier
    min_revenue_growth_pct: float = 20.0
    require_profitable: bool = True
    min_market_cap_musd: float = 50.0
    max_market_cap_musd: float = 5000.0
    include_otc: bool = False
    exclude_sectors: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_SECTORS))

    # ---- stage 2 stops (stage2-light.md)
    min_growth_quarters: int = 3              # S1: growth > 0 in at least this many of the last 4 quarters
    min_ttm_growth_pct: float = 15.0          # S1
    max_one_off_share: float = 0.5            # S3: share of TTM net income from flagged quarters
    non_operating_factor: float = 1.5         # a quarter is flagged if net income > factor x operating income
    max_dilution_pct: float = 15.0            # S5
    max_pe: float = 40.0                      # S7: stop if P/E > max_pe AND PEG > max_peg
    max_peg: float = 1.5
    filing_flag_months: int = 24              # look-back for 8-K / NT / issuance flags

    # ---- stage 2 run
    insider_top: int = 50                     # Form 4 and cover-page shares only for the best N
    insider_days: int = 180
    advance: int = 20                         # how many go on to stage 3
    concurrency: int = 6

    # ---- scoring.md weights (percent)
    weight_growth: float = 20.0
    weight_profitability: float = 20.0
    weight_strength: float = 15.0
    weight_valuation: float = 20.0
    weight_risk: float = 15.0
    weight_insider: float = 10.0
    placeholder_risk: float = 5.0             # risk is set by Opus in stage 3
    placeholder_insider: float = 5.0          # for companies outside insider_top

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    def set(self, assignment: str) -> None:
        """Apply 'key=value', converting value to the field's type."""
        key, sep, raw = assignment.partition("=")
        key, raw = key.strip(), raw.strip()
        known = {f.name: f for f in fields(self)}
        if not sep or key not in known:
            raise ValueError(f"Unknown setting '{assignment}'. Known: {', '.join(known)}")
        current = getattr(self, key)
        value: Any
        if isinstance(current, bool):
            value = raw.lower() in ("1", "true", "yes", "ja")
        elif isinstance(current, int):
            value = int(raw)
        elif isinstance(current, float):
            value = float(raw)
        elif isinstance(current, list):
            value = [p.strip() for p in raw.split(",") if p.strip()]
        elif raw.lower() in ("", "none", "null"):
            value = None
        else:
            value = raw
        setattr(self, key, value)

    @property
    def weights(self) -> dict[str, float]:
        return {
            "growth": self.weight_growth, "profitability": self.weight_profitability,
            "strength": self.weight_strength, "valuation": self.weight_valuation,
            "risk": self.weight_risk, "insider": self.weight_insider,
        }
