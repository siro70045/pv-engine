"""
tests/test_residential_defaults.py

Verifica il proxy di consumo residenziale (residential/estimate_defaults.py).

Casi coperti:
    1. Monotonia: più persone → più consumo; più m² → più consumo
    2. Additività: ogni carico speciale aumenta il totale
    3. Borzano gas-puro: centrale in [1.300, 2.200] kWh
    4. Tutto-elettrico: consumo molto superiore al caso gas-puro
    5. Banda coerente: basso < centrale < alto per ogni configurazione
    6. Breakdown sommato = centrale
    7. Validazione input: n_occupants < 1 e floor_area ≤ 0 sollevano ValueError
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from residential.estimate_defaults import (
    ConsumptionEstimate,
    build_residential_consumption_estimate as estimate,
)


# ── Helper ────────────────────────────────────────────────────────────────────

def _gas(n: int = 4, m2: float = 90.0, condizionatore: bool = False) -> ConsumptionEstimate:
    """Famiglia tutto-gas, senza EV né condizionatore (se non richiesto)."""
    return estimate(
        n_occupants=n,
        floor_area_m2=m2,
        riscaldamento_elettrico=False,
        acqua_calda_elettrica=False,
        ev=False,
        condizionatore=condizionatore,
    )


def _tutto_elettrico(n: int = 4, m2: float = 90.0) -> ConsumptionEstimate:
    """Famiglia con riscaldamento HP + ACS elettrica, niente EV né AC."""
    return estimate(
        n_occupants=n,
        floor_area_m2=m2,
        riscaldamento_elettrico=True,
        acqua_calda_elettrica=True,
        ev=False,
        condizionatore=False,
    )


# ── 1. Monotonia ──────────────────────────────────────────────────────────────

class TestMonotonicity:
    def test_more_people_more_consumption(self):
        """Aggiungere occupanti non può ridurre il consumo centrale."""
        prev = estimate(1, 90, False, False, False, False).consumo_annuo_kwh_centrale
        for n in range(2, 8):
            curr = estimate(n, 90, False, False, False, False).consumo_annuo_kwh_centrale
            assert curr >= prev, f"n={n}: consumo {curr} < n={n-1}: consumo {prev}"
            prev = curr

    def test_larger_area_more_consumption(self):
        """Superficie più grande non può ridurre il consumo centrale."""
        prev = _gas(4, 50.0).consumo_annuo_kwh_centrale
        for m2 in [70.0, 100.0, 130.0, 160.0, 200.0]:
            curr = _gas(4, m2).consumo_annuo_kwh_centrale
            assert curr >= prev, f"m2={m2}: consumo {curr} < m2 precedente: {prev}"
            prev = curr


# ── 2. Additività carichi speciali ────────────────────────────────────────────

class TestAdditivity:
    _BASE_PARAMS = dict(n_occupants=4, floor_area_m2=90.0,
                        riscaldamento_elettrico=False, acqua_calda_elettrica=False,
                        ev=False, condizionatore=False)

    def _base(self) -> float:
        return estimate(**self._BASE_PARAMS).consumo_annuo_kwh_centrale

    def test_condizionatore_increases_consumption(self):
        with_ac = estimate(**{**self._BASE_PARAMS, "condizionatore": True})
        assert with_ac.consumo_annuo_kwh_centrale > self._base()
        assert with_ac.breakdown.condizionatore_kwh > 0

    def test_ev_increases_consumption(self):
        with_ev = estimate(**{**self._BASE_PARAMS, "ev": True})
        assert with_ev.consumo_annuo_kwh_centrale > self._base()
        assert with_ev.breakdown.ev_kwh > 0

    def test_acqua_calda_elettrica_increases_consumption(self):
        with_dhw = estimate(**{**self._BASE_PARAMS, "acqua_calda_elettrica": True})
        assert with_dhw.consumo_annuo_kwh_centrale > self._base()
        assert with_dhw.breakdown.acqua_calda_elettrica_kwh > 0

    def test_riscaldamento_elettrico_increases_consumption(self):
        with_hp = estimate(**{**self._BASE_PARAMS, "riscaldamento_elettrico": True})
        assert with_hp.consumo_annuo_kwh_centrale > self._base()
        assert with_hp.breakdown.riscaldamento_elettrico_kwh > 0

    def test_all_loads_larger_than_each_alone(self):
        """Tutti i carichi insieme > ciascuno da solo."""
        all_loads = estimate(4, 90.0, True, True, True, True).consumo_annuo_kwh_centrale
        for kwargs in [
            {**self._BASE_PARAMS, "condizionatore": True},
            {**self._BASE_PARAMS, "ev": True},
            {**self._BASE_PARAMS, "acqua_calda_elettrica": True},
            {**self._BASE_PARAMS, "riscaldamento_elettrico": True},
        ]:
            assert all_loads > estimate(**kwargs).consumo_annuo_kwh_centrale


# ── 3. Caso Borzano (sanity check, non overfit) ───────────────────────────────

class TestBorzanoGasPuro:
    """
    4 persone, ~90 m², tutto gas (riscaldamento=False, ACS=False),
    condizionatore=True, EV=False. Consumo reale misurato: 1.435 kWh/anno.

    Il proxy non deve forzare il fit su un singolo caso; si verifica solo che
    la stima centrale rimanga nello stesso ordine di grandezza (1.300-2.200 kWh),
    plausibile per una famiglia italiana a basso consumo con gas predominante.
    """

    BORZANO_REAL = 1_435.0
    RANGE_LOW = 1_300.0
    RANGE_HIGH = 2_200.0

    def _estimate(self) -> ConsumptionEstimate:
        return estimate(4, 90.0, False, False, False, True)

    def test_centrale_in_expected_range(self):
        e = self._estimate()
        c = e.consumo_annuo_kwh_centrale
        assert self.RANGE_LOW <= c <= self.RANGE_HIGH, (
            f"Centrale {c:.0f} kWh fuori dal range atteso "
            f"[{self.RANGE_LOW}, {self.RANGE_HIGH}]. "
            f"Se cambi i parametri base, rileggere il docstring del modello."
        )

    def test_real_value_within_band(self):
        """Il valore misurato (1.435 kWh) deve cadere nella banda basso-alto."""
        e = self._estimate()
        assert e.consumo_annuo_kwh_basso <= self.BORZANO_REAL <= e.consumo_annuo_kwh_alto, (
            f"Borzano reale {self.BORZANO_REAL} kWh fuori dalla banda "
            f"[{e.consumo_annuo_kwh_basso}, {e.consumo_annuo_kwh_alto}]."
        )

    def test_breakdown_shows_zero_for_absent_loads(self):
        e = self._estimate()
        assert e.breakdown.riscaldamento_elettrico_kwh == 0.0
        assert e.breakdown.acqua_calda_elettrica_kwh == 0.0
        assert e.breakdown.ev_kwh == 0.0
        assert e.breakdown.condizionatore_kwh > 0.0


# ── 4. Caso tutto elettrico ───────────────────────────────────────────────────

class TestTuttoElettrico:
    """
    Stessa famiglia (4 persone, 90 m²) ma riscaldamento+ACS elettrici.
    Il consumo deve essere sostanzialmente più alto e rimanere plausibile
    per un'abitazione italiana con pompa di calore e boiler elettrico.
    """

    PLAUSIBLE_MIN = 4_000.0   # [kWh/anno] minimo credibile per HP + boiler elettrico
    PLAUSIBLE_MAX = 12_000.0  # [kWh/anno] massimo ragionevole (senza EV)

    def _estimate(self) -> ConsumptionEstimate:
        return _tutto_elettrico(4, 90.0)

    def test_much_higher_than_gas_only(self):
        gas = _gas(4, 90.0, condizionatore=False).consumo_annuo_kwh_centrale
        el  = self._estimate().consumo_annuo_kwh_centrale
        assert el > 2 * gas, (
            f"Tutto-elettrico ({el:.0f} kWh) deve essere > 2× gas-puro ({gas:.0f} kWh)."
        )

    def test_plausible_absolute_range(self):
        e = self._estimate()
        c = e.consumo_annuo_kwh_centrale
        assert self.PLAUSIBLE_MIN <= c <= self.PLAUSIBLE_MAX, (
            f"Centrale tutto-elettrico {c:.0f} kWh fuori dal range plausibile "
            f"[{self.PLAUSIBLE_MIN}, {self.PLAUSIBLE_MAX}]."
        )

    def test_heating_and_dhw_nonzero(self):
        e = self._estimate()
        assert e.breakdown.riscaldamento_elettrico_kwh > 0.0
        assert e.breakdown.acqua_calda_elettrica_kwh > 0.0


# ── 5. Banda di margine coerente ──────────────────────────────────────────────

class TestMarginCoherence:
    _CONFIGS = [
        dict(n_occupants=1, floor_area_m2=50,  riscaldamento_elettrico=False,
             acqua_calda_elettrica=False, ev=False, condizionatore=False),
        dict(n_occupants=4, floor_area_m2=90,  riscaldamento_elettrico=False,
             acqua_calda_elettrica=False, ev=False, condizionatore=True),
        dict(n_occupants=4, floor_area_m2=120, riscaldamento_elettrico=True,
             acqua_calda_elettrica=True,  ev=False, condizionatore=False),
        dict(n_occupants=2, floor_area_m2=70,  riscaldamento_elettrico=False,
             acqua_calda_elettrica=True,  ev=True, condizionatore=True),
        dict(n_occupants=6, floor_area_m2=160, riscaldamento_elettrico=True,
             acqua_calda_elettrica=True,  ev=True, condizionatore=True),
    ]

    @pytest.mark.parametrize("kwargs", _CONFIGS)
    def test_band_ordering(self, kwargs: dict):
        e = estimate(**kwargs)
        assert e.consumo_annuo_kwh_basso < e.consumo_annuo_kwh_centrale, (
            f"basso ({e.consumo_annuo_kwh_basso}) ≥ centrale ({e.consumo_annuo_kwh_centrale})"
        )
        assert e.consumo_annuo_kwh_centrale < e.consumo_annuo_kwh_alto, (
            f"centrale ({e.consumo_annuo_kwh_centrale}) ≥ alto ({e.consumo_annuo_kwh_alto})"
        )

    @pytest.mark.parametrize("kwargs", _CONFIGS)
    def test_wider_band_with_heating(self, kwargs: dict):
        """La banda con riscaldamento elettrico deve essere ≥ quella senza."""
        e_hp = estimate(**{**kwargs, "riscaldamento_elettrico": True})
        e_gas = estimate(**{**kwargs, "riscaldamento_elettrico": False})
        span_hp  = e_hp.consumo_annuo_kwh_alto  - e_hp.consumo_annuo_kwh_basso
        span_gas = e_gas.consumo_annuo_kwh_alto - e_gas.consumo_annuo_kwh_basso
        assert span_hp >= span_gas, (
            f"Banda con HP ({span_hp:.0f}) < banda senza HP ({span_gas:.0f}) — "
            "l'incertezza del riscaldamento elettrico dovrebbe allargarla."
        )


# ── 6. Breakdown → centrale ───────────────────────────────────────────────────

class TestBreakdownConsistency:
    def test_breakdown_sum_equals_central(self):
        """La somma del breakdown (arrotondata a 10) deve coincidere col centrale."""
        for n in [1, 2, 4, 6]:
            for hp in [False, True]:
                e = estimate(n, 100.0, hp, True, True, True)
                bd_sum = round(e.breakdown.totale_centrale / 10) * 10
                assert bd_sum == e.consumo_annuo_kwh_centrale, (
                    f"n={n}, hp={hp}: breakdown sum {bd_sum} ≠ centrale "
                    f"{e.consumo_annuo_kwh_centrale}"
                )


# ── 7. Validazione input ───────────────────────────────────────────────────────

class TestInputValidation:
    def test_zero_occupants_raises(self):
        with pytest.raises(ValueError, match="n_occupants"):
            estimate(0, 90.0, False, False, False, False)

    def test_negative_occupants_raises(self):
        with pytest.raises(ValueError, match="n_occupants"):
            estimate(-1, 90.0, False, False, False, False)

    def test_zero_area_raises(self):
        with pytest.raises(ValueError, match="floor_area_m2"):
            estimate(4, 0.0, False, False, False, False)

    def test_negative_area_raises(self):
        with pytest.raises(ValueError, match="floor_area_m2"):
            estimate(4, -10.0, False, False, False, False)
