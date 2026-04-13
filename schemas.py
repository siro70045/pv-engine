"""
schemas.py

Pydantic v2 request and response models for the PV Engine REST API.

Design principles
-----------------
- Every field has a default → partial requests are valid (progressive frontend)
- Field descriptions appear verbatim in /docs (OpenAPI)
- Request models mirror main.py dataclasses 1-to-1 — no logic here
- Response models are flat and JSON-safe (no numpy, no dataclass objects)
- Optional[X] = None means "not provided / not applicable"

Validation rules encoded here (not in api.py):
- lat/lon ranges
- positive-only numeric fields
- enum literals for categorical fields
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, model_validator


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — location
# ══════════════════════════════════════════════════════════════════════════════

class LocationRequest(BaseModel):
    """GPS coordinates of the building site."""

    lat: float = Field(
        ...,
        ge=-90, le=90,
        description="Latitude [decimal degrees, WGS84]. Example: 44.601",
    )
    lon: float = Field(
        ...,
        ge=-180, le=180,
        description="Longitude [decimal degrees, WGS84]. Example: 10.636",
    )
    altitude_m: float = Field(
        default=0.0, ge=0,
        description="Altitude above sea level [m]. If 0, PVGIS auto-detects.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — building
# ══════════════════════════════════════════════════════════════════════════════

class BuildingRequest(BaseModel):
    """Building geometry and occupancy parameters."""

    building_type: Literal[
        "residential", "warehouse", "office", "retail", "industrial", "other"
    ] = Field(
        default="residential",
        description="Building category — affects load profile shape.",
    )
    floor_area_m2: float = Field(
        default=200.0, gt=0,
        description="Total conditioned floor area [m²].",
    )
    avg_floor_height_m: float = Field(
        default=3.0, gt=0,
        description="Average internal floor-to-ceiling height [m].",
    )
    n_occupants: int = Field(
        default=4, ge=0,
        description="Typical number of occupants present during occupied hours.",
    )
    annual_demand_kWh: float = Field(
        default=0.0, ge=0,
        description=(
            "Annual electricity consumption from utility bill [kWh/yr]. "
            "If > 0, the bottom-up load profile is scaled to match this total. "
            "Required for accurate self-consumption calculations."
        ),
    )
    monthly_demand_kWh: Optional[list[float]] = Field(
        default=None,
        description=(
            "Monthly electricity consumption — 12 values [kWh/month], "
            "ordered January to December. "
            "If provided, scaling is applied month-by-month for a more accurate "
            "self-consumption profile. If null, uniform scaling is used."
        ),
    )
    occupancy_days_per_week: float = Field(
        default=7.0, ge=1.0, le=7.0,
        description=(
            "Average number of days per week the building is occupied. "
            "7 = full-time residence/office. "
            "3.5 = part-time (e.g. holiday flat used half the week)."
        ),
    )
    occupancy_hour_start: int = Field(
        default=0, ge=0, le=23,
        description=(
            "Hour of day when occupancy begins (0–23). "
            "0 = all-day occupancy. "
            "13 = afternoons/evenings only (absent during working hours). "
            "Demand before this hour is reduced to 10% (standby only)."
        ),
    )

    @model_validator(mode="after")
    def check_monthly_demand(self) -> "BuildingRequest":
        if self.monthly_demand_kWh is not None:
            if len(self.monthly_demand_kWh) != 12:
                raise ValueError(
                    "monthly_demand_kWh must have exactly 12 values (one per month)."
                )
            if any(v < 0 for v in self.monthly_demand_kWh):
                raise ValueError("monthly_demand_kWh values must be >= 0.")
        return self


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — PV system
# ══════════════════════════════════════════════════════════════════════════════

class PVSystemRequest(BaseModel):
    """Photovoltaic system specification."""

    peak_power_kWp: float = Field(
        ..., gt=0,
        description=(
            "DC peak power at STC (1 000 W/m², 25°C) [kWp]. "
            "Residential rooftop: 1.5–6 kWp. "
            "Industrial warehouse: 50–500 kWp."
        ),
    )
    tilt_deg: float = Field(
        default=20.0, ge=0, le=90,
        description=(
            "Panel tilt angle from horizontal [degrees]. "
            "0 = flat (horizontal). "
            "Optimal for Italy: 25–35°. "
            "Flat industrial roof: 10–15°."
        ),
    )
    azimuth_deg: float = Field(
        default=180.0, ge=0, le=360,
        description=(
            "Panel azimuth from North, clockwise [degrees]. "
            "0/360 = North, 90 = East, 180 = South (optimal), 270 = West."
        ),
    )
    mounting: Literal[
        "roof_integrated", "flat_roof_ballast", "ground_fixed", "bipv"
    ] = Field(
        default="roof_integrated",
        description=(
            "Mounting type — affects Faiman cell temperature model coefficients. "
            "roof_integrated: panels flush on pitched roof (hotter). "
            "flat_roof_ballast: raised on flat roof (better ventilation). "
            "ground_fixed: ground-mounted rack. "
            "bipv: building-integrated PV (facade/roof-as-panel)."
        ),
    )
    degradation_rate_pct_per_yr: float = Field(
        default=0.5, ge=0, le=3,
        description=(
            "Annual output degradation [%/yr]. "
            "Monocrystalline PERC premium: 0.4–0.5%/yr. "
            "Standard poly: 0.6–0.7%/yr."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — battery
# ══════════════════════════════════════════════════════════════════════════════

class BatteryRequest(BaseModel):
    """Battery storage system. Set capacity_kWh=0 to disable."""

    capacity_kWh: float = Field(
        default=0.0, ge=0,
        description="Usable storage capacity [kWh]. 0 = no battery.",
    )
    chemistry: Literal["LFP", "NMC"] = Field(
        default="LFP",
        description="Cell chemistry. LFP: safer, longer life. NMC: higher density.",
    )
    strategy: Literal["self_consumption", "peak_shaving"] = Field(
        default="self_consumption",
        description=(
            "Dispatch strategy. "
            "self_consumption: charge from PV surplus, discharge when demand > production. "
            "peak_shaving: discharge to reduce monthly peak demand [relevant for industrial tariffs]."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — tariff
# ══════════════════════════════════════════════════════════════════════════════

class TariffRequest(BaseModel):
    """Electricity tariff and grid connection parameters."""

    import_price_eur_kWh: float = Field(
        default=0.25, gt=0,
        description=(
            "All-in electricity purchase price [€/kWh]. "
            "Calculate from bill: total_bill_eur / kWh_consumed. "
            "Italian residential 2024: ~0.20–0.25 €/kWh. "
            "Italian industrial BT 2024: ~0.18–0.22 €/kWh."
        ),
    )
    export_price_eur_kWh: float = Field(
        default=0.08, ge=0,
        description=(
            "Net revenue per kWh exported to grid [€/kWh]. "
            "SSP (Scambio Sul Posto) indicative value: ~0.08–0.10 €/kWh. "
            "Ritiro Dedicato varies by month and market zone."
        ),
    )
    export_limit_pct: float = Field(
        default=100.0, ge=0, le=100,
        description=(
            "Maximum export as % of peak PV power [0–100]. "
            "Some grid operators limit export to 70% of kWp. "
            "100 = no limit."
        ),
    )
    demand_charge_eur_kW_month: float = Field(
        default=0.0, ge=0,
        description=(
            "Monthly demand charge [€/kW of peak monthly demand]. "
            "0 = flat tariff (most residential). "
            "Industrial MT tariffs in Italy: 5–15 €/kW/month."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — investment
# ══════════════════════════════════════════════════════════════════════════════

class InvestmentRequest(BaseModel):
    """Capital cost structure."""

    pv_cost_eur_kWp: float = Field(
        default=1000.0, gt=0,
        description=(
            "Installed PV cost [€/kWp], all-in: panels, inverter, "
            "mounting, installation, commissioning. "
            "Italian residential 2024: 1 400–2 000 €/kWp. "
            "Italian industrial 2024: 700–1 100 €/kWp."
        ),
    )
    battery_cost_eur_kWh: float = Field(
        default=600.0, ge=0,
        description="Battery installed cost [€/kWh usable]. Typical LFP 2024: 400–700 €/kWh.",
    )
    design_and_permits_eur: float = Field(
        default=1500.0, ge=0,
        description="Design, permits, grid connection fixed cost [€].",
    )
    other_capex_eur: float = Field(
        default=0.0, ge=0,
        description="Additional CAPEX items (structural reinforcement, EV chargers, etc.) [€].",
    )
    financing_equity_pct: float = Field(
        default=100.0, ge=0, le=100,
        description=(
            "Equity share of total investment [%]. "
            "100 = fully self-funded. "
            "30 = 30% equity + 70% bank loan."
        ),
    )
    loan_rate_pct: float = Field(
        default=4.5, ge=0, le=30,
        description="Annual interest rate on the financed portion [%].",
    )
    loan_duration_yr: int = Field(
        default=10, ge=1, le=30,
        description="Loan repayment period [years].",
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — financial parameters
# ══════════════════════════════════════════════════════════════════════════════

class FinancialRequest(BaseModel):
    """Economic model parameters."""

    discount_rate_pct: float = Field(
        default=5.0, ge=0, le=30,
        description=(
            "Discount rate (WACC) [%]. Used to compute NPV. "
            "Conservative: 5–6%. Opportunity cost: 4–5%."
        ),
    )
    energy_price_escalation_pct: float = Field(
        default=3.0, ge=0, le=15,
        description=(
            "Expected annual electricity price increase [%/yr]. "
            "Historical Italy 2010–2023: ~3–4%/yr. Conservative estimate: 2–3%."
        ),
    )
    analysis_years: int = Field(
        default=25, ge=5, le=40,
        description="Economic analysis horizon [years]. Typical PV lifetime: 25–30 years.",
    )
    om_pct_of_capex_per_yr: float = Field(
        default=1.0, ge=0, le=5,
        description=(
            "Annual O&M cost as % of total CAPEX. "
            "Includes maintenance, insurance, monitoring. "
            "Typical: 0.8–1.5% for PV without battery; 1.2–2.0% with battery."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — Italian incentives
# ══════════════════════════════════════════════════════════════════════════════

class IncentivesRequest(BaseModel):
    """Italian fiscal incentives applicable to the investment."""

    detrazione_irpef_pct: float = Field(
        default=50.0, ge=0, le=110,
        description=(
            "IRPEF/IRES deduction on PV investment [%], spread over 10 years. "
            "Private individuals on primary residence: 50%. "
            "0 = no deduction (companies on accelerated depreciation, etc.)."
        ),
    )
    ammortamento_accelerato: bool = Field(
        default=False,
        description=(
            "If True, apply accelerated depreciation (ammortamento accelerato) "
            "tax shield for companies. "
            "Applies to IRES-paying entities (SRL, SPA). "
            "Incompatible with IRPEF deduction — use one or the other."
        ),
    )
    aliquota_ires_pct: float = Field(
        default=24.0, ge=0, le=50,
        description="Corporate tax rate (IRES) [%]. Standard Italian rate: 24%.",
    )
    transizione_5_0_pct: float = Field(
        default=0.0, ge=0, le=40,
        description=(
            "Transizione 5.0 tax credit [%] (DM 2024). "
            "Available for qualifying efficiency investments. "
            "Typical range: 10–15% for standard cases."
        ),
    )
    cer_contribution_eur_kWh: float = Field(
        default=0.0, ge=0,
        description=(
            "CER (Comunità Energetica Rinnovabile) incentive [€/kWh shared]. "
            "GSE pays ~0.11 €/kWh on virtually shared energy (DM 07/12/2023). "
            "0 = not part of a CER. "
            "Maps to cer_incentivo_eur_kWh in the engine."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — gas system
# ══════════════════════════════════════════════════════════════════════════════

class GasSystemRequest(BaseModel):
    """Existing gas (or other fuel) heating system. Set has_gas=False to skip HP analysis."""

    has_gas: bool = Field(
        default=False,
        description=(
            "True if the building has a gas (or other fuel) heating system. "
            "Enables the full HP scenario comparison: Solo FV, Solo HP, FV+HP, FV+HP bivalente."
        ),
    )
    fuel_type: Literal["natural_gas", "diesel", "lpg"] = Field(
        default="natural_gas",
        description="Fuel type of the existing heating system.",
    )
    annual_consumption_units: float = Field(
        default=0.0, ge=0,
        description=(
            "Annual fuel consumption in native units: "
            "Sm³/yr for natural gas, litres/yr for diesel or LPG."
        ),
    )
    gas_price_eur_sm3: float = Field(
        default=0.0, ge=0,
        description=(
            "Actual gas price from utility bill [€/Sm³ for gas, €/litre for diesel/LPG]. "
            "CRITICAL: always set this from the real bill. "
            "If 0, the engine uses the reference price (0.95 €/Sm³) which may be wrong "
            "and produce incorrect HP break-even results."
        ),
    )
    boiler_efficiency: float = Field(
        default=0.88, gt=0, le=1,
        description=(
            "Seasonal boiler efficiency [0–1]. "
            "Condensing gas boiler: 0.92–0.98. "
            "Non-condensing gas: 0.82–0.88. "
            "Old cast-iron: 0.72–0.80."
        ),
    )
    terminal_type: Literal[
        "fan_coil", "radiant_floor", "radiant_ceiling",
        "low_temp_panel", "high_temp_panel", "old_radiator", "none"
    ] = Field(
        default="fan_coil",
        description=(
            "Heat distribution terminal type — critical for HP COP calculation. "
            "fan_coil: 40–45°C supply, ideal for HP (SCOP ~4.5–5.0). "
            "radiant_floor: 35–40°C supply, best for HP (SCOP ~5.0–5.5). "
            "old_radiator: 70–80°C supply, poor for HP (SCOP ~2.0–2.2) — "
            "terminals should be replaced before installing HP."
        ),
    )
    dhw_fraction: float = Field(
        default=0.10, ge=0, le=1,
        description=(
            "Fraction of total gas used for domestic hot water [0–1]. "
            "Residential: 0.15–0.30. Office/commercial: 0.05–0.15."
        ),
    )
    annual_heating_kWh: float = Field(
        default=0.0, ge=0,
        description=(
            "Known space heating demand [kWh_th/yr]. "
            "If 0, derived from annual_consumption_units and dhw_fraction."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST — heat pump
# ══════════════════════════════════════════════════════════════════════════════

class HeatPumpRequest(BaseModel):
    """Heat pump system to evaluate as boiler replacement. Used only if has_gas=True."""

    terminal_type: Literal[
        "fan_coil", "radiant_floor", "radiant_ceiling",
        "low_temp_panel", "high_temp_panel", "old_radiator", "none"
    ] = Field(
        default="fan_coil",
        description=(
            "Target terminal type after HP installation. "
            "If replacing old radiators, account for terminal_replacement_cost_eur."
        ),
    )
    carnot_efficiency_factor: float = Field(
        default=0.42, gt=0, le=0.65,
        description=(
            "Carnot correction factor η for COP model: COP = η × T_hot/(T_hot−T_ext). "
            "A++ air-to-water: 0.40–0.44. A+++ class: 0.45–0.52."
        ),
    )
    installed_cost_eur_kW: float = Field(
        default=380.0, gt=0,
        description=(
            "Total installed HP cost per kW of heating capacity [€/kW]. "
            "Air-to-water, Italy 2024: 250–500 €/kW depending on size. "
            "Smaller units (<20 kW): 400–500 €/kW. "
            "Larger units (>50 kW): 250–350 €/kW."
        ),
    )
    terminal_replacement_cost_eur: float = Field(
        default=0.0, ge=0,
        description=(
            "Cost to replace existing high-temperature terminals (old radiators) "
            "with low-temperature ones (fan coils) [€]. "
            "Typical Italian apartment 100 m²: 5 000–12 000 €."
        ),
    )
    use_bivalent: bool = Field(
        default=True,
        description=(
            "If True, keep the existing boiler as backup for extreme cold (bivalent system). "
            "Recommended: reduces HP size by 30–50% and improves economics. "
            "If False, HP is sized for 100% of peak demand (monovalent)."
        ),
    )
    bivalence_temp_c: float = Field(
        default=2.0, ge=-10, le=10,
        description=(
            "Outdoor temperature below which the backup boiler activates [°C]. "
            "Typical optimum: -2°C to +5°C. "
            "At +2°C: HP covers ~80% of annual heating energy."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN REQUEST
# ══════════════════════════════════════════════════════════════════════════════

class SimulationRequest(BaseModel):
    """
    Complete input for a PV feasibility simulation.

    Minimal valid request (only required fields):
        { "location": {"lat": 44.6, "lon": 10.6},
          "pv_system": {"peak_power_kWp": 100},
          "investment": {"pv_cost_eur_kWp": 900} }

    Everything else defaults to sensible values for an Italian industrial building.
    """

    # ── Required ────────────────────────────────────────────────────────────
    location: LocationRequest = Field(description="GPS coordinates of the site.")
    pv_system: PVSystemRequest = Field(description="PV system specification.")
    investment: InvestmentRequest = Field(description="Capital cost structure.")

    # ── Optional with defaults ───────────────────────────────────────────────
    building:   BuildingRequest   = Field(default_factory=BuildingRequest)
    battery:    BatteryRequest    = Field(default_factory=BatteryRequest)
    tariff:     TariffRequest     = Field(default_factory=TariffRequest)
    financial:  FinancialRequest  = Field(default_factory=FinancialRequest)
    incentives: IncentivesRequest = Field(default_factory=IncentivesRequest)
    gas_system: GasSystemRequest  = Field(default_factory=GasSystemRequest)
    heat_pump:  HeatPumpRequest   = Field(default_factory=HeatPumpRequest)

    # ── TMY source override ──────────────────────────────────────────────────
    tmy_csv_id: Optional[str] = Field(
        default=None,
        description=(
            "ID of a previously uploaded PVGIS TMY CSV file (from POST /tmy/upload). "
            "If provided, the engine uses this file instead of calling PVGIS API. "
            "Useful for offline use or when the PVGIS API is slow."
        ),
    )

    model_config = {"json_schema_extra": {"example": {
        "location": {"lat": 44.601, "lon": 10.636},
        "pv_system": {"peak_power_kWp": 100.0, "tilt_deg": 15, "azimuth_deg": 180},
        "investment": {"pv_cost_eur_kWp": 900.0, "design_and_permits_eur": 3000},
        "building": {"building_type": "warehouse", "floor_area_m2": 2000,
                     "annual_demand_kWh": 180000},
        "tariff": {"import_price_eur_kWh": 0.20, "export_price_eur_kWh": 0.07},
        "gas_system": {"has_gas": True, "annual_consumption_units": 15000,
                       "gas_price_eur_sm3": 0.95, "terminal_type": "fan_coil"},
    }}}


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE — KPI summary (flat, always present)
# ══════════════════════════════════════════════════════════════════════════════

class PVProductionResponse(BaseModel):
    annual_kWh: float
    specific_yield_kWh_kWp: float
    performance_ratio_pct: float
    monthly_kWh: list[float]


class EnergyBalanceResponse(BaseModel):
    self_consumption_rate_pct: float
    self_sufficiency_rate_pct: float
    grid_import_kWh: float
    grid_export_kWh: float
    curtailed_kWh: float
    annual_saving_eur: float


class EconomicsResponse(BaseModel):
    total_capex_eur: float
    net_capex_after_incentives_eur: float
    incentive_value_eur: float
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    payback_discounted_yr: float
    lcoe_eur_kWh: float
    score: float
    recommendation: str
    annual_cash_flows_eur: list[float]


class ClimateResponse(BaseModel):
    latitude: float
    longitude: float
    elevation_m: float
    dataset: str
    ghi_annual_kWh_m2: float
    t_avg_annual_c: float
    monthly_ghi_kWh_m2: list[float]
    monthly_t_avg_c: list[float]


class ScenarioResponse(BaseModel):
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
    recommended_scenario_id: str
    rationale: str
    current_gas_price_eur_sm3: float
    breakeven_gas_price_eur_sm3: float
    scenarios: dict[str, ScenarioResponse]
    gas_price_sensitivity: list[dict[str, Any]]


class SimulationResponse(BaseModel):
    """
    Complete simulation result.

    Always present: climate, pv_production, energy_balance, economics.
    Present only if gas_system.has_gas=True: scenario_comparison.
    """

    # ── Metadata ─────────────────────────────────────────────────────────────
    status: Literal["ok", "error"] = "ok"
    engine_version: str = "1.0.0"

    # ── Core results ─────────────────────────────────────────────────────────
    climate:        ClimateResponse
    pv_production:  PVProductionResponse
    energy_balance: EnergyBalanceResponse
    economics:      EconomicsResponse

    # ── Optional: gas / HP scenario comparison ────────────────────────────────
    scenario_comparison: Optional[ScenarioComparisonResponse] = None

    # ── Demand info ───────────────────────────────────────────────────────────
    annual_demand_kWh: float
    specific_demand_kWh_m2: float


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE — error
# ══════════════════════════════════════════════════════════════════════════════

class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    code: str
    message: str
    details: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE — health
# ══════════════════════════════════════════════════════════════════════════════

class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    pvlib_available: bool
    pvgis_reachable: bool


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE — TMY upload
# ══════════════════════════════════════════════════════════════════════════════

class TMYUploadResponse(BaseModel):
    tmy_csv_id: str = Field(description="Opaque ID to pass in SimulationRequest.tmy_csv_id.")
    latitude: float
    longitude: float
    elevation_m: float
    ghi_annual_kWh_m2: float
    records: int = Field(description="Number of hourly records parsed (should be 8760).")


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE — defaults
# ══════════════════════════════════════════════════════════════════════════════

class DefaultsResponse(BaseModel):
    """Pre-filled default values for the frontend form."""

    electricity_price_eur_kWh: float
    gas_price_eur_sm3: float
    pv_cost_residential_eur_kWp: float
    pv_cost_industrial_eur_kWp: float
    discount_rate_pct: float
    energy_escalation_pct: float
    irpef_deduction_pct: float
    ires_rate_pct: float
    grid_co2_kg_per_kWh: float
    analysis_years: int
