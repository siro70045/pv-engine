"""
school/climate_zones.py

Italian climate zones per DPR 412/93.
Provides thermal and electric energy benchmarks for schools,
heating season bounds, and auto-detection from latitude.
"""

from __future__ import annotations

# ── Zone definitions ──────────────────────────────────────────────────────────
# heating_months: (start_month, end_month) inclusive, e.g. (10, 4) = Oct-Apr
# heating_hours_day: max heating operation hours/day allowed by law

CLIMATE_ZONES: dict[str, dict] = {
    "A": {
        "gdd_range": (0, 600),
        "label": "Zone A — < 600 GDD (Lampedusa, Pantelleria)",
        "heating_months": (12, 3),
        "heating_hours_day": 6,
    },
    "B": {
        "gdd_range": (601, 900),
        "label": "Zone B — 601–900 GDD (Palermo, Catania, Reggio Calabria)",
        "heating_months": (12, 3),
        "heating_hours_day": 8,
    },
    "C": {
        "gdd_range": (901, 1400),
        "label": "Zone C — 901–1400 GDD (Naples, Bari, Cagliari)",
        "heating_months": (11, 3),
        "heating_hours_day": 10,
    },
    "D": {
        "gdd_range": (1401, 2100),
        "label": "Zone D — 1401–2100 GDD (Rome, Florence, Genoa)",
        "heating_months": (11, 4),
        "heating_hours_day": 12,
    },
    "E": {
        "gdd_range": (2101, 3000),
        "label": "Zone E — 2101–3000 GDD (Milan, Turin, Bologna, Venice)",
        "heating_months": (10, 4),
        "heating_hours_day": 14,
    },
    "F": {
        "gdd_range": (3001, 9999),
        "label": "Zone F — > 3000 GDD (Bolzano, Trento, Aosta)",
        "heating_months": (10, 4),
        "heating_hours_day": 14,
    },
}

# ── Electric benchmarks [kWh/m²/yr] ──────────────────────────────────────────
# Source: ENEA / RSE school energy audits, FIRE database.
# Keys: (zone, school_type)

SCHOOL_ELECTRIC_BENCHMARK: dict[tuple[str, str], float] = {
    # primary
    ("A", "primary"): 18, ("B", "primary"): 19, ("C", "primary"): 21,
    ("D", "primary"): 23, ("E", "primary"): 26, ("F", "primary"): 30,
    # middle
    ("A", "middle"): 20, ("B", "middle"): 21, ("C", "middle"): 24,
    ("D", "middle"): 27, ("E", "middle"): 31, ("F", "middle"): 36,
    # high_classic
    ("A", "high_classic"): 22, ("B", "high_classic"): 23, ("C", "high_classic"): 26,
    ("D", "high_classic"): 29, ("E", "high_classic"): 34, ("F", "high_classic"): 39,
    # high_tech (labs increase consumption)
    ("A", "high_tech"): 28, ("B", "high_tech"): 30, ("C", "high_tech"): 34,
    ("D", "high_tech"): 38, ("E", "high_tech"): 44, ("F", "high_tech"): 52,
    # university_dept
    ("A", "university_dept"): 35, ("B", "university_dept"): 37,
    ("C", "university_dept"): 42, ("D", "university_dept"): 47,
    ("E", "university_dept"): 55, ("F", "university_dept"): 65,
}

# ── Thermal benchmarks [kWh_th/m²/yr] ────────────────────────────────────────
# Source: CTI/UNI TS 11300 energy class thresholds for school buildings.
# Keys: (zone, construction_era)  era: "pre_1973" | "1973_1990" | "1991_2005" | "post_2005"

SCHOOL_THERMAL_BENCHMARK: dict[tuple[str, str], float] = {
    ("A", "pre_1973"): 25,  ("A", "1973_1990"): 18,  ("A", "1991_2005"): 12,  ("A", "post_2005"): 8,
    ("B", "pre_1973"): 40,  ("B", "1973_1990"): 28,  ("B", "1991_2005"): 18,  ("B", "post_2005"): 12,
    ("C", "pre_1973"): 65,  ("C", "1973_1990"): 48,  ("C", "1991_2005"): 30,  ("C", "post_2005"): 20,
    ("D", "pre_1973"): 90,  ("D", "1973_1990"): 65,  ("D", "1991_2005"): 42,  ("D", "post_2005"): 28,
    ("E", "pre_1973"): 120, ("E", "1973_1990"): 90,  ("E", "1991_2005"): 58,  ("E", "post_2005"): 38,
    ("F", "pre_1973"): 160, ("F", "1973_1990"): 120, ("F", "1991_2005"): 78,  ("F", "post_2005"): 52,
}


# ── Construction era classifier ───────────────────────────────────────────────

def classify_construction_era(year: int) -> str:
    """Return era key for SCHOOL_THERMAL_BENCHMARK lookup."""
    if year < 1973:
        return "pre_1973"
    if year < 1991:
        return "1973_1990"
    if year < 2006:
        return "1991_2005"
    return "post_2005"


# ── Zone auto-detection from latitude ────────────────────────────────────────

def zone_from_latitude(lat: float) -> str:
    """
    Rough climate zone estimate from latitude (mainland Italy).
    B  lat < 38.5  (coastal Sicily, Reggio Calabria, Palermo)
    C  lat < 41.5  (Naples, Bari, Catanzaro, Cagliari)
    D  lat < 44.5  (Rome, Florence, Genoa)
    E  lat < 46.0  (Milan, Turin, Venice, Bologna, Trento)
    F  lat >= 46.0 (Bolzano, Aosta, high-altitude Alpine areas)
    """
    if lat < 38.5:
        return "B"
    if lat < 41.5:
        return "C"
    if lat < 44.5:
        return "D"
    if lat < 46.0:
        return "E"
    return "F"


# ── Heating season ────────────────────────────────────────────────────────────

def get_heating_season(zone: str) -> tuple[int, int]:
    """
    Return (start_month, end_month) for the heating season in the given zone.
    E.g. (10, 4) means October through April inclusive.
    """
    return CLIMATE_ZONES[zone]["heating_months"]


# ── Benchmark getters ─────────────────────────────────────────────────────────

def get_thermal_benchmark(
    zone: str,
    construction_year: int,
    floor_area_m2: float,
) -> float:
    """
    Return annual thermal energy need [kWh_th/yr] for the building.
    Falls back to Zone E / pre_1973 if keys are missing.
    """
    era = classify_construction_era(construction_year)
    kwh_m2 = SCHOOL_THERMAL_BENCHMARK.get(
        (zone, era),
        SCHOOL_THERMAL_BENCHMARK.get(("E", era), 90.0),
    )
    return kwh_m2 * floor_area_m2


def get_electric_benchmark(
    zone: str,
    school_type: str,
    floor_area_m2: float,
) -> float:
    """
    Return annual electric energy benchmark [kWh_el/yr] for the building.
    Falls back to Zone E / high_tech if keys are missing.
    """
    kwh_m2 = SCHOOL_ELECTRIC_BENCHMARK.get(
        (zone, school_type),
        SCHOOL_ELECTRIC_BENCHMARK.get(("E", school_type), 44.0),
    )
    return kwh_m2 * floor_area_m2
