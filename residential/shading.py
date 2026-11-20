"""Near shading for the residential estimate (Livello 2).

Users cannot estimate a shading percentage, but they can say what they see.
The form asks three observable things; this module turns the answers into an
annual production loss with an uncertainty band.

Decision 20 nov 2026 (CLAUDE.md, "Livello 2 — decisioni prese"). Values are
OUR estimate, calibrated on the NREL/PVEL side-by-side test on residential
roofs: homes losing ~8 %, 19 % and 26 % of annual sun to shade lost ~6.5 %,
19 % and 26 % of energy with a string inverter, ~5 / 15 / 20 % with
optimisers or microinverters (25-35 % of the shading loss recovered).
Chimneys and antennas stay inside the default 3 % forfait (field study on
existing systems: median 0.31 %, max 2.67 % from neighbourhood and roof
details — arXiv 2609.23021).

The loss replaces SystemLosses.shading_near and is applied uniformly over the
year (simplification: an east obstacle really hits mornings only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

OmbraOstacoli = Literal["no", "si", "non_so"]
OmbraLato = Literal["sud", "est", "ovest"]
OmbraDurata = Literal["qualche_ora", "buona_parte"]

# Share of the shading loss recovered by optimisers / microinverters (NREL test: 25-35 %).
MLPE_RECOVERY_CENTRAL: float = 0.30

# Google sunshineQuantiles: relative gap between the 10th percentile and the
# median of annual sunshine hours on a used roof face. PROVISIONAL threshold
# (our choice, to calibrate on the first reviews): the minimum pixel is always
# an edge artefact (field survey 29 sep 2026), so the 10th percentile is used.
GOOGLE_SPREAD_ALERT: float = 0.30


@dataclass(frozen=True)
class ShadingClass:
    key: str                       # stable id for the API / report
    label: str                     # Italian, shown to the user
    loss_pct: float                # central annual loss [%]
    loss_low_pct: float
    loss_high_pct: float
    consiglia_ottimizzatori: bool  # suggest MLPE in the report
    da_verificare: bool            # flag the request for Sirot's manual review


NESSUNA = ShadingClass("nessuna", "Nessun ostacolo più alto del tetto nelle vicinanze",
                       3.0, 2.0, 4.0, False, False)
LATERALE = ShadingClass("est_ovest_qualche_ora", "Ostacolo a est o a ovest, ombra per qualche ora",
                        6.0, 4.0, 10.0, True, False)
SUD = ShadingClass("sud_qualche_ora", "Ostacolo a sud, ombra per qualche ora",
                   12.0, 8.0, 19.0, True, False)
FORTE = ShadingClass("buona_parte_del_giorno", "Ostacolo che fa ombra per buona parte del giorno",
                     20.0, 15.0, 26.0, True, True)
NON_SO = ShadingClass("non_so", "Non sappiamo se ci sono ostacoli",
                      6.0, 3.0, 15.0, False, True)


def classify(
    ostacoli: OmbraOstacoli,
    lati: Sequence[OmbraLato] = (),
    durata: Optional[OmbraDurata] = None,
) -> ShadingClass:
    """Map the three form answers to a shading class."""
    if ostacoli == "no":
        return NESSUNA
    if ostacoli == "non_so":
        return NON_SO
    if not lati or durata is None:
        raise ValueError("con ostacoli='si' servono il lato e la durata dell'ombra")
    if durata == "buona_parte":
        return FORTE
    return SUD if "sud" in lati else LATERALE


def loss_with_mlpe_pct(cls: ShadingClass) -> float:
    """Central loss if optimisers/microinverters are installed [%].

    Only the part above the 3 % forfait is shading the MLPE can partly recover.
    """
    extra = max(0.0, cls.loss_pct - NESSUNA.loss_pct)
    return NESSUNA.loss_pct + extra * (1.0 - MLPE_RECOVERY_CENTRAL)


def google_spread(sunshine_quantiles: Sequence[float]) -> Optional[float]:
    """(median - p10) / median of annual sunshine hours, or None if unusable."""
    if len(sunshine_quantiles) != 11:
        return None
    p10, median = float(sunshine_quantiles[1]), float(sunshine_quantiles[5])
    if median <= 0:
        return None
    return (median - p10) / median


def google_alert(sunshine_quantiles: Sequence[float]) -> bool:
    """True when a roof face looks probably shaded (detector only, never a correction)."""
    spread = google_spread(sunshine_quantiles)
    return spread is not None and spread > GOOGLE_SPREAD_ALERT
