"""Load-profile request/response models for residential & school buildings."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ResidentialLoadsRequest(BaseModel):
    # Geometry / occupancy
    floor_area_m2: float = Field(default=120.0, gt=0)
    n_occupants: int = Field(default=3, ge=0)
    building_subtype: Literal["detached", "terraced", "apartment"] = "detached"

    # Consumption
    annual_demand_kWh: float = Field(default=3000.0, ge=0)
    monthly_demand_kWh: Optional[list[float]] = Field(
        default=None, description="12-month override for annual_demand_kWh."
    )
    occupancy_days_per_week: float = Field(default=7.0, ge=1.0, le=7.0)
    occupancy_hour_start: int = Field(default=0, ge=0, le=23)

    # AC
    has_ac: bool = False
    ac_kw: float = Field(default=3.5, ge=0, le=50)


class SchoolLoadsRequest(BaseModel):
    # Building
    floor_area_m2: float = Field(default=3000.0, gt=0)
    n_floors: int = Field(default=3, ge=1, le=10)
    construction_year: int = Field(default=1981, ge=1900, le=2100)
    school_type: Literal[
        "primary", "middle", "high_classic", "high_tech", "university_dept"
    ] = "high_tech"

    # Occupancy
    n_students: int = Field(default=500, ge=0)
    n_teachers: int = Field(default=40, ge=0)
    n_ata_staff: int = Field(default=15, ge=0)
    full_time: bool = False
    extra_evening: bool = False
    extra_weekend: bool = False
    presence_factor: float = Field(default=0.87, ge=0.0, le=1.0)

    # Special rooms
    has_gym: bool = False
    has_canteen: bool = False
    has_it_lab: bool = False
    has_tech_lab: bool = False
    has_science_lab: bool = False
    has_auditorium: bool = False
    has_elevator: bool = False

    # Loads
    n_computers: int = Field(default=30, ge=0)
    it_lab_watts_per_pc: float = Field(default=120.0, ge=0)
    tech_lab_peak_kW: float = Field(default=0.0, ge=0)

    # HVAC
    heating_system: Literal["gas_boiler", "heat_pump", "district", "electric"] = "gas_boiler"
    has_ac: bool = False
    ac_type: Literal["none", "split", "central"] = "none"

    # Lighting
    lighting_type: Literal["led", "fluorescent", "mixed"] = "fluorescent"

    # DHW
    dhw_system: Literal["electric", "gas", "heat_pump", "none"] = "electric"
    dhw_standby_losses_pct: float = Field(default=0.15, ge=0, le=0.5)

    # Climate / region
    climate_zone: Literal["A", "B", "C", "D", "E", "F"] = "E"
    region: str = "emilia_romagna"

    # Bills
    annual_bill_kWh: float = Field(default=0.0, ge=0)
    monthly_bill_kWh: Optional[list[float]] = None

    # Optional: TMY GHI for daylight-aware lighting
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)


class ResidentialLoadsResponse(BaseModel):
    total_annual_kWh: float
    peak_demand_kW: float
    hourly_profile_Wh: list[int]
    monthly_breakdown_kWh: list[float]
    load_categories: dict[str, float]


class SchoolLoadsResponse(BaseModel):
    total_annual_kWh: float
    peak_demand_kW: float
    hourly_profile_Wh: list[int]
    monthly_breakdown_kWh: list[float]
    breakdown: dict[str, float]
    internal_gains: dict
    dhw_details: dict
    lighting_details: dict
    presence_factor: float
    effective_occupancy: dict
    climate_zone: str
    region: str
    scale_factor: float
    school_days_per_month: list[int]


class SchoolBenchmarkRequest(BaseModel):
    climate_zone: Literal["A", "B", "C", "D", "E", "F"] = "E"
    school_type: Literal[
        "primary", "middle", "high_classic", "high_tech", "university_dept"
    ] = "high_tech"
    construction_year: int = Field(default=1981, ge=1900, le=2100)
    floor_area_m2: float = Field(default=3000.0, gt=0)


class SchoolBenchmarkResponse(BaseModel):
    electric_benchmark_kWh_m2_yr: float
    thermal_benchmark_kWh_m2_yr: float
    electric_total_estimate_kWh: float
    thermal_total_estimate_kWh: float
    climate_zone: str
    construction_era: str
