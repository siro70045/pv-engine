"""Tests for the full residential report (residential/report.py + report block
of POST /estimate/casa).

The sensitivity table is recomputed from the cash-flow table instead of
re-running the hourly simulation: these tests check that shortcut against full
re-runs of the engine, lever by lever.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from residential.battery_scenario import (
    DEFAULT_DISCOUNT_RATE,
    DEFAULT_EXPORT_PRICE_EUR_KWH,
    DEFAULT_IMPORT_PRICE_EUR_KWH,
    compare_battery_scenarios,
    run_scenario,
)
from residential.report import (
    CO2_KG_PER_KWH,
    co2_evitata,
    detrazione_annua,
    sensibilita,
    valuta_flussi,
)


def _profiles(kwp: float = 3.0, consumo_kwh: float = 3_000.0):
    pv, dem = [], []
    for d in range(365):
        season = 0.6 + 0.4 * math.sin(math.pi * d / 365)
        for h in range(24):
            sun = math.sin(math.pi * (h - 6) / 12) if 6 <= h <= 18 else 0.0
            pv.append(kwp * 1000.0 * 0.75 * sun * season)
            dem.append(1.0)
    k = consumo_kwh * 1000.0 / sum(dem)
    return pv, [x * k for x in dem]


PV, DEM = _profiles()
KWP = 3.0
CAPEX = 2_500.0 + 1_150.0 * KWP


def _run(**kw):
    args = dict(label="t", pv_hourly_Wh=PV, demand_hourly_Wh=DEM, pv_kwp=KWP, pv_capex_eur=CAPEX)
    args.update(kw)
    return run_scenario(**args)


BASE = _run()


class TestValutaFlussi:
    def test_base_riproduce_motore(self):
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur, rate=DEFAULT_DISCOUNT_RATE)
        assert v.npv_eur == pytest.approx(BASE.npv_eur, abs=2)
        assert v.payback_anni == pytest.approx(min(BASE.payback_anni, 99.0), abs=0.05)

    @pytest.mark.parametrize("prezzo", [0.04, 0.08])
    def test_prezzo_cessione_come_rerun(self, prezzo):
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur,
                          fattore_cessione=prezzo / DEFAULT_EXPORT_PRICE_EUR_KWH)
        full = _run(export_price_eur_kwh=prezzo)
        assert v.npv_eur == pytest.approx(full.npv_eur, abs=3)
        assert v.payback_anni == pytest.approx(min(full.payback_anni, 99.0), abs=0.05)

    @pytest.mark.parametrize("k", [0.8, 1.2])
    def test_prezzo_acquisto_come_rerun(self, k):
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur, fattore_acquisto=k)
        full = _run(import_price_eur_kwh=DEFAULT_IMPORT_PRICE_EUR_KWH * k)
        assert v.npv_eur == pytest.approx(full.npv_eur, abs=3)

    def test_detrazione_36_come_rerun(self):
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur,
                          detrazione_annua_eur=detrazione_annua(CAPEX, False, None))
        full = _run(abitazione_principale=False)
        assert v.npv_eur == pytest.approx(full.npv_eur, abs=3)

    def test_costo_basso_come_rerun(self):
        low = CAPEX * 0.8
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur, capex_nuovo_eur=low,
                          detrazione_annua_eur=detrazione_annua(low, True, None))
        full = _run(pv_capex_eur=low)
        assert v.npv_eur == pytest.approx(full.npv_eur, abs=3)
        assert v.payback_anni == pytest.approx(min(full.payback_anni, 99.0), abs=0.05)

    def test_capienza_irpef_rispettata(self):
        # 200 €/year of IRPEF caps the 50 % instalment (CAPEX/2/10 > 200)
        assert detrazione_annua(CAPEX, True, 200.0) == pytest.approx(200.0, abs=1)

    def test_mai_recuperato_99(self):
        v = valuta_flussi(BASE.economics.cash_flows, BASE.capex_eur, capex_nuovo_eur=1e6,
                          detrazione_annua_eur=0.0)
        assert v.payback_anni == 99.0


class TestSensibilita:
    voci = sensibilita(
        BASE.economics.cash_flows, BASE.capex_eur,
        rate_base=DEFAULT_DISCOUNT_RATE, prezzo_cessione_base=DEFAULT_EXPORT_PRICE_EUR_KWH,
        abitazione_principale=True, irpef_annua_stimata=None,
        capex_banda=(CAPEX * 0.85, CAPEX * 1.2),
    )

    def test_cinque_leve(self):
        assert {v.leva for v in self.voci} == {
            "tasso", "prezzo_cessione", "prezzo_acquisto", "detrazione", "costo"}

    def test_una_base_per_leva_uguale_alla_stima(self):
        for leva in {v.leva for v in self.voci}:
            basi = [v for v in self.voci if v.leva == leva and v.base]
            assert len(basi) == 1, leva
            assert basi[0].npv_eur == pytest.approx(BASE.npv_eur, abs=2), leva

    def test_monotonia(self):
        by = lambda leva: [v.npv_eur for v in self.voci if v.leva == leva]
        t3, t4, t5 = by("tasso")
        assert t3 > t4 > t5
        c4, c6, c8 = by("prezzo_cessione")
        assert c4 < c6 < c8
        a_lo, a_0, a_hi = by("prezzo_acquisto")
        assert a_lo < a_0 < a_hi
        d50, d36, d0 = by("detrazione")
        assert d50 > d36 > d0
        lo, mid, hi = by("costo")
        assert lo > mid > hi


class TestCo2:
    def test_tutta_la_produzione(self):
        kg1, t = co2_evitata(BASE.economics.cash_flows)
        assert kg1 == pytest.approx(BASE.economics.cash_flows[0].pv_production_kWh * CO2_KG_PER_KWH, abs=1)
        assert t == pytest.approx(
            sum(cf.pv_production_kWh for cf in BASE.economics.cash_flows) * CO2_KG_PER_KWH / 1000, abs=0.1)

    def test_fattore_ispra(self):
        assert CO2_KG_PER_KWH == pytest.approx(0.1926)


class TestRunScenario:
    def test_compare_invariato(self):
        """compare_battery_scenarios now delegates to run_scenario: same numbers."""
        cmp = compare_battery_scenarios(PV, DEM, KWP, CAPEX / KWP, battery_kwh=5.0)
        assert cmp.senza_batteria.npv_eur == pytest.approx(BASE.npv_eur, abs=0.01)
        con = _run(battery_kwh=5.0, battery_capex=1_200.0 + 550.0 * 5.0)
        assert cmp.con_batteria.npv_eur == pytest.approx(con.npv_eur, abs=0.01)
        assert cmp.con_batteria.battery_replacement_years == con.battery_replacement_years

    def test_prezzo_preventivo_batteria_inclusa(self):
        """A quote with battery: the whole price as capex, battery still simulated."""
        prezzo = CAPEX + 4_000.0
        q = _run(pv_capex_eur=prezzo, battery_kwh=5.0, battery_capex=0.0)
        assert q.capex_eur == pytest.approx(prezzo, abs=1)
        assert q.autosufficienza_pct > BASE.autosufficienza_pct
        assert q.battery_replacement_years      # replacement still costed
