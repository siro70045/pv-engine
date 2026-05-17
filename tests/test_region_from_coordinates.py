"""
Regression tests for region_from_coordinates and REGION_ALIASES.

Anomaly #12 (bounding-box overlap) and #5 (Bolzano alias) were closed
together on 2026-05-17. Pre-fix error rate on this test set was 22 %
(21 / 94 cities misclassified, including 3 regional capitals: L'Aquila,
Firenze, Perugia, plus the entire northern Tuscany cluster).
"""

from __future__ import annotations

import pytest

from school.calendar_it import (
    REGION_ALIASES,
    REGIONAL_CALENDARS,
    _AMBIGUOUS_REGIONS,
    _build_regional_vacations,
    region_from_coordinates,
)


# ── 1) The 20 regional capitals ───────────────────────────────────────────────

REGIONAL_CAPITALS = [
    ("L'Aquila",   42.349, 13.399, "abruzzo"),       # pre-fix: lazio
    ("Potenza",    40.642, 15.808, "basilicata"),
    ("Catanzaro",  38.910, 16.587, "calabria"),
    ("Napoli",     40.852, 14.268, "campania"),
    ("Bologna",    44.494, 11.342, "emilia_romagna"),
    ("Trieste",    45.649, 13.776, "friuli_vg"),
    ("Roma",       41.903, 12.496, "lazio"),
    ("Genova",     44.405,  8.946, "liguria"),
    ("Milano",     45.464,  9.190, "lombardia"),
    ("Ancona",     43.616, 13.518, "marche"),
    ("Campobasso", 41.561, 14.667, "molise"),
    ("Torino",     45.070,  7.687, "piemonte"),
    ("Bari",       41.117, 16.872, "puglia"),
    ("Cagliari",   39.224,  9.121, "sardegna"),
    ("Palermo",    38.115, 13.361, "sicilia"),
    ("Firenze",    43.770, 11.255, "toscana"),       # pre-fix: emilia_romagna
    ("Trento",     46.066, 11.122, "trento"),
    ("Bolzano",    46.498, 11.354, "bolzano"),
    ("Perugia",    43.111, 12.388, "umbria"),        # pre-fix: toscana
    ("Aosta",      45.737,  7.320, "valle_daosta"),
    ("Venezia",    45.440, 12.316, "veneto"),
]


@pytest.mark.parametrize("city,lat,lon,expected", REGIONAL_CAPITALS)
def test_regional_capital_classified_correctly(city, lat, lon, expected):
    got = region_from_coordinates(lat, lon)
    assert got == expected, f"{city} ({lat},{lon}): got {got!r}, want {expected!r}"


# ── 2) Border cities that were misclassified pre-fix ──────────────────────────

PREVIOUSLY_MISCLASSIFIED = [
    # Northern Tuscany cluster — all were classified as emilia_romagna
    ("La Spezia",       44.103,  9.824, "liguria"),
    ("Massa",           44.035, 10.141, "toscana"),
    ("Carrara",         44.080, 10.097, "toscana"),
    ("Pisa",            43.722, 10.401, "toscana"),
    ("Lucca",           43.843, 10.504, "toscana"),
    ("Pistoia",         43.933, 10.917, "toscana"),
    ("Prato",           43.881, 11.097, "toscana"),
    # Po valley west
    ("Piacenza",        45.052,  9.693, "emilia_romagna"),  # was lombardia
    ("Novara",          45.446,  8.621, "piemonte"),        # was lombardia
    ("Verbania",        45.921,  8.554, "piemonte"),        # was lombardia
    # Western Liguria
    ("Savona",          44.310,  8.477, "liguria"),         # was piemonte
    ("Ventimiglia",     43.792,  7.608, "liguria"),         # was lazio
    # Veneto / Friuli
    ("Verona",          45.438, 10.992, "veneto"),          # was lombardia
    ("Pordenone",       45.960, 12.659, "friuli_vg"),       # was veneto
    # Centre / South
    ("Foggia",          41.461, 15.544, "puglia"),          # was campania
    ("Teramo",          42.661, 13.704, "abruzzo"),         # was lazio
    ("Viterbo",         42.418, 12.107, "lazio"),           # was toscana
    ("Reggio_Calabria", 38.110, 15.647, "calabria"),        # was sicilia
]


@pytest.mark.parametrize("city,lat,lon,expected", PREVIOUSLY_MISCLASSIFIED)
def test_previously_misclassified_now_correct(city, lat, lon, expected):
    got = region_from_coordinates(lat, lon)
    assert got == expected, (
        f"{city} ({lat},{lon}): got {got!r}, want {expected!r} "
        f"(regression — this city was misclassified pre-fix #12)"
    )


# ── 3) Other capoluoghi di provincia (broad coverage) ─────────────────────────

OTHER_PROVINCIAL_CAPITALS = [
    ("Cremona",       45.133, 10.022, "lombardia"),
    ("Mantova",       45.156, 10.791, "lombardia"),
    ("Rovigo",        45.071, 11.790, "veneto"),
    ("Brescia",       45.541, 10.220, "lombardia"),
    ("Ferrara",       44.838, 11.620, "emilia_romagna"),
    ("Rimini",        44.060, 12.567, "emilia_romagna"),
    ("Pesaro",        43.910, 12.913, "marche"),
    ("Belluno",       46.139, 12.218, "veneto"),
    ("Udine",         46.062, 13.234, "friuli_vg"),
    ("Cuneo",         44.388,  7.547, "piemonte"),
    ("Asti",          44.901,  8.207, "piemonte"),
    ("Alessandria",   44.913,  8.616, "piemonte"),
    ("Imperia",       43.886,  8.029, "liguria"),
    ("Sondrio",       46.170,  9.870, "lombardia"),
    ("Como",          45.808,  9.085, "lombardia"),
    ("Lecco",         45.857,  9.397, "lombardia"),
    ("Pavia",         45.185,  9.156, "lombardia"),
    ("Lodi",          45.314,  9.503, "lombardia"),
    ("Vercelli",      45.321,  8.418, "piemonte"),
    ("Biella",        45.566,  8.054, "piemonte"),
    ("Reggio_Emilia", 44.697, 10.631, "emilia_romagna"),
    ("Modena",        44.647, 10.925, "emilia_romagna"),
    ("Parma",         44.802, 10.328, "emilia_romagna"),
    ("Bergamo",       45.698,  9.669, "lombardia"),
    ("Vicenza",       45.546, 11.547, "veneto"),
    ("Treviso",       45.667, 12.243, "veneto"),
    ("Padova",        45.406, 11.876, "veneto"),
    ("Taranto",       40.464, 17.247, "puglia"),
    ("Lecce",         40.352, 18.174, "puglia"),
    ("Brindisi",      40.638, 17.946, "puglia"),
    ("Messina",       38.193, 15.554, "sicilia"),
    ("Catania",       37.502, 15.087, "sicilia"),
    ("Siracusa",      37.075, 15.286, "sicilia"),
    ("Sassari",       40.726,  8.560, "sardegna"),
    ("Olbia",         40.923,  9.498, "sardegna"),
    ("Salerno",       40.681, 14.759, "campania"),
    ("Avellino",      40.915, 14.793, "campania"),
    ("Benevento",     41.130, 14.782, "campania"),
    ("Frosinone",     41.640, 13.343, "lazio"),
    ("Latina",        41.467, 12.904, "lazio"),
    ("Rieti",         42.404, 12.857, "lazio"),
    ("Pescara",       42.464, 14.215, "abruzzo"),
    ("Chieti",        42.348, 14.166, "abruzzo"),
    ("Isernia",       41.598, 14.234, "molise"),
    ("Matera",        40.666, 16.604, "basilicata"),
    ("Cosenza",       39.299, 16.253, "calabria"),
    ("Vibo_Valentia", 38.674, 16.099, "calabria"),
    ("Crotone",       39.080, 17.127, "calabria"),
    ("Caserta",       41.073, 14.331, "campania"),
    ("Terni",         42.564, 12.643, "umbria"),
    ("Livorno",       43.546, 10.310, "toscana"),
    ("Siena",         43.318, 11.331, "toscana"),
    ("Arezzo",        43.464, 11.879, "toscana"),
    ("Grosseto",      42.760, 11.114, "toscana"),
]


@pytest.mark.parametrize("city,lat,lon,expected", OTHER_PROVINCIAL_CAPITALS)
def test_provincial_capital_classified_correctly(city, lat, lon, expected):
    got = region_from_coordinates(lat, lon)
    assert got == expected, f"{city} ({lat},{lon}): got {got!r}, want {expected!r}"


# ── 4) Borzano canonical regression guard ─────────────────────────────────────

def test_borzano_canonical_emilia_romagna():
    """Borzano di Albinea is the canonical reference for all public
    Borzano numbers. Its region must remain emilia_romagna across any
    refactor of the regional classifier."""
    assert region_from_coordinates(44.601, 10.636) == "emilia_romagna"


# ── 5) Trento/Bolzano split ───────────────────────────────────────────────────

def test_bolzano_city_returns_bolzano():
    assert region_from_coordinates(46.498, 11.354) == "bolzano"


def test_trento_city_returns_trento():
    assert region_from_coordinates(46.066, 11.122) == "trento"


def test_taa_split_threshold_north_is_bolzano():
    # Merano (BZ), well north of the split — should be bolzano
    assert region_from_coordinates(46.670, 11.160) == "bolzano"


def test_taa_split_threshold_south_is_trento():
    # Rovereto (TN), well south of the split — should be trento
    assert region_from_coordinates(45.890, 11.040) == "trento"


# ── 6) Sea / fallback behaviour ───────────────────────────────────────────────

def test_sea_point_near_naples_falls_back_to_campania():
    # Bay of Naples, offshore (~10 km west of Capri)
    got = region_from_coordinates(40.50, 14.05)
    assert got == "campania", f"sea fallback failed: got {got!r}"


def test_sea_point_near_venice_falls_back_to_veneto():
    # Adriatic, offshore Venice
    got = region_from_coordinates(45.30, 12.60)
    assert got == "veneto", f"sea fallback failed: got {got!r}"


def test_point_outside_italy_returns_nearest_region():
    """Paris is obviously outside Italy. The fallback returns the
    nearest Italian region by polygon distance (Valle d'Aosta or
    Piemonte for Paris). The exact answer is implementation-defined
    but must be a valid REGIONAL_CALENDARS key, not raise an error."""
    got = region_from_coordinates(48.85, 2.35)
    assert got in REGIONAL_CALENDARS, f"out-of-Italy fallback invalid: {got!r}"


# ── 7) REGION_ALIASES — voice #5 fix ──────────────────────────────────────────

def test_trentino_alto_adige_alias_removed():
    """Pre-fix, REGION_ALIASES mapped 'trentino_alto_adige' to 'trento',
    silently dropping Bolzano's autumn break. Post-fix the alias must
    no longer exist."""
    assert "trentino_alto_adige" not in REGION_ALIASES


def test_friuli_alias_preserved():
    """'friuli_venezia_giulia' → 'friuli_vg' is a purely linguistic
    alias (one region, one calendar) and must remain."""
    assert REGION_ALIASES.get("friuli_venezia_giulia") == "friuli_vg"


def test_ambiguous_region_raises_valueerror():
    """Passing the ambiguous TAA key must raise ValueError with a
    helpful message naming both autonomous provinces."""
    with pytest.raises(ValueError, match=r"trento.*bolzano|bolzano.*trento"):
        _build_regional_vacations("trentino_alto_adige", 2025)


def test_explicit_trento_works():
    vac, start, end = _build_regional_vacations("trento", 2025)
    assert isinstance(vac, set) and len(vac) > 0
    assert 0 <= start < 365 and 0 <= end < 365


def test_explicit_bolzano_works_with_autumn_break():
    vac_bz, _, _ = _build_regional_vacations("bolzano", 2025)
    vac_tn, _, _ = _build_regional_vacations("trento", 2025)
    # Bolzano has the long autumn break (30/10–08/11), Trento has a
    # shorter one (31/10–02/11) → Bolzano vacation set strictly larger.
    assert len(vac_bz) > len(vac_tn), (
        "Bolzano must have more vacation days than Trento "
        "(long autumn break is what voice #5 was protecting)"
    )


def test_ambiguous_regions_registry_minimal():
    """Guard that we have not silently expanded the ambiguous set."""
    assert _AMBIGUOUS_REGIONS.keys() == {"trentino_alto_adige"}
