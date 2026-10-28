"""Points over water (e.g. Venice's centroid, in the lagoon) fall back to the
nearest point on land instead of failing the whole estimate."""
import io
import urllib.error

import pytest

from api.utils import cache
from engine import pvgis_client
from engine.pvgis_client import PVGISError, PVGISLocationError


def _patch(monkeypatch, land, error=PVGISLocationError):
    calls = []

    def fake_fetch(lat, lon, use_cache=True):
        calls.append((lat, lon))
        if (round(lat, 4), round(lon, 4)) in land:
            return ("TMY", lat, lon)
        raise error("Location over the sea. Please, select another location")

    monkeypatch.setattr(cache, "fetch_tmy", fake_fetch)
    monkeypatch.setattr(cache, "_load_from_disk", lambda a, b: None)
    monkeypatch.setattr(cache, "_save_to_disk", lambda a, b, c: None)
    cache.clear_cache()
    return calls


def test_point_on_land_is_fetched_directly(monkeypatch):
    calls = _patch(monkeypatch, {(44.6, 10.64)})
    tmy, _ = cache.get_cached_tmy(44.601, 10.636)
    assert tmy == ("TMY", 44.6, 10.64)
    assert calls == [(44.6, 10.64)]


def test_point_over_water_uses_nearest_land(monkeypatch):
    # Venice: centroid (45.4669, 12.3459) rounds to (45.47, 12.35), in the lagoon.
    calls = _patch(monkeypatch, {(45.45, 12.35)})
    tmy, _ = cache.get_cached_tmy(45.466922, 12.345931)
    assert tmy == ("TMY", 45.45, 12.35)
    assert calls == [(45.47, 12.35), (45.49, 12.35), (45.45, 12.35)]


def test_no_land_within_range_raises_location_error(monkeypatch):
    calls = _patch(monkeypatch, set())
    with pytest.raises(PVGISLocationError):
        cache.get_cached_tmy(40.0, 15.0)
    assert len(calls) == 1 + 4 * 8


def test_other_pvgis_errors_are_not_retried_nearby(monkeypatch):
    calls = _patch(monkeypatch, set(), error=PVGISError)
    with pytest.raises(PVGISError):
        cache.get_cached_tmy(44.6, 10.64)
    assert len(calls) == 1


def _http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "Bad Request", {}, io.BytesIO(body))


def test_client_recognises_over_sea_message(monkeypatch):
    body = b'{"status": 400, "message": "Location over the sea. Please, select another location"}'

    def fake_urlopen(*a, **k):
        raise _http_error(400, body)

    monkeypatch.setattr(pvgis_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(PVGISLocationError, match="over the sea"):
        pvgis_client._fetch_raw("https://x")


def test_client_other_http_errors_stay_generic(monkeypatch):
    def fake_urlopen(*a, **k):
        raise _http_error(500, b"Internal Server Error")

    monkeypatch.setattr(pvgis_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(PVGISError) as exc:
        pvgis_client._fetch_raw("https://x")
    assert not isinstance(exc.value, PVGISLocationError)
