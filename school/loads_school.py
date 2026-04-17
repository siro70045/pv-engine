"""
school/loads_school.py

School-specific energy loads model and PV simulation runner.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from school.calendar_it import (
    build_school_calendar,
    school_days_per_month,
    DAYS_PER_MONTH,
    DAYS_BEFORE_MONTH,
    _doy_to_month,
    _is_school_day,
    _dow,
)


# ══════════════════════════════════════════════════════════════════════════════
# INPUT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SchoolLoadsInput:
    # Edificio
    floor_area_m2: float = 3000.0
    n_floors: int = 3
    construction_year: int = 1981
    school_type: str = "high_tech"

    # Occupazione
    n_students: int = 500
    n_teachers: int = 40
    n_ata_staff: int = 15
    full_time: bool = False
    extra_evening: bool = False
    extra_weekend: bool = False

    # Ambienti speciali
    has_gym: bool = False
    has_canteen: bool = False
    has_it_lab: bool = False
    has_tech_lab: bool = False
    has_science_lab: bool = False
    has_auditorium: bool = False
    has_elevator: bool = False

    # Carichi specifici
    n_computers: int = 30
    it_lab_watts_per_pc: float = 120.0
    tech_lab_peak_kW: float = 0.0

    # HVAC
    heating_system: str = "gas_boiler"
    has_ac: bool = False
    ac_type: str = "none"

    # Illuminazione
    lighting_type: str = "fluorescent"

    # Bollette
    annual_bill_kwh: float = 0.0
    monthly_bill_kwh: list = field(default_factory=lambda: [0.0] * 12)


# ══════════════════════════════════════════════════════════════════════════════
# WEIGHT ARRAYS (8760 h) FOR CATEGORY-SPECIFIC LOAD SHAPES
# ══════════════════════════════════════════════════════════════════════════════

def _pc_lab_weights(school_type: str) -> np.ndarray:
    """School hours 8:00–14:00 on school days."""
    w = np.zeros(8760)
    for doy in range(365):
        if _is_school_day(doy, school_type):
            base = doy * 24
            for h in range(8, 14):
                w[base + h] = 1.0
    return w


def _ac_weights() -> np.ndarray:
    """June–August, 09:00–17:00."""
    w = np.zeros(8760)
    for doy in range(365):
        month = _doy_to_month(doy)
        if 6 <= month <= 8:
            base = doy * 24
            for h in range(9, 17):
                w[base + h] = 1.0
    return w


def _heating_weights() -> np.ndarray:
    """October–April, 06:00–18:00."""
    w = np.zeros(8760)
    for doy in range(365):
        month = _doy_to_month(doy)
        if month >= 10 or month <= 4:
            base = doy * 24
            for h in range(6, 18):
                w[base + h] = 1.0
    return w


def _canteen_weights(school_type: str) -> np.ndarray:
    """11:30–13:30 → partial hours 11, full 12, partial 13 — on school days."""
    w = np.zeros(8760)
    for doy in range(365):
        if _is_school_day(doy, school_type):
            base = doy * 24
            w[base + 11] = 0.5
            w[base + 12] = 1.0
            w[base + 13] = 0.5
    return w


# ══════════════════════════════════════════════════════════════════════════════
# LOADS CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def calc_school_loads(inp: SchoolLoadsInput) -> dict:
    """
    Calcola il profilo di carico elettrico orario di una scuola italiana.

    Returns
    -------
    dict with keys:
        hourly_demand_Wh      list[float] — 8760 values [Wh/h]
        total_annual_kwh      float
        breakdown             dict[str, float] — kWh per category
        scale_factor          float
        peak_demand_kW        float
        school_days_per_month list[int] — 12 values
        total_monthly_kwh     list[float] — 12 values
    """
    school_days = school_days_per_month(inp.school_type)
    total_school_days = sum(school_days)

    # ── Thermal need per m² depending on construction era ─────────────────────
    if inp.construction_year < 1973:
        heat_need_kwh_m2 = 120.0
    elif inp.construction_year < 1990:
        heat_need_kwh_m2 = 90.0
    elif inp.construction_year < 2010:
        heat_need_kwh_m2 = 60.0
    else:
        heat_need_kwh_m2 = 40.0

    # ── School day length (hours of active use) ────────────────────────────────
    if inp.school_type == "university_dept":
        day_hours = 11    # 08:00-19:00
    elif inp.school_type == "primary":
        day_hours = 8 if inp.full_time else 5
    elif inp.school_type == "high_tech":
        day_hours = 7     # average of 6h normal + 9h lab days (2/6 days)
    else:
        day_hours = 6     # middle, high_classic: 08:00-13:30

    # ── 1. Lighting ───────────────────────────────────────────────────────────
    lighting_w_m2 = {"led": 8.0, "fluorescent": 18.0, "mixed": 12.0}.get(
        inp.lighting_type, 12.0
    )
    light_hours_yr = total_school_days * day_hours * 0.7
    lighting_kwh   = lighting_w_m2 * inp.floor_area_m2 * light_hours_yr / 1000.0

    # ── 2. PC laboratories ────────────────────────────────────────────────────
    n_pc     = inp.n_computers if inp.has_it_lab else 0
    pc_kwh   = n_pc * inp.it_lab_watts_per_pc * (total_school_days * 6) / 1000.0

    # ── 3. Technical/workshop laboratory ─────────────────────────────────────
    tech_kwh = (inp.tech_lab_peak_kW * total_school_days * 4.0
                if inp.has_tech_lab else 0.0)

    # ── 4. Canteen (0.15 kWh/pasto, fonte ENEA) ───────────────────────────────
    canteen_kwh = (inp.n_students * 0.15 * total_school_days
                   if inp.has_canteen else 0.0)

    # ── 5. Gym ────────────────────────────────────────────────────────────────
    gym_kwh = 0.0
    if inp.has_gym:
        gym_kwh = (8000.0
                   + (4000.0 if inp.extra_evening else 0.0)
                   + (2000.0 if inp.extra_weekend else 0.0))

    # ── 6. Admin / ATA staff ─────────────────────────────────────────────────
    admin_kwh = inp.n_ata_staff * 500.0

    # ── 7. Elevator ───────────────────────────────────────────────────────────
    elevator_kwh = 2500.0 if inp.has_elevator else 0.0

    # ── 8. Summer cooling (AC) ────────────────────────────────────────────────
    ac_kwh = 0.0
    if inp.has_ac or inp.ac_type not in ("none", ""):
        if inp.ac_type == "split":
            ac_kwh = inp.floor_area_m2 * 8.0 * 60.0 / 1000.0
        elif inp.ac_type == "central":
            ac_kwh = inp.floor_area_m2 * 12.0 * 60.0 / 1000.0

    # ── 9. Heat pump (only if heating_system == "heat_pump") ─────────────────
    heating_kwh = 0.0
    if inp.heating_system == "heat_pump":
        scop = 3.2 if inp.ac_type in ("split", "none") else 2.8
        heating_kwh = inp.floor_area_m2 * heat_need_kwh_m2 / scop

    # ── 10. Miscellaneous (servers, copiers, AV equipment, etc.) ─────────────
    misc_kwh = inp.floor_area_m2 * 2.5

    # ── Breakdown and total ───────────────────────────────────────────────────
    breakdown = {
        "lighting":   round(lighting_kwh,  1),
        "computers":  round(pc_kwh,         1),
        "tech_lab":   round(tech_kwh,       1),
        "canteen":    round(canteen_kwh,    1),
        "gym":        round(gym_kwh,        1),
        "admin":      round(admin_kwh,      1),
        "ac":         round(ac_kwh,         1),
        "heating_hp": round(heating_kwh,    1),
        "elevator":   round(elevator_kwh,   1),
        "misc":       round(misc_kwh,       1),
    }
    total_estimated = sum(breakdown.values())

    # ── Scale factor (bill vs. model) ─────────────────────────────────────────
    bill_kwh = inp.annual_bill_kwh
    if bill_kwh <= 0.0 and any(v > 0 for v in inp.monthly_bill_kwh):
        bill_kwh = sum(inp.monthly_bill_kwh)

    scale_factor = (bill_kwh / total_estimated
                    if bill_kwh > 0 and total_estimated > 0
                    else 1.0)

    total_annual = total_estimated * scale_factor

    # ── Build 8760-hour profile ───────────────────────────────────────────────
    general_w  = build_school_calendar(
        inp.school_type, inp.extra_evening, inp.extra_weekend, inp.full_time
    )
    pc_w       = _pc_lab_weights(inp.school_type)
    ac_w       = _ac_weights()
    heat_w     = _heating_weights()
    canteen_w  = _canteen_weights(inp.school_type)
    gym_w      = general_w.copy() if inp.has_gym else np.zeros(8760)

    def _distribute(kwh: float, weights: np.ndarray) -> np.ndarray:
        w_sum = weights.sum()
        if kwh <= 0.0 or w_sum <= 0.0:
            return np.zeros(8760)
        return weights * (kwh * 1000.0 / w_sum)

    hourly_Wh = (
        _distribute(breakdown["lighting"],   general_w)
        + _distribute(breakdown["computers"],  pc_w)
        + _distribute(breakdown["tech_lab"],   pc_w)
        + _distribute(breakdown["canteen"],    canteen_w)
        + _distribute(breakdown["gym"],        gym_w)
        + _distribute(breakdown["admin"],      general_w)
        + _distribute(breakdown["elevator"],   general_w)
        + _distribute(breakdown["ac"],         ac_w)
        + _distribute(breakdown["heating_hp"], heat_w)
        + _distribute(breakdown["misc"],       general_w)
    )

    hourly_Wh *= scale_factor

    # ── Monthly totals ────────────────────────────────────────────────────────
    monthly_kwh = []
    for m in range(12):
        start = DAYS_BEFORE_MONTH[m] * 24
        end   = start + DAYS_PER_MONTH[m] * 24
        monthly_kwh.append(round(float(hourly_Wh[start:end].sum()) / 1000.0, 1))

    peak_kW = float(hourly_Wh.max()) / 1000.0

    return {
        "hourly_demand_Wh":      [round(float(v), 2) for v in hourly_Wh],
        "total_annual_kwh":      round(total_annual, 1),
        "breakdown":             breakdown,
        "scale_factor":          round(scale_factor, 4),
        "peak_demand_kW":        round(peak_kW, 2),
        "school_days_per_month": school_days,
        "total_monthly_kwh":     monthly_kwh,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SCHOOL PV SIMULATION RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run_simulation_school(
    tmy,
    school_loads: dict,
    floor_area_m2: float,
    kwp: float,
    azimuth_deg: float = 180.0,
    tilt_deg: float = 10.0,
    import_price: float = 0.22,
    export_price: float = 0.08,
    capex_eur: float = 0.0,
    structural_eur: float = 2000.0,
    pnrr_grant: float = 0.0,
    cer_incentive_eur_kwh: float = 0.0,
    cer_fraction: float = 0.0,
    lifetime_years: int = 25,
):
    """
    Run the full PV feasibility pipeline using the school hourly load profile.

    This bypasses main.py's calc_loads() and injects the school-specific
    8760-hour demand profile directly into EnergyBalanceInput.

    Returns a SimulationResult (same as run_simulation() in main.py).
    """
    from engine.loads import LoadsResult
    from engine.solar import PVSystem, SystemLosses, simulate_pv
    from engine.energy_balance import (
        BatterySystem, GridTariff, EnergyBalanceInput,
        simulate_lifetime, extract_pv_profile,
        find_battery_replacement_years,
    )
    from engine.economics import (
        InvestmentCost, OperatingCost, FinancialParameters,
        ItalianIncentives, calc_economics,
    )
    from main import (
        SimulationResult, SimulationInput, LocationInput,
        BuildingInput, PVSystemInput, BatteryInput, TariffInput,
    )

    hourly_Wh  = school_loads["hourly_demand_Wh"]
    total_kwh  = school_loads["total_annual_kwh"]
    monthly    = school_loads["total_monthly_kwh"]
    breakdown  = school_loads["breakdown"]
    peak_W     = max(hourly_Wh) if hourly_Wh else 0.0
    avg_W      = sum(hourly_Wh) / len(hourly_Wh) if hourly_Wh else 0.0

    # ── Build LoadsResult from school data ────────────────────────────────────
    appliances_kwh = (
        breakdown["computers"] + breakdown["tech_lab"] + breakdown["canteen"]
        + breakdown["gym"] + breakdown["admin"] + breakdown["elevator"]
        + breakdown["misc"]
    )
    total = total_kwh or 1.0

    loads_result = LoadsResult(
        dhw_kWh                    = 0.0,
        ventilation_kWh            = 0.0,
        lighting_kWh               = breakdown["lighting"],
        appliances_kWh             = appliances_kwh,
        hvac_heating_kWh           = breakdown["heating_hp"],
        hvac_cooling_kWh           = breakdown["ac"],
        hvac_auxiliary_kWh         = 0.0,
        total_annual_kWh           = round(total_kwh, 1),
        dhw_monthly_kWh            = [0.0] * 12,
        ventilation_monthly_kWh    = [0.0] * 12,
        lighting_monthly_kWh       = [round(v * breakdown["lighting"] / total, 1) for v in monthly],
        appliances_monthly_kWh     = [round(v * appliances_kwh / total, 1) for v in monthly],
        hvac_heating_monthly_kWh   = [0.0] * 12,
        hvac_cooling_monthly_kWh   = [round(v * breakdown["ac"] / total, 1) for v in monthly],
        hvac_auxiliary_monthly_kWh = [0.0] * 12,
        total_monthly_kWh          = [round(v, 1) for v in monthly],
        hourly_demand_Wh           = hourly_Wh,
        specific_demand_kWh_m2     = round(total_kwh / floor_area_m2, 1) if floor_area_m2 > 0 else 0.0,
        peak_demand_W              = round(peak_W, 1),
        load_factor                = round(avg_W / peak_W, 3) if peak_W > 0 else 0.0,
        bill_cross_check           = {},
        category_shares            = {k: round(v / total * 100, 1) for k, v in breakdown.items()},
    )

    # ── PV simulation ─────────────────────────────────────────────────────────
    pv_system = PVSystem(
        peak_power_kWp             = kwp,
        tilt_deg                   = tilt_deg,
        azimuth_deg                = azimuth_deg,
        module_efficiency_stc      = 0.20,
        temp_coefficient_pct_per_K = -0.40,
        inverter_efficiency_nominal= 0.97,
        dc_ac_ratio                = 1.15,
        system_losses              = SystemLosses(),
        degradation_rate_pct_per_yr= 0.50,
        iam_b0                     = 0.05,
    )
    solar_result = simulate_pv(
        tmy=tmy, system=pv_system,
        mounting="flat_roof_ballast", use_pvlib=False,
    )

    # ── Energy balance ────────────────────────────────────────────────────────
    bat_system = BatterySystem(capacity_kWh=0.0)
    tariff_obj = GridTariff(
        import_price_eur_kWh = import_price,
        export_price_eur_kWh = export_price,
    )
    pv_profile = extract_pv_profile(solar_result)
    eb_inp = EnergyBalanceInput(
        pv_production_hourly_Wh = pv_profile,
        demand_hourly_Wh        = hourly_Wh,
        battery                 = bat_system,
        tariff                  = tariff_obj,
        peak_power_kWp          = kwp,
        battery_strategy        = "self_consumption",
    )
    yearly_balance = simulate_lifetime(
        base_inp              = eb_inp,
        lifetime_years        = lifetime_years,
        pv_degradation_pct_per_yr = 0.50,
    )

    # ── Economics ─────────────────────────────────────────────────────────────
    # Net capex after PNRR grant (fondo perduto reduces effective investment)
    net_capex = max(capex_eur - pnrr_grant, 0.0)
    pv_cost_per_kwp = (net_capex - structural_eur) / kwp if kwp > 0 else 0.0
    pv_cost_per_kwp = max(pv_cost_per_kwp, 0.0)

    investment = InvestmentCost(
        pv_cost_eur_kWp        = pv_cost_per_kwp,
        peak_power_kWp         = kwp,
        design_and_permits_eur = structural_eur,
    )
    incentives = ItalianIncentives(
        detrazione_fiscale_pct = 0.0,    # no IRPEF for schools
        ammortamento_accelerato= False,
        cer_incentivo_eur_kWh  = cer_incentive_eur_kwh,
        cer_shared_fraction    = cer_fraction,
    )
    opex = OperatingCost(
        om_pct_of_capex_per_yr     = 1.0,
        inverter_replacement_year  = 12,
    )
    fin = FinancialParameters(
        discount_rate           = 0.04,
        energy_price_escalation = 0.03,
        analysis_years          = lifetime_years,
        financing_equity_fraction=1.0,
    )
    economics_result = calc_economics(
        yearly_results = yearly_balance,
        investment     = investment,
        opex           = opex,
        fin            = fin,
        incentives     = incentives,
    )

    # ── Wrap into SimulationResult ────────────────────────────────────────────
    sim_inp = SimulationInput(
        location   = LocationInput(
            lat = tmy.location.latitude,
            lon = tmy.location.longitude,
        ),
        pv_system  = PVSystemInput(
            peak_power_kWp = kwp,
            tilt_deg       = tilt_deg,
            azimuth_deg    = azimuth_deg,
        ),
        investment = investment,
        building   = BuildingInput(
            building_type     = "warehouse",
            floor_area_m2     = floor_area_m2,
            annual_demand_kWh = total_kwh,
        ),
    )

    return SimulationResult(
        tmy                 = tmy,
        envelope_result     = None,
        loads_result        = loads_result,
        solar_result        = solar_result,
        yearly_balance      = yearly_balance,
        economics           = economics_result,
        scenario_comparison = None,
        inp                 = sim_inp,
    )
