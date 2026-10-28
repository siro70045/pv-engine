"""Two-level TMY cache shared across granular endpoints.

PVGIS responses are large and slow to fetch (1–8 s). To keep latency low and
avoid hammering PVGIS, we cache them at two levels:

1. **In-memory LRU** — keyed on coordinates rounded to 0.01° (~1.1 km), so
   nearby clicks reuse the same fetch within a single process.
2. **On-disk JSON** — survives Railway restarts (the LRU is wiped on every
   redeploy). Written to ``PVGIS_CACHE_DIR`` (default ``/tmp/pvgis_cache``).

Order of operations on a lookup:
    memory hit → disk hit (rebuild + populate memory) → live PVGIS fetch
    (populate both layers).

A best-effort write is attempted — read-only filesystems and quota errors are
swallowed so the API still serves the response.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from engine.pvgis_client import PVGISLocationError, TMYResult, _tmy_from_dict, fetch_tmy

logger = logging.getLogger("pv_engine_api.cache")

CACHE_MAXSIZE = int(os.getenv("PVGIS_CACHE_SIZE", "500"))
CACHE_DIR = Path(os.getenv("PVGIS_CACHE_DIR", "/tmp/pvgis_cache"))

try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logger.warning("Could not create cache dir %s: %s", CACHE_DIR, e)


def _disk_path(lat_r: float, lon_r: float) -> Path:
    return CACHE_DIR / f"tmy_{lat_r:.2f}_{lon_r:.2f}.json"


def _load_from_disk(lat_r: float, lon_r: float) -> Optional[TMYResult]:
    path = _disk_path(lat_r, lon_r)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _tmy_from_dict(data)
    except Exception as e:
        logger.warning("Disk cache miss (corrupt %s): %s", path.name, e)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _save_to_disk(lat_r: float, lon_r: float, tmy: TMYResult) -> None:
    path = _disk_path(lat_r, lon_r)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tmy.to_dict(), f, separators=(",", ":"))
    except OSError as e:
        # Read-only filesystem (some Railway deploys), quota, etc. — just skip.
        logger.warning("Could not persist cache to %s: %s", path, e)


@lru_cache(maxsize=CACHE_MAXSIZE)
def _memory_cache(lat_r: float, lon_r: float) -> TMYResult:
    """Inner LRU. On miss, falls through to disk → PVGIS."""
    disk = _load_from_disk(lat_r, lon_r)
    if disk is not None:
        return disk
    # Bypass pvgis_client's own disk cache — we manage persistence here.
    try:
        tmy = fetch_tmy(lat=lat_r, lon=lon_r, use_cache=False)
    except PVGISLocationError:
        tmy = _fetch_nearest_land(lat_r, lon_r)
    _save_to_disk(lat_r, lon_r, tmy)
    return tmy


# Some municipal centroids fall in water (Venice's lies in the lagoon), and
# PVGIS refuses points over the sea. We then search outward on rings of eight
# points roughly 2, 4, 6 and 8 km away and use the first point PVGIS accepts.
# At that distance the typical weather year is practically the same; the shift
# is logged. If no land is found, the original error is raised.
_LAND_SEARCH_STEPS_DEG = (0.02, 0.04, 0.06, 0.08)
_LAND_SEARCH_DIRECTIONS = (
    (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1),
)


def _fetch_nearest_land(lat: float, lon: float) -> TMYResult:
    last_error: Optional[PVGISLocationError] = None
    for step in _LAND_SEARCH_STEPS_DEG:
        for dlat, dlon in _LAND_SEARCH_DIRECTIONS:
            lat_t = round(lat + dlat * step, 4)
            lon_t = round(lon + dlon * step, 4)
            try:
                tmy = fetch_tmy(lat=lat_t, lon=lon_t, use_cache=False)
            except PVGISLocationError as e:
                last_error = e
                continue
            logger.warning(
                "PVGIS: (%.2f, %.2f) is over water; using nearest land point (%.4f, %.4f)",
                lat, lon, lat_t, lon_t,
            )
            return tmy
    assert last_error is not None
    raise last_error


def get_cached_tmy(lat: float, lon: float) -> tuple[TMYResult, bool]:
    """Return ``(tmy, was_cached)``. ``was_cached=True`` when the in-memory LRU
    served the response (no PVGIS call this turn — though disk reads count as
    "not cached" by this flag, since the LRU was missed)."""
    key = (round(lat, 2), round(lon, 2))
    info_before = _memory_cache.cache_info()
    tmy = _memory_cache(*key)
    info_after = _memory_cache.cache_info()
    was_cached = info_after.hits > info_before.hits
    return tmy, was_cached


def cache_size() -> int:
    return _memory_cache.cache_info().currsize


def clear_cache() -> None:
    _memory_cache.cache_clear()


def cache_stats() -> dict:
    info = _memory_cache.cache_info()
    try:
        disk_count = len(list(CACHE_DIR.glob("tmy_*.json")))
    except OSError:
        disk_count = 0
    return {
        "tmy_entries": info.currsize,
        "tmy_max": info.maxsize,
        "tmy_hits": info.hits,
        "tmy_misses": info.misses,
        "disk_entries": disk_count,
        "disk_dir": str(CACHE_DIR),
    }
