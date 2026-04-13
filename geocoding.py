"""
geocoding.py

Geocoding via Nominatim (OpenStreetMap) e calcolo dell'azimuth
dal click dell'utente sulla mappa.

Nessuna API key richiesta. Rispetta il rate limit Nominatim (max 1 req/s).

Funzioni pubbliche
------------------
geocode(address)        → GeoResult | None
calc_bearing(A, B)      → float [0–360°]
bearing_to_label(deg)   → str  (es. "Sud-Ovest (225°)")
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

try:
    import requests as _requests
    _USE_REQUESTS = True
except ImportError:
    import urllib.error
    import urllib.request
    _USE_REQUESTS = False


# ── Geocoding settings ───────────────────────────────────────────────────────
# Primary: Photon (komoot) — no API key, higher rate limits, OSM data
# Fallback: Nominatim — stricter limits (1 req/s), may 429 on shared IPs
_PHOTON_URL      = "https://photon.komoot.io/api/"
_NOMINATIM_URL   = "https://nominatim.openstreetmap.org/search"
_USER_AGENT      = "PVEngine/1.0 (solar-pv-analysis)"
_TIMEOUT_S       = 10
_LAST_REQUEST_TS = 0.0
_MIN_INTERVAL_S  = 0.5   # Photon is more lenient


@dataclass
class GeoResult:
    """Risultato di una geocodifica."""
    lat:          float
    lon:          float
    display_name: str       # indirizzo formattato restituito da Nominatim
    place_id:     int       # ID interno Nominatim
    importance:   float     # punteggio di rilevanza [0–1]; usato per ordinamento


class GeocodingError(Exception):
    """Errore durante la geocodifica."""


def geocode(
    address: str,
    country_codes: str = "it",
    limit: int = 5,
) -> list[GeoResult]:
    """
    Geocodes an address using Photon (primary) with Nominatim as fallback.
    Returns up to `limit` results ordered by relevance. Empty list if none found.

    Raises GeocodingError on network failure.
    """
    global _LAST_REQUEST_TS

    if not address or not address.strip():
        return []

    # ── Rate limiting ────────────────────────────────────────────────────────
    elapsed = time.time() - _LAST_REQUEST_TS
    if elapsed < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - elapsed)
    _LAST_REQUEST_TS = time.time()

    # ── Try Photon first ─────────────────────────────────────────────────────
    try:
        return _geocode_photon(address, country_codes, limit)
    except GeocodingError:
        pass   # fall through to Nominatim

    # ── Fallback: Nominatim ──────────────────────────────────────────────────
    return _geocode_nominatim(address, country_codes, limit)


def _geocode_photon(address: str, country_codes: str, limit: int) -> list[GeoResult]:
    """Geocode via Photon (komoot). Returns list[GeoResult]."""
    # Photon does not support country_codes filter natively;
    # we append the country hint to the query string instead.
    query = address.strip()
    if country_codes == "it":
        query = query  # Photon uses lang param, not country filter

    params = urllib.parse.urlencode({
        "q":     query,
        "limit": str(max(1, min(limit, 10))),
        "lang":  "en",
    })
    url = f"{_PHOTON_URL}?{params}"

    raw = _http_get(url)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeocodingError(f"Invalid Photon response: {e}") from e

    results: list[GeoResult] = []
    for feat in data.get("features", []):
        try:
            lon, lat = feat["geometry"]["coordinates"]
            props    = feat.get("properties", {})
            parts    = [props.get(k,"") for k in
                        ("name","street","housenumber","city","state","country")
                        if props.get(k)]
            display  = ", ".join(p for p in parts if p)
            results.append(GeoResult(
                lat          = float(lat),
                lon          = float(lon),
                display_name = display or query,
                place_id     = hash(f"{lat},{lon}") & 0xFFFFFF,
                importance   = 0.5,
            ))
        except (KeyError, ValueError, TypeError):
            continue

    return results


def _geocode_nominatim(address: str, country_codes: str, limit: int) -> list[GeoResult]:
    """Geocode via Nominatim. Raises GeocodingError on failure."""
    params: dict[str, str] = {
        "q":              address.strip(),
        "format":         "json",
        "limit":          str(max(1, min(limit, 10))),
        "addressdetails": "1",
    }
    if country_codes:
        params["countrycodes"] = country_codes

    url = f"{_NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    raw = _http_get(url, extra_headers={"Accept-Language": "it,en"})

    try:
        data: list[dict] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeocodingError(f"Invalid Nominatim response: {e}") from e

    results: list[GeoResult] = []
    for item in data:
        try:
            results.append(GeoResult(
                lat          = float(item["lat"]),
                lon          = float(item["lon"]),
                display_name = item.get("display_name", ""),
                place_id     = int(item.get("place_id", 0)),
                importance   = float(item.get("importance", 0.0)),
            ))
        except (KeyError, ValueError):
            continue

    return results


def _http_get(url: str, extra_headers: dict | None = None) -> str:
    """Performs a GET request, returns response text. Raises GeocodingError."""
    headers = {"User-Agent": _USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)

    try:
        if _USE_REQUESTS:
            resp = _requests.get(url, headers=headers, timeout=_TIMEOUT_S)
            resp.raise_for_status()
            return resp.text
        else:
            import urllib.request as _ur
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                return r.read().decode("utf-8")
    except Exception as e:
        msg  = str(e)
        name = type(e).__name__
        if "SSL" in msg or "certificate" in msg.lower():
            raise GeocodingError(
                f"SSL error ({name}). On macOS: run "
                f"'/Applications/Python 3.x/Install Certificates.command'."
            ) from e
        if "429" in msg or "Too Many" in msg:
            raise GeocodingError(
                f"Rate limit reached (429). The geocoding service is temporarily "
                f"unavailable. Please wait a few seconds and try again."
            ) from e
        raise GeocodingError(
            f"Cannot reach geocoding service: {msg}. "
            f"Check your internet connection."
        ) from e



def calc_bearing(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> float:
    """
    Calcola il bearing geografico dal punto A (lat1, lon1) al punto B (lat2, lon2).

    Usa la formula della rotta iniziale (forward azimuth) su ellissoide sferico.

    Returns
    -------
    float
        Angolo in gradi [0.0, 360.0).
        0° = Nord, 90° = Est, 180° = Sud, 270° = Ovest.

    Notes
    -----
    Questa è la formula corretta per coordinate geografiche.
    Non usare semplice atan2(dlat, dlon) perché la scala di latitudine e
    longitudine è diversa (1° lon ≈ 79 km a 44° N, 1° lat ≈ 111 km).
    """
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    Δλ = math.radians(lon2 - lon1)

    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)

    θ = math.degrees(math.atan2(x, y))
    return (θ + 360.0) % 360.0


def bearing_to_label(deg: float) -> str:
    """
    Converte un azimuth in gradi in un'etichetta leggibile.

    Examples
    --------
    >>> bearing_to_label(180)
    'Sud (180°)'
    >>> bearing_to_label(225)
    'Sud-Ovest (225°)'
    """
    deg = deg % 360.0
    # 16 settori di 22.5° ciascuno
    sectors = [
        (0,    "North"),
        (22.5, "North-North-East"),
        (45,   "North-East"),
        (67.5, "East-North-East"),
        (90,   "East"),
        (112.5,"East-South-East"),
        (135,  "South-East"),
        (157.5,"South-South-East"),
        (180,  "South"),
        (202.5,"South-South-West"),
        (225,  "South-West"),
        (247.5,"West-South-West"),
        (270,  "West"),
        (292.5,"West-North-West"),
        (315,  "North-West"),
        (337.5,"North-North-West"),
    ]
    # Trova il settore più vicino
    closest = min(sectors, key=lambda s: min(abs(deg - s[0]),
                                              360 - abs(deg - s[0])))
    return f"{closest[1]} ({deg:.0f}°)"


def azimuth_quality_note(azimuth_deg: float) -> tuple[str, str]:
    """
    Restituisce (livello, messaggio) sulla qualità dell'orientamento per il FV.

    Returns
    -------
    tuple[str, str]
        (livello, messaggio) dove livello è "good", "ok" o "warning".
    """
    # Deviazione dall'ottimale (Sud = 180°)
    dev = min(abs(azimuth_deg - 180), 360 - abs(azimuth_deg - 180))

    if dev <= 30:
        return "good", f"Optimal orientation (±{dev:.0f}° from South)"
    elif dev <= 60:
        loss_pct = dev * 0.15   # circa 0.15% di perdita per grado oltre 30°
        return "ok", (f"Acceptable orientation (±{dev:.0f}° from South, "
                      f"estimated loss ~{loss_pct:.0f}%)")
    else:
        loss_pct = 10 + (dev - 60) * 0.2
        return "warning", (f"Unfavourable orientation (±{dev:.0f}° from South, "
                           f"estimated loss ~{loss_pct:.0f}%)")
