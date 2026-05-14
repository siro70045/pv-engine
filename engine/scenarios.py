"""
engine/scenarios.py

Multi-scenario techno-economic comparison engine.

Evaluates and compares up to five energy scenarios for a building:

    Baseline    : existing situation (gas boiler + grid electricity, no PV)
    Solo-FV     : PV installed, boiler unchanged
    Solo-HP     : heat pump replaces boiler, no PV
    FV+HP       : PV + heat pump together (co-optimised)
    FV+HP+biv   : PV + heat pump + bivalent gas backup

For each scenario the module:
    1. Builds the combined electrical demand profile (existing loads + HP if present)
    2. Runs the full energy balance (simulate_lifetime)
    3. Runs the full economics (calc_economics)
    4. Computes scenario-specific metrics: CO₂ savings, gas break-even price,
       incremental NPV vs baseline, combined payback

The comparison output contains all scenarios side-by-side and a ranked
recommendation with rationale.

Design principle: each scenario is computed independently using the same
underlying modules (energy_balance, economics). No approximations are
introduced at the scenarios level — all accuracy comes from the lower modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine.energy_vectors import (
    BuildingEnergyProfile, HeatPumpSystem, GasBoilerSystem,
    BivalentConfig, DEFAULT_PRICES, FuelType,
)
from engine.heat_pump import (
    simulate_heat_pump_year, calc_h_tr_from_annual,
    calc_hourly_thermal_demand, suggest_hp_sizing,
    HeatPumpYearResult,
)
from engine.energy_balance import (
    BatterySystem, GridTariff, EnergyBalanceInput,
    EnergyBalanceResult, simulate_lifetime,
    find_battery_replacement_years,
)
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters,
    ItalianIncentives, EconomicsResult, calc_economics,
)
from engine.solar import BuildingFootprint


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO IDENTIFIERS
# ══════════════════════════════════════════════════════════════════════════════

SCENARIO_BASELINE  = "baseline"
SCENARIO_SOLO_PV   = "solo_pv"
SCENARIO_SOLO_HP   = "solo_hp"
SCENARIO_PV_HP     = "pv_hp"
SCENARIO_PV_HP_BIV = "pv_hp_bivalente"


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO INPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScenarioConfig:
    """
    Complete input for a multi-scenario comparison.

    Minimal required fields:
        building_profile, pv_production_hourly_Wh, base_demand_hourly_Wh

    Everything else has sensible defaults.

    PV sizing — single vs multi-footprint
    -------------------------------------
    The default (backward compatible) single-footprint mode uses
    `pv_peak_kWp` as the installed kWp; the caller supplies
    `pv_production_hourly_Wh` from `solar.simulate_pv()`.

    For multi-building installations (e.g. school complex with several
    roofs / orientations), the caller can additionally set
    `pv_footprints = [BuildingFootprint(...), ...]` and supply an
    aggregated `pv_production_hourly_Wh` from `solar.simulate_pv_multi()`.
    When `pv_footprints` is provided, `total_pv_kWp` is derived from
    the sum of footprint capacities and `pv_peak_kWp` is ignored
    (a warning is emitted from `compare_scenarios` if both are set).
    """
    # ── Energy profiles ────────────────────────────────────────────────────
    building_profile: BuildingEnergyProfile
    pv_production_hourly_Wh: list[float]       # from solar.simulate_pv()
    base_demand_hourly_Wh: list[float]         # electrical demand WITHOUT HP
    t_ext_hourly: list[float]                  # from TMY

    # ── Thermal demand for HP sizing ──────────────────────────────────────
    annual_heating_kWh: float = 0.0
    # Space heating demand only [kWh/yr].
    # If 0 and building_profile.has_gas, estimated from gas consumption.

    # ── PV and battery ────────────────────────────────────────────────────
    pv_peak_kWp: float = 0.0
    pv_footprints: Optional[list[BuildingFootprint]] = None
    # Optional multi-footprint metadata. When non-empty, `total_pv_kWp`
    # returns the sum of footprint capacities (for CAPEX and energy
    # balance sizing) and `pv_peak_kWp` is ignored. Pass-through only —
    # the caller is responsible for computing the PV production curve
    # via `solar.simulate_pv_multi()`.
    battery: BatterySystem = field(default_factory=lambda: BatterySystem(capacity_kWh=0))
    tariff: GridTariff = field(default_factory=GridTariff)
    pv_degradation_pct_per_yr: float = 0.50

    # ── Heat pump ─────────────────────────────────────────────────────────
    heat_pump: HeatPumpSystem = field(default_factory=HeatPumpSystem)
    bivalent: Optional[BivalentConfig] = None

    # ── Economics ─────────────────────────────────────────────────────────
    investment_pv: InvestmentCost = field(default_factory=InvestmentCost)
    opex_pv: OperatingCost = field(default_factory=OperatingCost)
    financial: FinancialParameters = field(default_factory=FinancialParameters)
    incentives_pv: ItalianIncentives = field(default_factory=ItalianIncentives)
    incentives_hp: ItalianIncentives = field(default_factory=ItalianIncentives)

    # ── Which scenarios to run ─────────────────────────────────────────────
    run_solo_pv: bool = True
    run_solo_hp: bool = True
    run_pv_hp: bool = True
    run_pv_hp_bivalente: bool = True

    # ── Sensitivity on gas price ──────────────────────────────────────────
    gas_price_sensitivity_range: list[float] = field(
        default_factory=lambda: [0.50, 0.80, 0.95, 1.20, 1.50, 2.00]
    )
    # Multipliers on current gas price. E.g. 2.0 = price doubles.

    def effective_heating_kWh(self) -> float:
        """Space heating demand [kWh/yr], derived from gas if not explicitly set."""
        if self.annual_heating_kWh > 0:
            return self.annual_heating_kWh
        if self.building_profile.has_gas:
            # Subtract DHW fraction from total thermal output
            dhw_frac = self.building_profile.gas_system.dhw_fraction
            return self.building_profile.annual_gas_thermal_kWh * (1.0 - dhw_frac)
        return 0.0

    @property
    def total_pv_kWp(self) -> float:
        """
        Installed PV capacity [kWp] used for CAPEX and energy-balance sizing.
        Sum of footprint capacities when `pv_footprints` is provided,
        otherwise the legacy `pv_peak_kWp`.
        """
        if self.pv_footprints:
            return sum(fp.peak_power_kWp for fp in self.pv_footprints)
        return self.pv_peak_kWp


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO RESULT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SingleScenarioResult:
    """Complete result for one scenario."""
    scenario_id: str
    scenario_label: str

    # ── Energy ──────────────────────────────────────────────────────────────
    total_electricity_demand_kWh: float   # base + HP electricity
    hp_electricity_kWh: float            # 0 if no HP
    pv_production_kWh: float             # 0 if no PV
    self_consumption_rate: float
    self_sufficiency_rate: float
    grid_import_kWh: float
    grid_export_kWh: float
    gas_consumption_units: float         # Sm³/yr remaining (0 if no gas)
    gas_thermal_kWh: float               # remaining gas thermal output
    annual_heating_covered_by_hp_pct: float  # 0 if no HP

    # ── Financial ───────────────────────────────────────────────────────────
    capex_eur: float                     # total investment this scenario
    pv_capex_eur: float
    hp_capex_eur: float
    annual_electricity_cost_eur: float
    annual_gas_cost_eur: float
    annual_om_cost_eur: float
    annual_total_energy_cost_eur: float
    annual_saving_vs_baseline_eur: float
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    payback_discounted_yr: float
    lcoe_eur_kwh: float
    score: float
    recommendation: str

    # ── CO₂ ─────────────────────────────────────────────────────────────────
    annual_co2_kg: float
    co2_saving_vs_baseline_kg: float
    co2_saving_pct: float

    # ── HP performance (None if no HP) ────────────────────────────────────
    hp_result: Optional[HeatPumpYearResult]
    seasonal_cop: float
    hp_coverage_fraction: float

    # ── Full economics result ─────────────────────────────────────────────
    economics: Optional[EconomicsResult]

    # ── Energy balance year 1 ─────────────────────────────────────────────
    energy_balance_y1: Optional[EnergyBalanceResult]


@dataclass
class GasPriceSensitivity:
    """Break-even and NPV at different gas price scenarios."""
    gas_price_multiplier: float
    gas_price_eur_sm3: float
    annual_gas_cost_eur: float
    annual_heating_cost_hp_eur: float    # HP electricity cost
    hp_cheaper: bool                     # True if HP is cheaper at this gas price
    npv_pv_hp_eur: float                 # NPV of PV+HP scenario


@dataclass
class ScenarioComparison:
    """
    Full multi-scenario comparison result.
    Contains all scenarios and the comparative analysis.
    """
    scenarios: dict[str, SingleScenarioResult]    # keyed by scenario_id
    baseline: SingleScenarioResult
    ranked_scenarios: list[SingleScenarioResult]  # by NPV descending

    # ── Optimal scenario recommendation ──────────────────────────────────
    recommended_scenario_id: str
    recommendation_rationale: str

    # ── Gas price analysis (only if has_gas) ─────────────────────────────
    gas_breakeven_price_eur_sm3: float    # gas price above which HP beats boiler
    gas_price_sensitivity: list[GasPriceSensitivity]
    current_gas_price_eur_sm3: float

    # ── HP sizing (if HP scenarios run) ──────────────────────────────────
    hp_sizing: dict

    # ── Summary table ─────────────────────────────────────────────────────
    def print_comparison(self) -> None:
        """Print a formatted comparison table to stdout."""
        SEP = "─" * 90
        LABELS = {
            SCENARIO_BASELINE:  "Baseline (attuale)",
            SCENARIO_SOLO_PV:   "Solo FV",
            SCENARIO_SOLO_HP:   "Solo pompa di calore",
            SCENARIO_PV_HP:     "FV + pompa di calore",
            SCENARIO_PV_HP_BIV: "FV + HP bivalente",
        }
        ORDER = [SCENARIO_BASELINE, SCENARIO_SOLO_PV, SCENARIO_SOLO_HP,
                 SCENARIO_PV_HP, SCENARIO_PV_HP_BIV]

        print(f"\n{'═'*90}")
        print("  CONFRONTO SCENARI ENERGETICI")
        print(f"{'═'*90}")
        print(f"\n  {'Indicatore':<32}", end="")
        for sid in ORDER:
            if sid in self.scenarios:
                lbl = LABELS.get(sid, sid)[:16]
                print(f" {lbl:>16}", end="")
        print()
        print(f"  {SEP}")

        def row(label: str, getter):
            print(f"  {label:<32}", end="")
            for sid in ORDER:
                if sid in self.scenarios:
                    val = getter(self.scenarios[sid])
                    print(f" {val:>16}", end="")
            print()

        row("CAPEX totale [€]",
            lambda s: f"{s.capex_eur:>10,.0f}")
        row("Risparmio annuo [€/yr]",
            lambda s: f"{s.annual_saving_vs_baseline_eur:>+10,.0f}")
        row("Costo energia annuo [€/yr]",
            lambda s: f"{s.annual_total_energy_cost_eur:>10,.0f}")
        row("VAN [€]",
            lambda s: f"{s.npv_eur:>10,.0f}")
        row("TIR [%]",
            lambda s: (f"{s.irr_pct:>9.1f}%" if s.irr_pct > -99 else "         n/a"))
        row("Payback semplice [anni]",
            lambda s: (f"{s.payback_simple_yr:>9.1f}" if s.payback_simple_yr < 99 else "        >25"))
        row("CO₂ annua [t CO₂/yr]",
            lambda s: f"{s.annual_co2_kg/1000:>10.1f}")
        row("Riduzione CO₂ [%]",
            lambda s: (f"{s.co2_saving_pct:>9.1f}%" if s.co2_saving_pct != 0 else "          —"))

        if any(s.seasonal_cop > 0 for s in self.scenarios.values()):
            row("SCOP pompa di calore",
                lambda s: (f"{s.seasonal_cop:>9.2f}" if s.seasonal_cop > 0 else "          —"))
            row("Copertura HP [%]",
                lambda s: (f"{s.hp_coverage_fraction*100:>9.1f}%" if s.hp_coverage_fraction > 0 else "          —"))

        print(f"\n  {'═'*90}")
        print(f"  RACCOMANDAZIONE: {LABELS.get(self.recommended_scenario_id, '?')}")
        print(f"  {self.recommendation_rationale}")

        if self.gas_breakeven_price_eur_sm3 > 0:
            print(f"\n  Break-even prezzo gas per HP: "
                  f"{self.gas_breakeven_price_eur_sm3:.2f} €/Sm³  "
                  f"(attuale: {self.current_gas_price_eur_sm3:.2f} €/Sm³)")
            if self.gas_breakeven_price_eur_sm3 <= self.current_gas_price_eur_sm3:
                print("  → La pompa di calore è già conveniente ai prezzi attuali del gas.")
            else:
                ratio = self.gas_breakeven_price_eur_sm3 / self.current_gas_price_eur_sm3
                print(f"  → Richiede un aumento del {(ratio-1)*100:.0f}% del prezzo del gas "
                      f"per diventare conveniente su sola operatività.")
        print(f"{'═'*90}\n")


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_investment_hp(
    hp: HeatPumpSystem,
    rated_kW: float,
    fin: FinancialParameters,
    incentives_hp: ItalianIncentives,
    hp_result: HeatPumpYearResult,
) -> tuple[InvestmentCost, OperatingCost]:
    """Build InvestmentCost and OperatingCost objects for the HP alone."""
    total_cost = hp.total_installed_cost_eur(rated_kW)
    inv = InvestmentCost(
        pv_cost_eur_kWp       = 0.0,
        peak_power_kWp        = 0.0,
        battery_cost_eur_kWh  = 0.0,
        battery_capacity_kWh  = 0.0,
        design_and_permits_eur= total_cost * 0.03,
        other_capex_eur       = hp.terminal_replacement_cost_eur,
    )
    inv.heat_pump_cost_eur = rated_kW * hp.installed_cost_eur_kW
    opex = OperatingCost(
        om_pct_of_capex_per_yr = 1.5,  # HP maintenance: ~1.5%/yr
        om_escalation_rate     = 0.02,
    )
    return inv, opex


def _combine_demands(
    base_demand_Wh: list[float],
    hp_hourly: Optional[list[float]],
) -> list[float]:
    """Add HP electricity demand to base electrical demand profile."""
    if hp_hourly is None:
        return base_demand_Wh
    return [b + h for b, h in zip(base_demand_Wh, hp_hourly)]


def _build_yearly_balance_with_hp(
    combined_demand_Wh: list[float],
    pv_production_Wh: list[float],
    battery: BatterySystem,
    tariff: GridTariff,
    peak_kWp: float,
    pv_deg_pct: float,
    lifetime_years: int,
) -> list[EnergyBalanceResult]:
    """Run simulate_lifetime with the combined demand."""
    inp = EnergyBalanceInput(
        pv_production_hourly_Wh = pv_production_Wh,
        demand_hourly_Wh        = combined_demand_Wh,
        battery                 = battery,
        tariff                  = tariff,
        peak_power_kWp          = peak_kWp,
    )
    return simulate_lifetime(inp, lifetime_years, pv_deg_pct)


# ══════════════════════════════════════════════════════════════════════════════
# BASELINE COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def _compute_baseline(cfg: ScenarioConfig) -> SingleScenarioResult:
    """
    Baseline: no PV, no HP. Building continues as-is.
    No investment, no VAN — only current costs and CO₂.
    """
    bp = cfg.building_profile
    el_cost   = bp.annual_electricity_cost_eur
    gas_cost  = bp.annual_gas_cost_eur
    total_cost= bp.annual_total_cost_eur
    co2_kg    = bp.annual_total_co2_kg

    return SingleScenarioResult(
        scenario_id       = SCENARIO_BASELINE,
        scenario_label    = "Baseline (situazione attuale)",
        total_electricity_demand_kWh  = bp.electricity_kwh_annual,
        hp_electricity_kWh            = 0.0,
        pv_production_kWh             = 0.0,
        self_consumption_rate         = 0.0,
        self_sufficiency_rate         = 0.0,
        grid_import_kWh               = bp.electricity_kwh_annual,
        grid_export_kWh               = 0.0,
        gas_consumption_units         = bp.gas_consumption_annual_units,
        gas_thermal_kWh               = bp.annual_gas_thermal_kWh,
        annual_heating_covered_by_hp_pct = 0.0,
        capex_eur                     = 0.0,
        pv_capex_eur                  = 0.0,
        hp_capex_eur                  = 0.0,
        annual_electricity_cost_eur   = el_cost,
        annual_gas_cost_eur           = gas_cost,
        annual_om_cost_eur            = 0.0,
        annual_total_energy_cost_eur  = total_cost,
        annual_saving_vs_baseline_eur = 0.0,
        npv_eur                       = 0.0,
        irr_pct                       = 0.0,
        payback_simple_yr             = float("inf"),
        payback_discounted_yr         = float("inf"),
        lcoe_eur_kwh                  = 0.0,
        score                         = 0.0,
        recommendation                = "Baseline — nessun intervento",
        annual_co2_kg                 = co2_kg,
        co2_saving_vs_baseline_kg     = 0.0,
        co2_saving_pct                = 0.0,
        hp_result                     = None,
        seasonal_cop                  = 0.0,
        hp_coverage_fraction          = 0.0,
        economics                     = None,
        energy_balance_y1             = None,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN COMPARISON FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def compare_scenarios(
    cfg: ScenarioConfig,
    lifetime_years: int = 25,
) -> ScenarioComparison:
    """
    Run all enabled scenarios and return a complete comparison.

    Parameters
    ----------
    cfg : ScenarioConfig
        Full scenario configuration.
    lifetime_years : int
        Simulation horizon for all scenarios.

    Returns
    -------
    ScenarioComparison
    """
    bp = cfg.building_profile
    scenarios: dict[str, SingleScenarioResult] = {}

    # Surface anomalous double-spec early — pv_footprints wins.
    if cfg.pv_footprints and cfg.pv_peak_kWp > 0:
        import warnings
        warnings.warn(
            "ScenarioConfig: pv_footprints provided — pv_peak_kWp ignored.",
            stacklevel=2,
        )

    # ── Baseline ──────────────────────────────────────────────────────────
    baseline = _compute_baseline(cfg)
    scenarios[SCENARIO_BASELINE] = baseline

    # ── HP thermal demand and sizing ──────────────────────────────────────
    heating_kWh = cfg.effective_heating_kWh()
    hp_result_year1: Optional[HeatPumpYearResult] = None
    hp_rated_kW = 0.0
    hp_sizing_info = {}

    if heating_kWh > 0 and (cfg.run_solo_hp or cfg.run_pv_hp or cfg.run_pv_hp_bivalente):
        # Hourly thermal demand
        h_tr = calc_h_tr_from_annual(heating_kWh, cfg.t_ext_hourly)
        thermal_demand_Wh = calc_hourly_thermal_demand(
            cfg.t_ext_hourly, h_tr,
            annual_heating_kWh=heating_kWh,
        )
        # Suggest sizing
        hp_sizing_info = suggest_hp_sizing(thermal_demand_Wh, cfg.t_ext_hourly, cfg.bivalent)
        hp_rated_kW = (cfg.heat_pump.rated_heating_power_kW
                       if cfg.heat_pump.rated_heating_power_kW > 0
                       else hp_sizing_info["bivalent_kW"])

        # Run HP simulation year 1
        hp_result_year1 = simulate_heat_pump_year(
            t_ext_hourly           = cfg.t_ext_hourly,
            thermal_demand_hourly_Wh = thermal_demand_Wh,
            hp                     = cfg.heat_pump,
            bivalent               = cfg.bivalent,
            electricity_price      = bp.electricity_price,
            simulation_year        = 1,
        )
    else:
        thermal_demand_Wh = [0.0] * 8760

    # ── Helper: run PV+demand scenario through economics ─────────────────
    def _run_pv_scenario(
        scenario_id: str,
        label: str,
        demand_Wh: list[float],
        inv: InvestmentCost,
        opex: OperatingCost,
        hp_r: Optional[HeatPumpYearResult],
        gas_remaining_units: float,
    ) -> SingleScenarioResult:

        # Battery replacement years
        bat_rep_years = []
        if cfg.battery.capacity_kWh > 0:
            bat_rep_years = find_battery_replacement_years(cfg.battery, lifetime_years)
            if bat_rep_years and not opex.battery_replacement_years:
                opex.battery_replacement_years = bat_rep_years

        # Auto-fill inverter cost if not set
        if opex.inverter_replacement_cost_eur == 0 and inv.peak_power_kWp > 0:
            opex.inverter_replacement_cost_eur = (
                inv.pv_cost_eur_kWp * inv.peak_power_kWp * 0.10
            )

        # Run lifetime energy balance
        eb_inp = EnergyBalanceInput(
            pv_production_hourly_Wh = cfg.pv_production_hourly_Wh,
            demand_hourly_Wh        = demand_Wh,
            battery                 = cfg.battery,
            tariff                  = cfg.tariff,
            peak_power_kWp          = cfg.total_pv_kWp,
        )
        yearly = simulate_lifetime(eb_inp, lifetime_years, cfg.pv_degradation_pct_per_yr)
        eb1 = yearly[0]

        # Economics — PV portion
        eco = calc_economics(yearly, inv, opex, cfg.financial,
                             cfg.incentives_pv)

        # Annual energy costs WITH this scenario
        el_grid_cost  = eb1.grid_import_kWh * cfg.tariff.import_price_eur_kWh
        gas_remaining_cost = (gas_remaining_units
                              * bp.gas_price.price_eur_per_unit)
        total_energy_cost = el_grid_cost + gas_remaining_cost + opex.om_year1(inv.total_capex_eur)
        annual_saving = baseline.annual_total_energy_cost_eur - total_energy_cost

        # CO₂ this scenario
        # el_co2: covers ALL electricity from grid (including HP electricity demand,
        #         since HP is inside demand_Wh → partially autoconsumbed, rest from grid)
        # gas_co2: gas_remaining_units = hp_r.backup_fuel_units (backup boiler gas)
        #          + any direct gas use not replaced by HP
        # No double-counting: backup gas is accounted for ONCE via gas_remaining_units
        el_co2    = eb1.grid_import_kWh * bp.co2_electricity_kg_per_kwh
        gas_co2   = gas_remaining_units * bp.gas_price.co2_factor_kg_per_unit
        total_co2 = el_co2 + gas_co2
        co2_saving = baseline.annual_co2_kg - total_co2
        co2_pct    = co2_saving / baseline.annual_co2_kg * 100 if baseline.annual_co2_kg > 0 else 0.0

        return SingleScenarioResult(
            scenario_id                   = scenario_id,
            scenario_label                = label,
            total_electricity_demand_kWh  = sum(demand_Wh) / 1000.0,
            hp_electricity_kWh            = hp_r.hp_electricity_kWh if hp_r else 0.0,
            pv_production_kWh             = eb1.pv_production_kWh,
            self_consumption_rate         = eb1.self_consumption_rate,
            self_sufficiency_rate         = eb1.self_sufficiency_rate,
            grid_import_kWh               = eb1.grid_import_kWh,
            grid_export_kWh               = eb1.grid_export_kWh,
            gas_consumption_units         = gas_remaining_units,
            gas_thermal_kWh               = gas_remaining_units * bp.gas_price.unit_kwh_conversion,
            annual_heating_covered_by_hp_pct = (hp_r.hp_coverage_fraction * 100 if hp_r else 0.0),
            capex_eur                     = inv.total_capex_eur,
            pv_capex_eur                  = inv.pv_capex_eur,
            hp_capex_eur                  = inv.heat_pump_cost_eur,
            annual_electricity_cost_eur   = el_grid_cost,
            annual_gas_cost_eur           = gas_remaining_cost,
            annual_om_cost_eur            = opex.om_year1(inv.total_capex_eur),
            annual_total_energy_cost_eur  = total_energy_cost,
            annual_saving_vs_baseline_eur = annual_saving,
            npv_eur                       = eco.npv_eur,
            irr_pct                       = eco.irr_pct,
            payback_simple_yr             = eco.payback_simple_yr,
            payback_discounted_yr         = eco.payback_discounted_yr,
            lcoe_eur_kwh                  = eco.lcoe_eur_kWh,
            score                         = eco.score,
            recommendation                = eco.recommendation,
            annual_co2_kg                 = total_co2,
            co2_saving_vs_baseline_kg     = co2_saving,
            co2_saving_pct                = co2_pct,
            hp_result                     = hp_r,
            seasonal_cop                  = hp_r.seasonal_cop if hp_r else 0.0,
            hp_coverage_fraction          = hp_r.hp_coverage_fraction if hp_r else 0.0,
            economics                     = eco,
            energy_balance_y1             = eb1,
        )

    # ── Solo FV ──────────────────────────────────────────────────────────
    if cfg.run_solo_pv and cfg.total_pv_kWp > 0:
        inv_pv = cfg.investment_pv
        inv_pv.peak_power_kWp = cfg.total_pv_kWp
        opex_pv = cfg.opex_pv
        scenarios[SCENARIO_SOLO_PV] = _run_pv_scenario(
            SCENARIO_SOLO_PV, "Solo FV",
            cfg.base_demand_hourly_Wh,
            inv_pv, opex_pv,
            hp_r=None,
            gas_remaining_units=bp.gas_consumption_annual_units,
        )

    # ── Solo HP (no PV) ───────────────────────────────────────────────────
    if cfg.run_solo_hp and hp_result_year1 and heating_kWh > 0:
        hp_capex = cfg.heat_pump.total_installed_cost_eur(hp_rated_kW)
        # HP-only: no PV production, just HP demand added to grid
        hp_demand_Wh = hp_result_year1.hp_electricity_profile_Wh
        combined_demand = _combine_demands(cfg.base_demand_hourly_Wh, hp_demand_Wh)

        # Use a dummy PV investment with 0 kWp — economics from HP investment only
        inv_hp_only = InvestmentCost(
            pv_cost_eur_kWp=0, peak_power_kWp=0,
            heat_pump_cost_eur=hp_capex,
            design_and_permits_eur=hp_capex * 0.03,
            other_capex_eur=cfg.heat_pump.terminal_replacement_cost_eur,
        )
        opex_hp = OperatingCost(om_pct_of_capex_per_yr=1.5, om_escalation_rate=0.02)

        # For HP-only, we build a pseudo-PV scenario with zero production
        zero_pv = [0.0] * 8760
        eb_inp_hp = EnergyBalanceInput(
            pv_production_hourly_Wh=zero_pv,
            demand_hourly_Wh=combined_demand,
            battery=BatterySystem(capacity_kWh=0),
            tariff=cfg.tariff,
            peak_power_kWp=0.0,
        )
        yearly_hp = simulate_lifetime(eb_inp_hp, lifetime_years, 0.0)

        # The "saving" for HP comes from avoided gas cost
        gas_after_hp = hp_result_year1.backup_fuel_units  # only backup gas remains
        el_extra_cost = (hp_result_year1.hp_electricity_kWh
                         * cfg.tariff.import_price_eur_kWh)
        gas_remaining_cost = gas_after_hp * bp.gas_price.price_eur_per_unit
        om_cost = opex_hp.om_year1(inv_hp_only.total_capex_eur)
        total_energy_cost_hp = el_extra_cost + gas_remaining_cost + om_cost
        annual_saving_hp = baseline.annual_total_energy_cost_eur - total_energy_cost_hp

        # Build cash flows for HP-only scenario correctly.
        # The "saving" is the avoided gas cost minus the incremental HP electricity cost.
        # We construct synthetic yearly EnergyBalanceResult-like objects that carry
        # the correct annual_saving_eur (gas_saved - hp_el_cost) so calc_economics
        # produces the right NPV.

        class _HPFakeEB:
            """Lightweight stub carrying the HP annual saving for economics module."""
            def __init__(self, yr: int, hp_res, bp_ref, tariff_ref, esc: float):
                esc_f = (1 + esc)**(yr - 1)
                hp_el_cost = hp_res.hp_electricity_kWh * tariff_ref.import_price_eur_kWh * esc_f
                orig_gas_cost   = bp_ref.gas_consumption_annual_units * bp_ref.gas_price.price_eur_per_unit * esc_f
                backup_gas_cost = hp_res.backup_fuel_units * bp_ref.gas_price.price_eur_per_unit * esc_f
                gas_saved = orig_gas_cost - backup_gas_cost
                self.annual_saving_eur          = max(gas_saved - hp_el_cost, -99999)
                self.export_revenue_eur         = 0.0
                self.demand_charge_saving_eur   = 0.0
                self.net_annual_benefit_eur     = self.annual_saving_eur
                self.pv_production_kWh          = 0.0
                self.self_consumption_rate      = 0.0
                self.self_sufficiency_rate      = 0.0
                self.self_consumed_kWh          = 0.0
                self.grid_import_kWh            = (bp_ref.electricity_kwh_annual
                                                    + hp_res.hp_electricity_kWh)
                self.grid_export_kWh            = 0.0
                self.curtailed_kWh              = 0.0
                self.battery_needs_replacement  = False

        esc = cfg.financial.energy_price_escalation
        n_yrs = min(lifetime_years, cfg.financial.analysis_years)
        fake_yearly_hp = [
            _HPFakeEB(yr, hp_result_year1, bp, cfg.tariff, esc)
            for yr in range(1, n_yrs + 1)
        ]
        eco_hp = calc_economics(fake_yearly_hp, inv_hp_only, opex_hp, cfg.financial,
                                cfg.incentives_hp)

        # total = (all_el_from_grid × co2_factor) + backup_gas_co2
        total_grid_import = bp.electricity_kwh_annual + hp_result_year1.hp_electricity_kWh
        co2_hp = (total_grid_import * bp.co2_electricity_kg_per_kwh
                  + hp_result_year1.backup_fuel_units * bp.gas_price.co2_factor_kg_per_unit)
        co2_save = baseline.annual_co2_kg - co2_hp
        co2_pct  = co2_save / baseline.annual_co2_kg * 100 if baseline.annual_co2_kg > 0 else 0.0

        eb1_hp = yearly_hp[0]
        scenarios[SCENARIO_SOLO_HP] = SingleScenarioResult(
            scenario_id                   = SCENARIO_SOLO_HP,
            scenario_label                = "Solo pompa di calore",
            total_electricity_demand_kWh  = sum(combined_demand) / 1000.0,
            hp_electricity_kWh            = hp_result_year1.hp_electricity_kWh,
            pv_production_kWh             = 0.0,
            self_consumption_rate         = 0.0,
            self_sufficiency_rate         = 0.0,
            grid_import_kWh               = sum(combined_demand) / 1000.0,
            grid_export_kWh               = 0.0,
            gas_consumption_units         = gas_after_hp,
            gas_thermal_kWh               = gas_after_hp * bp.gas_price.unit_kwh_conversion,
            annual_heating_covered_by_hp_pct = hp_result_year1.hp_coverage_fraction * 100,
            capex_eur                     = inv_hp_only.total_capex_eur,
            pv_capex_eur                  = 0.0,
            hp_capex_eur                  = hp_capex,
            annual_electricity_cost_eur   = el_extra_cost,
            annual_gas_cost_eur           = gas_remaining_cost,
            annual_om_cost_eur            = om_cost,
            annual_total_energy_cost_eur  = total_energy_cost_hp,
            annual_saving_vs_baseline_eur = annual_saving_hp,
            npv_eur                       = eco_hp.npv_eur,
            irr_pct                       = eco_hp.irr_pct,
            payback_simple_yr             = eco_hp.payback_simple_yr,
            payback_discounted_yr         = eco_hp.payback_discounted_yr,
            lcoe_eur_kwh                  = eco_hp.lcoe_eur_kWh,
            score                         = eco_hp.score,
            recommendation                = eco_hp.recommendation,
            annual_co2_kg                 = co2_hp,
            co2_saving_vs_baseline_kg     = co2_save,
            co2_saving_pct                = co2_pct,
            hp_result                     = hp_result_year1,
            seasonal_cop                  = hp_result_year1.seasonal_cop,
            hp_coverage_fraction          = hp_result_year1.hp_coverage_fraction,
            economics                     = eco_hp,
            energy_balance_y1             = eb1_hp,
        )

    # ── FV + HP (monovalente) ─────────────────────────────────────────────
    if cfg.run_pv_hp and hp_result_year1 and cfg.total_pv_kWp > 0:
        hp_demand_Wh = hp_result_year1.hp_electricity_profile_Wh
        combined = _combine_demands(cfg.base_demand_hourly_Wh, hp_demand_Wh)
        hp_capex = cfg.heat_pump.total_installed_cost_eur(hp_rated_kW)

        inv_combined = InvestmentCost(
            pv_cost_eur_kWp       = cfg.investment_pv.pv_cost_eur_kWp,
            peak_power_kWp        = cfg.total_pv_kWp,
            battery_cost_eur_kWh  = cfg.investment_pv.battery_cost_eur_kWh,
            battery_capacity_kWh  = cfg.battery.capacity_kWh,
            design_and_permits_eur= cfg.investment_pv.design_and_permits_eur + hp_capex * 0.03,
            heat_pump_cost_eur    = hp_capex,
            other_capex_eur       = (cfg.investment_pv.other_capex_eur
                                     + cfg.heat_pump.terminal_replacement_cost_eur),
        )
        opex_combined = OperatingCost(
            om_pct_of_capex_per_yr = 1.0,
            om_escalation_rate     = 0.02,
            battery_replacement_years = (find_battery_replacement_years(
                cfg.battery, lifetime_years) if cfg.battery.capacity_kWh > 0 else []),
        )
        gas_after = hp_result_year1.backup_fuel_units

        scenarios[SCENARIO_PV_HP] = _run_pv_scenario(
            SCENARIO_PV_HP, "FV + pompa di calore",
            combined, inv_combined, opex_combined,
            hp_r=hp_result_year1,
            gas_remaining_units=gas_after,
        )

    # ── FV + HP bivalente ─────────────────────────────────────────────────
    if (cfg.run_pv_hp_bivalente and cfg.bivalent and cfg.total_pv_kWp > 0
            and heating_kWh > 0):
        h_tr = calc_h_tr_from_annual(heating_kWh, cfg.t_ext_hourly)
        thermal_demand_Wh_biv = calc_hourly_thermal_demand(
            cfg.t_ext_hourly, h_tr, annual_heating_kWh=heating_kWh,
        )
        hp_biv_result = simulate_heat_pump_year(
            cfg.t_ext_hourly, thermal_demand_Wh_biv,
            cfg.heat_pump, cfg.bivalent,
            electricity_price=bp.electricity_price,
        )
        hp_demand_biv = hp_biv_result.hp_electricity_profile_Wh
        combined_biv = _combine_demands(cfg.base_demand_hourly_Wh, hp_demand_biv)

        hp_capex_biv = cfg.heat_pump.total_installed_cost_eur(
            hp_sizing_info.get("bivalent_kW", hp_rated_kW)
        )
        inv_biv = InvestmentCost(
            pv_cost_eur_kWp       = cfg.investment_pv.pv_cost_eur_kWp,
            peak_power_kWp        = cfg.total_pv_kWp,
            battery_cost_eur_kWh  = cfg.investment_pv.battery_cost_eur_kWh,
            battery_capacity_kWh  = cfg.battery.capacity_kWh,
            design_and_permits_eur= cfg.investment_pv.design_and_permits_eur + hp_capex_biv * 0.03,
            heat_pump_cost_eur    = hp_capex_biv,
            other_capex_eur       = (cfg.investment_pv.other_capex_eur
                                     + cfg.heat_pump.terminal_replacement_cost_eur),
        )
        opex_biv = OperatingCost(
            om_pct_of_capex_per_yr=1.0, om_escalation_rate=0.02,
        )
        gas_after_biv = hp_biv_result.backup_fuel_units

        scenarios[SCENARIO_PV_HP_BIV] = _run_pv_scenario(
            SCENARIO_PV_HP_BIV, "FV + HP bivalente",
            combined_biv, inv_biv, opex_biv,
            hp_r=hp_biv_result,
            gas_remaining_units=gas_after_biv,
        )

    # ── Ranking ───────────────────────────────────────────────────────────
    non_baseline = [s for k, s in scenarios.items() if k != SCENARIO_BASELINE]
    ranked = sorted(non_baseline, key=lambda s: s.npv_eur, reverse=True)

    best = ranked[0] if ranked else baseline
    recommended_id = best.scenario_id

    # Build recommendation rationale
    rationale_parts = []
    if best.npv_eur > 0:
        rationale_parts.append(f"VAN €{best.npv_eur:,.0f}")
    if best.irr_pct > 0:
        rationale_parts.append(f"TIR {best.irr_pct:.1f}%")
    if best.payback_simple_yr < 25:
        rationale_parts.append(f"payback {best.payback_simple_yr:.1f} anni")
    if best.co2_saving_pct > 0:
        rationale_parts.append(f"CO₂ −{best.co2_saving_pct:.0f}%")
    rationale = ("Migliore scenario per VAN. " + " | ".join(rationale_parts)
                 if rationale_parts else "Nessuno scenario redditizio.")

    # ── Gas price sensitivity ─────────────────────────────────────────────
    gas_breakeven = 0.0
    sensitivity_list: list[GasPriceSensitivity] = []
    current_gas_price = (bp.gas_price.price_eur_per_unit
                         if bp.has_gas else 0.0)

    if bp.has_gas and hp_result_year1:
        # Break-even: HP electricity cost = equivalent gas cost
        gas_breakeven = bp.gas_price_breakeven_eur_sm3(
            hp_result_year1.hp_electricity_kWh,
            cfg.tariff.import_price_eur_kWh,
        )

        for mult in cfg.gas_price_sensitivity_range:
            gas_price_s = current_gas_price * mult
            gas_cost_s  = bp.gas_consumption_annual_units * gas_price_s
            hp_el_cost  = hp_result_year1.hp_electricity_kWh * cfg.tariff.import_price_eur_kWh
            hp_cheaper  = hp_el_cost < gas_cost_s

            # Approximate NPV of PV+HP at this gas price
            # (just shift the gas saving component)
            pv_hp_npv = 0.0
            if SCENARIO_PV_HP in scenarios:
                base_pv_hp = scenarios[SCENARIO_PV_HP].npv_eur
                old_gas_saving = (bp.gas_consumption_annual_units - hp_result_year1.backup_fuel_units) * current_gas_price
                new_gas_saving = (bp.gas_consumption_annual_units - hp_result_year1.backup_fuel_units) * gas_price_s
                # Approx: NPV shifts by PV of gas saving difference
                r = cfg.financial.discount_rate
                n = lifetime_years
                ann = (1 - (1+r)**(-n)) / r if r > 0 else n
                pv_hp_npv = base_pv_hp + (new_gas_saving - old_gas_saving) * ann

            sensitivity_list.append(GasPriceSensitivity(
                gas_price_multiplier   = mult,
                gas_price_eur_sm3      = round(gas_price_s, 3),
                annual_gas_cost_eur    = round(gas_cost_s, 0),
                annual_heating_cost_hp_eur = round(hp_el_cost, 0),
                hp_cheaper             = hp_cheaper,
                npv_pv_hp_eur          = round(pv_hp_npv, 0),
            ))

    return ScenarioComparison(
        scenarios                  = scenarios,
        baseline                   = baseline,
        ranked_scenarios           = ranked,
        recommended_scenario_id    = recommended_id,
        recommendation_rationale   = rationale,
        gas_breakeven_price_eur_sm3= round(gas_breakeven, 3),
        gas_price_sensitivity      = sensitivity_list,
        current_gas_price_eur_sm3  = current_gas_price,
        hp_sizing                  = hp_sizing_info,
    )
