"""Loads endpoints: residential profile, school profile, school benchmarks."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from api.schemas.loads import (
    ResidentialLoadsRequest, ResidentialLoadsResponse,
    SchoolLoadsRequest, SchoolLoadsResponse,
    SchoolBenchmarkRequest, SchoolBenchmarkResponse,
)
from api.utils.cache import get_cached_tmy
from api.utils.compression import compress_hourly_Wh, compress_monthly
from engine.loads import (
    LoadsInput, OccupancySchedule, DHWSystem, VentilationSystem,
    LightingSystem, HVACSystem, calc_loads,
)
from engine.pvgis_client import PVGISError
from school.climate_zones import (
    classify_construction_era,
    get_electric_benchmark, get_thermal_benchmark,
    SCHOOL_ELECTRIC_BENCHMARK, SCHOOL_THERMAL_BENCHMARK,
)
from school.loads_school import SchoolLoadsInput, calc_school_loads

logger = logging.getLogger("pv_engine_api.loads")

router = APIRouter(prefix="/loads", tags=["loads"])


def _residential_inputs(req: ResidentialLoadsRequest) -> LoadsInput:
    """Build a `LoadsInput` mirroring the assumptions in main.run_simulation()."""
    occ_frac = max(0.05, req.occupancy_days_per_week / 7.0)
    n_occ = max(1, round(req.n_occupants * occ_frac))

    hvac = HVACSystem(
        heating_system="heat_pump",
        cooling_system="split_ac",
        heating_cop=3.0,
        cooling_eer=2.8,
        auxiliary_W=max(req.floor_area_m2 * 0.1, 200),
    )
    return LoadsInput(
        floor_area_m2 = req.floor_area_m2,
        building_type = "residential",
        occupancy = OccupancySchedule(
            building_type="residential",
            n_occupants=n_occ,
            vacation_fraction=max(0.05, 1.0 - occ_frac),
            vacation_months=[],
        ),
        dhw = DHWSystem(daily_demand_L_per_person=50.0, is_electric=True),
        ventilation = VentilationSystem(
            airflow_m3h_per_person=36.0,
            specific_fan_power_W_m3h=1.2,
            operation_hours_per_day=16.0,
        ),
        lighting = LightingSystem(
            installed_power_W_m2=10.0,
            floor_area_m2=req.floor_area_m2,
            daylight_factor=0.10,
        ),
        appliances = [],
        hvac = hvac,
        heating_demand_monthly_kWh=[0.0] * 12,
        cooling_demand_monthly_kWh=[0.0] * 12,
        annual_bill_kWh=req.annual_demand_kWh,
        monthly_T_ext=None,
    )


@router.post("/residential", response_model=ResidentialLoadsResponse,
             summary="Residential 8760-h load profile")
async def residential_loads(req: ResidentialLoadsRequest) -> ResidentialLoadsResponse:
    inp = _residential_inputs(req)
    result = calc_loads(inp)

    hourly = list(result.hourly_demand_Wh)

    # Apply occupancy_hour_start mask (matches main.run_simulation behaviour)
    if req.occupancy_hour_start > 0:
        standby = 0.25
        for i in range(len(hourly)):
            if (i % 24) < req.occupancy_hour_start:
                hourly[i] *= standby

    # AC additive load distributed over June-Aug daytime
    if req.has_ac and req.ac_kw > 0:
        from school.calendar_it import DAYS_BEFORE_MONTH, DAYS_PER_MONTH
        ac_hours_summer = req.ac_kw * 6 * 90  # kWh
        # Spread evenly across hours 12-18 of Jun/Jul/Aug
        target_hours: list[int] = []
        for m in (5, 6, 7):
            base = DAYS_BEFORE_MONTH[m] * 24
            for d in range(DAYS_PER_MONTH[m]):
                for h in range(12, 18):
                    target_hours.append(base + d * 24 + h)
        if target_hours:
            extra_per_hour = ac_hours_summer * 1000.0 / len(target_hours)
            for i in target_hours:
                hourly[i] += extra_per_hour

    # Bill scaling — anchor to req.annual_demand_kWh if provided
    total_kwh = sum(hourly) / 1000.0
    if req.annual_demand_kWh > 0 and total_kwh > 0:
        scale = req.annual_demand_kWh / total_kwh
        if abs(scale - 1.0) > 0.05:
            hourly = [v * scale for v in hourly]
            total_kwh = req.annual_demand_kWh

    # Monthly breakdown
    from school.calendar_it import DAYS_BEFORE_MONTH, DAYS_PER_MONTH
    monthly = []
    for m in range(12):
        s = DAYS_BEFORE_MONTH[m] * 24
        e = s + DAYS_PER_MONTH[m] * 24
        monthly.append(round(sum(hourly[s:e]) / 1000.0, 1))

    return ResidentialLoadsResponse(
        total_annual_kWh=round(total_kwh, 1),
        peak_demand_kW=round(max(hourly) / 1000.0, 2),
        hourly_profile_Wh=compress_hourly_Wh(hourly),
        monthly_breakdown_kWh=monthly,
        load_categories={
            "dhw":         round(result.dhw_kWh, 1),
            "ventilation": round(result.ventilation_kWh, 1),
            "lighting":    round(result.lighting_kWh, 1),
            "appliances":  round(result.appliances_kWh, 1),
            "hvac_heating":round(result.hvac_heating_kWh, 1),
            "hvac_cooling":round(result.hvac_cooling_kWh, 1),
            "hvac_aux":    round(result.hvac_auxiliary_kWh, 1),
            "ac_additive": round(req.ac_kw * 6 * 90, 1) if req.has_ac else 0.0,
        },
    )


def _school_inputs(req: SchoolLoadsRequest) -> SchoolLoadsInput:
    return SchoolLoadsInput(
        floor_area_m2=req.floor_area_m2,
        n_floors=req.n_floors,
        construction_year=req.construction_year,
        school_type=req.school_type,
        n_students=req.n_students,
        n_teachers=req.n_teachers,
        n_ata_staff=req.n_ata_staff,
        full_time=req.full_time,
        extra_evening=req.extra_evening,
        extra_weekend=req.extra_weekend,
        has_gym=req.has_gym,
        has_canteen=req.has_canteen,
        has_it_lab=req.has_it_lab,
        has_tech_lab=req.has_tech_lab,
        has_science_lab=req.has_science_lab,
        has_auditorium=req.has_auditorium,
        has_elevator=req.has_elevator,
        n_computers=req.n_computers,
        it_lab_watts_per_pc=req.it_lab_watts_per_pc,
        tech_lab_peak_kW=req.tech_lab_peak_kW,
        heating_system=req.heating_system,
        has_ac=req.has_ac,
        ac_type=req.ac_type,
        lighting_type=req.lighting_type,
        climate_zone=req.climate_zone,
        region=req.region,
        presence_factor=req.presence_factor,
        dhw_system=req.dhw_system,
        dhw_standby_losses_pct=req.dhw_standby_losses_pct,
        annual_bill_kwh=req.annual_bill_kWh,
        monthly_bill_kwh=req.monthly_bill_kWh or [0.0] * 12,
    )


@router.post("/school", response_model=SchoolLoadsResponse,
             summary="School 8760-h load profile (calc_school_loads)")
async def school_loads(req: SchoolLoadsRequest) -> SchoolLoadsResponse:
    tmy_ghi = None
    if req.lat is not None and req.lon is not None:
        try:
            tmy, _ = get_cached_tmy(req.lat, req.lon)
            tmy_ghi = [rec.G_h for rec in tmy.hourly]
        except (PVGISError, ValueError) as e:
            logger.warning("School TMY fetch failed: %s — falling back to non-daylight model.", e)

    inp = _school_inputs(req)
    try:
        result = calc_school_loads(inp, tmy_hourly_ghi=tmy_ghi)
    except Exception as e:
        logger.exception("calc_school_loads failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Loads engine error: {e}")

    return SchoolLoadsResponse(
        total_annual_kWh=result["total_annual_kwh"],
        peak_demand_kW=result["peak_demand_kW"],
        hourly_profile_Wh=compress_hourly_Wh(result["hourly_demand_Wh"]),
        monthly_breakdown_kWh=compress_monthly(result["total_monthly_kwh"]),
        breakdown=result["breakdown"],
        internal_gains=result["internal_gains"],
        dhw_details=result["dhw_details"],
        lighting_details=result.get("lighting_details", {}),
        presence_factor=result["presence_factor"],
        effective_occupancy=result["effective_occupancy"],
        climate_zone=result["climate_zone"],
        region=req.region,
        scale_factor=result["scale_factor"],
        school_days_per_month=result["school_days_per_month"],
    )


@router.post("/school/benchmarks", response_model=SchoolBenchmarkResponse,
             summary="ENEA / CTI per-m² benchmarks for schools")
async def school_benchmarks(req: SchoolBenchmarkRequest) -> SchoolBenchmarkResponse:
    era = classify_construction_era(req.construction_year)
    el_per_m2 = SCHOOL_ELECTRIC_BENCHMARK.get(
        (req.climate_zone, req.school_type),
        SCHOOL_ELECTRIC_BENCHMARK.get(("E", req.school_type), 44.0),
    )
    th_per_m2 = SCHOOL_THERMAL_BENCHMARK.get(
        (req.climate_zone, era),
        SCHOOL_THERMAL_BENCHMARK.get(("E", era), 90.0),
    )
    return SchoolBenchmarkResponse(
        electric_benchmark_kWh_m2_yr=el_per_m2,
        thermal_benchmark_kWh_m2_yr=th_per_m2,
        electric_total_estimate_kWh=round(get_electric_benchmark(
            req.climate_zone, req.school_type, req.floor_area_m2), 1),
        thermal_total_estimate_kWh=round(get_thermal_benchmark(
            req.climate_zone, req.construction_year, req.floor_area_m2), 1),
        climate_zone=req.climate_zone,
        construction_era=era,
    )
