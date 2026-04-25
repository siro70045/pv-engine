"""Economics, energy-balance and incentives request/response models."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class EnergyBalanceRequest(BaseModel):
    pv_hourly_Wh: list[float] = Field(..., min_length=8760, max_length=8784)
    demand_hourly_Wh: list[float] = Field(..., min_length=8760, max_length=8784)
    battery_kWh: float = Field(default=0.0, ge=0)
    import_price_eur_kWh: float = Field(default=0.22, ge=0)
    export_price_eur_kWh: float = Field(default=0.08, ge=0)
    peak_power_kWp: float = Field(default=0.0, ge=0)
    battery_strategy: Literal["self_consumption", "peak_shaving"] = "self_consumption"


class EnergyBalanceResponse(BaseModel):
    self_consumption_rate: float
    self_sufficiency_rate: float
    grid_import_kWh: float
    grid_export_kWh: float
    self_consumed_kWh: float
    annual_saving_eur: float
    export_revenue_eur: float
    pv_production_kWh: float
    total_demand_kWh: float
    monthly_pv_kWh: list[float]
    monthly_demand_kWh: list[float]
    monthly_grid_import_kWh: list[float]
    monthly_grid_export_kWh: list[float]


class KPIRequest(BaseModel):
    capex_eur: float = Field(..., gt=0)
    annual_saving_eur: float = Field(..., ge=0)
    annual_production_kWh: float = Field(default=0.0, ge=0)
    lifetime_years: int = Field(default=25, ge=5, le=40)
    discount_rate: float = Field(default=0.05, ge=0, le=0.30)
    escalation: float = Field(default=0.03, ge=0, le=0.15)
    om_pct_of_capex_per_yr: float = Field(default=1.0, ge=0, le=5)
    incentive_pct: float = Field(
        default=0.0, ge=0, le=110,
        description="Up-front grant or tax-credit equivalent expressed as % of CAPEX.",
    )
    incentive_spread_years: int = Field(default=10, ge=1, le=30)
    degradation_pct_yr: float = Field(default=0.5, ge=0, le=3)
    inverter_replacement_year: int = Field(default=12, ge=0, le=40)
    inverter_replacement_cost_pct: float = Field(default=0.15, ge=0, le=1.0)


class KPIResponse(BaseModel):
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    payback_discounted_yr: float
    lcoe_eur_kWh: float
    score: float
    cash_flows: list[float]
    cumulative_cash_flows: list[float]


class IncentiveResidentialRequest(BaseModel):
    capex_eur: float = Field(..., gt=0)
    detrazione_irpef_pct: float = Field(default=50.0, ge=0, le=110)
    spread_years: int = Field(default=10, ge=1, le=20)


class IncentiveResidentialResponse(BaseModel):
    detrazione_irpef_pct: float
    spread_years: int
    annual_benefit: float
    total_benefit: float
    net_capex_eur: float


class IncentiveSchoolRequest(BaseModel):
    capex_eur: float = Field(..., gt=0)
    peak_power_kWp: float = Field(..., gt=0)
    is_public: bool = True
    has_pnrr: bool = True
    pnrr_grant_pct: float = Field(default=0.50, ge=0, le=1.0)
    cer_participation: bool = False
    cer_shared_fraction: float = Field(default=0.0, ge=0, le=1.0)
    cer_incentive_eur_kWh: float = Field(default=0.11, ge=0, le=0.50)
    annual_production_kWh: float = Field(default=0.0, ge=0)


class IncentiveSchoolResponse(BaseModel):
    capex_iva_corrected: float
    iva_saving: float
    pnrr_grant: float
    net_capex_after_grants: float
    cer_annual_incentive: float
    total_first_year_benefit: float
    notes: list[str]


class ScenarioComparisonRequest(BaseModel):
    """Lightweight wrapper that re-uses /simulate full inputs but turns on
    gas/HP scenarios. Pass any FullSimulationRequest body with `gas_system.has_gas=True`."""
    simulation_request: dict[str, Any] = Field(
        ..., description="Body of /simulate with gas_system.has_gas=true."
    )


class ScenarioOutcome(BaseModel):
    scenario_id: str
    label: str
    capex_eur: float
    annual_total_energy_cost_eur: float
    annual_saving_vs_baseline_eur: float
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    co2_annual_kg: float
    co2_saving_pct: float
    seasonal_cop: float
    hp_coverage_pct: float
    score: float


class ScenarioComparisonResponse(BaseModel):
    scenarios: dict[str, ScenarioOutcome]
    ranked: list[str]
    recommended: str
    rationale: str
    current_gas_price_eur_sm3: float
    break_even_gas_price_eur_sm3: float
    gas_price_sensitivity: list[dict]
