"""Request/response models for the public Level-1 residential estimate (POST /estimate/casa).

The request mirrors the residential form on the Meridiana site. Consumption
can come from the actual bill (bolletta_kwh, preferred) or from a proxy
(n_occupants + floor_area + boolean loads). Battery trade-off is always shown
for both scenarios — never only the favourable half.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from api.schemas.estimate import CandidatoComune, ComuneRisolto, RangeLowHigh


class EstimateCasaRequest(BaseModel):
    # ── Location ──────────────────────────────────────────────────────────────
    comune: str = Field(..., min_length=2, description="Nome del comune (denominazione ISTAT).")
    provincia: Optional[str] = Field(
        default=None, min_length=2, max_length=2,
        description="Sigla provincia per disambiguare gli omonimi (es. 'RE').",
    )

    # ── Consumption ───────────────────────────────────────────────────────────
    bolletta_kwh: Optional[float] = Field(
        default=None, ge=100, le=20_000,
        description=(
            "Consumo elettrico annuo dalla bolletta [kWh/anno]. "
            "Se fornito, vince sempre sul proxy. Preferito: più accurato."
        ),
    )
    n_occupants: int = Field(..., ge=1, le=10, description="Numero di occupanti stabili.")
    floor_area_m2: float = Field(..., gt=0, le=1_000, description="Superficie lorda abitabile [m²].")
    riscaldamento_elettrico: bool = Field(
        default=False,
        description="True se pompa di calore o resistenze elettriche, False se gas/pellet.",
    )
    acqua_calda_elettrica: bool = Field(
        default=False,
        description="True se boiler/scaldabagno elettrico, False se caldaia a gas o solare termico.",
    )
    ev: bool = Field(default=False, description="True se presente almeno un'auto elettrica caricata in casa.")
    condizionatore: bool = Field(default=False, description="True se presente climatizzatore (split o centrale).")

    # ── Presence profile ──────────────────────────────────────────────────────
    presenza_diurna: Literal["per_lo_piu_vuota", "spesso_occupata"] = Field(
        default="per_lo_piu_vuota",
        description=(
            "'per_lo_piu_vuota': adulti al lavoro, figli a scuola (profilo working_family). "
            "'spesso_occupata': pensionati, chi lavora da casa (profilo residential classico)."
        ),
    )

    # ── PV ────────────────────────────────────────────────────────────────────
    orientamento: Literal["sud", "sud_est_sud_ovest", "est_o_ovest", "nord", "non_so"] = Field(
        default="non_so",
        description=(
            "Orientamento della falda dove installare i pannelli (una sola falda). "
            "'sud'=ottimale (azimuth 180°); "
            "'sud_est_sud_ovest'=leggermente deviato, ~20° dal sud (azimuth 200°); "
            "'est_o_ovest'=falda esposta a est OPPURE a ovest — si modella come ovest puro "
            "(azimuth 270°), produzione ~80% rispetto a sud pieno; "
            "'nord'=falda esposta a nord, sfavorevole (azimuth 0°); "
            "'non_so'=orientamento ignoto — si assume sud-ovest (azimuth 225°) come ipotesi prudente."
        ),
    )
    potenza_kwp: Optional[float] = Field(
        default=None, ge=1, le=100,
        description="Potenza ipotizzata [kWp]; se assente viene auto-dimensionata.",
    )

    # ── Economics ─────────────────────────────────────────────────────────────
    abitazione_principale: bool = Field(
        default=True,
        description="True = residenza anagrafica del proprietario (detrazione 50%), False = 36%.",
    )
    irpef_annua_stimata: Optional[float] = Field(
        default=None, ge=0,
        description=(
            "IRPEF lorda annua stimata [€]. Se assente, si assume capienza piena "
            "(ottimistico — la perdita fiscale non viene calcolata)."
        ),
    )

    # ── Gas consumption (optional, for electrification scenario) ─────────────
    gas_smc_annui: Optional[float] = Field(
        default=None, ge=0, le=10_000,
        description=(
            "Consumo annuo di gas naturale in Smc (Standard metri cubi), leggibile "
            "dalla bolletta del gas. Se fornito, viene usato nello scenario di "
            "elettrificazione per ricavare il fabbisogno termico dall'uso reale "
            "invece che dal benchmark 33 kWh_e/m². "
            "Tipicamente include riscaldamento e ACS (entrambi a gas)."
        ),
    )
    gas_smc_estivi_mese: Optional[float] = Field(
        default=None, ge=0, le=500,
        description=(
            "Consumo mensile medio di gas in estate (giugno–agosto) in Smc, quando "
            "il riscaldamento è spento. Rappresenta solo cottura e ACS. "
            "Se fornito insieme a gas_smc_annui, permette di separare la quota di "
            "riscaldamento dagli altri usi e ottenere uno scenario di elettrificazione "
            "più accurato. Tipicamente 15–35 Smc/mese per una famiglia di 4 persone."
        ),
    )

    # ── Terminal type (optional, for electrification scenario SCOP) ───────────
    terminali: Optional[Literal["termosifoni", "pavimento_radiante", "ventilconvettori", "non_so"]] = Field(
        default=None,
        description=(
            "Tipo di terminali di riscaldamento presenti nell'abitazione. "
            "Influenza il SCOP (rendimento stagionale) della pompa di calore nello "
            "scenario di elettrificazione. "
            "'termosifoni': radiatori ad alta temperatura (70–80°C), meno adatti a HP; "
            "'pavimento_radiante': riscaldamento a bassa temperatura (35–40°C), ottimale per HP; "
            "'ventilconvettori': fan coil (40–45°C), buono per HP; "
            "'non_so': ipotesi conservativa (termosifoni, peggiore caso). "
            "Se assente, si usa SCOP conservativo 2.5 (zona C-E media)."
        ),
    )


class MetricheScenarioAlternativo(BaseModel):
    """Metriche principali di uno scenario alternativo (solo caso centrale, senza batteria)."""
    consumo_elettrico_stimato_kwh: float
    kwp_consigliato: float
    autosufficienza_pct: float
    payback_anni: float
    npv_eur: float
    etichetta_verdetto: Literal["ottimo", "buono", "accettabile", "marginale", "negativo"]


class DeltaVsBase(BaseModel):
    """Delta vs scenario base (senza batteria, domanda centrale, kWp consigliato base)."""
    autosufficienza_pp: float   # positivo = migliora
    payback_anni: float         # negativo = migliora (payback più breve)
    npv_eur: float              # positivo = migliora


class ScenarioAlternativoOut(BaseModel):
    """Scenario 'cosa cambierebbe' — calcolato server-side nella stessa risposta."""
    tipo: Literal["elettrificazione"]
    titolo: str
    descrizione: str
    metriche: MetricheScenarioAlternativo
    delta_vs_base: DeltaVsBase


class ScenarioOut(BaseModel):
    """Risultato di un singolo scenario (senza o con batteria)."""
    label: str
    capex_eur: float
    battery_kwh: float
    autoconsumo_pct: float
    autosufficienza_pct: float
    payback_anni: float
    npv_eur: float
    irr_pct: float
    detrazione_totale_eur: float
    detrazione_effettiva_eur: float
    risparmio_annuo_anno1_eur: float
    produzione_anno1_kwh: float


class TradeoffOut(BaseModel):
    """Trade-off esplicito tra scenario con e senza batteria."""
    delta_autoconsumo_pp: float
    delta_autosufficienza_pp: float
    delta_payback_anni: float
    delta_npv_eur: float
    delta_capex_eur: float
    batteria_conveniente: bool
    nota: str


class BeneficioBreakdown(BaseModel):
    """Scomposizione del beneficio economico anno 1 (scenario senza batteria)."""
    risparmio_bolletta_eur: float   # energia autoconsumata × prezzo acquisto
    ricavi_export_eur: float        # energia ceduta × prezzo cessione GSE
    quota_detrazione_eur: float     # singola rata IRPEF annua (detrazione_totale / 10)
    totale_anno1_eur: float         # somma delle tre voci


class VerdettoPV(BaseModel):
    """Verdetto onesto sul solo FV (senza batteria, domanda centrale)."""
    payback_anni: float
    npv_eur: float
    etichetta: Literal["ottimo", "buono", "accettabile", "marginale", "negativo"]
    autosufficienza_pct: float      # % del fabbisogno coperto dall'autoproduzione
    testo: str


class TagliaOut(BaseModel):
    """Confronto per una singola taglia d'impianto (domanda centrale)."""
    label: str                      # "minima" | "media" | "consigliata" | "massima"
    kwp: float
    capex_eur: float
    produzione_annua_kwh: float
    autoconsumo_pct: float
    autosufficienza_pct: float
    risparmio_annuo_anno1_eur: float
    payback_anni: float
    npv_eur: float
    beneficio_breakdown: BeneficioBreakdown
    verdetto: VerdettoPV
    senza_batteria: ScenarioOut
    con_batteria: ScenarioOut
    tradeoff: TradeoffOut


class EstimateCasaResponse(BaseModel):
    # ── Comune ───────────────────────────────────────────────────────────────
    comune_risolto: ComuneRisolto

    # ── Consumption ──────────────────────────────────────────────────────────
    consumo_kwh_annuo: float           # valore centrale usato nelle simulazioni
    consumo_kwh_basso: float
    consumo_kwh_alto: float
    consumo_da_bolletta: bool          # True = bolletta reale, False = proxy

    # ── PV sizing ─────────────────────────────────────────────────────────────
    kwp_ipotizzato: float
    auto_dimensionato: bool
    taglie_disponibili: dict[Literal["minimal", "recommended", "maximum"], float]

    # ── Production (fixed kWp → single value, no range) ──────────────────────
    produzione_annua_kwh: float

    # ── Economics range (across consumption uncertainty) ─────────────────────
    risparmio_annuo_eur: RangeLowHigh
    payback_anni: RangeLowHigh
    co2_evitata_kg_anno: float         # centrale (self-consumed × CO₂ factor)

    # ── Battery comparison (central demand, primary kWp) ─────────────────────
    senza_batteria: ScenarioOut
    con_batteria: ScenarioOut
    tradeoff: TradeoffOut

    # ── Verdetto onesto (primary kWp, central demand) ─────────────────────────
    verdetto_pv: VerdettoPV

    # ── Ventaglio taglie confrontabili ────────────────────────────────────────
    taglie_confronto: list[TagliaOut]

    # ── CAPEX ─────────────────────────────────────────────────────────────────
    capex_indicativo_eur: float

    # ── Human-readable assumptions for the honesty block ─────────────────────
    assunzioni: list[str]

    # ── Alternative scenarios ("cosa cambierebbe") ────────────────────────────
    scenari_alternativi: list[ScenarioAlternativoOut] = []
