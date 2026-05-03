"""PV endpoints: production, cost estimate, auto-sizing scenarios."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter
from api.schemas.pv import (
    PVProductionRequest, PVProductionResponse,
    PVCostRequest, PVCostResponse,
    PVAutoSizeRequest, PVAutoSizeResponse, PVAutoSizeScenario,
)
from api.utils.cache import get_cached_tmy
from api.utils.compression import compress_hourly_Wh
from api.utils.cost import estimate_pv_cost, estimate_pv_cost_residential, estimate_pv_cost_school, auto_scenarios
from engine.pvgis_client import PVGISError
from engine.solar import PVSystem, SystemLosses, simulate_pv

logger = logging.getLogger("pv_engine_api.pv")

router = APIRouter(prefix="/pv", tags=["pv"])


@router.post("/production", response_model=PVProductionResponse,
             summary="Run PV simulation for a given system at a location")
@limiter.limit("20/minute")
async def production(request: Request, req: PVProductionRequest) -> PVProductionResponse:
    try:
        tmy, cached = get_cached_tmy(req.lat, req.lon)
    except PVGISError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PVGIS: {e}")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    system = PVSystem(
        peak_power_kWp=req.peak_power_kWp,
        tilt_deg=req.tilt_deg,
        azimuth_deg=req.azimuth_deg,
        system_losses=SystemLosses(),
        degradation_rate_pct_per_yr=req.degradation_pct_yr,
    )
    try:
        result = simulate_pv(tmy=tmy, system=system, mounting=req.mounting, use_pvlib=True)
    except Exception as e:
        logger.exception("simulate_pv failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"PV engine error: {e}")

    # Apply degradation for the requested analysis year (1-based)
    deg = (1.0 - req.degradation_pct_yr / 100.0) ** max(0, req.analysis_year - 1)
    annual = result.production_annual_kWh * deg
    monthly = [round(v * deg, 1) for v in result.production_monthly_kWh]

    hourly_out = None
    if req.include_hourly:
        hourly_out = compress_hourly_Wh(h.P_ac_W * deg for h in result.hourly)

    return PVProductionResponse(
        annual_production_kWh=round(annual, 1),
        specific_yield_kWh_kWp=round(result.specific_yield_kWh_kWp * deg, 1),
        performance_ratio=round(result.performance_ratio, 4),
        monthly_production_kWh=monthly,
        hourly_production_Wh=hourly_out,
        cached=cached,
    )


@router.post("/cost-estimate", response_model=PVCostResponse,
             summary="Tier-based CAPEX estimate (Italy 2024)")
async def cost_estimate(req: PVCostRequest) -> PVCostResponse:
    breakdown = estimate_pv_cost(req.peak_power_kWp, req.building_type)
    return PVCostResponse(**breakdown)


@router.post("/auto-size", response_model=PVAutoSizeResponse,
             summary="Three-scenario sizing (minimal/recommended/maximum)")
async def auto_size(req: PVAutoSizeRequest) -> PVAutoSizeResponse:
    kwp_max_by_roof = round(req.usable_roof_m2 / req.m2_per_kwp, 1)
    sizing = auto_scenarios(req.annual_demand_kWh, kwp_max_by_roof,
                            yield_kwh_per_kwp=req.yield_kwh_per_kwp)

    if req.scenario != "all" and req.scenario in sizing:
        names = [req.scenario]
    else:
        names = ["minimal", "recommended", "maximum"]

    out: list[PVAutoSizeScenario] = []
    for name in names:
        kwp = sizing[name]
        if req.building_type in ("school", "public"):
            capex, _ = estimate_pv_cost_school(kwp)
        else:
            capex = estimate_pv_cost_residential(kwp)
        ann_prod = kwp * req.yield_kwh_per_kwp
        coverage = min(100.0, round(ann_prod / req.annual_demand_kWh * 100.0, 1))
        out.append(PVAutoSizeScenario(
            name=name, kwp=kwp, capex_eur=capex,
            coverage_pct=coverage, annual_production_kWh=round(ann_prod, 0),
        ))

    return PVAutoSizeResponse(scenarios=out, kwp_max_by_roof=kwp_max_by_roof)
