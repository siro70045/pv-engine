"""Sun position must be computed at the instant PVGIS irradiance refers to.

PVGIS/SARAH hourly values are not hour-centred: `irradiance_time_offset`
(0.1756 h at Albinea) is in the response. Using HH:30 instead skewed east/west
faces by about ±3 % (found 20 nov 2026).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.pvgis_client import _parse_tmy, _tmy_from_dict  # noqa: E402
from engine.solar import PVSystem, simulate_pv  # noqa: E402


def _raw(offset):
    hours = []
    for d in range(365):
        for h in range(24):
            # crude clear-ish day: irradiance only between 05 and 18 UTC
            sun = 1.0 if 5 <= h <= 18 else 0.0
            hours.append({
                "time(UTC)": f"2019{1 + d // 31 if d < 341 else 12:02d}{1 + d % 28:02d}:{h:02d}00",
                "T2m": 15.0, "RH": 60.0, "G(h)": 500.0 * sun, "Gb(n)": 600.0 * sun,
                "Gd(h)": 150.0 * sun, "IR(h)": 300.0, "WS10m": 2.0, "WD10m": 0.0, "SP": 101325.0,
            })
    loc = {"latitude": 44.6, "longitude": 10.6, "elevation": 170.0}
    if offset is not None:
        loc["irradiance_time_offset"] = offset
    return {"inputs": {"location": loc, "meteo_data": {"radiation_db": "PVGIS-SARAH2"}},
            "outputs": {"tmy_hourly": hours}}


def test_offset_parsed_and_roundtrips():
    tmy = _parse_tmy(_raw(0.1756))
    assert tmy.location.irradiance_time_offset_h == 0.1756
    assert _tmy_from_dict(tmy.to_dict()).location.irradiance_time_offset_h == 0.1756


def test_missing_offset_is_none():
    assert _parse_tmy(_raw(None)).location.irradiance_time_offset_h is None


def test_offset_changes_east_west_production():
    with_off = _parse_tmy(_raw(0.1756))
    legacy = copy.deepcopy(with_off)
    legacy.location.irradiance_time_offset_h = None   # hour centre (0.5)
    east = PVSystem(peak_power_kWp=3.0, tilt_deg=30, azimuth_deg=90)
    e_off = simulate_pv(with_off, east, mounting="close_roof").production_annual_kWh
    e_leg = simulate_pv(legacy, east, mounting="close_roof").production_annual_kWh
    assert abs(e_off - e_leg) / e_leg > 0.005, (e_off, e_leg)
