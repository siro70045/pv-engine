"""
engine/loads.py

Building energy loads module.
Calculates all electrical and thermal energy demands except the
envelope heating/cooling (which comes from engine/envelope.py).

Loads covered
-------------
1. Domestic Hot Water (DHW / ACS)      — UNI TS 11300-2, EN 15316-3
2. Mechanical ventilation (VMC)        — EN 13779, UNI 10339
3. Lighting                            — EN 15193-1:2017, UNI EN 12464
4. Appliances & plug loads             — UNI TS 11300-1, Annex A
5. Process / machinery loads           — industrial buildings, custom profiles
6. Hourly load profile assembly        — 8 760-hour demand curve

Output
------
LoadsResult — monthly and hourly electrical demand [kWh],
              annual totals per category, and the full 8 760-hour
              profile ready to intersect with solar production.

Standards / references
----------------------
    UNI TS 11300-1:2014   Building energy performance — Part 1
    UNI TS 11300-2:2019   DHW energy needs
    EN 15316-3-1:2017     DHW system heat loss
    EN 15193-1:2017       Energy performance of buildings — Lighting
    EN 13779:2008         Ventilation for non-residential buildings
    ASHRAE 90.1-2019      Energy standard for buildings (appliance benchmarks)
    IEA ECBCS Annex 42    Occupancy and appliance schedules
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULE PRIMITIVES
# ══════════════════════════════════════════════════════════════════════════════

# Hours per month (standard non-leap year)
HOURS_PER_MONTH = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]

# Day-of-week for Jan 1 of the reference year (2023 → Monday = 0)
_JAN1_DOW = 6  # Sunday

def _day_of_year(month: int, day: int) -> int:
    days_before = [0,31,59,90,120,151,181,212,243,273,304,334]
    return days_before[month - 1] + day

def _day_of_week(month: int, day: int) -> int:
    """Day of week: 0=Monday … 6=Sunday."""
    return (_JAN1_DOW + _day_of_year(month, day) - 1) % 7

def _is_weekend(month: int, day: int) -> bool:
    return _day_of_week(month, day) >= 5

# Days per month (non-leap year)
DAYS_PER_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

def _weekdays_weekends_in_month(month: int) -> tuple[int, int]:
    """Returns (n_weekdays, n_weekends) for a given month."""
    wd = we = 0
    for d in range(1, DAYS_PER_MONTH[month - 1] + 1):
        if _is_weekend(month, d):
            we += 1
        else:
            wd += 1
    return wd, we


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — INPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OccupancySchedule:
    """
    Defines when and how intensely a building is occupied.

    Parameters
    ----------
    building_type : str
        One of: "residential", "office", "warehouse", "factory_light",
        "factory_heavy", "retail", "school", "hotel".
    n_occupants : int
        Full-time equivalent occupants (residents, workers, etc.).
    occupied_hours_weekday : list[float]
        24 values (hour 0–23): occupancy fraction [0–1] on a weekday.
        1.0 = fully occupied, 0.0 = empty.
        If None, a standard profile for the building_type is used.
    occupied_hours_weekend : list[float]
        Same, for Saturday/Sunday. If None, derived from building_type.
    vacation_months : list[int]
        Months where building is partially unoccupied (1–12).
        Occupancy is reduced to vacation_fraction.
    vacation_fraction : float
        Residual occupancy fraction during vacation_months [0–1].
    """
    building_type: str = "residential"
    n_occupants: int = 4
    occupied_hours_weekday: Optional[list[float]] = None
    occupied_hours_weekend: Optional[list[float]] = None
    vacation_months: list[int] = field(default_factory=list)
    vacation_fraction: float = 0.10


@dataclass
class DHWSystem:
    """
    Domestic Hot Water (ACS — Acqua Calda Sanitaria) system.

    Parameters
    ----------
    daily_demand_L_per_person : float
        Hot water demand per person per day [L/day/person].
        Standard values (UNI TS 11300-2):
          Residential: 50 L/day/person
          Hotel:       80 L/day/person
          Office:       5 L/day/person
          School:       5 L/day/person
    cold_water_T_monthly : list[float]
        Monthly cold water inlet temperature [°C] — 12 values.
        If None, uses Italian average values (8–16°C).
    hot_water_T_celsius : float
        Hot water delivery temperature [°C]. Typically 45–60°C.
    system_efficiency : float
        Overall DHW system efficiency including storage losses (0–1).
        Electric water heater: 0.90, Gas boiler: 0.80, Heat pump: 0.90.
    is_electric : bool
        True if DHW is electrically heated (contributes to electrical demand).
        False if gas/district heat (thermal demand only, no electrical load).
    solar_thermal_fraction : float
        Fraction of DHW covered by solar thermal [0–1]. 0 if none.
    """
    daily_demand_L_per_person: float = 50.0
    cold_water_T_monthly: Optional[list[float]] = None
    hot_water_T_celsius: float = 45.0
    system_efficiency: float = 0.90
    is_electric: bool = True
    solar_thermal_fraction: float = 0.0
    has_recirculation_loop: bool = False
    recirculation_pipe_loss_W: float = 0.0
    # Recirculation loop: continuous heat loss from the hot-water pipe ring.
    # EN 15316-3-1 method: W of pipe heat loss × 8760 h / system_efficiency.
    # Typical values:
    #   Small residential (no loop): 0 W
    #   Office/hotel (short loop):   100–300 W
    #   Industrial (long loop):      500–2000 W
    # Set has_recirculation_loop=True and provide recirculation_pipe_loss_W.


@dataclass
class VentilationSystem:
    """
    Mechanical ventilation / VMC system electrical load.

    Parameters
    ----------
    airflow_m3h_per_person : float
        Fresh air supply per person [m³/h/person].
        EN 13779 category IDA2 (good): 36 m³/h/person.
        EN 13779 category IDA3 (medium): 22 m³/h/person.
    specific_fan_power_W_m3h : float
        Specific Fan Power (SFP) [W/(m³/h)].
        EN 13779: SFP ≤ 1.5 W/(m³/h) for category SFP3 (good).
        Typical range: 0.8 (excellent) to 2.5 (poor).
    heat_recovery_efficiency : float
        Sensible heat recovery efficiency [0–1]. 0 if no VMC.
    operation_hours_per_day : float
        Hours per day the ventilation system runs.
    n_fans : int
        Number of independent fan units (for redundancy / partial operation).
    """
    airflow_m3h_per_person: float = 36.0
    specific_fan_power_W_m3h: float = 1.2
    heat_recovery_efficiency: float = 0.75
    operation_hours_per_day: float = 16.0
    n_fans: int = 1


@dataclass
class LightingSystem:
    """
    Artificial lighting system.

    Parameters
    ----------
    installed_power_W_m2 : float
        Installed lighting power density [W/m²].
        EN 15193 / UNI EN 12464 benchmarks:
          Office:           10–12 W/m²  (LED)
          Warehouse:         5–8  W/m²  (LED highbay)
          Factory:           8–15 W/m²
          Retail:           15–25 W/m²
          Residential:       5–8  W/m²
    floor_area_m2 : float
        Conditioned floor area [m²].
    daylight_factor : float
        Fraction of lighting hours saved by daylight [0–1].
        0 = no daylight contribution (windowless warehouses).
        0.3 = typical office with windows.
        Varies monthly with available daylight hours.
    occupancy_control_factor : float
        Fractional saving from occupancy/presence sensors [0–1].
        0 = no sensors.  0.20 = 20% saving (EN 15193 Table B.6).
    on_hours_weekday : list[float]
        24 values: fraction of lighting power ON each hour on weekdays [0–1].
        If None, a standard profile for building_type is used.
    on_hours_weekend : list[float]
        Same for weekends.
    """
    installed_power_W_m2: float = 8.0
    floor_area_m2: float = 100.0
    daylight_factor: float = 0.15
    occupancy_control_factor: float = 0.10
    on_hours_weekday: Optional[list[float]] = None
    on_hours_weekend: Optional[list[float]] = None


@dataclass
class ApplianceLoad:
    """
    A single appliance category or process load.

    Parameters
    ----------
    name : str
        Descriptive label (e.g. "refrigeration", "CNC machines").
    annual_kWh : float
        Annual electrical consumption [kWh/yr].
        Use this when the total is known from bills or datasheets.
    power_W : float
        Rated power [W]. Used only if annual_kWh is 0.
    hours_per_day : float
        Average daily usage [h/day]. Used only if annual_kWh is 0.
    hourly_profile : list[float]
        24 values: fraction of power consumed each hour [0–1].
        Sum need not equal 24; it encodes shape, not magnitude.
        If None, a flat profile is used.
    active_months : list[int]
        Months when this load is active (1–12). Default: all 12.
    weekday_only : bool
        True if the load only runs on weekdays (e.g. industrial machinery).
    """
    name: str = "appliance"
    annual_kWh: float = 0.0
    power_W: float = 0.0
    hours_per_day: float = 8.0
    hourly_profile: Optional[list[float]] = None
    active_months: list[int] = field(default_factory=lambda: list(range(1, 13)))
    weekday_only: bool = False
    standby_fraction: float = 0.0
    # Fraction of rated power consumed during OFF hours (standby/idle mode).
    # 0 = completely off when not in use (default).
    # Typical values:
    #   Office PC with sleep:        0.02  (2% standby)
    #   Industrial CNC machine:      0.05–0.10
    #   Compressor (control panel):  0.03
    #   Server (idle):               0.40
    temperature_dependent: bool = False
    # If True, monthly consumption scales with outdoor temperature delta.
    # Use for refrigeration / cooling equipment: works harder in hot months.
    # Scaling: kWh_month = base × (1 + temp_sensitivity × (T_ext - T_ref) / T_ref)
    temp_sensitivity: float = 0.0
    # Fractional increase in consumption per °C above T_ref (e.g. 0.03 = 3%/°C).
    temp_ref_celsius: float = 20.0
    # Reference outdoor temperature [°C] at which annual_kWh is declared.

    def resolve_annual_kWh(self) -> float:
        """Return annual_kWh, computing from power × hours if not set."""
        if self.annual_kWh > 0:
            return self.annual_kWh
        active = len(self.active_months)
        days_per_year = sum(DAYS_PER_MONTH[m-1] for m in self.active_months)
        if self.weekday_only:
            days_per_year = round(days_per_year * 5 / 7)
        return self.power_W * self.hours_per_day * days_per_year / 1000.0


@dataclass
class HVACSystem:
    """
    Heating, cooling and ventilation system electrical consumption.
    Only the ELECTRICAL part (compressors, pumps, fans) is tracked here.
    Thermal demand comes from envelope.py.

    Parameters
    ----------
    heating_system : str
        "heat_pump", "electric_resistance", "gas_boiler", "district".
        Non-electric systems → 0 electrical demand for heating.
    cooling_system : str
        "split_ac", "chiller", "none".
    heating_cop : float
        COP of heating system. Heat pump: 2.5–4.5. Resistance: 1.0.
    cooling_eer : float
        EER (COP) of cooling system. Split AC: 2.5–4.0. Chiller: 4.0–6.0.
    auxiliary_W : float
        Electrical power of auxiliary loads (pumps, controls) [W].
        Run whenever HVAC is active.
    """
    heating_system: str = "heat_pump"
    cooling_system: str = "split_ac"
    heating_cop: float = 3.0
    cooling_eer: float = 2.8
    auxiliary_W: float = 200.0


@dataclass
class LoadsInput:
    """
    Complete input for the loads module.

    Parameters
    ----------
    floor_area_m2 : float
        Conditioned floor area [m²].
    building_type : str
        Building category — drives default profiles.
    occupancy : OccupancySchedule
    dhw : DHWSystem
    ventilation : VentilationSystem
    lighting : LightingSystem
    appliances : list[ApplianceLoad]
        Any number of appliance/process loads.
    hvac : HVACSystem
        HVAC system parameters (for electrical demand from thermal loads).
    heating_demand_monthly_kWh : list[float]
        12 monthly heating demand values from envelope.py [kWh].
    cooling_demand_monthly_kWh : list[float]
        12 monthly cooling demand values from envelope.py [kWh].
    annual_bill_kWh : float
        Optional: total annual electricity bill [kWh].
        If > 0 and bottom-up total differs by > 20%, a warning is issued.
        Does NOT override the bottom-up calculation.
    """
    floor_area_m2: float
    building_type: str
    occupancy: OccupancySchedule
    dhw: DHWSystem
    ventilation: VentilationSystem
    lighting: LightingSystem
    appliances: list[ApplianceLoad]
    hvac: HVACSystem
    heating_demand_monthly_kWh: list[float]
    cooling_demand_monthly_kWh: list[float]
    annual_bill_kWh: float = 0.0
    monthly_T_ext: Optional[list[float]] = None
    # Monthly average outdoor temperatures [°C] — 12 values.
    # Used for temperature-dependent appliance loads (refrigeration).
    # If None, Italian average values are used as fallback.
    # Best source: tmy.monthly.T_avg from pvgis_client.fetch_tmy().


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — OUTPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LoadsResult:
    """
    Complete output of the loads module.

    All electrical values in [kWh].
    Hourly profile has 8 760 values (1 per hour of the year).
    Monthly arrays have 12 values (index 0 = January).
    """
    # ── Annual totals by category ──────────────────────────────────────────
    dhw_kWh: float                # DHW electrical demand
    ventilation_kWh: float        # Fan electrical demand
    lighting_kWh: float           # Artificial lighting
    appliances_kWh: float         # Appliances + plug loads + machinery
    hvac_heating_kWh: float       # Electrical input for heating (heat pump etc.)
    hvac_cooling_kWh: float       # Electrical input for cooling
    hvac_auxiliary_kWh: float     # Pumps, controls, auxiliary

    total_annual_kWh: float       # Sum of all categories

    # ── Monthly profiles [12 values] ──────────────────────────────────────
    dhw_monthly_kWh: list[float]
    ventilation_monthly_kWh: list[float]
    lighting_monthly_kWh: list[float]
    appliances_monthly_kWh: list[float]
    hvac_heating_monthly_kWh: list[float]
    hvac_cooling_monthly_kWh: list[float]
    hvac_auxiliary_monthly_kWh: list[float]
    total_monthly_kWh: list[float]

    # ── Hourly profile [8 760 values, Wh/h] ──────────────────────────────
    # Index: hour 0 = 00:00–01:00 of Jan 1
    hourly_demand_Wh: list[float]

    # ── Diagnostics ───────────────────────────────────────────────────────
    specific_demand_kWh_m2: float   # total / floor_area
    peak_demand_W: float            # maximum hourly power
    load_factor: float              # avg / peak (0–1)
    bill_cross_check: dict          # comparison vs annual_bill_kWh if provided
    category_shares: dict           # percentage breakdown by category


# ══════════════════════════════════════════════════════════════════════════════
# STANDARD OCCUPANCY PROFILES
# Fraction of full occupancy, per hour (0–23)
# Sources: IEA ECBCS Annex 42, ASHRAE 90.1, EN 15193 Annex B
# ══════════════════════════════════════════════════════════════════════════════

_OCC_PROFILES: dict[str, dict[str, list[float]]] = {
    "residential": {
        # Home all day (conservative — some leave for work)
        "weekday": [0.8,0.8,0.8,0.8,0.8,0.8,0.9,0.6,0.3,0.2,0.2,0.2,
                    0.3,0.2,0.2,0.2,0.3,0.5,0.7,0.8,0.9,0.9,0.9,0.9],
        "weekend": [0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.8,0.7,0.6,0.6,0.6,
                    0.7,0.7,0.7,0.7,0.8,0.8,0.9,0.9,0.9,0.9,0.9,0.9],
    },
    "office": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.7,0.9,0.9,0.9,
                    0.8,0.9,0.9,0.9,0.8,0.5,0.2,0.1,0.0,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,
                    0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
    },
    "warehouse": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.8,1.0,1.0,1.0,1.0,
                    0.9,1.0,1.0,1.0,0.8,0.5,0.1,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.2,0.5,0.5,0.5,0.5,
                    0.3,0.3,0.3,0.2,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
    },
    "factory_light": {
        # Two-shift operation
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.9,1.0,1.0,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,1.0,0.8,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.5,0.8,0.8,0.8,0.8,
                    0.5,0.5,0.5,0.5,0.3,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
    },
    "factory_heavy": {
        # Three-shift continuous operation
        "weekday": [1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0],
        "weekend": [1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0],
    },
    "retail": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.5,0.9,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,0.9,0.7,0.5,0.1,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.3,0.8,1.0,1.0,
                    1.0,1.0,1.0,1.0,0.8,0.5,0.2,0.0,0.0,0.0,0.0,0.0],
    },
    "school": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.2,0.9,1.0,1.0,1.0,
                    0.9,1.0,1.0,0.8,0.2,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0]*24,
    },
    "hotel": {
        "weekday": [0.6,0.5,0.4,0.4,0.4,0.4,0.5,0.7,0.9,0.7,0.6,0.6,
                    0.7,0.7,0.7,0.7,0.8,0.9,1.0,1.0,1.0,0.9,0.8,0.7],
        "weekend": [0.7,0.6,0.5,0.5,0.5,0.5,0.6,0.8,1.0,0.9,0.8,0.8,
                    0.9,0.9,0.9,0.9,1.0,1.0,1.0,1.0,1.0,0.9,0.8,0.8],
    },
}

# Lighting ON profiles (fraction of installed power active each hour)
_LIGHT_PROFILES: dict[str, dict[str, list[float]]] = {
    "residential": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.1,0.3,0.2,0.1,0.1,0.1,0.1,
                    0.1,0.1,0.1,0.2,0.3,0.5,0.7,0.8,0.8,0.7,0.5,0.2],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.2,0.3,0.2,0.2,0.2,
                    0.2,0.2,0.2,0.3,0.4,0.6,0.7,0.8,0.8,0.7,0.5,0.2],
    },
    "office": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.8,1.0,1.0,1.0,
                    0.9,1.0,1.0,1.0,0.9,0.5,0.2,0.1,0.0,0.0,0.0,0.0],
        "weekend": [0.0]*24,
    },
    "warehouse": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.1,0.9,1.0,1.0,1.0,1.0,
                    0.9,1.0,1.0,1.0,0.8,0.3,0.0,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.2,0.5,0.5,0.5,0.5,
                    0.3,0.3,0.3,0.2,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
    },
    "factory_light": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.8,1.0,1.0,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,1.0,0.5,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0]*24,
    },
    "factory_heavy": {
        "weekday": [1.0]*24,
        "weekend": [1.0]*24,
    },
    "retail": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.2,0.8,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,1.0,1.0,0.8,0.5,0.1,0.0,0.0,0.0],
        "weekend": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.5,1.0,1.0,1.0,
                    1.0,1.0,1.0,1.0,0.8,0.4,0.1,0.0,0.0,0.0,0.0,0.0],
    },
    "school": {
        "weekday": [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.3,1.0,1.0,1.0,1.0,
                    0.9,1.0,1.0,0.8,0.3,0.0,0.0,0.0,0.0,0.0,0.0,0.0],
        "weekend": [0.0]*24,
    },
    "hotel": {
        "weekday": [0.3,0.2,0.2,0.2,0.2,0.2,0.3,0.5,0.7,0.5,0.5,0.5,
                    0.5,0.5,0.5,0.5,0.6,0.8,1.0,1.0,1.0,0.9,0.7,0.5],
        "weekend": [0.4,0.3,0.2,0.2,0.2,0.2,0.3,0.5,0.8,0.7,0.6,0.6,
                    0.6,0.6,0.6,0.6,0.7,0.9,1.0,1.0,1.0,0.9,0.7,0.5],
    },
}

# DHW draw profile (fraction of daily demand drawn each hour)
# Peak in morning (shower) and evening (kitchen)
_DHW_PROFILE = [
    0.010, 0.006, 0.004, 0.004, 0.004, 0.010,  # 00-05
    0.030, 0.080, 0.100, 0.070, 0.040, 0.040,  # 06-11
    0.050, 0.040, 0.030, 0.030, 0.040, 0.070,  # 12-17
    0.090, 0.090, 0.070, 0.050, 0.030, 0.020,  # 18-23
]  # sum = 0.912 — renormalised in calc

# Cold water temperatures for Italy (monthly) [°C] — UNI TS 11300-2 Table 3
_COLD_WATER_T_ITALY = [8.0, 8.5, 9.5, 11.0, 13.0, 15.0,
                        17.0, 17.0, 15.5, 13.0, 10.5, 8.5]

# Daylight hours per month (approximate, lat ~44°N — Italy)
# Used to compute daylight-based lighting reduction
_DAYLIGHT_HOURS_MONTHLY = [9.5, 10.5, 11.8, 13.2, 14.5, 15.2,
                             14.8, 13.8, 12.3, 10.8, 9.7, 9.2]


# ══════════════════════════════════════════════════════════════════════════════
# DHW CALCULATION — UNI TS 11300-2 / EN 15316-3
# ══════════════════════════════════════════════════════════════════════════════

def _calc_dhw(
    inp: LoadsInput,
) -> tuple[list[float], list[float]]:
    """
    Monthly DHW thermal demand and electrical demand [kWh/month].

    Returns (dhw_thermal_monthly, dhw_electric_monthly).
    Thermal demand is always calculated.
    Electrical demand is non-zero only if dhw.is_electric = True.

    Physics:
        Q_DHW = m_water × c_p × (T_hot - T_cold)
        m_water = n_persons × daily_litres × days_in_month
        c_p = 1.163 Wh/(kg·K) = 4.187 kJ/(kg·K) / 3600
    """
    dhw  = inp.dhw
    occ  = inp.occupancy
    T_cold = dhw.cold_water_T_monthly or _COLD_WATER_T_ITALY
    T_hot  = dhw.hot_water_T_celsius
    C_P_WH_KG_K = 1.163   # specific heat of water [Wh/kgK]
    DENSITY_KG_L = 1.0    # water density [kg/L]

    thermal_m: list[float] = []
    electric_m: list[float] = []

    for m in range(12):
        days    = DAYS_PER_MONTH[m]
        T_c     = T_cold[m]
        delta_T = max(T_hot - T_c, 0.0)

        # Effective persons (vacation reduction)
        month_num = m + 1
        occ_factor = (dhw.solar_thermal_fraction == 0 and 1.0 or
                      max(1.0 - dhw.solar_thermal_fraction, 0.0))
        vacation   = month_num in occ.vacation_months
        n_eff      = occ.n_occupants * (occ.vacation_fraction if vacation else 1.0)

        # Daily water volume [L/day]
        daily_L = n_eff * dhw.daily_demand_L_per_person

        # Monthly thermal demand [kWh] — before solar fraction
        Q_thermal = (daily_L * days * DENSITY_KG_L * C_P_WH_KG_K * delta_T
                     / dhw.system_efficiency / 1000.0)

        # Apply solar thermal coverage
        solar_frac = dhw.solar_thermal_fraction
        # Solar fraction varies by month (higher in summer)
        # Using a cosine approximation: higher in June, lower in December
        month_solar_adj = 1.0 + 0.5 * math.cos(math.radians(360 * (m - 5) / 12))
        solar_m = min(solar_frac * month_solar_adj, 0.95)   # cap at 95%
        Q_thermal_net = Q_thermal * (1.0 - solar_m)

        # ── Recirculation loop distribution losses (EN 15316-3-1) ────────────
        # Continuous pipe heat loss regardless of occupancy.
        # Loss = pipe_loss_W × hours_in_month / system_efficiency / 1000
        if dhw.has_recirculation_loop and dhw.recirculation_pipe_loss_W > 0:
            Q_recirc = (dhw.recirculation_pipe_loss_W
                        * HOURS_PER_MONTH[m]
                        / dhw.system_efficiency
                        / 1000.0)
            # Solar thermal does NOT cover recirculation losses
            Q_thermal_net += Q_recirc

        thermal_m.append(round(Q_thermal_net, 2))

        # Electrical demand: only if electric system
        if dhw.is_electric:
            electric_m.append(round(Q_thermal_net, 2))
        else:
            electric_m.append(0.0)

    return thermal_m, electric_m


# ══════════════════════════════════════════════════════════════════════════════
# VENTILATION ELECTRICAL DEMAND — EN 13779
# ══════════════════════════════════════════════════════════════════════════════

def _calc_ventilation(inp: LoadsInput) -> list[float]:
    """
    Monthly fan electrical consumption [kWh/month].

    P_fan = Q_air × SFP   [W]
    Q_air = n_occupants × airflow_per_person  [m³/h]

    Fans modulate with occupancy: part-load power ∝ (flow)^3 (fan law).
    """
    vent = inp.ventilation
    occ  = inp.occupancy

    Q_full = inp.occupancy.n_occupants * vent.airflow_m3h_per_person   # m³/h at full occupancy
    P_full_W = Q_full * vent.specific_fan_power_W_m3h                  # W

    occ_profile_wd = (occ.occupied_hours_weekday
                      or _OCC_PROFILES.get(occ.building_type, _OCC_PROFILES["office"])["weekday"])
    occ_profile_we = (occ.occupied_hours_weekend
                      or _OCC_PROFILES.get(occ.building_type, _OCC_PROFILES["office"])["weekend"])

    monthly: list[float] = []
    for m in range(12):
        month_num = m + 1
        vacation  = month_num in occ.vacation_months
        vac_f     = occ.vacation_fraction if vacation else 1.0

        kwh = 0.0
        for d in range(1, DAYS_PER_MONTH[m] + 1):
            profile = occ_profile_we if _is_weekend(m + 1, d) else occ_profile_wd
            for h in range(24):
                occ_frac = profile[h] * vac_f
                # Fan law: P ∝ flow^3, but minimum flow is 30% for IAQ
                flow_frac = max(occ_frac, 0.30)
                # Power at part-load (fan affinity law)
                P_h = P_full_W * flow_frac ** 3
                # Ventilation only runs during operation hours
                if h < vent.operation_hours_per_day:
                    kwh += P_h / 1000.0   # Wh → kWh per hour

        monthly.append(round(kwh, 2))

    return monthly


# ══════════════════════════════════════════════════════════════════════════════
# LIGHTING — EN 15193-1
# ══════════════════════════════════════════════════════════════════════════════

def _calc_lighting(inp: LoadsInput) -> list[float]:
    """
    Monthly lighting electrical consumption [kWh/month].

    Method: EN 15193-1 simplified (hourly profile × installed power).

    Corrections applied:
    - Daylight availability: linear reduction during daylight hours
    - Occupancy control: constant fractional saving
    """
    light = inp.lighting
    occ   = inp.occupancy

    P_installed_W = light.installed_power_W_m2 * light.floor_area_m2
    occ_ctrl      = 1.0 - light.occupancy_control_factor

    light_wd = (light.on_hours_weekday
                or _LIGHT_PROFILES.get(occ.building_type, _LIGHT_PROFILES["office"])["weekday"])
    light_we = (light.on_hours_weekend
                or _LIGHT_PROFILES.get(occ.building_type, _LIGHT_PROFILES["office"])["weekend"])

    monthly: list[float] = []
    for m in range(12):
        month_num   = m + 1
        vacation    = month_num in occ.vacation_months
        vac_f       = occ.vacation_fraction if vacation else 1.0
        daylight_h  = _DAYLIGHT_HOURS_MONTHLY[m]   # hours of useful daylight

        kwh = 0.0
        for d in range(1, DAYS_PER_MONTH[m] + 1):
            profile  = light_we if _is_weekend(m + 1, d) else light_wd
            for h in range(24):
                frac = profile[h] * vac_f

                # Daylight correction: reduce lighting during daylight hours
                # Sunrise assumed at (12 - daylight_h/2), sunset at (12 + daylight_h/2)
                sunrise = 12.0 - daylight_h / 2.0
                sunset  = 12.0 + daylight_h / 2.0
                in_daylight = sunrise <= h < sunset
                if in_daylight:
                    frac *= (1.0 - light.daylight_factor)

                # Occupancy sensor saving
                frac *= occ_ctrl

                kwh += P_installed_W * frac / 1000.0

        monthly.append(round(kwh, 2))

    return monthly


# ══════════════════════════════════════════════════════════════════════════════
# APPLIANCES & MACHINERY
# ══════════════════════════════════════════════════════════════════════════════

# Monthly average outdoor temperatures for Italy — used for temp-dependent loads
# Falls back to this if no T_ext provided in LoadsInput
_T_EXT_ITALY_AVG = [2.0, 4.0, 8.5, 13.0, 18.0, 22.5,
                     25.5, 25.0, 20.5, 14.5, 8.0, 3.0]


def _calc_appliances(inp: LoadsInput) -> list[float]:
    """
    Monthly appliance + machinery electrical consumption [kWh/month].
    Sums all ApplianceLoad items, applying:

    1. Standby consumption during OFF hours.
       Standby power = rated_power × standby_fraction.
       Added for all hours NOT covered by hours_per_day.

    2. Temperature-dependent scaling (refrigeration / cooling loads).
       Monthly consumption scales with outdoor temperature:
           factor = 1 + temp_sensitivity × (T_ext_m - T_ref)
       Capped at 0 (load cannot go negative).

    3. Seasonal activation: only in active_months.
    4. Weekday-only: load only runs on working days.
    """
    if not inp.appliances:
        return _benchmark_appliances(inp)

    T_ext_m = getattr(inp, 'monthly_T_ext', None) or _T_EXT_ITALY_AVG

    monthly = [0.0] * 12

    for app in inp.appliances:
        annual_kwh = app.resolve_annual_kWh()

        # ── Standby load ──────────────────────────────────────────────────────
        # Standby power: fraction of rated power consumed during idle hours.
        # Applies across ALL months (equipment plugged in year-round).
        if app.standby_fraction > 0 and app.power_W > 0:
            P_standby_W = app.power_W * app.standby_fraction
            # Off-hours per day = 24 - hours_per_day
            off_hours_per_day = max(24.0 - app.hours_per_day, 0.0)
            for m in range(12):
                days = DAYS_PER_MONTH[m]
                if app.weekday_only:
                    wd, _ = _weekdays_weekends_in_month(m + 1)
                    # During weekends: all 24h are standby if weekday_only
                    _, we = _weekdays_weekends_in_month(m + 1)
                    standby_kwh = (P_standby_W * off_hours_per_day * wd / 1000.0
                                   + P_standby_W * 24.0 * we / 1000.0)
                else:
                    standby_kwh = P_standby_W * off_hours_per_day * days / 1000.0
                monthly[m] += standby_kwh

        if annual_kwh <= 0:
            continue

        # Distribute across active months proportional to days
        total_active_days = sum(DAYS_PER_MONTH[m-1] for m in app.active_months)
        if total_active_days == 0:
            continue

        for m in range(12):
            month_num = m + 1
            if month_num not in app.active_months:
                continue
            days = DAYS_PER_MONTH[m]

            if app.weekday_only:
                wd, _ = _weekdays_weekends_in_month(month_num)
                day_weight = wd / (total_active_days * 5 / 7)
            else:
                day_weight = days / total_active_days

            kwh_m = annual_kwh * day_weight

            # ── Temperature-dependent scaling ─────────────────────────────────
            # Models refrigeration / cooling equipment that works harder
            # when outdoor temperature rises above reference.
            # factor = 1 + sensitivity × (T_ext - T_ref)
            # Capped at minimum 0.1 (equipment doesn't shut off completely).
            if app.temperature_dependent and app.temp_sensitivity > 0:
                T_ext = T_ext_m[m]
                factor = 1.0 + app.temp_sensitivity * (T_ext - app.temp_ref_celsius)
                factor = max(factor, 0.1)
                kwh_m *= factor

            monthly[m] += kwh_m

    return [round(v, 2) for v in monthly]


def _benchmark_appliances(inp: LoadsInput) -> list[float]:
    """
    Benchmark appliance load when no detailed list is provided.
    Based on UNI TS 11300-1 Annex A and ASHRAE 90.1 Table G3.1.

    Returns monthly kWh, uniform distribution across months.
    """
    # Specific annual load [kWh/m²/yr] by building type
    BENCH: dict[str, float] = {
        "residential":   30.0,   # appliances + electronics per m²
        "office":        20.0,   # computers, printers, servers, coffee
        "warehouse":      5.0,   # minimal plug loads
        "factory_light": 15.0,   # tools, compressors, conveyors
        "factory_heavy": 50.0,   # heavy machinery (process-specific)
        "retail":        25.0,   # refrigeration, POS, displays
        "school":        12.0,   # computers, projectors, canteen
        "hotel":         35.0,   # restaurant, bar, fitness, laundry
    }
    annual = BENCH.get(inp.building_type, 20.0) * inp.floor_area_m2
    monthly = [annual * DAYS_PER_MONTH[m] / 365 for m in range(12)]
    return [round(v, 2) for v in monthly]


# ══════════════════════════════════════════════════════════════════════════════
# HVAC ELECTRICAL DEMAND
# ══════════════════════════════════════════════════════════════════════════════

def _calc_hvac_electrical(inp: LoadsInput) -> tuple[list[float], list[float], list[float]]:
    """
    Monthly electrical demand for heating, cooling, and HVAC auxiliaries.

    Returns (heating_electric_monthly, cooling_electric_monthly, aux_monthly).

    For heat pumps:   E_heating = Q_heating / COP
    For gas boilers:  E_heating = 0 (no electrical demand for heat generation)
    For cooling:      E_cooling = Q_cooling / EER
    """
    hvac = inp.hvac

    heating_e: list[float] = []
    cooling_e: list[float] = []
    aux_e:     list[float] = []

    ELECTRIC_HEATING = {"heat_pump", "electric_resistance"}
    ACTIVE_COOLING   = {"split_ac", "chiller"}

    # Estimate monthly HVAC operating hours for auxiliary load
    # (active when heating or cooling is running)
    for m in range(12):
        Q_h = inp.heating_demand_monthly_kWh[m]
        Q_c = inp.cooling_demand_monthly_kWh[m]

        # Heating electrical input
        if hvac.heating_system in ELECTRIC_HEATING and Q_h > 0:
            cop = hvac.heating_cop
            if hvac.heating_system == "electric_resistance":
                cop = 1.0
            heating_e.append(round(Q_h / cop, 2))
        else:
            heating_e.append(0.0)

        # Cooling electrical input
        if hvac.cooling_system in ACTIVE_COOLING and Q_c > 0:
            cooling_e.append(round(Q_c / hvac.cooling_eer, 2))
        else:
            cooling_e.append(0.0)

        # Auxiliary: pumps, controls — run whenever HVAC is active
        # Estimate active hours from load magnitude (rough proxy)
        hvac_active_h = HOURS_PER_MONTH[m] * 0.4 if (Q_h + Q_c) > 0 else 0
        aux_kwh = hvac.auxiliary_W * hvac_active_h / 1000.0
        aux_e.append(round(aux_kwh, 2))

    return heating_e, cooling_e, aux_e


# ══════════════════════════════════════════════════════════════════════════════
# HOURLY PROFILE ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════════

def _build_hourly_profile(
    inp: LoadsInput,
    dhw_monthly: list[float],
    vent_monthly: list[float],
    light_monthly: list[float],
    app_monthly: list[float],
    hvac_h_monthly: list[float],
    hvac_c_monthly: list[float],
    hvac_aux_monthly: list[float],
) -> list[float]:
    """
    Build the 8 760-hour demand profile [Wh/h].

    Each monthly total is disaggregated hourly using the category-specific
    shape profiles.  The profiles encode load shape — each is normalised so
    its sum over the month equals the monthly total.
    """
    occ     = inp.occupancy
    btype   = occ.building_type

    occ_wd  = (occ.occupied_hours_weekday
               or _OCC_PROFILES.get(btype, _OCC_PROFILES["office"])["weekday"])
    occ_we  = (occ.occupied_hours_weekend
               or _OCC_PROFILES.get(btype, _OCC_PROFILES["office"])["weekend"])
    lgt_wd  = (inp.lighting.on_hours_weekday
               or _LIGHT_PROFILES.get(btype, _LIGHT_PROFILES["office"])["weekday"])
    lgt_we  = (inp.lighting.on_hours_weekend
               or _LIGHT_PROFILES.get(btype, _LIGHT_PROFILES["office"])["weekend"])

    # DHW draw profile (normalised)
    dhw_sum = sum(_DHW_PROFILE)
    dhw_h   = [v / dhw_sum for v in _DHW_PROFILE]

    hourly: list[float] = []

    for m in range(12):
        month_num = m + 1
        vacation  = month_num in occ.vacation_months
        vac_f     = occ.vacation_fraction if vacation else 1.0

        # ── Compute per-hour weights for occupancy-driven loads ──────────────
        # We need to distribute monthly totals proportional to hourly shape.

        # Sum of shape weights for the month (to normalise correctly)
        wd_count, we_count = _weekdays_weekends_in_month(month_num)

        def month_weight_sum(wd_profile: list[float], we_profile: list[float]) -> float:
            return (sum(wd_profile) * wd_count + sum(we_profile) * we_count)

        occ_wsum = month_weight_sum(occ_wd, occ_we)
        lgt_wsum = month_weight_sum(lgt_wd, lgt_we)

        # HVAC: distribute heating load in cold hours, cooling in hot hours
        # Heating concentrated at night/morning, cooling in afternoon
        heat_shape_wd = [max(0.12 - 0.01 * h, 0.01) if h < 12
                         else max(0.04 - 0.002 * (h - 12), 0.01) for h in range(24)]
        cool_shape_wd = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01,
                         0.02, 0.04, 0.06, 0.08, 0.09, 0.10,
                         0.10, 0.10, 0.10, 0.09, 0.08, 0.07,
                         0.06, 0.05, 0.04, 0.03, 0.02, 0.01]
        hvac_heat_wsum = sum(heat_shape_wd) * (wd_count + we_count)
        hvac_cool_wsum = sum(cool_shape_wd) * (wd_count + we_count)

        # Appliance shape follows occupancy
        app_wsum = occ_wsum

        for d in range(1, DAYS_PER_MONTH[m] + 1):
            weekend  = _is_weekend(month_num, d)
            occ_p    = occ_we if weekend else occ_wd
            lgt_p    = lgt_we if weekend else lgt_wd

            for h in range(24):
                Wh = 0.0
                occ_f = occ_p[h] * vac_f

                # DHW — follows draw profile × DHW monthly total
                dhw_w = dhw_h[h] / 24   # per-hour weight
                Wh += dhw_monthly[m] * 1000 * dhw_w

                # Ventilation — follows occupancy (fan law applied in monthly, here shape)
                if occ_wsum > 0:
                    Wh += vent_monthly[m] * 1000 * occ_f / (occ_wsum * vac_f + 1e-9)

                # Lighting — follows lighting profile
                if lgt_wsum > 0:
                    Wh += light_monthly[m] * 1000 * lgt_p[h] / lgt_wsum

                # Appliances — follow occupancy shape
                if app_wsum > 0:
                    Wh += app_monthly[m] * 1000 * occ_f / (app_wsum * vac_f + 1e-9)

                # HVAC heating
                if hvac_heat_wsum > 0:
                    Wh += hvac_h_monthly[m] * 1000 * heat_shape_wd[h] / hvac_heat_wsum

                # HVAC cooling
                if hvac_cool_wsum > 0:
                    Wh += hvac_c_monthly[m] * 1000 * cool_shape_wd[h] / hvac_cool_wsum

                # HVAC auxiliary — follows occupancy
                if occ_wsum > 0:
                    Wh += hvac_aux_monthly[m] * 1000 * occ_f / (occ_wsum * vac_f + 1e-9)

                hourly.append(max(Wh, 0.0))

    # ── Exact normalisation: scale hourly profile so its sum matches
    # the sum of monthly totals exactly (removes floating-point drift). ──────
    all_monthly = [
        dhw_monthly[m] + vent_monthly[m] + light_monthly[m]
        + app_monthly[m] + hvac_h_monthly[m] + hvac_c_monthly[m]
        + hvac_aux_monthly[m]
        for m in range(12)
    ]
    target_Wh = sum(all_monthly) * 1000.0
    actual_Wh = sum(hourly)
    if actual_Wh > 0:
        scale = target_Wh / actual_Wh
        hourly = [v * scale for v in hourly]

    return hourly


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

def calc_loads(inp: LoadsInput) -> LoadsResult:
    """
    Calculate all building energy loads and assemble the 8 760-hour profile.

    Parameters
    ----------
    inp : LoadsInput
        Complete building and system description.

    Returns
    -------
    LoadsResult
        Monthly and annual totals by category, hourly profile, diagnostics.

    Example
    -------
    >>> from engine.loads import *
    >>> from engine.envelope import calc_thermal_demand, ...
    >>>
    >>> env_result = calc_thermal_demand(...)
    >>>
    >>> inp = LoadsInput(
    ...     floor_area_m2=1000,
    ...     building_type="warehouse",
    ...     occupancy=OccupancySchedule(building_type="warehouse", n_occupants=10),
    ...     dhw=DHWSystem(daily_demand_L_per_person=5, is_electric=True),
    ...     ventilation=VentilationSystem(airflow_m3h_per_person=36, specific_fan_power_W_m3h=1.2),
    ...     lighting=LightingSystem(installed_power_W_m2=6, floor_area_m2=1000),
    ...     appliances=[],   # use benchmark
    ...     hvac=HVACSystem(heating_system="heat_pump", cooling_system="split_ac"),
    ...     heating_demand_monthly_kWh=env_result.heating_monthly_kWh,
    ...     cooling_demand_monthly_kWh=env_result.cooling_monthly_kWh,
    ... )
    >>> result = calc_loads(inp)
    >>> print(f"Total annual demand: {result.total_annual_kWh:.0f} kWh")
    """
    # ── Calculate each category ───────────────────────────────────────────────
    _, dhw_electric_m     = _calc_dhw(inp)
    vent_m                = _calc_ventilation(inp)
    light_m               = _calc_lighting(inp)
    app_m                 = _calc_appliances(inp)
    hvac_h_m, hvac_c_m, hvac_aux_m = _calc_hvac_electrical(inp)

    # ── Annual totals ─────────────────────────────────────────────────────────
    dhw_annual      = sum(dhw_electric_m)
    vent_annual     = sum(vent_m)
    light_annual    = sum(light_m)
    app_annual      = sum(app_m)
    hvac_h_annual   = sum(hvac_h_m)
    hvac_c_annual   = sum(hvac_c_m)
    hvac_aux_annual = sum(hvac_aux_m)
    total_annual    = (dhw_annual + vent_annual + light_annual + app_annual
                       + hvac_h_annual + hvac_c_annual + hvac_aux_annual)

    # ── Monthly totals ────────────────────────────────────────────────────────
    total_m = [
        dhw_electric_m[m] + vent_m[m] + light_m[m] + app_m[m]
        + hvac_h_m[m] + hvac_c_m[m] + hvac_aux_m[m]
        for m in range(12)
    ]

    # ── Hourly profile ────────────────────────────────────────────────────────
    hourly = _build_hourly_profile(
        inp, dhw_electric_m, vent_m, light_m, app_m,
        hvac_h_m, hvac_c_m, hvac_aux_m,
    )

    # ── Diagnostics ───────────────────────────────────────────────────────────
    peak_W     = max(hourly) if hourly else 0.0
    avg_W      = sum(hourly) / len(hourly) if hourly else 0.0
    load_factor = avg_W / peak_W if peak_W > 0 else 0.0

    specific   = total_annual / inp.floor_area_m2 if inp.floor_area_m2 > 0 else 0.0

    # Bill cross-check
    bill_check: dict = {}
    if inp.annual_bill_kWh > 0:
        diff_pct = (total_annual - inp.annual_bill_kWh) / inp.annual_bill_kWh * 100
        bill_check = {
            "bill_kWh":       inp.annual_bill_kWh,
            "calculated_kWh": round(total_annual, 0),
            "difference_%":   round(diff_pct, 1),
            "status": ("OK" if abs(diff_pct) < 20
                       else "WARNING: >20% difference — check inputs"),
        }

    cat_total = total_annual or 1.0
    shares = {
        "DHW_%":             round(dhw_annual      / cat_total * 100, 1),
        "ventilation_%":     round(vent_annual      / cat_total * 100, 1),
        "lighting_%":        round(light_annual     / cat_total * 100, 1),
        "appliances_%":      round(app_annual       / cat_total * 100, 1),
        "hvac_heating_%":    round(hvac_h_annual    / cat_total * 100, 1),
        "hvac_cooling_%":    round(hvac_c_annual    / cat_total * 100, 1),
        "hvac_auxiliary_%":  round(hvac_aux_annual  / cat_total * 100, 1),
    }

    return LoadsResult(
        dhw_kWh             = round(dhw_annual,      1),
        ventilation_kWh     = round(vent_annual,      1),
        lighting_kWh        = round(light_annual,     1),
        appliances_kWh      = round(app_annual,       1),
        hvac_heating_kWh    = round(hvac_h_annual,    1),
        hvac_cooling_kWh    = round(hvac_c_annual,    1),
        hvac_auxiliary_kWh  = round(hvac_aux_annual,  1),
        total_annual_kWh    = round(total_annual,     1),
        dhw_monthly_kWh             = dhw_electric_m,
        ventilation_monthly_kWh     = vent_m,
        lighting_monthly_kWh        = light_m,
        appliances_monthly_kWh      = app_m,
        hvac_heating_monthly_kWh    = hvac_h_m,
        hvac_cooling_monthly_kWh    = hvac_c_m,
        hvac_auxiliary_monthly_kWh  = hvac_aux_m,
        total_monthly_kWh           = [round(v, 1) for v in total_m],
        hourly_demand_Wh        = [round(v, 2) for v in hourly],
        specific_demand_kWh_m2  = round(specific, 1),
        peak_demand_W           = round(peak_W,   1),
        load_factor             = round(load_factor, 3),
        bill_cross_check        = bill_check,
        category_shares         = shares,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRINT SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def print_loads_summary(result: LoadsResult) -> None:
    """Pretty-print the loads calculation results."""
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
    SEP = "─" * 74

    print(f"\n{SEP}")
    print("  ENERGY LOADS SUMMARY")
    print(SEP)
    print(f"  Total annual demand        : {result.total_annual_kWh:>10.0f}  kWh/yr")
    print(f"  Specific demand            : {result.specific_demand_kWh_m2:>10.1f}  kWh/m²yr")
    print(f"  Peak hourly demand         : {result.peak_demand_W:>10.0f}  W")
    print(f"  Load factor                : {result.load_factor:>10.3f}")

    print(f"\n{SEP}")
    print("  ANNUAL BREAKDOWN BY CATEGORY")
    print(SEP)
    cats = [
        ("DHW (hot water)",      result.dhw_kWh),
        ("Ventilation (fans)",   result.ventilation_kWh),
        ("Lighting",             result.lighting_kWh),
        ("Appliances/machinery", result.appliances_kWh),
        ("HVAC heating",         result.hvac_heating_kWh),
        ("HVAC cooling",         result.hvac_cooling_kWh),
        ("HVAC auxiliary",       result.hvac_auxiliary_kWh),
    ]
    for name, val in cats:
        pct = val / result.total_annual_kWh * 100 if result.total_annual_kWh > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {name:<24} {val:>8.0f} kWh  ({pct:5.1f}%)  {bar}")

    print(f"\n{SEP}")
    print(f"  {'Month':<5} {'DHW':>7} {'Vent':>7} {'Light':>7} {'App':>7}"
          f" {'H.heat':>7} {'H.cool':>7} {'Total':>8}")
    print(f"  {'':5} {'kWh':>7} {'kWh':>7} {'kWh':>7} {'kWh':>7}"
          f" {'kWh':>7} {'kWh':>7} {'kWh':>8}")
    print(f"  {SEP}")
    for m in range(12):
        print(
            f"  {MONTHS[m]:<5}"
            f" {result.dhw_monthly_kWh[m]:>7.0f}"
            f" {result.ventilation_monthly_kWh[m]:>7.0f}"
            f" {result.lighting_monthly_kWh[m]:>7.0f}"
            f" {result.appliances_monthly_kWh[m]:>7.0f}"
            f" {result.hvac_heating_monthly_kWh[m]:>7.0f}"
            f" {result.hvac_cooling_monthly_kWh[m]:>7.0f}"
            f" {result.total_monthly_kWh[m]:>8.0f}"
        )
    print(f"  {SEP}")
    print(
        f"  {'YEAR':<5}"
        f" {result.dhw_kWh:>7.0f}"
        f" {result.ventilation_kWh:>7.0f}"
        f" {result.lighting_kWh:>7.0f}"
        f" {result.appliances_kWh:>7.0f}"
        f" {result.hvac_heating_kWh:>7.0f}"
        f" {result.hvac_cooling_kWh:>7.0f}"
        f" {result.total_annual_kWh:>8.0f}"
    )

    if result.bill_cross_check:
        bc = result.bill_cross_check
        print(f"\n{SEP}")
        print("  BILL CROSS-CHECK")
        print(SEP)
        print(f"  Electricity bill (input)   : {bc['bill_kWh']:>10.0f}  kWh")
        print(f"  Calculated total           : {bc['calculated_kWh']:>10.0f}  kWh")
        print(f"  Difference                 : {bc['difference_%']:>10.1f}  %")
        print(f"  Status                     :  {bc['status']}")

    print(f"{SEP}\n")
