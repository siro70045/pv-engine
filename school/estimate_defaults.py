"""
school/estimate_defaults.py

Default-filling logic for the Level-1 public estimate (/estimate, Fase A).

Maps the 5-field public form (comune, tipologia scuola, fascia studenti,
fascia anno di costruzione, potenza ipotizzata) onto complete
``SchoolLoadsRequest``-compatible profiles, generating a low/central/high
sweep on ``floor_area_m2`` via the m²-per-student table below.

Pure logic, no I/O: comune resolution (coordinates, climate zone, region)
lives in ``school.comuni`` and is wired in by the API layer (Fase B).

m² per student table
--------------------
Reference: DM 18/12/1975 (norme tecniche per l'edilizia scolastica),
Tabelle 3/A-3/B "superfici lorde per sezione, classe, alunno". Values are a
draft approved by Sirot (2026-06) and intentionally span a wider band than
the normative table, since the real building stock predates/exceeds the DM
minimums. To be validated against Anagrafe Nazionale Edilizia Scolastica.

    tipologia               central   low    high
    elementare              7.0       5.5    8.5
    media                   9.0       7.0    11.0
    liceo                   10.0      8.0    12.0
    tecnico_professionale   12.0      9.5    14.5
"""

from __future__ import annotations

from typing import Optional

# ── Form tipologia → engine school_type ──────────────────────────────────────
# university_dept exists in the engine but is NOT exposed in the public form.
SCHOOL_TYPE_BY_FORM: dict[str, str] = {
    "elementare": "primary",
    "media": "middle",
    "liceo": "high_classic",
    "tecnico_professionale": "high_tech",
}

_TIPOLOGIA_ALIASES: dict[str, str] = {
    "istituto_tecnico_professionale": "tecnico_professionale",
    "tecnico_prof": "tecnico_professionale",
}

# ── Student bands → central value ─────────────────────────────────────────────
STUDENTS_BY_BAND: dict[str, int] = {
    "<100": 70,
    "100-300": 200,
    "300-600": 450,
    "600+": 800,
}

# ── m² per student (low / central / high) — draft approved by Sirot ──────────
M2_PER_STUDENT: dict[str, dict[str, float]] = {
    "elementare":            {"low": 5.5, "central": 7.0,  "high": 8.5},
    "media":                 {"low": 7.0, "central": 9.0,  "high": 11.0},
    "liceo":                 {"low": 8.0, "central": 10.0, "high": 12.0},
    "tecnico_professionale": {"low": 9.5, "central": 12.0, "high": 14.5},
}

# ── Construction-year bands → central year ───────────────────────────────────
# "non_lo_so" falls back to 1981, the engine-wide default construction year.
YEAR_BY_BAND: dict[str, int] = {
    "pre-1960": 1950,
    "1960-1980": 1970,
    "1980-2000": 1990,
    "post-2000": 2010,
    "non_lo_so": 1981,
}

# ── Low-sensitivity defaults (decided by Sirot, 2026-06) ─────────────────────
N_FLOORS_DEFAULT = 2
TEACHERS_PER_STUDENT_DIVISOR = 10   # n_teachers  = n_students // 10
ATA_PER_STUDENT_DIVISOR = 30        # n_ata_staff = n_students // 30


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def resolve_tipologia(tipologia_form: str) -> str:
    """Canonical form-tipologia key, accepting common aliases."""
    key = _normalize_key(tipologia_form)
    key = _TIPOLOGIA_ALIASES.get(key, key)
    if key not in SCHOOL_TYPE_BY_FORM:
        raise ValueError(
            f"tipologia_form {tipologia_form!r} non valida; attese: "
            f"{sorted(SCHOOL_TYPE_BY_FORM)}"
        )
    return key


def build_estimate_profiles(
    tipologia_form: str,
    fascia_studenti: str,
    fascia_anno: Optional[str] = None,
    potenza_kwp: Optional[float] = None,
) -> dict:
    """Build the three (low/central/high) school profiles for the estimate.

    Parameters
    ----------
    tipologia_form : str
        One of ``elementare | media | liceo | tecnico_professionale``
        (aliases like "istituto tecnico-professionale" accepted).
    fascia_studenti : str
        One of ``<100 | 100-300 | 300-600 | 600+``.
    fascia_anno : str, optional
        One of ``pre-1960 | 1960-1980 | 1980-2000 | post-2000 | non_lo_so``.
        ``None`` behaves like "non_lo_so" (→ 1981, engine default).
    potenza_kwp : float, optional
        User-hypothesised PV size; passed through untouched. When ``None``
        Fase B will derive the three sizes via ``auto_scenarios`` using
        ``roof_area_m2``.

    Returns
    -------
    dict with keys:
        tipologia_form   canonical form key
        school_type      engine school_type
        n_students       central value of the band
        construction_year
        potenza_kwp      pass-through (may be None)
        m2_per_student   {"low": .., "central": .., "high": ..}
        profiles         {"low"|"central"|"high": dict} — each dict validates
                         as ``SchoolLoadsRequest(**profile)``; low/high differ
                         from central ONLY in ``floor_area_m2``
        roof_area_m2     {"low"|"central"|"high": float} — floor_area/n_floors,
                         input for Fase B sizing (auto_scenarios)
    """
    tipologia = resolve_tipologia(tipologia_form)
    school_type = SCHOOL_TYPE_BY_FORM[tipologia]

    band = fascia_studenti.strip()
    if band not in STUDENTS_BY_BAND:
        raise ValueError(
            f"fascia_studenti {fascia_studenti!r} non valida; attese: "
            f"{sorted(STUDENTS_BY_BAND)}"
        )
    n_students = STUDENTS_BY_BAND[band]

    year_key = "non_lo_so" if fascia_anno is None else _normalize_key(fascia_anno)
    if year_key not in YEAR_BY_BAND:
        raise ValueError(
            f"fascia_anno {fascia_anno!r} non valida; attese: "
            f"{sorted(YEAR_BY_BAND)}"
        )
    construction_year = YEAR_BY_BAND[year_key]

    is_elementare = tipologia == "elementare"
    base_profile = {
        "school_type": school_type,
        "n_students": n_students,
        "n_teachers": n_students // TEACHERS_PER_STUDENT_DIVISOR,
        "n_ata_staff": n_students // ATA_PER_STUDENT_DIVISOR,
        "construction_year": construction_year,
        "n_floors": N_FLOORS_DEFAULT,
        "has_gym": True,
        "has_canteen": is_elementare,
        "full_time": is_elementare,
    }

    profiles: dict[str, dict] = {}
    roof_area_m2: dict[str, float] = {}
    for level, m2 in M2_PER_STUDENT[tipologia].items():
        floor_area = round(n_students * m2, 1)
        profiles[level] = {**base_profile, "floor_area_m2": floor_area}
        roof_area_m2[level] = round(floor_area / N_FLOORS_DEFAULT, 1)

    return {
        "tipologia_form": tipologia,
        "school_type": school_type,
        "n_students": n_students,
        "construction_year": construction_year,
        "potenza_kwp": potenza_kwp,
        "m2_per_student": dict(M2_PER_STUDENT[tipologia]),
        "profiles": profiles,
        "roof_area_m2": roof_area_m2,
    }
