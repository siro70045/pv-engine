"""
main.py

Entry point for the PV feasibility engine.
Connects all modules into a single end-to-end pipeline:

    PVGIS  →  Envelope  →  Loads  →  Solar  →  EnergyBalance  →  Economics

Usage (Python)
--------------
    from main import run_simulation, SimulationInput
    result = run_simulation(inp)
    result.print_summary()

Usage (CLI)
-----------
    python main.py --config example_warehouse.json
    python main.py --lat 44.5 --lon 11.3 --kwp 120 --demand 95000

Pipeline
--------

  1. PVGIS fetch
     fetch_tmy(lat, lon)  →  TMYResult
     Provides: hourly weather, monthly T_ext, GHI, DNI for the exact site.

  2. Envelope (optional — skip if annual_demand_kWh is given directly)
     calc_thermal_demand(envelope, T_ext, GHI, DNI)  →  EnvelopeResult
     Provides: monthly heating and cooling demand.

  3. Loads
     calc_loads(loads_input)  →  LoadsResult
     Provides: 8760-hour electrical demand profile.

  4. Solar simulation
     simulate_pv(tmy, system)  →  SolarResult
     Provides: 8760-hour AC production profile.

  5. Lifetime energy balance
     simulate_lifetime(base_inp)  →  list[EnergyBalanceResult]
     Provides: self-consumption, grid flows, savings — for each of 25 years.

  6. Economics
     calc_economics(yearly, investment, opex, fin, incentives)  →  EconomicsResult
     Provides: NPV, IRR, payback, score, sensitivity, cash flows.
"""

from __future__ import annotations

import json
import sys
import argparse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from engine.pvgis_client import fetch_tmy, TMYResult, PVGISError
from engine.envelope import (
    Envelope, OpaqueComponent, Window, GroundFloor,
    LinearThermalBridge, PointThermalBridge,
    calc_thermal_demand, EnvelopeResult,
)
from engine.loads import (
    LoadsInput, LoadsResult, calc_loads,
    OccupancySchedule, DHWSystem, VentilationSystem,
    LightingSystem, ApplianceLoad, HVACSystem,
)
from engine.solar import (
    PVSystem, SystemLosses, simulate_pv, SolarResult,
    PVLIB_AVAILABLE,
)
from engine.energy_balance import (
    BatterySystem, GridTariff, EnergyBalanceInput,
    EnergyBalanceResult, simulate_lifetime,
    extract_pv_profile, find_battery_replacement_years,
)
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters,
    ItalianIncentives, EconomicsResult, calc_economics,
    print_economics_summary,
)
from engine.energy_vectors import (
    BuildingEnergyProfile, GasBoilerSystem, HeatPumpSystem,
    BivalentConfig, FuelType, TerminalType, DEFAULT_PRICES,
    HeatPumpType,
)
from engine.scenarios import (
    ScenarioConfig, ScenarioComparison, compare_scenarios,
    SCENARIO_BASELINE, SCENARIO_SOLO_PV, SCENARIO_SOLO_HP,
    SCENARIO_PV_HP, SCENARIO_PV_HP_BIV,
)


# ══════════════════════════════════════════════════════════════════════════════
# TOP-LEVEL INPUT STRUCTURE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LocationInput:
    """Site location — drives PVGIS fetch and solar position."""
    lat: float
    lon: float
    altitude_m: float = 0.0
    use_pvgis_cache: bool = True


@dataclass
class BuildingInput:
    """
    Building general parameters.
    Either provide envelope (full physics) OR annual_demand_kWh (from bill).
    If both are provided, envelope takes precedence.
    """
    building_type: str = "warehouse"
    floor_area_m2: float = 1000.0
    heated_volume_m3: float = 0.0           # auto: floor_area × avg_height
    avg_floor_height_m: float = 6.0         # used to compute volume if not given
    n_occupants: int = 10

    # Optional: full envelope physics
    envelope: Optional[Envelope] = None

    # Fallback: direct annual demand from utility bills
    annual_demand_kWh: float = 0.0           # total electricity + thermal
    heating_demand_kWh: float = 0.0          # if known separately
    cooling_demand_kWh: float = 0.0

    # HVAC system (for electrical demand from thermal loads)
    hvac: Optional[HVACSystem] = None

    # ── Part-time occupancy ───────────────────────────────────────────────
    occupancy_days_per_week: float = 7.0
    # How many days per week the building is actually occupied [1–7].
    # Affects both the demand profile shape and the self-consumption rate.
    # Full-time residence: 7.0 (default)
    # Weekend/holiday home: 2.0–3.0
    # Half-week (e.g. Mon–Thu + alternating weekends): ~4.0
    # Alberto Sirotti case: ~3.5 (roughly half the week, evenings/weekends)

    occupancy_hour_start: int = 0
    # First hour of the day when occupancy begins (0–23).
    # 0 = all day (default for full-time residents)
    # 12 = afternoons/evenings only (e.g. workers away during the day)
    # 17 = evenings only
    # This shifts the demand profile so PV production in the morning/midday
    # hours encounters an unoccupied building — reducing self-consumption
    # in a way that flat scaling cannot capture.

    def effective_volume(self) -> float:
        if self.heated_volume_m3 > 0:
            return self.heated_volume_m3
        return self.floor_area_m2 * self.avg_floor_height_m


@dataclass
class PVSystemInput:
    """PV system configuration."""
    peak_power_kWp: float
    tilt_deg: float = 15.0             # shallow = flat industrial roof
    azimuth_deg: float = 180.0         # South
    module_efficiency_stc: float = 0.20
    temp_coefficient_pct_per_K: float = -0.40
    inverter_efficiency_nominal: float = 0.97
    dc_ac_ratio: float = 1.15
    mounting: str = "flat_roof_ballast"   # Faiman model mounting type
    iam_b0: float = 0.05
    degradation_rate_pct_per_yr: float = 0.50
    system_losses: Optional[SystemLosses] = None


@dataclass
class BatteryInput:
    """Battery storage — set capacity_kWh=0 for no battery."""
    capacity_kWh: float = 0.0
    chemistry: str = "LFP"
    roundtrip_efficiency: float = 0.93
    depth_of_discharge: float = 0.90
    calendar_degradation_pct_per_yr: float = 2.0
    cycle_degradation_pct_per_cycle: float = 0.003
    strategy: str = "self_consumption"   # or "peak_shaving"


@dataclass
class TariffInput:
    """Electricity tariff."""
    import_price_eur_kWh: float = 0.25
    export_price_eur_kWh: float = 0.08
    export_is_net_metering: bool = False
    time_of_use: bool = False
    peak_hours: list[int] = field(default_factory=lambda: list(range(8, 20)))
    peak_price_multiplier: float = 1.5
    fixed_charges_eur_yr: float = 0.0
    export_limit_pct: float = 100.0
    demand_charge_eur_kW_month: float = 0.0



@dataclass
class GasSystemInput:
    """
    Existing gas (or other fuel) heating system.
    Provide when the building has a gas boiler — enables HP scenario comparison.
    Set has_gas=False to skip all HP scenarios.
    """
    has_gas: bool = False
    fuel_type: str = "natural_gas"         # "natural_gas", "diesel", "lpg"
    annual_consumption_units: float = 0.0  # Sm³/yr for gas, litres for diesel/lpg
    boiler_efficiency: float = 0.88        # seasonal efficiency [0–1]
    terminal_type: str = "fan_coil"        # "fan_coil", "radiant_floor",
                                           # "low_temp_panel", "old_radiator", "none"
    dhw_fraction: float = 0.10             # fraction of gas used for hot water [0–1]
    annual_heating_kWh: float = 0.0        # space heating demand [kWh/yr]
                                           # if 0: derived from gas consumption
    gas_price_eur_sm3: float = 0.0         # actual price from bill [€/Sm³]
                                           # if 0: uses DEFAULT_PRICES (currently 0.95 €/Sm³)
                                           # IMPORTANT: always set from real bill to get
                                           # correct HP vs gas break-even analysis

    def to_boiler_system(self) -> GasBoilerSystem:
        fuel_map = {
            "natural_gas": FuelType.NATURAL_GAS,
            "diesel":      FuelType.DIESEL,
            "lpg":         FuelType.LPG,
        }
        term_map = {
            "fan_coil":      TerminalType.FAN_COIL,
            "radiant_floor": TerminalType.RADIANT_FLOOR,
            "low_temp_panel":TerminalType.LOW_TEMP_PANEL,
            "old_radiator":  TerminalType.OLD_RADIATOR,
            "none":          TerminalType.NONE,
        }
        return GasBoilerSystem(
            fuel_type                    = fuel_map.get(self.fuel_type, FuelType.NATURAL_GAS),
            annual_fuel_consumption_units= self.annual_consumption_units,
            boiler_efficiency            = self.boiler_efficiency,
            terminal_type                = term_map.get(self.terminal_type, TerminalType.FAN_COIL),
            dhw_fraction                 = self.dhw_fraction,
        )


@dataclass
class HeatPumpInput:
    """
    Heat pump system to evaluate as replacement for the gas boiler.
    Only used if GasSystemInput.has_gas = True.
    All fields have sensible defaults for a standard Italian renovation project.
    """
    terminal_type: str = "fan_coil"         # matches existing or new terminals
    carnot_efficiency_factor: float = 0.42  # A++ class air-to-water
    min_operating_temp_c: float = -15.0
    defrost_loss_factor: float = 0.08
    installed_cost_eur_kW: float = 380.0    # total installed €/kW thermal
    terminal_replacement_cost_eur: float = 0.0  # add if replacing old radiators
    cop_degradation_pct_per_yr: float = 0.5
    use_bivalent: bool = True               # keep boiler as backup (recommended)
    bivalence_temp_c: float = 2.0          # HP covers load above this °C

    def to_hp_system(self) -> HeatPumpSystem:
        term_map = {
            "fan_coil":      TerminalType.FAN_COIL,
            "radiant_floor": TerminalType.RADIANT_FLOOR,
            "low_temp_panel":TerminalType.LOW_TEMP_PANEL,
            "old_radiator":  TerminalType.OLD_RADIATOR,
            "none":          TerminalType.NONE,
        }
        return HeatPumpSystem(
            hp_type                   = HeatPumpType.AIR_TO_WATER,
            terminal_type             = term_map.get(self.terminal_type, TerminalType.FAN_COIL),
            carnot_efficiency_factor  = self.carnot_efficiency_factor,
            min_operating_temp_c      = self.min_operating_temp_c,
            defrost_loss_factor       = self.defrost_loss_factor,
            installed_cost_eur_kW     = self.installed_cost_eur_kW,
            terminal_replacement_cost_eur = self.terminal_replacement_cost_eur,
            cop_degradation_pct_per_yr= self.cop_degradation_pct_per_yr,
        )

@dataclass
class SimulationInput:
    """
    Complete input for the PV feasibility simulation.
    Minimal required fields: location, pv_system, investment.
    All other fields have sensible defaults.
    """
    # ── Required ──────────────────────────────────────────────────────────
    location: LocationInput
    pv_system: PVSystemInput
    investment: InvestmentCost

    # ── Building (provide envelope OR annual demand) ───────────────────────
    building: BuildingInput = field(default_factory=BuildingInput)

    # ── Storage ───────────────────────────────────────────────────────────
    battery: BatteryInput = field(default_factory=BatteryInput)

    # ── Economics ─────────────────────────────────────────────────────────
    tariff: TariffInput = field(default_factory=TariffInput)
    opex: OperatingCost = field(default_factory=OperatingCost)
    financial: FinancialParameters = field(default_factory=FinancialParameters)
    incentives: ItalianIncentives = field(default_factory=ItalianIncentives)

    # ── Gas system and heat pump (optional) ─────────────────────────────
    gas_system: GasSystemInput = field(default_factory=GasSystemInput)
    heat_pump: HeatPumpInput = field(default_factory=HeatPumpInput)
    # If gas_system.has_gas=True, the pipeline automatically runs:
    #   - Solo FV   (PV only, boiler unchanged)
    #   - Solo HP   (heat pump replaces boiler, no PV)
    #   - FV + HP   (PV + heat pump, co-optimised)
    #   - FV + HP bivalente  (PV + HP with gas backup)
    # Set gas_system.has_gas=False to run only the standard PV analysis.
    incentives_hp: ItalianIncentives = field(default_factory=ItalianIncentives)
    # Italian incentives applicable to the heat pump investment separately.

    # ── Simulation control ────────────────────────────────────────────────
    lifetime_years: int = 25
    use_pvlib: bool = True       # use pvlib if installed; fallback to internal


# ══════════════════════════════════════════════════════════════════════════════
# COMPLETE SIMULATION RESULT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SimulationResult:
    """
    Aggregated output of the full pipeline.
    Contains results from each module for inspection and reporting.
    """
    # ── Intermediate results ──────────────────────────────────────────────
    tmy: TMYResult
    envelope_result: Optional[EnvelopeResult]
    loads_result: LoadsResult
    solar_result: SolarResult
    yearly_balance: list[EnergyBalanceResult]   # one per year

    # ── Final economics ───────────────────────────────────────────────────
    economics: EconomicsResult

    # ── Multi-scenario comparison (None if gas_system.has_gas=False) ─────
    scenario_comparison: Optional[ScenarioComparison]

    # ── Input echo ────────────────────────────────────────────────────────
    inp: SimulationInput

    def print_summary(self) -> None:
        """Print a concise end-to-end summary to stdout."""
        SEP = "═" * 72
        loc = self.tmy.location
        eb1 = self.yearly_balance[0]

        print(f"\n{SEP}")
        print(f"  PV FEASIBILITY REPORT")
        print(f"  {loc.country}  |  {loc.latitude:.4f}°N  {loc.longitude:.4f}°E"
              f"  |  elev {loc.elevation_m:.0f} m  |  dataset {loc.dataset}")
        print(SEP)

        print(f"\n  SYSTEM")
        print(f"    PV power            : {self.inp.pv_system.peak_power_kWp} kWp")
        if self.inp.battery.capacity_kWh > 0:
            print(f"    Battery             : {self.inp.battery.capacity_kWh} kWh"
                  f"  ({self.inp.battery.chemistry})")
        print(f"    Tilt / Azimuth      : {self.inp.pv_system.tilt_deg}° /"
              f" {self.inp.pv_system.azimuth_deg}°")
        if PVLIB_AVAILABLE:
            print(f"    Solar model         : pvlib")
        else:
            print(f"    Solar model         : internal (install pvlib for higher accuracy)")

        print(f"\n  SITE CLIMATE  (TMY annual averages)")
        m = self.tmy.monthly
        print(f"    Mean temperature    : {sum(m.T_avg)/12:.1f} °C")
        print(f"    Annual GHI          : {sum(m.GHI_monthly):.0f} kWh/m²/yr")

        sr = self.solar_result
        print(f"\n  PV PRODUCTION  (year 1)")
        print(f"    Annual production   : {sr.production_annual_kWh:>10,.0f} kWh/yr")
        print(f"    Specific yield      : {sr.specific_yield_kWh_kWp:>10,.0f} kWh/kWp")
        print(f"    Performance ratio   : {sr.performance_ratio:>10.1%}")

        lr = self.loads_result
        print(f"\n  BUILDING DEMAND")
        print(f"    Annual demand       : {lr.total_annual_kWh:>10,.0f} kWh/yr")
        print(f"    Specific demand     : {lr.specific_demand_kWh_m2:>10.1f} kWh/m²yr")

        print(f"\n  ENERGY BALANCE  (year 1)")
        print(f"    Self-consumption    : {eb1.self_consumption_rate:>10.1%}"
              f"  of PV used on-site")
        print(f"    Self-sufficiency    : {eb1.self_sufficiency_rate:>10.1%}"
              f"  of demand covered by PV")
        print(f"    Grid import         : {eb1.grid_import_kWh:>10,.0f} kWh/yr")
        print(f"    Grid export         : {eb1.grid_export_kWh:>10,.0f} kWh/yr")
        if eb1.curtailed_kWh > 0:
            print(f"    Curtailed           : {eb1.curtailed_kWh:>10,.0f} kWh/yr"
                  f"  ← lost to export limit")

        eco = self.economics
        print(f"\n  INVESTMENT")
        print(f"    Total CAPEX         : {eco.total_capex_eur:>10,.0f} €")
        if eco.incentive_present_value_eur > 0:
            print(f"    Incentives (PV)     : {eco.incentive_present_value_eur:>10,.0f} €")
            print(f"    Net CAPEX           : {eco.net_capex_after_incentives_eur:>10,.0f} €")

        irr_str = f"{eco.irr_pct:.1f}%" if eco.irr_pct > -99 else "n/a"
        pb_str  = (f"{eco.payback_simple_yr:.1f} yr"
                   if eco.payback_simple_yr < 99 else "> analysis period")
        print(f"\n  KEY KPIs")
        print(f"    NPV ({eco.discount_rate:.0%})           : {eco.npv_eur:>10,.0f} €")
        print(f"    IRR                 : {irr_str:>10}")
        print(f"    Simple payback      : {pb_str:>10}")
        print(f"    LCOE                : {eco.lcoe_eur_kWh:>10.4f} €/kWh")

        bar  = "█" * int(eco.score) + ("▌" if (eco.score % 1) >= 0.5 else "")
        bar += "░" * (10 - len(bar.replace("▌","x")))
        print(f"\n  VERDICT  (solo FV)")
        print(f"    Score : {eco.score:.1f} / 10   {bar}")
        print(f"    → {eco.recommendation}")

        if self.scenario_comparison:
            sc = self.scenario_comparison
            print(f"\n  SCENARIO COMPARISON")
            LABELS = {
                SCENARIO_BASELINE:  "Baseline (attuale)",
                SCENARIO_SOLO_PV:   "Solo FV",
                SCENARIO_SOLO_HP:   "Solo pompa di calore",
                SCENARIO_PV_HP:     "FV + pompa di calore",
                SCENARIO_PV_HP_BIV: "FV + HP bivalente",
            }
            for sid, s in sc.scenarios.items():
                lbl = LABELS.get(sid, sid)
                npv_str = f"VAN €{s.npv_eur:,.0f}" if s.npv_eur != 0 else "baseline"
                pb_str  = (f"PB {s.payback_simple_yr:.1f} anni"
                           if s.payback_simple_yr < 99 else "")
                co2_str = (f"CO₂ −{s.co2_saving_pct:.0f}%"
                           if s.co2_saving_pct > 0 else "")
                parts = [p for p in [npv_str, pb_str, co2_str] if p]
                print(f"    {lbl:<28} {' | '.join(parts)}")
            print(f"\n    → Raccomandato: "
                  f"{LABELS.get(sc.recommended_scenario_id, sc.recommended_scenario_id)}")
            if sc.gas_breakeven_price_eur_sm3 > 0:
                cur  = sc.current_gas_price_eur_sm3
                bep  = sc.gas_breakeven_price_eur_sm3
                if bep <= cur:
                    print(f"    → Break-even gas: {bep:.2f} €/Sm³ — HP già conveniente ora")
                else:
                    pct = (bep/cur - 1) * 100
                    print(f"    → Break-even gas: {bep:.2f} €/Sm³ "
                          f"(+{pct:.0f}% dal prezzo attuale {cur:.2f} €/Sm³)")
        print(f"\n{SEP}\n")


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_simulation(inp: SimulationInput) -> SimulationResult:
    """
    Run the complete PV feasibility simulation.

    Parameters
    ----------
    inp : SimulationInput
        All input parameters.

    Returns
    -------
    SimulationResult
        Full results from every pipeline stage.

    Raises
    ------
    PVGISError
        If the PVGIS API call fails.
    ValueError
        If required inputs are inconsistent.
    """

    # ── Step 1: fetch TMY from PVGIS ─────────────────────────────────────────
    print(f"[1/6] Fetching TMY from PVGIS for"
          f" ({inp.location.lat:.4f}°N, {inp.location.lon:.4f}°E)...")
    tmy = fetch_tmy(
        lat       = inp.location.lat,
        lon       = inp.location.lon,
        use_cache = inp.location.use_pvgis_cache,
    )
    print(f"      {tmy.location.country}  |  {tmy.location.dataset}"
          f"  |  elev {tmy.location.elevation_m:.0f} m"
          f"  |  GHI {sum(tmy.monthly.GHI_monthly):.0f} kWh/m²/yr")

    T_ext = tmy.monthly.T_avg
    GHI   = tmy.monthly.GHI_monthly
    DNI   = tmy.monthly.DNI_monthly

    # ── Step 2: envelope (optional) ──────────────────────────────────────────
    envelope_result: Optional[EnvelopeResult] = None
    heating_monthly = [0.0] * 12
    cooling_monthly = [0.0] * 12

    if inp.building.envelope is not None:
        print("[2/6] Computing thermal envelope demand...")
        bld = inp.building
        env = bld.envelope

        # Ensure volume is set
        if env.heated_volume_m3 <= 0:
            env.heated_volume_m3 = bld.effective_volume()

        envelope_result = calc_thermal_demand(
            envelope          = env,
            floor_area_m2     = bld.floor_area_m2,
            building_type     = bld.building_type,
            monthly_T_ext     = T_ext,
            monthly_irradiance_horizontal = GHI,
            monthly_irradiance_direct     = DNI,
            latitude_deg      = tmy.location.latitude,
        )
        heating_monthly = envelope_result.heating_monthly_kWh
        cooling_monthly = envelope_result.cooling_monthly_kWh
        print(f"      Heating: {envelope_result.heating_demand_kWh:,.0f} kWh/yr  |"
              f"  Cooling: {envelope_result.cooling_demand_kWh:,.0f} kWh/yr")
    else:
        print("[2/6] Envelope skipped — using provided demand values.")

        # ── Fix 4: seasonal distribution using TMY temperatures ───────────────
        # Heating is distributed proportionally to Heating Degree Days (HDD):
        #   HDD_m = Σ max(T_set - T_ext, 0) × hours_in_month
        # Cooling uses Cooling Degree Days (CDD) analogously.
        # This avoids the physically wrong flat-by-days distribution which
        # would assign heating load to August.
        T_HEAT_SET = 18.0   # heating setpoint [°C]
        T_COOL_SET = 26.0   # cooling setpoint [°C]
        HOURS_M    = [744,672,744,720,744,720,744,744,720,744,720,744]

        hdd = [max(T_HEAT_SET - T_ext[m], 0.0) * HOURS_M[m] for m in range(12)]
        cdd = [max(T_ext[m] - T_COOL_SET, 0.0) * HOURS_M[m] for m in range(12)]
        hdd_total = sum(hdd) or 1.0
        cdd_total = sum(cdd) or 1.0

        if inp.building.heating_demand_kWh > 0:
            heating_monthly = [
                inp.building.heating_demand_kWh * hdd[m] / hdd_total
                for m in range(12)
            ]
        if inp.building.cooling_demand_kWh > 0:
            cooling_monthly = [
                inp.building.cooling_demand_kWh * cdd[m] / cdd_total
                for m in range(12)
            ]

    # ── Step 3: loads ─────────────────────────────────────────────────────────
    print("[3/6] Computing hourly load profile...")
    bld = inp.building

    # Build HVAC system
    hvac = bld.hvac or HVACSystem(
        heating_system = "heat_pump",
        cooling_system = "split_ac",
        heating_cop    = 3.0,
        cooling_eer    = 2.8,
        auxiliary_W    = max(bld.floor_area_m2 * 0.1, 200),
    )

    # ── Fix B: build occupancy schedule reflecting actual usage pattern ────
    # days_per_week < 7 means the building is unoccupied on some days.
    # hour_start > 0 means occupants are away during morning/midday hours.
    # Both reduce self-consumption because PV production peaks ~11:00–15:00.
    _days_pw   = max(1.0, min(7.0, bld.occupancy_days_per_week))
    _hour_st   = max(0, min(23, bld.occupancy_hour_start))
    # Convert to fraction for OccupancySchedule (vacation_fraction proxy)
    # We map occupancy_days_per_week into a vacation_fraction equivalent
    # and restrict which hours generate peak demand via the OccupancySchedule.
    _occ_frac  = _days_pw / 7.0   # 0.5 = occupied 50% of days

    loads_input = LoadsInput(
        floor_area_m2              = bld.floor_area_m2,
        building_type              = bld.building_type,
        occupancy                  = OccupancySchedule(
            building_type    = bld.building_type,
            n_occupants      = max(1, round(bld.n_occupants * _occ_frac)),
            vacation_fraction= max(0.05, 1.0 - _occ_frac),
            vacation_months  = [],   # not fixed-month vacations but distributed
        ),
        dhw                        = DHWSystem(
            daily_demand_L_per_person = (
                50.0 if bld.building_type == "residential" else 5.0
            ),
            is_electric = True,
        ),
        ventilation                = VentilationSystem(
            airflow_m3h_per_person    = 36.0,
            specific_fan_power_W_m3h  = 1.2,
            operation_hours_per_day   = 16.0,
        ),
        lighting                   = LightingSystem(
            installed_power_W_m2 = (
                8.0 if bld.building_type in ("warehouse","factory_light","factory_heavy")
                else 10.0
            ),
            floor_area_m2        = bld.floor_area_m2,
            daylight_factor      = 0.10,
        ),
        appliances                 = [],   # use benchmark by default
        hvac                       = hvac,
        heating_demand_monthly_kWh = heating_monthly,
        cooling_demand_monthly_kWh = cooling_monthly,
        annual_bill_kWh            = inp.building.annual_demand_kWh,
        monthly_T_ext              = T_ext,
    )

    loads_result = calc_loads(loads_input)

    # ── Fix B continued: zero out demand during unoccupied hours ─────────────
    # If hour_start > 0, the building is unoccupied before that hour.
    # Scale down demand in those hours (not to zero — HVAC frost protection,
    # fridge, standby loads remain) to 10% of normal.
    if _hour_st > 0:
        # Standby fraction when unoccupied:
        #   Residential: ~25% (fridge always-on, router, standby, water heater)
        #   Commercial/industrial: ~10% (HVAC frost, security, servers)
        _standby = 0.25 if bld.building_type == "residential" else 0.10
        masked = list(loads_result.hourly_demand_Wh)
        for i in range(len(masked)):
            h = i % 24
            if h < _hour_st:
                masked[i] *= _standby   # standby load when unoccupied
        from dataclasses import replace as _dc_replace
        # Recompute annual total after masking
        new_total = sum(masked) / 1000.0
        loads_result = _dc_replace(
            loads_result,
            hourly_demand_Wh       = masked,
            total_annual_kWh       = round(new_total, 1),
            specific_demand_kWh_m2 = round(new_total / bld.floor_area_m2, 1),
        )
        print(f"      [occupancy mask] hour_start={_hour_st}h: "
              f"demand reduced to {new_total:,.0f} kWh/yr "
              f"({new_total/bld.floor_area_m2:.1f} kWh/m²yr)")

    # ── Fix 1: scale bottom-up profile to match annual bill if provided ────────
    # calc_loads() builds a physically correct *shape* (hourly pattern).
    # When the user knows the annual total from a real utility bill, we scale
    # the entire profile proportionally so the total matches — preserving
    # the correct load-shape while anchoring to measured consumption.
    bill = inp.building.annual_demand_kWh
    if bill > 0 and loads_result.total_annual_kWh > 0:
        scale = bill / loads_result.total_annual_kWh
        if abs(scale - 1.0) > 0.05:   # only scale if difference > 5%
            scaled_hourly = [v * scale for v in loads_result.hourly_demand_Wh]
            # Rebuild result with scaled values (keep all other fields)
            from dataclasses import replace as _dc_replace
            loads_result = _dc_replace(
                loads_result,
                hourly_demand_Wh        = scaled_hourly,
                total_annual_kWh        = round(loads_result.total_annual_kWh * scale, 1),
                specific_demand_kWh_m2  = round(loads_result.specific_demand_kWh_m2 * scale, 1),
                total_monthly_kWh       = [round(v * scale, 1) for v in loads_result.total_monthly_kWh],
                dhw_monthly_kWh         = [round(v * scale, 1) for v in loads_result.dhw_monthly_kWh],
                ventilation_monthly_kWh = [round(v * scale, 1) for v in loads_result.ventilation_monthly_kWh],
                lighting_monthly_kWh    = [round(v * scale, 1) for v in loads_result.lighting_monthly_kWh],
                appliances_monthly_kWh  = [round(v * scale, 1) for v in loads_result.appliances_monthly_kWh],
                hvac_heating_monthly_kWh= [round(v * scale, 1) for v in loads_result.hvac_heating_monthly_kWh],
                hvac_cooling_monthly_kWh= [round(v * scale, 1) for v in loads_result.hvac_cooling_monthly_kWh],
                hvac_auxiliary_monthly_kWh = [round(v * scale, 1) for v in loads_result.hvac_auxiliary_monthly_kWh],
                dhw_kWh         = round(loads_result.dhw_kWh * scale, 1),
                ventilation_kWh = round(loads_result.ventilation_kWh * scale, 1),
                lighting_kWh    = round(loads_result.lighting_kWh * scale, 1),
                appliances_kWh  = round(loads_result.appliances_kWh * scale, 1),
                hvac_heating_kWh= round(loads_result.hvac_heating_kWh * scale, 1),
                hvac_cooling_kWh= round(loads_result.hvac_cooling_kWh * scale, 1),
                hvac_auxiliary_kWh = round(loads_result.hvac_auxiliary_kWh * scale, 1),
                peak_demand_W   = round(loads_result.peak_demand_W * scale, 1),
            )
            print(f"      [bill scaling] bottom-up {bill/scale:,.0f} → "
                  f"bill-anchored {bill:,.0f} kWh/yr (×{scale:.2f})")

    print(f"      Annual demand: {loads_result.total_annual_kWh:,.0f} kWh/yr"
          f"  ({loads_result.specific_demand_kWh_m2:.1f} kWh/m²yr)")

    # ── Step 4: PV simulation ─────────────────────────────────────────────────
    print(f"[4/6] Simulating PV production"
          f" ({'pvlib' if PVLIB_AVAILABLE and inp.use_pvlib else 'internal engine'})...")

    # ── Fix 3: auto-sync investment.peak_power_kWp ────────────────────────────
    # If investment.peak_power_kWp was left at 0 (or differs from pv_system),
    # silently align it to avoid a CAPEX of €0.
    pv_kwp = inp.pv_system.peak_power_kWp
    if inp.investment.peak_power_kWp != pv_kwp:
        if inp.investment.peak_power_kWp == 0:
            import warnings
            warnings.warn(
                f"investment.peak_power_kWp was 0 — auto-set to "
                f"pv_system.peak_power_kWp={pv_kwp} kWp. "
                f"Set investment.peak_power_kWp explicitly to silence this warning.",
                UserWarning, stacklevel=2,
            )
        inp.investment.peak_power_kWp = pv_kwp
    # Also sync battery capacity in investment if battery is set
    if (inp.battery.capacity_kWh > 0
            and inp.investment.battery_capacity_kWh != inp.battery.capacity_kWh):
        inp.investment.battery_capacity_kWh = inp.battery.capacity_kWh

    pv_inp = inp.pv_system
    pv_system = PVSystem(
        peak_power_kWp             = pv_inp.peak_power_kWp,
        tilt_deg                   = pv_inp.tilt_deg,
        azimuth_deg                = pv_inp.azimuth_deg,
        module_efficiency_stc      = pv_inp.module_efficiency_stc,
        temp_coefficient_pct_per_K = pv_inp.temp_coefficient_pct_per_K,
        inverter_efficiency_nominal= pv_inp.inverter_efficiency_nominal,
        dc_ac_ratio                = pv_inp.dc_ac_ratio,
        system_losses              = pv_inp.system_losses or SystemLosses(),
        degradation_rate_pct_per_yr= pv_inp.degradation_rate_pct_per_yr,
        iam_b0                     = pv_inp.iam_b0,
    )

    solar_result = simulate_pv(
        tmy      = tmy,
        system   = pv_system,
        mounting = pv_inp.mounting,
        use_pvlib= inp.use_pvlib,
    )
    print(f"      Production: {solar_result.production_annual_kWh:,.0f} kWh/yr"
          f"  ({solar_result.specific_yield_kWh_kWp:.0f} kWh/kWp)"
          f"  PR: {solar_result.performance_ratio:.1%}")

    # ── Step 5: lifetime energy balance ──────────────────────────────────────
    print(f"[5/6] Running {inp.lifetime_years}-year energy balance...")

    # Battery replacement years (for opex)
    bat_inp = inp.battery
    bat_system = BatterySystem(
        capacity_kWh                    = bat_inp.capacity_kWh,
        roundtrip_efficiency            = bat_inp.roundtrip_efficiency,
        depth_of_discharge              = bat_inp.depth_of_discharge,
        calendar_degradation_pct_per_yr = bat_inp.calendar_degradation_pct_per_yr,
        cycle_degradation_pct_per_cycle = bat_inp.cycle_degradation_pct_per_cycle,
        chemistry                       = bat_inp.chemistry,
    )

    bat_replacement_years = []
    if bat_inp.capacity_kWh > 0:
        bat_replacement_years = find_battery_replacement_years(
            bat_system,
            lifetime_years = inp.lifetime_years,
            threshold      = 0.80,
        )
        if bat_replacement_years:
            print(f"      Battery replacement scheduled: year(s) {bat_replacement_years}")

    tariff_obj = GridTariff(
        import_price_eur_kWh   = inp.tariff.import_price_eur_kWh,
        export_price_eur_kWh   = inp.tariff.export_price_eur_kWh,
        export_is_net_metering = inp.tariff.export_is_net_metering,
        time_of_use            = inp.tariff.time_of_use,
        peak_hours             = inp.tariff.peak_hours,
        peak_price_multiplier  = inp.tariff.peak_price_multiplier,
        fixed_charges_eur_yr   = inp.tariff.fixed_charges_eur_yr,
        export_limit_pct       = inp.tariff.export_limit_pct,
        demand_charge_eur_kW_month = inp.tariff.demand_charge_eur_kW_month,
    )

    pv_profile = extract_pv_profile(solar_result)
    base_eb_inp = EnergyBalanceInput(
        pv_production_hourly_Wh = pv_profile,
        demand_hourly_Wh        = loads_result.hourly_demand_Wh,
        battery                 = bat_system,
        tariff                  = tariff_obj,
        peak_power_kWp          = pv_inp.peak_power_kWp,
        battery_strategy        = bat_inp.strategy,
    )

    yearly_balance = simulate_lifetime(
        base_inp              = base_eb_inp,
        lifetime_years        = inp.lifetime_years,
        pv_degradation_pct_per_yr = pv_inp.degradation_rate_pct_per_yr,
    )

    eb1 = yearly_balance[0]
    print(f"      SCR: {eb1.self_consumption_rate:.1%}  "
          f"SSR: {eb1.self_sufficiency_rate:.1%}  "
          f"Saving yr1: €{eb1.annual_saving_eur:,.0f}")

    # ── Step 6: economics ─────────────────────────────────────────────────────
    print("[6/6] Computing techno-economic assessment...")

    # Auto-fill battery replacement cost in opex
    opex = inp.opex
    if bat_replacement_years and not opex.battery_replacement_years:
        opex.battery_replacement_years = bat_replacement_years

    # Auto-fill inverter replacement if not set
    if opex.inverter_replacement_cost_eur == 0 and pv_inp.peak_power_kWp > 0:
        # Rough estimate: 10% of PV CAPEX
        opex.inverter_replacement_cost_eur = (
            inp.investment.pv_cost_eur_kWp * pv_inp.peak_power_kWp * 0.10
        )

    # ── Fix 2: synchronise analysis horizon ──────────────────────────────────
    # financial.analysis_years controls the economics window.
    # lifetime_years controls the energy simulation window.
    # They should match; if they differ, align economics to min of the two
    # and warn the user.
    effective_years = min(inp.lifetime_years, inp.financial.analysis_years)
    if inp.lifetime_years != inp.financial.analysis_years:
        import warnings
        warnings.warn(
            f"lifetime_years ({inp.lifetime_years}) != "
            f"financial.analysis_years ({inp.financial.analysis_years}). "
            f"Economics computed over {effective_years} years (the minimum). "
            f"Set both to the same value to avoid this mismatch.",
            UserWarning, stacklevel=2,
        )
        from dataclasses import replace as _dc_replace2
        fin_aligned = _dc_replace2(inp.financial, analysis_years=effective_years)
        yearly_balance_aligned = yearly_balance[:effective_years]
    else:
        fin_aligned = inp.financial
        yearly_balance_aligned = yearly_balance

    economics_result = calc_economics(
        yearly_results = yearly_balance_aligned,
        investment     = inp.investment,
        opex           = opex,
        fin            = fin_aligned,
        incentives     = inp.incentives,
    )
    print(f"      NPV: €{economics_result.npv_eur:,.0f}  |"
          f"  IRR: {economics_result.irr_pct:.1f}%  |"
          f"  Payback: {economics_result.payback_simple_yr:.1f} yr  |"
          f"  Score: {economics_result.score}/10")

    # ── Step 7: multi-scenario comparison (gas + HP) ──────────────────────────
    scenario_comparison: Optional[ScenarioComparison] = None

    if inp.gas_system.has_gas:
        print("[7/7] Running multi-scenario comparison (gas → HP)...")

        # Build BuildingEnergyProfile from inputs
        from engine.energy_vectors import EnergyPrice

        # ── Fix A: use real gas price from user input ─────────────────────────
        # DEFAULT_PRICES embeds a reference price (0.95 €/Sm³ residential 2024).
        # The actual price from the user's bill can differ significantly.
        # We build a custom EnergyPrice using the real price so that all
        # HP vs gas break-even calculations use the correct baseline.
        if inp.gas_system.gas_price_eur_sm3 > 0:
            gas_price_actual = EnergyPrice(
                price_eur_per_unit      = inp.gas_system.gas_price_eur_sm3,
                unit_kwh_conversion     = DEFAULT_PRICES[FuelType.NATURAL_GAS].unit_kwh_conversion,
                annual_escalation_rate  = inp.financial.energy_price_escalation,
                co2_factor_kg_per_unit  = DEFAULT_PRICES[FuelType.NATURAL_GAS].co2_factor_kg_per_unit,
            )
            # Temporarily patch DEFAULT_PRICES so GasBoilerSystem properties
            # (annual_fuel_cost_eur, break-even etc.) use the real price
            _orig_gas_price = DEFAULT_PRICES[FuelType.NATURAL_GAS]
            DEFAULT_PRICES[FuelType.NATURAL_GAS] = gas_price_actual
        else:
            _orig_gas_price = None
            import warnings
            warnings.warn(
                "GasSystemInput.gas_price_eur_sm3 is 0 — using default 0.95 €/Sm³. "
                "Set gas_price_eur_sm3 from the real bill for accurate HP break-even analysis.",
                UserWarning, stacklevel=2,
            )

        boiler_sys = inp.gas_system.to_boiler_system()
        bp = BuildingEnergyProfile(
            electricity_kwh_annual       = loads_result.total_annual_kWh,
            electricity_monthly_kwh      = loads_result.total_monthly_kWh,
            electricity_price            = DEFAULT_PRICES[FuelType.ELECTRICITY],
            gas_consumption_annual_units = inp.gas_system.annual_consumption_units,
            gas_system                   = boiler_sys,
            has_gas                      = True,
            co2_electricity_kg_per_kwh   = 0.233,
        )
        # Override electricity price from tariff
        bp.electricity_price = EnergyPrice(
            price_eur_per_unit     = inp.tariff.import_price_eur_kWh,
            unit_kwh_conversion    = 1.0,
            annual_escalation_rate = inp.financial.energy_price_escalation,
            co2_factor_kg_per_unit = 0.233,
        )

        # Build HP system
        hp_sys = inp.heat_pump.to_hp_system()

        # Build bivalent config if requested
        bivalent_cfg = None
        if inp.heat_pump.use_bivalent:
            bivalent_cfg = BivalentConfig(
                bivalence_temp_c  = inp.heat_pump.bivalence_temp_c,
                backup_system     = boiler_sys,
                keep_existing_boiler = True,
            )

        # Annual heating demand for HP sizing
        heating_kWh = inp.gas_system.annual_heating_kWh
        if heating_kWh <= 0 and inp.gas_system.annual_consumption_units > 0:
            heating_kWh = boiler_sys.annual_heating_kWh

        # Build GridTariff for scenarios
        from engine.energy_balance import GridTariff as _GT
        tariff_sc = _GT(
            import_price_eur_kWh    = inp.tariff.import_price_eur_kWh,
            export_price_eur_kWh    = inp.tariff.export_price_eur_kWh,
            export_is_net_metering  = inp.tariff.export_is_net_metering,
            export_limit_pct        = inp.tariff.export_limit_pct,
            demand_charge_eur_kW_month = inp.tariff.demand_charge_eur_kW_month,
        )

        # Build BatterySystem for scenarios
        from engine.energy_balance import BatterySystem as _BS
        bat_sc = _BS(capacity_kWh=inp.battery.capacity_kWh) if inp.battery.capacity_kWh > 0             else _BS(capacity_kWh=0)

        # TMY hourly temperatures for HP simulation
        t_ext_hourly = []
        for m_idx, (T, days) in enumerate(
            zip(tmy.monthly.T_avg, [31,28,31,30,31,30,31,31,30,31,30,31])
        ):
            t_ext_hourly.extend([T] * days * 24)

        sc_cfg = ScenarioConfig(
            building_profile            = bp,
            pv_production_hourly_Wh     = pv_profile,
            base_demand_hourly_Wh       = loads_result.hourly_demand_Wh,
            t_ext_hourly                = t_ext_hourly,
            annual_heating_kWh          = heating_kWh,
            pv_peak_kWp                 = pv_inp.peak_power_kWp,
            battery                     = bat_sc,
            tariff                      = tariff_sc,
            pv_degradation_pct_per_yr   = pv_inp.degradation_rate_pct_per_yr,
            heat_pump                   = hp_sys,
            bivalent                    = bivalent_cfg,
            investment_pv               = inp.investment,
            opex_pv                     = opex,
            financial                   = fin_aligned,
            incentives_pv               = inp.incentives,
            incentives_hp               = inp.incentives_hp,
            run_solo_pv                 = True,
            run_solo_hp                 = True,
            run_pv_hp                   = True,
            run_pv_hp_bivalente         = inp.heat_pump.use_bivalent,
        )

        try:
            scenario_comparison = compare_scenarios(sc_cfg, lifetime_years=effective_years)
            rec = scenario_comparison.recommended_scenario_id
            print(f"      Raccomandato: {rec}  |  "
                  f"break-even gas: {scenario_comparison.gas_breakeven_price_eur_sm3:.2f} €/Sm³")
        except Exception as e:
            import warnings
            warnings.warn(f"Scenario comparison failed: {e}", UserWarning)
            scenario_comparison = None
        finally:
            # Restore DEFAULT_PRICES to avoid side-effects on subsequent calls
            if _orig_gas_price is not None:
                DEFAULT_PRICES[FuelType.NATURAL_GAS] = _orig_gas_price
    else:
        print("[7/7] Scenario comparison skipped (no gas system declared).")

    return SimulationResult(
        tmy                  = tmy,
        envelope_result      = envelope_result,
        loads_result         = loads_result,
        solar_result         = solar_result,
        yearly_balance       = yearly_balance,
        economics            = economics_result,
        scenario_comparison  = scenario_comparison,
        inp                  = inp,
    )


# ══════════════════════════════════════════════════════════════════════════════
# JSON SERIALISATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_input_from_json(path: str | Path) -> SimulationInput:
    """
    Load a SimulationInput from a JSON config file.

    The JSON must have keys matching SimulationInput fields.
    Nested dataclasses are constructed recursively.
    See example_warehouse.json for a complete template.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _build(cls, d: dict):
        """Recursively build a dataclass from a dict."""
        import dataclasses
        if not dataclasses.is_dataclass(cls):
            return d
        hints  = {f.name: f.type for f in dataclasses.fields(cls)}
        kwargs = {}
        for key, val in d.items():
            if key not in hints:
                continue
            hint = hints[key]
            # Resolve string annotations
            if isinstance(hint, str):
                hint = eval(hint, {
                    "Optional": Optional,
                    "list": list,
                    **globals(),
                })
            # Unwrap Optional[X] → X
            origin = getattr(hint, "__origin__", None)
            args   = getattr(hint, "__args__", ())
            if origin is type(None):
                kwargs[key] = val
                continue
            if origin is type(Optional) or (
                hasattr(hint, "__origin__") and hint.__origin__ is type(None)
            ):
                pass
            # Check if it's Optional (Union with None)
            import typing
            if (hasattr(hint, "__origin__")
                    and hint.__origin__ is typing.Union
                    and type(None) in args):
                inner = [a for a in args if a is not type(None)][0]
                if val is None:
                    kwargs[key] = None
                elif isinstance(val, dict) and hasattr(inner, "__dataclass_fields__"):
                    kwargs[key] = _build(inner, val)
                else:
                    kwargs[key] = val
            elif isinstance(val, dict) and hasattr(hint, "__dataclass_fields__"):
                kwargs[key] = _build(hint, val)
            else:
                kwargs[key] = val
        return cls(**kwargs)

    return _build(SimulationInput, data)


def result_to_dict(result: SimulationResult) -> dict:
    """
    Export key results to a JSON-serialisable dict.
    Omits hourly data (too large) but includes all monthly and annual figures.
    """
    eco = result.economics
    eb1 = result.yearly_balance[0]
    sr  = result.solar_result
    lr  = result.loads_result

    return {
        "location": {
            "country":          result.tmy.location.country,
            "latitude":         result.tmy.location.latitude,
            "longitude":        result.tmy.location.longitude,
            "elevation_m":      result.tmy.location.elevation_m,
            "dataset":          result.tmy.location.dataset,
            "annual_ghi_kWh_m2": round(sum(result.tmy.monthly.GHI_monthly), 1),
            "t_avg_annual_c":   round(sum(result.tmy.monthly.T_avg) / 12, 1),
            "monthly_ghi_kWh_m2": [round(v, 1) for v in result.tmy.monthly.GHI_monthly],
            "monthly_t_avg_c":  [round(v, 1) for v in result.tmy.monthly.T_avg],
        },
        "system": {
            "peak_power_kWp":     result.inp.pv_system.peak_power_kWp,
            "tilt_deg":           result.inp.pv_system.tilt_deg,
            "azimuth_deg":        result.inp.pv_system.azimuth_deg,
            "battery_kWh":        result.inp.battery.capacity_kWh,
            "battery_chemistry":  result.inp.battery.chemistry,
        },
        "pv_performance": {
            "production_annual_kWh":  sr.production_annual_kWh,
            "specific_yield_kWh_kWp": sr.specific_yield_kWh_kWp,
            "performance_ratio":      sr.performance_ratio,
            "production_monthly_kWh": sr.production_monthly_kWh,
        },
        "demand": {
            "total_annual_kWh":        lr.total_annual_kWh,
            "specific_kWh_m2":         lr.specific_demand_kWh_m2,
            "total_monthly_kWh":       lr.total_monthly_kWh,
            "category_shares":         lr.category_shares,
        },
        "energy_balance_year1": {
            "self_consumption_rate":  eb1.self_consumption_rate,
            "self_sufficiency_rate":  eb1.self_sufficiency_rate,
            "self_consumed_kWh":      eb1.self_consumed_kWh,
            "grid_import_kWh":        eb1.grid_import_kWh,
            "grid_export_kWh":        eb1.grid_export_kWh,
            "curtailed_kWh":          eb1.curtailed_kWh,
            "annual_saving_eur":      eb1.annual_saving_eur,
            "net_annual_benefit_eur": eb1.net_annual_benefit_eur,
        },
        "economics": {
            "total_capex_eur":              eco.total_capex_eur,
            "incentive_pv_eur":             eco.incentive_present_value_eur,
            "net_capex_eur":                eco.net_capex_after_incentives_eur,
            "npv_eur":                      eco.npv_eur,
            "irr_pct":                      eco.irr_pct,
            "payback_simple_yr":            eco.payback_simple_yr,
            "payback_discounted_yr":        eco.payback_discounted_yr,
            "lcoe_eur_kWh":                 eco.lcoe_eur_kWh,
            "roi_pct":                      eco.roi_pct,
            "benefit_cost_ratio":           eco.benefit_cost_ratio,
            "lifetime_production_kWh":      eco.lifetime_production_kWh,
            "lifetime_saving_eur":          eco.lifetime_saving_eur,
            "score":                        eco.score,
            "recommendation":               eco.recommendation,
            "score_breakdown":              eco.score_breakdown,
        },
        "sensitivity": [
            {
                "name":                    s.name,
                "energy_escalation_pct":   round(s.energy_price_escalation * 100, 1),
                "discount_rate_pct":       round(s.discount_rate * 100, 1),
                "npv_eur":                 s.npv_eur,
                "irr_pct":                 s.irr_pct,
                "payback_yr":              s.payback_simple_yr,
            }
            for s in eco.sensitivity
        ],
        "cash_flows": [
            {
                "year":              cf.year,
                "pv_production_kWh": cf.pv_production_kWh,
                "energy_saving_eur": cf.energy_saving_eur,
                "export_revenue_eur":cf.export_revenue_eur,
                "om_cost_eur":       cf.om_cost_eur,
                "replacement_eur":   cf.replacement_cost_eur,
                "tax_benefit_eur":   cf.tax_benefit_eur,
                "net_cf_eur":        cf.net_cash_flow_eur,
                "cumulative_cf_eur": cf.cumulative_cash_flow_eur,
                "npv_running_eur":   cf.cumulative_discounted_cf_eur,
            }
            for cf in eco.cash_flows
        ],
        "scenario_comparison": _scenario_comparison_to_dict(result.scenario_comparison),
    }


def _scenario_comparison_to_dict(sc: Optional[ScenarioComparison]) -> Optional[dict]:
    """Serialise ScenarioComparison to a JSON-safe dict."""
    if sc is None:
        return None

    LABELS = {
        SCENARIO_BASELINE:  "Baseline (attuale)",
        SCENARIO_SOLO_PV:   "Solo FV",
        SCENARIO_SOLO_HP:   "Solo pompa di calore",
        SCENARIO_PV_HP:     "FV + pompa di calore",
        SCENARIO_PV_HP_BIV: "FV + HP bivalente",
    }

    scenarios_out = {}
    for sid, s in sc.scenarios.items():
        scenarios_out[sid] = {
            "label":                          LABELS.get(sid, sid),
            "capex_eur":                      s.capex_eur,
            "pv_capex_eur":                   s.pv_capex_eur,
            "hp_capex_eur":                   s.hp_capex_eur,
            "annual_electricity_cost_eur":    s.annual_electricity_cost_eur,
            "annual_gas_cost_eur":            s.annual_gas_cost_eur,
            "annual_total_energy_cost_eur":   s.annual_total_energy_cost_eur,
            "annual_saving_vs_baseline_eur":  s.annual_saving_vs_baseline_eur,
            "pv_production_kWh":              s.pv_production_kWh,
            "self_consumption_rate":          s.self_consumption_rate,
            "self_sufficiency_rate":          s.self_sufficiency_rate,
            "grid_import_kWh":                s.grid_import_kWh,
            "grid_export_kWh":                s.grid_export_kWh,
            "gas_consumption_units":          s.gas_consumption_units,
            "hp_electricity_kWh":             s.hp_electricity_kWh,
            "seasonal_cop":                   s.seasonal_cop,
            "hp_coverage_pct":                round(s.hp_coverage_fraction * 100, 1),
            "annual_co2_kg":                  s.annual_co2_kg,
            "co2_saving_pct":                 s.co2_saving_pct,
            "npv_eur":                        s.npv_eur,
            "irr_pct":                        s.irr_pct,
            "payback_simple_yr":              s.payback_simple_yr,
            "payback_discounted_yr":          s.payback_discounted_yr,
            "lcoe_eur_kwh":                   s.lcoe_eur_kwh,
            "score":                          s.score,
            "recommendation":                 s.recommendation,
        }

    sensitivity_out = [
        {
            "gas_price_multiplier":   s.gas_price_multiplier,
            "gas_price_eur_sm3":      s.gas_price_eur_sm3,
            "annual_gas_cost_eur":    s.annual_gas_cost_eur,
            "annual_hp_cost_eur":     s.annual_heating_cost_hp_eur,
            "hp_cheaper":             s.hp_cheaper,
            "npv_pv_hp_eur":          s.npv_pv_hp_eur,
        }
        for s in sc.gas_price_sensitivity
    ]

    return {
        "recommended_scenario":          sc.recommended_scenario_id,
        "recommended_label":             LABELS.get(sc.recommended_scenario_id, ""),
        "recommendation_rationale":      sc.recommendation_rationale,
        "current_gas_price_eur_sm3":     sc.current_gas_price_eur_sm3,
        "gas_breakeven_price_eur_sm3":   sc.gas_breakeven_price_eur_sm3,
        "hp_sizing":                     sc.hp_sizing,
        "scenarios":                     scenarios_out,
        "gas_price_sensitivity":         sensitivity_out,
    }


def save_result_json(result: SimulationResult, path: str | Path) -> None:
    """Save the key results to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result_to_dict(result), f, indent=2, ensure_ascii=False)
    print(f"Results saved to {path}")


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE INPUT BUILDER (for quick testing without a JSON file)
# ══════════════════════════════════════════════════════════════════════════════

def make_example_warehouse_input(
    lat: float = 44.50,
    lon: float = 11.34,
    kwp: float = 120.0,
    annual_demand_kWh: float = 95000.0,
    import_price: float = 0.22,
    export_price: float = 0.07,
    annual_gas_sm3: float = 0.0,
    gas_heating_kwh: float = 0.0,
    gas_price_eur_sm3: float = 0.0,
) -> SimulationInput:
    """
    Build a ready-to-run SimulationInput for a typical Italian industrial warehouse.

    Parameters
    ----------
    annual_gas_sm3 : float
        Annual gas consumption [Sm³/yr]. Set > 0 to enable HP scenario comparison.
        Example: a 1 000 m² warehouse with gas boiler typically uses 6 000–12 000 Sm³/yr.
    gas_heating_kwh : float
        Space heating demand [kWh/yr]. If 0, derived from gas consumption.
    """
    return SimulationInput(
        location = LocationInput(
            lat               = lat,
            lon               = lon,
            use_pvgis_cache   = True,
        ),
        building = BuildingInput(
            building_type      = "warehouse",
            floor_area_m2      = 1000.0,
            avg_floor_height_m = 6.0,
            n_occupants        = 10,
            annual_demand_kWh  = annual_demand_kWh,
            hvac               = HVACSystem(
                heating_system = "heat_pump",
                cooling_system = "split_ac",
                heating_cop    = 3.0,
                cooling_eer    = 2.8,
                auxiliary_W    = 300.0,
            ),
        ),
        pv_system = PVSystemInput(
            peak_power_kWp             = kwp,
            tilt_deg                   = 10.0,
            azimuth_deg                = 180.0,
            mounting                   = "flat_roof_ballast",
            degradation_rate_pct_per_yr= 0.50,
        ),
        battery = BatteryInput(
            capacity_kWh = 0.0,   # no battery by default
        ),
        investment = InvestmentCost(
            pv_cost_eur_kWp        = 950.0,
            peak_power_kWp         = kwp,
            design_and_permits_eur = 6000.0,
            grid_connection_eur    = 4000.0,
        ),
        tariff = TariffInput(
            import_price_eur_kWh   = import_price,
            export_price_eur_kWh   = export_price,
            export_is_net_metering = False,
            fixed_charges_eur_yr   = 800.0,
        ),
        opex = OperatingCost(
            om_pct_of_capex_per_yr = 1.0,
            inverter_replacement_year = 12,
            om_escalation_rate     = 0.02,
        ),
        financial = FinancialParameters(
            discount_rate            = 0.05,
            energy_price_escalation  = 0.025,
            analysis_years           = 25,
            financing_equity_fraction= 1.0,
        ),
        incentives = ItalianIncentives(
            ammortamento_accelerato = True,
            aliquota_ammortamento   = 0.09,
            aliquota_ires_pct       = 24.0,
        ),
        gas_system = GasSystemInput(
            has_gas                  = annual_gas_sm3 > 0,
            fuel_type                = "natural_gas",
            annual_consumption_units = annual_gas_sm3,
            boiler_efficiency        = 0.88,
            terminal_type            = "fan_coil",
            dhw_fraction             = 0.10,
            annual_heating_kWh       = gas_heating_kwh,
            gas_price_eur_sm3        = gas_price_eur_sm3,
        ),
        heat_pump = HeatPumpInput(
            terminal_type            = "fan_coil",
            carnot_efficiency_factor = 0.42,
            installed_cost_eur_kW    = 380.0,
            use_bivalent             = True,
            bivalence_temp_c         = 2.0,
        ),
        incentives_hp = ItalianIncentives(
            ammortamento_accelerato = True,
            aliquota_ammortamento   = 0.09,
            aliquota_ires_pct       = 24.0,
        ),
        lifetime_years = 25,
        use_pvlib      = True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog        = "python main.py",
        description = "PV Feasibility Engine — techno-economic analysis",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Config file (overrides all other flags)
    p.add_argument("--config", metavar="FILE",
                   help="Path to JSON config file (see make_example_warehouse_input)")
    # Quick-run overrides
    p.add_argument("--lat",    type=float, default=44.50, help="Latitude [°]")
    p.add_argument("--lon",    type=float, default=11.34, help="Longitude [°]")
    p.add_argument("--kwp",    type=float, default=100.0, help="PV peak power [kWp]")
    p.add_argument("--demand", type=float, default=80000, help="Annual electricity demand [kWh]")
    p.add_argument("--import-price", type=float, default=0.22, help="Import tariff [€/kWh]")
    p.add_argument("--export-price", type=float, default=0.07, help="Export tariff [€/kWh]")
    p.add_argument("--battery-kwh",  type=float, default=0.0,  help="Battery capacity [kWh]")
    p.add_argument("--gas-sm3",   type=float, default=0.0,
                   help="Annual gas consumption [Sm³/yr] — enables HP scenario comparison")
    p.add_argument("--gas-heat-kwh", type=float, default=0.0,
                   help="Space heating demand [kWh/yr] (optional, derived from gas if 0)")
    p.add_argument("--gas-price", type=float, default=0.0,
                   help="Actual gas price from bill [€/Sm³] — critical for HP break-even accuracy")
    p.add_argument("--no-cache",     action="store_true", help="Bypass PVGIS cache")
    p.add_argument("--out",    metavar="FILE", help="Save JSON result to file")
    p.add_argument("--full",   action="store_true",
                   help="Print full economics table (in addition to summary)")
    p.add_argument("--scenarios", action="store_true",
                   help="Print full scenario comparison table")
    return p


def main() -> None:
    parser = _build_cli_parser()
    args   = parser.parse_args()

    if args.config:
        inp = load_input_from_json(args.config)
    else:
        inp = make_example_warehouse_input(
            lat               = args.lat,
            lon               = args.lon,
            kwp               = args.kwp,
            annual_demand_kWh = args.demand,
            import_price      = args.import_price,
            export_price      = args.export_price,
            annual_gas_sm3    = args.gas_sm3,
            gas_heating_kwh   = args.gas_heat_kwh,
            gas_price_eur_sm3 = args.gas_price,
        )
        if args.battery_kwh > 0:
            inp.battery.capacity_kWh = args.battery_kwh
            inp.investment.battery_capacity_kWh = args.battery_kwh
        if args.no_cache:
            inp.location.use_pvgis_cache = False

    try:
        result = run_simulation(inp)
    except PVGISError as e:
        print(f"\nERROR — PVGIS API: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"\nERROR — invalid input: {e}", file=sys.stderr)
        sys.exit(1)

    result.print_summary()

    if args.full:
        print_economics_summary(result.economics)

    if args.scenarios and result.scenario_comparison:
        result.scenario_comparison.print_comparison()

    if args.out:
        save_result_json(result, args.out)


if __name__ == "__main__":
    main()
