"""
Tests for day-of-week modulation of thermal loads in school/loads_school.py.

The modulation factors (_DOW_FACTORS_THERMAL) sum to 5.0, so the weekly
average is 1.0 — they redistribute thermal energy across weekdays without
changing the annual total. Non-thermal loads must remain unmodulated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from school.calendar_it import _dow
from school.loads_school import (
    _DOW_FACTORS_THERMAL,
    SchoolLoadsInput,
    calc_school_loads,
)


def _make_typical_school() -> SchoolLoadsInput:
    """Borzano-like primary school in zone E, heat pump heating, with AC."""
    return SchoolLoadsInput(
        floor_area_m2=3000.0,
        n_floors=3,
        construction_year=1981,
        school_type="primary",
        n_students=500,
        n_teachers=40,
        n_ata_staff=15,
        has_canteen=True,
        has_gym=True,
        has_it_lab=True,
        n_computers=30,
        heating_system="heat_pump",
        has_ac=True,
        ac_type="split",
        lighting_type="fluorescent",
        climate_zone="E",
        region="emilia_romagna",
    )


def _first_full_school_week_jan() -> list[int]:
    """
    Return five consecutive doy values [Mon..Fri] forming a fully-school
    week in January 2025. doy 12..16 = Mon Jan 13 .. Fri Jan 17.
    Avoids Jan 6 (Epiphany).
    """
    week = [12, 13, 14, 15, 16]
    expected_dow = [0, 1, 2, 3, 4]
    assert [_dow(d) for d in week] == expected_dow, (
        f"calendar drift: {[_dow(d) for d in week]} != {expected_dow}"
    )
    return week


def test_dow_factors_sum_to_five():
    """Sanity: the five weekday factors must sum to 5.0 so weekly avg = 1.0."""
    s = sum(_DOW_FACTORS_THERMAL.values())
    assert abs(s - 5.0) < 1e-9, f"factors sum to {s}, expected 5.0"
    assert set(_DOW_FACTORS_THERMAL.keys()) == {0, 1, 2, 3, 4}


def test_annual_total_preserved():
    """
    TEST A — Annual conservation.

    The dow modulation only redistributes per-category kWh inside `_dist()`,
    so the resulting annual total must match the un-scaled breakdown sum
    (within 0.5%).
    """
    inp = _make_typical_school()
    res = calc_school_loads(inp)

    breakdown_total = sum(res["breakdown"].values())
    annual_total = res["total_annual_kwh"]
    rel_err = abs(annual_total - breakdown_total) / breakdown_total
    assert rel_err < 0.005, (
        f"annual total {annual_total:.1f} kWh diverges from breakdown sum "
        f"{breakdown_total:.1f} by {rel_err:.4%}"
    )

    # And the hourly profile must integrate to the same total
    hourly_kwh = sum(res["hourly_demand_Wh"]) / 1000.0
    rel_err2 = abs(hourly_kwh - annual_total) / annual_total
    assert rel_err2 < 0.005, (
        f"sum(hourly_Wh)/1000 = {hourly_kwh:.1f} vs total_annual {annual_total:.1f}"
    )


def test_weekly_thermal_pattern_january():
    """
    TEST B — Weekly thermal pattern in January (heating active, no AC).

    Build the school profile, isolate the heating-only contribution by
    rebuilding category profiles, and check Mon > Wed > Fri with the
    expected magnitudes.
    """
    inp = _make_typical_school()
    inp.has_ac = False
    inp.ac_type = "none"
    res = calc_school_loads(inp)

    # Re-derive the heating component as fraction of total.
    # Easiest: inspect breakdown shares against weekly relative pattern.
    # We use the underlying weight array: rebuild via the public path by
    # comparing one week's heating-attributable demand.

    # Strategy: compute per-day heating Wh contribution by leveraging
    # category_shares from the full simulation: heating's share is constant
    # per hour pattern, but weight differs by dow. Easier — directly call
    # the internal builder.
    from school.loads_school import _build_category_profiles
    from school.calendar_it import build_school_calendar
    from school.climate_zones import get_heating_season

    cal = build_school_calendar(
        inp.school_type, inp.extra_evening, inp.extra_weekend, inp.full_time,
        region=inp.region,
    )
    school_days_arr = np.zeros(8760, dtype=bool)
    for doy in range(365):
        day_vals = cal[doy * 24:(doy + 1) * 24]
        if sum(1 for v in day_vals if v > 0.5) >= 4:
            school_days_arr[doy * 24:(doy + 1) * 24] = True

    # Probe with breakdown that has only heating non-zero so the resulting
    # 8760 array is purely the heating shape.
    heat_only_breakdown = {
        "lighting":   0.0,
        "computers":  0.0,
        "tech_lab":   0.0,
        "canteen":    0.0,
        "gym":        0.0,
        "admin":      0.0,
        "ac":         0.0,
        "heating_hp": 10000.0,
        "elevator":   0.0,
        "misc":       0.0,
        "dhw":        0.0,
    }
    heating_only = _build_category_profiles(
        inp, heat_only_breakdown, school_days_arr,
        get_heating_season("E"),
    )

    week = _first_full_school_week_jan()
    daily_sums = [
        float(heating_only[d * 24:(d + 1) * 24].sum()) for d in week
    ]
    mon, tue, wed, thu, fri = daily_sums

    # Confirm the qualitative pattern Mon > Wed > Fri
    assert mon > wed, f"Mon ({mon:.1f}) should exceed Wed ({wed:.1f})"
    assert wed > fri, f"Wed ({wed:.1f}) should exceed Fri ({fri:.1f})"

    # Expected ratios from _DOW_FACTORS_THERMAL.
    # Within the same week with constant heating activity, the weight ratios
    # should reproduce the factor ratios exactly (before final _dist scaling,
    # which is a single global multiplier and cancels in ratios).
    expected_mon_wed = 1.15 / 1.00
    expected_mon_fri = 1.15 / 0.85
    expected_wed_thu = 1.00 / 0.98

    assert abs(mon / wed - expected_mon_wed) < 0.01, (
        f"Mon/Wed ratio {mon / wed:.4f} != expected {expected_mon_wed:.4f}"
    )
    assert abs(mon / fri - expected_mon_fri) < 0.01, (
        f"Mon/Fri ratio {mon / fri:.4f} != expected {expected_mon_fri:.4f}"
    )
    assert abs(wed / thu - expected_wed_thu) < 0.05, (
        f"Wed/Thu ratio {wed / thu:.4f} != expected {expected_wed_thu:.4f}"
    )


def test_non_thermal_loads_not_modulated():
    """
    TEST C — Non-thermal loads (e.g. lighting) must NOT be modulated by dow.
    """
    inp = _make_typical_school()
    inp.has_ac = False
    inp.ac_type = "none"

    from school.loads_school import _build_category_profiles
    from school.calendar_it import build_school_calendar
    from school.climate_zones import get_heating_season

    cal = build_school_calendar(
        inp.school_type, inp.extra_evening, inp.extra_weekend, inp.full_time,
        region=inp.region,
    )
    school_days_arr = np.zeros(8760, dtype=bool)
    for doy in range(365):
        day_vals = cal[doy * 24:(doy + 1) * 24]
        if sum(1 for v in day_vals if v > 0.5) >= 4:
            school_days_arr[doy * 24:(doy + 1) * 24] = True

    light_only_breakdown = {
        "lighting":   10000.0,
        "computers":  0.0,
        "tech_lab":   0.0,
        "canteen":    0.0,
        "gym":        0.0,
        "admin":      0.0,
        "ac":         0.0,
        "heating_hp": 0.0,
        "elevator":   0.0,
        "misc":       0.0,
        "dhw":        0.0,
    }
    lighting_only = _build_category_profiles(
        inp, light_only_breakdown, school_days_arr,
        get_heating_season("E"),
    )

    week = _first_full_school_week_jan()
    daily = [float(lighting_only[d * 24:(d + 1) * 24].sum()) for d in week]
    mon, _tue, wed, _thu, fri = daily

    # Lighting daily sums on a same-shape week must be identical to within
    # 1% (they are equal up to floating-point noise — no dow factor applied).
    assert abs(mon - wed) / wed < 0.01, (
        f"Lighting Mon ({mon:.1f}) and Wed ({wed:.1f}) differ by "
        f"{abs(mon - wed) / wed:.4%}, must be within 1%"
    )
    assert abs(mon - fri) / fri < 0.01, (
        f"Lighting Mon ({mon:.1f}) and Fri ({fri:.1f}) differ by "
        f"{abs(mon - fri) / fri:.4%}, must be within 1%"
    )


def test_metabolic_reduction_dimensional_consistency_split():
    """
    Metabolic gains are produced in kWh thermal; HP electrical consumption
    is in kWh electrical. Subtracting them directly (pre-fix bug) overstates
    the reduction by a SCOP factor. Verify the post-fix formula:

        reduction_electrical = gains_thermal / SCOP
        heating_post         = heating_pre - reduction_electrical

    For ac_type in {split, none} the SCOP defaults to 3.2.
    Plausible reduction for a typical school: 5–20% on HP electrical
    consumption. The pre-fix bug produced 30–60% routinely.
    """
    inp = _make_typical_school()
    # ac_type defaults to "split" in the fixture → SCOP 3.2
    res = calc_school_loads(inp)

    info = res["internal_gains"]
    reduction_pct = info["reduction_pct"]
    gains_thermal = info["annual_kWh"]
    heating_post = res["breakdown"]["heating_hp"]

    SCOP = 3.2
    gains_electrical = gains_thermal / SCOP
    heating_pre = heating_post + gains_electrical
    expected_reduction_pct = (gains_electrical / heating_pre) * 100

    # Must match the dimensional formula (within rounding tolerance)
    assert abs(reduction_pct - expected_reduction_pct) < 0.5, (
        f"reduction_pct {reduction_pct:.2f}% != dimensional "
        f"{expected_reduction_pct:.2f}%. The pre-fix buggy formula "
        f"would produce ~{(gains_thermal / heating_pre) * 100:.2f}%."
    )

    # Plausibility range: a typical Italian school has metabolic gains
    # offsetting 5–20% of HP electrical demand. Anything above 25% is
    # symptomatic of a unit error.
    assert 5.0 <= reduction_pct <= 20.0, (
        f"reduction_pct {reduction_pct:.2f}% out of plausible range "
        f"[5%, 20%]. Pre-fix bug typically gave 30–40%."
    )


def test_metabolic_reduction_dimensional_consistency_central():
    """
    Same as the split variant, but with ac_type='central' → SCOP 2.8.
    Confirms the scop variable is used (not a hardcoded 3.2) in the
    metabolic conversion as well.
    """
    inp = _make_typical_school()
    inp.ac_type = "central"  # forces SCOP = 2.8 in calc_school_loads
    res = calc_school_loads(inp)

    info = res["internal_gains"]
    reduction_pct = info["reduction_pct"]
    gains_thermal = info["annual_kWh"]
    heating_post = res["breakdown"]["heating_hp"]

    SCOP = 2.8
    gains_electrical = gains_thermal / SCOP
    heating_pre = heating_post + gains_electrical
    expected_reduction_pct = (gains_electrical / heating_pre) * 100

    assert abs(reduction_pct - expected_reduction_pct) < 0.5, (
        f"reduction_pct {reduction_pct:.2f}% != dimensional "
        f"{expected_reduction_pct:.2f}% for ac_type='central' (SCOP=2.8). "
        f"If the test fails with a value matching SCOP=3.2 instead, the "
        f"scop variable is not being threaded through to the metabolic "
        f"conversion."
    )
