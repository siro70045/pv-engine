"""
school/calendar_it.py

Italian school calendar for PV load profiling.
Reference year: 2025 (non-leap, Jan 1 = Wednesday = DOW 2).

Supports per-region calendars for the 20 Italian regions, with the
Trentino-Alto Adige region split into its two autonomous provinces
(Trento and Bolzano), which have distinct school calendars — yielding
21 keys in REGIONAL_CALENDARS.

Region classification from (lat, lon) uses official ISTAT regional
polygons (school/data/regioni_istat.geojson, ~5 km simplified). The
previous bounding-box approximation produced ~22% misclassification on
a 94-city test set and was replaced on 2026-05-17 (anomaly #12).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

# ── Calendar constants ────────────────────────────────────────────────────────

DAYS_PER_MONTH    = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
DAYS_BEFORE_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def _doy(month: int, day: int) -> int:
    """0-indexed day of year."""
    return DAYS_BEFORE_MONTH[month - 1] + day - 1


def _dow(doy: int) -> int:
    """Day of week: 0=Monday … 6=Sunday. Jan 1 2025 = Wednesday = 2."""
    return (2 + doy) % 7


def _doy_to_month(doy: int) -> int:
    """1-indexed month from 0-indexed day of year."""
    for m in range(11, -1, -1):
        if doy >= DAYS_BEFORE_MONTH[m]:
            return m + 1
    return 1


def _day_of_year_to_month(day_idx: int) -> int:
    """0-indexed month from 0-indexed day of year."""
    days_cumulative = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365]
    for m in range(12):
        if day_idx < days_cumulative[m + 1]:
            return m
    return 11


# ── Easter calculation ────────────────────────────────────────────────────────

def _easter_date(year: int) -> date:
    """Calcola la data di Pasqua usando l'algoritmo di Gauss."""
    a = year % 19
    b = year % 4
    c = year % 7
    k = year // 100
    p = (13 + 8 * k) // 25
    q = k // 4
    M = (15 - p + k - q) % 30
    N = (4 + k - q) % 7
    d = (19 * a + M) % 30
    e = (2 * b + 4 * c + 6 * d + N) % 7
    if d + e < 10:
        day   = d + e + 22
        month = 3
    else:
        day   = d + e - 9
        month = 4
    # Correzioni speciali
    if day == 26 and month == 4:
        day = 19
    if day == 25 and month == 4 and d == 28 and e == 6 and a > 10:
        day = 18
    return date(year, month, day)


# ── Regional calendars ────────────────────────────────────────────────────────

# Anno scolastico 2025/2026. Valori approssimati; variazioni di 1-2 giorni
# sono comuni tra un anno scolastico e l'altro.
REGIONAL_CALENDARS: dict[str, dict] = {
    "abruzzo":        {"start": (9, 13), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (5, 10)},
    "basilicata":     {"start": (9, 15), "end": (6,  9),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (9, 20)},
    "calabria":       {"start": (9, 16), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (7, 26)},
    "campania":       {"start": (9, 15), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (9, 19)},
    "emilia_romagna": {"start": (9, 12), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (10, 4)},
    "friuli_vg":      {"start": (9, 10), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (4, 25)},
    "lazio":          {"start": (9, 15), "end": (6,  9),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (6, 29)},
    "liguria":        {"start": (9, 13), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (6, 24)},
    "lombardia":      {"start": (9, 12), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 2,
                       "patron_day": (12, 7)},
    "marche":         {"start": (9, 15), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (5,  4)},
    "molise":         {"start": (9, 15), "end": (6,  9),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (7, 17)},
    "piemonte":       {"start": (9, 11), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (6, 24)},
    "puglia":         {"start": (9, 16), "end": (6, 10),
                       "autumn_break": None, "carnival_days": 0,
                       "patron_day": (12, 6)},
    "sardegna":       {"start": (9, 18), "end": (6, 12),
                       "autumn_break": None, "carnival_days": 2,
                       "patron_day": (5,  1)},
    "sicilia":        {"start": (9, 15), "end": (6, 11),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (7, 15)},
    "toscana":        {"start": (9, 15), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (6, 24)},
    "trento":         {"start": (9,  9), "end": (6, 14),
                       "autumn_break": (10, 31, 11, 2),
                       "carnival_days": 2,
                       "patron_day": (6, 26)},
    "bolzano":        {"start": (9,  5), "end": (6, 14),
                       "autumn_break": (10, 30, 11,  8),   # ~10 giorni
                       "carnival_days": 2,
                       "patron_day": (8, 15)},
    "umbria":         {"start": (9, 13), "end": (6,  9),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (1, 29)},
    "valle_daosta":   {"start": (9, 11), "end": (6, 12),
                       "autumn_break": None, "carnival_days": 4,
                       "patron_day": (9,  7)},
    "veneto":         {"start": (9, 13), "end": (6,  8),
                       "autumn_break": None, "carnival_days": 1,
                       "patron_day": (4, 25)},
}

REGION_ALIASES: dict[str, str] = {
    # Linguistic alias only — Friuli-Venezia Giulia is a single region
    # with one school calendar, so this mapping is safe.
    "friuli_venezia_giulia": "friuli_vg",
}

# Regions that cannot be aliased because they have multiple calendars
# (must be specified explicitly).
_AMBIGUOUS_REGIONS: dict[str, str] = {
    "trentino_alto_adige":
        "Per il Trentino-Alto Adige specifica 'trento' o 'bolzano': "
        "le due province autonome hanno calendari scolastici distinti "
        "(Bolzano ha la pausa autunnale lunga 30/10–08/11).",
}


# ── Coordinate → region lookup (ISTAT polygons) ───────────────────────────────

# Map ISTAT region name (as in the GeoJSON `reg_name` property) to the
# region key used by REGIONAL_CALENDARS. Trentino-Alto Adige is mapped
# to a sentinel here and split into "trento"/"bolzano" by latitude in
# region_from_coordinates (ISTAT GeoJSON has TAA as a single polygon).
_ISTAT_NAME_TO_REGION_KEY: dict[str, str] = {
    "Abruzzo":                       "abruzzo",
    "Basilicata":                    "basilicata",
    "Calabria":                      "calabria",
    "Campania":                      "campania",
    "Emilia-Romagna":                "emilia_romagna",
    "Friuli-Venezia Giulia":         "friuli_vg",
    "Lazio":                         "lazio",
    "Liguria":                       "liguria",
    "Lombardia":                     "lombardia",
    "Marche":                        "marche",
    "Molise":                        "molise",
    "Piemonte":                      "piemonte",
    "Puglia":                        "puglia",
    "Sardegna":                      "sardegna",
    "Sicilia":                       "sicilia",
    "Toscana":                       "toscana",
    "Trentino-Alto Adige/Südtirol":  "trentino_alto_adige",  # split by lat
    "Umbria":                        "umbria",
    "Valle d'Aosta/Vallée d'Aoste":  "valle_daosta",
    "Veneto":                        "veneto",
}

# Latitude that separates Bolzano (north) from Trento (south).
# Trento city: 46.07°N · Bolzano city: 46.50°N. The provincial border
# runs roughly along 46.40°N; 46.45 keeps Bolzano city safely on its side.
_TAA_SPLIT_LAT: float = 46.45

# Module-level cache for polygons and spatial index.
_ITALIAN_REGIONS: list[tuple[str, "BaseGeometry"]] = []
_GEOJSON_PATH = Path(__file__).parent / "data" / "regioni_istat.geojson"


def _load_italian_regions() -> list[tuple[str, "BaseGeometry"]]:
    """Lazy-load ISTAT region polygons. Cached after first call."""
    global _ITALIAN_REGIONS
    if _ITALIAN_REGIONS:
        return _ITALIAN_REGIONS

    from shapely.geometry import shape

    with _GEOJSON_PATH.open() as f:
        gj = json.load(f)

    polys: list[tuple[str, "BaseGeometry"]] = []
    for feat in gj["features"]:
        istat_name = feat["properties"]["reg_name"]
        region_key = _ISTAT_NAME_TO_REGION_KEY[istat_name]
        polys.append((region_key, shape(feat["geometry"])))

    _ITALIAN_REGIONS = polys
    return _ITALIAN_REGIONS


def _resolve_taa(key: str, lat: float) -> str:
    """Split the TAA sentinel into 'bolzano' or 'trento' by latitude."""
    if key == "trentino_alto_adige":
        return "bolzano" if lat > _TAA_SPLIT_LAT else "trento"
    return key


def region_from_coordinates(lat: float, lon: float) -> str:
    """
    Italian region key for (lat, lon) using ISTAT regional polygons.

    Returns one of the 21 keys in REGIONAL_CALENDARS. For points that
    fall outside any Italian region polygon (sea, border, foreign land),
    returns the nearest region by polygon distance.

    Trentino-Alto Adige is split by latitude into the autonomous
    provinces of Bolzano (north of ~46.45°N) and Trento (south), since
    they have distinct school calendars.
    """
    from shapely.geometry import Point

    polys = _load_italian_regions()
    pt    = Point(lon, lat)

    for key, poly in polys:
        if poly.contains(pt):
            return _resolve_taa(key, lat)

    # Fallback: nearest region polygon. Covers sea points (Sicilia
    # offshore, Adriatic), border points, and any input outside Italy
    # (which is then snapped to the closest Italian region).
    nearest_key, _ = min(polys, key=lambda kp: kp[1].distance(pt))
    return _resolve_taa(nearest_key, lat)


# ── Vacation and holiday sets (legacy — 2025 reference) ──────────────────────

_CHRISTMAS_VACATION: set[int] = (
    set(range(0, 7))                        # Jan 1-7
    | set(range(_doy(12, 23), 365))         # Dec 23-31
)

# Easter 2026 for the default 2025/2026 school year
_EASTER_2026 = _easter_date(2026)
_EASTER_DOY  = _doy(_EASTER_2026.month, _EASTER_2026.day)
_EASTER_VACATION: set[int] = set(range(_EASTER_DOY - 3, _EASTER_DOY + 3))

_NATIONAL_HOLIDAYS: set[int] = {
    _doy(1,  1),   # Capodanno
    _doy(1,  6),   # Epifania
    _doy(4, 25),   # Festa della Liberazione
    _doy(5,  1),   # Festa del Lavoro
    _doy(6,  2),   # Festa della Repubblica
    _doy(8, 15),   # Ferragosto
    _doy(11, 1),   # Ognissanti
    _doy(12, 8),   # Immacolata Concezione
    _doy(12, 25),  # Natale
    _doy(12, 26),  # Santo Stefano
}


def _is_holiday(doy: int) -> bool:
    return (doy in _CHRISTMAS_VACATION
            or doy in _EASTER_VACATION
            or doy in _NATIONAL_HOLIDAYS)


# ── School year bounds (legacy Emilia-Romagna defaults) ──────────────────────

_SCHOOL_START = _doy(9, 12)   # Emilia-Romagna 2025
_SCHOOL_END   = _doy(6,  8)
_UNI_START    = _doy(9,  4)
_UNI_END      = _doy(7, 15)


def _in_school_year(doy: int, school_type: str) -> bool:
    if school_type == "university_dept":
        start, end = _UNI_START, _UNI_END
    else:
        start, end = _SCHOOL_START, _SCHOOL_END
    return doy <= end or doy >= start


def _is_school_day(doy: int, school_type: str) -> bool:
    if not _in_school_year(doy, school_type):
        return False
    if _is_holiday(doy):
        return False
    dow = _dow(doy)
    if school_type == "primary":
        return dow <= 4
    if school_type == "university_dept":
        return dow <= 5
    return dow <= 5


# ── Daily occupancy profiles ──────────────────────────────────────────────────

def _make_school_day_profile(
    school_type: str,
    full_time: bool,
    is_lab_day: bool,
    is_saturday: bool,
) -> list[float]:
    """24-value daily occupancy profile for a school day."""
    p = [0.02] * 24
    p[7] = 0.15

    if school_type == "primary":
        if full_time:
            for h in range(8, 13): p[h] = 1.00
            p[13] = 0.90
            p[14] = 0.80
            p[15] = 0.60
            p[16] = 0.20
        else:
            for h in range(8, 12): p[h] = 1.00
            p[12] = 0.80
            p[13] = 0.30

    elif school_type in ("middle", "high_classic"):
        for h in range(8, 12): p[h] = 1.00
        p[12] = 0.80
        p[13] = 0.30

    elif school_type == "high_tech":
        for h in range(8, 12): p[h] = 1.00
        p[12] = 0.80
        if is_saturday:
            p[13] = 0.20
        elif is_lab_day:
            p[13] = 0.80
            for h in range(14, 17): p[h] = 0.10
        else:
            p[13] = 0.30
            p[14] = 0.10

    elif school_type == "university_dept":
        if is_saturday:
            for h in range(8, 12): p[h] = 1.00
            p[12] = 0.80
            p[13] = 0.30
        else:
            for h in range(8, 19): p[h] = 1.00
            p[19] = 0.20

    return p


# ── Regional vacation builder ─────────────────────────────────────────────────

def _build_regional_vacations(
    region: str,
    reference_year: int,
) -> tuple[set[int], int, int]:
    """
    Returns (vacation_doy_set, school_start_doy, school_end_doy).
    Easter vacation is computed for reference_year + 1 (second half of school year).
    """
    if region in _AMBIGUOUS_REGIONS:
        raise ValueError(_AMBIGUOUS_REGIONS[region])
    reg_key = REGION_ALIASES.get(region, region)
    reg     = REGIONAL_CALENDARS.get(reg_key, REGIONAL_CALENDARS["emilia_romagna"])

    school_start = _doy(*reg["start"])
    school_end   = _doy(*reg["end"])

    # Easter of the year that contains the spring term
    easter      = _easter_date(reference_year + 1)
    easter_doy  = _doy(easter.month, easter.day)
    vacations   = (
        _CHRISTMAS_VACATION
        | _NATIONAL_HOLIDAYS
        | set(range(easter_doy - 3, easter_doy + 3))
    )

    # Autumn break (Bolzano / Trento)
    if reg["autumn_break"]:
        ab        = reg["autumn_break"]
        ab_start  = _doy(ab[0], ab[1])
        ab_end    = _doy(ab[2], ab[3])
        vacations = vacations | set(range(ab_start, ab_end + 1))

    # Carnival: carnival_days days ending on Martedì Grasso (Easter - 47)
    if reg["carnival_days"] > 0:
        martedi   = easter_doy - 47
        c_start   = martedi - reg["carnival_days"] + 1
        vacations = vacations | set(range(c_start, martedi + 1))

    # Patron day (skip if weekend or already in national holidays)
    if reg["patron_day"]:
        p_doy = _doy(*reg["patron_day"])
        if _dow(p_doy) <= 4 and p_doy not in _NATIONAL_HOLIDAYS:
            vacations = vacations | {p_doy}

    return vacations, school_start, school_end


# ── Public API ────────────────────────────────────────────────────────────────

def build_school_calendar(
    school_type: str,
    extra_evening: bool = False,
    extra_weekend: bool = False,
    full_time: bool = False,
    region: str = "emilia_romagna",
    reference_year: int = 2025,
) -> np.ndarray:
    """
    Returns an 8760-element array of hourly occupancy fractions [0.0–1.0].

    Parameters
    ----------
    school_type : str
        "primary" | "middle" | "high_tech" | "high_classic" | "university_dept"
    extra_evening : bool
        +0.40 occupancy Mon–Fri 18:00–22:00.
    extra_weekend : bool
        +0.25 occupancy Sat 9:00–17:00, Sun 9:00–13:00.
    full_time : bool
        Full-time schedule for primary school (until 16:00).
    region : str
        Italian region key from REGIONAL_CALENDARS.
    reference_year : int
        First year of the school year (e.g. 2025 → 2025/2026).
    """
    if school_type == "university_dept":
        # University keeps legacy bounds (national, no regional variation)
        vacations    = _CHRISTMAS_VACATION | _NATIONAL_HOLIDAYS | _EASTER_VACATION
        school_start = _UNI_START
        school_end   = _UNI_END
    else:
        vacations, school_start, school_end = _build_regional_vacations(
            region, reference_year
        )

    def _in_sy(doy: int) -> bool:
        return doy <= school_end or doy >= school_start

    def _is_regional_school_day(doy: int, dow: int) -> bool:
        if not _in_sy(doy):
            return False
        if doy in vacations:
            return False
        if school_type == "primary":
            return dow <= 4
        return dow <= 5   # Mon-Sat for all other types

    result   = np.zeros(8760)
    _LAB_DOWS = {0, 2}

    for doy in range(365):
        dow      = _dow(doy)
        is_sat   = dow == 5
        is_sun   = dow == 6
        in_sy    = _in_sy(doy)
        holiday  = doy in vacations
        school_d = _is_regional_school_day(doy, dow)

        if school_d:
            is_lab  = school_type == "high_tech" and dow in _LAB_DOWS
            profile = _make_school_day_profile(school_type, full_time, is_lab, is_sat)
        elif in_sy and holiday and not is_sat and not is_sun:
            profile = [0.02] * 24   # guardiania
        elif not in_sy:
            profile = [0.05] * 24   # estate / fuori anno
        else:
            profile = [0.02] * 24   # weekend in anno scolastico

        if extra_evening and not is_sat and not is_sun and in_sy and not holiday:
            for h in range(18, 22):
                profile[h] = max(profile[h], 0.40)

        if extra_weekend and in_sy:
            if is_sat:
                for h in range(9, 17):
                    profile[h] = max(profile[h], 0.25)
            elif is_sun:
                for h in range(9, 13):
                    profile[h] = max(profile[h], 0.25)

        base = doy * 24
        for h in range(24):
            result[base + h] = profile[h]

    return result


def school_days_per_month(
    school_type: str = "high_tech",
    region: str = "emilia_romagna",
    reference_year: int = 2025,
) -> list[int]:
    """
    Returns the number of effective school days per month [12 values].
    A day counts as a school day if it has ≥ 4 hours with occupancy > 0.5.
    """
    cal    = build_school_calendar(school_type, False, False, False,
                                   region, reference_year)
    counts = [0] * 12
    for doy in range(365):
        day_hours = cal[doy * 24:(doy + 1) * 24]
        if sum(1 for h in day_hours if h > 0.5) >= 4:
            m = _doy_to_month(doy) - 1
            counts[m] += 1
    return counts
