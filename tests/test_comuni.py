"""Tests for school/comuni.py and the comuni_italiani.csv dataset."""

from __future__ import annotations

import pytest

from school.comuni import Comune, load_comuni, lookup_comune, normalize_comune_name


# ── Dataset integrity ─────────────────────────────────────────────────────────

def test_row_count_in_expected_range():
    comuni = load_comuni()
    assert 7700 <= len(comuni) <= 8100, f"unexpected row count: {len(comuni)}"


def test_climate_zones_all_present_and_valid():
    for c in load_comuni():
        assert c.zona_climatica in {"A", "B", "C", "D", "E", "F"}, (
            f"{c.nome} ({c.sigla_provincia}): invalid zone {c.zona_climatica!r}"
        )


def test_coordinates_inside_italian_bounding_box():
    # Mainland + islands: Lampedusa ~35.5N, Vetta d'Italia ~47.1N,
    # Rocca Bernauda ~6.6E, Capo d'Otranto ~18.5E
    for c in load_comuni():
        assert 35.0 < c.lat < 47.5, f"{c.nome}: lat {c.lat} out of range"
        assert 6.0 < c.lon < 19.0, f"{c.nome}: lon {c.lon} out of range"


def test_codici_istat_unique():
    codes = [c.codice_istat for c in load_comuni()]
    assert len(codes) == len(set(codes))


# ── Lookup ────────────────────────────────────────────────────────────────────

def test_lookup_albinea():
    matches = lookup_comune("Albinea")
    assert len(matches) == 1
    albinea = matches[0]
    assert albinea.sigla_provincia == "RE"
    assert albinea.regione == "Emilia-Romagna"
    assert albinea.zona_climatica == "E"
    assert albinea.lat == pytest.approx(44.6, abs=0.1)
    assert albinea.lon == pytest.approx(10.6, abs=0.1)


def test_homonym_returns_multiple_matches():
    # Castro exists both in provincia di Bergamo and provincia di Lecce
    matches = lookup_comune("Castro")
    assert len(matches) >= 2
    provinces = {c.sigla_provincia for c in matches}
    assert {"BG", "LE"} <= provinces


def test_homonym_disambiguation_by_provincia():
    matches = lookup_comune("Castro", provincia="le")
    assert len(matches) == 1
    assert matches[0].sigla_provincia == "LE"
    assert matches[0].regione == "Puglia"


def test_lookup_normalization_case_insensitive():
    assert lookup_comune("ALBINEA") == lookup_comune("albinea") == lookup_comune("Albinea")


def test_lookup_normalization_accents_and_apostrophes():
    # Forlì with and without accent
    assert lookup_comune("Forli") == lookup_comune("Forlì")
    assert len(lookup_comune("Forlì")) == 1
    # Apostrophe variants
    assert lookup_comune("Sant'Agata Bolognese") == lookup_comune("Sant Agata Bolognese")


def test_lookup_no_match_returns_empty_list():
    assert lookup_comune("Comune Inesistente XYZ") == []


def test_normalize_comune_name():
    assert normalize_comune_name("Forlì") == "forli"
    assert normalize_comune_name("SANT'AGATA   Bolognese") == "sant agata bolognese"
    assert normalize_comune_name("Reggio nell'Emilia") == "reggio nell emilia"


def test_load_comuni_cached_same_object():
    assert load_comuni() is load_comuni()
