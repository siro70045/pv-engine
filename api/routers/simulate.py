"""Monolithic simulation endpoints — retrocompatibility with Streamlit wizards.

`/simulate`        delegates to `main.run_simulation()` and re-uses the legacy
                   SimulationRequest/Response Pydantic contract.
`/simulate/school` runs `school.loads_school.run_simulation_school()` after
                   computing the school load profile and PV TMY.
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

# Re-use the legacy adapter logic so we stay bug-for-bug compatible
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.schemas.simulate import (
    FullSimulationRequest, FullSimulationResponse,
    FullSchoolSimulationRequest, FullSchoolSimulationResponse,
)
from api.utils.cache import get_cached_tmy
from api.utils.cost import estimate_pv_cost_school
from engine.pvgis_client import PVGISError

logger = logging.getLogger("pv_engine_api.simulate")

router = APIRouter(prefix="/simulate", tags=["simulate"])


@router.post("", response_model=FullSimulationResponse,
             summary="Run a complete PV feasibility simulation (legacy contract)")
async def simulate(req: FullSimulationRequest) -> FullSimulationResponse:
    """Delegates to `api_legacy.simulate` — same engine, same response shape."""
    from api_legacy import _request_to_simulation_input, _result_to_response
    from main import run_simulation, result_to_dict

    try:
        inp = _request_to_simulation_input(req)
        result = run_simulation(inp)
        d = result_to_dict(result)
        return _result_to_response(d)
    except PVGISError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PVGIS: {e}")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except Exception as e:
        logger.exception("simulate failed")
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"{type(e).__name__}: {e}")


@router.post("/school", response_model=FullSchoolSimulationResponse,
             summary="Run a complete school PV feasibility simulation")
async def simulate_school(req: FullSchoolSimulationRequest) -> FullSchoolSimulationResponse:
    from api.schemas.loads import SchoolLoadsRequest
    from api.routers.loads import _school_inputs
    from school.loads_school import calc_school_loads, run_simulation_school

    try:
        tmy, _ = get_cached_tmy(req.lat, req.lon)
    except PVGISError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PVGIS: {e}")

    try:
        # Validate the embedded school dict using SchoolLoadsRequest
        school_req = SchoolLoadsRequest(**req.school)
    except Exception as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Invalid `school` body: {e}")

    school_inp = _school_inputs(school_req)
    tmy_ghi = [rec.G_h for rec in tmy.hourly]
    loads = calc_school_loads(school_inp, tmy_hourly_ghi=tmy_ghi)

    capex = req.capex_eur
    structural = req.structural_eur
    if capex is None:
        capex_total, structural_auto = estimate_pv_cost_school(req.peak_power_kWp)
        capex = float(capex_total)
        structural = float(structural_auto)

    try:
        sim_result = run_simulation_school(
            tmy=tmy, school_loads=loads,
            floor_area_m2=school_inp.floor_area_m2,
            kwp=req.peak_power_kWp,
            azimuth_deg=req.azimuth_deg, tilt_deg=req.tilt_deg,
            import_price=req.import_price_eur_kWh,
            export_price=req.export_price_eur_kWh,
            capex_eur=capex,
            structural_eur=structural,
            pnrr_grant=req.pnrr_grant,
            cer_incentive_eur_kwh=req.cer_incentive_eur_kWh,
            cer_fraction=req.cer_fraction,
            lifetime_years=req.lifetime_years,
            pv_degradation_pct_per_yr=req.pv_degradation_pct_per_yr,
            inverter_replacement_year=req.inverter_replacement_year,
            inverter_replacement_cost_pct=req.inverter_replacement_cost_pct,
        )
    except Exception as e:
        logger.exception("run_simulation_school failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"{type(e).__name__}: {e}")

    eb = sim_result.yearly_balance[0]
    eco = sim_result.economics
    sr = sim_result.solar_result
    monthly = eb.monthly

    return FullSchoolSimulationResponse(
        annual_demand_kWh=loads["total_annual_kwh"],
        annual_production_kWh=round(sr.production_annual_kWh, 1),
        self_consumption_rate=round(eb.self_consumption_rate, 4),
        self_sufficiency_rate=round(eb.self_sufficiency_rate, 4),
        grid_import_kWh=round(eb.grid_import_kWh, 1),
        grid_export_kWh=round(eb.grid_export_kWh, 1),
        annual_saving_eur=round(eb.annual_saving_eur, 2),
        capex_eur=round(capex, 0),
        structural_eur=round(structural, 0),
        pnrr_grant=round(req.pnrr_grant, 0),
        npv_eur=round(eco.npv_eur, 0),
        irr_pct=round(eco.irr_pct, 2),
        payback_simple_yr=round(eco.payback_simple_yr, 1),
        lcoe_eur_kWh=round(eco.lcoe_eur_kWh, 4),
        score=round(eco.score, 1),
        recommendation=eco.recommendation,
        monthly_pv_kWh=[round(v, 1) for v in monthly.pv_production_kWh],
        monthly_demand_kWh=[round(v, 1) for v in monthly.demand_kWh],
        monthly_grid_import_kWh=[round(v, 1) for v in monthly.grid_import_kWh],
        monthly_grid_export_kWh=[round(v, 1) for v in monthly.grid_export_kWh],
        breakdown=loads["breakdown"],
        climate_zone=loads["climate_zone"],
        region=school_inp.region,
    )
