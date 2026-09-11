"""Public Level-1 residential estimate endpoint (POST /estimate/casa).

Assembles the three residential/ modules into a single response:

    lookup_comune → get_cached_tmy →
    [proxy OR bolletta] consumption estimate →
    calc_loads (profile shape) →
    simulate_pv (recommended kWp) →
    compare_battery_scenarios ×2 (low/high demand for range) +
    compare_battery_scenarios ×2-3 (taglie_confronto, central demand) →
    verdetto onesto (pesa autosufficienza + economics)

PV kWp is FIXED across demand runs (same decision, building consumption is
the uncertainty).  Battery taglia scales with central demand.

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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.schemas.estimate import CandidatoComune, ComuneRisolto, RangeLowHigh
from api.schemas.estimate_casa import (
    BeneficioBreakdown,
    EstimateCasaRequest,
    EstimateCasaResponse,
    ScenarioOut,
    TagliaOut,
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

# Autosufficienza threshold: below this, verdict notes low energy independence
_AUTOSUFFICIENZA_AVVERTENZA_SOGLIA = 35.0


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


def _compute_beneficio_breakdown(sr: ScenarioResult) -> BeneficioBreakdown:
    """Scompone il beneficio anno 1 in: risparmio bolletta / export / quota detrazione."""
    saving = sr.economics.year1_saving_eur
    export = sr.economics.year1_export_revenue_eur
    quota_detr = sr.detrazione_totale_eur / 10.0
    return BeneficioBreakdown(
        risparmio_bolletta_eur=round(saving, 0),
        ricavi_export_eur=round(export, 0),
        quota_detrazione_eur=round(quota_detr, 0),
        totale_anno1_eur=round(saving + export + quota_detr, 0),
    )


def _compute_verdetto(
    pb: float,
    npv: float,
    autosufficienza_pct: float,
    beneficio: BeneficioBreakdown,
) -> VerdettoPV:
    """Verdetto onesto che pesa economics e copertura energetica.

    Autosufficienza < 35% degrada di un livello: il beneficio dipende
    principalmente da detrazione e export, non dalla riduzione della bolletta.
    """
    pb = min(pb, 99.0)

    # Financial grade: 0=negativo, 1=accettabile, 2=buono, 3=ottimo
    if pb <= 8 and npv > 0:
        fin_grade = 3
    elif pb <= 12 and npv > 0:
        fin_grade = 2
    elif pb < 20 and npv > 0:
        fin_grade = 1
    else:
        fin_grade = 0

    bassa_as = autosufficienza_pct < _AUTOSUFFICIENZA_AVVERTENZA_SOGLIA
    # Downgrade by one level if energy independence is low (only from buono/ottimo)
    if bassa_as and fin_grade >= 2:
        fin_grade -= 1

    etichetta = ("negativo", "accettabile", "buono", "ottimo")[fin_grade]

    if etichetta == "negativo":
        testo = (
            f"⚠ Investimento difficile da giustificare nelle assunzioni correnti: "
            f"payback ~{pb:.1f} anni, NPV a 25 anni {npv:+,.0f} €. "
            "Riconsiderare la taglia o aggiornare il consumo reale."
        )
    else:
        grade_label = {
            "ottimo": "Investimento eccellente",
            "buono": "Investimento buono",
            "accettabile": "Investimento accettabile",
        }[etichetta]
        parti = [
            f"{grade_label}: payback ~{pb:.1f} anni, NPV a 25 anni +€{_fmt_it(npv)}.",
            (
                f"Beneficio anno 1: risparmio bolletta €{beneficio.risparmio_bolletta_eur:.0f}"
                f" + export €{beneficio.ricavi_export_eur:.0f}"
                f" + quota detrazione €{beneficio.quota_detrazione_eur:.0f}"
                f" = €{beneficio.totale_anno1_eur:.0f}."
            ),
        ]
        if bassa_as:
            parti.append(
                f"⚠ Autosufficienza stimata {autosufficienza_pct:.0f}%: l'impianto copre "
                f"solo una quota ridotta del fabbisogno. Il beneficio dipende in larga "
                f"misura dalla detrazione fiscale e dai ricavi di cessione — non da una "
                f"effettiva riduzione dei prelievi dalla rete."
            )
        testo = " ".join(parti)

    return VerdettoPV(
        payback_anni=round(pb, 1),
        npv_eur=round(npv, 0),
        etichetta=etichetta,
        autosufficienza_pct=round(autosufficienza_pct, 1),
        testo=testo,
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

    # ── 4. PV size (fixed across demand runs) ────────────────────────────────
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

    # ── 5. PV simulation for primary kWp ─────────────────────────────────────
    pv_sys = PVSystem(peak_power_kWp=kwp, tilt_deg=30, azimuth_deg=180)
    pv_result = simulate_pv(tmy, pv_sys, mounting="close_roof")
    pv_hourly_Wh = [h.P_ac_W for h in pv_result.hourly]
    produzione_annua_kwh = pv_result.production_annual_kWh

    # ── 6. Demand profile shape ───────────────────────────────────────────────
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
    raw_total = sum(raw_Wh) or 1.0

    def _scale(target_kwh: float) -> list[float]:
        factor = target_kwh * 1000.0 / raw_total
        return [w * factor for w in raw_Wh]

    # ── 7. Battery taglia (dynamic, scales with central consumption) ───────────
    battery_kwh = _battery_size_kwh(centrale)

    # ── 8. Shared battery-comparison helper ───────────────────────────────────
    def _run_battery(
        pv_h: list[float],
        demand_Wh: list[float],
        kwp_val: float,
        capex_val: float,
    ) -> BatteryScenarioComparison:
        return compare_battery_scenarios(
            pv_hourly_Wh=pv_h,
            demand_hourly_Wh=demand_Wh,
            pv_kwp=kwp_val,
            pv_cost_eur_kwp=capex_val / kwp_val,
            abitazione_principale=req.abitazione_principale,
            irpef_annua_stimata=req.irpef_annua_stimata,
            battery_kwh=battery_kwh,
        )

    # ── 9. Primary scenario (central demand) + range (low/high demand) ────────
    cmp_centrale = _run_battery(pv_hourly_Wh, _scale(centrale), kwp, pv_capex)
    cmp_basso    = _run_battery(pv_hourly_Wh, _scale(basso),    kwp, pv_capex)
    cmp_alto     = _run_battery(pv_hourly_Wh, _scale(alto),     kwp, pv_capex)

    # ── 10. Ranges from low/central/high demand ───────────────────────────────
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

    # ── 11. Taglie confronto (2-3 auto_scenarios kWp, central demand each) ────
    kwp_min = taglie["minimal"]
    kwp_rec = taglie["recommended"]
    kwp_max_t = taglie["maximum"]

    kwp_vals = sorted(set([kwp_min, kwp_rec, kwp_max_t]))
    if len(kwp_vals) < 3:
        # Add a midpoint between min and max to get 3 comparison points
        mid_raw = (min(kwp_vals) + max(kwp_vals)) / 2.0
        mid = round(mid_raw * 2) / 2  # round to nearest 0.5 kWp
        mid = max(min(kwp_vals), min(max(kwp_vals), mid))
        kwp_vals = sorted(set(kwp_vals + [mid]))

    def _taglia_label(kwp_val: float) -> str:
        if abs(kwp_val - kwp_rec) < 0.01:
            return "consigliata"
        elif abs(kwp_val - min(kwp_vals)) < 0.01:
            return "minima"
        elif abs(kwp_val - max(kwp_vals)) < 0.01:
            return "massima"
        return "media"

    taglie_confronto: list[TagliaOut] = []
    for kwp_val in kwp_vals:
        # Reuse primary PV simulation if same kWp; otherwise simulate fresh
        if abs(kwp_val - kwp) < 0.01:
            pv_h_t = pv_hourly_Wh
            prod_t = produzione_annua_kwh
            capex_t = pv_capex
            cmp_t = cmp_centrale
        else:
            pv_sys_t = PVSystem(peak_power_kWp=kwp_val, tilt_deg=30, azimuth_deg=180)
            pv_res_t = simulate_pv(tmy, pv_sys_t, mounting="close_roof")
            pv_h_t = [h.P_ac_W for h in pv_res_t.hourly]
            prod_t = pv_res_t.production_annual_kWh
            capex_t = estimate_pv_cost_residential(kwp_val)
            cmp_t = _run_battery(pv_h_t, _scale(centrale), kwp_val, capex_t)

        sb_t = cmp_t.senza_batteria
        beneficio_t = _compute_beneficio_breakdown(sb_t)
        verdetto_t = _compute_verdetto(
            min(sb_t.payback_anni, 99.0), sb_t.npv_eur,
            sb_t.autosufficienza_pct, beneficio_t,
        )
        taglie_confronto.append(TagliaOut(
            label=_taglia_label(kwp_val),
            kwp=round(kwp_val, 1),
            capex_eur=float(capex_t),
            produzione_annua_kwh=round(prod_t, 0),
            autoconsumo_pct=sb_t.autoconsumo_pct,
            autosufficienza_pct=sb_t.autosufficienza_pct,
            risparmio_annuo_anno1_eur=sb_t.risparmio_annuo_anno1_eur,
            payback_anni=min(sb_t.payback_anni, 99.0),
            npv_eur=sb_t.npv_eur,
            beneficio_breakdown=beneficio_t,
            verdetto=verdetto_t,
            senza_batteria=_to_scenario_out(sb_t),
            con_batteria=_to_scenario_out(cmp_t.con_batteria),
            tradeoff=TradeoffOut(
                delta_autoconsumo_pp=cmp_t.tradeoff.delta_autoconsumo_pp,
                delta_autosufficienza_pp=cmp_t.tradeoff.delta_autosufficienza_pp,
                delta_payback_anni=cmp_t.tradeoff.delta_payback_anni,
                delta_npv_eur=cmp_t.tradeoff.delta_npv_eur,
                delta_capex_eur=cmp_t.tradeoff.delta_capex_eur,
                batteria_conveniente=cmp_t.tradeoff.batteria_conveniente,
                nota=cmp_t.tradeoff.nota,
            ),
        ))

    # ── 12. Primary verdetto (primary kWp, central demand) ────────────────────
    sb_central = cmp_centrale.senza_batteria
    beneficio_centrale = _compute_beneficio_breakdown(sb_central)
    verdetto_pv = _compute_verdetto(
        min(sb_central.payback_anni, 99.0), sb_central.npv_eur,
        sb_central.autosufficienza_pct, beneficio_centrale,
    )

    # ── 13. CO₂ evitata (central, self-consumed FV only) ─────────────────────
    self_consumed_kwh = produzione_annua_kwh * sb_central.autoconsumo_pct / 100.0
    co2_evitata = round(self_consumed_kwh * _CO2_KG_PER_KWH, 0)

    # ── 14. Assumptions block ─────────────────────────────────────────────────
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
        f"CAPEX indicativo residenziale: €{_fmt_it(pv_capex)} totali "
        f"(€{pv_cost_per_kwp:,.0f}/kWp medio, include €1.500 costi fissi "
        f"ponteggio/inverter/CILA)",
        f"Batteria confrontata: {battery_kwh:.0f} kWh nominali "
        f"(totale €{_fmt_it(battery_kwh * 550)} — €550/kWh installato, ENEA RAEE 2023)",
        f"Detrazione IRPEF: {'50%' if req.abitazione_principale else '36%'} "
        f"{'(abitazione principale)' if req.abitazione_principale else '(altro immobile)'}, "
        f"10 quote annuali, tetto €96.000 — art. 16-bis TUIR (L. 207/2024)",
        "Prezzi energia: 0,25 €/kWh acquisto, 0,08 €/kWh cessione; "
        "orizzonte 25 anni, degrado FV 0,5 %/anno, escalation elettricità 2,5 %/anno",
        "⚠ Detrazione 50% valida per spese 2026 (L. 207/2024): "
        "scende al 36% dal 2027 salvo ulteriori proroghe.",
        "⚠ Prezzo di cessione 0,08 €/kWh (scambio sul posto GSE 2026): "
        "in caso di rinegoziazione o passaggio a ritiro dedicato il ricavo può variare.",
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
        verdetto_pv=verdetto_pv,
        taglie_confronto=taglie_confronto,
        capex_indicativo_eur=float(pv_capex),
        assunzioni=assunzioni,
    )
