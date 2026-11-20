"""Quote check ("verifica preventivi", Livello 2).

Checks the NUMBERS of an installer's quote — price for the size, promised
production, size versus consumption — never the technical quality of the
components (outside our competence and responsibility; CLAUDE.md decision).
No document is stored: the user types the figures (GDPR choice, 20 nov 2026).

Price band: residential cost curve in api/utils/cost.py (GSE/IEA survey,
calibrated 20 nov 2026) plus a battery band from the same research
(low 800 + 450 €/kWh, central 1.200 + 550, high 1.500 + 750; VAT included).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from api.utils.cost import estimate_pv_cost_residential, estimate_pv_cost_residential_range

BATTERY_BAND = {
    "low": (800.0, 450.0),
    "central": (1_200.0, 550.0),
    "high": (1_500.0, 750.0),
}

# Promised vs our production: above +15 % the card on the site already tells the
# user to ask which data the installer used; keep the same threshold here.
PROD_OPTIMISTIC_RATIO: float = 1.05
PROD_INFLATED_RATIO: float = 1.15
PROD_PRUDENT_RATIO: float = 0.90

# Same cap as the estimate: beyond 150 % of consumption the system lives on export.
OVERSIZE_RATIO: float = 1.50

VAT_RESIDENTIAL: float = 0.10

PriceVerdict = Literal["sotto_fascia", "fascia_bassa", "fascia_alta", "sopra_fascia"]
ProdVerdict = Literal["prudente", "coerente", "ottimistica", "gonfiata"]


def _battery_cost(kwh: float, scenario: str) -> float:
    if kwh <= 0:
        return 0.0
    fixed, per_kwh = BATTERY_BAND[scenario]
    return fixed + per_kwh * kwh


@dataclass(frozen=True)
class ExpectedCost:
    low: float
    central: float
    high: float


def expected_cost(kwp: float, battery_kwh: float = 0.0) -> ExpectedCost:
    """Market band for a turnkey system of this size (VAT included)."""
    pv_low, pv_high = estimate_pv_cost_residential_range(kwp)
    pv_central = estimate_pv_cost_residential(kwp)
    return ExpectedCost(
        low=pv_low + _battery_cost(battery_kwh, "low"),
        central=pv_central + _battery_cost(battery_kwh, "central"),
        high=pv_high + _battery_cost(battery_kwh, "high"),
    )


def price_verdict(price: float, exp: ExpectedCost) -> tuple[PriceVerdict, str]:
    if price < exp.low:
        return "sotto_fascia", (
            "Il prezzo è sotto la fascia di mercato per questa taglia. Può essere un buon affare, "
            "ma chiedi cosa include: componenti, pratiche, connessione alla rete, ponteggio, garanzie. "
            "Un prezzo molto basso a volte nasconde costi che arrivano dopo."
        )
    if price <= exp.central:
        return "fascia_bassa", "Il prezzo è nella fascia di mercato, nella parte più conveniente."
    if price <= exp.high:
        return "fascia_alta", (
            "Il prezzo è nella fascia di mercato, ma nella parte alta. Se i componenti sono standard "
            "c'è margine per chiedere uno sconto o un altro preventivo."
        )
    return "sopra_fascia", (
        "Il prezzo è sopra la fascia di mercato per questa taglia. Chiedi cosa giustifica la differenza "
        "(componenti premium, lavori extra sul tetto o sull'impianto elettrico, servizi inclusi) "
        "e confrontalo con almeno un altro preventivo."
    )


def production_verdict(promised: float, ours: float) -> tuple[ProdVerdict, float, str]:
    """Return (verdict, ratio promised/ours, message)."""
    ratio = promised / ours if ours > 0 else float("inf")
    if ratio > PROD_INFLATED_RATIO:
        return "gonfiata", ratio, (
            f"La produzione promessa è superiore del {100 * (ratio - 1):.0f}% alla nostra stima. "
            "Chiedi all'installatore su quali dati si basa: lo strumento pubblico di riferimento è "
            "PVGIS della Commissione Europea, lo stesso che usiamo noi."
        )
    if ratio > PROD_OPTIMISTIC_RATIO:
        return "ottimistica", ratio, (
            f"La produzione promessa è un po' sopra la nostra stima (+{100 * (ratio - 1):.0f}%). "
            "Può dipendere da ipotesi diverse sulle perdite: è un margine plausibile ma ottimistico."
        )
    if ratio >= PROD_PRUDENT_RATIO:
        return "coerente", ratio, "La produzione promessa è coerente con la nostra stima."
    return "prudente", ratio, (
        f"La produzione promessa è più bassa della nostra stima ({100 * (ratio - 1):.0f}%). "
        "Non è un problema: l'installatore è stato prudente, o ha visto ombre che noi non conosciamo."
    )


def price_with_vat(price: float, includes_vat: bool) -> float:
    return price if includes_vat else price * (1.0 + VAT_RESIDENTIAL)
