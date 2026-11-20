"""Quote check (POST /verifica/preventivo) — numbers only, no documents."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from residential.quote_check import (  # noqa: E402
    expected_cost, price_verdict, price_with_vat, production_verdict,
)
from tests.test_estimate_casa_api import TMY  # noqa: E402  (synthetic TMY, no PVGIS calls)


# ── unit ──────────────────────────────────────────────────────────────────────

def test_fascia_3kwp_senza_batteria():
    e = expected_cost(3.0)
    assert (e.low, e.central, e.high) == (4800, 6000, 7600)  # curve rounded to 100 €


def test_batteria_alza_tutta_la_fascia():
    a, b = expected_cost(3.0), expected_cost(3.0, battery_kwh=5.0)
    assert b.low - a.low == pytest.approx(800 + 450 * 5)
    assert b.central - a.central == pytest.approx(1200 + 550 * 5)
    assert b.high - a.high == pytest.approx(1500 + 750 * 5)


@pytest.mark.parametrize("price,atteso", [
    (4_000, "sotto_fascia"), (5_500, "fascia_bassa"), (7_000, "fascia_alta"), (9_000, "sopra_fascia"),
])
def test_giudizio_prezzo(price, atteso):
    assert price_verdict(price, expected_cost(3.0))[0] == atteso


@pytest.mark.parametrize("promessa,atteso", [
    (3_000, "prudente"), (4_000, "coerente"), (4_300, "ottimistica"), (4_800, "gonfiata"),
])
def test_giudizio_produzione(promessa, atteso):
    assert production_verdict(promessa, 4_000)[0] == atteso


def test_iva():
    assert price_with_vat(1_000, True) == 1_000
    assert price_with_vat(1_000, False) == pytest.approx(1_100)


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import api.routers.verifica_preventivo as vp_mod
    from api.utils.limiter import limiter
    from api.main import app

    orig, orig_enabled = vp_mod.get_cached_tmy, limiter.enabled
    vp_mod.get_cached_tmy = lambda lat, lon: (TMY, True)
    limiter.enabled = False
    try:
        with TestClient(app) as c:
            yield c
    finally:
        vp_mod.get_cached_tmy, limiter.enabled = orig, orig_enabled


_BASE = {"comune": "Albinea", "provincia": "RE", "orientamento": "sud"}


def test_un_preventivo(client):
    r = client.post("/verifica/preventivo", json={**_BASE, "preventivi": [
        {"kwp": 3.0, "prezzo_totale_eur": 6_500, "produzione_promessa_kwh": 9_999},
    ]})
    assert r.status_code == 200, r.text
    e = r.json()["esiti"][0]
    assert e["etichetta"] == "Preventivo A"
    assert e["giudizio_prezzo"] == "fascia_alta"
    assert e["giudizio_produzione"] == "gonfiata"
    assert e["eur_per_kwp"] == round(6_500 / 3)
    assert r.json()["piu_conveniente"] is None
    assert any("non la qualità" in a for a in r.json()["avvertenze"])


def test_confronto_tre_preventivi(client):
    r = client.post("/verifica/preventivo", json={**_BASE, "preventivi": [
        {"etichetta": "Ditta 1", "kwp": 3.0, "prezzo_totale_eur": 7_500},
        {"etichetta": "Ditta 2", "kwp": 4.0, "prezzo_totale_eur": 7_000},
        {"etichetta": "Ditta 3", "kwp": 3.0, "prezzo_totale_eur": 5_000, "prezzo_iva_inclusa": False},
    ]}).json()
    assert len(r["esiti"]) == 3
    assert r["esiti"][2]["prezzo_iva_inclusa_eur"] == 5_500
    assert r["piu_conveniente"] == "Ditta 2"      # most kWh per euro


def test_sovradimensionamento(client):
    r = client.post("/verifica/preventivo", json={**_BASE, "consumo_annuo_kwh": 1_500, "preventivi": [
        {"kwp": 6.0, "prezzo_totale_eur": 9_500},
    ]}).json()
    assert r["esiti"][0]["sovradimensionato"] is True


def test_ombra_abbassa_la_produzione_stimata(client):
    p = [{"kwp": 3.0, "prezzo_totale_eur": 6_000}]
    base = client.post("/verifica/preventivo", json={**_BASE, "preventivi": p}).json()
    ombra = client.post("/verifica/preventivo", json={
        **_BASE, "preventivi": p, "ombra_ostacoli": "si", "ombra_lati": ["sud"], "ombra_durata": "qualche_ora",
    }).json()
    assert ombra["esiti"][0]["produzione_stimata_kwh"] < base["esiti"][0]["produzione_stimata_kwh"]


def test_limiti_input(client):
    assert client.post("/verifica/preventivo", json={**_BASE, "preventivi": []}).status_code == 422
    quattro = [{"kwp": 3.0, "prezzo_totale_eur": 6_000}] * 4
    assert client.post("/verifica/preventivo", json={**_BASE, "preventivi": quattro}).status_code == 422


def test_sovradimensionato_non_vince_il_confronto(client):
    r = client.post("/verifica/preventivo", json={**_BASE, "consumo_annuo_kwh": 3_500, "preventivi": [
        {"etichetta": "Piccolo", "kwp": 3.0, "prezzo_totale_eur": 7_000},
        {"etichetta": "Enorme", "kwp": 8.0, "prezzo_totale_eur": 11_000},
    ]}).json()
    assert r["esiti"][1]["sovradimensionato"] is True
    assert r["piu_conveniente"] == "Piccolo"
    assert "escluso" in r["nota_confronto"]


def test_batteria_non_penalizza_eur_per_kwh(client):
    r = client.post("/verifica/preventivo", json={**_BASE, "preventivi": [
        {"etichetta": "Senza", "kwp": 3.0, "prezzo_totale_eur": 6_000},
        {"etichetta": "Con", "kwp": 3.0, "prezzo_totale_eur": 6_000 + 1_200 + 550 * 5, "batteria_kwh": 5},
    ]}).json()
    a, b = r["esiti"]
    assert a["eur_per_kwh_annuo"] == b["eur_per_kwh_annuo"]
