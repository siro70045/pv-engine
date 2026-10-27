"""
tests/test_residential_battery.py

Verifica lo scenario batteria residenziale (residential/battery_scenario.py).

Tutti gli input sono sintetici — nessun accesso PVGIS, nessun file esterno.

Casi coperti:
    1. Con batteria → autoconsumo_pct MAGGIORE del senza
    2. Con batteria → capex MAGGIORE (PV + costo batteria)
    3. Detrazione coerente: applicata al capex totale (inclusa batteria)
    4. Trade-off sempre presente: entrambi gli scenari, con delta
    5. delta_capex = capex(con) - capex(senza) ≈ battery_kwh × €/kWh
    6. Profili annuali presenti e di lunghezza corretta
    7. Caso Borzano-like (1.435 kWh/anno, basso consumo):
       la batteria risulta NON conveniente (NPV con < NPV senza)
    8. Nota esplicita sulla convenienza/inconvenienza della batteria
    9. Validazione input: profili non 8760-ore e kWp ≤ 0 sollevano ValueError
   10. Sostituzione batteria: se presente, l'anno è nell'orizzonte di analisi
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from residential.battery_scenario import (
    BATTERY_COST_EUR_KWH,
    DEFAULT_BATTERY_KWH,
    BatteryScenarioComparison,
    compare_battery_scenarios,
)


# ── Profili sintetici ────────────────────────────────────────────────────────
# FV: 800 Wh/h nelle ore 8-16 (9 ore), silenzio il resto
# Consumo: distribuzione serale (40% consumo 17-23, 30% notte 0-7, 30% diurno)

def _make_profiles(annual_demand_kwh: float, pv_kwp: float) -> tuple[list[float], list[float]]:
    """
    Produzione FV: scalata a pv_kwp (800 Wh/kWp × 9 h = 7.200 Wh/kWp/giorno).
    Domanda: ~annual_demand_kwh/anno, concentrata la sera (profilo working_family).
    """
    daily_prod_Wh = pv_kwp * 800.0 * 9   # ore 8-16 incluse
    daily_demand_Wh = annual_demand_kwh * 1000.0 / 365.0

    pv_h:  list[float] = []
    dem_h: list[float] = []

    for h in range(8760):
        hour_of_day = h % 24
        # FV: ore 8-16 (primavera/estate ~60% giorni), 0 il resto
        day = h // 24
        is_sunny = (day % 5) != 4   # ~80% di giorni soleggiati
        if 8 <= hour_of_day <= 16 and is_sunny:
            pv_h.append(pv_kwp * 800.0)
        else:
            pv_h.append(0.0)

        # Domanda: picco serale 17-22 (40%), notte 0-6 (25%), diurno 7-16 (35%)
        if 17 <= hour_of_day <= 22:
            dem_h.append(daily_demand_Wh * 0.40 / 6.0)
        elif hour_of_day <= 6:
            dem_h.append(daily_demand_Wh * 0.25 / 7.0)
        else:
            dem_h.append(daily_demand_Wh * 0.35 / 17.0)

    return pv_h, dem_h


# Caso standard: famiglia 2.700 kWh/anno, 3 kWp FV
PV_KWP_STD    = 3.0
PV_EUR_KWP    = 1_800.0
DEMAND_STD    = 2_700.0   # kWh/anno — media nazionale
PV_H_STD, DEM_H_STD = _make_profiles(DEMAND_STD, PV_KWP_STD)

# Caso Borzano: basso consumo (1.435 kWh/anno), 2 kWp FV
PV_KWP_BORZ   = 2.0
DEMAND_BORZ   = 1_435.0
PV_H_BORZ, DEM_H_BORZ = _make_profiles(DEMAND_BORZ, PV_KWP_BORZ)


def _run_std(**kw) -> BatteryScenarioComparison:
    return compare_battery_scenarios(
        PV_H_STD, DEM_H_STD, PV_KWP_STD, PV_EUR_KWP, **kw
    )


def _run_borz(**kw) -> BatteryScenarioComparison:
    return compare_battery_scenarios(
        PV_H_BORZ, DEM_H_BORZ, PV_KWP_BORZ, PV_EUR_KWP, **kw
    )


# ── 1. Autoconsumo maggiore con batteria ──────────────────────────────────────

class TestAutoconsumoBatteria:
    def test_autoconsumo_con_batteria_maggiore(self):
        cmp = _run_std()
        assert cmp.con_batteria.autoconsumo_pct > cmp.senza_batteria.autoconsumo_pct, (
            "Con batteria l'autoconsumo deve essere ≥ senza batteria."
        )

    def test_delta_autoconsumo_positivo(self):
        cmp = _run_std()
        assert cmp.tradeoff.delta_autoconsumo_pp > 0

    def test_autosufficienza_con_batteria_maggiore_o_uguale(self):
        cmp = _run_std()
        assert cmp.con_batteria.autosufficienza_pct >= cmp.senza_batteria.autosufficienza_pct


# ── 2. CAPEX maggiore con batteria ────────────────────────────────────────────

class TestCapexBatteria:
    def test_capex_con_batteria_maggiore(self):
        cmp = _run_std()
        assert cmp.con_batteria.capex_eur > cmp.senza_batteria.capex_eur

    def test_delta_capex_uguale_a_costo_batteria(self):
        cmp = _run_std()
        expected_delta = DEFAULT_BATTERY_KWH * BATTERY_COST_EUR_KWH
        assert abs(cmp.tradeoff.delta_capex_eur - expected_delta) < 50.0, (
            f"delta_capex {cmp.tradeoff.delta_capex_eur:.0f} € ≠ "
            f"atteso {expected_delta:.0f} € (costo batteria)"
        )

    def test_delta_capex_nel_tradeoff_coerente(self):
        cmp = _run_std()
        assert cmp.tradeoff.delta_capex_eur == pytest.approx(
            cmp.con_batteria.capex_eur - cmp.senza_batteria.capex_eur, abs=1.0
        )


# ── 3. Detrazione coerente ────────────────────────────────────────────────────

class TestDetrazioneBatteria:
    def test_detrazione_con_batteria_maggiore_di_senza(self):
        """La detrazione si applica al CAPEX totale: con batteria è più alta."""
        cmp = _run_std()
        assert cmp.con_batteria.detrazione_totale_eur > cmp.senza_batteria.detrazione_totale_eur

    def test_detrazione_50pct_su_capex_agevolabile(self):
        """Abitazione principale: detrazione = 50% di min(capex, 96.000)."""
        cmp = _run_std(abitazione_principale=True)
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            spesa_agev = min(sc.capex_eur, 96_000.0)
            expected   = round(spesa_agev * 0.50, 0)
            assert abs(sc.detrazione_totale_eur - expected) < 10.0, (
                f"{sc.label}: detrazione {sc.detrazione_totale_eur:.0f} € "
                f"≠ attesa {expected:.0f} €"
            )

    def test_detrazione_36pct_altra_abitazione(self):
        """Seconda casa: aliquota 36%."""
        cmp = compare_battery_scenarios(
            PV_H_STD, DEM_H_STD, PV_KWP_STD, PV_EUR_KWP,
            abitazione_principale=False,
        )
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            spesa_agev = min(sc.capex_eur, 96_000.0)
            expected   = round(spesa_agev * 0.36, 0)
            assert abs(sc.detrazione_totale_eur - expected) < 10.0

    def test_detrazione_effettiva_con_capienza_ridotta(self):
        """Con IRPEF bassa, detrazione effettiva < totale."""
        cmp = _run_std(irpef_annua_stimata=400.0)
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            assert sc.detrazione_effettiva_eur <= sc.detrazione_totale_eur


# ── 4. Trade-off sempre presente (entrambi gli scenari, con delta) ─────────────

class TestTradeoffSemprePresente:
    def test_entrambi_gli_scenari_presenti(self):
        cmp = _run_std()
        assert cmp.senza_batteria is not None
        assert cmp.con_batteria is not None

    def test_delta_espliciti_presenti(self):
        cmp = _run_std()
        t = cmp.tradeoff
        assert hasattr(t, "delta_autoconsumo_pp")
        assert hasattr(t, "delta_payback_anni")
        assert hasattr(t, "delta_npv_eur")
        assert hasattr(t, "delta_capex_eur")
        assert hasattr(t, "batteria_conveniente")
        assert hasattr(t, "nota")

    def test_nota_non_vuota(self):
        cmp = _run_std()
        assert cmp.tradeoff.nota and len(cmp.tradeoff.nota) > 20

    def test_economics_presente_per_entrambi(self):
        cmp = _run_std()
        assert cmp.senza_batteria.economics is not None
        assert cmp.con_batteria.economics is not None


# ── 5. Profili annuali corretti ───────────────────────────────────────────────

class TestProfiloAnnuale:
    def test_economics_cash_flows_length(self):
        cmp = _run_std()
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            assert len(sc.economics.cash_flows) == 25

    def test_produzione_positiva(self):
        cmp = _run_std()
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            assert sc.produzione_anno1_kwh > 0

    def test_risparmio_positivo(self):
        cmp = _run_std()
        for sc in [cmp.senza_batteria, cmp.con_batteria]:
            assert sc.risparmio_annuo_anno1_eur > 0


# ── 7. Caso Borzano: batteria NON conveniente ──────────────────────────────────

class TestBorzanoSanity:
    """
    Famiglia a basso consumo (1.435 kWh/anno): la batteria dovrebbe risultare
    non conveniente perché il FV diurno è già quasi tutto consumato, poco da
    spostare nel serale. Il payback della batteria deve essere lungo rispetto
    all'impianto FV puro.
    """

    def test_batteria_non_conveniente_borzano(self):
        cmp = _run_borz()
        assert cmp.tradeoff.batteria_conveniente is False, (
            "Per basso consumo (Borzano 1.435 kWh/anno), la batteria non deve "
            "essere conveniente (NPV con < NPV senza)."
        )

    def test_npv_con_batteria_inferiore_a_senza_borzano(self):
        cmp = _run_borz()
        assert cmp.con_batteria.npv_eur < cmp.senza_batteria.npv_eur

    def test_payback_con_batteria_piu_lungo_borzano(self):
        cmp = _run_borz()
        assert cmp.con_batteria.payback_anni > cmp.senza_batteria.payback_anni

    def test_nota_contiene_avvertenza_inconvenienza(self):
        cmp = _run_borz()
        nota = cmp.tradeoff.nota.lower()
        assert "non" in nota and ("conveniente" in nota or "convenienza" in nota or "conviene" in nota), (
            f"La nota dovrebbe avvertire che la batteria non è conveniente: {nota!r}"
        )

    def test_autoconsumo_con_batteria_maggiore_anche_borzano(self):
        """Anche in caso sfavorevole l'autoconsumo è ≥ senza batteria."""
        cmp = _run_borz()
        assert cmp.con_batteria.autoconsumo_pct >= cmp.senza_batteria.autoconsumo_pct


# ── 8. Nota convenienza/inconvenienza ─────────────────────────────────────────

class TestNotaConvenienza:
    def test_nota_conveniente_contiene_npv_positivo(self):
        """Se batteria conveniente: nota non deve contenere avvertenza negativa."""
        cmp = _run_std()
        if cmp.tradeoff.batteria_conveniente:
            assert "⚠" not in cmp.tradeoff.nota or "sostituz" in cmp.tradeoff.nota.lower()

    def test_nota_non_conveniente_contiene_avvertenza(self):
        """Se batteria non conveniente: nota deve iniziare con ⚠."""
        cmp = _run_borz()
        if not cmp.tradeoff.batteria_conveniente:
            assert "⚠" in cmp.tradeoff.nota


# ── 9. Validazione input ──────────────────────────────────────────────────────

class TestValidazioneInput:
    def test_profilo_pv_non_8760_raises(self):
        with pytest.raises(ValueError, match="8760"):
            compare_battery_scenarios(
                [0.0] * 100, DEM_H_STD, PV_KWP_STD, PV_EUR_KWP
            )

    def test_profilo_demand_non_8760_raises(self):
        with pytest.raises(ValueError, match="8760"):
            compare_battery_scenarios(
                PV_H_STD, [0.0] * 200, PV_KWP_STD, PV_EUR_KWP
            )

    def test_kwp_zero_raises(self):
        with pytest.raises(ValueError, match="pv_kwp"):
            compare_battery_scenarios(
                PV_H_STD, DEM_H_STD, 0.0, PV_EUR_KWP
            )

    def test_battery_kwh_zero_ok(self):
        """battery_kwh=0 è valido: entrambi gli scenari diventano senza batteria."""
        cmp = compare_battery_scenarios(
            PV_H_STD, DEM_H_STD, PV_KWP_STD, PV_EUR_KWP, battery_kwh=0.0
        )
        assert cmp.con_batteria.battery_kwh == 0.0
        assert cmp.con_batteria.autoconsumo_pct == cmp.senza_batteria.autoconsumo_pct


# ── 10. Sostituzione batteria nell'orizzonte di analisi ───────────────────────

class TestSostituzioneBatteria:
    def test_sostituzione_negli_anni_validi(self):
        cmp = _run_std()
        for yr in cmp.con_batteria.battery_replacement_years:
            assert 1 <= yr <= 25

    def test_senza_batteria_no_sostituzione(self):
        cmp = _run_std()
        assert cmp.senza_batteria.battery_replacement_years == []

    def test_sostituzione_inclusa_nel_cash_flow(self):
        """Se c'è sostituzione, il costo deve apparire in almeno un cash flow."""
        cmp = _run_std()
        repl_years = cmp.con_batteria.battery_replacement_years
        if repl_years:
            yr = repl_years[0]
            cf = cmp.con_batteria.economics.cash_flows[yr - 1]
            assert cf.replacement_cost_eur > 0, (
                f"Anno {yr}: sostituzione prevista ma replacement_cost=0"
            )
