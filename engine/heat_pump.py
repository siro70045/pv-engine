"""
engine/heat_pump.py

Hourly heat pump simulation: COP, thermal demand, bivalent dispatch.

This module is the physical core of the gas/HP comparison. Everything is
computed at 1-hour resolution using the TMY outdoor temperature profile,
giving a physically accurate picture of how the heat pump interacts with
the building's thermal load and the PV production profile.

Key models
----------
COP model (hourly Carnot with correction)
    COP(h) = η × T_hot_K / (T_hot_K − max(T_ext(h), T_min_K))
    
    Source: derived from Carnot efficiency for vapour-compression cycles.
    η (Carnot efficiency factor) calibrated from EN 14825 seasonal data.
    This is the approach used by PHPP, TABULA, and EN 15316-4-2.
    Accuracy: ±10% on annual electricity consumption vs manufacturer curves.
    Better than monthly-average COP: correctly captures the worst-case hours
    (cold nights) without averaging them away.

    Defrost penalty: applied when T_ext < 5°C (frost on evaporator).
    COP_effective(h) = COP(h) × (1 − defrost_factor)  for T_ext < 5°C

Thermal demand (hourly from H_tr)
    Q_heat(h) = max(H_tr × (T_set − T_ext(h)), 0)   [W]
    
    H_tr is the total heat transfer coefficient [W/K], calculated from
    the monthly demand if the full envelope model is not available:
        H_tr = annual_heating_kWh × 1000 / sum(max(T_set-T_ext_h, 0) for all h)
    
    This is equivalent to the "quasi-steady" approach of EN ISO 13790.

Bivalent dispatch
    Three zones determined by outdoor temperature:
    - HP-only zone: T_ext > T_bivalence
    - Parallel zone: T_min_hp ≤ T_ext ≤ T_bivalence (HP at full capacity + boiler tops up)
    - Boiler-only zone: T_ext < T_min_hp
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine.energy_vectors import (
    HeatPumpSystem, GasBoilerSystem, BivalentConfig,
    FuelType, DEFAULT_PRICES, TERMINAL_SUPPLY_TEMP_C,
)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

_CELSIUS_TO_KELVIN = 273.15
_DEFROST_THRESHOLD_C = 5.0      # °C: defrost cycles start below this temperature
_HEATING_SETPOINT_C  = 20.0     # °C: default indoor setpoint
_HOURS_PER_YEAR      = 8760


# ══════════════════════════════════════════════════════════════════════════════
# HOURLY COP
# ══════════════════════════════════════════════════════════════════════════════

def calc_hourly_cop(
    t_ext_hourly: list[float],
    hp: HeatPumpSystem,
    simulation_year: int = 1,
    t_hot_override_c: Optional[float] = None,
) -> list[float]:
    """
    Calculate heat pump COP for each of the 8 760 hours.

    Model: Carnot with correction factor and defrost penalty.

        COP(h) = η × T_hot_K / (T_hot_K − T_source_K(h))
        COP_eff(h) = COP(h) × (1 − defrost_factor)  if T_ext(h) < 5°C
        COP_eff(h) = 0                                if T_ext(h) < T_min_hp

    Parameters
    ----------
    t_ext_hourly : list[float]
        8 760 hourly outdoor temperatures [°C] from TMY.
    hp : HeatPumpSystem
        Heat pump specification.
    simulation_year : int
        Year (1-based) for applying COP degradation.
    t_hot_override_c : float, optional
        Override HP supply temperature [°C]. If None, uses hp.supply_temp_c.

    Returns
    -------
    list[float]
        8 760 hourly COP values. 0.0 means HP is off (too cold or below minimum).

    Notes
    -----
    The Carnot correction factor η is empirically calibrated:
        - Typical air-to-water A++ class: η ≈ 0.42
        - Typical air-to-water A+++ class: η ≈ 0.46
        - Ground-to-water: η ≈ 0.55 (more stable source temperature)
    These values reproduce manufacturer seasonal COP data within ±10%.
    """
    if len(t_ext_hourly) != _HOURS_PER_YEAR:
        raise ValueError(
            f"t_ext_hourly must have {_HOURS_PER_YEAR} values, got {len(t_ext_hourly)}."
        )

    T_hot_C = t_hot_override_c if t_hot_override_c is not None else hp.supply_temp_c
    T_hot_K = T_hot_C + _CELSIUS_TO_KELVIN

    eta = hp.carnot_efficiency_factor
    defrost_factor = hp.defrost_loss_factor
    T_min_C = hp.min_operating_temp_c
    T_min_K = T_min_C + _CELSIUS_TO_KELVIN

    # COP degradation: linear decay per year
    deg_factor = max(1.0 - hp.cop_degradation_pct_per_yr / 100.0 * (simulation_year - 1), 0.5)

    cops: list[float] = []
    for T_ext_C in t_ext_hourly:
        if T_ext_C <= T_min_C:
            cops.append(0.0)
            continue

        T_source_K = max(T_ext_C, T_min_C) + _CELSIUS_TO_KELVIN
        delta = T_hot_K - T_source_K
        if delta <= 0:
            # Source hotter than supply — unlikely in heating mode but guard
            cops.append(0.0)
            continue

        cop = eta * T_hot_K / delta

        # Defrost penalty when T_ext is near freezing
        if T_ext_C < _DEFROST_THRESHOLD_C:
            cop *= (1.0 - defrost_factor)

        # Physical upper bound: COP cannot exceed Carnot
        cop_carnot = T_hot_K / max(delta, 0.01)
        cop = min(cop, cop_carnot)

        # Apply ageing
        cop *= deg_factor

        cops.append(round(cop, 3))

    return cops


# ══════════════════════════════════════════════════════════════════════════════
# HOURLY THERMAL DEMAND
# ══════════════════════════════════════════════════════════════════════════════

def calc_h_tr_from_annual(
    annual_heating_kWh: float,
    t_ext_hourly: list[float],
    t_set_c: float = _HEATING_SETPOINT_C,
) -> float:
    """
    Estimate the building heat transfer coefficient H_tr [W/K] from annual data.

    H_tr is the proportionality constant between indoor-outdoor temperature
    difference and hourly heat loss: Q_loss(h) = H_tr × (T_set − T_ext(h)).

    Inversion:
        annual_heating_kWh × 1000 = H_tr × Σ max(T_set − T_ext(h), 0)
        → H_tr = annual_heating_kWh × 1000 / HDD_hours

    where HDD_hours = Σ max(T_set − T_ext(h), 0)  [°C·h/yr]

    Parameters
    ----------
    annual_heating_kWh : float
        Annual space heating demand [kWh/yr].
    t_ext_hourly : list[float]
        8 760 hourly outdoor temperatures [°C].
    t_set_c : float
        Indoor heating setpoint [°C]. Default 20°C.

    Returns
    -------
    float
        H_tr [W/K].
    """
    hdd_hours = sum(max(t_set_c - t, 0.0) for t in t_ext_hourly)
    if hdd_hours <= 0 or annual_heating_kWh <= 0:
        return 0.0
    return (annual_heating_kWh * 1000.0) / hdd_hours


def calc_hourly_thermal_demand(
    t_ext_hourly: list[float],
    h_tr: float,
    annual_heating_kWh: float = 0.0,
    t_set_c: float = _HEATING_SETPOINT_C,
    occupancy_schedule: Optional[list[float]] = None,
) -> list[float]:
    """
    Calculate the hourly space heating thermal demand [Wh/h = W].

    Q_heat(h) = max(H_tr × (T_set − T_ext(h)), 0) × occupancy_factor(h)

    Normalisation: after computing the raw profile, it is scaled so the annual
    sum matches annual_heating_kWh exactly (eliminates floating-point drift).

    Parameters
    ----------
    t_ext_hourly : list[float]
        8 760 hourly outdoor temperatures [°C].
    h_tr : float
        Building heat transfer coefficient [W/K].
    annual_heating_kWh : float
        Target annual heating demand [kWh/yr].
        If > 0, the profile is normalised to this total.
    t_set_c : float
        Indoor heating setpoint [°C].
    occupancy_schedule : list[float], optional
        8 760 hourly occupancy factors [0–1] that modulate setpoint.
        If None, constant setpoint is used (no setback).
        Useful for modelling night setback (0.5 at night = 18°C effective setpoint).

    Returns
    -------
    list[float]
        8 760 hourly heating demand [Wh/h]. Never negative.
    """
    if len(t_ext_hourly) != _HOURS_PER_YEAR:
        raise ValueError(f"t_ext_hourly must have {_HOURS_PER_YEAR} values.")

    profile: list[float] = []
    for i, t_ext in enumerate(t_ext_hourly):
        occ = occupancy_schedule[i] if occupancy_schedule else 1.0
        # Setback: occ < 1 reduces effective setpoint proportionally
        t_set_eff = t_set_c - (1.0 - occ) * 4.0  # 4°C setback at occ=0
        demand_W = max(h_tr * (t_set_eff - t_ext), 0.0)
        profile.append(demand_W)

    # Normalise to annual total if provided
    total_kwh = sum(profile) / 1000.0
    if annual_heating_kWh > 0 and total_kwh > 0:
        scale = annual_heating_kWh / total_kwh
        profile = [v * scale for v in profile]

    return profile


# ══════════════════════════════════════════════════════════════════════════════
# BIVALENT DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HourlyHeatPumpRecord:
    """Energy flows for one hour of HP + backup operation."""
    t_ext_c: float
    thermal_demand_Wh: float         # building heating demand [Wh]
    cop: float                       # HP COP this hour (0 = HP off)
    hp_thermal_output_Wh: float      # heat from HP [Wh]
    hp_electricity_Wh: float         # electricity consumed by HP [Wh]
    backup_thermal_output_Wh: float  # heat from backup boiler [Wh]
    backup_fuel_units: float         # fuel consumed by backup (Sm³ for gas)
    zone: str                        # "hp_only", "parallel", "backup_only", "idle"


@dataclass
class HeatPumpYearResult:
    """Annual aggregated results of the heat pump simulation."""
    # Energy summary
    annual_thermal_demand_kWh: float
    hp_thermal_output_kWh: float
    hp_electricity_kWh: float
    backup_thermal_output_kWh: float
    backup_fuel_units: float
    backup_thermal_kWh: float

    # HP coverage
    hp_coverage_fraction: float      # hp_thermal / total_thermal
    avg_cop: float                   # weighted average COP (over running hours)
    seasonal_cop: float              # = hp_thermal_output / hp_electricity (SCOP)

    # Hours breakdown
    hours_hp_only: int
    hours_parallel: int
    hours_backup_only: int
    hours_idle: int

    # Economic
    hp_electricity_cost_eur: float
    backup_fuel_cost_eur: float
    total_heating_cost_eur: float

    # CO2
    hp_electricity_co2_kg: float
    backup_fuel_co2_kg: float
    total_co2_kg: float

    # Monthly breakdowns
    monthly_hp_electricity_kWh: list[float]   # 12 values
    monthly_backup_fuel_units: list[float]
    monthly_hp_thermal_kWh: list[float]
    monthly_thermal_demand_kWh: list[float]

    # Hourly records (for energy_balance integration)
    hourly: list[HourlyHeatPumpRecord]

    @property
    def hp_electricity_profile_Wh(self) -> list[float]:
        """8760-hour HP electricity demand [Wh/h] for energy_balance integration."""
        return [r.hp_electricity_Wh for r in self.hourly]


# ══════════════════════════════════════════════════════════════════════════════
# CORE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

_DAYS_PER_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def simulate_heat_pump_year(
    t_ext_hourly: list[float],
    thermal_demand_hourly_Wh: list[float],
    hp: HeatPumpSystem,
    bivalent: Optional[BivalentConfig] = None,
    electricity_price: Optional[object] = None,
    simulation_year: int = 1,
) -> HeatPumpYearResult:
    """
    Simulate one year of heat pump operation with optional bivalent backup.

    For each hour:
    1.  Determine operating zone from outdoor temperature.
    2.  Calculate HP COP.
    3.  Dispatch HP thermal output (limited by rated power and demand).
    4.  If demand unmet, dispatch backup boiler.
    5.  Record all flows.

    Parameters
    ----------
    t_ext_hourly : list[float]
        8 760 hourly outdoor temperatures [°C].
    thermal_demand_hourly_Wh : list[float]
        8 760 hourly space heating demand [Wh/h].
    hp : HeatPumpSystem
        Heat pump specification.
    bivalent : BivalentConfig, optional
        Bivalent system config. If None, HP runs alone (no backup).
    electricity_price : EnergyPrice, optional
        Electricity price for cost calculation.
    simulation_year : int
        Year number for HP COP degradation.

    Returns
    -------
    HeatPumpYearResult
    """
    from engine.energy_vectors import DEFAULT_PRICES, FuelType

    n = len(t_ext_hourly)
    if n != _HOURS_PER_YEAR or len(thermal_demand_hourly_Wh) != _HOURS_PER_YEAR:
        raise ValueError("Both hourly arrays must have exactly 8760 values.")

    # Determine HP rated power in Wh/h
    # If not specified, size to 95th percentile of demand (bivalent sizing)
    sorted_demand = sorted(thermal_demand_hourly_Wh, reverse=True)
    p95_demand_W = sorted_demand[int(0.05 * n)] if sorted_demand else 0.0

    if bivalent:
        # Bivalent sizing: HP covers load down to bivalence temperature
        # HP rated power ≈ demand at bivalence temperature
        bipoint_demand = [
            d for d, t in zip(thermal_demand_hourly_Wh, t_ext_hourly)
            if abs(t - bivalent.bivalence_temp_c) < 2.0
        ]
        hp_rated_W = (sum(bipoint_demand) / len(bipoint_demand)
                      if bipoint_demand else p95_demand_W * 0.7)
    else:
        # Monovalent: HP sized for full load
        hp_rated_W = p95_demand_W

    # Use explicit rated power if provided
    if hp.rated_heating_power_kW > 0:
        hp_rated_W = hp.rated_heating_power_kW * 1000.0

    # Pre-compute hourly COP
    cops = calc_hourly_cop(t_ext_hourly, hp, simulation_year)

    # Backup fuel properties
    backup_fuel_type = (bivalent.backup_system.fuel_type
                        if bivalent else FuelType.NATURAL_GAS)
    backup_eff = (bivalent.backup_system.boiler_efficiency
                  if bivalent else 0.88)
    backup_price_obj = DEFAULT_PRICES.get(backup_fuel_type,
                                           DEFAULT_PRICES[FuelType.NATURAL_GAS])
    backup_price_per_unit = backup_price_obj.price_eur_per_unit
    backup_co2_per_unit  = backup_price_obj.co2_factor_kg_per_unit
    _REF_EFF = 0.90
    backup_gross_kwh_per_unit  = backup_price_obj.unit_kwh_conversion / _REF_EFF
    backup_useful_kwh_per_unit = backup_gross_kwh_per_unit * backup_eff

    # Electricity price
    el_price_obj = (electricity_price or hp.hp_electricity_price
                    or DEFAULT_PRICES[FuelType.ELECTRICITY])
    el_price_kwh = el_price_obj.price_eur_per_kwh
    el_co2_kwh   = el_price_obj.co2_factor_kg_per_kwh

    # Bivalence thresholds
    t_biv   = bivalent.bivalence_temp_c if bivalent else -999.0
    t_min   = hp.min_operating_temp_c

    # Month index lookup
    hour_to_month: list[int] = []
    for m, days in enumerate(_DAYS_PER_MONTH):
        hour_to_month.extend([m] * (days * 24))

    # Accumulators
    hourly: list[HourlyHeatPumpRecord] = []
    monthly_hp_el   = [0.0] * 12
    monthly_backup  = [0.0] * 12
    monthly_hp_th   = [0.0] * 12
    monthly_dem_th  = [0.0] * 12

    total_hp_thermal = total_hp_el = total_backup_th = total_backup_fuel = 0.0
    hours_hp_only = hours_parallel = hours_backup_only = hours_idle = 0
    cop_sum_weighted = 0.0

    for i in range(n):
        t_ext = t_ext_hourly[i]
        demand_Wh = max(thermal_demand_hourly_Wh[i], 0.0)
        cop = cops[i]
        m = hour_to_month[i] if i < len(hour_to_month) else 11

        monthly_dem_th[m] += demand_Wh

        if demand_Wh == 0.0:
            # No heating demand — HP idle
            rec = HourlyHeatPumpRecord(
                t_ext_c=t_ext, thermal_demand_Wh=0.0, cop=0.0,
                hp_thermal_output_Wh=0.0, hp_electricity_Wh=0.0,
                backup_thermal_output_Wh=0.0, backup_fuel_units=0.0,
                zone="idle",
            )
            hours_idle += 1
            hourly.append(rec)
            continue

        # Determine operating zone
        hp_thermal_Wh = 0.0
        hp_el_Wh = 0.0
        backup_th_Wh = 0.0
        backup_fuel_u = 0.0
        zone = "idle"

        if cop <= 0 or t_ext <= t_min:
            # Boiler-only zone: HP cannot operate
            backup_th_Wh   = demand_Wh
            backup_fuel_u  = (demand_Wh / 1000.0) / backup_useful_kwh_per_unit
            zone = "backup_only"
            hours_backup_only += 1

        elif bivalent and t_ext <= t_biv:
            # Parallel zone: HP at max capacity, boiler covers remainder
            hp_cap_Wh    = min(hp_rated_W, demand_Wh)
            hp_thermal_Wh = hp_cap_Wh
            hp_el_Wh      = hp_thermal_Wh / cop if cop > 0 else 0.0
            remaining_Wh  = max(demand_Wh - hp_thermal_Wh, 0.0)
            backup_th_Wh  = remaining_Wh
            backup_fuel_u = (remaining_Wh / 1000.0) / backup_useful_kwh_per_unit
            zone = "parallel"
            hours_parallel += 1

        else:
            # HP-only zone
            hp_thermal_Wh = min(hp_rated_W, demand_Wh)
            hp_el_Wh      = hp_thermal_Wh / cop if cop > 0 else 0.0
            # Any unmet demand (HP undersized) → backup
            unmet_Wh = max(demand_Wh - hp_thermal_Wh, 0.0)
            if unmet_Wh > 0:
                backup_th_Wh  = unmet_Wh
                backup_fuel_u = (unmet_Wh / 1000.0) / backup_useful_kwh_per_unit
            zone = "hp_only"
            hours_hp_only += 1

        # Accumulate
        total_hp_thermal += hp_thermal_Wh
        total_hp_el      += hp_el_Wh
        total_backup_th  += backup_th_Wh
        total_backup_fuel+= backup_fuel_u
        if hp_thermal_Wh > 0 and cop > 0:
            cop_sum_weighted += cop * hp_thermal_Wh

        monthly_hp_el[m]  += hp_el_Wh
        monthly_backup[m] += backup_fuel_u
        monthly_hp_th[m]  += hp_thermal_Wh

        hourly.append(HourlyHeatPumpRecord(
            t_ext_c=t_ext,
            thermal_demand_Wh=demand_Wh,
            cop=cop,
            hp_thermal_output_Wh=hp_thermal_Wh,
            hp_electricity_Wh=hp_el_Wh,
            backup_thermal_output_Wh=backup_th_Wh,
            backup_fuel_units=backup_fuel_u,
            zone=zone,
        ))

    # Aggregate
    total_demand_kWh = sum(thermal_demand_hourly_Wh) / 1000.0
    hp_th_kWh    = total_hp_thermal / 1000.0
    hp_el_kWh    = total_hp_el      / 1000.0
    backup_th_kWh= total_backup_th  / 1000.0

    scop = hp_th_kWh / hp_el_kWh if hp_el_kWh > 0 else 0.0
    avg_cop = cop_sum_weighted / total_hp_thermal if total_hp_thermal > 0 else 0.0
    hp_cov  = hp_th_kWh / total_demand_kWh if total_demand_kWh > 0 else 0.0

    hp_el_cost    = hp_el_kWh    * el_price_kwh
    backup_cost   = total_backup_fuel * backup_price_per_unit
    total_cost    = hp_el_cost   + backup_cost

    hp_el_co2    = hp_el_kWh    * el_co2_kwh
    backup_co2   = total_backup_fuel * backup_co2_per_unit
    total_co2    = hp_el_co2    + backup_co2

    return HeatPumpYearResult(
        annual_thermal_demand_kWh   = round(total_demand_kWh, 1),
        hp_thermal_output_kWh       = round(hp_th_kWh, 1),
        hp_electricity_kWh          = round(hp_el_kWh, 1),
        backup_thermal_output_kWh   = round(backup_th_kWh, 1),
        backup_fuel_units           = round(total_backup_fuel, 1),
        backup_thermal_kWh          = round(backup_th_kWh, 1),
        hp_coverage_fraction        = round(hp_cov, 4),
        avg_cop                     = round(avg_cop, 3),
        seasonal_cop                = round(scop, 3),
        hours_hp_only               = hours_hp_only,
        hours_parallel              = hours_parallel,
        hours_backup_only           = hours_backup_only,
        hours_idle                  = hours_idle,
        hp_electricity_cost_eur     = round(hp_el_cost, 2),
        backup_fuel_cost_eur        = round(backup_cost, 2),
        total_heating_cost_eur      = round(total_cost, 2),
        hp_electricity_co2_kg       = round(hp_el_co2, 1),
        backup_fuel_co2_kg          = round(backup_co2, 1),
        total_co2_kg                = round(total_co2, 1),
        monthly_hp_electricity_kWh  = [round(v/1000, 1) for v in monthly_hp_el],
        monthly_backup_fuel_units   = [round(v, 2) for v in monthly_backup],
        monthly_hp_thermal_kWh      = [round(v/1000, 1) for v in monthly_hp_th],
        monthly_thermal_demand_kWh  = [round(v/1000, 1) for v in monthly_dem_th],
        hourly                      = hourly,
    )


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-SIZING HELPER
# ══════════════════════════════════════════════════════════════════════════════

def suggest_hp_sizing(
    thermal_demand_hourly_Wh: list[float],
    t_ext_hourly: list[float],
    bivalent: Optional[BivalentConfig] = None,
) -> dict:
    """
    Suggest heat pump rated power and bivalence temperature.

    Returns a dict with:
    - monovalent_kW   : HP sized to cover 100% of peak demand
    - bivalent_kW     : HP sized for bivalent operation at default T_bivalence
    - peak_demand_kW  : actual peak thermal demand
    - at_bivalence_kW : demand at the bivalence temperature
    - hp_coverage_pct : % of annual thermal energy covered at bivalent sizing
    """
    peak_W = max(thermal_demand_hourly_Wh)
    t_biv = bivalent.bivalence_temp_c if bivalent else 2.0

    # Demand at bivalence temperature (average of hours near T_biv ± 2°C)
    biv_hours = [
        d for d, t in zip(thermal_demand_hourly_Wh, t_ext_hourly)
        if abs(t - t_biv) < 2.0 and d > 0
    ]
    biv_kW = (sum(biv_hours) / len(biv_hours) / 1000.0) if biv_hours else peak_W / 1000.0

    # Coverage at bivalent sizing (hours where demand ≤ biv_kW × 1000)
    biv_W = biv_kW * 1000.0
    hp_th  = sum(min(d, biv_W) for d in thermal_demand_hourly_Wh)
    total_th = sum(thermal_demand_hourly_Wh)
    coverage = hp_th / total_th if total_th > 0 else 0.0

    return {
        "monovalent_kW":    round(peak_W / 1000.0, 1),
        "bivalent_kW":      round(biv_kW, 1),
        "peak_demand_kW":   round(peak_W / 1000.0, 1),
        "at_bivalence_kW":  round(biv_kW, 1),
        "hp_coverage_pct":  round(coverage * 100, 1),
    }
