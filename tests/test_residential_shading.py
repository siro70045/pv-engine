"""Near-shading classes (decision 20 nov 2026, CLAUDE.md "Livello 2")."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from residential.shading import (  # noqa: E402
    FORTE, LATERALE, NESSUNA, NON_SO, SUD,
    classify, google_alert, google_spread, loss_with_mlpe_pct,
)


def test_nessun_ostacolo_e_il_forfait_storico():
    assert classify("no") is NESSUNA and NESSUNA.loss_pct == 3.0


def test_non_so_va_verificato():
    c = classify("non_so")
    assert c is NON_SO and c.da_verificare and c.loss_low_pct < c.loss_pct < c.loss_high_pct


@pytest.mark.parametrize("lati,attesa", [
    (["est"], LATERALE), (["ovest"], LATERALE), (["est", "ovest"], LATERALE),
    (["sud"], SUD), (["est", "sud"], SUD),
])
def test_qualche_ora_dipende_dal_lato(lati, attesa):
    assert classify("si", lati, "qualche_ora") is attesa


def test_buona_parte_del_giorno_vince_sul_lato():
    assert classify("si", ["est"], "buona_parte") is FORTE


def test_si_senza_dettagli_e_un_errore():
    with pytest.raises(ValueError):
        classify("si", [], None)


def test_classi_ordinate_per_gravita():
    assert NESSUNA.loss_pct < LATERALE.loss_pct < SUD.loss_pct < FORTE.loss_pct
    for c in (NESSUNA, LATERALE, SUD, FORTE, NON_SO):
        assert c.loss_low_pct <= c.loss_pct <= c.loss_high_pct


def test_ottimizzatori_recuperano_solo_la_parte_d_ombra():
    # 12 % → 3 % forfait + 9 % shade × 0.70 = 9.3 %
    assert loss_with_mlpe_pct(SUD) == pytest.approx(9.3)
    assert loss_with_mlpe_pct(NESSUNA) == pytest.approx(3.0)


def test_google_spread_e_soglia():
    uniforme = [900, 1000, 1096, 1150, 1202, 1257, 1304, 1347, 1385, 1413, 1478]  # Borzano sud
    assert google_spread(uniforme) == pytest.approx((1257 - 1000) / 1257)
    assert not google_alert(uniforme)
    ombreggiato = [100, 500, 700, 800, 900, 1000, 1100, 1200, 1250, 1300, 1400]
    assert google_alert(ombreggiato)


def test_google_quantili_mancanti_nessun_avviso():
    assert google_spread([]) is None and not google_alert([])
