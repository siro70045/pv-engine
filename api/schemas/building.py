"""Building-related request/response models."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class OSMBuildingRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    radius_m: int = Field(default=100, ge=20, le=500)


class OSMBuilding(BaseModel):
    osm_id: int
    polygon: list[list[float]] = Field(
        ..., description="List of [lat, lon] pairs forming the building outline."
    )
    centroid_lat: float
    centroid_lon: float
    area_m2: float
    building_type: str
    n_floors: Optional[int] = None


class OSMBuildingResponse(BaseModel):
    buildings: list[OSMBuilding]
    count: int


class RoofEstimateRequest(BaseModel):
    floor_area_m2: float = Field(..., gt=0)
    n_floors: int = Field(default=1, ge=1)
    roof_share_pct: float = Field(default=80.0, ge=0, le=100)
    building_type: str = Field(default="residential")
    mounting: Literal[
        "roof_integrated", "flat_roof_ballast", "ground_fixed", "bipv"
    ] = "flat_roof_ballast"


class RoofEstimateResponse(BaseModel):
    footprint_m2: float
    usable_roof_m2: float
    max_kwp_estimate: float
    notes: list[str]


class ThermalFactorRequest(BaseModel):
    building_subtype: Literal["detached", "terraced", "apartment"] = "detached"
    floor_number: int = Field(default=0, ge=0, le=20)
    is_top_floor: bool = False


class ThermalFactorResponse(BaseModel):
    thermal_factor: float
    description: str
