"""Tests for POST /estimate (public Level-1 estimate).

No live PVGIS calls: `get_cached_tmy` is replaced with a synthetic 8 760-hour
TMY (same pattern as tests/test_multi_footprint.py). The slowapi limiter is
disabled for the module — the suite issues more requests than the public
5/minute budget.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.pvgis_client import (
    HourlyRecord, MonthlyClimate, PVGISLocation, TMYResult,
)


# ── Synthetic TMY (offline) ───────────────────────────────────────────────────
_DAYS_M = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
_T_MONTHLY = [2.0, 4.0, 8.5, 13.0, 18.0, 22.5, 25.5, 25.0, 20.5, 14.5, 8.0, 3.0]


def _make_synthetic_tmy(lat: float = 44.6, lon: float = 10.6) -> TMYResult:
    records: list[HourlyRecord] = []
    for m, days in enumerate(_DAYS_M, start=1):
        t_amb = _T_MONTHLY[m - 1]
        for d in range(1, days + 1):
            for h in range(24):
                if 6 <= h <= 18:
                    sun = math.sin(math.pi * (h - 6) / 12)
                    ghi, dni, dhi = 800.0 * sun, 700.0 * sun, 200.0 * sun
                else:
                    ghi = dni = dhi = 0.0
                records.append(HourlyRecord(
                    month=m, day=d, hour=h, T2m=t_amb, RH=60.0,
                    G_h=ghi, Gb_n=dni, Gd_h=dhi, IR_h=0.0,
                    WS10m=2.0, WD10m=180.0, SP=101325.0,
                ))
    ghi_m = [0.0] * 12
    dni_m = [0.0] * 12
    dhi_m = [0.0] * 12
    for r in records:
        ghi_m[r.month - 1] += r.G_h / 1000.0
        dni_m[r.month - 1] += r.Gb_n / 1000.0
        dhi_m[r.month - 1] += r.Gd_h / 1000.0
    monthly = MonthlyClimate(
        T_avg=list(_T_MONTHLY),
        T_min=[t - 5.0 for t in _T_MONTHLY],
        T_max=[t + 5.0 for t in _T_MONTHLY],
        GHI_monthly=ghi_m, DNI_monthly=dni_m, DHI_monthly=dhi_m,
        WS_avg=[2.0] * 12, RH_avg=[60.0] * 12,
    )
    location = PVGISLocation(
        latitude=lat, longitude=lon, elevation_m=200.0,
        country="IT", dataset="PVGIS-SARAH3", horizon_profile=[],
    )
    return TMYResult(location=location, hourly=records, monthly=monthly)


TMY = _make_synthetic_tmy()


@pytest.fixture(scope="module")
def client():
    import api.routers.estimate as est_mod
    from api.utils.limiter import limiter
    from api.main import app

    orig_fetch = est_mod.get_cached_tmy
    orig_enabled = limiter.enabled
    est_mod.get_cached_tmy = lambda lat, lon: (TMY, True)
    limiter.enabled = False
    try:
        with TestClient(app) as c:
            yield c
    finally:
        est_mod.get_cached_tmy = orig_fetch
        limiter.enabled = orig_enabled


_BASE_REQ = {
    "comune": "Albinea",
    "tipologia": "media",
    "fascia_studenti": "300_600",
}

_RANGE_FIELDS = [
    "produzione_annua_kwh", "risparmio_annuo_eur",
    "ricavi_cessione_eur_anno", "payback_anni", "co2_evitata_kg_anno",
]


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_albinea(client):
    r = client.post("/estimate", json=_BASE_REQ)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["comune_risolto"] == {
        "nome": "Albinea", "provincia": "RE",
        "regione": "Emilia-Romagna", "zona_climatica": "E",
    }
    for field in _RANGE_FIELDS:
        rng = body[field]
        assert rng["low"] <= rng["high"], f"{field}: {rng}"
        assert rng["low"] > 0, f"{field} non positivo: {rng}"
    # Fixed kWp + same TMY → production is identical across the sweep
    assert body["produzione_annua_kwh"]["low"] == body["produzione_annua_kwh"]["high"]
    assert body["capex_indicativo_eur"] > 0
    assert set(body["taglie_disponibili"]) == {"minimal", "recommended", "maximum"}
    assert (body["taglie_disponibili"]["minimal"]
            <= body["taglie_disponibili"]["recommended"]
            <= body["taglie_disponibili"]["maximum"])
    assert isinstance(body["assunzioni"], list) and len(body["assunzioni"]) >= 5


def test_payback_coherent_with_total_benefit(client):
    """CAPEX / (risparmio + ricavi cessione) ≈ payback semplice, per run.

    Pairing: production is fixed, so the low-area building has LOW saving
    (less self-consumption) but HIGH export revenue — and vice versa. The
    engine payback uses 25-y cash flows (O&M −1 %/yr, escalation +3 %/yr),
    so year-1 naive payback only approximates it: tolerance 20 %.
    """
    body = client.post("/estimate", json=_BASE_REQ).json()
    capex = body["capex_indicativo_eur"]
    saving = body["risparmio_annuo_eur"]
    ricavi = body["ricavi_cessione_eur_anno"]
    payback = body["payback_anni"]

    benefit_low_building = saving["low"] + ricavi["high"]
    benefit_high_building = saving["high"] + ricavi["low"]
    assert capex / benefit_low_building == pytest.approx(payback["high"], rel=0.2)
    assert capex / benefit_high_building == pytest.approx(payback["low"], rel=0.2)


def test_assunzioni_mention_area_and_zone(client):
    body = client.post("/estimate", json=_BASE_REQ).json()
    text = " ".join(body["assunzioni"])
    assert "4.050" in text, "separatore migliaia italiano atteso (4.050)"
    assert "4,050" not in text
    assert "DPR 412/93" in text
    # CO₂ methodology is reserved for the Level-2 report (Sirot, giu 2026):
    # the response field stays, but no assumption may explain the factor.
    assert "CO₂" not in text and "ENTSO" not in text


# ── Comune resolution ─────────────────────────────────────────────────────────

def test_homonym_without_provincia_422_with_candidates(client):
    r = client.post("/estimate", json={**_BASE_REQ, "comune": "Castro"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert len(detail["candidati"]) >= 2
    provinces = {c["provincia"] for c in detail["candidati"]}
    assert {"BG", "LE"} <= provinces


def test_homonym_with_provincia_resolves(client):
    r = client.post("/estimate",
                    json={**_BASE_REQ, "comune": "Castro", "provincia": "LE"})
    assert r.status_code == 200, r.text
    assert r.json()["comune_risolto"]["provincia"] == "LE"
    assert r.json()["comune_risolto"]["regione"] == "Puglia"


def test_unknown_comune_404(client):
    r = client.post("/estimate", json={**_BASE_REQ, "comune": "Atlantide"})
    assert r.status_code == 404
    assert "Atlantide" in r.json()["detail"]


# ── PV size handling ──────────────────────────────────────────────────────────

def test_user_potenza_is_used_verbatim(client):
    r = client.post("/estimate", json={**_BASE_REQ, "potenza_kwp": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kwp_ipotizzato"] == 100.0
    assert body["auto_dimensionato"] is False


def test_auto_sizing_uses_recommended(client):
    body = client.post("/estimate", json=_BASE_REQ).json()
    assert body["auto_dimensionato"] is True
    assert body["kwp_ipotizzato"] == body["taglie_disponibili"]["recommended"]


# ── Construction year default ─────────────────────────────────────────────────

def test_fascia_anno_omitted_assumes_1981(client):
    body = client.post("/estimate", json=_BASE_REQ).json()  # no fascia_anno
    assert any("1981" in a for a in body["assunzioni"])


# ── Sanity anchor (test-only, not runtime logic) ──────────────────────────────

def test_central_demand_within_40pct_of_enea_benchmark():
    """Bottom-up demand of the central profile vs SCHOOL_ELECTRIC_BENCHMARK.

    Anchor, not tautology: if it fails, REPORT — do not tune numbers to pass.
    """
    from api.routers.loads import _school_inputs
    from api.schemas.loads import SchoolLoadsRequest
    from school.estimate_defaults import build_estimate_profiles
    from school.loads_school import calc_school_loads
    from school.climate_zones import get_electric_benchmark

    profile = build_estimate_profiles("media", "300-600")["profiles"]["central"]
    profile = {**profile, "climate_zone": "E", "region": "emilia_romagna"}
    ghi = [rec.G_h for rec in TMY.hourly]
    loads = calc_school_loads(_school_inputs(SchoolLoadsRequest(**profile)),
                              tmy_hourly_ghi=ghi)

    area = profile["floor_area_m2"]
    benchmark_kwh = get_electric_benchmark("E", "middle", area)
    ratio = loads["total_annual_kwh"] / benchmark_kwh
    assert 0.6 <= ratio <= 1.4, (
        f"bottom-up {loads['total_annual_kwh']:.0f} kWh vs benchmark ENEA "
        f"{benchmark_kwh:.0f} kWh → ratio {ratio:.2f} fuori da ±40%"
    )
