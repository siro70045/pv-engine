"""Public Level-1 residential estimate endpoint (POST /estimate/casa).

Assembles the three residential/ modules into a single response:

    lookup_comune → get_cached_tmy →
    [proxy OR bolletta] consumption estimate →
    calc_loads (profile shape) →
    simulate_pv × N taglie →
    compare_battery_scenarios × N (central demand, for kWp selection) +
    compare_battery_scenarios × 2 (low/high demand for range, primary kWp) →
    verdetto onesto (pesa autosufficienza + economics)

AUTO-SIZING POLICY (non-negotiable):
  When the user does NOT specify potenza_kwp, the primary kWp is chosen as:

    1. CAP: only candidates where production ≤ 150% of annual consumption are
       considered (prevents recommending systems that primarily sell to the grid
       at the fragile 0.08 €/kWh export price).
    2. NPV-BEST: among those, pick the one with the highest senza_batteria NPV.
    3. FORCE-MINIMA: if all candidates exceed the cap (e.g. even the minimum
       market size of 1.5 kWp overshoots 150%), use the smallest candidate and
       flag it explicitly in the verdict.

  If all cap-eligible candidates have NPV ≤ 0, verdetto_pv.testo says so
  explicitly and mentions that larger sizes have higher absolute NPV but depend
  primarily on fragile assumptions (export price, fiscal deduction).

Constraints (non-negotiable):
  - ammortamento_accelerato = False, transizione_5_0_pct = 0.0
  - Verdetto negativo NEVER suppressed
  - Battery trade-off: always both scenarios, never only the favourable half
  - auto_scenarios must NOT be modified (shared with school flow)
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.schemas.estimate import CandidatoComune, ComuneRisolto, RangeLowHigh
from api.schemas.estimate_casa import (
    BeneficioBreakdown,
    DeltaVsBase,
    EstimateCasaRequest,
    EstimateCasaResponse,
    MetricheScenarioAlternativo,
    ScenarioAlternativoOut,
    ScenarioOut,
    TagliaOut,
    TradeoffOut,
    VerdettoPV,
)
from api.utils.cache import get_cached_tmy
from api.utils.cost import auto_scenarios, estimate_pv_cost_residential
from buildings import kwp_from_area
from engine.economics import (
    FinancialParameters,
    ItalianIncentives,
    InvestmentCost,
    OperatingCost,
    calc_economics,
)
from engine.energy_balance import (
    BatterySystem,
    EnergyBalanceInput,
    GridTariff,
    simulate_lifetime,
)
from engine.energy_vectors import BuildingEnergyProfile, TERMINAL_SUPPLY_TEMP_C, TerminalType
from engine.heat_pump import calc_h_tr_from_annual, calc_hourly_thermal_demand
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
from residential.battery_scenario import (
    BATTERY_REPLACEMENT_COST_EUR_KWH,
    BatteryScenarioComparison,
    DEFAULT_ANALYSIS_YEARS,
    DEFAULT_DISCOUNT_RATE,
    DEFAULT_ESC_RATE,
    DEFAULT_EXPORT_PRICE_EUR_KWH,
    DEFAULT_IMPORT_PRICE_EUR_KWH,
    DEFAULT_OM_PCT,
    ScenarioResult,
    compare_battery_scenarios,
)
from residential.estimate_defaults import (
    _DHW_KWH,
    _DHW_MAX,
    _HP_KWH_PER_M2_CENTRAL,
    build_residential_consumption_estimate,
)
from residential.incentives import calc_irpef_deduction
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
    (2_000.0, 3.0),
    (4_000.0, 5.0),
    (float("inf"), 7.0),
]

# Autosufficienza threshold: below this, verdict notes low energy independence
_AUTOSUFFICIENZA_AVVERTENZA_SOGLIA = 35.0

# Sizing cap: auto-sizing only considers candidates whose annual production
# does not exceed this multiple of the estimated annual consumption.
# Above 150%: the system primarily sells to the grid at the fragile 0.08 €/kWh
# export price — not a "house-oriented" system.
_MAX_PRODUZIONE_SU_CONSUMO = 1.50

# Transparency warning threshold: when the selected kWp (even user-specified)
# produces more than this multiple of consumption, warn in the verdetto text.
_AVVISO_SOVRADIMENSIONAMENTO_RATIO = 1.50

# Azimuth mapping for user-selected roof orientation.
# Conventions: 0°=N, 90°=E, 180°=S, 270°=W.
# est_o_ovest uses 270° (west face) as proxy; east gives same annual total.
# non_so uses 225° (SW) as a prudent middle-ground for unknown orientation.
_ORIENTAMENTO_AZIMUTH: dict[str, float] = {
    "sud":               180.0,
    "sud_est_sud_ovest": 200.0,
    "est_o_ovest":       270.0,   # una sola falda: est O ovest, proxy ovest puro
    "nord":              0.0,
    "non_so":            225.0,
}

# ── Gas → thermal → electrical conversion (for electrification scenario) ─────
# PCI (Potere Calorifico Inferiore) gas naturale italiano: 9.45 kWh/Smc
# Fonte: DM 26/06/1993 (Ministero dell'Industria), tabella TERNA/ARERA standard.
_GAS_PCI_KWH_PER_SMC: float = 9.45

# Rendimento medio caldaia a condensazione: 0.87 (su PCI, classe energetica B+).
# Fonte: EN 15316-4-1:2017 tabella nazionale. Conservativo rispetto alle classi A+/A++
# (η > 1.0 su PCI per condensazione piena) per catturare il parco caldaie misto reale.
_ETA_BOILER: float = 0.87

# SCOP prudente per pompa di calore aria-aria in regime italiano (zona C-E media).
# Range tipico ENEA RAEE 2023: 2.5–3.5 in base a zona e installazione.
# 2.5 = limite inferiore (zona F, installazione sub-ottimale): scelta prudente perché
# stima più consumo elettrico → scenario più conservativo sul beneficio FV.
_SCOP_PRUDENTE: float = 2.5

# COP pompa di calore per ACS (scaldabagno a pompa di calore):
# - Più basso di quello di riscaldamento perché la temperatura di accumulo è più alta (45°C).
# - Conservativo con termosifoni/non_so (2.0): HP ad alta temp. fa anche ACS meno efficiente.
_COP_ACS_STANDARD: float = 2.5    # terminali buoni (fan coil, pavimento radiante)
_COP_ACS_CONSERVATIVO: float = 2.0  # termosifoni / non_so (worst case)

# COP per cottura a induzione vs gas (~1:1 come output termico utile):
# Gas hob η≈0.45; induzione η≈0.85 → 1 kWh_termico_gas ≈ 0.5 kWh_e_induzione.
# Usiamo COP=1.0 (conservativo) per non sottostimare il consumo elettrico.
_COP_COTTURA: float = 1.0

# Default SCOP benchmark (senza terminal type, senza gas_smc_annui):
# 2.75 = ENEA RAEE 2023 (range 2.5–3.5, media zona C-E).
_SCOP_BENCHMARK: float = 2.75

# Carnot efficiency factor η per HP aria-acqua classe A++ (EN 14825):
_HP_ETA_CARNOT: float = 0.42

# Temperatura di setpoint riscaldamento [°C] (standard EN 15316-4-2)
_HP_T_SETPOINT_C: float = 20.0

# Temperatura minima operativa HP [°C] (sotto questo valore HP spenta)
_HP_T_MIN_C: float = -15.0

# Gas use split quando solo gas_smc_annui è noto (senza gas_smc_estivi_mese).
# Fonte: ARERA Relazione Annuale 2023; ISTAT Consumi Energetici delle Famiglie 2022.
# Cottura: base 75 Smc + 15 Smc/occupante (da 2 in poi).
# ACS: 45 Smc/occupante (UNI TS 11300-2: 50 L/gg × ΔT ~35°C × 4190 J/(kg·K) / PCI / η)
_GAS_COTTURA_BASE_SMC: float = 75.0
_GAS_COTTURA_PER_OCCUPANT_SMC: float = 15.0
_GAS_ACS_SMC_PER_OCCUPANT: float = 45.0

# Quando gas_smc_estivi_mese è fornito, la quota base estate è cottura + ACS.
# ISTAT: cottura ≈ 35–40% della spesa base estiva per una famiglia con cucina a gas.
_FRAC_ACS_SU_BASE_ESTIVA: float = 0.60   # 60% ACS, 40% cottura

# Human-readable label for each orientation (used in assunzioni).
_ORIENTAMENTO_DESC: dict[str, str] = {
    "sud":              "SUD (azimuth 180°, esposizione ottimale)",
    "sud_est_sud_ovest": "SUD-SUD-OVEST (azimuth 200°, ~20° dal sud — produzione ~97% rispetto a sud pieno)",
    "est_o_ovest":      "EST O OVEST — una sola falda (azimuth 270°, modellata come ovest puro — "
                        "produzione ~80% rispetto a sud pieno; vale sia per falda est che ovest in "
                        "termini di produzione annua; un impianto su DUE falde richiede l'analisi di Livello 2)",
    "nord":             "NORD (azimuth 0°, esposizione sfavorevole — produzione ~55-60% rispetto a sud pieno)",
    "non_so":           "SUD-OVEST (azimuth 225°, ipotesi prudente per orientamento non noto — "
                        "produzione ~90-95% rispetto a sud pieno; l'orientamento reale può "
                        "spostare la stima del ±10-20%)",
}


@dataclass
class _TagliaData:
    """Pre-computed PV + central-demand battery scenarios for one kWp candidate."""
    kwp: float
    pv_hourly_Wh: list[float]
    produzione_kwh: float
    capex: int
    cmp: BatteryScenarioComparison


def _battery_size_kwh(consumo_centrale: float) -> float:
    for threshold, bat_kwh in _BATTERY_KWH_TIERS:
        if consumo_centrale <= threshold:
            return bat_kwh
    return 7.0


def _fmt_it(value: float) -> str:
    """Integer formatting with Italian thousands separator."""
    return f"{value:,.0f}".replace(",", ".")


def _scop_from_tmy(t_ext_hourly: list[float], t_supply_c: float) -> float:
    """Demand-weighted harmonic mean COP (= SCOP) for a site from TMY temperatures.

    Uses Carnot model with η=0.42 (EN 14825 calibrated, A++ class) and defrost
    penalty below 5°C. Weighted by degree-hours (thermal demand proxy).
    """
    T_hot_K = t_supply_c + 273.15
    total_demand = 0.0
    total_elec = 0.0
    for t_ext in t_ext_hourly:
        if t_ext >= _HP_T_SETPOINT_C or t_ext <= _HP_T_MIN_C:
            continue
        demand = _HP_T_SETPOINT_C - t_ext   # degree-hours (proportional to thermal demand)
        if demand <= 0:
            continue
        T_source_K = t_ext + 273.15
        delta = T_hot_K - T_source_K
        if delta <= 0:
            continue
        cop = _HP_ETA_CARNOT * T_hot_K / delta
        if t_ext < 5.0:
            cop *= 0.85   # defrost penalty
        if cop <= 0:
            continue
        total_demand += demand
        total_elec += demand / cop
    if total_elec <= 0:
        return _SCOP_PRUDENTE
    return total_demand / total_elec


def _terminal_supply_temp(terminali: str | None) -> float:
    """Return mid-range supply temperature [°C] for the given terminal type."""
    mapping = {
        "termosifoni":        TerminalType.OLD_RADIATOR,
        "pavimento_radiante": TerminalType.RADIANT_FLOOR,
        "ventilconvettori":   TerminalType.FAN_COIL,
        "non_so":             TerminalType.OLD_RADIATOR,   # worst-case assumption
    }
    term = mapping.get(terminali or "non_so", TerminalType.OLD_RADIATOR)
    t_lo, t_hi = TERMINAL_SUPPLY_TEMP_C[term]
    return (t_lo + t_hi) / 2.0


def _heating_elec_profile_wh(
    t_ext_hourly: list[float],
    heating_kwh_e: float,
) -> list[float]:
    """Return winter-concentrated hourly HP electric demand [Wh/h], total = heating_kwh_e kWh.

    Uses TMY-derived H_tr (building heat-loss coefficient) to distribute the
    annual electrical demand to hours where outdoor T < 20°C (heating season).
    Because SCOP cancels in the normalization, the shape depends only on temperature.
    """
    if heating_kwh_e <= 0:
        return [0.0] * 8760
    h_tr = calc_h_tr_from_annual(heating_kwh_e, t_ext_hourly)
    thermal_Wh = calc_hourly_thermal_demand(t_ext_hourly, h_tr, heating_kwh_e)
    # thermal_Wh sums to heating_kwh_e kWh (normalized inside calc_hourly_thermal_demand).
    # Since SCOP cancels, this is already the correct electric profile shape at the right total.
    return thermal_Wh


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
    prod_ratio: float = 0.0,
    capex_eur: float = 0.0,
) -> VerdettoPV:
    """Compute the honest verdict for a PV scenario.

    capex_eur is used to detect fragile-positive investments: NPV/capex < 10%
    (= barely above break-even over 25 years, ~0.4% nominal annual return)
    triggers the "marginale" label instead of "accettabile".
    With capex_eur=0 (default) the marginale threshold is never triggered.
    """
    pb = min(pb, 99.0)

    if pb <= 8 and npv > 0:
        fin_grade = 3
    elif pb <= 12 and npv > 0:
        fin_grade = 2
    elif pb < 20 and npv > 0:
        fin_grade = 1
    else:
        fin_grade = 0

    bassa_as = autosufficienza_pct < _AUTOSUFFICIENZA_AVVERTENZA_SOGLIA
    if bassa_as and fin_grade >= 2:
        fin_grade -= 1

    etichetta = ("negativo", "accettabile", "buono", "ottimo")[fin_grade]

    # Marginale override: NPV > 0 but below 10% of capex over 25 years.
    # 10% of capex in 25 years ≈ 0.4% nominal annual return — below inflation.
    # Example: 3 kWp @ €4.200 capex → threshold €420; nord case (€101) → marginale.
    if fin_grade >= 1 and npv > 0 and capex_eur > 0 and npv < capex_eur * 0.10:
        etichetta = "marginale"

    sovradimensionato = prod_ratio > _AVVISO_SOVRADIMENSIONAMENTO_RATIO

    if etichetta == "negativo":
        testo = (
            f"⚠ Con questa taglia il conto non torna: recupereresti la spesa in "
            f"~{pb:.1f} anni e dopo 25 anni ti resterebbe {npv:+,.0f} € in tasca. "
            "Valuta una taglia diversa o controlla il consumo reale con Livello 2."
        )
        if sovradimensionato:
            testo += (
                f" L'impianto produce circa {prod_ratio:.1f} volte il tuo consumo: "
                "gran parte dell'energia andrebbe venduta alla rete."
            )
    elif etichetta == "marginale":
        parti = [
            f"⚠ Investimento fragile: recupereresti la spesa in ~{pb:.1f} anni "
            f"e dopo 25 anni ti resterebbero +€{_fmt_it(npv)} in tasca — "
            f"meno del 10% dei {_fmt_it(capex_eur)} € spesi.",
            "Basta una variazione del prezzo di vendita alla rete (oggi 0,08 €/kWh), "
            "dell'aliquota IRPEF o dei prezzi dell'elettricità per azzerare quel margine.",
            "Prima di decidere ti consigliamo l'analisi di Livello 2 con i dati reali della tua casa.",
        ]
        if bassa_as:
            parti.append(
                f"Con il {autosufficienza_pct:.0f}% di copertura stimata, il vantaggio "
                "dipende soprattutto dalla detrazione fiscale e dalla vendita alla rete, "
                "non da un effettivo risparmio in bolletta."
            )
        testo = " ".join(parti)
    else:
        grade_label = {
            "ottimo": "Ottimo investimento",
            "buono": "Buon investimento",
            "accettabile": "Investimento accettabile",
        }[etichetta]
        parti = [
            f"{grade_label}: recuperi la spesa in ~{pb:.1f} anni "
            f"e dopo 25 anni ti restano +€{_fmt_it(npv)} in tasca.",
            (
                f"Al primo anno: risparmi €{beneficio.risparmio_bolletta_eur:.0f} in bolletta"
                f" + €{beneficio.ricavi_export_eur:.0f} venduti alla rete"
                f" + €{beneficio.quota_detrazione_eur:.0f} di detrazione"
                f" = €{beneficio.totale_anno1_eur:.0f} totali."
            ),
        ]
        if bassa_as:
            parti.append(
                f"L'impianto copre circa il {autosufficienza_pct:.0f}% del tuo fabbisogno annuo. "
                "Gran parte del beneficio viene dalla detrazione fiscale e dalla vendita alla rete, "
                "non dal risparmio diretto in bolletta."
            )
        if sovradimensionato:
            parti.append(
                f"⚠ L'impianto produce circa {prod_ratio:.1f} volte il tuo consumo annuo: "
                "gran parte dell'energia va venduta alla rete (0,08 €/kWh). "
                "La convenienza è fragile se quel prezzo cambia."
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

    # ── 4. Roof area and auto_scenarios (coverage-based, unchanged) ───────────
    roof_area = req.floor_area_m2 * 0.5
    kwp_max = kwp_from_area(roof_area, "roof_integrated", 100.0)
    taglie = auto_scenarios(centrale, kwp_max)

    auto_dimensionato = req.potenza_kwp is None
    azimuth = _ORIENTAMENTO_AZIMUTH[req.orientamento]

    # ── 5. Demand profile shape ───────────────────────────────────────────────
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

    # ── 6. Battery taglia (dynamic, scales with central consumption) ───────────
    battery_kwh = _battery_size_kwh(centrale)

    # ── 7. Battery-compare helper (captures req params + battery_kwh) ─────────
    def _run_battery(
        pv_h: list[float],
        demand_Wh: list[float],
        kwp_val: float,
        capex_val: int,
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

    # ── 7b. Lightweight senza-batteria helper (single simulate_lifetime call) ──
    # Used only for alternative scenarios (not the primary taglie analysis).
    # Runs one simulate_lifetime instead of two, halving the cost vs _run_battery.
    def _quick_sb(
        pv_h: list[float],
        demand_Wh: list[float],
        kwp_val: float,
        capex_val: int,
    ) -> ScenarioResult:
        pv_cost_per_kwp = capex_val / kwp_val
        detr = calc_irpef_deduction(
            float(capex_val),
            abitazione_principale=req.abitazione_principale,
            irpef_annua_stimata=req.irpef_annua_stimata,
        )
        detr_pct = detr.detrazione_totale_eur / capex_val * 100.0 if capex_val > 0 else 0.0
        tariff = GridTariff(
            import_price_eur_kWh=DEFAULT_IMPORT_PRICE_EUR_KWH,
            export_price_eur_kWh=DEFAULT_EXPORT_PRICE_EUR_KWH,
        )
        fin = FinancialParameters(
            discount_rate=DEFAULT_DISCOUNT_RATE,
            energy_price_escalation=DEFAULT_ESC_RATE,
            analysis_years=DEFAULT_ANALYSIS_YEARS,
        )
        investment = InvestmentCost(
            pv_cost_eur_kWp=pv_cost_per_kwp,
            peak_power_kWp=kwp_val,
            battery_cost_eur_kWh=0.0,
            battery_capacity_kWh=0.0,
        )
        opex = OperatingCost(
            om_pct_of_capex_per_yr=DEFAULT_OM_PCT,
            battery_replacement_years=[],
            battery_replacement_cost_eur_kWh=BATTERY_REPLACEMENT_COST_EUR_KWH,
        )
        incentives = ItalianIncentives(
            detrazione_fiscale_pct=detr_pct,
            detrazione_spread_years=10,
            ammortamento_accelerato=False,
            transizione_5_0_pct=0.0,
        )
        inp = EnergyBalanceInput(
            pv_production_hourly_Wh=pv_h,
            demand_hourly_Wh=demand_Wh,
            battery=BatterySystem(capacity_kWh=0.0),
            tariff=tariff,
            peak_power_kWp=kwp_val,
        )
        yearly = simulate_lifetime(inp, lifetime_years=DEFAULT_ANALYSIS_YEARS)
        econ = calc_economics(yearly, investment, opex, fin, incentives)
        yr1 = yearly[0]
        return ScenarioResult(
            label="senza_batteria",
            capex_eur=round(float(capex_val), 0),
            battery_kwh=0.0,
            battery_replacement_years=[],
            autoconsumo_pct=round(yr1.self_consumption_rate * 100, 1),
            autosufficienza_pct=round(yr1.self_sufficiency_rate * 100, 1),
            payback_anni=econ.payback_simple_yr,
            payback_scontato_anni=econ.payback_discounted_yr,
            npv_eur=econ.npv_eur,
            irr_pct=econ.irr_pct,
            detrazione_totale_eur=detr.detrazione_totale_eur,
            detrazione_effettiva_eur=detr.detrazione_effettiva_eur,
            risparmio_annuo_anno1_eur=econ.year1_saving_eur + econ.year1_export_revenue_eur,
            produzione_anno1_kwh=econ.year1_production_kWh,
            economics=econ,
        )

    # ── 8. Compute kwp_vals for the comparison set ────────────────────────────
    # Always derived from auto_scenarios output (not from user's potenza_kwp).
    kwp_min = taglie["minimal"]
    kwp_rec_auto = taglie["recommended"]   # coverage-based, for reference only
    kwp_max_t = taglie["maximum"]

    kwp_vals_set = sorted(set([kwp_min, kwp_rec_auto, kwp_max_t]))
    if len(kwp_vals_set) < 3:
        mid_raw = (min(kwp_vals_set) + max(kwp_vals_set)) / 2.0
        mid = round(mid_raw * 2) / 2
        mid = max(min(kwp_vals_set), min(max(kwp_vals_set), mid))
        kwp_vals_set = sorted(set(kwp_vals_set + [mid]))
    kwp_vals = kwp_vals_set

    # ── 9. Simulate PV + central battery for EVERY taglia candidate ──────────
    # All taglie first so cap filtering (section 9b) can pick kwp_consigliata
    # before we commit to the primary kWp in section 10.
    taglia_data: dict[float, _TagliaData] = {}
    for kv in kwp_vals:
        pv_sys_t = PVSystem(peak_power_kWp=kv, tilt_deg=30, azimuth_deg=azimuth)
        pv_res_t = simulate_pv(tmy, pv_sys_t, mounting="close_roof")
        capex_t = estimate_pv_cost_residential(kv)
        pv_h_t = [h.P_ac_W for h in pv_res_t.hourly]
        taglia_data[kv] = _TagliaData(
            kwp=kv,
            pv_hourly_Wh=pv_h_t,
            produzione_kwh=pv_res_t.production_annual_kWh,
            capex=capex_t,
            cmp=_run_battery(pv_h_t, _scale(centrale), kv, capex_t),
        )

    # ── 9b. Cap-filtered best-NPV kWp (must precede section 10) ─────────────
    # Only recommend a size that produces ≤ 150% of estimated annual consumption.
    # Beyond that the system primarily sells to the grid — dependent on fragile
    # assumptions (export price, fiscal deduction).

    def _prod_ratio(kv: float) -> float:
        return taglia_data[kv].produzione_kwh / centrale if centrale > 0 else float("inf")

    candidates_nel_cap = [k for k in kwp_vals if _prod_ratio(k) <= _MAX_PRODUZIONE_SU_CONSUMO]
    forza_minima = not candidates_nel_cap  # all candidates exceed cap

    if candidates_nel_cap:
        kwp_consigliata = max(
            candidates_nel_cap,
            key=lambda k: taglia_data[k].cmp.senza_batteria.npv_eur,
        )
        all_taglie_negative = all(
            taglia_data[k].cmp.senza_batteria.npv_eur <= 0 for k in candidates_nel_cap
        )
    else:
        # No candidate under cap — use smallest (least oversized)
        kwp_consigliata = min(kwp_vals)
        all_taglie_negative = taglia_data[kwp_consigliata].cmp.senza_batteria.npv_eur <= 0

    # ── 10. Select primary kWp ────────────────────────────────────────────────
    if auto_dimensionato:
        # Use the cap-filtered best-NPV candidate so kwp_ipotizzato and the
        # "consigliata" label in taglie_confronto are always the same kWp.
        kwp = kwp_consigliata
    else:
        kwp = float(req.potenza_kwp)
        # If user's kWp is not in the pre-computed set, add it now.
        if not any(abs(kwp - k) < 0.01 for k in kwp_vals):
            pv_sys_u = PVSystem(peak_power_kWp=kwp, tilt_deg=30, azimuth_deg=azimuth)
            pv_res_u = simulate_pv(tmy, pv_sys_u, mounting="close_roof")
            capex_u = estimate_pv_cost_residential(kwp)
            pv_h_u = [h.P_ac_W for h in pv_res_u.hourly]
            taglia_data[kwp] = _TagliaData(
                kwp=kwp,
                pv_hourly_Wh=pv_h_u,
                produzione_kwh=pv_res_u.production_annual_kWh,
                capex=capex_u,
                cmp=_run_battery(pv_h_u, _scale(centrale), kwp, capex_u),
            )

    # Primary kWp data (guaranteed to be in taglia_data)
    td_primary = taglia_data[next(k for k in taglia_data if abs(k - kwp) < 0.01)]
    pv_hourly_Wh = td_primary.pv_hourly_Wh
    produzione_annua_kwh = td_primary.produzione_kwh
    pv_capex = td_primary.capex
    pv_cost_per_kwp = pv_capex / kwp
    cmp_centrale = td_primary.cmp

    # ── 11. Range compute (low/high demand, primary kWp) ──────────────────────
    cmp_basso = _run_battery(pv_hourly_Wh, _scale(basso), kwp, pv_capex)
    cmp_alto  = _run_battery(pv_hourly_Wh, _scale(alto),  kwp, pv_capex)

    def _taglia_label(kwp_val: float) -> str:
        if abs(kwp_val - kwp_consigliata) < 0.01:
            return "consigliata"
        elif abs(kwp_val - min(kwp_vals)) < 0.01:
            return "minima"
        elif abs(kwp_val - max(kwp_vals)) < 0.01:
            return "massima"
        return "media"

    # ── 13. Ranges from low/central/high demand ───────────────────────────────
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

    # ── 14. Build taglie_confronto ────────────────────────────────────────────
    taglie_confronto: list[TagliaOut] = []
    for kv in kwp_vals:
        td = taglia_data[kv]
        sb_t = td.cmp.senza_batteria
        beneficio_t = _compute_beneficio_breakdown(sb_t)
        verdetto_t = _compute_verdetto(
            min(sb_t.payback_anni, 99.0), sb_t.npv_eur,
            sb_t.autosufficienza_pct, beneficio_t,
            prod_ratio=_prod_ratio(kv),
            capex_eur=float(td.capex),
        )
        taglie_confronto.append(TagliaOut(
            label=_taglia_label(kv),
            kwp=round(kv, 1),
            capex_eur=float(td.capex),
            produzione_annua_kwh=round(td.produzione_kwh, 0),
            autoconsumo_pct=sb_t.autoconsumo_pct,
            autosufficienza_pct=sb_t.autosufficienza_pct,
            risparmio_annuo_anno1_eur=sb_t.risparmio_annuo_anno1_eur,
            payback_anni=min(sb_t.payback_anni, 99.0),
            npv_eur=sb_t.npv_eur,
            beneficio_breakdown=beneficio_t,
            verdetto=verdetto_t,
            senza_batteria=_to_scenario_out(sb_t),
            con_batteria=_to_scenario_out(td.cmp.con_batteria),
            tradeoff=TradeoffOut(
                delta_autoconsumo_pp=td.cmp.tradeoff.delta_autoconsumo_pp,
                delta_autosufficienza_pp=td.cmp.tradeoff.delta_autosufficienza_pp,
                delta_payback_anni=td.cmp.tradeoff.delta_payback_anni,
                delta_npv_eur=td.cmp.tradeoff.delta_npv_eur,
                delta_capex_eur=td.cmp.tradeoff.delta_capex_eur,
                batteria_conveniente=td.cmp.tradeoff.batteria_conveniente,
                nota=td.cmp.tradeoff.nota,
            ),
        ))

    # ── 15. Primary verdetto (primary kWp, central demand) ────────────────────
    sb_central = cmp_centrale.senza_batteria
    beneficio_centrale = _compute_beneficio_breakdown(sb_central)
    prod_ratio_primary = td_primary.produzione_kwh / centrale if centrale > 0 else 0.0
    verdetto_pv = _compute_verdetto(
        min(sb_central.payback_anni, 99.0), sb_central.npv_eur,
        sb_central.autosufficienza_pct, beneficio_centrale,
        prod_ratio=prod_ratio_primary,
        capex_eur=float(pv_capex),
    )

    # If auto-sized AND every cap-eligible candidate has NPV ≤ 0: explicit message
    if auto_dimensionato and all_taglie_negative:
        limite_kwh = centrale * _MAX_PRODUZIONE_SU_CONSUMO
        if not forza_minima and candidates_nel_cap:
            best_npv_nel_cap = max(
                taglia_data[k].cmp.senza_batteria.npv_eur for k in candidates_nel_cap
            )
            testo_neg = (
                f"⚠ Nelle condizioni attuali, nessuna delle taglie proporzionate alla tua casa "
                f"(produzione fino a {_fmt_it(limite_kwh)} kWh/anno — il {_MAX_PRODUZIONE_SU_CONSUMO * 100:.0f}% "
                f"del tuo consumo) si ripaga in modo soddisfacente: il risultato migliore è "
                f"{best_npv_nel_cap:+,.0f} € in tasca dopo 25 anni. "
                "Gli impianti più grandi sembrano più convenienti sulla carta, "
                "ma quasi tutto l'extra andrebbe venduto alla rete a 0,08 €/kWh — "
                "un'ipotesi che può cambiare nel tempo. "
                "Verifica il tuo consumo reale o chiedi l'analisi di Livello 2."
            )
        else:
            # forza_minima: even min candidate exceeds cap
            min_prod = _fmt_it(taglia_data[min(kwp_vals)].produzione_kwh)
            testo_neg = (
                f"⚠ Anche il più piccolo impianto disponibile supera il "
                f"{_MAX_PRODUZIONE_SU_CONSUMO * 100:.0f}% del tuo consumo stimato "
                f"({min_prod} kWh prodotti contro i {_fmt_it(centrale)} kWh che consumi): "
                "qualsiasi impianto produrrebbe principalmente per la rete. "
                "Verifica il consumo reale o chiedi l'analisi di Livello 2."
            )
        verdetto_pv = VerdettoPV(
            payback_anni=verdetto_pv.payback_anni,
            npv_eur=verdetto_pv.npv_eur,
            etichetta="negativo",
            autosufficienza_pct=verdetto_pv.autosufficienza_pct,
            testo=testo_neg,
        )

    # ── 16. CO₂ evitata (central, self-consumed only) ─────────────────────────
    self_consumed_kwh = produzione_annua_kwh * sb_central.autoconsumo_pct / 100.0
    co2_evitata = round(self_consumed_kwh * _CO2_KG_PER_KWH, 0)

    # ── 17. Assumptions block ─────────────────────────────────────────────────
    zona_label = f"Zona climatica {comune.zona_climatica} — fonte DPR 412/93 (tabella ENEA)"
    if comune.zona_fonte == "nearest_neighbor":
        zona_label += " — comune nato da fusione, zona ereditata dal comune limitrofo"
    profilo_label = (
        "adulti al lavoro / figli a scuola (picco serale)"
        if req.presenza_diurna == "per_lo_piu_vuota"
        else "occupazione diurna alta (pensionati / smart-working)"
    )
    consumo_fonte_label = (
        f"Consumi dalla bolletta: {_fmt_it(centrale)} kWh/anno (±15%)"
        if consumo_da_bolletta
        else (
            f"Consumo annuo stimato: {_fmt_it(centrale)} kWh "
            f"(da {_fmt_it(basso)} a {_fmt_it(alto)} kWh, "
            f"margine {'±45%' if req.riscaldamento_elettrico else '±30%'})"
        )
    )
    if auto_dimensionato:
        if forza_minima:
            sizing_note = (
                "taglia minima disponibile — tutti gli impianti superano il "
                f"{_MAX_PRODUZIONE_SU_CONSUMO * 100:.0f}% del consumo stimato"
            )
        elif all_taglie_negative:
            sizing_note = (
                f"taglia con il miglior risultato tra quelle proporzionate alla casa "
                f"(produzione ≤{_MAX_PRODUZIONE_SU_CONSUMO * 100:.0f}% del consumo) — nessuna conveniente"
            )
        else:
            sizing_note = (
                f"taglia con il miglior risultato tra quelle proporzionate alla casa "
                f"(produzione ≤{_MAX_PRODUZIONE_SU_CONSUMO * 100:.0f}% del consumo)"
            )
    else:
        sizing_note = "potenza indicata dall'utente"

    assunzioni = [
        consumo_fonte_label,
        f"Profilo d'uso: {profilo_label}",
        f"Superficie abitativa: {_fmt_it(req.floor_area_m2)} m² → "
        f"area di tetto stimata: {_fmt_it(roof_area)} m² (circa la metà, falda inclinata)",
        zona_label,
        f"Impianto: {kwp:.1f} kWp, inclinazione 30° (falda tipica), {sizing_note}",
        f"Orientamento tetto: {_ORIENTAMENTO_DESC[req.orientamento]}",
        f"Costo indicativo dell'impianto: €{_fmt_it(pv_capex)} totali "
        f"(€{pv_cost_per_kwp:,.0f}/kWp in media; include €1.500 di costi fissi: "
        f"ponteggio, inverter, CILA)",
        f"Batteria nell'alternativa: {battery_kwh:.0f} kWh "
        f"(costo €{_fmt_it(battery_kwh * 550)} — €550/kWh installato, fonte ENEA RAEE 2023)",
        (
            "Detrazione fiscale (IRPEF): "
            + ("50%" if req.abitazione_principale else "36%")
            + " "
            + ("per l'abitazione principale" if req.abitazione_principale else "(altro immobile)")
            + ", recuperata in 10 anni, tetto €96.000 (art. 16-bis TUIR, L. 207/2024)"
        ),
        "Prezzi usati: 0,25 €/kWh per l'energia acquistata, 0,08 €/kWh per quella venduta alla rete; "
        "orizzonte di analisi 25 anni, degrado impianto 0,5 %/anno, "
        "rivalutazione prezzi elettricità 2,5 %/anno",
        "⚠ La detrazione al 50% vale per spese sostenute nel 2026 (L. 207/2024). "
        "Dal 2027 scende al 36%, salvo nuove proroghe.",
        "⚠ Il prezzo di vendita alla rete (0,08 €/kWh, GSE scambio sul posto 2026) "
        "può variare se le condizioni cambiano.",
        "Stima automatica di Livello 1 — i valori reali dipendono dalla tua casa "
        "e richiedono l'analisi di Livello 2",
    ]
    assunzioni.extend(avvertenze_consumo)
    if req.orientamento == "non_so":
        assunzioni.append(
            "⚠ Orientamento non dichiarato: calcoliamo con esposizione a sud-ovest (ipotesi prudente). "
            "Un tetto esposto a est o a ovest produce circa il 15-20% in meno; "
            "uno esposto a nord circa il 40-45% in meno rispetto a un tetto a sud."
        )

    # ── 18. Alternative scenarios ("cosa cambierebbe") ─────────────────────────
    # LIGHT: only central case, no battery, no taglie comparison.
    # Server-side to avoid frontend doing multiple rate-limited calls.
    scenari_alternativi: list[ScenarioAlternativoOut] = []

    # ── 18a. Scenario "elettrificazione" ──────────────────────────────────────
    # Triggered only when the house currently has gas heating (not electric).
    if not req.riscaldamento_elettrico:
        # ── Compute site-specific SCOP from TMY + terminal type ───────────────
        t_ext_hourly = [h.T2m for h in tmy.hourly]
        terminali = req.terminali   # type: ignore[attr-defined]
        t_supply_c = _terminal_supply_temp(terminali)
        scop_heat = _scop_from_tmy(t_ext_hourly, t_supply_c)

        # ACS COP: more conservative for high-temp terminals (termosifoni / non_so)
        cop_acs = (
            _COP_ACS_CONSERVATIVO
            if terminali in ("termosifoni", "non_so") or terminali is None
            else _COP_ACS_STANDARD
        )

        # ── Separate gas into cottura / ACS / riscaldamento ──────────────────
        if req.gas_smc_annui is not None:
            if req.gas_smc_estivi_mese is not None:
                # Summer base = cooking + ACS (no heating).
                base_smc = 12.0 * req.gas_smc_estivi_mese
                acs_smc = base_smc * _FRAC_ACS_SU_BASE_ESTIVA
                cottura_smc = base_smc * (1.0 - _FRAC_ACS_SU_BASE_ESTIVA)
                heating_smc = max(0.0, req.gas_smc_annui - base_smc)
                fonte_gas = (
                    f"gas_smc_annui={req.gas_smc_annui:.0f} Smc, "
                    f"gas_smc_estivi_mese={req.gas_smc_estivi_mese:.1f} Smc/mese → "
                    f"cottura {cottura_smc:.0f} Smc + ACS {acs_smc:.0f} Smc + "
                    f"riscaldamento {heating_smc:.0f} Smc"
                )
            else:
                # Default Italian split (ARERA 2023 / ISTAT):
                # cottura ≈ 75 + 15/occupante, ACS ≈ 45 Smc/occupante.
                cottura_smc = _GAS_COTTURA_BASE_SMC + max(0, req.n_occupants - 2) * _GAS_COTTURA_PER_OCCUPANT_SMC
                acs_smc = req.n_occupants * _GAS_ACS_SMC_PER_OCCUPANT
                # Cap cottura+ACS at 50% of total (prevents negative heating for low-use families)
                max_base = req.gas_smc_annui * 0.50
                if cottura_smc + acs_smc > max_base:
                    ratio = max_base / (cottura_smc + acs_smc)
                    cottura_smc *= ratio
                    acs_smc *= ratio
                heating_smc = max(0.0, req.gas_smc_annui - cottura_smc - acs_smc)
                fonte_gas = (
                    f"gas_smc_annui={req.gas_smc_annui:.0f} Smc → split ARERA/ISTAT default: "
                    f"cottura {cottura_smc:.0f} Smc + ACS {acs_smc:.0f} Smc + "
                    f"riscaldamento {heating_smc:.0f} Smc"
                )

            # Convert Smc → thermal kWh → electrical kWh per use
            cottura_kwh_e = cottura_smc * _GAS_PCI_KWH_PER_SMC * _ETA_BOILER / _COP_COTTURA
            acs_kwh_e = acs_smc * _GAS_PCI_KWH_PER_SMC * _ETA_BOILER / cop_acs
            heating_kwh_e = heating_smc * _GAS_PCI_KWH_PER_SMC * _ETA_BOILER / scop_heat

            # Only include ACS if it was gas (otherwise user already has electric ACS)
            if req.acqua_calda_elettrica:
                acs_kwh_e = 0.0

            fonte_heating = (
                f"gas {req.gas_smc_annui:.0f} Smc → riscaldamento {heating_smc:.0f} Smc × "
                f"η_caldaia {_ETA_BOILER} ÷ SCOP {scop_heat:.2f} "
                f"({terminali or 'n.d.'}); "
                f"ACS {acs_smc:.0f} Smc ÷ COP_ACS {cop_acs:.1f}; "
                f"cottura {cottura_smc:.0f} Smc ÷ COP_cottura {_COP_COTTURA:.0f}"
            )
            is_indicative = False
        else:
            # ── Benchmark fallback (no gas bill provided) ─────────────────────
            # Uses area-based HP benchmark + DHW table. Mark as indicative.
            cottura_kwh_e = 0.0   # cooking stays gas, not modeled
            heating_kwh_e = req.floor_area_m2 * _HP_KWH_PER_M2_CENTRAL
            acs_kwh_e = (
                0.0 if req.acqua_calda_elettrica
                else float(_DHW_KWH[min(req.n_occupants, _DHW_MAX)])
            )
            fonte_heating = (
                f"benchmark area: {req.floor_area_m2:.0f} m² × {_HP_KWH_PER_M2_CENTRAL} kWh_e/m² "
                f"(SCOP medio {_SCOP_BENCHMARK}, ENEA RAEE 2023)"
                + ("" if req.acqua_calda_elettrica
                   else f" + ACS {acs_kwh_e:.0f} kWh ({req.n_occupants} occupanti)")
            )
            is_indicative = True

        consumo_elettrif = round(centrale + heating_kwh_e + acs_kwh_e + cottura_kwh_e, 0)

        # ── Build hourly demand profile with proper thermal distribution ──────
        # Heating: winter-concentrated via TMY temperatures (PARTE 4).
        # ACS + cooking: uniform (minimal seasonal variation for simplicity).
        heating_profile_Wh = _heating_elec_profile_wh(t_ext_hourly, heating_kwh_e)
        acs_per_hour_Wh = acs_kwh_e * 1000.0 / 8760.0
        cottura_per_hour_Wh = cottura_kwh_e * 1000.0 / 8760.0
        base_profile_Wh = _scale(centrale)   # base electric: residential profile unchanged
        demand_elettrif = [
            base_profile_Wh[h] + heating_profile_Wh[h] + acs_per_hour_Wh + cottura_per_hour_Wh
            for h in range(8760)
        ]

        # Cap-filtered NPV-best kWp for the new (higher) consumption.
        # We reuse the already-simulated kwp_vals (no new PV simulation needed).
        def _el_ratio(kv: float) -> float:
            return taglia_data[kv].produzione_kwh / consumo_elettrif if consumo_elettrif > 0 else float("inf")

        cands_el = [k for k in kwp_vals if _el_ratio(k) <= _MAX_PRODUZIONE_SU_CONSUMO]
        if not cands_el:
            cands_el = [min(kwp_vals)]   # force smallest if all exceed cap

        # One simulate_lifetime per candidate (already-cached PV hourly data).
        el_sb: dict[float, ScenarioResult] = {
            kv: _quick_sb(taglia_data[kv].pv_hourly_Wh, demand_elettrif, kv, taglia_data[kv].capex)
            for kv in cands_el
        }
        kwp_el = max(cands_el, key=lambda k: el_sb[k].npv_eur)
        sr_el = el_sb[kwp_el]

        ben_el = _compute_beneficio_breakdown(sr_el)
        verdetto_el = _compute_verdetto(
            min(sr_el.payback_anni, 99.0), sr_el.npv_eur,
            sr_el.autosufficienza_pct, ben_el,
            prod_ratio=taglia_data[kwp_el].produzione_kwh / consumo_elettrif,
            capex_eur=float(taglia_data[kwp_el].capex),
        )

        # ── SCOP label per terminale (human-readable) ─────────────────────────
        terminali_label = {
            "termosifoni": "termosifoni a radiatori (70–80°C — rendimento pompa basso)",
            "pavimento_radiante": "pavimento radiante (35–40°C — rendimento pompa ottimale)",
            "ventilconvettori": "ventilconvettori/fan coil (40–45°C — rendimento pompa buono)",
            "non_so": "terminali non dichiarati — calcolato come termosifoni (ipotesi prudente)",
        }.get(terminali or "", f"terminali non specificati — SCOP benchmark {_SCOP_BENCHMARK} (ENEA RAEE 2023)")

        # ── Indicative warning (PARTE 5) ──────────────────────────────────────
        indicativa_note = ""
        if is_indicative:
            indicativa_note = (
                " ⚠ Stima indicativa (±50%): non hai fornito i consumi di gas, "
                "quindi il fabbisogno elettrico della pompa di calore è stimato da un benchmark "
                "di area. Per una stima più precisa, aggiungi i metri cubi di gas annui."
            )

        # ── High-temp terminal warning (termosifoni / non_so) ─────────────────
        alta_temp_note = ""
        if terminali in ("termosifoni", "non_so"):
            alta_temp_note = (
                " ⚠ Attenzione: i termosifoni lavorano ad alta temperatura (70–80°C). "
                "Con questi terminali la pompa di calore è meno efficiente "
                "e il consumo elettrico stimato è più alto rispetto a un impianto con pavimento radiante. "
                "Se in questo scenario il fotovoltaico sembra più conveniente, "
                "è perché si dimensiona su un fabbisogno più alto — "
                "non perché i termosifoni siano un vantaggio per la pompa di calore."
            )

        scenari_alternativi.append(ScenarioAlternativoOut(
            tipo="elettrificazione",
            titolo="Cosa cambierebbe con pompa di calore",
            descrizione=(
                "PERIMETRO: questo scenario risponde a una domanda specifica — "
                "quanto cambierebbe la convenienza del fotovoltaico se il riscaldamento "
                "(e l'acqua calda, se a gas) passasse a una pompa di calore? "
                "Non dice se convenga installare la pompa di calore: "
                "costi dell'impianto, incentivi e confronto col gas rimangono fuori da questa analisi "
                "e richiedono il Livello 2. "
                f"Terminali: {terminali_label}. "
                f"Rendimento stimato (SCOP): {scop_heat:.2f} "
                f"(modello Carnot × η={_HP_ETA_CARNOT}, dati climatici locali). "
                f"Fabbisogno termico: {fonte_heating}."
                + alta_temp_note
                + indicativa_note
            ),
            metriche=MetricheScenarioAlternativo(
                consumo_elettrico_stimato_kwh=consumo_elettrif,
                kwp_consigliato=round(kwp_el, 1),
                autosufficienza_pct=sr_el.autosufficienza_pct,
                payback_anni=round(min(sr_el.payback_anni, 99.0), 1),
                npv_eur=round(sr_el.npv_eur, 0),
                etichetta_verdetto=verdetto_el.etichetta,
            ),
            delta_vs_base=DeltaVsBase(
                autosufficienza_pp=round(
                    sr_el.autosufficienza_pct - sb_central.autosufficienza_pct, 1
                ),
                payback_anni=round(
                    min(sr_el.payback_anni, 99.0) - min(sb_central.payback_anni, 99.0), 1
                ),
                npv_eur=round(sr_el.npv_eur - sb_central.npv_eur, 0),
            ),
        ))

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
        scenari_alternativi=scenari_alternativi,
    )
