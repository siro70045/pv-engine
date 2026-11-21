"""
residential/report.py

Full residential report (Livello 2, step 4): numbers the estimate already
computes, made visible, plus a one-lever-at-a-time sensitivity table.

Sensitivity is recomputed from the scenario's own annual cash-flow table, not by
re-running the hourly simulation. This is exact, not an approximation: the
energy balance (kWh self-consumed / exported per year) does not depend on prices,
and the engine's € flows are linear in them:

    saving_y  = self_consumed_kWh_y × p_import × (1+esc)^(y-1)
    export_y  = exported_kWh_y      × p_export × (1+esc_exp)^(y-1)
    tax_y     = detrazione_effettiva / 10          (years 1–10)
    net_y     = saving_y + export_y + tax_y − O&M_y − replacements_y

So changing the import price by k multiplies every saving_y by k, and so on.
O&M and the inverter replacement are per-kWp and do not move with the PV price.
tests/test_residential_report.py checks the base case against the engine and a
lever against a full re-run.

CO₂: counted on ALL production, self-consumed and exported (decision 21/11/2026):
an exported kWh also displaces a kWh produced by the grid's power stations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from residential.incentives import calc_irpef_deduction

# ISPRA (2026), "Settore elettrico: emissioni di CO2 e altri impatti", Rapporti
# 430/2026, tab. 1.7 — emission factor of Italian ELECTRICITY CONSUMPTION, 2024
# (final data): 192.6 g CO2/kWh. The 2025 preliminary value is 199.6; the gross
# production factor is 209.4 (2024). We take the consumption factor, the lowest
# of the three: prudent. The previous 0.233 (ENTSO-E mix 2023) is still used by
# the school flow and is out of scope here.
CO2_KG_PER_KWH: float = 0.1926
CO2_FONTE: str = (
    "ISPRA, Settore elettrico: emissioni di CO2 e altri impatti (Rapporti 430/2026), "
    "fattore dei consumi elettrici 2024: 192,6 g CO₂/kWh"
)

DETRAZIONE_ANNI: int = 10


@dataclass
class Valutazione:
    npv_eur: float
    payback_anni: float          # simple payback, capped at 99 (never recovered)


def valuta_flussi(
    flows: list,                 # list[engine.economics.AnnualCashFlow]
    capex_eur: float,
    *,
    rate: float = 0.05,
    fattore_acquisto: float = 1.0,
    fattore_cessione: float = 1.0,
    detrazione_annua_eur: Optional[float] = None,
    capex_nuovo_eur: Optional[float] = None,
) -> Valutazione:
    """NPV and simple payback of a cash-flow table with one or more levers moved.

    detrazione_annua_eur replaces the tax benefit of years 1–10 (None = unchanged).
    capex_nuovo_eur replaces the upfront cost (None = unchanged). When the cost
    moves, the caller must also pass the matching detrazione (it is a % of cost).
    Payback reproduces engine.economics: first year the cumulative turns ≥ 0,
    linearly interpolated inside that year.
    """
    capex = capex_eur if capex_nuovo_eur is None else capex_nuovo_eur
    cum = -capex
    npv = -capex
    payback = float("inf")
    for cf in flows:
        tax = cf.tax_benefit_eur
        if detrazione_annua_eur is not None:
            tax = detrazione_annua_eur if cf.year <= DETRAZIONE_ANNI else 0.0
        net = (
            cf.energy_saving_eur * fattore_acquisto
            + cf.export_revenue_eur * fattore_cessione
            + cf.cer_incentive_eur
            + tax
            - cf.om_cost_eur
            - cf.replacement_cost_eur
            - cf.loan_payment_eur
        )
        prev = cum
        cum += net
        npv += net / (1.0 + rate) ** cf.year
        if payback == float("inf") and cum >= 0:
            frac = -prev / net if net > 0 else 0.0
            payback = cf.year - 1 + max(0.0, min(frac, 1.0))
    return Valutazione(npv_eur=round(npv, 0), payback_anni=round(min(payback, 99.0), 1))


def detrazione_annua(
    capex_eur: float,
    abitazione_principale: bool,
    irpef_annua_stimata: Optional[float],
) -> float:
    """Effective yearly deduction instalment for a given cost [€/year]."""
    if capex_eur <= 0:
        return 0.0
    d = calc_irpef_deduction(
        capex_eur,
        abitazione_principale=abitazione_principale,
        irpef_annua_stimata=irpef_annua_stimata,
    )
    return d.detrazione_effettiva_eur / DETRAZIONE_ANNI


@dataclass
class VoceSensibilitaData:
    leva: str                    # tasso | prezzo_cessione | prezzo_acquisto | detrazione | costo
    variante: str                # human label, e.g. "0,04 €/kWh"
    base: bool                   # True = the value used in the estimate
    npv_eur: float
    payback_anni: float


def _eur(v: float, dec: int = 2) -> str:
    return f"{v:.{dec}f}".replace(".", ",")


def _int_it(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def sensibilita(
    flows: list,
    capex_eur: float,
    *,
    rate_base: float,
    prezzo_cessione_base: float,
    abitazione_principale: bool,
    irpef_annua_stimata: Optional[float],
    capex_banda: tuple[float, float],
) -> list[VoceSensibilitaData]:
    """The five levers agreed on 21/11/2026, one at a time, around the estimate."""
    out: list[VoceSensibilitaData] = []

    def add(leva: str, variante: str, base: bool, **kw) -> None:
        rate = kw.pop("rate", rate_base)
        v = valuta_flussi(flows, capex_eur, rate=rate, **kw)
        out.append(VoceSensibilitaData(leva, variante, base, v.npv_eur, v.payback_anni))

    # 1. Discount rate (payback does not depend on it)
    for t in (0.03, 0.04, 0.05):
        add("tasso", f"{_eur(t * 100, 0)}%", abs(t - rate_base) < 1e-9, rate=t)

    # 2. Export price (ritiro dedicato)
    for p in (0.04, 0.06, 0.08):
        add("prezzo_cessione", f"{_eur(p)} €/kWh", abs(p - prezzo_cessione_base) < 1e-9,
            fattore_cessione=p / prezzo_cessione_base)

    # 3. Purchase price ±20 %
    for k, lab in ((0.8, "−20%"), (1.0, "come oggi"), (1.2, "+20%")):
        add("prezzo_acquisto", lab, k == 1.0, fattore_acquisto=k)

    # 4. Deduction: 50 % / 36 % / none usable
    base_pct = 50 if abitazione_principale else 36
    for pct, ab_princ in ((50, True), (36, False)):
        add("detrazione", f"{pct}%", pct == base_pct,
            detrazione_annua_eur=detrazione_annua(capex_eur, ab_princ, irpef_annua_stimata))
    add("detrazione", "nessuna", False, detrazione_annua_eur=0.0)

    # 5. Installed cost at the low / high end of the market band
    low, high = capex_banda
    for c, lab, base in ((low, "minimo di mercato", False),
                         (capex_eur, "stima centrale", True),
                         (high, "massimo di mercato", False)):
        add("costo", f"{lab} (€{_int_it(c)})", base, capex_nuovo_eur=c,
            detrazione_annua_eur=detrazione_annua(c, abitazione_principale, irpef_annua_stimata))
    return out


def co2_evitata(flows: list) -> tuple[float, float]:
    """(kg in year 1, tonnes over the whole horizon) on all production."""
    anno1 = flows[0].pv_production_kWh * CO2_KG_PER_KWH if flows else 0.0
    totale_t = sum(cf.pv_production_kWh for cf in flows) * CO2_KG_PER_KWH / 1000.0
    return round(anno1, 0), round(totale_t, 1)
