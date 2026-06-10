"""Tests for school/estimate_defaults.py (Fase A of /estimate)."""

from __future__ import annotations

import pytest

from api.schemas.loads import SchoolLoadsRequest
from school.estimate_defaults import (
    M2_PER_STUDENT,
    SCHOOL_TYPE_BY_FORM,
    STUDENTS_BY_BAND,
    YEAR_BY_BAND,
    build_estimate_profiles,
    resolve_tipologia,
)

_VALID_SCHOOL_TYPES = {"primary", "middle", "high_classic", "high_tech", "university_dept"}


# ── Mapping ───────────────────────────────────────────────────────────────────

def test_all_form_tipologie_map_to_valid_school_types():
    assert set(SCHOOL_TYPE_BY_FORM) == {
        "elementare", "media", "liceo", "tecnico_professionale"
    }
    for tipologia in SCHOOL_TYPE_BY_FORM:
        result = build_estimate_profiles(tipologia, "100-300")
        assert result["school_type"] in _VALID_SCHOOL_TYPES


def test_university_dept_not_exposed():
    assert "university_dept" not in SCHOOL_TYPE_BY_FORM.values()


def test_tipologia_aliases_and_normalization():
    assert resolve_tipologia("istituto tecnico-professionale".replace("-", "_")) \
        == "tecnico_professionale"
    assert resolve_tipologia("  LICEO ") == "liceo"
    assert resolve_tipologia("istituto_tecnico_professionale") == "tecnico_professionale"


def test_unknown_tipologia_raises():
    with pytest.raises(ValueError, match="tipologia_form"):
        build_estimate_profiles("conservatorio", "100-300")


def test_unknown_fascia_raises():
    with pytest.raises(ValueError, match="fascia_studenti"):
        build_estimate_profiles("media", "1-50")
    with pytest.raises(ValueError, match="fascia_anno"):
        build_estimate_profiles("media", "100-300", fascia_anno="1800-1900")


# ── Monotonicity ──────────────────────────────────────────────────────────────

def test_larger_student_band_gives_larger_area():
    bands_in_order = ["<100", "100-300", "300-600", "600+"]
    for tipologia in SCHOOL_TYPE_BY_FORM:
        areas = [
            build_estimate_profiles(tipologia, b)["profiles"]["central"]["floor_area_m2"]
            for b in bands_in_order
        ]
        assert areas == sorted(areas) and len(set(areas)) == len(areas), (
            f"{tipologia}: non-monotonic areas {areas}"
        )


def test_low_central_high_ordering_every_tipologia():
    for tipologia, table in M2_PER_STUDENT.items():
        assert table["low"] < table["central"] < table["high"], tipologia
        result = build_estimate_profiles(tipologia, "300-600")
        p = result["profiles"]
        assert (p["low"]["floor_area_m2"]
                < p["central"]["floor_area_m2"]
                < p["high"]["floor_area_m2"])


# ── Pydantic compatibility ────────────────────────────────────────────────────

def test_profiles_validate_as_school_loads_request():
    for tipologia in SCHOOL_TYPE_BY_FORM:
        for band in STUDENTS_BY_BAND:
            for fascia_anno in list(YEAR_BY_BAND) + [None]:
                result = build_estimate_profiles(tipologia, band, fascia_anno)
                for level, profile in result["profiles"].items():
                    req = SchoolLoadsRequest(**profile)
                    assert req.school_type == result["school_type"], (tipologia, band, level)


def test_low_high_vary_only_floor_area():
    result = build_estimate_profiles("liceo", "600+")
    central = result["profiles"]["central"]
    for level in ("low", "high"):
        diff = {
            k for k in central
            if result["profiles"][level][k] != central[k]
        }
        assert diff == {"floor_area_m2"}, f"{level}: unexpected diffs {diff}"


# ── Year bands ────────────────────────────────────────────────────────────────

def test_non_lo_so_and_none_give_engine_default_1981():
    assert build_estimate_profiles("media", "100-300", "non_lo_so")["construction_year"] == 1981
    assert build_estimate_profiles("media", "100-300", "non lo so")["construction_year"] == 1981
    assert build_estimate_profiles("media", "100-300", None)["construction_year"] == 1981


def test_year_bands_central_values():
    expected = {"pre-1960": 1950, "1960-1980": 1970, "1980-2000": 1990, "post-2000": 2010}
    for band, year in expected.items():
        assert build_estimate_profiles("media", "100-300", band)["construction_year"] == year


# ── Documented sample case ────────────────────────────────────────────────────

def test_sample_case_media_300_600():
    """Caso campione (documentato in CLAUDE.md): scuola media, 300-600 studenti.

    450 studenti × 9.0 m² = 4 050 m² centrale; 7.0 → 3 150 low; 11.0 → 4 950 high.
    Tetto = area / 2 piani.
    """
    result = build_estimate_profiles("media", "300-600")
    assert result["n_students"] == 450
    assert result["school_type"] == "middle"
    p = result["profiles"]
    assert p["central"]["floor_area_m2"] == pytest.approx(4050.0)
    assert p["low"]["floor_area_m2"] == pytest.approx(3150.0)
    assert p["high"]["floor_area_m2"] == pytest.approx(4950.0)
    assert result["roof_area_m2"] == {
        "low": 1575.0, "central": 2025.0, "high": 2475.0,
    }
    # Low-sensitivity defaults
    assert p["central"]["n_teachers"] == 45
    assert p["central"]["n_ata_staff"] == 15
    assert p["central"]["n_floors"] == 2
    assert p["central"]["has_gym"] is True
    assert p["central"]["has_canteen"] is False   # solo elementare
    assert p["central"]["full_time"] is False     # solo elementare


def test_elementare_canteen_and_full_time():
    p = build_estimate_profiles("elementare", "<100")["profiles"]["central"]
    assert p["has_canteen"] is True
    assert p["full_time"] is True
    assert p["n_students"] == 70


def test_potenza_kwp_passthrough():
    assert build_estimate_profiles("media", "100-300")["potenza_kwp"] is None
    assert build_estimate_profiles("media", "100-300", potenza_kwp=99.5)["potenza_kwp"] == 99.5
