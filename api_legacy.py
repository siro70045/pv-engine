"""
api.py

FastAPI application for the PV Engine.

Start with:
    uvicorn api:app --reload --port 8000

Interactive docs:
    http://localhost:8000/docs   (Swagger UI)
    http://localhost:8000/redoc  (ReDoc)
"""

from __future__ import annotations

import sys
import uuid
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware

# ── Engine imports ────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from main import (
    SimulationInput,
    LocationInput, BuildingInput, PVSystemInput,
    BatteryInput, TariffInput,
    GasSystemInput, HeatPumpInput,
    run_simulation, result_to_dict,
)
from engine.pvgis_client import parse_pvgis_csv, PVGISError
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters, ItalianIncentives,
)
from engine.energy_vectors import DEFAULT_PRICES, FuelType

from schemas import (
    SimulationRequest, SimulationResponse,
    ClimateResponse, PVProductionResponse,
    EnergyBalanceResponse, EconomicsResponse,
    ScenarioResponse, ScenarioComparisonResponse,
    HealthResponse, ErrorResponse,
    TMYUploadResponse, DefaultsResponse,
)

# ── Try pvlib ────────────────────────────────────────────────────────────────
try:
    import pvlib as _pvlib
    PVLIB_AVAILABLE = True
except ImportError:
    PVLIB_AVAILABLE = False

ENGINE_VERSION = "1.0.0"

# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="PV Engine API",
    description=(
        "Techno-economic feasibility analysis for photovoltaic installations "
        "on residential and industrial buildings. "
        "Computes NPV, IRR, payback, self-consumption, and multi-scenario "
        "gas/heat-pump comparison using real PVGIS-SARAH3 climate data."
    ),
    version=ENGINE_VERSION,
    contact={"name": "PV Engine", "email": "info@pvengine.it"},
    license_info={"name": "Proprietary"},
)

# CORS — allow all origins during development; restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # replace with ["https://yourdomain.com"] in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY TMY CACHE  (maps tmy_csv_id → parsed TMYResult)
# Production: replace with Redis or filesystem-based store
# ══════════════════════════════════════════════════════════════════════════════

_tmy_store: dict[str, Any] = {}   # { uuid_str: TMYResult }


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _request_to_simulation_input(req: SimulationRequest) -> SimulationInput:
    """
    Convert a validated SimulationRequest (Pydantic) into a SimulationInput
    (main.py dataclass) — the only coupling point between API and engine.
    """
    loc = req.location
    bld = req.building
    pv  = req.pv_system
    bat = req.battery
    tar = req.tariff
    fin = req.financial
    inc = req.incentives
    gas = req.gas_system
    hp  = req.heat_pump
    inv = req.investment

    return SimulationInput(
        location = LocationInput(
            lat            = loc.lat,
            lon            = loc.lon,
            altitude_m     = loc.altitude_m,
            use_pvgis_cache= True,
        ),
        building = BuildingInput(
            building_type           = bld.building_type,
            floor_area_m2           = bld.floor_area_m2,
            avg_floor_height_m      = bld.avg_floor_height_m,
            n_occupants             = bld.n_occupants,
            # Use sum of monthly if provided, otherwise use annual directly
            annual_demand_kWh       = (sum(bld.monthly_demand_kWh)
                                        if bld.monthly_demand_kWh
                                        else bld.annual_demand_kWh),
            occupancy_days_per_week = bld.occupancy_days_per_week,
            occupancy_hour_start    = bld.occupancy_hour_start,
        ),
        pv_system = PVSystemInput(
            peak_power_kWp              = pv.peak_power_kWp,
            tilt_deg                    = pv.tilt_deg,
            azimuth_deg                 = pv.azimuth_deg,
            mounting                    = pv.mounting,
            degradation_rate_pct_per_yr = pv.degradation_rate_pct_per_yr,
        ),
        battery = BatteryInput(
            capacity_kWh  = bat.capacity_kWh,
            chemistry     = bat.chemistry,
            strategy      = bat.strategy,
        ),
        investment = InvestmentCost(
            pv_cost_eur_kWp        = inv.pv_cost_eur_kWp,
            peak_power_kWp         = pv.peak_power_kWp,
            battery_cost_eur_kWh   = inv.battery_cost_eur_kWh,
            battery_capacity_kWh   = bat.capacity_kWh,
            design_and_permits_eur = inv.design_and_permits_eur,
            other_capex_eur        = inv.other_capex_eur,
        ),
        tariff = TariffInput(
            import_price_eur_kWh       = tar.import_price_eur_kWh,
            export_price_eur_kWh       = tar.export_price_eur_kWh,
            export_limit_pct           = tar.export_limit_pct,
            demand_charge_eur_kW_month = tar.demand_charge_eur_kW_month,
        ),
        opex = OperatingCost(
            om_pct_of_capex_per_yr = fin.om_pct_of_capex_per_yr,
            om_escalation_rate     = 0.02,
        ),
        financial = FinancialParameters(
            discount_rate           = fin.discount_rate_pct / 100.0,
            energy_price_escalation = fin.energy_price_escalation_pct / 100.0,
            analysis_years          = fin.analysis_years,
            financing_equity_fraction = inv.financing_equity_pct / 100.0,
            loan_interest_rate        = inv.loan_rate_pct / 100.0,
            loan_duration_years       = inv.loan_duration_yr,
        ),
        incentives = ItalianIncentives(
            detrazione_fiscale_pct     = inc.detrazione_irpef_pct,
            detrazione_spread_years    = 10,
            ammortamento_accelerato    = inc.ammortamento_accelerato,
            aliquota_ires_pct          = inc.aliquota_ires_pct,
            transizione_5_0_pct        = inc.transizione_5_0_pct,
            cer_incentivo_eur_kWh      = inc.cer_contribution_eur_kWh,
        ),
        gas_system = GasSystemInput(
            has_gas                  = gas.has_gas,
            fuel_type                = gas.fuel_type,
            annual_consumption_units = gas.annual_consumption_units,
            boiler_efficiency        = gas.boiler_efficiency,
            terminal_type            = gas.terminal_type,
            dhw_fraction             = gas.dhw_fraction,
            annual_heating_kWh       = gas.annual_heating_kWh,
            gas_price_eur_sm3        = gas.gas_price_eur_sm3,
        ),
        heat_pump = HeatPumpInput(
            terminal_type                 = hp.terminal_type,
            carnot_efficiency_factor      = hp.carnot_efficiency_factor,
            installed_cost_eur_kW         = hp.installed_cost_eur_kW,
            terminal_replacement_cost_eur = hp.terminal_replacement_cost_eur,
            use_bivalent                  = hp.use_bivalent,
            bivalence_temp_c              = hp.bivalence_temp_c,
        ),
        incentives_hp = ItalianIncentives(
            detrazione_fiscale_pct  = 65.0,   # Ecobonus HP standard
            detrazione_spread_years = 10,
            ammortamento_accelerato = inc.ammortamento_accelerato,
            aliquota_ires_pct       = inc.aliquota_ires_pct,
        ),
        lifetime_years = fin.analysis_years,
        use_pvlib      = PVLIB_AVAILABLE,
    )


def _result_to_response(d: dict) -> SimulationResponse:
    """
    Convert the result_to_dict() output into a typed SimulationResponse.
    Key mapping: result_to_dict() field names → SimulationResponse field names.
    """
    eco  = d["economics"]
    eb   = d["energy_balance_year1"]
    pv   = d["pv_performance"]
    site = d["location"]
    dem  = d["demand"]

    # ── Monthly climate data (not in result_to_dict — derived from tmy) ──────
    # result_to_dict only stores annual_ghi_kWh_m2; monthly data is in tmy
    # We reconstruct from pv_performance monthly production proxy where available
    # For simplicity we return empty lists — the raw endpoint has full TMY data
    monthly_ghi: list[float] = []
    monthly_t:   list[float] = []

    sc_response = None
    if d.get("scenario_comparison"):
        sc = d["scenario_comparison"]
        scenarios_out = {}
        for sid, sv in sc["scenarios"].items():
            scenarios_out[sid] = ScenarioResponse(
                scenario_id                   = sid,
                label                         = sv["label"],
                capex_eur                     = sv["capex_eur"],
                annual_total_energy_cost_eur  = sv["annual_total_energy_cost_eur"],
                annual_saving_vs_baseline_eur = sv["annual_saving_vs_baseline_eur"],
                npv_eur                       = sv["npv_eur"],
                irr_pct                       = sv["irr_pct"],
                payback_simple_yr             = sv["payback_simple_yr"],
                co2_annual_kg                 = sv["annual_co2_kg"],
                co2_saving_pct                = sv["co2_saving_pct"],
                seasonal_cop                  = sv["seasonal_cop"],
                hp_coverage_pct               = sv["hp_coverage_pct"],
                score                         = sv["score"],
            )
        sc_response = ScenarioComparisonResponse(
            recommended_scenario_id     = sc["recommended_scenario"],
            rationale                   = sc["recommendation_rationale"],
            current_gas_price_eur_sm3   = sc["current_gas_price_eur_sm3"],
            breakeven_gas_price_eur_sm3 = sc["gas_breakeven_price_eur_sm3"],
            scenarios                   = scenarios_out,
            gas_price_sensitivity       = sc["gas_price_sensitivity"],
        )

    return SimulationResponse(
        status         = "ok",
        engine_version = ENGINE_VERSION,
        annual_demand_kWh      = dem["total_annual_kWh"],
        specific_demand_kWh_m2 = dem["specific_kWh_m2"],
        climate = ClimateResponse(
            latitude           = site["latitude"],
            longitude          = site["longitude"],
            elevation_m        = site["elevation_m"],
            dataset            = site["dataset"],
            ghi_annual_kWh_m2  = site["annual_ghi_kWh_m2"],
            t_avg_annual_c     = 0.0,          # not in result_to_dict; use /raw for full TMY
            monthly_ghi_kWh_m2 = monthly_ghi,
            monthly_t_avg_c    = monthly_t,
        ),
        pv_production = PVProductionResponse(
            annual_kWh             = pv["production_annual_kWh"],
            specific_yield_kWh_kWp = pv["specific_yield_kWh_kWp"],
            performance_ratio_pct  = round(pv["performance_ratio"] * 100, 1),
            monthly_kWh            = pv["production_monthly_kWh"],
        ),
        energy_balance = EnergyBalanceResponse(
            self_consumption_rate_pct = round(eb["self_consumption_rate"] * 100, 1),
            self_sufficiency_rate_pct = round(eb["self_sufficiency_rate"] * 100, 1),
            grid_import_kWh           = eb["grid_import_kWh"],
            grid_export_kWh           = eb["grid_export_kWh"],
            curtailed_kWh             = eb.get("curtailed_kWh", 0.0),
            annual_saving_eur         = eb["annual_saving_eur"],
        ),
        economics = EconomicsResponse(
            total_capex_eur                = eco["total_capex_eur"],
            net_capex_after_incentives_eur = eco["net_capex_eur"],
            incentive_value_eur            = eco["incentive_pv_eur"],
            npv_eur                        = eco["npv_eur"],
            irr_pct                        = eco["irr_pct"],
            payback_simple_yr              = eco["payback_simple_yr"],
            payback_discounted_yr          = eco["payback_discounted_yr"],
            lcoe_eur_kWh                   = eco["lcoe_eur_kWh"],
            score                          = eco["score"],
            recommendation                 = eco["recommendation"],
            annual_cash_flows_eur          = [cf["net_cf_eur"] for cf in d["cash_flows"]],
        ),
        scenario_comparison = sc_response,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["system"],
)
async def health() -> HealthResponse:
    """Returns engine status, version, and availability of optional dependencies."""
    pvgis_ok = True
    try:
        import urllib.request
        urllib.request.urlopen(
            "https://re.jrc.ec.europa.eu/api/v5_2/tmy?lat=45&lon=9&outputformat=json",
            timeout=5,
        )
    except Exception:
        pvgis_ok = False

    return HealthResponse(
        status           = "ok",
        version          = ENGINE_VERSION,
        pvlib_available  = PVLIB_AVAILABLE,
        pvgis_reachable  = pvgis_ok,
    )


@app.get(
    "/defaults",
    response_model=DefaultsResponse,
    summary="Default values for the frontend form",
    tags=["system"],
)
async def defaults() -> DefaultsResponse:
    """
    Returns current default values for all configurable parameters.
    The frontend uses these to pre-populate form fields.
    """
    return DefaultsResponse(
        electricity_price_eur_kWh      = DEFAULT_PRICES[FuelType.ELECTRICITY].price_eur_per_kwh,
        gas_price_eur_sm3              = DEFAULT_PRICES[FuelType.NATURAL_GAS].price_eur_per_unit,
        pv_cost_residential_eur_kWp    = 1800.0,
        pv_cost_industrial_eur_kWp     = 900.0,
        discount_rate_pct              = 5.0,
        energy_escalation_pct          = 3.0,
        irpef_deduction_pct            = 50.0,
        ires_rate_pct                  = 24.0,
        grid_co2_kg_per_kWh            = DEFAULT_PRICES[FuelType.ELECTRICITY].co2_factor_kg_per_kwh,
        analysis_years                 = 25,
    )


@app.post(
    "/tmy/upload",
    response_model=TMYUploadResponse,
    summary="Upload a PVGIS TMY CSV file",
    tags=["climate"],
)
async def upload_tmy(
    file: UploadFile = File(
        ...,
        description="PVGIS TMY CSV file downloaded from re.jrc.ec.europa.eu/pvg_tools",
    ),
) -> TMYUploadResponse:
    """
    Upload a PVGIS TMY CSV file downloaded from the PVGIS web interface.

    Returns a `tmy_csv_id` to pass in subsequent simulation requests.
    The uploaded file is parsed immediately and stored in memory —
    the raw CSV is not persisted.

    Use this endpoint when:
    - PVGIS API is slow or unreachable
    - You want to use a specific TMY period (e.g. 2005–2023)
    - You want to include a custom horizon profile

    The file must be the **TMY** format (not Hourly Radiation or Monthly Data).
    """
    content = await file.read()
    try:
        csv_text = content.decode("utf-8", errors="replace")
        tmy = parse_pvgis_csv(csv_text)
    except PVGISError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid PVGIS CSV file: {e}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not parse uploaded file: {e}",
        )

    tmy_id = str(uuid.uuid4())
    _tmy_store[tmy_id] = tmy

    return TMYUploadResponse(
        tmy_csv_id        = tmy_id,
        latitude          = tmy.location.latitude,
        longitude         = tmy.location.longitude,
        elevation_m       = tmy.location.elevation_m,
        ghi_annual_kWh_m2 = round(sum(tmy.monthly.GHI_monthly), 1),
        records           = len(tmy.hourly),
    )


@app.post(
    "/simulate",
    response_model=SimulationResponse,
    summary="Run a full PV feasibility simulation",
    tags=["simulation"],
    responses={
        200: {"description": "Simulation completed successfully."},
        422: {"description": "Validation error — check field constraints."},
        500: {"description": "Internal engine error."},
    },
)
async def simulate(req: SimulationRequest) -> SimulationResponse:
    """
    Run a complete PV techno-economic feasibility simulation.

    **Typical response time:** 2–8 seconds depending on whether PVGIS needs
    to be called (cold) or the TMY is already cached / uploaded.

    **Minimal request:**
    ```json
    {
      "location": {"lat": 44.601, "lon": 10.636},
      "pv_system": {"peak_power_kWp": 100},
      "investment": {"pv_cost_eur_kWp": 900}
    }
    ```

    **With gas/HP comparison:**
    Add `"gas_system": {"has_gas": true, "annual_consumption_units": 15000,
    "gas_price_eur_sm3": 0.95, "terminal_type": "fan_coil"}` to the request.
    """
    import main as _main

    # ── Inject pre-uploaded TMY if provided ──────────────────────────────────
    original_fetch = None
    if req.tmy_csv_id:
        tmy_obj = _tmy_store.get(req.tmy_csv_id)
        if tmy_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"TMY CSV ID '{req.tmy_csv_id}' not found. "
                       f"Upload the file first via POST /tmy/upload.",
            )
        import engine.pvgis_client as _pvc
        original_fetch = _pvc.fetch_tmy
        _pvc.fetch_tmy = lambda lat, lon, use_cache=True: tmy_obj
        _main.fetch_tmy = lambda lat, lon, use_cache=True: tmy_obj

    try:
        inp = _request_to_simulation_input(req)
        result = run_simulation(inp)
        d = result_to_dict(result)
        return _result_to_response(d)

    except PVGISError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"PVGIS climate data unavailable: {e}. "
                f"Try uploading a TMY CSV file via POST /tmy/upload."
            ),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid simulation parameters: {e}",
        )
    except Exception as e:
        # Log full traceback server-side; return safe message to client
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Engine error: {type(e).__name__}: {e}",
        )
    finally:
        # Always restore original fetch function to avoid side effects
        if original_fetch is not None:
            import engine.pvgis_client as _pvc
            _pvc.fetch_tmy = original_fetch
            _main.fetch_tmy = original_fetch


@app.post(
    "/simulate/raw",
    summary="Run simulation and return full raw result dict",
    tags=["simulation"],
)
async def simulate_raw(req: SimulationRequest) -> dict:
    """
    Same as /simulate but returns the complete unfiltered result_to_dict()
    output. Useful for advanced clients, debugging, or building custom reports.
    """
    import main as _main
    original_fetch = None
    if req.tmy_csv_id:
        tmy_obj = _tmy_store.get(req.tmy_csv_id)
        if tmy_obj is None:
            raise HTTPException(status_code=404, detail=f"TMY CSV ID not found.")
        import engine.pvgis_client as _pvc
        original_fetch = _pvc.fetch_tmy
        _pvc.fetch_tmy = lambda lat, lon, use_cache=True: tmy_obj
        _main.fetch_tmy = lambda lat, lon, use_cache=True: tmy_obj
    try:
        inp = _request_to_simulation_input(req)
        result = run_simulation(inp)
        return result_to_dict(result)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    finally:
        if original_fetch is not None:
            import engine.pvgis_client as _pvc
            _pvc.fetch_tmy = original_fetch
            _main.fetch_tmy = original_fetch
