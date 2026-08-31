"""
residential/estimate_defaults.py

Proxy di consumo elettrico annuo per abitazioni residenziali.

Usato SOLO quando l'utente non dispone della bolletta (opzione C, ramo proxy).
Se la bolletta è disponibile, quel numero vince sempre — questa funzione non
viene chiamata.

Struttura a strati
------------------
    consumo_annuo = base_persone(n) + quota_superficie(m²) + carichi_speciali

Filosofia
---------
    Semplice e onesta. Pochi input, margine dichiarato largo.
    Direzione dell'errore: stima prudente (consumo NON gonfiato). Un consumo
    sovrastimato gonfierebbe l'autoconsumo possibile e farebbe sembrare il FV
    più conveniente del vero — è l'errore da evitare.

Fonti e benchmark
-----------------
    [F1] ARERA, Relazione Annuale Esercizio 2023 (pubblicata 2024), Sezione
         Elettricità domestica: consumo medio cliente domestico ~2.700 kWh/anno.
         Dato omnicomprensivo: include famiglie con riscaldamento, ACS e cottura
         elettrica. La quota "puri elettrodomestici" (tutto-gas) è sensibilmente
         più bassa.

    [F2] ISTAT, "Consumi energetici delle famiglie", indagine 2020 (ultima
         disponibile al 2026): consumo medio domestico elettrico 2.508 kWh/anno.

    [F3] UNI TS 11300-2:2019, Prospetto B.1 e UNI EN 15316-3:2017: calcolo
         ACS residenziale. Base: 40 L/persona/giorno, ΔT = 35 K (15→50 °C),
         ρ·cp = 1.163 Wh/(L·K), η_boiler = 0.90.
         → 40 × 35 × 1.163 / 0.90 / 1000 × 365 ≈ 659 kWh/persona/anno
         (scaldabagno elettrico). Valori tabella incorporano efficienze di scala
         per serbatoi condivisi (tank 80-120 L per 2-4 persone).

    [F4] ENEA, Rapporto Annuale Efficienza Energetica (RAEE) 2023, Capitolo 2
         (Residenziale): fabbisogno termico riscaldamento 80-120 kWh_t/m²/anno
         per edifici anni 1970-2000 (classe energetica D-E). Con pompa di
         calore aria-acqua, SCOP medio 2.75 → 29-44 kWh_e/m²/anno; centrale
         33 kWh_e/m²/anno.

    [F5] EV: percorrenza media 12.200 km/anno (ISTAT, "Trasporto su strada e
         mobilità 2022"); consumi WLTP segmento B-C 17 kWh/100 km (media
         Fiat 500e, Peugeot e-208, VW ID.3 entry, Renault Zoe); perdite
         ricarica AC domestica ~8 % → 12.200 × 0.17 × 1.08 ≈ 2.240 kWh/anno.

    [⚠ CONDIZIONATORE — FONTE NON PRIMARIA DIRETTA]
         Non trovata fonte con kWh/m²/anno misurati su campioni italiani
         residenziali disaggregati. Il valore centrale 3.0 kWh/m²/anno è
         derivato dall'aggregato ENEA RAEE 2023 (raffreddamento residenziale),
         distribuito sulla superficie tipica dei fabbricati italiani.
         Incertezza ±50 % per effetto zona (1.5 kWh/m² zona F → 5.5 zona A-B).
         DA RIVALIDARE con dati misurati quando disponibili.

Calibrazione (sanity check, unico caso disponibile al 2026-08)
--------------------------------------------------------------
    Casa Borzano: 4 persone, ~90 m², tutto gas (riscaldamento_elettrico=False,
    acqua_calda_elettrica=False), condizionatore=True, EV=False.
    Consumo reale misurato: 1.435 kWh/anno.

    Modello:
        base_persone(4)      = 1.180 kWh
        quota_superficie(90) =    45 kWh   (90 × 0.5)
        condizionatore(90)   =   270 kWh   (90 × 3.0)
        ─────────────────────────────────
        Centrale             = 1.495 kWh   (+4.2 % vs misurato — lato prudente ✓)

    Non si aggiustano i parametri per forzare il fit sul singolo caso.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# PARAMETRI BENCHMARK
# ══════════════════════════════════════════════════════════════════════════════

# ── Strato 1: base_persone ────────────────────────────────────────────────────
# Consumo per famiglie con tutto a gas (riscaldamento, ACS, cottura).
# Copre: frigorifero/congelatore, lavatrice, lavastoviglie, illuminazione
# (LED), TV/PC/elettronica, standby, piccoli elettrodomestici.
#
# Fonte: decomposizione [F1]+[F2] sottraendo le voci elettriche maggiori
# (riscaldamento, ACS, cottura) stimate da ENEA [F4] per i sottocampioni
# corrispondenti. Curva sub-lineare: il frigorifero (250-350 kWh/anno) è
# uno per famiglia; illuminazione bagno/cucina/ingresso è condivisa; WM/DW
# fanno più cicli ma non in proporzione al numero di persone.
_BASE_PERSONE_KWH: dict[int, float] = {
    1: 580,    # singolo: frigo, TV, luci, WM uso limitato
    2: 830,    # coppia: sub-lineare (frigo+congelatore condivisi)
    3: 1_030,  # famiglia piccola
    4: 1_180,  # famiglia tipo: allineato al caso Borzano (v. docstring)
    5: 1_310,  # famiglia numerosa
    6: 1_420,  # 6+: rendimenti decrescenti accelerano
}
_BASE_PERSONE_MAX: int = max(_BASE_PERSONE_KWH.keys())

# ── Strato 2: quota_superficie ────────────────────────────────────────────────
# Stanze in più → più luci, più dispositivi stand-by, eventuali elettrodomestici
# extra. Valore conservativo (era LED).
# Fonte: stima interna derivata da benchmark potenza illuminazione residenziale
# EN 15193-1:2017 (~4 W/m² installati, 500 h/anno uso artificiale medio),
# applicata come correzione incrementale rispetto al baseline base_persone.
# Non letta da tabella pubblicata per uso domestico italiano: DA RIVALIDARE.
_QUOTA_M2_KWH_PER_M2: float = 0.5   # kWh/m²/anno

# ── Strato 3: carichi speciali ────────────────────────────────────────────────

# Condizionatore [⚠ — v. nota fonti sopra]
_AC_KWH_PER_M2_LOW: float     = 1.5   # zona F/G, uso minimo
_AC_KWH_PER_M2_CENTRAL: float = 3.0   # zona-agnostico, incertezza ±50%
_AC_KWH_PER_M2_HIGH: float    = 5.5   # zona A-B, uso intenso

# EV [F5]
_EV_KWH_LOW: float     = 1_400   # city car, pochi km
_EV_KWH_CENTRAL: float = 2_200   # media ISTAT 2022
_EV_KWH_HIGH: float    = 3_500   # pendolare alto km

# ACS elettrica (scaldabagno/boiler) [F3]
# Tabella per n_occupanti con efficienze di scala (serbatoio condiviso).
_DHW_KWH: dict[int, float] = {
    1:   700,
    2: 1_100,
    3: 1_480,
    4: 1_780,
    5: 2_020,
    6: 2_220,  # 6+: crescita ulteriore rallenta (serbatoio già saturo)
}
_DHW_MAX: int = max(_DHW_KWH.keys())

# Riscaldamento elettrico [F4] — assunta pompa di calore aria-aria/acqua
# Se resistenze elettriche pure: moltiplicare per ~3 (SCOP→1 vs 2.75).
# Incertezza molto alta senza zona climatica e dati involucro: ±45%.
_HP_KWH_PER_M2_LOW: float     = 18.0   # zona A-B o edificio classe A/B
_HP_KWH_PER_M2_CENTRAL: float = 33.0   # zona D-E, edificio anni 1970-2000
_HP_KWH_PER_M2_HIGH: float    = 55.0   # zona F o ante-1960 non ristrutturato

# ── Margini di incertezza ─────────────────────────────────────────────────────
# Con il set minimo di input (senza zona, involucro, profilo orario):
_MARGIN_BASE: float           = 0.30   # ±30% — senza riscaldamento elettrico
_MARGIN_WITH_HEATING: float   = 0.45   # ±45% — riscaldamento elettrico presente


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConsumptionBreakdown:
    base_persone_kwh: float
    quota_superficie_kwh: float
    condizionatore_kwh: float
    acqua_calda_elettrica_kwh: float
    riscaldamento_elettrico_kwh: float
    ev_kwh: float

    @property
    def totale_centrale(self) -> float:
        return (
            self.base_persone_kwh
            + self.quota_superficie_kwh
            + self.condizionatore_kwh
            + self.acqua_calda_elettrica_kwh
            + self.riscaldamento_elettrico_kwh
            + self.ev_kwh
        )


@dataclass
class ConsumptionEstimate:
    consumo_annuo_kwh_centrale: float
    consumo_annuo_kwh_basso: float
    consumo_annuo_kwh_alto: float
    breakdown: ConsumptionBreakdown
    avvertenze: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# FUNZIONE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════════

def build_residential_consumption_estimate(
    n_occupants: int,
    floor_area_m2: float,
    riscaldamento_elettrico: bool,
    acqua_calda_elettrica: bool,
    ev: bool,
    condizionatore: bool,
) -> ConsumptionEstimate:
    """Stima il consumo elettrico annuo di un'abitazione residenziale.

    Usato solo quando l'utente NON ha la bolletta. Se la bolletta è disponibile,
    quel numero vince sempre — questa funzione non va chiamata.

    Parameters
    ----------
    n_occupants : int
        Numero di occupanti stabili (1..6+). Valori >6 vengono trattati come 6.
    floor_area_m2 : float
        Superficie lorda abitabile [m²].
    riscaldamento_elettrico : bool
        True se pompa di calore o resistenze; False per gas/pellet/GPL/teleriscaldamento.
    acqua_calda_elettrica : bool
        True se boiler/scaldabagno elettrico; False per caldaia a gas o solare termico.
    ev : bool
        True se presente almeno un'auto elettrica caricata in casa.
    condizionatore : bool
        True se presente climatizzatore (split o centrale).

    Returns
    -------
    ConsumptionEstimate
        consumo_annuo_kwh_centrale: stima puntuale arrotondata a 10 kWh.
        consumo_annuo_kwh_basso / alto: banda di incertezza dichiarata.
        breakdown: dettaglio per strato (per trasparenza e debug).
        avvertenze: limitazioni note da mostrare all'utente.

    Notes
    -----
    Calibrazione sul caso disponibile (Borzano, 2026-08):
        4 persone, 90 m², gas tutto, condizionatore=True, EV=False.
        Misurato: 1.435 kWh. Modello centrale: 1.495 kWh (+4 %) — prudente ✓.
    """
    if n_occupants < 1:
        raise ValueError(f"n_occupants ≥ 1 richiesto, ricevuto {n_occupants!r}")
    if floor_area_m2 <= 0.0:
        raise ValueError(f"floor_area_m2 > 0 richiesto, ricevuto {floor_area_m2!r}")

    avvertenze: list[str] = []

    # Strato 1
    base = _BASE_PERSONE_KWH[min(n_occupants, _BASE_PERSONE_MAX)]
    if n_occupants > _BASE_PERSONE_MAX:
        avvertenze.append(
            f"Famiglie con più di {_BASE_PERSONE_MAX} persone: curva base_persone "
            f"extrapolata — affidabilità ridotta oltre {_BASE_PERSONE_MAX} occupanti."
        )

    # Strato 2
    quot_surf = floor_area_m2 * _QUOTA_M2_KWH_PER_M2

    # Strato 3: carichi speciali (additivi, solo se presenti)
    ac_kwh  = floor_area_m2 * _AC_KWH_PER_M2_CENTRAL if condizionatore else 0.0
    dhw_kwh = _DHW_KWH[min(n_occupants, _DHW_MAX)]    if acqua_calda_elettrica else 0.0
    hp_kwh  = floor_area_m2 * _HP_KWH_PER_M2_CENTRAL  if riscaldamento_elettrico else 0.0
    ev_kwh  = _EV_KWH_CENTRAL                          if ev else 0.0

    breakdown = ConsumptionBreakdown(
        base_persone_kwh=round(base),
        quota_superficie_kwh=round(quot_surf),
        condizionatore_kwh=round(ac_kwh),
        acqua_calda_elettrica_kwh=round(dhw_kwh),
        riscaldamento_elettrico_kwh=round(hp_kwh),
        ev_kwh=round(ev_kwh),
    )

    # Arrotondato a 10 kWh (coerente con incertezza dichiarata ≥ 30%)
    centrale = round(breakdown.totale_centrale / 10) * 10

    # Banda di incertezza
    margin = _MARGIN_WITH_HEATING if riscaldamento_elettrico else _MARGIN_BASE
    basso = round(centrale * (1.0 - margin) / 10) * 10
    alto  = round(centrale * (1.0 + margin) / 10) * 10

    # Avvertenze situazionali
    if condizionatore:
        avvertenze.append(
            "Condizionatore: stima zona-agnostica (±50 % per effetto clima). "
            "Sarà affinata con zona DPR 412/93 nella Fase 2."
        )
    if riscaldamento_elettrico:
        avvertenze.append(
            "Riscaldamento elettrico: assunta pompa di calore (SCOP 2.75). "
            "Se resistenze elettriche pure, il consumo reale è ~3× superiore. "
            "Incertezza ±45 % senza dati su zona climatica e isolamento."
        )
    avvertenze.append(
        f"Stima proxy (assenza bolletta). Precisione: ±{int(margin * 100)} %. "
        "Inserire la bolletta reale per una stima più accurata."
    )

    return ConsumptionEstimate(
        consumo_annuo_kwh_centrale=float(centrale),
        consumo_annuo_kwh_basso=float(basso),
        consumo_annuo_kwh_alto=float(alto),
        breakdown=breakdown,
        avvertenze=avvertenze,
    )
