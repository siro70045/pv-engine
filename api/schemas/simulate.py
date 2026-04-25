"""Monolithic /simulate request/response — re-uses the legacy contract.

We re-export the classes living in the original `schemas` module (now sitting
next to `api_legacy.py`) so the `/simulate` endpoint stays bug-for-bug
compatible with the Streamlit wizards while the wizard frontend is being built.

For the school flavour we add a thin wrapper that drives `run_simulation_school()`.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field

# Re-export legacy schemas for retrocompatibility (`api/main.py` imports
# `FullSimulationRequest` / `FullSimulationResponse` from here).
from schemas import SimulationRequest as FullSimulationRequest  # noqa: F401
from schemas import SimulationResponse as FullSimulationResponse  # noqa: F401


class FullSchoolSimulationRequest(BaseModel):
    """Inputs for the school feasibility runner.

    Drives `school.loads_school.calc_school_loads()` + `run_simulation_school()`.
    """
    # Location (PVGIS)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

    # PV
    peak_power_kWp: float = Field(..., gt=0, le=10000)
    tilt_deg: float = Field(default=10.0, ge=0, le=90)
    azimuth_deg: float = Field(default=180.0, ge=0, le=360)

    # Loads (full school inputs as a dict to avoid duplicating ~30 fields)
    school: dict[str, Any] = Field(
        ..., description="SchoolLoadsRequest body (see /loads/school)."
    )

    # Tariff & economics
    import_price_eur_kWh: float = Field(default=0.22, ge=0)
    export_price_eur_kWh: float = Field(default=0.08, ge=0)
    structural_eur: float = Field(default=2000.0, ge=0)
    pnrr_grant: float = Field(default=0.0, ge=0)
    cer_incentive_eur_kWh: float = Field(default=0.0, ge=0)
    cer_fraction: float = Field(default=0.0, ge=0, le=1)

    lifetime_years: int = Field(default=25, ge=5, le=40)
    pv_degradation_pct_per_yr: float = Field(default=0.5, ge=0, le=3)
    inverter_replacement_year: int = Field(default=12, ge=0, le=40)
    inverter_replacement_cost_pct: float = Field(default=0.15, ge=0, le=1)
    capex_eur: Optional[float] = Field(
        default=None, description="If null, derived from estimate_pv_cost_school()."
    )


class FullSchoolSimulationResponse(BaseModel):
    """Compact JSON view of `school.loads_school.run_simulation_school()`."""
    status: str = "ok"
    annual_demand_kWh: float
    annual_production_kWh: float
    self_consumption_rate: float
    self_sufficiency_rate: float
    grid_import_kWh: float
    grid_export_kWh: float
    annual_saving_eur: float
    capex_eur: float
    structural_eur: float
    pnrr_grant: float
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    lcoe_eur_kWh: float
    score: float
    recommendation: str
    monthly_pv_kWh: list[float]
    monthly_demand_kWh: list[float]
    monthly_grid_import_kWh: list[float]
    monthly_grid_export_kWh: list[float]
    breakdown: dict
    climate_zone: str
    region: str
