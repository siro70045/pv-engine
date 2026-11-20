"""Google Solar API client — roof geometry for PV analysis.

Returns roofSegmentStats (pitchDegrees, azimuthDegrees, areaMeters2,
sunshineQuantiles) from buildingInsights:findClosest.

Design constraints:
- Silent failure: returns None on missing key, HTTP error, network error.
  Never blocks the main estimate flow.
- Cache: in-memory LRU keyed on (lat_r, lon_r, api_key), coordinates rounded
  to 0.00001° (~1 m) — tight enough to isolate individual buildings.
  (TMY cache uses 0.01° because climate is uniform at 1 km; roof geometry is
  per-building and must never be shared across neighbours.)
- dataLayers endpoint NOT available for EU accounts (tested 2026-09-28):
  hourlyShadeUrls and monthlyFluxUrl return 404. Shading is NOT modelled.
- sunshineQuantiles are stored and forwarded to the client but NOT used for
  segment scoring: the q_min pixel always contains edge/gutter geometry
  artifacts, inflating the spread on every real roof (98% trigger rate in
  field survey 2026-09-29). Segments are ranked by a real simulation of 1 kWp
  on their tilt/azimuth (see plan_segments); the caller supplies the simulator.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

logger = logging.getLogger("pv_engine.roof_google")

_BASE = "https://solar.googleapis.com/v1"
_COORD_ROUND = 5   # 0.00001° ≈ 1 m — isolates individual buildings


@dataclass
class RoofSegment:
    pitch_deg: float
    azimuth_deg: float
    area_m2: float
    ground_area_m2: float
    sunshine_quantiles: list[float] = field(default_factory=list)


@dataclass
class RoofData:
    segments: list[RoofSegment]
    imagery_quality: str      # "HIGH" | "MEDIUM" | "BASE"
    imagery_year: int
    imagery_date_str: str     # "YYYY-MM-DD" for display
    lat_rounded: float
    lon_rounded: float


def _round_coords(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, _COORD_ROUND), round(lon, _COORD_ROUND)


@lru_cache(maxsize=512)
def _cached_fetch(lat_r: float, lon_r: float, api_key: str) -> Optional[dict]:
    url = (
        f"{_BASE}/buildingInsights:findClosest"
        f"?location.latitude={lat_r}&location.longitude={lon_r}"
        f"&requiredQuality=LOW&key={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.info("Google Solar API HTTP %s for (%.5f, %.5f)", e.code, lat_r, lon_r)
        return None
    except Exception as e:
        logger.info("Google Solar API error for (%.5f, %.5f): %s", lat_r, lon_r, e)
        return None


def fetch_roof_data(lat: float, lon: float) -> Optional[RoofData]:
    """Return roof geometry from Google Solar API, or None on any failure."""
    api_key = os.getenv("GOOGLE_SOLAR_API_KEY", "").strip()
    if not api_key:
        logger.debug("GOOGLE_SOLAR_API_KEY not set — skipping roof fetch")
        return None
    lat_r, lon_r = _round_coords(lat, lon)
    raw = _cached_fetch(lat_r, lon_r, api_key)
    if not raw:
        return None
    sp = raw.get("solarPotential", {})
    rss = sp.get("roofSegmentStats", [])
    if not rss:
        logger.info("Google Solar: no roofSegmentStats for (%.5f, %.5f)", lat_r, lon_r)
        return None
    segments = []
    for seg in rss:
        stats = seg.get("stats", {})
        segments.append(RoofSegment(
            pitch_deg=float(seg.get("pitchDegrees", 30.0)),
            azimuth_deg=float(seg.get("azimuthDegrees", 180.0)),
            area_m2=float(stats.get("areaMeters2", 0.0)),
            ground_area_m2=float(stats.get("groundAreaMeters2", 0.0)),
            sunshine_quantiles=list(stats.get("sunshineQuantiles", [])),
        ))
    idate = raw.get("imageryDate", {})
    year = int(idate.get("year", 0))
    month = int(idate.get("month", 1))
    day = int(idate.get("day", 1))
    date_str = f"{year}-{month:02d}-{day:02d}" if year else "N/A"
    return RoofData(
        segments=segments,
        imagery_quality=raw.get("imageryQuality", "UNKNOWN"),
        imagery_year=year,
        imagery_date_str=date_str,
        lat_rounded=lat_r,
        lon_rounded=lon_r,
    )


# Segments whose specific yield (kWh/kWp) is below this fraction of the best
# segment are not used for PV. Our choice (audit 🔴): on the Albinea TMY east/west
# faces yield ~0.78-0.81 of south (kept), north faces ~0.56 (dropped).
MIN_YIELD_RATIO: float = 0.70


@dataclass
class SegmentPlan:
    """A roof segment usable for PV, with its simulated specific yield and capacity."""
    segment: RoofSegment
    specific_yield_kwh_kwp: float   # annual AC output of 1 kWp on this segment
    capacity_kwp: float             # installable kWp on this segment


def plan_segments(
    roof: RoofData,
    specific_yield: Callable[[RoofSegment], float],
    capacity: Callable[[RoofSegment], float],
    min_ratio: float = MIN_YIELD_RATIO,
) -> list[SegmentPlan]:
    """Rank segments by simulated specific yield and keep the usable ones.

    Replaces the old area × cosine heuristic, whose ratios were not physical
    (E/W 0.50 instead of ~0.80, N 0 instead of ~0.56 — audit §6.2). The caller
    supplies `specific_yield` (a real simulation of 1 kWp on the segment's tilt
    and azimuth) and `capacity` (installable kWp from the segment area).

    Returns the usable segments sorted best-first (ties: larger capacity first).
    Always contains at least the best segment.
    """
    scored = [
        SegmentPlan(seg, float(specific_yield(seg)), float(capacity(seg)))
        for seg in roof.segments
        if seg.area_m2 > 0
    ]
    if not scored:
        raise ValueError("roof has no segment with positive area")
    scored.sort(key=lambda p: (p.specific_yield_kwh_kwp, p.capacity_kwp), reverse=True)
    best = scored[0].specific_yield_kwh_kwp
    return [p for p in scored if p.specific_yield_kwh_kwp >= min_ratio * best]


def allocate_kwp(plan: list[SegmentPlan], kwp_total: float) -> list[tuple[SegmentPlan, float]]:
    """Fill segments best-first up to their capacity.

    Any kWp beyond the total capacity (user-forced size) goes on the best segment.
    Returns only segments that receive power.
    """
    if not plan:
        raise ValueError("empty segment plan")
    alloc: list[list] = []
    remaining = max(0.0, kwp_total)
    for p in plan:
        if remaining <= 1e-9:
            break
        k = min(remaining, p.capacity_kwp)
        if k > 1e-9:
            alloc.append([p, k])
            remaining -= k
    if remaining > 1e-9:
        if alloc and alloc[0][0] is plan[0]:
            alloc[0][1] += remaining
        else:
            alloc.insert(0, [plan[0], remaining])
    return [(p, k) for p, k in alloc]
