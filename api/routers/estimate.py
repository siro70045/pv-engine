"""Public Level-1 estimate endpoint (POST /estimate).

Five-field form → low/central/high sweep on the building (m²/student), with
a FIXED PV size across the three runs: the plant is the decision, the
building is the uncertainty. Orchestrates existing pieces only:

    lookup_comune → build_estimate_profiles → get_cached_tmy →
    calc_school_loads ×3 → auto_scenarios → run_simulation_school ×3

Climate zone and region come from the official ISTAT/ENEA dataset
(``school/data/comuni_italiani.csv``), NOT from the latitude heuristics.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from api.utils.limiter import limiter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.schemas.estimate import (
    CandidatoComune,
    ComuneRisolto,
    EstimateRequest,
    EstimateResponse,
    RangeLowHigh,
)
from api.schemas.loads import SchoolLoadsRequest
from api.routers.loads import _school_inputs
from api.utils.cache import get_cached_tmy
from api.utils.cost import auto_scenarios, estimate_pv_cost_school
from buildings import kwp_from_area
from engine.energy_vectors import BuildingEnergyProfile
from engine.pvgis_client import PVGISError
from school.comuni import Comune, lookup_comune
from school.estimate_defaults import build_estimate_profiles
from school.loads_school import calc_school_loads, run_simulation_school

logger = logging.getLogger("pv_engine_api.estimate")

router = APIRouter(prefix="/estimate", tags=["estimate"])

# Italian grid CO₂ factor [kg/kWh] — single source of truth is the engine
# dataclass default (ENTSO-E 2024); read it instead of duplicating the value.
_CO2_KG_PER_KWH = next(
    f.default for f in dataclasses.fields(BuildingEnergyProfile)
    if f.name == "co2_electricity_kg_per_kwh"
)

# API wire keys → school.estimate_defaults keys (Fase A predates this schema)
_FASCIA_STUDENTI = {
    "lt_100": "<100", "100_300": "100-300",
    "300_600": "300-600", "gt_600": "600+",
}
_FASCIA_ANNO = {
    "pre_1960": "pre-1960", "1960_1980": "1960-1980",
    "1980_2000": "1980-2000", "post_2000": "post-2000",
    "non_lo_so": "non_lo_so",
}
_TIPOLOGIA_LABELS = {
    "elementare": "scuola elementare", "media": "scuola media",
    "liceo": "liceo", "tecnico_professionale": "istituto tecnico-professionale",
}

# ISTAT region denomination → school calendar key. Trentino-Alto Adige is the
# only region with two calendars, split by province (TN/BZ).
_CALENDAR_KEY_BY_REGIONE = {
    "Abruzzo": "abruzzo", "Basilicata": "basilicata", "Calabria": "calabria",
    "Campania": "campania", "Emilia-Romagna": "emilia_romagna",
    "Friuli-Venezia Giulia": "friuli_vg", "Lazio": "lazio",
    "Liguria": "liguria", "Lombardia": "lombardia", "Marche": "marche",
    "Molise": "molise", "Piemonte": "piemonte", "Puglia": "puglia",
    "Sardegna": "sardegna", "Sicilia": "sicilia", "Toscana": "toscana",
    "Umbria": "umbria", "Valle d'Aosta/Vallée d'Aoste": "valle_daosta",
    "Veneto": "veneto",
}


def _fmt_it(value: float) -> str:
    """Integer formatting with Italian thousands separator (4.050, not 4,050)."""
    return f"{value:,.0f}".replace(",", ".")


def _calendar_region_key(comune: Comune) -> str:
    if comune.regione.startswith("Trentino"):
        return "bolzano" if comune.sigla_provincia.upper() == "BZ" else "trento"
    return _CALENDAR_KEY_BY_REGIONE.get(comune.regione, "emilia_romagna")


@router.post("", response_model=EstimateResponse,
             summary="Public Level-1 preliminary estimate (5-field form)")
@limiter.limit("5/minute")
async def estimate(request: Request, req: EstimateRequest) -> EstimateResponse:
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
    comune = matches[0]
    region_key = _calendar_region_key(comune)

    # ── 2. Default profiles (low/central/high sweep on m²/student) ──────────
    estimate_data = build_estimate_profiles(
        tipologia_form=req.tipologia,
        fascia_studenti=_FASCIA_STUDENTI[req.fascia_studenti],
        fascia_anno=_FASCIA_ANNO[req.fascia_anno],
        potenza_kwp=req.potenza_kwp,
    )
    # Official climate zone + regional calendar from the dataset — override
    # the SchoolLoadsRequest defaults (zone E / emilia_romagna).
    profiles = {
        level: {**p, "climate_zone": comune.zona_climatica, "region": region_key}
        for level, p in estimate_data["profiles"].items()
    }

    # ── 3. One shared TMY fetch ───────────────────────────────────────────────
    try:
        tmy, _ = get_cached_tmy(comune.lat, comune.lon)
    except PVGISError as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Il servizio meteo europeo PVGIS non è raggiungibile al momento "
            f"({e}). Riprova tra qualche minuto.",
        )
    tmy_ghi = [rec.G_h for rec in tmy.hourly]

    # ── 4. Loads ×3 ───────────────────────────────────────────────────────────
    loads = {
        level: calc_school_loads(
            _school_inputs(SchoolLoadsRequest(**p)), tmy_hourly_ghi=tmy_ghi,
        )
        for level, p in profiles.items()
    }

    # ── 5. PV size: fixed across the sweep ───────────────────────────────────
    central_demand = loads["central"]["total_annual_kwh"]
    kwp_max = kwp_from_area(
        estimate_data["roof_area_m2"]["central"], "flat_roof_ballast", 100.0,
    )
    taglie = auto_scenarios(central_demand, kwp_max)
    if req.potenza_kwp is not None:
        kwp, auto_dimensionato = req.potenza_kwp, False
    else:
        kwp, auto_dimensionato = taglie["recommended"], True
    capex, structural = estimate_pv_cost_school(kwp)

    # ── 6. Simulations ×3 (fixed kWp) ─────────────────────────────────────────
    runs = {}
    for level, load in loads.items():
        sim = run_simulation_school(
            tmy=tmy,
            school_loads=load,
            floor_area_m2=profiles[level]["floor_area_m2"],
            kwp=kwp,
            capex_eur=float(capex),
            structural_eur=float(structural),
        )
        eb = sim.yearly_balance[0]
        runs[level] = {
            "prod": sim.solar_result.production_annual_kWh,
            "saving": eb.annual_saving_eur,
            "export_revenue": eb.export_revenue_eur,
            "payback": min(sim.economics.payback_simple_yr, 99.0),
            # Avoided CO₂ = self-consumed PV only (production − export):
            # conservative, consistent with the engine's import-based accounting
            "co2": (sim.solar_result.production_annual_kWh - eb.grid_export_kWh)
                   * _CO2_KG_PER_KWH,
        }

    # ── 7. Aggregate min/max across the three runs ────────────────────────────
    def _range(key: str, digits: int) -> RangeLowHigh:
        vals = [runs[level][key] for level in ("low", "central", "high")]
        return RangeLowHigh(low=round(min(vals), digits),
                            high=round(max(vals), digits))

    n_students = estimate_data["n_students"]
    m2 = estimate_data["m2_per_student"]
    areas = {lv: profiles[lv]["floor_area_m2"] for lv in ("low", "central", "high")}
    anno_label = (
        f"{estimate_data['construction_year']} (default, anno non specificato)"
        if req.fascia_anno == "non_lo_so"
        else str(estimate_data["construction_year"])
    )
    zona_label = f"Zona climatica {comune.zona_climatica} — fonte DPR 412/93 (tabella ENEA)"
    if comune.zona_fonte == "nearest_neighbor":
        zona_label += " — comune nato da fusione, zona ereditata dal comune limitrofo"

    assunzioni = [
        f"Superficie stimata: {_fmt_it(areas['central'])} m² da {n_students} studenti "
        f"× {m2['central']:.1f} m²/studente (rif. DM 18/12/1975); il range "
        f"{_fmt_it(areas['low'])}–{_fmt_it(areas['high'])} m² riflette l'incertezza "
        f"sull'edificio ({m2['low']:.1f}–{m2['high']:.1f} m²/studente)",
        f"Tipologia: {_TIPOLOGIA_LABELS[req.tipologia]}; palestra presente, "
        f"mensa e tempo pieno {'inclusi' if req.tipologia == 'elementare' else 'esclusi'}, "
        f"edificio su 2 piani",
        f"Anno di costruzione assunto: {anno_label}",
        zona_label,
        f"Calendario scolastico regionale: {region_key.replace('_', ' ').title()}",
        f"Riscaldamento esistente a gas: la stima copre i soli consumi elettrici "
        f"(pompa di calore e altri interventi sono analizzati nel Livello 2)",
        f"Impianto: {kwp:.1f} kWp su tetto piano (tilt 10°, esposizione sud), "
        + ("taglia consigliata calcolata sul fabbisogno stimato"
           if auto_dimensionato else "potenza indicata dall'utente"),
        f"CAPEX indicativo da listino scuole pubbliche: €{_fmt_it(capex)} "
        f"(inclusa verifica strutturale €{_fmt_it(structural)}), IVA inclusa",
        f"Prezzi energia: 0,22 €/kWh acquisto, 0,08 €/kWh cessione; "
        f"orizzonte 25 anni con degrado 0,5 %/anno",
        f"CO₂ evitata calcolata sul solo autoconsumo, fattore rete italiana "
        f"{_CO2_KG_PER_KWH} kg CO₂/kWh (ENTSO-E 2024)",
        "Stima preliminare automatica: i valori reali dipendono dall'edificio "
        "specifico e richiedono l'analisi di Livello 2",
    ]

    return EstimateResponse(
        comune_risolto=ComuneRisolto(
            nome=comune.nome,
            provincia=comune.sigla_provincia,
            regione=comune.regione,
            zona_climatica=comune.zona_climatica,
        ),
        kwp_ipotizzato=round(kwp, 1),
        auto_dimensionato=auto_dimensionato,
        taglie_disponibili={k: round(v, 1) for k, v in taglie.items()},
        produzione_annua_kwh=_range("prod", 0),
        risparmio_annuo_eur=_range("saving", 0),
        ricavi_cessione_eur_anno=_range("export_revenue", 0),
        payback_anni=_range("payback", 1),
        co2_evitata_kg_anno=_range("co2", 0),
        capex_indicativo_eur=float(capex),
        assunzioni=assunzioni,
    )
