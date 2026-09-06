"""
tests/test_residential_incentives.py

Verifica il modello di detrazione IRPEF residenziale (residential/incentives.py).

Casi coperti:
    1. Aliquota 50% vs 36% per abitazione_principale
    2. Tetto 96.000 €: capex 120.000 → spesa agevolabile 96.000
    3. Caso concreto capex 15.000, principale → detrazione 7.500, quota 750
    4. Capienza insufficiente: irpef 500/anno, quota 750 → perdita 250/anno
    5. irpef None → capienza piena + flag assunzione_capienza_piena
    6. Profilo annuale (quote_per_anno): lunghezza 10, valori coerenti
    7. Nota assunzioni: testo presente e non vuoto
    8. Validazione input: capex ≤ 0 e irpef < 0 sollevano ValueError
    9. Perdita fiscale = totale - effettiva (invariante)
   10. Nessuna perdita se IRPEF ≥ quota_annua
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from residential.incentives import (
    ALIQUOTA_ALTRI_CASI_PCT,
    ALIQUOTA_PRINCIPALE_PCT,
    QUOTE_ANNUALI,
    TETTO_SPESA_EUR,
    IrpefDeductionResult,
    calc_irpef_deduction,
)


# ── 1. Aliquota corretta per tipo immobile ────────────────────────────────────

class TestAliquota:
    def test_abitazione_principale_50_pct(self):
        r = calc_irpef_deduction(10_000.0, abitazione_principale=True)
        assert r.aliquota_pct == ALIQUOTA_PRINCIPALE_PCT == 50.0

    def test_altro_immobile_36_pct(self):
        r = calc_irpef_deduction(10_000.0, abitazione_principale=False)
        assert r.aliquota_pct == ALIQUOTA_ALTRI_CASI_PCT == 36.0

    def test_detrazione_principale_maggiore_di_secondaria(self):
        r1 = calc_irpef_deduction(20_000.0, abitazione_principale=True)
        r2 = calc_irpef_deduction(20_000.0, abitazione_principale=False)
        assert r1.detrazione_totale_eur > r2.detrazione_totale_eur


# ── 2. Tetto 96.000 € ─────────────────────────────────────────────────────────

class TestTettoSpesa:
    def test_capex_sopra_tetto_agevolabile_cappato(self):
        r = calc_irpef_deduction(120_000.0, abitazione_principale=True)
        assert r.spesa_agevolabile_eur == TETTO_SPESA_EUR == 96_000.0

    def test_detrazione_cappata_su_96k(self):
        r = calc_irpef_deduction(120_000.0, abitazione_principale=True)
        expected_totale = round(96_000.0 * 50.0 / 100.0, 2)
        assert r.detrazione_totale_eur == expected_totale == 48_000.0

    def test_capex_sotto_tetto_agevolabile_uguale_a_capex(self):
        r = calc_irpef_deduction(30_000.0, abitazione_principale=True)
        assert r.spesa_agevolabile_eur == 30_000.0

    def test_capex_esattamente_sul_tetto(self):
        r = calc_irpef_deduction(96_000.0, abitazione_principale=True)
        assert r.spesa_agevolabile_eur == 96_000.0
        assert r.detrazione_totale_eur == 48_000.0


# ── 3. Caso concreto capex 15.000, principale ────────────────────────────────

class TestCasoConcretoSmall:
    """capex 15.000, principale → detrazione 7.500, quota 750"""

    def setup_method(self):
        self.r = calc_irpef_deduction(15_000.0, abitazione_principale=True)

    def test_aliquota(self):
        assert self.r.aliquota_pct == 50.0

    def test_spesa_agevolabile(self):
        assert self.r.spesa_agevolabile_eur == 15_000.0

    def test_detrazione_totale(self):
        assert self.r.detrazione_totale_eur == 7_500.0

    def test_quota_annua(self):
        assert self.r.quota_annua_eur == 750.0

    def test_profilo_annuale_lunghezza(self):
        assert len(self.r.quote_per_anno) == QUOTE_ANNUALI == 10

    def test_capienza_piena_senza_irpef(self):
        assert self.r.assunzione_capienza_piena is True
        assert self.r.detrazione_effettiva_eur == 7_500.0
        assert self.r.perdita_fiscale_eur == 0.0


# ── 4. Capienza insufficiente: irpef 500, quota 750 ─────────────────────────

class TestCapienzaInsuffficiente:
    """irpef 500/anno < quota 750/anno → perdita 250/anno × 10 = 2.500"""

    def setup_method(self):
        self.r = calc_irpef_deduction(
            15_000.0, abitazione_principale=True, irpef_annua_stimata=500.0
        )

    def test_assunzione_capienza_piena_false(self):
        assert self.r.assunzione_capienza_piena is False

    def test_quota_annua_originale(self):
        # La quota normativa è 750 indipendentemente dalla capienza
        assert self.r.quota_annua_eur == 750.0

    def test_quota_effettiva_per_anno(self):
        # Ogni quota è cappata all'IRPEF: 500
        assert all(q == 500.0 for q in self.r.quote_per_anno)

    def test_detrazione_effettiva(self):
        assert self.r.detrazione_effettiva_eur == pytest.approx(5_000.0, abs=1.0)

    def test_perdita_fiscale(self):
        assert self.r.perdita_fiscale_eur == pytest.approx(2_500.0, abs=1.0)

    def test_effettiva_minore_di_totale(self):
        assert self.r.detrazione_effettiva_eur < self.r.detrazione_totale_eur


# ── 5. irpef None → capienza piena assunta ────────────────────────────────────

class TestCapienzaPienaAssunta:
    def test_flag_assunzione_piena(self):
        r = calc_irpef_deduction(20_000.0, irpef_annua_stimata=None)
        assert r.assunzione_capienza_piena is True

    def test_effettiva_uguale_a_totale(self):
        r = calc_irpef_deduction(20_000.0, irpef_annua_stimata=None)
        assert r.detrazione_effettiva_eur == r.detrazione_totale_eur

    def test_perdita_zero(self):
        r = calc_irpef_deduction(20_000.0, irpef_annua_stimata=None)
        assert r.perdita_fiscale_eur == 0.0

    def test_nota_contiene_avvertenza_capienza(self):
        r = calc_irpef_deduction(20_000.0, irpef_annua_stimata=None)
        assert "capienza" in r.nota_assunzioni.lower()


# ── 6. Profilo annuale (quote_per_anno) ───────────────────────────────────────

class TestProfiloAnnuale:
    def test_lunghezza_10(self):
        for capex in [8_000.0, 15_000.0, 96_000.0]:
            r = calc_irpef_deduction(capex)
            assert len(r.quote_per_anno) == 10, f"capex={capex}"

    def test_somma_profilo_uguale_a_effettiva(self):
        for irpef in [None, 300.0, 1_000.0, 5_000.0]:
            r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=irpef)
            assert sum(r.quote_per_anno) == pytest.approx(
                r.detrazione_effettiva_eur, abs=0.1
            ), f"irpef={irpef}"

    def test_valori_non_negativi(self):
        r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=100.0)
        assert all(q >= 0 for q in r.quote_per_anno)

    def test_capienza_piena_quote_uguali(self):
        r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=None)
        assert len(set(r.quote_per_anno)) == 1   # tutte uguali a quota_annua


# ── 7. Nota assunzioni ────────────────────────────────────────────────────────

class TestNotaAssunzioni:
    def test_nota_non_vuota(self):
        r = calc_irpef_deduction(15_000.0)
        assert r.nota_assunzioni and len(r.nota_assunzioni) > 20

    def test_nota_contiene_aliquota(self):
        r = calc_irpef_deduction(15_000.0, abitazione_principale=True)
        assert "50%" in r.nota_assunzioni

    def test_nota_seconda_casa_contiene_36(self):
        r = calc_irpef_deduction(15_000.0, abitazione_principale=False)
        assert "36%" in r.nota_assunzioni

    def test_nota_capienza_insuff_contiene_avvertenza(self):
        r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=200.0)
        assert "perdita" in r.nota_assunzioni.lower()

    def test_nota_capienza_ok_no_perdita(self):
        r = calc_irpef_deduction(5_000.0, irpef_annua_stimata=5_000.0)
        # Con IRPEF >> quota non c'è perdita — il testo non deve allertare inutilmente
        assert r.perdita_fiscale_eur == 0.0
        assert "nessuna perdita" in r.nota_assunzioni.lower()


# ── 8. Validazione input ──────────────────────────────────────────────────────

class TestInputValidation:
    def test_capex_zero_raises(self):
        with pytest.raises(ValueError, match="capex_eur"):
            calc_irpef_deduction(0.0)

    def test_capex_negativo_raises(self):
        with pytest.raises(ValueError, match="capex_eur"):
            calc_irpef_deduction(-1_000.0)

    def test_irpef_negativa_raises(self):
        with pytest.raises(ValueError, match="irpef_annua_stimata"):
            calc_irpef_deduction(10_000.0, irpef_annua_stimata=-1.0)

    def test_irpef_zero_ok(self):
        # IRPEF zero = nessun recupero possibile (es. pensionato minimo)
        r = calc_irpef_deduction(10_000.0, irpef_annua_stimata=0.0)
        assert r.detrazione_effettiva_eur == 0.0
        assert r.perdita_fiscale_eur == r.detrazione_totale_eur


# ── 9. Invariante: perdita = totale - effettiva ──────────────────────────────

class TestInvariantePerdita:
    @pytest.mark.parametrize("capex,irpef", [
        (8_000.0, None),
        (15_000.0, 750.0),
        (15_000.0, 200.0),
        (96_000.0, 5_000.0),
        (120_000.0, 10_000.0),  # capex sopra tetto
        (20_000.0, 0.0),
    ])
    def test_perdita_uguale_a_differenza(self, capex: float, irpef):
        r = calc_irpef_deduction(capex, irpef_annua_stimata=irpef)
        assert r.perdita_fiscale_eur == pytest.approx(
            r.detrazione_totale_eur - r.detrazione_effettiva_eur, abs=0.1
        )


# ── 10. Nessuna perdita se capienza sufficiente ───────────────────────────────

class TestNessunaPerdita:
    def test_irpef_pari_a_quota_nessuna_perdita(self):
        r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=750.0)
        assert r.perdita_fiscale_eur == 0.0
        assert r.detrazione_effettiva_eur == r.detrazione_totale_eur

    def test_irpef_sopra_quota_nessuna_perdita(self):
        r = calc_irpef_deduction(15_000.0, irpef_annua_stimata=5_000.0)
        assert r.perdita_fiscale_eur == 0.0

    def test_grande_irpef_detrazione_piena(self):
        r = calc_irpef_deduction(96_000.0, irpef_annua_stimata=100_000.0)
        assert r.perdita_fiscale_eur == 0.0
        assert r.detrazione_effettiva_eur == r.detrazione_totale_eur
