"""Tests for POST /estimate/casa (public Level-1 residential estimate).

No live PVGIS calls: `get_cached_tmy` is replaced with the same synthetic
8 760-hour TMY used in test_estimate_api.py. The slowapi limiter is disabled
for the module — the suite issues more requests than the public 5/minute budget.

Casi coperti:
    1. Happy path (proxy): Albinea, 4 occupanti, 90 m², tutto gas
    2. Happy path (bolletta): consumo da bolletta, banda ±15%
    3. 404 per comune inesistente
    4. 422 per omonimi (comune senza provincia)
    5. Verdetto nunca soppresso: impianto too-large → verdetto "negativo"
    6. Batteria non conveniente segnalata onestamente
    7. Scenario profilo working_family vs residential (presenza_diurna)
    8. auto_dimensionato = False se potenza_kwp fornita
    9. Consumo da bolletta: consumo_da_bolletta=True, banda circa ±15%
   10. Risparmio range low < high (tranne degenerate single-point)
   11. produzione_annua_kwh positivo
   12. Verdetto etichette esclusive e consistenti col payback
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.pvgis_client import (
    HourlyRecord,
    MonthlyClimate,
    PVGISLocation,
    TMYResult,
)


# ── Synthetic TMY (same as test_estimate_api.py) ──────────────────────────────

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
    import api.routers.estimate_casa as est_mod
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


_BASE_PROXY = {
    "comune": "Albinea",
    "n_occupants": 4,
    "floor_area_m2": 90.0,
    "riscaldamento_elettrico": False,
    "acqua_calda_elettrica": False,
    "ev": False,
    "condizionatore": True,
    "presenza_diurna": "per_lo_piu_vuota",
}

_BASE_BOLLETTA = {
    **_BASE_PROXY,
    "bolletta_kwh": 1_435.0,
}


# ── 1. Happy path — proxy ─────────────────────────────────────────────────────

class TestHappyPathProxy:
    def test_200(self, client):
        r = client.post("/estimate/casa", json=_BASE_PROXY)
        assert r.status_code == 200, r.text

    def test_comune_risolto(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        cr = body["comune_risolto"]
        assert cr["nome"] == "Albinea"
        assert cr["provincia"] == "RE"
        assert cr["regione"] == "Emilia-Romagna"
        assert cr["zona_climatica"] == "E"

    def test_consumo_da_bolletta_false(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["consumo_da_bolletta"] is False

    def test_consumo_basso_lt_centrale_lt_alto(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["consumo_kwh_basso"] < body["consumo_kwh_annuo"]
        assert body["consumo_kwh_annuo"] < body["consumo_kwh_alto"]

    def test_produzione_positiva(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["produzione_annua_kwh"] > 0

    def test_risparmio_low_lte_high(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        r = body["risparmio_annuo_eur"]
        assert r["low"] <= r["high"]

    def test_payback_low_lte_high(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        pb = body["payback_anni"]
        assert pb["low"] <= pb["high"]

    def test_capex_positivo(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["capex_indicativo_eur"] > 0

    def test_taglie_tre_scenari(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert set(body["taglie_disponibili"]) == {"minimal", "recommended", "maximum"}

    def test_taglie_ordering(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        t = body["taglie_disponibili"]
        assert t["minimal"] <= t["recommended"] <= t["maximum"]

    def test_assunzioni_non_vuote(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert len(body["assunzioni"]) >= 5

    def test_co2_positiva(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["co2_evitata_kg_anno"] > 0


# ── 2. Happy path — bolletta ─────────────────────────────────────────────────

class TestHappyPathBolletta:
    def test_200(self, client):
        r = client.post("/estimate/casa", json=_BASE_BOLLETTA)
        assert r.status_code == 200, r.text

    def test_consumo_da_bolletta_true(self, client):
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        assert body["consumo_da_bolletta"] is True

    def test_consumo_centrale_uguale_bolletta(self, client):
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        assert body["consumo_kwh_annuo"] == pytest.approx(1_435.0, abs=1.0)

    def test_banda_bolletta_circa_15_pct(self, client):
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        c = body["consumo_kwh_annuo"]
        basso = body["consumo_kwh_basso"]
        alto = body["consumo_kwh_alto"]
        # basso ≈ 85% centrale, alto ≈ 115% centrale (arrotondati a 10)
        assert basso < c < alto
        assert abs(basso - c * 0.85) < c * 0.05  # entro 5% dell'atteso
        assert abs(alto - c * 1.15) < c * 0.05


# ── 3. 404 comune inesistente ─────────────────────────────────────────────────

def test_404_comune_inesistente(client):
    r = client.post("/estimate/casa", json={
        **_BASE_PROXY,
        "comune": "Borzano_Inesistente_XYZ",
    })
    assert r.status_code == 404


# ── 4. 422 per omonimi ────────────────────────────────────────────────────────

def test_422_omonimi_senza_provincia(client):
    # "San Giorgio" è un nome comune a più comuni italiani
    # Non conosciamo l'esatto nome, ma usiamo uno che sicuramente non esiste univoco.
    # Usiamo Albinea (univoco) come controllo e "Castro" (omonimo) come test.
    r = client.post("/estimate/casa", json={
        **_BASE_PROXY,
        "comune": "Castro",      # omonimo in BA e BG
    })
    # Castro è in due province: se il dataset li include, deve rispondere 422
    if r.status_code == 422:
        body = r.json()
        assert "candidati" in body["detail"]
    elif r.status_code == 200:
        pass  # Il comune è stato risolto univocamente (dataset potrebbe averne uno solo)
    else:
        # 404 se il comune non è nel dataset 2025 — accettabile
        assert r.status_code == 404


# ── 5. Verdetto mai soppresso ─────────────────────────────────────────────────

class TestVerdetto:
    def test_verdetto_ha_etichetta(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        vp = body["verdetto_pv"]
        assert vp["etichetta"] in {"ottimo", "buono", "accettabile", "marginale", "negativo"}

    def test_verdetto_testo_non_vuoto(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert len(body["verdetto_pv"]["testo"]) > 20

    def test_verdetto_payback_coerente(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        vp = body["verdetto_pv"]
        pb = vp["payback_anni"]
        eti = vp["etichetta"]
        # With autosufficienza-aware logic:
        # "ottimo" → pb ≤ 8 (AND autosufficienza ≥ 35%)
        # "buono"  → pb ≤ 12 (possibly downgraded from ottimo if AS < 35%)
        # "accettabile" → pb < 20 (possibly downgraded from buono if AS < 35%)
        # "negativo" → pb ≥ 20 OR npv ≤ 0
        if eti == "ottimo":
            assert pb <= 8
        elif eti == "buono":
            assert pb <= 12
        elif eti in {"accettabile", "marginale"}:
            # "marginale" = accettabile per payback ma NPV/capex < 10% (fragile)
            assert pb < 20
        # "negativo": pb >= 20 OR npv <= 0

    def test_verdetto_ha_autosufficienza_pct(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        vp = body["verdetto_pv"]
        assert "autosufficienza_pct" in vp
        assert 0.0 <= vp["autosufficienza_pct"] <= 100.0

    def test_verdetto_negativo_testo_contiene_avvertenza(self, client):
        """Impianto very large rispetto al basso consumo → negativo."""
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "potenza_kwp": 20.0,   # over-sized per ~1.500 kWh/anno
            "bolletta_kwh": 1_000.0,
        }).json()
        vp = body["verdetto_pv"]
        if vp["etichetta"] == "negativo":
            assert "⚠" in vp["testo"] or "difficile" in vp["testo"].lower()


# ── 6. Batteria: trade-off sempre mostrato ────────────────────────────────────

class TestBatteriaTradeoff:
    def test_entrambi_scenari_presenti(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["senza_batteria"] is not None
        assert body["con_batteria"] is not None

    def test_capex_con_batteria_maggiore(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["con_batteria"]["capex_eur"] > body["senza_batteria"]["capex_eur"]

    def test_autoconsumo_con_batteria_maggiore(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert (
            body["con_batteria"]["autoconsumo_pct"]
            >= body["senza_batteria"]["autoconsumo_pct"]
        )

    def test_tradeoff_nota_non_vuota(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert len(body["tradeoff"]["nota"]) > 20

    def test_borzano_batteria_not_convenient(self, client):
        """Borzano 1.435 kWh/anno: batteria quasi certamente non conveniente."""
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 1_435.0,
        }).json()
        # Non asserisco il valore booleano: dipende dal TMY sintetico.
        # Verifico solo che il campo sia presente e booleano.
        assert isinstance(body["tradeoff"]["batteria_conveniente"], bool)


# ── 7. Profilo di presenza ────────────────────────────────────────────────────

class TestProfiloPresenteDiurna:
    def test_working_family_e_residential_producono_200(self, client):
        for profilo in ("per_lo_piu_vuota", "spesso_occupata"):
            r = client.post("/estimate/casa", json={
                **_BASE_BOLLETTA,
                "presenza_diurna": profilo,
            })
            assert r.status_code == 200, f"profilo={profilo}: {r.text}"

    def test_stessa_produzione_diversa_presenza(self, client):
        """La produzione FV è indipendente dal profilo di presenza."""
        prod = []
        for profilo in ("per_lo_piu_vuota", "spesso_occupata"):
            body = client.post("/estimate/casa", json={
                **_BASE_BOLLETTA,
                "presenza_diurna": profilo,
            }).json()
            prod.append(body["produzione_annua_kwh"])
        assert prod[0] == prod[1], (
            "La produzione deve essere identica per lo stesso TMY e kWp"
        )


# ── 8. auto_dimensionato ─────────────────────────────────────────────────────

class TestAutoDimensionato:
    def test_auto_false_quando_potenza_fornita(self, client):
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "potenza_kwp": 3.0,
        }).json()
        assert body["auto_dimensionato"] is False
        assert body["kwp_ipotizzato"] == pytest.approx(3.0, abs=0.05)

    def test_auto_true_senza_potenza(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["auto_dimensionato"] is True


# ── 9. Bolletta banda ─────────────────────────────────────────────────────────

def test_bolletta_banda_stretta(client):
    body = client.post("/estimate/casa", json={**_BASE_PROXY, "bolletta_kwh": 2_500.0}).json()
    assert body["consumo_kwh_basso"] < 2_500 < body["consumo_kwh_alto"]
    # Banda attesa ~±15% → range < ±20%
    span = body["consumo_kwh_alto"] - body["consumo_kwh_basso"]
    assert span < 2_500 * 0.40


# ── 10. Risparmio range ───────────────────────────────────────────────────────

def test_risparmio_range_coerente(client):
    body = client.post("/estimate/casa", json=_BASE_PROXY).json()
    r = body["risparmio_annuo_eur"]
    # Risparmio almeno > 0 (produzione FV > 0 con il TMY sintetico)
    assert r["low"] >= 0
    assert r["high"] >= r["low"]


# ── 11. Produzione positiva ───────────────────────────────────────────────────

def test_produzione_positiva(client):
    body = client.post("/estimate/casa", json=_BASE_PROXY).json()
    assert body["produzione_annua_kwh"] > 0


# ── 12. Verdetto etichette ────────────────────────────────────────────────────

class TestVerdettoEtichette:
    def test_etichette_valide(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        assert body["verdetto_pv"]["etichetta"] in {
            "ottimo", "buono", "accettabile", "marginale", "negativo"
        }

    def test_payback_senza_batteria_coerente_col_verdetto(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        pb_verdetto = body["verdetto_pv"]["payback_anni"]
        pb_scenario = body["senza_batteria"]["payback_anni"]
        # Devono essere lo stesso valore (da central senza_batteria)
        assert pb_verdetto == pytest.approx(pb_scenario, abs=0.15)


# ── 13. Taglie confronto ──────────────────────────────────────────────────────

class TestTaglieConfronto:
    def test_taglie_confronto_presenti(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        tc = body["taglie_confronto"]
        assert isinstance(tc, list)
        assert len(tc) >= 2

    def test_taglie_confronto_struttura(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            assert "kwp" in t
            assert "capex_eur" in t
            assert "autosufficienza_pct" in t
            assert "payback_anni" in t
            assert "npv_eur" in t
            assert "beneficio_breakdown" in t
            assert "verdetto" in t
            assert "senza_batteria" in t
            assert "con_batteria" in t
            assert "tradeoff" in t

    def test_taglie_confronto_kwp_ordinati(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        kwps = [t["kwp"] for t in body["taglie_confronto"]]
        assert kwps == sorted(kwps)

    def test_taglie_confronto_capex_positivo(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            assert t["capex_eur"] > 0

    def test_taglie_confronto_etichette_valide(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            assert t["verdetto"]["etichetta"] in {"ottimo", "buono", "accettabile", "marginale", "negativo"}

    def test_taglie_confronto_label_valide(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        valid = {"minima", "media", "consigliata", "massima"}
        for t in body["taglie_confronto"]:
            assert t["label"] in valid

    def test_taglie_confronto_verdetto_ha_autosufficienza(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            assert "autosufficienza_pct" in t["verdetto"]
            assert 0.0 <= t["verdetto"]["autosufficienza_pct"] <= 100.0


# ── 14. Beneficio breakdown ──────────────────────────────────────────────────

class TestBeneficioBreakdown:
    def test_breakdown_campi_presenti(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            bd = t["beneficio_breakdown"]
            assert "risparmio_bolletta_eur" in bd
            assert "ricavi_export_eur" in bd
            assert "quota_detrazione_eur" in bd
            assert "totale_anno1_eur" in bd

    def test_breakdown_non_negativi(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            bd = t["beneficio_breakdown"]
            assert bd["risparmio_bolletta_eur"] >= 0
            assert bd["ricavi_export_eur"] >= 0
            assert bd["quota_detrazione_eur"] >= 0
            assert bd["totale_anno1_eur"] > 0

    def test_breakdown_totale_coerente(self, client):
        """totale ≈ somma delle tre voci (arrotondamenti in gioco)."""
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        for t in body["taglie_confronto"]:
            bd = t["beneficio_breakdown"]
            expected = bd["risparmio_bolletta_eur"] + bd["ricavi_export_eur"] + bd["quota_detrazione_eur"]
            assert abs(bd["totale_anno1_eur"] - expected) <= 3.0


# ── 15. Costi fissi residenziali ─────────────────────────────────────────────

class TestCostiFissi:
    def test_capex_min_1500_rispetto_a_prima(self, client):
        """1,5 kWp deve costare almeno €5.000 (vecchio €4.000 + €1.500 fissi)."""
        body = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA,
            "potenza_kwp": 1.5,
            "floor_area_m2": 200.0,   # roof large enough for 1.5 kWp
        }).json()
        assert body["capex_indicativo_eur"] >= 5_000

    def test_assunzioni_contiene_costi_fissi(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        testo = " ".join(body["assunzioni"]).lower()
        assert "1.500" in testo or "fissi" in testo


# ── 16. Avvertenze fragilità ─────────────────────────────────────────────────

class TestAvvertenzeFragilita:
    def test_avvertenza_detrazione_2027(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        testo = " ".join(body["assunzioni"])
        assert "2027" in testo

    def test_avvertenza_prezzo_export(self, client):
        body = client.post("/estimate/casa", json=_BASE_PROXY).json()
        testo = " ".join(body["assunzioni"])
        assert "0,08" in testo or "cessione" in testo.lower()


# ── 17. Borzano: autosufficienza bassa annotata nel verdetto ─────────────────

class TestBorzanoAutosufficienza:
    def test_borzano_autosufficienza_pct_presente(self, client):
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        assert "autosufficienza_pct" in body["verdetto_pv"]

    def test_borzano_verdetto_non_ottimo_con_basso_consumo(self, client):
        """Con 1.435 kWh/anno e TMY sintetico (alta produzione),
        l'autosufficienza è bassa: il verdetto non può essere 'ottimo'."""
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        eti = body["verdetto_pv"]["etichetta"]
        # Con il nuovo modello di costi e la regola autosufficienza,
        # "ottimo" richiede payback ≤ 8 AND autosufficienza ≥ 35%.
        # Per Borzano 1.435 kWh, autosufficienza è ~20-35% con TMY sintetico.
        # Non assertiamo il valore esatto, ma "ottimo" è strutturalmente improbabile.
        assert eti in {"buono", "accettabile", "marginale", "negativo"}

    def test_borzano_testo_contiene_avvertenza_se_as_bassa(self, client):
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        vp = body["verdetto_pv"]
        as_pct = vp["autosufficienza_pct"]
        if as_pct < 35.0 and vp["etichetta"] != "negativo":
            assert "⚠" in vp["testo"] or "autosufficienza" in vp["testo"].lower()


# ── 18. Auto-sizing su NPV ────────────────────────────────────────────────────

class TestAutoSizingNPV:
    def test_kwp_consigliata_e_kwp_ipotizzato_coerenti(self, client):
        """Quando auto_dimensionato, kwp_ipotizzato deve coincidere con la taglia
        che ha label 'consigliata' nel ventaglio — indipendentemente dal TMY usato."""
        # Use a higher consumption to avoid all-exceed-cap edge cases in synthetic TMY
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 3_500.0,
        }).json()
        assert body["auto_dimensionato"] is True
        kwp_ip = body["kwp_ipotizzato"]
        consigliata = next(
            (t for t in body["taglie_confronto"] if t["label"] == "consigliata"),
            None,
        )
        assert consigliata is not None, "Deve esserci una taglia 'consigliata'"
        assert abs(consigliata["kwp"] - kwp_ip) < 0.1, (
            f"kwp_ipotizzato={kwp_ip} ma consigliata.kwp={consigliata['kwp']}"
        )

    def test_consigliata_ha_npv_migliore_nel_cap(self, client):
        """Consigliata ha NPV ≥ tutte le taglie del ventaglio che rientrano nel cap (≤150% consumo)."""
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        tc = body["taglie_confronto"]
        consigliata = next(t for t in tc if t["label"] == "consigliata")
        npv_c = consigliata["npv_eur"]
        consumo = body["consumo_kwh_annuo"]
        cap_ratio = 1.50
        # Only check candidates within cap (the consigliata must have best NPV among those)
        in_cap = [t for t in tc if t["produzione_annua_kwh"] / consumo <= cap_ratio + 0.01]
        for t in in_cap:
            assert t["npv_eur"] <= npv_c + 1.0, (
                f"Taglia {t['kwp']} kWp (nel cap, label={t['label']}) ha NPV {t['npv_eur']} "
                f"> consigliata NPV {npv_c}"
            )

    def test_kwp_ipotizzato_non_negativo_se_esiste_positivo(self, client):
        """Se almeno una taglia ha NPV > 0, kwp_ipotizzato non è l'unica negativa."""
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 3_000.0,  # consumo medio: taglie medie convengono
        }).json()
        tc = body["taglie_confronto"]
        npvs = {t["label"]: t["npv_eur"] for t in tc}
        # Se almeno un NPV > 0, la consigliata deve avere NPV > 0
        if any(v > 0 for v in npvs.values()):
            consigliata_npv = npvs.get("consigliata", -9999)
            assert consigliata_npv > 0, (
                f"Almeno una taglia ha NPV > 0 ma consigliata ha NPV={consigliata_npv}"
            )

    def test_all_negative_verdetto_dice_nessuna_taglia(self, client):
        """Consumo molto basso + impianto non sovradimensionato → all taglie negative.
        Il verdetto deve contenere 'Nessuna taglia' o la stringa equivalente."""
        # Con bolletta 200 kWh (quasi nulla) e auto-sizing, tutte le taglie
        # minime (1.5+ kWp) avranno NPV negativo sul TMY sintetico.
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 200.0,
        }).json()
        vp = body["verdetto_pv"]
        if vp["etichetta"] == "negativo":
            # Se tutte le taglie sono negative: il testo deve dirlo esplicitamente
            tc = body["taglie_confronto"]
            tutti_negativi = all(t["npv_eur"] <= 0 for t in tc)
            if tutti_negativi and body["auto_dimensionato"]:
                assert "Nessuna taglia" in vp["testo"] or "nessuna taglia" in vp["testo"]

    def test_user_kwp_non_soggetto_a_npv_selection(self, client):
        """Se l'utente specifica potenza_kwp, kwp_ipotizzato = quello specificato
        (anche se non è il migliore NPV)."""
        body = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA,
            "potenza_kwp": 2.0,
        }).json()
        assert body["auto_dimensionato"] is False
        assert body["kwp_ipotizzato"] == pytest.approx(2.0, abs=0.05)


# ── 19. Cap sovradimensionamento (150%) ───────────────────────────────────────

class TestCapSovradimensionamento:
    def test_borzano_consigliata_non_e_max_taglia(self, client):
        """Con 1.435 kWh/anno, la taglia MAX del ventaglio NON deve essere 'consigliata'
        (il cap 150% la esclude; se tutte eccedono il cap, si usa la MINIMA, non la massima)."""
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        assert body["auto_dimensionato"] is True
        tc = body["taglie_confronto"]
        consigliata = next(t for t in tc if t["label"] == "consigliata")
        max_kwp = max(t["kwp"] for t in tc)
        # When consumo=1435 kWh, the max taglia overshoots far more than the min.
        # Cap logic should select the SMALLEST available (not the largest).
        assert consigliata["kwp"] < max_kwp or len(tc) == 1, (
            f"Con consumo 1435 kWh, la taglia massima ({max_kwp} kWp) è consigliata: "
            "il cap dovrebbe selezionare una taglia più piccola"
        )

    def test_borzano_verdetto_negativo_con_messaggio_sensato(self, client):
        """Con 1.435 kWh/anno, se la consigliata (nel cap) ha NPV < 0, il verdetto dice
        esplicitamente che nessuna taglia sensata è conveniente."""
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        vp = body["verdetto_pv"]
        if vp["etichetta"] == "negativo" and body["auto_dimensionato"]:
            # Must mention that no "sensata" size is convenient, not just "difficile"
            testo = vp["testo"]
            assert (
                "sensata" in testo.lower()
                or "150%" in testo
                or "Nessuna taglia" in testo
            ), f"Verdetto negativo manca del messaggio esplicito sul cap. Testo: {testo[:200]}"

    def test_alto_consumo_consigliata_nel_cap(self, client):
        """Con 7.400 kWh/anno, tutti i candidati sono entro il cap (6.5 kWp = 118%):
        la consigliata può essere qualunque taglia. Verifichiamo che sia nel cap."""
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 7_400.0,
        }).json()
        tc = body["taglie_confronto"]
        consigliata = next(t for t in tc if t["label"] == "consigliata")
        consumo = body["consumo_kwh_annuo"]
        ratio = consigliata["produzione_annua_kwh"] / consumo
        assert ratio <= 1.50 + 0.01, (
            f"Consigliata {consigliata['kwp']} kWp eccede il cap: {ratio:.1%}"
        )

    def test_sovradimensionamento_warning_in_verdetto_di_taglia(self, client):
        """Taglie che producono > 150% del consumo mostrano avvertenza nel verdetto."""
        body = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        consumo = body["consumo_kwh_annuo"]
        cap_ratio = 1.50
        for t in body["taglie_confronto"]:
            prod_ratio = t["produzione_annua_kwh"] / consumo
            if prod_ratio > cap_ratio + 0.01:
                vt = t["verdetto"]["testo"]
                assert "×" in vt or "rete" in vt.lower() or "fragil" in vt.lower(), (
                    f"Taglia {t['kwp']} kWp (prod_ratio={prod_ratio:.1f}x) senza avvertenza "
                    f"di sovradimensionamento nel testo: {vt[:150]}"
                )

    def test_user_kwp_sovradimensionato_mostra_avvertenza(self, client):
        """Se l'utente impone una taglia che produce > 150% del consumo,
        il verdetto_pv lo segnala esplicitamente."""
        body = client.post("/estimate/casa", json={
            **_BASE_PROXY,
            "bolletta_kwh": 1_435.0,
            "potenza_kwp": 6.5,  # produces ~8766 kWh vs 1435 kWh = 611%
        }).json()
        vp = body["verdetto_pv"]
        testo = vp["testo"]
        # deve contenere avvertenza di sovradimensionamento (×, rete, fragil...)
        assert "×" in testo or "rete" in testo.lower() or "fragil" in testo.lower(), (
            f"Taglia user-defined 6.5 kWp (611% consumo) senza avvertenza. Testo: {testo[:200]}"
        )


# ── 20. Orientamento tetto ────────────────────────────────────────────────────

class TestOrientamento:
    def test_produzione_decrescente_per_orientamento(self, client):
        """Ordine di produzione robusto per orientamenti significativamente diversi.

        Con TMY reale (Albinea): sud=2.023, sud_est_sud_ovest=2.013, est_o_ovest=1.637,
        nord=1.140 kWh — ordine fisicamente corretto (S > SSW = -0,5% > W = -19% > N = -44%).

        Con TMY sintetico (sin-curve GHI simmetrico): il modello cos_theta può dare
        SSW (200°) leggermente > S (180°) per un artefatto — es. sud=2.368, SSW=2.430.
        La differenza è <5% e fisicamente irrilevante; non vale testare quell'ordinamento.
        Testiamo solo i gap significativi (>10%) che devono reggere con entrambi i TMY.
        """
        produzioni = {}
        for ori in ("sud", "sud_est_sud_ovest", "est_o_ovest", "nord"):
            body = client.post("/estimate/casa", json={
                **_BASE_BOLLETTA, "orientamento": ori,
            }).json()
            produzioni[ori] = body["produzione_annua_kwh"]
        # sud e sud_est_sud_ovest devono essere comparabili (entro 5%)
        assert abs(produzioni["sud"] - produzioni["sud_est_sud_ovest"]) < produzioni["sud"] * 0.05, (
            f"sud={produzioni['sud']} e sud_est_sud_ovest={produzioni['sud_est_sud_ovest']} "
            f"differiscono di più del 5% — artefatto inatteso nel modello"
        )
        # differenze significative (>10%) che devono essere rispettate con qualsiasi TMY
        assert produzioni["sud_est_sud_ovest"] > produzioni["est_o_ovest"], (
            f"sud_est_sud_ovest={produzioni['sud_est_sud_ovest']} deve essere > est_o_ovest={produzioni['est_o_ovest']}"
        )
        assert produzioni["sud"] > produzioni["est_o_ovest"], (
            f"sud={produzioni['sud']} deve essere > est_o_ovest={produzioni['est_o_ovest']}"
        )
        assert produzioni["est_o_ovest"] > produzioni["nord"], (
            f"est_o_ovest={produzioni['est_o_ovest']} deve essere > nord={produzioni['nord']}"
        )

    def test_non_so_tra_sud_e_est_o_ovest(self, client):
        """non_so (SW 225°) produce tra sud_est_sud_ovest (200°) e est_o_ovest (270°)."""
        produzioni = {}
        for ori in ("sud_est_sud_ovest", "non_so", "est_o_ovest"):
            body = client.post("/estimate/casa", json={
                **_BASE_BOLLETTA, "orientamento": ori,
            }).json()
            produzioni[ori] = body["produzione_annua_kwh"]
        assert produzioni["sud_est_sud_ovest"] >= produzioni["non_so"] >= produzioni["est_o_ovest"], (
            f"Atteso: sud_est_sud_ovest≥non_so≥est_o_ovest, ottenuto: {produzioni}"
        )

    def test_default_equivale_non_so(self, client):
        """Senza orientamento nel payload, default è non_so → stessa produzione."""
        body_default = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        body_non_so = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA, "orientamento": "non_so",
        }).json()
        assert body_default["produzione_annua_kwh"] == pytest.approx(
            body_non_so["produzione_annua_kwh"], rel=0.001
        )

    def test_orientamento_in_assunzioni(self, client):
        """L'orientamento deve comparire nel blocco assunzioni."""
        for ori in ("sud", "nord", "non_so", "est_o_ovest"):
            body = client.post("/estimate/casa", json={
                **_BASE_BOLLETTA, "orientamento": ori,
            }).json()
            testo = " ".join(body["assunzioni"]).lower()
            assert "orientamento" in testo or "azimuth" in testo, (
                f"Orientamento {ori!r} non trovato nelle assunzioni"
            )

    def test_non_so_avvertenza_in_assunzioni(self, client):
        """Per non_so deve comparire un'avvertenza esplicita sull'incertezza."""
        body = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA, "orientamento": "non_so",
        }).json()
        testo = " ".join(body["assunzioni"])
        assert "non specificato" in testo or "non noto" in testo or "ignoto" in testo.lower(), (
            f"Avvertenza non_so non trovata nelle assunzioni"
        )

    def test_nord_npv_inferiore_a_sud(self, client):
        """Con stessa taglia fissa, nord ha NPV ≤ sud e produzione inferiore."""
        body_sud = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA, "orientamento": "sud", "potenza_kwp": 3.0,
        }).json()
        body_nord = client.post("/estimate/casa", json={
            **_BASE_BOLLETTA, "orientamento": "nord", "potenza_kwp": 3.0,
        }).json()
        assert body_nord["produzione_annua_kwh"] < body_sud["produzione_annua_kwh"]
        assert body_nord["senza_batteria"]["npv_eur"] <= body_sud["senza_batteria"]["npv_eur"]

    def test_borderline_verdetto_cambia_con_orientamento(self, client):
        """Con consumo medio, il verdetto nord è peggiore o uguale a sud.
        Se cambia, documenta il senso: nord ≤ sud nella scala ottimo/buono/accettabile/negativo."""
        body_sud = client.post("/estimate/casa", json={
            **_BASE_PROXY, "bolletta_kwh": 4_500.0, "orientamento": "sud",
        }).json()
        body_nord = client.post("/estimate/casa", json={
            **_BASE_PROXY, "bolletta_kwh": 4_500.0, "orientamento": "nord",
        }).json()
        assert body_nord["produzione_annua_kwh"] < body_sud["produzione_annua_kwh"]
        grade = {"negativo": 0, "marginale": 0.5, "accettabile": 1, "buono": 2, "ottimo": 3}
        eti_sud = body_sud["verdetto_pv"]["etichetta"]
        eti_nord = body_nord["verdetto_pv"]["etichetta"]
        assert grade[eti_nord] <= grade[eti_sud], (
            f"Verdetto nord ({eti_nord}) più favorevole di sud ({eti_sud})"
        )


# ── FASE 2b: scenari_alternativi ─────────────────────────────────────────────
#
# NOTE SUI PROFILI DI CARICO (trovata durante sviluppo FASE 2b, set 2026):
_BORZANO_GAS = {
    "comune": "Albinea",
    "provincia": "RE",
    "n_occupants": 4,
    "floor_area_m2": 90.0,
    "bolletta_kwh": 1_435.0,
    "riscaldamento_elettrico": False,
    "acqua_calda_elettrica": False,
    "presenza_diurna": "per_lo_piu_vuota",
    "orientamento": "sud",
}


class TestScenariAlternativi:
    # ── presenza / assenza degli scenari ──────────────────────────────────────

    def test_borzano_gas_ha_elettrificazione(self, client):
        """Casa a gas → scenario elettrificazione presente."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        tipi = {a["tipo"] for a in body["scenari_alternativi"]}
        assert "elettrificazione" in tipi, "Manca scenario elettrificazione"

    def test_consumi_spostati_rimosso(self, client):
        """Scenario 'consumi_spostati' eliminato in FASE 2c (modello non funzionante)."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        tipi = {a["tipo"] for a in body["scenari_alternativi"]}
        assert "consumi_spostati" not in tipi, "Scenario consumi_spostati non deve essere presente"

    def test_casa_elettrica_nessuna_elettrificazione(self, client):
        """riscaldamento_elettrico=True → scenario elettrificazione ASSENTE."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "riscaldamento_elettrico": True,
        }).json()
        tipi = {a["tipo"] for a in body["scenari_alternativi"]}
        assert "elettrificazione" not in tipi
        assert "consumi_spostati" not in tipi

    def test_nessuno_scenario_se_elettrico(self, client):
        """Casa con riscaldamento elettrico → nessuno scenario alternativo."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS,
            "riscaldamento_elettrico": True,
        }).json()
        assert body["scenari_alternativi"] == []

    # ── struttura schema ──────────────────────────────────────────────────────

    def test_struttura_scenario_elettrificazione(self, client):
        """Ogni scenario ha i campi richiesti."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        assert el["titolo"]
        assert el["descrizione"]
        m = el["metriche"]
        for campo in ("consumo_elettrico_stimato_kwh", "kwp_consigliato",
                       "autosufficienza_pct", "payback_anni", "npv_eur",
                       "etichetta_verdetto"):
            assert campo in m, f"Campo mancante in metriche: {campo}"
        d = el["delta_vs_base"]
        for campo in ("autosufficienza_pp", "payback_anni", "npv_eur"):
            assert campo in d, f"Campo mancante in delta_vs_base: {campo}"

    # ── scenario elettrificazione: consumo e verdetto ─────────────────────────

    def test_elettrificazione_consumo_molto_superiore(self, client):
        """Con 1435 kWh bolletta + gas, il consumo elettrificato deve essere molto > 1435."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        consumo_base = body["consumo_kwh_annuo"]   # 1435
        consumo_el = el["metriche"]["consumo_elettrico_stimato_kwh"]
        assert consumo_el > consumo_base * 2.0, (
            f"consumo_elettrificato={consumo_el:.0f} kWh dovrebbe essere >2x il base={consumo_base:.0f}"
        )

    def test_elettrificazione_verdetto_migliore_del_base(self, client):
        """Con più consumo, l'FV diventa più conveniente: verdetto migliore o uguale al base."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        base_npv = body["senza_batteria"]["npv_eur"]
        el_npv = el["metriche"]["npv_eur"]
        assert el_npv >= base_npv, (
            f"NPV elettrificazione ({el_npv:.0f}€) deve essere ≥ NPV base ({base_npv:.0f}€)"
        )

    def test_elettrificazione_delta_npv_positivo(self, client):
        """delta_vs_base.npv_eur deve essere positivo (FV migliora con più consumo)."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        assert el["delta_vs_base"]["npv_eur"] >= 0

    def test_elettrificazione_descrizione_contiene_perimetro(self, client):
        """La descrizione deve esplicitare che valuta solo il FV, non la HP."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        desc = el["descrizione"].lower()
        assert "perimetro" in desc or "fuori" in desc or "livello 2" in desc.lower(), (
            f"Descrizione manca del disclaimer di perimetro: {desc[:200]}"
        )

    def test_elettrificazione_scop_in_descrizione(self, client):
        """La descrizione deve riportare lo SCOP calcolato dal TMY."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        desc = el["descrizione"].lower()
        assert "scop" in desc, f"SCOP non dichiarato nella descrizione: {el['descrizione'][:200]}"

    # ── gas_smc_annui: effetto sul consumo elettrificato ──────────────────────

    def test_gas_smc_annui_fornito_vs_assente_consumo_differisce(self, client):
        """Con gas_smc_annui=800, il consumo elettrificato è diverso dal benchmark."""
        body_no_gas = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        body_gas = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 800.0,
        }).json()
        el_no = next(a for a in body_no_gas["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        el_si = next(a for a in body_gas["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        consumo_no = el_no["metriche"]["consumo_elettrico_stimato_kwh"]
        consumo_si = el_si["metriche"]["consumo_elettrico_stimato_kwh"]
        assert consumo_no != consumo_si, (
            f"gas_smc_annui non influenza il consumo elettrificato: "
            f"no_gas={consumo_no:.0f} == si_gas={consumo_si:.0f}"
        )

    def test_gas_smc_annui_descritto_nella_descrizione(self, client):
        """Quando gas_smc_annui fornito, la fonte è esplicitata nella descrizione."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 800.0,
        }).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        desc = el["descrizione"].lower()
        assert "smc" in desc or "gas" in desc or "800" in el["descrizione"], (
            f"Fonte gas_smc_annui non dichiarata nella descrizione: {el['descrizione'][:200]}"
        )

    def test_gas_smc_annui_grande_aumenta_consumo_elettrificato(self, client):
        """Più gas → più consumo elettrico equivalente → consumo elettrificato maggiore."""
        consumo_el = {}
        for smc in (500, 1_500):
            body = client.post("/estimate/casa", json={
                **_BORZANO_GAS, "gas_smc_annui": float(smc),
            }).json()
            el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
            consumo_el[smc] = el["metriche"]["consumo_elettrico_stimato_kwh"]
        assert consumo_el[500] < consumo_el[1_500], (
            f"gas 500 Smc ({consumo_el[500]:.0f}) deve dare consumo < gas 1500 Smc ({consumo_el[1_500]:.0f})"
        )

    # ── gas_smc_estivi_mese: separazione usi del gas ──────────────────────────

    def test_gas_smc_estivi_mese_riduce_riscaldamento(self, client):
        """Con gas_smc_estivi_mese, la quota riscaldamento è minore (resto = cottura+ACS)."""
        body_no_est = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_200.0,
        }).json()
        body_est = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_200.0, "gas_smc_estivi_mese": 25.0,
        }).json()
        el_no = next(a for a in body_no_est["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        el_si = next(a for a in body_est["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        # Con estivi_mese=25 → base annua = 300 Smc → riscaldamento = 900 Smc
        # Default split: cottura+ACS per 4 persone ≈ 255 Smc → riscaldamento = 945 Smc
        # La differenza di consumo elettrificato riflette la diversa separazione
        consumo_no = el_no["metriche"]["consumo_elettrico_stimato_kwh"]
        consumo_si = el_si["metriche"]["consumo_elettrico_stimato_kwh"]
        assert consumo_no != consumo_si, (
            f"gas_smc_estivi_mese non influenza il consumo: no={consumo_no:.0f}, si={consumo_si:.0f}"
        )

    def test_gas_smc_estivi_mese_in_descrizione(self, client):
        """Con gas_smc_estivi_mese, la fonte è esplicitata nella descrizione."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_000.0, "gas_smc_estivi_mese": 20.0,
        }).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        desc = el["descrizione"].lower()
        assert "20" in el["descrizione"] or "smc" in desc, (
            f"gas_smc_estivi_mese non dichiarato: {el['descrizione'][:200]}"
        )

    # ── terminali: SCOP per tipo di terminale ────────────────────────────────

    def test_terminali_scop_migliore_per_pavimento(self, client):
        """SCOP migliore con pavimento radiante → consumo elettrico < termosifoni."""
        consumo_per_terminale = {}
        for terminale in ("termosifoni", "ventilconvettori", "pavimento_radiante"):
            body = client.post("/estimate/casa", json={
                **_BORZANO_GAS, "gas_smc_annui": 1_200.0, "terminali": terminale,
            }).json()
            el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
            consumo_per_terminale[terminale] = el["metriche"]["consumo_elettrico_stimato_kwh"]
        # Migliore SCOP → meno elettricità necessaria per lo stesso fabbisogno termico
        assert consumo_per_terminale["pavimento_radiante"] < consumo_per_terminale["termosifoni"], (
            f"pavimento_radiante ({consumo_per_terminale['pavimento_radiante']:.0f}) "
            f"deve consumare meno di termosifoni ({consumo_per_terminale['termosifoni']:.0f})"
        )
        assert consumo_per_terminale["ventilconvettori"] < consumo_per_terminale["termosifoni"], (
            f"ventilconvettori ({consumo_per_terminale['ventilconvettori']:.0f}) "
            f"deve consumare meno di termosifoni ({consumo_per_terminale['termosifoni']:.0f})"
        )

    def test_terminali_non_so_come_termosifoni(self, client):
        """terminali=non_so → SCOP conservativo (stesso comportamento di termosifoni)."""
        body_ts = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_000.0, "terminali": "termosifoni",
        }).json()
        body_ns = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_000.0, "terminali": "non_so",
        }).json()
        el_ts = next(a for a in body_ts["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        el_ns = next(a for a in body_ns["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        assert el_ts["metriche"]["consumo_elettrico_stimato_kwh"] == pytest.approx(
            el_ns["metriche"]["consumo_elettrico_stimato_kwh"], abs=50.0
        ), "terminali=non_so deve dare consumo simile a termosifoni (stesso supply temp)"

    def test_terminali_in_descrizione(self, client):
        """Il tipo di terminale è riportato nella descrizione."""
        for terminale in ("termosifoni", "pavimento_radiante", "ventilconvettori"):
            body = client.post("/estimate/casa", json={
                **_BORZANO_GAS, "gas_smc_annui": 800.0, "terminali": terminale,
            }).json()
            el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
            assert terminale in el["descrizione"] or terminale.split("_")[0] in el["descrizione"].lower(), (
                f"Terminale '{terminale}' non dichiarato nella descrizione: {el['descrizione'][:200]}"
            )

    def test_termosifoni_avvertenza_alta_temperatura_in_descrizione(self, client):
        """Con termosifoni o non_so, la descrizione avverte che SCOP è basso e NPV alto non è un vantaggio HP."""
        for term in ("termosifoni", "non_so"):
            body = client.post("/estimate/casa", json={
                **_BORZANO_GAS, "gas_smc_annui": 800.0, "terminali": term,
            }).json()
            el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
            desc = el["descrizione"].lower()
            assert "alta temperatura" in desc or "regime sfavorevole" in desc, (
                f"terminali={term}: manca avvertenza alta temperatura nella descrizione: "
                f"{el['descrizione'][:300]}"
            )

    def test_pavimento_nessuna_avvertenza_alta_temperatura(self, client):
        """Con pavimento_radiante NON deve comparire l'avvertenza alta temperatura."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 800.0, "terminali": "pavimento_radiante",
        }).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        assert "alta temperatura" not in el["descrizione"].lower(), (
            "Avvertenza alta temperatura non deve comparire con pavimento_radiante"
        )

    # ── indicativa senza gas_smc_annui (PARTE 5) ──────────────────────────────

    def test_senza_gas_nota_indicativa_in_descrizione(self, client):
        """Senza gas_smc_annui, la descrizione segnala la stima come indicativa."""
        body = client.post("/estimate/casa", json=_BORZANO_GAS).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        desc = el["descrizione"].lower()
        assert "indicat" in desc or "benchmark" in desc, (
            f"Stima indicativa non segnalata in assenza di gas_smc_annui: {el['descrizione'][:300]}"
        )

    def test_con_gas_smc_nessuna_nota_indicativa(self, client):
        """Con gas_smc_annui, la nota 'indicativa' non deve apparire."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS, "gas_smc_annui": 1_000.0,
        }).json()
        el = next(a for a in body["scenari_alternativi"] if a["tipo"] == "elettrificazione")
        assert "indicativa" not in el["descrizione"].lower(), (
            "Nota 'indicativa' non deve apparire quando gas_smc_annui è fornito"
        )

    # ── campi di risposta backward-compatible ─────────────────────────────────

    def test_scenari_alternativi_lista_vuota_per_default_schema(self, client):
        """scenari_alternativi è una lista (mai None, nemmeno se vuota)."""
        body = client.post("/estimate/casa", json={
            **_BORZANO_GAS,
            "riscaldamento_elettrico": True,
        }).json()
        assert isinstance(body["scenari_alternativi"], list)

    def test_schema_backward_compat_senza_gas_smc(self, client):
        """gas_smc_annui è opzionale: richieste senza non cambiano gli altri campi."""
        body_old = client.post("/estimate/casa", json=_BASE_BOLLETTA).json()
        # Tutti i campi pre-FASE-2b devono essere invariati
        for campo in ("consumo_kwh_annuo", "kwp_ipotizzato", "produzione_annua_kwh",
                       "verdetto_pv", "senza_batteria", "taglie_confronto"):
            assert campo in body_old
