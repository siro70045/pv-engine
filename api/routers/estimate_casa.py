"""Public Level-1 residential estimate endpoint (POST /estimate/casa).

Assembles the three residential/ modules into a single response:

    lookup_comune → get_cached_tmy →
    [proxy OR bolletta] consumption estimate →
    calc_loads (profile shape) →
    simulate_pv →
    compare_battery_scenarios ×3 (low/central/high demand) →
    verdetto onesto

PV kWp is FIXED across the three demand runs (same decision, building
consumption is the uncertainty).  Battery taglia scales with central demand.

Constraints (non-negotiable):
  - ammortamento_accelerato = False, transizione_5_0_pct = 0.0
  - Verdetto negativo NEVER suppressed
  - Battery trade-off: always both scenarios, never only the favourable half
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.schemas.estimate import CandidatoComune, ComuneRisolto, RangeLowHigh
from api.schemas.estimate_casa import (
    EstimateCasaRequest,
    EstimateCasaResponse,
    ScenarioOut,
    TradeoffOut,
    VerdettoPV,
)
from api.utils.cache import get_cached_tmy
from api.utils.cost import auto_scenarios, estimate_pv_cost_residential
from buildings import kwp_from_area
from engine.energy_vectors import BuildingEnergyProfile
from engine.loads import (
    DHWSystem,
    HVACSystem,
    LightingSystem,
    LoadsInput,
    OccupancySchedule,
    VentilationSystem,
    calc_loads,
)
from engine.pvgis_client import PVGISError
from engine.solar import PVSystem, simulate_pv
from residential.battery_scenario import BatteryScenarioComparison, ScenarioResult, compare_battery_scenarios
from residential.estimate_defaults import build_residential_consumption_estimate
from school.comuni import Comune, lookup_comune

logger = logging.getLogger("pv_engine_api.estimate_casa")

router = APIRouter(prefix="/estimate/casa", tags=["estimate"])

# Italian grid CO₂ factor [kg/kWh] — single source of truth (ENTSO-E 2024)
_CO2_KG_PER_KWH = next(
    f.default for f in dataclasses.fields(BuildingEnergyProfile)
    if f.name == "co2_electricity_kg_per_kwh"
)

# Battery taglia thresholds [kWh/year → kWh nominal]
_BATTERY_KWH_TIERS = [
    (2_000.0, 3.0),   # low-consumption households
    (4_000.0, 5.0),   # medium consumption
    (float("inf"), 7.0),
]


def _battery_size_kwh(consumo_centrale: float) -> float:
    for threshold, bat_kwh in _BATTERY_KWH_TIERS:
        if consumo_centrale <= threshold:
            return bat_kwh
    return 7.0


def _fmt_it(value: float) -> str:
    """Integer formatting with Italian thousands separator."""
    return f"{value:,.0f}".replace(",", ".")


def _to_scenario_out(sr: ScenarioResult) -> ScenarioOut:
    return ScenarioOut(
        label=sr.label,
        capex_eur=sr.capex_eur,
        battery_kwh=sr.battery_kwh,
        autoconsumo_pct=sr.autoconsumo_pct,
        autosufficienza_pct=sr.autosufficienza_pct,
        payback_anni=min(sr.payback_anni, 99.0),
        npv_eur=sr.npv_eur,
        irr_pct=sr.irr_pct,
        detrazione_totale_eur=sr.detrazione_totale_eur,
        detrazione_effettiva_eur=sr.detrazione_effettiva_eur,
        risparmio_annuo_anno1_eur=sr.risparmio_annuo_anno1_eur,
        produzione_anno1_kwh=sr.produzione_anno1_kwh,
    )


@router.post("", response_model=EstimateCasaResponse,
             summary="Public Level-1 preliminary residential estimate")
@limiter.limit("5/minute")
async def estimate_casa(request: Request, req: EstimateCasaRequest) -> EstimateCasaResponse:
    # ── 1. Resolve comune ─────────────────────────────────────────────────────
    matches = lookup_comune(req.comune, req.provincia)
    if not matches:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Comune {req.comune!r}"
            + (f" (provincia {req.provincia.upper()!r})" if req.provincia else "")
            + " non trovato. Verifica la denominazione ISTAT ufficiale.",
        )
    if len(matches) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    f"Esistono {len(matches)} comuni chiamati {req.comune!r}: "
                    "specifica la provincia."
                ),
                "candidati": [
                    CandidatoComune(
                        nome=c.nome, provincia=c.sigla_provincia, regione=c.regione,
                    ).model_dump()
                    for c in matches
                ],
            },
        )
    comune: Comune = matches[0]

    # ── 2. One shared TMY fetch ───────────────────────────────────────────────
    try:
        tmy, _ = get_cached_tmy(comune.lat, comune.lon)
    except PVGISError as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Il servizio meteo europeo PVGIS non è raggiungibile al momento "
            f"({e}). Riprova tra qualche minuto.",
        )

    # ── 3. Consumption estimate ───────────────────────────────────────────────
    if req.bolletta_kwh is not None:
        centrale = float(req.bolletta_kwh)
        basso = round(centrale * 0.85 / 10) * 10
        alto = round(centrale * 1.15 / 10) * 10
        consumo_da_bolletta = True
        avvertenze_consumo: list[str] = []
    else:
        ce = build_residential_consumption_estimate(
            n_occupants=req.n_occupants,
            floor_area_m2=req.floor_area_m2,
            riscaldamento_elettrico=req.riscaldamento_elettrico,
            acqua_calda_elettrica=req.acqua_calda_elettrica,
            ev=req.ev,
            condizionatore=req.condizionatore,
        )
        centrale = ce.consumo_annuo_kwh_centrale
        basso = ce.consumo_annuo_kwh_basso
        alto = ce.consumo_annuo_kwh_alto
        consumo_da_bolletta = False
        avvertenze_consumo = ce.avvertenze

    # ── 4. PV size (fixed across the sweep) ──────────────────────────────────
    # Residential roof: half of floor area, pitched, high-density layout
    roof_area = req.floor_area_m2 * 0.5
    kwp_max = kwp_from_area(roof_area, "roof_integrated", 100.0)
    taglie = auto_scenarios(centrale, kwp_max)
    if req.potenza_kwp is not None:
        kwp, auto_dimensionato = float(req.potenza_kwp), False
    else:
        kwp, auto_dimensionato = taglie["recommended"], True
    pv_capex = estimate_pv_cost_residential(kwp)
    pv_cost_per_kwp = pv_capex / kwp

    # ── 5. PV production profile ─────────────────────────────────────────────
    pv_sys = PVSystem(peak_power_kWp=kwp, tilt_deg=30, azimuth_deg=180)
    pv_result = simulate_pv(tmy, pv_sys, mounting="close_roof")
    # P_ac_W [W] for 1-hour intervals = [Wh]
    pv_hourly_Wh = [h.P_ac_W for h in pv_result.hourly]
    produzione_annua_kwh = pv_result.production_annual_kWh

    # ── 6. Demand profile shape (profile drives hourly distribution) ──────────
    profile_type = (
        "residential_working_family"
        if req.presenza_diurna == "per_lo_piu_vuota"
        else "residential"
    )
    raw_loads = calc_loads(LoadsInput(
        floor_area_m2=req.floor_area_m2,
        building_type=profile_type,
        occupancy=OccupancySchedule(
            building_type=profile_type,
            n_occupants=req.n_occupants,
        ),
        dhw=DHWSystem(daily_demand_L_per_person=50.0, is_electric=False),
        ventilation=VentilationSystem(
            airflow_m3h_per_person=36.0,
            specific_fan_power_W_m3h=0.0,
            operation_hours_per_day=0.0,
        ),
        lighting=LightingSystem(
            installed_power_W_m2=8.0,
            floor_area_m2=req.floor_area_m2,
            daylight_factor=0.15,
        ),
        appliances=[],
        hvac=HVACSystem(heating_system="gas_boiler", cooling_system="none"),
        heating_demand_monthly_kWh=[0.0] * 12,
        cooling_demand_monthly_kWh=[0.0] * 12,
    ))
    raw_Wh = raw_loads.hourly_demand_Wh
    raw_total = sum(raw_Wh) or 1.0  # guard against degenerate zero profile

    def _scale(target_kwh: float) -> list[float]:
        factor = target_kwh * 1000.0 / raw_total
        return [w * factor for w in raw_Wh]

    # ── 7. Battery taglia (dynamic, scales with central consumption) ───────────
    battery_kwh = _battery_size_kwh(centrale)

    # ── 8. Simulations ×3 (fixed kWp, three demand levels) ───────────────────
    def _run(demand_Wh: list[float]) -> BatteryScenarioComparison:
        return compare_battery_scenarios(
            pv_hourly_Wh=pv_hourly_Wh,
            demand_hourly_Wh=demand_Wh,
            pv_kwp=kwp,
            pv_cost_eur_kwp=pv_cost_per_kwp,
            abitazione_principale=req.abitazione_principale,
            irpef_annua_stimata=req.irpef_annua_stimata,
            battery_kwh=battery_kwh,
        )

    cmp_centrale = _run(_scale(centrale))
    cmp_basso = _run(_scale(basso))
    cmp_alto = _run(_scale(alto))

    # ── 9. Ranges (senza_batteria across the three demand levels) ─────────────
    all_sb = [
        cmp_basso.senza_batteria,
        cmp_centrale.senza_batteria,
        cmp_alto.senza_batteria,
    ]
    risparmio_range = RangeLowHigh(
        low=round(min(sc.risparmio_annuo_anno1_eur for sc in all_sb), 0),
        high=round(max(sc.risparmio_annuo_anno1_eur for sc in all_sb), 0),
    )
    payback_range = RangeLowHigh(
        low=round(min(min(sc.payback_anni, 99.0) for sc in all_sb), 1),
        high=round(min(max(sc.payback_anni for sc in all_sb), 99.0), 1),
    )

    # ── 10. Verdetto onesto (central, senza_batteria) ─────────────────────────
    sb_central = cmp_centrale.senza_batteria
    pb = min(sb_central.payback_anni, 99.0)
    npv = sb_central.npv_eur
    if pb <= 8 and npv > 0:
        etichetta = "ottimo"
        testo_verdetto = (
            f"Investimento eccellente: payback ~{pb:.1f} anni, "
            f"NPV a 25 anni +€{_fmt_it(npv)}."
        )
    elif pb <= 12 and npv > 0:
        etichetta = "buono"
        testo_verdetto = (
            f"Investimento buono: payback ~{pb:.1f} anni, "
            f"NPV a 25 anni +€{_fmt_it(npv)}."
        )
    elif pb < 20 and npv > 0:
        etichetta = "accettabile"
        testo_verdetto = (
            f"Investimento accettabile: payback ~{pb:.1f} anni, "
            f"NPV a 25 anni +€{_fmt_it(npv)}. Valutare alternative."
        )
    else:
        etichetta = "negativo"
        testo_verdetto = (
            f"⚠ Investimento difficile da giustificare nelle assunzioni correnti: "
            f"payback ~{pb:.1f} anni, NPV a 25 anni {npv:+,.0f} €. "
            "Riconsiderare la taglia o aggiornare il consumo reale."
        )

    # ── 11. CO₂ evitata (central, self-consumed FV only) ─────────────────────
    self_consumed_kwh = produzione_annua_kwh * sb_central.autoconsumo_pct / 100.0
    co2_evitata = round(self_consumed_kwh * _CO2_KG_PER_KWH, 0)

    # ── 12. Assumptions block ─────────────────────────────────────────────────
    zona_label = f"Zona climatica {comune.zona_climatica} — fonte DPR 412/93 (tabella ENEA)"
    if comune.zona_fonte == "nearest_neighbor":
        zona_label += " — comune nato da fusione, zona ereditata dal comune limitrofo"
    profilo_label = (
        "adulti al lavoro / figli a scuola (picco serale)"
        if req.presenza_diurna == "per_lo_piu_vuota"
        else "occupazione diurna alta (pensionati / smart-working)"
    )
    consumo_fonte_label = (
        f"Consumo fornito dalla bolletta: {_fmt_it(centrale)} kWh/anno (banda ±15%)"
        if consumo_da_bolletta
        else (
            f"Consumo stimato dal proxy: {_fmt_it(centrale)} kWh/anno "
            f"({_fmt_it(basso)}–{_fmt_it(alto)} kWh/anno, "
            f"banda {'±45%' if req.riscaldamento_elettrico else '±30%'})"
        )
    )

    assunzioni = [
        consumo_fonte_label,
        f"Profilo di presenza diurna: {profilo_label}",
        f"Superficie: {_fmt_it(req.floor_area_m2)} m² → area tetto stimata "
        f"{_fmt_it(roof_area)} m² (metà della superficie lorda, falda inclinata)",
        zona_label,
        f"Impianto: {kwp:.1f} kWp, tilt 30°, esposizione sud (close_roof), "
        + ("taglia consigliata calcolata sul consumo centrale"
           if auto_dimensionato else "potenza indicata dall'utente"),
        f"CAPEX indicativo residenziale: €{_fmt_it(pv_capex)} "
        f"(€{pv_cost_per_kwp:,.0f}/kWp, listino 2026)",
        f"Batteria confrontata: {battery_kwh:.0f} kWh nominali "
        f"(€{_fmt_it(battery_kwh * 550)}/kWh installato, ENEA RAEE 2023)",
        f"Detrazione IRPEF: {'50%' if req.abitazione_principale else '36%'} "
        f"{'(abitazione principale)' if req.abitazione_principale else '(altro immobile)'}, "
        f"10 quote annuali, tetto €96.000 — art. 16-bis TUIR (L. 207/2024)",
        "Prezzi energia: 0,25 €/kWh acquisto, 0,08 €/kWh cessione; "
        "orizzonte 25 anni, degrado FV 0,5 %/anno, escalation elettricità 2,5 %/anno",
        "Stima preliminare automatica — i valori reali dipendono dall'abitazione "
        "specifica e richiedono l'analisi di Livello 2",
    ]
    assunzioni.extend(avvertenze_consumo)

    return EstimateCasaResponse(
        comune_risolto=ComuneRisolto(
            nome=comune.nome,
            provincia=comune.sigla_provincia,
            regione=comune.regione,
            zona_climatica=comune.zona_climatica,
        ),
        consumo_kwh_annuo=centrale,
        consumo_kwh_basso=basso,
        consumo_kwh_alto=alto,
        consumo_da_bolletta=consumo_da_bolletta,
        kwp_ipotizzato=round(kwp, 1),
        auto_dimensionato=auto_dimensionato,
        taglie_disponibili={k: round(v, 1) for k, v in taglie.items()},
        produzione_annua_kwh=round(produzione_annua_kwh, 0),
        risparmio_annuo_eur=risparmio_range,
        payback_anni=payback_range,
        co2_evitata_kg_anno=co2_evitata,
        senza_batteria=_to_scenario_out(cmp_centrale.senza_batteria),
        con_batteria=_to_scenario_out(cmp_centrale.con_batteria),
        tradeoff=TradeoffOut(
            delta_autoconsumo_pp=cmp_centrale.tradeoff.delta_autoconsumo_pp,
            delta_autosufficienza_pp=cmp_centrale.tradeoff.delta_autosufficienza_pp,
            delta_payback_anni=cmp_centrale.tradeoff.delta_payback_anni,
            delta_npv_eur=cmp_centrale.tradeoff.delta_npv_eur,
            delta_capex_eur=cmp_centrale.tradeoff.delta_capex_eur,
            batteria_conveniente=cmp_centrale.tradeoff.batteria_conveniente,
            nota=cmp_centrale.tradeoff.nota,
        ),
        verdetto_pv=VerdettoPV(
            payback_anni=round(pb, 1),
            npv_eur=round(npv, 0),
            etichetta=etichetta,
            testo=testo_verdetto,
        ),
        capex_indicativo_eur=float(pv_capex),
        assunzioni=assunzioni,
    )
