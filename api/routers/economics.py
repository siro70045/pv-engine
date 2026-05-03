"""Economics endpoints: energy balance, NPV/IRR, residential & school incentives.

Heavy multi-scenario comparisons are routed through `/simulate` (which reuses
the engine pipeline) — exposed here as `/economics/scenario-comparison`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from api.schemas.economics import (
    EnergyBalanceRequest, EnergyBalanceResponse,
    KPIRequest, KPIResponse,
    IncentiveResidentialRequest, IncentiveResidentialResponse,
    IncentiveSchoolRequest, IncentiveSchoolResponse,
    ScenarioComparisonRequest, ScenarioComparisonResponse, ScenarioOutcome,
)
from api.schemas.simulate import FullSimulationRequest
from engine.energy_balance import (
    BatterySystem, GridTariff, EnergyBalanceInput, simulate_energy_balance,
)

logger = logging.getLogger("pv_engine_api.economics")

router = APIRouter(prefix="/economics", tags=["economics"])


# ─────────────────────────────────────────────────────────────────────────────
# /economics/balance
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_8760(arr: list[float], name: str) -> list[float]:
    if len(arr) == 8760:
        return list(arr)
    if len(arr) == 8784:
        return list(arr[:8760])
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        f"{name} must have 8760 hourly values (got {len(arr)}).",
    )


@router.post("/balance", response_model=EnergyBalanceResponse,
             summary="Hourly energy balance (PV vs demand, optional battery)")
async def balance(req: EnergyBalanceRequest) -> EnergyBalanceResponse:
    pv = _ensure_8760(req.pv_hourly_Wh, "pv_hourly_Wh")
    demand = _ensure_8760(req.demand_hourly_Wh, "demand_hourly_Wh")

    inp = EnergyBalanceInput(
        pv_production_hourly_Wh=pv,
        demand_hourly_Wh=demand,
        battery=BatterySystem(capacity_kWh=req.battery_kWh),
        tariff=GridTariff(
            import_price_eur_kWh=req.import_price_eur_kWh,
            export_price_eur_kWh=req.export_price_eur_kWh,
        ),
        peak_power_kWp=req.peak_power_kWp,
        battery_strategy=req.battery_strategy,
    )
    result = simulate_energy_balance(inp)
    m = result.monthly
    return EnergyBalanceResponse(
        self_consumption_rate=round(result.self_consumption_rate, 4),
        self_sufficiency_rate=round(result.self_sufficiency_rate, 4),
        grid_import_kWh=round(result.grid_import_kWh, 1),
        grid_export_kWh=round(result.grid_export_kWh, 1),
        self_consumed_kWh=round(result.self_consumed_kWh, 1),
        annual_saving_eur=round(result.annual_saving_eur, 2),
        export_revenue_eur=round(result.export_revenue_eur, 2),
        pv_production_kWh=round(result.pv_production_kWh, 1),
        total_demand_kWh=round(result.total_demand_kWh, 1),
        monthly_pv_kWh=[round(v, 1) for v in m.pv_production_kWh],
        monthly_demand_kWh=[round(v, 1) for v in m.demand_kWh],
        monthly_grid_import_kWh=[round(v, 1) for v in m.grid_import_kWh],
        monthly_grid_export_kWh=[round(v, 1) for v in m.grid_export_kWh],
    )


# ─────────────────────────────────────────────────────────────────────────────
# /economics/kpis  — standalone NPV/IRR/payback/LCOE
# ─────────────────────────────────────────────────────────────────────────────

def _irr(cash_flows: list[float], guess: float = 0.1, tol: float = 1e-7) -> float:
    """Bisection IRR — robust enough for monotone PV cash flows."""
    if not cash_flows or all(cf >= 0 for cf in cash_flows):
        return float("nan")
    lo, hi = -0.99, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        npv = sum(cf / ((1 + mid) ** i) for i, cf in enumerate(cash_flows))
        if abs(npv) < tol:
            return mid
        npv_lo = sum(cf / ((1 + lo) ** i) for i, cf in enumerate(cash_flows))
        if (npv_lo > 0) == (npv > 0):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


@router.post("/kpis", response_model=KPIResponse, summary="NPV / IRR / payback / LCOE")
async def kpis(req: KPIRequest) -> KPIResponse:
    n = req.lifetime_years
    om_yr = req.capex_eur * req.om_pct_of_capex_per_yr / 100.0

    incentive_total = req.capex_eur * req.incentive_pct / 100.0
    incentive_per_year = incentive_total / max(1, req.incentive_spread_years)

    cf: list[float] = [-req.capex_eur]
    for y in range(1, n + 1):
        deg = (1.0 - req.degradation_pct_yr / 100.0) ** (y - 1)
        esc = (1.0 + req.escalation) ** (y - 1)
        saving = req.annual_saving_eur * deg * esc
        om = om_yr * (1.0 + 0.02) ** (y - 1)
        repl = (
            req.capex_eur * req.inverter_replacement_cost_pct
            if y == req.inverter_replacement_year and req.inverter_replacement_year > 0
            else 0.0
        )
        inc = incentive_per_year if y <= req.incentive_spread_years else 0.0
        cf.append(saving - om - repl + inc)

    # NPV
    r = req.discount_rate
    discounted = [cf[i] / ((1.0 + r) ** i) for i in range(len(cf))]
    npv = sum(discounted)

    # Cumulative
    cum = []
    running = 0.0
    for v in cf:
        running += v
        cum.append(running)

    # Simple payback
    payback_simple = float("inf")
    for i, c in enumerate(cum):
        if c >= 0 and i > 0:
            prev = cum[i - 1]
            payback_simple = (i - 1) + (-prev) / max(1e-6, c - prev)
            break

    # Discounted payback
    cum_disc = []
    rd = 0.0
    for v in discounted:
        rd += v
        cum_disc.append(rd)
    payback_disc = float("inf")
    for i, c in enumerate(cum_disc):
        if c >= 0 and i > 0:
            prev = cum_disc[i - 1]
            payback_disc = (i - 1) + (-prev) / max(1e-6, c - prev)
            break

    # IRR
    try:
        irr = _irr(cf) * 100.0
    except Exception:
        irr = float("nan")

    # LCOE
    lcoe = float("nan")
    if req.annual_production_kWh > 0:
        total_cost = req.capex_eur + sum(
            (om_yr * (1.0 + 0.02) ** (y - 1)) / ((1.0 + r) ** y)
            for y in range(1, n + 1)
        )
        total_prod = sum(
            req.annual_production_kWh
            * (1.0 - req.degradation_pct_yr / 100.0) ** (y - 1)
            / ((1.0 + r) ** y)
            for y in range(1, n + 1)
        )
        lcoe = total_cost / total_prod if total_prod > 0 else float("nan")

    # Score (heuristic 0-10)
    score = 0.0
    if irr == irr:                 # not NaN
        score += min(5.0, max(0.0, irr / 4.0))
    if payback_simple < 25:
        score += max(0.0, 5.0 - payback_simple / 4.0)
    score = round(min(10.0, score), 1)

    return KPIResponse(
        npv_eur=round(npv, 0),
        irr_pct=round(irr, 2) if irr == irr else 0.0,
        payback_simple_yr=round(payback_simple, 1) if payback_simple != float("inf") else 99.0,
        payback_discounted_yr=round(payback_disc, 1) if payback_disc != float("inf") else 99.0,
        lcoe_eur_kWh=round(lcoe, 4) if lcoe == lcoe else 0.0,
        score=score,
        cash_flows=[round(v, 2) for v in cf],
        cumulative_cash_flows=[round(v, 2) for v in cum],
    )


# ─────────────────────────────────────────────────────────────────────────────
# /economics/incentives/*
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/incentives/residential", response_model=IncentiveResidentialResponse,
             summary="IRPEF deduction (residential)")
async def incentive_residential(req: IncentiveResidentialRequest) -> IncentiveResidentialResponse:
    total_benefit = req.capex_eur * req.detrazione_irpef_pct / 100.0
    annual = total_benefit / req.spread_years
    return IncentiveResidentialResponse(
        detrazione_irpef_pct=req.detrazione_irpef_pct,
        spread_years=req.spread_years,
        annual_benefit=round(annual, 2),
        total_benefit=round(total_benefit, 2),
        net_capex_eur=round(req.capex_eur - total_benefit, 2),
    )


@router.post("/incentives/school", response_model=IncentiveSchoolResponse,
             summary="IVA agevolata + PNRR + CER for public schools")
async def incentive_school(req: IncentiveSchoolRequest) -> IncentiveSchoolResponse:
    from school.incentives_school import calc_school_incentives
    out = calc_school_incentives(
        capex_eur=req.capex_eur,
        kwp=req.peak_power_kWp,
        is_public=req.is_public,
        has_pnrr=req.has_pnrr,
        pnrr_grant_pct=req.pnrr_grant_pct,
        cer_participation=req.cer_participation,
        cer_incentive_eur_kwh=req.cer_incentive_eur_kWh,
        cer_shared_fraction=req.cer_shared_fraction,
        annual_production_kwh=req.annual_production_kWh,
    )
    return IncentiveSchoolResponse(**out)


# ─────────────────────────────────────────────────────────────────────────────
# /economics/scenario-comparison  — wraps /simulate with gas/HP enabled
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/scenario-comparison", response_model=ScenarioComparisonResponse,
             summary="Multi-scenario gas/HP comparison (delegates to engine pipeline)")
async def scenario_comparison(request: Request, req: ScenarioComparisonRequest) -> ScenarioComparisonResponse:
    from api.routers.simulate import simulate as run_full_simulate
    try:
        sim_req = FullSimulationRequest(**req.simulation_request)
    except Exception as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Invalid simulation_request: {e}")

    # Force gas on so the engine runs the full scenario set
    sim_req.gas_system.has_gas = True

    sim_resp = await run_full_simulate(request, sim_req)
    sc = sim_resp.scenario_comparison
    if sc is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Engine did not produce a scenario comparison — check gas_system inputs.",
        )

    out = {
        sid: ScenarioOutcome(
            scenario_id=s.scenario_id, label=s.label,
            capex_eur=s.capex_eur,
            annual_total_energy_cost_eur=s.annual_total_energy_cost_eur,
            annual_saving_vs_baseline_eur=s.annual_saving_vs_baseline_eur,
            npv_eur=s.npv_eur, irr_pct=s.irr_pct,
            payback_simple_yr=s.payback_simple_yr,
            co2_annual_kg=s.co2_annual_kg, co2_saving_pct=s.co2_saving_pct,
            seasonal_cop=s.seasonal_cop, hp_coverage_pct=s.hp_coverage_pct,
            score=s.score,
        )
        for sid, s in sc.scenarios.items()
    }
    ranked = sorted(out.keys(), key=lambda k: out[k].npv_eur, reverse=True)
    return ScenarioComparisonResponse(
        scenarios=out,
        ranked=ranked,
        recommended=sc.recommended_scenario_id,
        rationale=sc.rationale,
        current_gas_price_eur_sm3=sc.current_gas_price_eur_sm3,
        break_even_gas_price_eur_sm3=sc.breakeven_gas_price_eur_sm3,
        gas_price_sensitivity=sc.gas_price_sensitivity,
    )
