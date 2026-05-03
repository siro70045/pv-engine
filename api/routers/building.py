"""Building endpoints: OSM nearby, roof estimate, thermal factor."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter
from api.schemas.building import (
    OSMBuildingRequest, OSMBuildingResponse, OSMBuilding,
    RoofEstimateRequest, RoofEstimateResponse,
    ThermalFactorRequest, ThermalFactorResponse,
)
from api.utils.thermal import thermal_factor as compute_thermal_factor
from buildings import fetch_buildings, polygon_area_m2, kwp_from_area

logger = logging.getLogger("pv_engine_api.building")

router = APIRouter(prefix="/building", tags=["building"])


@router.post("/osm-nearby", response_model=OSMBuildingResponse,
             summary="Fetch OSM building polygons within a radius")
@limiter.limit("30/minute")
async def osm_nearby(request: Request, req: OSMBuildingRequest) -> OSMBuildingResponse:
    try:
        polys = fetch_buildings(req.lat, req.lon, radius_m=req.radius_m)
    except Exception as e:   # broad: Overpass network failure is non-critical
        logger.warning("OSM fetch failed: %s", e)
        polys = []

    out: list[OSMBuilding] = []
    for b in polys:
        coords = [[float(lat), float(lon)] for lat, lon in b.coords]
        area = polygon_area_m2(b.coords)
        out.append(OSMBuilding(
            osm_id        = b.osm_id,
            polygon       = coords,
            centroid_lat  = b.centroid_lat,
            centroid_lon  = b.centroid_lon,
            area_m2       = round(area, 1),
            building_type = b.building_type,
            n_floors      = b.n_floors,
        ))
    return OSMBuildingResponse(buildings=out, count=len(out))


@router.post("/roof-estimate", response_model=RoofEstimateResponse,
             summary="Estimate usable roof area and max kWp")
async def roof_estimate(req: RoofEstimateRequest) -> RoofEstimateResponse:
    n_floors = max(1, req.n_floors)
    footprint = req.floor_area_m2 / n_floors
    notes: list[str] = []

    util_share = req.roof_share_pct / 100.0
    # 0.75 accounts for skylights, technical equipment, walkways, inter-row spacing
    practical_factor = 0.75
    usable = footprint * util_share * practical_factor

    # Use the existing helper for kWp estimate (m²/kWp varies by mounting)
    kwp_estimate = kwp_from_area(footprint * util_share, req.mounting,
                                 roof_share_pct=100.0)

    notes.append(f"Footprint = {req.floor_area_m2:.0f} m² / {n_floors} floors = {footprint:.0f} m².")
    notes.append(
        f"Usable area = footprint × {req.roof_share_pct:.0f}% share × 75% practical "
        f"= {usable:.0f} m²."
    )
    notes.append(
        f"Max kWp ({req.mounting}) ≈ {kwp_estimate:.1f} kWp using the engine's tier "
        f"(roof_integrated 0.80, flat_roof 0.60, ground 0.90, bipv 0.70)."
    )
    if req.building_type == "school":
        notes.append("Schools: factor in fire-escape paths and PV-free safety strips.")

    return RoofEstimateResponse(
        footprint_m2=round(footprint, 1),
        usable_roof_m2=round(usable, 1),
        max_kwp_estimate=round(kwp_estimate, 1),
        notes=notes,
    )


@router.post("/thermal-factor", response_model=ThermalFactorResponse,
             summary="Heat-loss correction factor for residential subtypes")
async def thermal_factor(req: ThermalFactorRequest) -> ThermalFactorResponse:
    factor, desc = compute_thermal_factor(
        req.building_subtype, req.floor_number, req.is_top_floor,
    )
    return ThermalFactorResponse(thermal_factor=factor, description=desc)
