"""LRU TMY cache shared across granular endpoints.

PVGIS responses are large and slow to fetch (1–8 s). Endpoints that need TMY
data (`/location/tmy`, `/location/tmy/hourly`, `/pv/production`, ...) hit this
cache first, keyed on coordinates rounded to 0.01° (~1.1 km) so that nearby
clicks reuse the same fetch.
"""

from __future__ import annotations

from functools import lru_cache

from engine.pvgis_client import TMYResult, fetch_tmy

CACHE_MAXSIZE = 500


@lru_cache(maxsize=CACHE_MAXSIZE)
def _cached_tmy(lat_rounded: float, lon_rounded: float) -> TMYResult:
    return fetch_tmy(lat=lat_rounded, lon=lon_rounded, use_cache=True)


def get_cached_tmy(lat: float, lon: float) -> tuple[TMYResult, bool]:
    """Return (tmy, was_cached). `was_cached=True` means the in-memory LRU
    served the response (no PVGIS call this turn)."""
    key = (round(lat, 2), round(lon, 2))
    info_before = _cached_tmy.cache_info()
    tmy = _cached_tmy(*key)
    info_after = _cached_tmy.cache_info()
    was_cached = info_after.hits > info_before.hits
    return tmy, was_cached


def cache_size() -> int:
    return _cached_tmy.cache_info().currsize


def clear_cache() -> None:
    _cached_tmy.cache_clear()
