"""
buildings.py

Recupera le sagome degli edifici da OpenStreetMap tramite Overpass API
e calcola i centroidi con la formula di Gauss (shoelace).

Funzioni pubbliche
------------------
fetch_buildings(lat, lon, radius_m) → list[BuildingPolygon] | []
polygon_centroid(coords)            → (lat, lon)
closest_building(lat, lon, buildings) → BuildingPolygon | None
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests as _requests
    _USE_REQUESTS = True
except ImportError:
    _USE_REQUESTS = False

# ── Overpass settings ──────────────────────────────────────────────────────────
_OVERPASS_URL   = "https://overpass-api.de/api/interpreter"
_USER_AGENT     = "PVEngine/1.0 (analisi-fotovoltaica)"
_TIMEOUT_S      = 12
_LAST_REQ_TS    = 0.0
_MIN_INTERVAL_S = 1.0


@dataclass
class BuildingPolygon:
    """Un edificio OSM rappresentato come poligono geografico."""
    osm_id:       int
    coords:       list[tuple[float, float]]   # [(lat, lon), ...]  chiuso (primo == ultimo)
    centroid_lat: float
    centroid_lon: float
    tags:         dict[str, str] = field(default_factory=dict)

    @property
    def building_type(self) -> str:
        return self.tags.get("building", "yes")

    @property
    def n_floors(self) -> Optional[int]:
        v = self.tags.get("building:levels") or self.tags.get("levels")
        try:
            return int(v) if v else None
        except ValueError:
            return None


class OverpassError(Exception):
    """Errore durante la query Overpass."""


def polygon_centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    """
    Calcola il centroide di un poligono con la formula di Gauss (shoelace).

    Parameters
    ----------
    coords : list of (lat, lon) tuples
        Vertici del poligono — il primo e l'ultimo possono coincidere.

    Returns
    -------
    (centroid_lat, centroid_lon)

    Notes
    -----
    Usa lon come coordinata X e lat come Y per il calcolo planare.
    L'approssimazione è trascurabile per edifici (< 1 mm di errore).
    """
    # Rimuovi il punto di chiusura se coincide con il primo
    pts = coords[:-1] if (len(coords) > 1 and coords[0] == coords[-1]) else coords
    n = len(pts)

    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return pts[0]
    if n == 2:
        return (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2

    # Shoelace: X = lon, Y = lat
    area = 0.0
    cx   = 0.0   # centroide longitudine
    cy   = 0.0   # centroide latitudine

    for i in range(n):
        j  = (i + 1) % n
        xi = pts[i][1];  yi = pts[i][0]   # lon, lat
        xj = pts[j][1];  yj = pts[j][0]   # lon, lat
        cross = xi * yj - xj * yi
        area += cross
        cx   += (xi + xj) * cross
        cy   += (yi + yj) * cross

    area /= 2.0
    if abs(area) < 1e-12:
        # Fallback: media aritmetica (poligono degenere)
        return (sum(p[0] for p in pts) / n,
                sum(p[1] for p in pts) / n)

    cx /= 6.0 * area
    cy /= 6.0 * area
    return cy, cx   # (lat, lon)


def _overpass_query(query: str) -> dict:
    """
    Esegue una query Overpass QL e restituisce il JSON parsato.
    Usa `requests` se disponibile, altrimenti `urllib`.
    """
    global _LAST_REQ_TS
    elapsed = time.time() - _LAST_REQ_TS
    if elapsed < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - elapsed)

    data = urllib.parse.urlencode({"data": query}).encode()
    _LAST_REQ_TS = time.time()

    try:
        if _USE_REQUESTS:
            resp = _requests.post(
                _OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": _USER_AGENT},
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            return resp.json()
        else:
            req = urllib.request.Request(
                _OVERPASS_URL,
                data=data,
                method="POST",
                headers={"User-Agent": _USER_AGENT,
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))

    except Exception as e:
        msg = str(e)
        if "SSL" in msg or "certificate" in msg.lower():
            raise OverpassError(
                f"Errore SSL Overpass. Su macOS: "
                f"/Applications/Python 3.x/Install\\ Certificates.command"
            ) from e
        raise OverpassError(f"Overpass non raggiungibile: {msg}") from e


def fetch_buildings(
    lat:      float,
    lon:      float,
    radius_m: int = 150,
) -> list[BuildingPolygon]:
    """
    Recupera i poligoni degli edifici OSM entro `radius_m` metri da (lat, lon).

    Parameters
    ----------
    lat, lon  : coordinate del punto centrale (dal geocoding)
    radius_m  : raggio di ricerca in metri (default 150)

    Returns
    -------
    list[BuildingPolygon]
        Lista di edifici trovati, ordinata per distanza crescente dal punto.
        Lista vuota se nessun edificio trovato o se Overpass non è raggiungibile.

    Notes
    -----
    La query include sia `way["building"]` che `relation["building"]`
    per catturare edifici sia semplici che complessi (es. con cortili interni).
    Per le relazioni viene usata solo la parte outer.
    """
    query = f"""
[out:json][timeout:{_TIMEOUT_S}];
(
  way["building"](around:{radius_m},{lat},{lon});
  relation["building"](around:{radius_m},{lat},{lon});
);
out body;
>;
out skel qt;
""".strip()

    try:
        raw = _overpass_query(query)
    except OverpassError:
        return []   # Fallback silenzioso — la mappa funziona comunque senza poligoni

    elements = raw.get("elements", [])

    # ── Costruisce lookup nodi: {node_id → (lat, lon)} ──────────────────────
    nodes: dict[int, tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node":
            nodes[el["id"]] = (float(el["lat"]), float(el["lon"]))

    # ── Costruisce lookup ways: {way_id → [node_id, ...]} ───────────────────
    ways: dict[int, list[int]] = {}
    way_tags: dict[int, dict] = {}
    for el in elements:
        if el.get("type") == "way":
            ways[el["id"]]     = el.get("nodes", [])
            way_tags[el["id"]] = el.get("tags", {})

    # ── Processa edifici ─────────────────────────────────────────────────────
    buildings: list[BuildingPolygon] = []

    for el in elements:
        # --- Way semplice ---
        if el.get("type") == "way" and "building" in el.get("tags", {}):
            node_ids = el.get("nodes", [])
            coords   = [nodes[nid] for nid in node_ids if nid in nodes]
            if len(coords) < 3:
                continue
            c_lat, c_lon = polygon_centroid(coords)
            buildings.append(BuildingPolygon(
                osm_id      = el["id"],
                coords      = coords,
                centroid_lat= c_lat,
                centroid_lon= c_lon,
                tags        = el.get("tags", {}),
            ))

        # --- Relation (edificio complesso) ---
        elif el.get("type") == "relation" and "building" in el.get("tags", {}):
            # Usa solo i membri outer per il contorno esterno
            outer_coords: list[tuple[float, float]] = []
            for member in el.get("members", []):
                if member.get("role") == "outer" and member.get("type") == "way":
                    way_id = member.get("ref")
                    if way_id and way_id in ways:
                        for nid in ways[way_id]:
                            if nid in nodes:
                                outer_coords.append(nodes[nid])
            if len(outer_coords) < 3:
                continue
            c_lat, c_lon = polygon_centroid(outer_coords)
            buildings.append(BuildingPolygon(
                osm_id      = el["id"],
                coords      = outer_coords,
                centroid_lat= c_lat,
                centroid_lon= c_lon,
                tags        = el.get("tags", {}),
            ))

    # ── Ordina per distanza dal punto di geocoding ───────────────────────────
    def _dist(b: BuildingPolygon) -> float:
        dlat = (b.centroid_lat - lat) * 111_000
        dlon = (b.centroid_lon - lon) * 111_000 * math.cos(math.radians(lat))
        return math.hypot(dlat, dlon)

    buildings.sort(key=_dist)
    return buildings


def closest_building(
    lat:       float,
    lon:       float,
    buildings: list[BuildingPolygon],
) -> Optional[BuildingPolygon]:
    """
    Restituisce l'edificio il cui centroide è più vicino a (lat, lon).
    Restituisce None se la lista è vuota.
    """
    if not buildings:
        return None

    def _dist(b: BuildingPolygon) -> float:
        dlat = (b.centroid_lat - lat) * 111_000
        dlon = (b.centroid_lon - lon) * 111_000 * math.cos(math.radians(lat))
        return math.hypot(dlat, dlon)

    return min(buildings, key=_dist)


def polygon_area_m2(coords: list[tuple[float, float]]) -> float:
    """
    Calcola l'area in m² di un poligono geografico.
    Usa la formula shoelace in proiezione locale (piano tangente).
    Errore < 0.1% per edifici fino a ~1 km di lato.
    """
    import math
    if len(coords) < 3:
        return 0.0
    pts = list(coords)
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return 0.0
    lat_c  = sum(p[0] for p in pts) / len(pts)
    lat_m  = 111320.0
    lon_m  = 111320.0 * math.cos(math.radians(lat_c))
    xy     = [(p[1] * lon_m, p[0] * lat_m) for p in pts]
    n      = len(xy)
    area   = 0.0
    for i in range(n):
        j     = (i + 1) % n
        area += xy[i][0] * xy[j][1] - xy[j][0] * xy[i][1]
    return abs(area) / 2.0


def kwp_from_area(
    area_m2:       float,
    mounting_type: str,
    roof_share_pct: float = 100.0,
) -> float:
    """
    Stima la potenza installabile in kWp da un'area di tetto.

    Parameters
    ----------
    area_m2        : area lorda del tetto in m²
    mounting_type  : tipo di montaggio (flat_roof_ballast, roof_integrated, ...)
    roof_share_pct : percentuale del tetto di proprietà dell'utente [0–100]

    Returns
    -------
    float : kWp installabili (arrotondato a 0.5 kWp)
    """
    util = {
        "flat_roof_ballast": 0.60,   # spazio per accesso/sicurezza, ombra tra file
        "roof_integrated":   0.80,   # falda inclinata, alta densità
        "ground_fixed":      0.90,
        "bipv":              0.70,
    }.get(mounting_type, 0.70)

    m2_per_kwp = 5.5   # ~400 Wp panel (~2 m²) + spacing e corridoi
    raw = (area_m2 * util * roof_share_pct / 100.0) / m2_per_kwp
    # Arrotonda a 0.5 kWp
    return max(0.5, round(raw * 2) / 2)
