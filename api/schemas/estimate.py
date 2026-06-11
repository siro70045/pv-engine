"""Request/response models for the public Level-1 estimate (POST /estimate).

The request mirrors the 5-field public form on the Meridiana site. All
heavy lifting (default-filling, low/central/high sweep) lives in
``school.estimate_defaults``; this module only defines the wire contract.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class EstimateRequest(BaseModel):
    comune: str = Field(..., min_length=2, description="Nome del comune (denominazione ISTAT).")
    provincia: Optional[str] = Field(
        default=None, min_length=2, max_length=2,
        description="Sigla provincia per disambiguare gli omonimi (es. 'RE').",
    )
    tipologia: Literal["elementare", "media", "liceo", "tecnico_professionale"]
    fascia_studenti: Literal["lt_100", "100_300", "300_600", "gt_600"]
    fascia_anno: Literal[
        "pre_1960", "1960_1980", "1980_2000", "post_2000", "non_lo_so"
    ] = "non_lo_so"
    potenza_kwp: Optional[float] = Field(
        default=None, ge=1, le=1000,
        description="Potenza ipotizzata [kWp]; se assente viene auto-dimensionata.",
    )


class ComuneRisolto(BaseModel):
    nome: str
    provincia: str
    regione: str
    zona_climatica: str


class CandidatoComune(BaseModel):
    nome: str
    provincia: str
    regione: str


class RangeLowHigh(BaseModel):
    low: float
    high: float


class EstimateResponse(BaseModel):
    comune_risolto: ComuneRisolto
    kwp_ipotizzato: float
    auto_dimensionato: bool
    # Teaser for the Level-2 deep dive: "confrontiamo tre taglie"
    taglie_disponibili: dict[Literal["minimal", "recommended", "maximum"], float]
    produzione_annua_kwh: RangeLowHigh
    risparmio_annuo_eur: RangeLowHigh
    # Export revenue (energia immessa × prezzo cessione) — separate from
    # risparmio: the engine's annual_saving_eur is import-side only
    ricavi_cessione_eur_anno: RangeLowHigh
    payback_anni: RangeLowHigh
    co2_evitata_kg_anno: RangeLowHigh
    capex_indicativo_eur: float
    # Human-readable assumptions feeding the frontend honesty block
    assunzioni: list[str]
