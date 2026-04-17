"""
school/calendar_it.py

Italian school calendar for PV load profiling.
Reference year: 2023 (non-leap, Jan 1 = Sunday).
"""

from __future__ import annotations

import numpy as np

# ── Calendar constants ────────────────────────────────────────────────────────

DAYS_PER_MONTH    = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
DAYS_BEFORE_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def _doy(month: int, day: int) -> int:
    """0-indexed day of year."""
    return DAYS_BEFORE_MONTH[month - 1] + day - 1


def _dow(doy: int) -> int:
    """Day of week: 0=Monday … 6=Sunday. Jan 1 2023 = Sunday = 6."""
    return (6 + doy) % 7


def _doy_to_month(doy: int) -> int:
    for m in range(11, -1, -1):
        if doy >= DAYS_BEFORE_MONTH[m]:
            return m + 1
    return 1


# ── Vacation and holiday sets ─────────────────────────────────────────────────

# Christmas vacation: Dec 23 – Jan 7 (spanning two calendar years)
_CHRISTMAS_VACATION: set[int] = (
    set(range(0, 7))                        # Jan 1-7
    | set(range(_doy(12, 23), 365))         # Dec 23-31  (doy 356-364)
)

# Easter 2023 = April 9; Holy Thursday = April 6; Easter Tuesday = April 11
_EASTER_VACATION: set[int] = set(range(_doy(4, 6), _doy(4, 12)))  # 95-100 inclusive

# National public holidays
_NATIONAL_HOLIDAYS: set[int] = {
    _doy(1, 1),   # Capodanno
    _doy(1, 6),   # Epifania
    _doy(4, 25),  # Festa della Liberazione
    _doy(5, 1),   # Festa del Lavoro
    _doy(6, 2),   # Festa della Repubblica
    _doy(8, 15),  # Ferragosto
    _doy(11, 1),  # Ognissanti
    _doy(12, 8),  # Immacolata Concezione
    _doy(12, 25), # Natale
    _doy(12, 26), # Santo Stefano
}


def _is_holiday(doy: int) -> bool:
    return (doy in _CHRISTMAS_VACATION
            or doy in _EASTER_VACATION
            or doy in _NATIONAL_HOLIDAYS)


# ── School year bounds ────────────────────────────────────────────────────────
# Sep 11 = second Monday of September 2023  (doy 253)
# Jun 8  = last Friday of first week June   (doy 158)
# Jul 15 = university end                   (doy 195)

_SCHOOL_START = _doy(9, 11)   # 253
_SCHOOL_END   = _doy(6, 8)    # 158
_UNI_START    = _doy(9, 4)    # 246
_UNI_END      = _doy(7, 15)   # 195


def _in_school_year(doy: int, school_type: str) -> bool:
    """True if doy falls within the academic year (school in-session period)."""
    if school_type == "university_dept":
        start, end = _UNI_START, _UNI_END
    else:
        start, end = _SCHOOL_START, _SCHOOL_END
    # Academic year spans Sep→Jun/Jul: doy ≤ end  OR  doy ≥ start
    return doy <= end or doy >= start


def _is_school_day(doy: int, school_type: str) -> bool:
    """True if this is an in-session school day (not holiday, correct weekday)."""
    if not _in_school_year(doy, school_type):
        return False
    if _is_holiday(doy):
        return False
    dow = _dow(doy)
    if school_type == "primary":
        return dow <= 4           # Mon-Fri
    if school_type == "university_dept":
        return dow <= 5           # Mon-Sat
    # middle, high_tech, high_classic: Mon-Sat
    return dow <= 5


# ── Daily occupancy profiles ──────────────────────────────────────────────────
# All profiles are 24 values: occupancy fraction for each hour 0-23.

def _make_school_day_profile(
    school_type: str,
    full_time: bool,
    is_lab_day: bool,
    is_saturday: bool,
) -> list[float]:
    """24-value daily occupancy profile for a school day."""
    p = [0.02] * 24
    p[7] = 0.15   # apertura: personale ATA

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
            p[14] = 0.10   # personale rimasto

    elif school_type == "university_dept":
        if is_saturday:
            for h in range(8, 12): p[h] = 1.00
            p[12] = 0.80
            p[13] = 0.30
        else:
            for h in range(8, 19): p[h] = 1.00
            p[19] = 0.20

    return p


# ── Public API ────────────────────────────────────────────────────────────────

def build_school_calendar(
    school_type: str,
    extra_evening: bool = False,
    extra_weekend: bool = False,
    full_time: bool = False,
) -> np.ndarray:
    """
    Returns an 8760-element array of hourly occupancy fractions [0.0–1.0].

    Parameters
    ----------
    school_type : str
        "primary" | "middle" | "high_tech" | "high_classic" | "university_dept"
    extra_evening : bool
        +0.40 occupancy Mon–Fri 18:00–22:00 (corsi serali, palestra, riunioni).
    extra_weekend : bool
        +0.25 occupancy Sat 9:00–17:00, Sun 9:00–13:00 (eventi, gare).
    full_time : bool
        Full-time schedule for primary school (until 16:00).
    """
    result = np.zeros(8760)
    _LAB_DOWS = {0, 2}   # Monday=0, Wednesday=2

    for doy in range(365):
        dow      = _dow(doy)
        is_sat   = dow == 5
        is_sun   = dow == 6
        in_sy    = _in_school_year(doy, school_type)
        holiday  = _is_holiday(doy)
        school_d = _is_school_day(doy, school_type)

        if school_d:
            is_lab  = (school_type == "high_tech" and dow in _LAB_DOWS)
            profile = _make_school_day_profile(school_type, full_time, is_lab, is_sat)
        elif in_sy and holiday and not is_sat and not is_sun:
            profile = [0.02] * 24   # holiday during school year: guardiania
        elif not in_sy:
            profile = [0.05] * 24   # summer: personale ATA minimo
        else:
            profile = [0.02] * 24   # weekend during school year (no extras yet)

        # Extra evening (Mon-Fri, school year, non-holiday)
        if extra_evening and not is_sat and not is_sun and in_sy and not holiday:
            for h in range(18, 22):
                profile[h] = max(profile[h], 0.40)

        # Extra weekend (Sat/Sun, school year)
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


def school_days_per_month(school_type: str = "high_tech") -> list[int]:
    """
    Returns the number of effective school days per month [12 values].
    Useful for scaling monthly energy consumption estimates.
    """
    counts = [0] * 12
    for doy in range(365):
        if _is_school_day(doy, school_type):
            m = _doy_to_month(doy) - 1
            counts[m] += 1
    return counts
