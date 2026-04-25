"""PV system request/response models."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class PVProductionRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    peak_power_kWp: float = Field(..., gt=0, le=10000)
    tilt_deg: float = Field(default=15.0, ge=0, le=90)
    azimuth_deg: float = Field(default=180.0, ge=0, le=360)
    mounting: Literal[
        "roof_integrated", "flat_roof_ballast", "ground_fixed", "bipv"
    ] = "flat_roof_ballast"
    degradation_pct_yr: float = Field(default=0.5, ge=0, le=3)
    analysis_year: int = Field(default=1, ge=1, le=40)
    include_hourly: bool = Field(
        default=False,
        description="If true, include the 8760-h profile (~150 kB even compressed).",
    )


class PVProductionResponse(BaseModel):
    annual_production_kWh: float
    specific_yield_kWh_kWp: float
    performance_ratio: float
    monthly_production_kWh: list[float]
    hourly_production_Wh: Optional[list[int]] = None
    cached: bool


class PVCostRequest(BaseModel):
    peak_power_kWp: float = Field(..., gt=0)
    building_type: Literal[
        "residential", "school", "public", "warehouse", "office", "industrial"
    ] = "residential"


class PVCostResponse(BaseModel):
    total_cost_eur: int
    cost_per_kWp: float
    breakdown: dict[str, int]


class PVAutoSizeRequest(BaseModel):
    annual_demand_kWh: float = Field(..., gt=0)
    usable_roof_m2: float = Field(..., gt=0)
    building_type: Literal[
        "residential", "school", "public", "warehouse", "office", "industrial"
    ] = "school"
    yield_kwh_per_kwp: float = Field(default=1200.0, gt=0)
    m2_per_kwp: float = Field(default=5.0, gt=0, le=20)
    scenario: Literal["minimal", "recommended", "maximum", "all"] = "all"


class PVAutoSizeScenario(BaseModel):
    name: Literal["minimal", "recommended", "maximum"]
    kwp: float
    capex_eur: int
    coverage_pct: float
    annual_production_kWh: float


class PVAutoSizeResponse(BaseModel):
    scenarios: list[PVAutoSizeScenario]
    kwp_max_by_roof: float
