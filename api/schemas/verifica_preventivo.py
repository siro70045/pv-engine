"""Schemas for POST /verifica/preventivo (quote check, Livello 2)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from api.schemas.estimate import ComuneRisolto, RangeLowHigh


class PreventivoIn(BaseModel):
    etichetta: str = Field(default="", max_length=40, description="Es. 'Preventivo A'. Nessun dato personale.")
    kwp: float = Field(..., gt=0, le=20, description="Potenza dell'impianto [kWp].")
    prezzo_totale_eur: float = Field(..., gt=0, le=200_000)
    prezzo_iva_inclusa: bool = Field(default=True)
    batteria_kwh: float = Field(default=0.0, ge=0, le=40)
    produzione_promessa_kwh: Optional[float] = Field(default=None, gt=0, le=100_000)


class VerificaPreventivoRequest(BaseModel):
    comune: str = Field(..., min_length=2)
    provincia: Optional[str] = Field(default=None, min_length=2, max_length=2)
    orientamento: Literal["sud", "sud_est_sud_ovest", "est", "ovest", "nord", "non_so"] = "non_so"
    inclinazione_deg: float = Field(default=30.0, ge=0, le=60)
    consumo_annuo_kwh: Optional[float] = Field(default=None, gt=0, le=50_000)
    ombra_ostacoli: Optional[Literal["no", "si", "non_so"]] = None
    ombra_lati: list[Literal["sud", "est", "ovest"]] = Field(default_factory=list)
    ombra_durata: Optional[Literal["qualche_ora", "buona_parte"]] = None
    preventivi: list[PreventivoIn] = Field(..., min_length=1, max_length=3)

    @model_validator(mode="after")
    def _ombra_completa(self) -> "VerificaPreventivoRequest":
        if self.ombra_ostacoli == "si" and (not self.ombra_lati or self.ombra_durata is None):
            raise ValueError("con ombra_ostacoli='si' servono ombra_lati e ombra_durata")
        return self


class EsitoPreventivo(BaseModel):
    etichetta: str
    kwp: float
    batteria_kwh: float
    prezzo_iva_inclusa_eur: float
    eur_per_kwp: float
    prezzo_atteso_eur: float
    prezzo_banda_eur: RangeLowHigh
    scarto_prezzo_pct: float                 # vs central expected price
    giudizio_prezzo: str                     # sotto_fascia | fascia_bassa | fascia_alta | sopra_fascia
    nota_prezzo: str
    produzione_stimata_kwh: float            # our estimate for this kWp on this roof
    produzione_promessa_kwh: Optional[float] = None
    scarto_produzione_pct: Optional[float] = None
    giudizio_produzione: Optional[str] = None  # prudente | coerente | ottimistica | gonfiata
    nota_produzione: Optional[str] = None
    eur_per_kwh_annuo: float                 # price / our annual production: lower = more energy per euro
    sovradimensionato: Optional[bool] = None
    nota_taglia: Optional[str] = None


class VerificaPreventivoResponse(BaseModel):
    comune_risolto: ComuneRisolto
    esiti: list[EsitoPreventivo]
    piu_conveniente: Optional[str] = None    # etichetta with the lowest €/kWh-year (only with 2-3 quotes)
    nota_confronto: Optional[str] = None
    avvertenze: list[str]
