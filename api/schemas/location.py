"""Location-related request/response models."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class LatLonRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude [decimal degrees, WGS84].")
    lon: float = Field(..., ge=-180, le=180, description="Longitude [decimal degrees, WGS84].")


class GeocodeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Free-text address or place name.")
    limit: int = Field(default=5, ge=1, le=15)


class GeocodeResultItem(BaseModel):
    display_name: str
    lat: float
    lon: float
    type: str = "address"
    importance: float = 0.0


class GeocodeResponse(BaseModel):
    results: list[GeocodeResultItem]


class AzimuthRequest(BaseModel):
    building_lat: float = Field(..., ge=-90, le=90)
    building_lon: float = Field(..., ge=-180, le=180)
    sun_lat: float = Field(..., ge=-90, le=90)
    sun_lon: float = Field(..., ge=-180, le=180)


class AzimuthResponse(BaseModel):
    azimuth_deg: float = Field(..., ge=0, le=360)
    direction_label: str
    quality: Literal["good", "ok", "warning"]
    quality_message: str


class TMYResponse(BaseModel):
    ghi_annual_kWh_m2: float
    temperature_mean_C: float
    ghi_monthly: list[float]
    temperature_monthly: list[float]
    elevation_m: float
    source: str
    cached: bool
    latitude: float
    longitude: float


class TMYHourlyResponse(BaseModel):
    ghi_hourly_Wm2: list[int]
    temperature_hourly_C: list[float]


class ClimateZoneResponse(BaseModel):
    zone: Literal["A", "B", "C", "D", "E", "F"]
    zone_label: str
    heating_months: list[int]
    heating_hours_day: int


class RegionCalendarResponse(BaseModel):
    start_date: str
    end_date: str
    autumn_break: Optional[str] = None
    carnival_days: int
    patron_day: Optional[str] = None


class RegionResponse(BaseModel):
    region_key: str
    region_label: str
    calendar: RegionCalendarResponse
