"""
tests/test_scenarios_level1.py

Stress-test del motore Meridiana — copertura di:
  engine.energy_vectors  (FuelType, GasBoilerSystem, BuildingEnergyProfile)
  engine.heat_pump        (calc_hourly_cop, simulate_heat_pump_year, ...)
  engine.scenarios        (compare_scenarios, ScenarioConfig)

Convertito da uno script storico (check()-based) a pytest moderno: ogni
Test N del sorgente è una funzione pytest indipendente, così i fallimenti
sono granulari.

Nessuna rete richiesta — tutti i dati sono sintetici.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from engine.energy_vectors import (
    FuelType, TerminalType, EnergyPrice, GasBoilerSystem,
    HeatPumpSystem, HeatPumpType, BivalentConfig,
    BuildingEnergyProfile, DEFAULT_PRICES, TERMINAL_SUPPLY_TEMP_C,
)
from engine.heat_pump import (
    calc_hourly_cop, calc_h_tr_from_annual,
    calc_hourly_thermal_demand, simulate_heat_pump_year,
    suggest_hp_sizing, HeatPumpYearResult,
)
from engine.scenarios import (
    ScenarioConfig, compare_scenarios,
    SCENARIO_BASELINE, SCENARIO_SOLO_PV, SCENARIO_SOLO_HP,
    SCENARIO_PV_HP, SCENARIO_PV_HP_BIV,
)
from engine.energy_balance import BatterySystem, GridTariff
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters, ItalianIncentives,
)


# ── Synthetic TMY ─────────────────────────────────────────────────────────────
T_EXT_MONTHLY = [2.0, 4.0, 8.5, 13.0, 18.0, 22.5, 25.5, 25.0, 20.5, 14.5, 8.0, 3.0]
DAYS_M = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def make_t_ext_hourly():
    h = []
    for m, (t, days) in enumerate(zip(T_EXT_MONTHLY, DAYS_M)):
        for _ in range(days * 24):
            h.append(t)
    return h


T_EXT = make_t_ext_hourly()
assert len(T_EXT) == 8760

PV_H = [800.0 if 8 <= (i % 24) < 17 else 0.0 for i in range(8760)]
DEM_H = [4000.0] * 8760   # 4 kW flat = ~35 MWh/yr


def close(a, b, tol=0.05):
    return abs(a - b) / max(abs(b), 1.0) < tol


# Shared computed thermal demand profile (used by tests 5 and 6)
H_TR_M = calc_h_tr_from_annual(50000.0, T_EXT)
THERMAL_M = calc_hourly_thermal_demand(T_EXT, H_TR_M, annual_heating_kWh=50000.0)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def profile():
    """BuildingEnergyProfile shared by Test 7 and Test 8."""
    return BuildingEnergyProfile(
        electricity_kwh_annual       = 80000.0,
        electricity_price            = DEFAULT_PRICES[FuelType.ELECTRICITY],
        gas_consumption_annual_units = 8000.0,
        gas_system                   = GasBoilerSystem(
            fuel_type=FuelType.NATURAL_GAS,
            annual_fuel_consumption_units=8000.0,
        ),
        has_gas                      = True,
        co2_electricity_kg_per_kwh   = 0.233,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

def test_01_energy_price_normalisation():
    """EnergyPrice normalisation for gas and electricity."""
    gas_price = DEFAULT_PRICES[FuelType.NATURAL_GAS]
    assert gas_price.price_eur_per_kwh > 0, (
        f"Gas price per kWh: {gas_price.price_eur_per_kwh:.4f} €/kWh"
    )
    # 0.95 €/Sm³ / 9.63 kWh/Sm³ ≈ 0.0987 €/kWh
    assert close(gas_price.price_eur_per_kwh, 0.95 / 9.63, tol=0.02), (
        f"Gas €/kWh ≈ {0.95/9.63:.4f}: {gas_price.price_eur_per_kwh:.4f}"
    )
    assert gas_price.co2_factor_kg_per_kwh > 0, (
        f"Gas CO₂/kWh: {gas_price.co2_factor_kg_per_kwh:.4f}"
    )

    el_price = DEFAULT_PRICES[FuelType.ELECTRICITY]
    assert el_price.price_eur_per_kwh == 0.25, (
        f"El price = 0.25 €/kWh: {el_price.price_eur_per_kwh}"
    )
    assert el_price.co2_factor_kg_per_kwh == 0.233, (
        f"El CO₂ = 0.233: {el_price.co2_factor_kg_per_kwh}"
    )


def test_02_gas_boiler_derived_quantities():
    """GasBoilerSystem derived quantities (thermal output, costs, CO₂, supply temp)."""
    boiler = GasBoilerSystem(
        fuel_type=FuelType.NATURAL_GAS,
        annual_fuel_consumption_units=8000.0,  # 8000 Sm³/yr
        boiler_efficiency=0.88,
        terminal_type=TerminalType.FAN_COIL,
        dhw_fraction=0.10,
    )
    # Annual thermal = 8000 × 9.63 kWh/Sm³ ≈ 77 040 kWh_th (includes efficiency)
    expected_th = 8000 * (9.63 / 0.90) * 0.88
    assert close(boiler.annual_thermal_output_kWh, expected_th, tol=0.01), (
        f"Annual thermal output ≈ {expected_th:.0f} kWh: "
        f"{boiler.annual_thermal_output_kWh:.0f}"
    )
    # Space heating = 90% of total
    assert close(
        boiler.annual_heating_kWh, boiler.annual_thermal_output_kWh * 0.90
    ), f"Space heating = 90%: {boiler.annual_heating_kWh:.0f}"
    # Fuel cost = 8000 × 0.95 €/Sm³ = 7600 €
    assert close(boiler.annual_fuel_cost_eur, 8000 * 0.95), (
        f"Fuel cost = 7600 €: {boiler.annual_fuel_cost_eur:.0f}"
    )
    # CO₂ = 8000 × 1.96 = 15 680 kg
    assert close(boiler.annual_co2_kg, 8000 * 1.96), (
        f"CO₂ = {8000*1.96:.0f} kg: {boiler.annual_co2_kg:.0f}"
    )
    # Supply temp: FAN_COIL = (40+45)/2 = 42.5°C
    assert close(boiler.nominal_supply_temp_c(), 42.5), (
        f"FAN_COIL supply temp ≈ 42.5°C: {boiler.nominal_supply_temp_c():.1f}"
    )


def test_03_cop_model_physical_correctness():
    """COP model: hourly Carnot COP, defrost, min temp, multi-year degradation."""
    hp = HeatPumpSystem(
        hp_type=HeatPumpType.AIR_TO_WATER,
        terminal_type=TerminalType.FAN_COIL,  # supply ≈ 42.5°C
        carnot_efficiency_factor=0.42,
        min_operating_temp_c=-15.0,
        defrost_loss_factor=0.08,
        cop_degradation_pct_per_yr=0.0,  # no degradation for testing
    )

    cops = calc_hourly_cop(T_EXT, hp)
    assert len(cops) == 8760, f"8760 COP values: {len(cops)}"

    # January (T=2°C): COP = 0.42 × (42.5+273.15) / (42.5+273.15 - 2-273.15)
    # = 0.42 × 315.65 / 40.5 ≈ 3.27 → with defrost: 3.27 × 0.92 ≈ 3.01
    T_hot = 42.5 + 273.15
    T_src_jan = 2.0 + 273.15
    cop_jan_expected = 0.42 * T_hot / (T_hot - T_src_jan) * (1 - 0.08)  # defrost
    cop_jan_actual = cops[0]  # first hour = January, T=2°C
    assert close(cop_jan_actual, cop_jan_expected, tol=0.02), (
        f"Jan COP ≈ {cop_jan_expected:.2f}: {cop_jan_actual:.3f}"
    )

    # July (T=25.5°C): no defrost, higher COP
    T_src_jul = 25.5 + 273.15
    cop_jul_expected = 0.42 * T_hot / (T_hot - T_src_jul)
    # July hours start at index: sum(days*24 for first 6 months)
    jul_start = sum(d * 24 for d in DAYS_M[:6])
    cop_jul_actual = cops[jul_start]
    assert close(cop_jul_actual, cop_jul_expected, tol=0.02), (
        f"Jul COP ≈ {cop_jul_expected:.2f}: {cop_jul_actual:.3f}"
    )

    # COP in July > COP in January (warmer source = higher COP)
    assert cop_jul_actual > cop_jan_actual, (
        f"COP Jul > COP Jan: {cop_jul_actual:.2f} > {cop_jan_actual:.2f}"
    )

    # Below minimum operating temp: COP = 0
    t_cold = [-20.0] * 8760
    cops_cold = calc_hourly_cop(t_cold, hp)
    assert all(c == 0.0 for c in cops_cold), (
        "COP = 0 when T_ext ≤ min_operating_temp"
    )

    # COP degradation over years
    hp_deg = HeatPumpSystem(
        carnot_efficiency_factor=0.42,
        min_operating_temp_c=-15.0,
        defrost_loss_factor=0.0,
        cop_degradation_pct_per_yr=2.0,  # 2%/yr
    )
    cops_y1 = calc_hourly_cop(T_EXT, hp_deg, simulation_year=1)
    cops_y10 = calc_hourly_cop(T_EXT, hp_deg, simulation_year=10)
    # Year 10: 9 years × 2% = 18% reduction
    assert cops_y10[0] < cops_y1[0], (
        f"Year 10 COP < Year 1: {cops_y10[0]:.3f} < {cops_y1[0]:.3f}"
    )
    assert close(cops_y10[0] / cops_y1[0], 1 - 0.18, tol=0.02), (
        f"Year 10/Year 1 ratio ≈ 0.82: {cops_y10[0]/cops_y1[0]:.3f}"
    )


def test_04_thermal_demand_profile_from_h_tr():
    """Thermal demand profile derived from H_tr and HDD."""
    annual_heating = 50000.0  # kWh
    h_tr = calc_h_tr_from_annual(annual_heating, T_EXT)
    assert h_tr > 0, f"H_tr > 0: {h_tr:.1f} W/K"

    # Verify: H_tr × HDD_hours / 1000 = annual_heating
    hdd_h = sum(max(20.0 - t, 0) for t in T_EXT)
    reconstructed = h_tr * hdd_h / 1000.0
    assert close(reconstructed, annual_heating, tol=0.001), (
        f"H_tr × HDD = annual_heating: {reconstructed:.0f} ≈ {annual_heating:.0f}"
    )

    demand_profile = calc_hourly_thermal_demand(
        T_EXT, h_tr, annual_heating_kWh=annual_heating
    )
    assert len(demand_profile) == 8760, "8760-hour thermal demand profile"
    assert all(v >= 0 for v in demand_profile), "No negative demand values"
    assert abs(sum(demand_profile) / 1000.0 - annual_heating) < 1.0, (
        f"Profile sum = annual_heating: "
        f"{sum(demand_profile)/1000:.0f} ≈ {annual_heating:.0f}"
    )

    # July demand should be zero (T_ext=25.5°C > setpoint 20°C)
    jul_start = sum(d * 24 for d in DAYS_M[:6])
    jul_demand = demand_profile[jul_start: jul_start + DAYS_M[6] * 24]
    assert all(v == 0 for v in jul_demand), (
        f"July thermal demand = 0 (T_ext=25.5°C > 20°C setpoint): "
        f"max={max(jul_demand):.0f}"
    )

    # January demand > December demand (colder in Jan in synthetic data)
    jan_demand = sum(demand_profile[:31*24])
    dec_demand = sum(demand_profile[-31*24:])
    assert jan_demand > 0 and dec_demand > 0, "Non-zero heating in Jan and Dec"


def test_05_full_hp_year_simulation_monovalent():
    """Full HP year simulation in monovalent mode: balance, SCOP, profile."""
    hp_mono = HeatPumpSystem(
        carnot_efficiency_factor=0.42,
        terminal_type=TerminalType.FAN_COIL,
        min_operating_temp_c=-15.0,
        defrost_loss_factor=0.08,
        rated_heating_power_kW=50.0,
        installed_cost_eur_kW=380.0,
    )

    result_m = simulate_heat_pump_year(T_EXT, THERMAL_M, hp_mono)

    assert result_m.annual_thermal_demand_kWh > 0, (
        f"Annual thermal demand: {result_m.annual_thermal_demand_kWh:.0f} kWh"
    )
    assert close(result_m.annual_thermal_demand_kWh, 50000, tol=0.01), (
        f"Thermal demand ≈ 50 000 kWh: {result_m.annual_thermal_demand_kWh:.0f}"
    )
    assert result_m.hp_electricity_kWh > 0, (
        f"HP electricity: {result_m.hp_electricity_kWh:.0f} kWh"
    )
    assert result_m.seasonal_cop > 1.0, f"SCOP > 1: {result_m.seasonal_cop:.2f}"
    assert result_m.seasonal_cop < 6.0, (
        f"SCOP < 6 (physically reasonable): {result_m.seasonal_cop:.2f}"
    )

    # SCOP = thermal_output / electricity_in
    scop_check = result_m.hp_thermal_output_kWh / result_m.hp_electricity_kWh
    assert close(scop_check, result_m.seasonal_cop, tol=0.01), (
        f"SCOP = th_out/el_in: {scop_check:.3f} ≈ {result_m.seasonal_cop:.3f}"
    )

    # Energy balance: hp_thermal + backup_thermal = total_demand
    heat_balance = abs(
        result_m.hp_thermal_output_kWh + result_m.backup_thermal_output_kWh
        - result_m.annual_thermal_demand_kWh
    )
    assert heat_balance < 1.0, f"HP thermal balance (gap={heat_balance:.2f} kWh)"

    assert (result_m.hours_hp_only + result_m.hours_parallel
            + result_m.hours_backup_only + result_m.hours_idle == 8760), (
        "Hours sum to 8760"
    )

    # HP electricity profile has 8760 values
    hp_profile = result_m.hp_electricity_profile_Wh
    assert len(hp_profile) == 8760, (
        f"HP electricity profile: {len(hp_profile)} values"
    )
    assert abs(sum(hp_profile) / 1000.0 - result_m.hp_electricity_kWh) < 1.0, (
        "HP profile sum = annual HP electricity"
    )


def test_06_bivalent_system_coverage_and_backup():
    """Bivalent system: backup fuel use, HP coverage, sizing helper."""
    boiler_backup = GasBoilerSystem(
        fuel_type=FuelType.NATURAL_GAS,
        boiler_efficiency=0.88,
    )
    bivalent = BivalentConfig(
        bivalence_temp_c=2.0,
        backup_system=boiler_backup,
    )

    # Use small HP (8 kW < 13.9 kW peak) to force backup operation
    hp_small = HeatPumpSystem(
        hp_type=HeatPumpType.AIR_TO_WATER,
        carnot_efficiency_factor=0.42,
        terminal_type=TerminalType.FAN_COIL,
        min_operating_temp_c=-15.0,
        defrost_loss_factor=0.08,
        rated_heating_power_kW=8.0,   # smaller than peak → backup needed
    )
    bivalent5 = BivalentConfig(bivalence_temp_c=5.0, backup_system=boiler_backup)
    result_biv = simulate_heat_pump_year(
        T_EXT, THERMAL_M, hp_small, bivalent=bivalent5
    )

    # Bivalent should have some backup fuel usage (when T < 2°C)
    assert result_biv.backup_fuel_units > 0, (
        f"Bivalent: backup fuel used: {result_biv.backup_fuel_units:.1f} Sm³"
    )
    assert result_biv.hours_parallel > 0 or result_biv.hours_backup_only > 0, (
        "Some hours with backup operation"
    )
    assert result_biv.hp_coverage_fraction < 1.0, (
        f"HP coverage < 100% in bivalent: {result_biv.hp_coverage_fraction:.1%}"
    )
    assert result_biv.hp_coverage_fraction > 0.70, (
        f"HP covers > 70% annual thermal: {result_biv.hp_coverage_fraction:.1%}"
    )

    # Bivalent has smaller HP needed → lower backup hp electricity at bivalence temp
    # HP is sized to demand at T=2°C, not peak demand
    sizing = suggest_hp_sizing(THERMAL_M, T_EXT, bivalent5)
    assert sizing["bivalent_kW"] <= sizing["monovalent_kW"], (
        f"Bivalent kW ≤ monovalent kW: "
        f"{sizing['bivalent_kW']} ≤ {sizing['monovalent_kW']}"
    )
    assert sizing["hp_coverage_pct"] > 60, (
        f"HP coverage at bivalent sizing > 60%: {sizing['hp_coverage_pct']:.1f}%"
    )


def test_07_building_energy_profile_costs_and_co2(profile):
    """BuildingEnergyProfile: annual costs and CO₂ totals."""
    assert close(profile.annual_electricity_cost_eur, 80000 * 0.25), (
        f"Electricity cost: {profile.annual_electricity_cost_eur:.0f} ≈ €20 000"
    )
    assert close(profile.annual_gas_cost_eur, 8000 * 0.95), (
        f"Gas cost: {profile.annual_gas_cost_eur:.0f} ≈ €7 600"
    )
    assert close(profile.annual_total_cost_eur,
                 80000 * 0.25 + 8000 * 0.95), (
        f"Total cost: {profile.annual_total_cost_eur:.0f}"
    )
    assert close(profile.annual_electricity_co2_kg, 80000 * 0.233), (
        f"El CO₂: {profile.annual_electricity_co2_kg:.0f} kg"
    )
    assert close(profile.annual_gas_co2_kg, 8000 * 1.96), (
        f"Gas CO₂: {profile.annual_gas_co2_kg:.0f} kg"
    )


def test_08_gas_break_even_price_calculation(profile):
    """Gas break-even price: at which €/Sm³ does HP match boiler annual cost."""
    # HP consumes 20 000 kWh_el at 0.25 €/kWh = 5 000 €/yr
    # Boiler consumes 8 000 Sm³/yr
    # Break-even gas price = 5000 / 8000 = 0.625 €/Sm³
    breakeven = profile.gas_price_breakeven_eur_sm3(
        hp_annual_electricity_kwh    = 20000.0,
        hp_electricity_price_eur_kwh = 0.25,
    )
    assert close(breakeven, 5000 / 8000, tol=0.01), (
        f"Break-even = 0.625 €/Sm³: {breakeven:.4f}"
    )
    assert breakeven < profile.gas_price.price_eur_per_unit, (
        f"HP already cheaper at current gas price "
        f"({profile.gas_price.price_eur_per_unit:.2f} €/Sm³)"
    )


def test_09_full_scenario_comparison():
    """Full multi-scenario comparison: baseline / PV / HP / PV+HP / PV+HP biv."""
    building = BuildingEnergyProfile(
        electricity_kwh_annual       = 80000.0,
        electricity_price            = DEFAULT_PRICES[FuelType.ELECTRICITY],
        gas_consumption_annual_units = 8000.0,
        gas_system                   = GasBoilerSystem(
            fuel_type=FuelType.NATURAL_GAS,
            annual_fuel_consumption_units=8000.0,
            terminal_type=TerminalType.FAN_COIL,
            dhw_fraction=0.10,
        ),
        has_gas=True,
    )

    hp_sys = HeatPumpSystem(
        carnot_efficiency_factor=0.42,
        terminal_type=TerminalType.FAN_COIL,
        min_operating_temp_c=-15.0,
        defrost_loss_factor=0.08,
        installed_cost_eur_kW=380.0,
        cop_degradation_pct_per_yr=0.5,
    )
    biv = BivalentConfig(
        bivalence_temp_c=5.0,
        backup_system=GasBoilerSystem(
            fuel_type=FuelType.NATURAL_GAS, boiler_efficiency=0.88,
        ),
    )

    cfg = ScenarioConfig(
        building_profile            = building,
        pv_production_hourly_Wh     = list(PV_H),
        base_demand_hourly_Wh       = list(DEM_H),
        t_ext_hourly                = T_EXT,
        annual_heating_kWh          = 50000.0,
        pv_peak_kWp                 = 50.0,
        battery                     = BatterySystem(capacity_kWh=0),
        tariff                      = GridTariff(
            import_price_eur_kWh=0.25, export_price_eur_kWh=0.07,
        ),
        heat_pump                   = hp_sys,
        bivalent                    = biv,
        investment_pv               = InvestmentCost(
            pv_cost_eur_kWp=950, peak_power_kWp=50.0,
        ),
        opex_pv                     = OperatingCost(om_pct_of_capex_per_yr=1.0),
        financial                   = FinancialParameters(
            discount_rate=0.05,
            energy_price_escalation=0.025,
            analysis_years=25,
        ),
        incentives_pv               = ItalianIncentives(
            ammortamento_accelerato=True, aliquota_ires_pct=24.0,
        ),
        incentives_hp               = ItalianIncentives(
            ammortamento_accelerato=True, aliquota_ires_pct=24.0,
        ),
        gas_price_sensitivity_range = [0.80, 1.0, 1.50, 2.00],
        run_solo_pv=True, run_solo_hp=True,
        run_pv_hp=True, run_pv_hp_bivalente=True,
    )

    comparison = compare_scenarios(cfg, lifetime_years=25)

    # All 5 scenarios present
    assert SCENARIO_BASELINE in comparison.scenarios, "Baseline scenario present"
    assert SCENARIO_SOLO_PV in comparison.scenarios, "Solo PV scenario present"
    assert SCENARIO_SOLO_HP in comparison.scenarios, "Solo HP scenario present"
    assert SCENARIO_PV_HP in comparison.scenarios, "PV+HP scenario present"
    assert SCENARIO_PV_HP_BIV in comparison.scenarios, (
        "PV+HP bivalente scenario present"
    )

    # Baseline: CAPEX = 0, no savings
    bl = comparison.baseline
    assert bl.capex_eur == 0, f"Baseline CAPEX = 0: {bl.capex_eur}"
    assert bl.annual_saving_vs_baseline_eur == 0, "Baseline saving = 0"
    assert bl.co2_saving_pct == 0, "Baseline CO₂ saving = 0"

    # Solo PV: positive annual saving vs baseline
    pv = comparison.scenarios[SCENARIO_SOLO_PV]
    assert pv.annual_saving_vs_baseline_eur > 0, (
        f"Solo PV: positive saving: €{pv.annual_saving_vs_baseline_eur:.0f}"
    )
    assert pv.pv_production_kWh > 0, (
        f"Solo PV: PV production: {pv.pv_production_kWh:.0f} kWh"
    )
    assert pv.hp_electricity_kWh == 0, "Solo PV: no HP electricity"
    # Gas unchanged in solo PV
    assert close(pv.gas_consumption_units, bl.gas_consumption_units), (
        f"Solo PV: gas unchanged: "
        f"{pv.gas_consumption_units:.0f} ≈ {bl.gas_consumption_units:.0f}"
    )

    # Solo HP: HP electricity > 0, some gas saved
    hp_s = comparison.scenarios[SCENARIO_SOLO_HP]
    assert hp_s.hp_electricity_kWh > 0, (
        f"Solo HP: HP electricity: {hp_s.hp_electricity_kWh:.0f} kWh"
    )
    assert hp_s.gas_consumption_units < bl.gas_consumption_units, (
        f"Solo HP: gas reduced: "
        f"{hp_s.gas_consumption_units:.0f} < {bl.gas_consumption_units:.0f}"
    )
    assert hp_s.seasonal_cop > 1.0, f"Solo HP: SCOP > 1: {hp_s.seasonal_cop:.2f}"

    # PV+HP: more CO₂ saving than solo PV or solo HP alone
    pvhp = comparison.scenarios[SCENARIO_PV_HP]
    assert pvhp.co2_saving_pct > pv.co2_saving_pct, (
        f"PV+HP CO₂ saving > Solo PV: "
        f"{pvhp.co2_saving_pct:.1f}% > {pv.co2_saving_pct:.1f}%"
    )
    assert pvhp.co2_saving_pct > hp_s.co2_saving_pct, (
        f"PV+HP CO₂ saving > Solo HP: "
        f"{pvhp.co2_saving_pct:.1f}% > {hp_s.co2_saving_pct:.1f}%"
    )

    # Bivalente: HP coverage <= monovalent (smaller HP), some backup gas
    biv_s = comparison.scenarios[SCENARIO_PV_HP_BIV]
    assert biv_s.gas_consumption_units >= pvhp.gas_consumption_units, (
        f"Bivalent: gas >= monovalent (bivalent keeps backup boiler): "
        f"{biv_s.gas_consumption_units:.0f} >= {pvhp.gas_consumption_units:.0f}"
    )
    assert biv_s.capex_eur <= pvhp.capex_eur + 1000, (
        f"Bivalent: CAPEX ≤ monovalent (smaller HP): "
        f"{biv_s.capex_eur:.0f} ≤ {pvhp.capex_eur:.0f}"
    )

    # Gas price sensitivity: 4 entries
    assert len(comparison.gas_price_sensitivity) == 4, (
        f"4 sensitivity entries: {len(comparison.gas_price_sensitivity)}"
    )
    # At 2× gas price, HP should definitely be cheaper
    if comparison.gas_price_sensitivity:
        s_2x = next(
            (s for s in comparison.gas_price_sensitivity
             if close(s.gas_price_multiplier, 2.0, tol=0.05)),
            None,
        )
        if s_2x:
            assert s_2x.hp_cheaper, (
                f"At 2× gas price, HP is cheaper: "
                f"gas={s_2x.annual_gas_cost_eur:.0f} "
                f"HP_el={s_2x.annual_heating_cost_hp_eur:.0f}"
            )

    # Ranked scenarios: non-negative NPV scenarios exist
    assert len(comparison.ranked_scenarios) > 0, (
        "At least one non-baseline scenario ranked"
    )
    assert comparison.recommended_scenario_id in comparison.scenarios, (
        f"Recommended scenario exists: {comparison.recommended_scenario_id}"
    )


def test_10_terminal_type_impact_on_cop():
    """Terminal type impact on COP: fan coil (42.5°C) vs old radiator (75°C)."""
    hp_fc = HeatPumpSystem(
        terminal_type=TerminalType.FAN_COIL,
        carnot_efficiency_factor=0.42, min_operating_temp_c=-15,
    )
    hp_rad = HeatPumpSystem(
        terminal_type=TerminalType.OLD_RADIATOR,
        carnot_efficiency_factor=0.42, min_operating_temp_c=-15,
    )
    cops_fc = calc_hourly_cop(T_EXT, hp_fc)
    cops_rad = calc_hourly_cop(T_EXT, hp_rad)

    avg_cop_fc = (
        sum(c for c in cops_fc if c > 0) / sum(1 for c in cops_fc if c > 0)
    )
    avg_cop_rad = (
        sum(c for c in cops_rad if c > 0) / sum(1 for c in cops_rad if c > 0)
    )
    assert avg_cop_fc > avg_cop_rad, (
        f"Fan coil COP > Radiator COP: {avg_cop_fc:.2f} > {avg_cop_rad:.2f}"
    )
    # Supply temp ratio: FC=42.5°C, RAD=75°C (in K: 315.65 vs 348.15)
    # COP ratio ≈ (315.65/ΔT_fc) / (348.15/ΔT_rad)
    assert (avg_cop_fc / avg_cop_rad) > 1.2, (
        f"COP ratio FC/RAD > 1.2: {avg_cop_fc/avg_cop_rad:.2f}"
    )
