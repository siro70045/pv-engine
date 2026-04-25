"""Location endpoints: geocode, azimuth, TMY (summary + hourly), climate zone, region."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, status

from api.schemas.location import (
    GeocodeRequest,
    GeocodeResponse,
    GeocodeResultItem,
    AzimuthRequest,
    AzimuthResponse,
    LatLonRequest,
    TMYResponse,
    TMYHourlyResponse,
    ClimateZoneResponse,
    RegionResponse,
    RegionCalendarResponse,
)
from api.utils.cache import get_cached_tmy
from api.utils.compression import compress_hourly_temp
from engine.pvgis_client import PVGISError
from geocoding import (
    geocode as run_geocode,
    GeocodingError,
    calc_bearing,
    bearing_to_label,
    azimuth_quality_note,
)
from school.calendar_it import REGIONAL_CALENDARS, region_from_coordinates, _easter_date
from school.climate_zones import CLIMATE_ZONES, zone_from_latitude

logger = logging.getLogger("pv_engine_api.location")

router = APIRouter(prefix="/location", tags=["location"])


@router.get("/health", summary="Lightweight liveness probe")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/geocode", response_model=GeocodeResponse, summary="Address → coordinates")
async def geocode(req: GeocodeRequest) -> GeocodeResponse:
    try:
        results = run_geocode(req.query, country_codes="it", limit=req.limit)
    except GeocodingError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    out = [
        GeocodeResultItem(
            display_name=r.display_name,
            lat=r.lat, lon=r.lon,
            type="address",
            importance=r.importance,
        )
        for r in results
    ]
    return GeocodeResponse(results=out)


@router.post("/azimuth", response_model=AzimuthResponse, summary="Compute panel azimuth from map clicks")
async def azimuth(req: AzimuthRequest) -> AzimuthResponse:
    bearing = calc_bearing(req.building_lat, req.building_lon, req.sun_lat, req.sun_lon)
    label = bearing_to_label(bearing)
    quality, message = azimuth_quality_note(bearing)
    return AzimuthResponse(
        azimuth_deg=round(bearing, 1),
        direction_label=label,
        quality=quality,
        quality_message=message,
    )


@router.post("/tmy", response_model=TMYResponse, summary="PVGIS TMY summary (no hourly)")
async def tmy_summary(req: LatLonRequest) -> TMYResponse:
    try:
        tmy, was_cached = get_cached_tmy(req.lat, req.lon)
    except PVGISError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PVGIS: {e}")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    monthly_ghi = [round(v, 1) for v in tmy.monthly.GHI_monthly]
    monthly_t   = [round(v, 1) for v in tmy.monthly.T_avg]

    return TMYResponse(
        ghi_annual_kWh_m2  = round(sum(tmy.monthly.GHI_monthly), 1),
        temperature_mean_C = round(sum(tmy.monthly.T_avg) / 12.0, 1),
        ghi_monthly        = monthly_ghi,
        temperature_monthly= monthly_t,
        elevation_m        = tmy.location.elevation_m,
        source             = tmy.location.dataset,
        cached             = was_cached,
        latitude           = tmy.location.latitude,
        longitude          = tmy.location.longitude,
    )


@router.post("/tmy/hourly", response_model=TMYHourlyResponse,
             summary="PVGIS hourly arrays (8760 GHI + temperature)")
async def tmy_hourly(req: LatLonRequest) -> TMYHourlyResponse:
    try:
        tmy, _ = get_cached_tmy(req.lat, req.lon)
    except PVGISError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PVGIS: {e}")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    ghi_hourly = [int(round(rec.G_h)) for rec in tmy.hourly]
    t_hourly   = compress_hourly_temp(rec.T2m for rec in tmy.hourly)
    return TMYHourlyResponse(
        ghi_hourly_Wm2=ghi_hourly,
        temperature_hourly_C=t_hourly,
    )


@router.post("/climate-zone", response_model=ClimateZoneResponse,
             summary="DPR 412/93 climate zone from coordinates")
async def climate_zone(req: LatLonRequest) -> ClimateZoneResponse:
    zone = zone_from_latitude(req.lat)
    info = CLIMATE_ZONES[zone]
    start_m, end_m = info["heating_months"]
    # Build inclusive month list, wrapping at year boundary (e.g. (10,4) → 10..12,1..4)
    if start_m <= end_m:
        months = list(range(start_m, end_m + 1))
    else:
        months = list(range(start_m, 13)) + list(range(1, end_m + 1))
    return ClimateZoneResponse(
        zone=zone,
        zone_label=info["label"],
        heating_months=months,
        heating_hours_day=info["heating_hours_day"],
    )


def _format_calendar(reg_key: str, reference_year: int = 2025) -> RegionCalendarResponse:
    cal = REGIONAL_CALENDARS[reg_key]
    sm, sd = cal["start"]
    em, ed = cal["end"]
    start = date(reference_year, sm, sd).isoformat()
    end = date(reference_year + 1, em, ed).isoformat()

    autumn = None
    if cal.get("autumn_break"):
        m1, d1, m2, d2 = cal["autumn_break"]
        autumn = f"{date(reference_year, m1, d1).isoformat()}/{date(reference_year, m2, d2).isoformat()}"

    patron = None
    if cal.get("patron_day"):
        pm, pd = cal["patron_day"]
        # Patron day in autumn → reference_year, in spring → reference_year+1
        py = reference_year if pm >= 9 else reference_year + 1
        patron = date(py, pm, pd).isoformat()

    return RegionCalendarResponse(
        start_date=start,
        end_date=end,
        autumn_break=autumn,
        carnival_days=cal.get("carnival_days", 0),
        patron_day=patron,
    )


_REGION_LABELS = {
    "abruzzo": "Abruzzo", "basilicata": "Basilicata", "calabria": "Calabria",
    "campania": "Campania", "emilia_romagna": "Emilia-Romagna",
    "friuli_vg": "Friuli-Venezia Giulia", "lazio": "Lazio", "liguria": "Liguria",
    "lombardia": "Lombardia", "marche": "Marche", "molise": "Molise",
    "piemonte": "Piemonte", "puglia": "Puglia", "sardegna": "Sardegna",
    "sicilia": "Sicilia", "toscana": "Toscana", "trento": "Trentino",
    "bolzano": "Alto Adige", "umbria": "Umbria", "valle_daosta": "Valle d'Aosta",
    "veneto": "Veneto",
}


@router.post("/region", response_model=RegionResponse,
             summary="Italian region + school-year calendar from coordinates")
async def region(req: LatLonRequest) -> RegionResponse:
    reg_key = region_from_coordinates(req.lat, req.lon)
    if reg_key not in REGIONAL_CALENDARS:
        reg_key = "emilia_romagna"
    return RegionResponse(
        region_key=reg_key,
        region_label=_REGION_LABELS.get(reg_key, reg_key.title()),
        calendar=_format_calendar(reg_key),
    )
