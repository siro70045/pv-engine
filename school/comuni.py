"""
school/comuni.py

Lookup of Italian comuni with centroid coordinates and official DPR 412/93
climate zone, backed by ``school/data/comuni_italiani.csv`` (see the README
in that directory for sources and licensing).

Public API
----------
load_comuni()                       → tuple[Comune, ...]  (cached)
lookup_comune(nome, provincia=None) → list[Comune]  (all exact matches)
normalize_comune_name(nome)         → str

Name matching is exact on the normalized form (lowercase, accents stripped,
apostrophes/hyphens treated as spaces). Homonyms exist (e.g. Castro BG/LE,
Livo CO/TN): ``lookup_comune`` returns ALL matches; pass ``provincia`` to
disambiguate. No fuzzy matching by design — the frontend autocomplete will
guide the user to an exact name.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CSV_PATH = Path(__file__).parent / "data" / "comuni_italiani.csv"


@dataclass(frozen=True)
class Comune:
    nome: str
    codice_istat: str
    sigla_provincia: str
    regione: str
    lat: float
    lon: float
    zona_climatica: str
    gradi_giorno: int | None
    zona_fonte: str


def normalize_comune_name(nome: str) -> str:
    """Normalize a comune name for exact matching.

    Lowercase, accents stripped, apostrophes and hyphens treated as word
    separators, whitespace collapsed — so "Forlì", "FORLI'" and "forli"
    all map to the same key.
    """
    s = unicodedata.normalize("NFKD", nome)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", " ").replace("’", " ").replace("-", " ")
    return " ".join(s.split())


@lru_cache(maxsize=1)
def load_comuni() -> tuple[Comune, ...]:
    """Load and cache the full comuni table (~7 900 rows, parsed once)."""
    rows: list[Comune] = []
    with open(_CSV_PATH, encoding="utf-8", newline="") as f:
        for rec in csv.DictReader(f):
            gg = rec["gradi_giorno"].strip()
            rows.append(Comune(
                nome=rec["nome"],
                codice_istat=rec["codice_istat"],
                sigla_provincia=rec["sigla_provincia"],
                regione=rec["regione"],
                lat=float(rec["lat"]),
                lon=float(rec["lon"]),
                zona_climatica=rec["zona_climatica"],
                gradi_giorno=int(gg) if gg else None,
                zona_fonte=rec["zona_fonte"],
            ))
    return tuple(rows)


@lru_cache(maxsize=1)
def _index_by_name() -> dict[str, tuple[Comune, ...]]:
    idx: dict[str, list[Comune]] = {}
    for c in load_comuni():
        idx.setdefault(normalize_comune_name(c.nome), []).append(c)
    return {k: tuple(v) for k, v in idx.items()}


def lookup_comune(nome: str, provincia: str | None = None) -> list[Comune]:
    """Return all comuni whose normalized name matches ``nome`` exactly.

    Parameters
    ----------
    nome : str
        Comune name in any casing/accent variant.
    provincia : str, optional
        Two-letter province code (e.g. "RE") to disambiguate homonyms.
        Case-insensitive.

    Returns
    -------
    list[Comune] — empty if no match; more than one element for homonyms
    when ``provincia`` is not given.
    """
    matches = list(_index_by_name().get(normalize_comune_name(nome), ()))
    if provincia is not None:
        prov = provincia.strip().upper()
        matches = [c for c in matches if c.sigla_provincia.upper() == prov]
    return matches
