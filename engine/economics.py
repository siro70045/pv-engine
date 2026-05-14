"""
engine/economics.py

Techno-economic analysis of a PV investment.

Receives the 25-year energy balance from energy_balance.simulate_lifetime()
and produces a complete financial assessment: investment costs, annual cash
flows, NPV, IRR, payback periods, LCOE, Italian fiscal incentives, sensitivity
analysis, and a composite investment score.

Position in the chain
---------------------
    energy_balance.simulate_lifetime()  →  list[EnergyBalanceResult]
                                                    │
                                                    ▼
                                          economics.calc_economics()
                                                    │
                                                    ▼
                                           EconomicsResult
                                     (VAN, TIR, payback, score, ...)

Standards and references
------------------------
    IEC 62670-3:2016   PV performance — Energy evaluation
    EN 15459-1:2017    Energy performance of buildings — Economic evaluation
    ARERA delibera     Italian electricity market tariffs
    DM 23/06/2016      Scambio sul posto (GSE)
    L. 234/2021        Comunità Energetiche Rinnovabili (CER)
    DM Piano Trans.5.0 Credito d'imposta Transizione 5.0 (2024)
    DPR 917/1986 TUIR  Italian income tax — investment deductions
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — INPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class InvestmentCost:
    """
    Capital expenditure (CAPEX) breakdown.

    All values in EUR. Set to 0 if not applicable.

    Parameters
    ----------
    pv_cost_eur_kWp : float
        Total installed PV system cost per kWp [€/kWp].
        Includes modules, inverter, mounting, DC/AC cabling.
        Typical 2024 Italy:
          Residential < 10 kWp  : 1 600–2 200 €/kWp
          Commercial 10–100 kWp : 1 000–1 500 €/kWp
          Industrial > 100 kWp  : 700–1 100 €/kWp
    peak_power_kWp : float
        PV system peak power [kWp].
    battery_cost_eur_kWh : float
        Battery system cost per usable kWh [€/kWh].
        Typical 2024: LFP AC-coupled 400–700 €/kWh usable.
    battery_capacity_kWh : float
        Battery nominal capacity [kWh].
    design_and_permits_eur : float
        Engineering, permits, grid connection application [€].
        Typical: 2–5% of PV CAPEX.
    grid_connection_eur : float
        Grid connection upgrade cost [€].
        0 for small systems already connected.
    heat_pump_cost_eur : float
        Heat pump system installed cost [€] (unit + installation).
        0 if no HP in this scenario. Used by `engine/scenarios.py`
        for HP-bearing scenarios (solo_hp, pv_hp, pv_hp_biv).
    other_capex_eur : float
        Miscellaneous: EV charger, monitoring system, structural works, etc.
    """
    pv_cost_eur_kWp: float = 1100.0
    peak_power_kWp: float = 0.0
    battery_cost_eur_kWh: float = 550.0
    battery_capacity_kWh: float = 0.0
    design_and_permits_eur: float = 0.0
    grid_connection_eur: float = 0.0
    heat_pump_cost_eur: float = 0.0
    other_capex_eur: float = 0.0

    @property
    def pv_capex_eur(self) -> float:
        return self.pv_cost_eur_kWp * self.peak_power_kWp

    @property
    def battery_capex_eur(self) -> float:
        return self.battery_cost_eur_kWh * self.battery_capacity_kWh

    @property
    def total_capex_eur(self) -> float:
        return (self.pv_capex_eur
                + self.battery_capex_eur
                + self.design_and_permits_eur
                + self.grid_connection_eur
                + self.heat_pump_cost_eur
                + self.other_capex_eur)


@dataclass
class OperatingCost:
    """
    Annual operating expenditure (OPEX) and replacement costs.

    Parameters
    ----------
    om_pct_of_capex_per_yr : float
        O&M as fraction of total CAPEX [% per year].
        Typical: 0.5–1.5 % for utility-scale; 1–2 % for rooftop.
        Includes: cleaning, inverter maintenance, monitoring, insurance.
    om_fixed_eur_yr : float
        Fixed O&M regardless of system size [€/yr].
        Use when a service contract has a fixed annual fee.
    insurance_eur_yr : float
        Annual insurance premium [€/yr]. Often included in O&M.
    inverter_replacement_year : int
        Year when inverter is replaced. Typical lifespan: 10–15 yr.
        0 = no planned replacement.
    inverter_replacement_cost_eur : float
        Inverter replacement cost [€]. Typically 20–30% of original inverter cost.
    battery_replacement_cost_eur_kWh : float
        Battery replacement cost per kWh at replacement time [€/kWh].
        Typically 60–70% of original cost (price decline).
    battery_replacement_years : list[int]
        Years when battery is replaced (from find_battery_replacement_years()).
        Empty = no replacement planned.
    om_escalation_rate : float
        Annual increase in O&M costs [fraction/yr]. Typical: 0.02 (inflation).
    """
    om_pct_of_capex_per_yr: float = 1.0
    om_fixed_eur_yr: float = 0.0
    insurance_eur_yr: float = 0.0
    inverter_replacement_year: int = 12
    inverter_replacement_cost_eur: float = 0.0
    battery_replacement_cost_eur_kWh: float = 350.0
    battery_replacement_years: list[int] = field(default_factory=list)
    om_escalation_rate: float = 0.02

    def om_year1(self, total_capex: float) -> float:
        """Annual O&M in year 1 [€/yr]."""
        return (total_capex * self.om_pct_of_capex_per_yr / 100.0
                + self.om_fixed_eur_yr
                + self.insurance_eur_yr)

    def om_in_year(self, total_capex: float, year: int) -> float:
        """O&M cost in a given year (year 1-based), with escalation."""
        base = self.om_year1(total_capex)
        return base * (1 + self.om_escalation_rate) ** (year - 1)


@dataclass
class FinancialParameters:
    """
    Financial assumptions for the NPV/IRR calculation.

    Parameters
    ----------
    discount_rate : float
        Weighted average cost of capital (WACC) or required return [fraction].
        Typical for industrial PV Italy: 0.04–0.07 (4–7%).
        Use the real (inflation-adjusted) rate if using real cash flows,
        or nominal rate with nominal cash flows.
    energy_price_escalation : float
        Annual growth rate of electricity purchase price [fraction/yr].
        Historical Italy 2010–2023: ~3.5 %/yr (with high volatility).
        Conservative estimate: 0.02–0.03.
    inflation_rate : float
        General inflation rate [fraction/yr]. Used for O&M escalation.
    analysis_years : int
        Evaluation horizon [years]. Typically 20–25 (module warranty).
    financing_equity_fraction : float
        Fraction of CAPEX paid with equity [0–1]. 1.0 = all equity (no loan).
    loan_interest_rate : float
        Annual interest rate on the financed portion [fraction].
        0 if financing_equity_fraction = 1.0.
    loan_duration_years : int
        Duration of the financing loan [years].
    """
    discount_rate: float = 0.05
    energy_price_escalation: float = 0.025
    inflation_rate: float = 0.02
    analysis_years: int = 25
    financing_equity_fraction: float = 1.0
    loan_interest_rate: float = 0.04
    loan_duration_years: int = 10
    inverter_replacement_year: int = 12
    inverter_replacement_cost_pct: float = 0.0


@dataclass
class ItalianIncentives:
    """
    Italian fiscal and regulatory incentives.

    Parameters
    ----------
    detrazione_fiscale_pct : float
        IRPEF/IRES deduction as percentage of CAPEX [0–100].
        Residential IRPEF: 50% over 10 years (L.449/1997, prorogated annually).
        Commercial / corporate: the cost is capitalized and depreciated;
          set detrazione_fiscale_pct=0 and use ammortamento_accelerato=True.
        Industrial Superbonus (if applicable, now largely expired): 110%.
    detrazione_spread_years : int
        Number of years over which the deduction is spread.
        Residential: 10 years. Other: depends on scheme.
    ammortamento_accelerato : bool
        True for corporate entities: the entire CAPEX can be depreciated
        (ammortizzato) against taxable income.
        Uses aliquota_ammortamento to compute annual tax shield.
    aliquota_ammortamento : float
        Annual depreciation rate for tax purposes [fraction].
        Italy: PV impianti industriali, typically 0.09 (9%/yr → 11 years).
    aliquota_ires_pct : float
        Corporate income tax rate (IRES) [%]. Current: 24%.
        Only used if ammortamento_accelerato = True.
    transizione_5_0_pct : float
        Tax credit "Transizione 5.0" (DM 2024) on qualified investments [%].
        0 = not applicable.
        Available for energy efficiency improvements with ESG criteria:
          15% for investments 0.5–2.5 M€
          10% for 2.5–10 M€ (reduced rates)
        Must verify eligibility — set to 0 if uncertain.
    cer_incentivo_eur_kWh : float
        CER (Comunità Energetica Rinnovabile) virtual net metering incentive [€/kWh].
        GSE pays ~0.11 €/kWh on shared energy in CER (DM 07/12/2023).
        0 if not joining a CER.
    cer_shared_fraction : float
        Fraction of production shared in the CER [0–1].
        Used only if cer_incentivo_eur_kWh > 0.
    """
    detrazione_fiscale_pct: float = 0.0
    detrazione_spread_years: int = 10
    ammortamento_accelerato: bool = False
    aliquota_ammortamento: float = 0.09
    aliquota_ires_pct: float = 24.0
    transizione_5_0_pct: float = 0.0
    cer_incentivo_eur_kWh: float = 0.0
    cer_shared_fraction: float = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — OUTPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AnnualCashFlow:
    """Financial flows for a single year."""
    year: int
    pv_production_kWh: float
    self_consumed_kWh: float
    grid_export_kWh: float
    energy_saving_eur: float          # avoided grid purchase cost
    export_revenue_eur: float         # income from grid export
    cer_incentive_eur: float          # CER virtual net metering incentive
    om_cost_eur: float                # O&M + insurance
    replacement_cost_eur: float       # inverter or battery replacement
    loan_payment_eur: float           # annual loan instalment (if financed)
    tax_benefit_eur: float            # detrazione fiscale / ammortamento shield
    net_cash_flow_eur: float          # total after-tax net benefit
    cumulative_cash_flow_eur: float   # running total (for payback detection)
    discounted_cash_flow_eur: float   # net_cf / (1+r)^year
    cumulative_discounted_cf_eur: float


@dataclass
class SensitivityScenario:
    """Result of one sensitivity scenario."""
    name: str
    energy_price_escalation: float
    discount_rate: float
    npv_eur: float
    irr_pct: float
    payback_simple_yr: float
    payback_discounted_yr: float


@dataclass
class EconomicsResult:
    """
    Complete economic assessment of the PV investment.
    """
    # ── Investment ────────────────────────────────────────────────────────
    total_capex_eur: float
    pv_capex_eur: float
    battery_capex_eur: float
    incentive_present_value_eur: float    # PV of tax deductions / credits
    net_capex_after_incentives_eur: float # CAPEX - incentive PV

    # ── Annual performance (year 1 reference) ─────────────────────────────
    year1_production_kWh: float
    year1_self_consumed_kWh: float
    year1_self_consumption_rate: float
    year1_self_sufficiency_rate: float
    year1_saving_eur: float
    year1_export_revenue_eur: float
    year1_net_benefit_eur: float
    year1_om_cost_eur: float

    # ── Lifetime totals ───────────────────────────────────────────────────
    lifetime_production_kWh: float
    lifetime_saving_eur: float
    lifetime_export_revenue_eur: float
    lifetime_om_cost_eur: float
    lifetime_replacement_cost_eur: float

    # ── Key financial KPIs ────────────────────────────────────────────────
    npv_eur: float                  # Net Present Value at discount_rate
    irr_pct: float                  # Internal Rate of Return [%]
    payback_simple_yr: float        # simple (undiscounted) payback [years]
    payback_discounted_yr: float    # discounted payback [years]
    lcoe_eur_kWh: float             # Levelized Cost of Energy [€/kWh]
    roi_pct: float                  # Return on Investment = NPV / CAPEX [%]
    benefit_cost_ratio: float       # PV(benefits) / CAPEX

    # ── Annual cash flow table ────────────────────────────────────────────
    cash_flows: list[AnnualCashFlow]

    # ── Sensitivity analysis ──────────────────────────────────────────────
    sensitivity: list[SensitivityScenario]

    # ── Investment recommendation ─────────────────────────────────────────
    score: float           # composite score 0–10
    recommendation: str    # "Excellent", "Good", "Marginal", "Not recommended"
    score_breakdown: dict  # individual score components

    # ── Parameters used ───────────────────────────────────────────────────
    analysis_years: int
    discount_rate: float
    peak_power_kWp: float
    battery_capacity_kWh: float


# ══════════════════════════════════════════════════════════════════════════════
# LOAN AMORTISATION
# ══════════════════════════════════════════════════════════════════════════════

def _annual_loan_payment(principal: float, rate: float, years: int) -> float:
    """
    Annual loan payment (annuity) using the standard amortisation formula.

    PMT = P × r × (1+r)^n / ((1+r)^n - 1)

    Returns 0 if rate = 0 (interest-free) or years = 0.
    """
    if principal <= 0 or years <= 0:
        return 0.0
    if rate <= 0:
        return principal / years
    return principal * rate * (1 + rate)**years / ((1 + rate)**years - 1)


# ══════════════════════════════════════════════════════════════════════════════
# INCENTIVE PRESENT VALUE
# ══════════════════════════════════════════════════════════════════════════════

def _incentive_present_value(
    total_capex: float,
    incentives: ItalianIncentives,
    fin: FinancialParameters,
) -> float:
    """
    Present value of all Italian fiscal incentives at t=0.

    1. Detrazione fiscale: annual credit = (capex × pct%) / spread_years
       for spread_years years, discounted at discount_rate.

    2. Ammortamento accelerato: annual tax shield = capex × rate × IRES%
       for 1/rate years, discounted.

    3. Transizione 5.0: lump sum at t=0 (credit received when filing taxes,
       typically year 1 or 2; modelled as year 1 for simplicity).
    """
    r   = fin.discount_rate
    pv  = 0.0

    # Detrazione fiscale (IRPEF/IRES deduction)
    if incentives.detrazione_fiscale_pct > 0:
        annual_credit = (total_capex
                         * incentives.detrazione_fiscale_pct / 100.0
                         / incentives.detrazione_spread_years)
        for yr in range(1, incentives.detrazione_spread_years + 1):
            pv += annual_credit / (1 + r)**yr

    # Ammortamento accelerato (corporate depreciation tax shield)
    if incentives.ammortamento_accelerato:
        rate       = incentives.aliquota_ammortamento
        ires       = incentives.aliquota_ires_pct / 100.0
        n_years    = min(int(math.ceil(1.0 / rate)), fin.analysis_years)
        annual_dep = total_capex * rate
        for yr in range(1, n_years + 1):
            shield = annual_dep * ires
            pv    += shield / (1 + r)**yr

    # Transizione 5.0 tax credit (received in year 1)
    if incentives.transizione_5_0_pct > 0:
        credit = total_capex * incentives.transizione_5_0_pct / 100.0
        pv    += credit / (1 + r)    # year 1

    return round(pv, 2)


# ══════════════════════════════════════════════════════════════════════════════
# IRR — NUMERICAL SOLUTION
# ══════════════════════════════════════════════════════════════════════════════

def _calc_irr(cash_flows_including_t0: list[float], max_iter: int = 200) -> float:
    """
    Internal Rate of Return: rate r such that NPV(r) = 0.

    Uses the Newton-Raphson method with bisection fallback.

    Parameters
    ----------
    cash_flows_including_t0 : list[float]
        CF[0] = -CAPEX (negative), CF[1..n] = annual net benefits.

    Returns
    -------
    float
        IRR as a fraction (multiply by 100 for %). Returns -1.0 if no
        solution found (e.g. no positive cash flows).
    """
    cfs = cash_flows_including_t0

    def npv_at(r: float) -> float:
        return sum(cf / (1 + r)**t for t, cf in enumerate(cfs))

    # Quick check: if no positive cash flows, IRR is undefined
    if all(cf <= 0 for cf in cfs[1:]):
        return -1.0

    # Try Newton-Raphson starting from 10%
    r = 0.10
    for _ in range(max_iter):
        npv  = npv_at(r)
        # Derivative dNPV/dr
        dnpv = sum(-t * cf / (1 + r)**(t + 1) for t, cf in enumerate(cfs))
        if abs(dnpv) < 1e-12:
            break
        r_new = r - npv / dnpv
        if abs(r_new - r) < 1e-8:
            return r_new
        r = max(-0.999, min(r_new, 10.0))   # clamp to [-99.9%, 1000%]

    # Fallback: bisection between -99% and 500%
    lo, hi = -0.999, 5.0
    if npv_at(lo) * npv_at(hi) > 0:
        return -1.0   # no sign change — IRR not in range

    for _ in range(100):
        mid = (lo + hi) / 2.0
        if npv_at(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    return (lo + hi) / 2.0


# ══════════════════════════════════════════════════════════════════════════════
# LCOE
# ══════════════════════════════════════════════════════════════════════════════

def _calc_lcoe(
    capex: float,
    annual_om: list[float],
    annual_production_kWh: list[float],
    discount_rate: float,
) -> float:
    """
    Levelized Cost of Energy [€/kWh].

    LCOE = (CAPEX + Σ(OM_t / (1+r)^t)) / Σ(E_t / (1+r)^t)

    Represents the break-even cost at which the system generates energy
    at zero NPV — useful to compare against the grid tariff.
    """
    n = len(annual_production_kWh)
    pv_costs   = capex + sum(om / (1 + discount_rate)**t
                              for t, om in enumerate(annual_om, start=1))
    pv_energy  = sum(e / (1 + discount_rate)**t
                     for t, e in enumerate(annual_production_kWh, start=1))
    if pv_energy <= 0:
        return 0.0
    return round(pv_costs / pv_energy, 4)


# ══════════════════════════════════════════════════════════════════════════════
# INVESTMENT SCORE
# ══════════════════════════════════════════════════════════════════════════════

def _investment_score(
    irr_pct: float,
    payback_simple_yr: float,
    npv_eur: float,
    capex: float,
    self_consumption_rate: float,
    analysis_years: int,
) -> tuple[float, dict, str]:
    """
    Compute a composite investment score 0–10.

    Five equally-weighted components (0–2 points each):

    1. IRR vs discount rate spread
       2.0: IRR > 15%
       1.5: IRR 10–15%
       1.0: IRR 7–10%
       0.5: IRR 4–7%
       0.0: IRR < 4% or negative

    2. Simple payback period
       2.0: ≤ 5 years
       1.5: 5–8 years
       1.0: 8–12 years
       0.5: 12–15 years
       0.0: > 15 years

    3. NPV / CAPEX ratio (ROI index)
       2.0: > 100%
       1.5: 60–100%
       1.0: 30–60%
       0.5: 10–30%
       0.0: < 10%

    4. Self-consumption rate (energy value quality)
       2.0: > 80%
       1.5: 60–80%
       1.0: 40–60%
       0.5: 20–40%
       0.0: < 20%

    5. Payback < half of analysis period (robustness)
       2.0: payback ≤ analysis_years / 3
       1.0: payback ≤ analysis_years / 2
       0.0: payback > analysis_years / 2
    """
    breakdown: dict = {}

    # 1. IRR
    if irr_pct >= 15:
        s_irr = 2.0
    elif irr_pct >= 10:
        s_irr = 1.5
    elif irr_pct >= 7:
        s_irr = 1.0
    elif irr_pct >= 4:
        s_irr = 0.5
    else:
        s_irr = 0.0
    breakdown["irr_score"] = s_irr

    # 2. Payback
    if payback_simple_yr <= 5:
        s_pb = 2.0
    elif payback_simple_yr <= 8:
        s_pb = 1.5
    elif payback_simple_yr <= 12:
        s_pb = 1.0
    elif payback_simple_yr <= 15:
        s_pb = 0.5
    else:
        s_pb = 0.0
    breakdown["payback_score"] = s_pb

    # 3. NPV / CAPEX
    roi_ratio = npv_eur / capex if capex > 0 else 0
    if roi_ratio >= 1.0:
        s_npv = 2.0
    elif roi_ratio >= 0.6:
        s_npv = 1.5
    elif roi_ratio >= 0.3:
        s_npv = 1.0
    elif roi_ratio >= 0.1:
        s_npv = 0.5
    else:
        s_npv = 0.0
    breakdown["npv_roi_score"] = s_npv

    # 4. Self-consumption
    scr = self_consumption_rate
    if scr >= 0.80:
        s_sc = 2.0
    elif scr >= 0.60:
        s_sc = 1.5
    elif scr >= 0.40:
        s_sc = 1.0
    elif scr >= 0.20:
        s_sc = 0.5
    else:
        s_sc = 0.0
    breakdown["self_consumption_score"] = s_sc

    # 5. Payback robustness
    if payback_simple_yr <= analysis_years / 3:
        s_rob = 2.0
    elif payback_simple_yr <= analysis_years / 2:
        s_rob = 1.0
    else:
        s_rob = 0.0
    breakdown["robustness_score"] = s_rob

    total = s_irr + s_pb + s_npv + s_sc + s_rob
    breakdown["total"] = round(total, 2)

    if total >= 8.0:
        rec = "Excellent investment"
    elif total >= 6.0:
        rec = "Good investment"
    elif total >= 4.0:
        rec = "Marginal — review assumptions"
    else:
        rec = "Not recommended at current conditions"

    return round(total, 2), breakdown, rec


# ══════════════════════════════════════════════════════════════════════════════
# MAIN CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def _calc_economics_core(
    yearly_results: list,
    investment: InvestmentCost,
    opex: OperatingCost,
    fin: FinancialParameters,
    incentives: ItalianIncentives,
    n_years: int,
) -> tuple[float, float, float, float]:
    """
    Internal: compute (NPV, IRR%, simple_payback, discounted_payback)
    without the sensitivity analysis loop — avoids infinite recursion.
    """
    capex       = investment.total_capex_eur
    r           = fin.discount_rate
    esc         = fin.energy_price_escalation
    incentive_pv = _incentive_present_value(capex, incentives, fin)
    loan_pmt    = _annual_loan_payment(
        capex * (1.0 - fin.financing_equity_fraction),
        fin.loan_interest_rate, fin.loan_duration_years
    )
    detr_annual = (capex * incentives.detrazione_fiscale_pct / 100.0
                   / incentives.detrazione_spread_years
                   if incentives.detrazione_fiscale_pct > 0 else 0.0)
    amm_annual  = (capex * incentives.aliquota_ammortamento
                   * incentives.aliquota_ires_pct / 100.0
                   if incentives.ammortamento_accelerato else 0.0)

    equity_outflow = capex * fin.financing_equity_fraction
    irr_flows = [-equity_outflow]   # t=0: equity portion only (loan repaid via PMT)
    cum_cf = cum_dcf = -equity_outflow
    pb_simple = pb_disc = float("inf")

    for yr in range(1, n_years + 1):
        res     = yearly_results[yr - 1]
        saving  = res.annual_saving_eur * (1 + esc)**(yr - 1)
        export  = (res.export_revenue_eur + res.demand_charge_saving_eur) * (1 + esc)**(yr - 1)
        cer     = (res.pv_production_kWh * incentives.cer_shared_fraction
                   * incentives.cer_incentivo_eur_kWh
                   if incentives.cer_incentivo_eur_kWh > 0 else 0.0)
        om      = opex.om_in_year(capex, yr)
        repl    = 0.0
        if yr == opex.inverter_replacement_year:
            repl += opex.inverter_replacement_cost_eur
        if yr in opex.battery_replacement_years and investment.battery_capacity_kWh > 0:
            repl += investment.battery_capacity_kWh * opex.battery_replacement_cost_eur_kWh
        if fin.inverter_replacement_cost_pct > 0 and yr == fin.inverter_replacement_year:
            repl += capex * fin.inverter_replacement_cost_pct
        loan    = loan_pmt if yr <= fin.loan_duration_years else 0.0
        tax     = 0.0
        if yr <= incentives.detrazione_spread_years:
            tax += detr_annual
        if incentives.ammortamento_accelerato:
            n_dep = int(math.ceil(1.0 / max(incentives.aliquota_ammortamento, 0.01)))
            if yr <= n_dep:
                tax += amm_annual
        if yr == 1 and incentives.transizione_5_0_pct > 0:
            tax += capex * incentives.transizione_5_0_pct / 100.0

        net_cf  = saving + export + cer + tax - om - repl - loan
        dcf     = net_cf / (1 + r)**yr
        cum_cf += net_cf
        cum_dcf += dcf

        if pb_simple == float("inf") and cum_cf >= 0:
            prev = cum_cf - net_cf
            frac = -prev / net_cf if net_cf > 0 else 0.0
            pb_simple = yr - 1 + max(0.0, min(frac, 1.0))
        if pb_disc == float("inf") and cum_dcf >= 0:
            prev = cum_dcf - dcf
            frac = -prev / dcf if dcf > 0 else 0.0
            pb_disc = yr - 1 + max(0.0, min(frac, 1.0))

        irr_flows.append(net_cf)

    npv    = round(cum_dcf, 2)
    irr_r  = _calc_irr(irr_flows)
    irr_pct = round(irr_r * 100, 2) if irr_r > -1.0 else -100.0
    return npv, irr_pct, round(pb_simple, 2), round(pb_disc, 2)


def calc_economics(
    yearly_results: list,    # list[EnergyBalanceResult] from simulate_lifetime()
    investment: InvestmentCost,
    opex: OperatingCost,
    fin: FinancialParameters,
    incentives: ItalianIncentives,
) -> EconomicsResult:
    """
    Full techno-economic analysis of a PV investment.

    Parameters
    ----------
    yearly_results : list[EnergyBalanceResult]
        Output of energy_balance.simulate_lifetime().
        Each element covers one year; index 0 = year 1.
    investment : InvestmentCost
        CAPEX breakdown.
    opex : OperatingCost
        Annual O&M, insurance, replacement costs.
    fin : FinancialParameters
        Discount rate, escalation, loan parameters.
    incentives : ItalianIncentives
        Italian fiscal incentives applicable to the investment.

    Returns
    -------
    EconomicsResult
        Complete financial assessment.

    Example
    -------
    >>> from engine.energy_balance import simulate_lifetime, EnergyBalanceInput
    >>> from engine.economics import *
    >>>
    >>> yearly = simulate_lifetime(base_inp, lifetime_years=25)
    >>>
    >>> result = calc_economics(
    ...     yearly_results = yearly,
    ...     investment = InvestmentCost(
    ...         pv_cost_eur_kWp=1000, peak_power_kWp=120,
    ...         battery_cost_eur_kWh=500, battery_capacity_kWh=100,
    ...     ),
    ...     opex = OperatingCost(om_pct_of_capex_per_yr=1.0),
    ...     fin  = FinancialParameters(discount_rate=0.05,
    ...                                energy_price_escalation=0.025),
    ...     incentives = ItalianIncentives(
    ...         ammortamento_accelerato=True,
    ...         aliquota_ires_pct=24.0,
    ...     ),
    ... )
    >>> print(f"NPV:     €{result.npv_eur:,.0f}")
    >>> print(f"IRR:     {result.irr_pct:.1f}%")
    >>> print(f"Payback: {result.payback_simple_yr:.1f} years")
    >>> print(f"Score:   {result.score}/10 — {result.recommendation}")
    """
    n_years     = min(len(yearly_results), fin.analysis_years)
    capex       = investment.total_capex_eur
    r           = fin.discount_rate
    esc         = fin.energy_price_escalation

    # ── Incentive present value ───────────────────────────────────────────────
    incentive_pv = _incentive_present_value(capex, incentives, fin)

    # ── Loan payments ─────────────────────────────────────────────────────────
    loan_principal = capex * (1.0 - fin.financing_equity_fraction)
    annual_loan_pmt = _annual_loan_payment(
        loan_principal, fin.loan_interest_rate, fin.loan_duration_years
    )

    # ── Year-1 reference values ───────────────────────────────────────────────
    yr1 = yearly_results[0]
    y1_saving  = yr1.annual_saving_eur
    y1_export  = yr1.export_revenue_eur + yr1.demand_charge_saving_eur
    y1_benefit = yr1.net_annual_benefit_eur

    # ── Build annual cash flows ───────────────────────────────────────────────
    cash_flows:       list[AnnualCashFlow] = []
    # t=0 outflow = equity portion only.
    # The loan is received at t=0 (offsets part of CAPEX) and repaid via annual PMT.
    # This correctly reflects the equity investor's perspective:
    #   With fair-cost debt (loan rate = discount rate), NPV should be invariant
    #   to the financing mix — now it is.
    equity_outflow = capex * fin.financing_equity_fraction
    irr_flows:        list[float]          = [-equity_outflow]
    annual_om_list:   list[float]          = []
    annual_prod_list: list[float]          = []

    cum_cf   = -equity_outflow   # starts negative (equity invested, un-discounted)
    cum_dcf  = -equity_outflow   # starts negative (equity invested, discounted)

    # Incentive tax benefit per year (for CashFlow object)
    detr_annual = 0.0
    if incentives.detrazione_fiscale_pct > 0:
        detr_annual = (capex * incentives.detrazione_fiscale_pct / 100.0
                       / incentives.detrazione_spread_years)

    amm_annual = 0.0
    if incentives.ammortamento_accelerato:
        amm_annual = (capex
                      * incentives.aliquota_ammortamento
                      * incentives.aliquota_ires_pct / 100.0)

    trans50_year1 = capex * incentives.transizione_5_0_pct / 100.0

    for yr in range(1, n_years + 1):
        res = yearly_results[yr - 1]

        # Energy saving escalated: the avoided cost grows with electricity prices
        # Year 1 saving is the base; each subsequent year the tariff is higher.
        # We use the actual yearly result which already has degradation applied.
        # To account for price escalation, scale year-1 saving by (1+esc)^(yr-1).
        energy_saving  = res.annual_saving_eur   * (1 + esc)**(yr - 1)
        export_revenue = (res.export_revenue_eur
                          + res.demand_charge_saving_eur) * (1 + esc)**(yr - 1)

        # CER incentive
        cer_incentive = 0.0
        if incentives.cer_incentivo_eur_kWh > 0 and incentives.cer_shared_fraction > 0:
            cer_incentive = (res.pv_production_kWh
                             * incentives.cer_shared_fraction
                             * incentives.cer_incentivo_eur_kWh)

        # O&M cost
        om_cost = opex.om_in_year(capex, yr)
        annual_om_list.append(om_cost)

        # Replacement costs this year
        repl_cost = 0.0
        if yr == opex.inverter_replacement_year and opex.inverter_replacement_cost_eur > 0:
            repl_cost += opex.inverter_replacement_cost_eur
        if yr in opex.battery_replacement_years and investment.battery_capacity_kWh > 0:
            repl_cost += (investment.battery_capacity_kWh
                          * opex.battery_replacement_cost_eur_kWh)
        if fin.inverter_replacement_cost_pct > 0 and yr == fin.inverter_replacement_year:
            repl_cost += capex * fin.inverter_replacement_cost_pct

        # Loan payment (0 after loan_duration_years)
        loan_pmt = annual_loan_pmt if yr <= fin.loan_duration_years else 0.0

        # Tax benefit this year
        tax_benefit = 0.0
        if yr <= incentives.detrazione_spread_years:
            tax_benefit += detr_annual
        if incentives.ammortamento_accelerato:
            n_dep_years = int(math.ceil(1.0 / max(incentives.aliquota_ammortamento, 0.01)))
            if yr <= n_dep_years:
                tax_benefit += amm_annual
        if yr == 1 and incentives.transizione_5_0_pct > 0:
            tax_benefit += trans50_year1

        # Net cash flow
        net_cf = (energy_saving + export_revenue + cer_incentive
                  + tax_benefit - om_cost - repl_cost - loan_pmt)

        cum_cf   += net_cf
        dcf       = net_cf / (1 + r)**yr
        cum_dcf  += dcf

        cash_flows.append(AnnualCashFlow(
            year                        = yr,
            pv_production_kWh           = res.pv_production_kWh,
            self_consumed_kWh           = res.self_consumed_kWh,
            grid_export_kWh             = res.grid_export_kWh,
            energy_saving_eur           = round(energy_saving, 2),
            export_revenue_eur          = round(export_revenue, 2),
            cer_incentive_eur           = round(cer_incentive, 2),
            om_cost_eur                 = round(om_cost, 2),
            replacement_cost_eur        = round(repl_cost, 2),
            loan_payment_eur            = round(loan_pmt, 2),
            tax_benefit_eur             = round(tax_benefit, 2),
            net_cash_flow_eur           = round(net_cf, 2),
            cumulative_cash_flow_eur    = round(cum_cf, 2),
            discounted_cash_flow_eur    = round(dcf, 2),
            cumulative_discounted_cf_eur= round(cum_dcf, 2),
        ))

        irr_flows.append(net_cf)
        annual_prod_list.append(res.pv_production_kWh)

    # ── NPV ───────────────────────────────────────────────────────────────────
    # NPV = Σ(discounted annual CFs) - equity_outflow at t=0
    # equity_outflow is already embedded in cum_dcf (starts at -equity_outflow),
    # so the final cumulative discounted CF IS the NPV directly.
    npv = sum(cf.discounted_cash_flow_eur for cf in cash_flows) - equity_outflow
    npv = round(npv, 2)

    # ── IRR ───────────────────────────────────────────────────────────────────
    irr_raw = _calc_irr(irr_flows)
    irr_pct = round(irr_raw * 100, 2) if irr_raw > -1.0 else -100.0

    # ── Simple payback ────────────────────────────────────────────────────────
    payback_simple = float("inf")
    for cf in cash_flows:
        if cf.cumulative_cash_flow_eur >= 0:
            # Linear interpolation within the year
            prev_cum = cf.cumulative_cash_flow_eur - cf.net_cash_flow_eur
            if cf.net_cash_flow_eur > 0:
                frac = -prev_cum / cf.net_cash_flow_eur
                payback_simple = cf.year - 1 + max(0.0, min(frac, 1.0))
            else:
                payback_simple = float(cf.year)
            break
    payback_simple = round(payback_simple, 2)

    # ── Discounted payback ────────────────────────────────────────────────────
    payback_discounted = float("inf")
    for cf in cash_flows:
        if cf.cumulative_discounted_cf_eur >= 0:
            prev_cum = cf.cumulative_discounted_cf_eur - cf.discounted_cash_flow_eur
            if cf.discounted_cash_flow_eur > 0:
                frac = -prev_cum / cf.discounted_cash_flow_eur
                payback_discounted = cf.year - 1 + max(0.0, min(frac, 1.0))
            else:
                payback_discounted = float(cf.year)
            break
    payback_discounted = round(payback_discounted, 2)

    # ── LCOE ─────────────────────────────────────────────────────────────────
    lcoe = _calc_lcoe(capex, annual_om_list, annual_prod_list, r)

    # ── ROI and benefit-cost ratio ────────────────────────────────────────────
    roi_pct = round(npv / capex * 100, 2) if capex > 0 else 0.0
    pv_benefits = sum(
        (cf.energy_saving_eur + cf.export_revenue_eur
         + cf.cer_incentive_eur + cf.tax_benefit_eur)
        / (1 + r)**cf.year
        for cf in cash_flows
    )
    bcr = round(pv_benefits / capex, 3) if capex > 0 else 0.0

    # ── Lifetime totals ───────────────────────────────────────────────────────
    lt_prod    = sum(cf.pv_production_kWh   for cf in cash_flows)
    lt_saving  = sum(cf.energy_saving_eur   for cf in cash_flows)
    lt_export  = sum(cf.export_revenue_eur  for cf in cash_flows)
    lt_om      = sum(cf.om_cost_eur         for cf in cash_flows)
    lt_repl    = sum(cf.replacement_cost_eur for cf in cash_flows)

    # ── Sensitivity analysis ─────────────────────────────────────────────────
    # Uses _calc_economics_core to avoid infinite recursion
    sensitivity_scenarios = [
        ("Pessimistic",  0.00,  0.07),
        ("Base case",    esc,   r),
        ("Optimistic",   0.04,  0.04),
        ("High tariff",  0.05,  r),
        ("High discount",esc,   0.08),
    ]

    sensitivity: list[SensitivityScenario] = []
    for name, esc_s, r_s in sensitivity_scenarios:
        fin_s = FinancialParameters(
            discount_rate              = r_s,
            energy_price_escalation    = esc_s,
            inflation_rate             = fin.inflation_rate,
            analysis_years             = n_years,
            financing_equity_fraction  = fin.financing_equity_fraction,
            loan_interest_rate         = fin.loan_interest_rate,
            loan_duration_years        = fin.loan_duration_years,
        )
        npv_s, irr_s, pb_s, pbd_s = _calc_economics_core(
            yearly_results, investment, opex, fin_s, incentives, n_years
        )
        sensitivity.append(SensitivityScenario(
            name                    = name,
            energy_price_escalation = esc_s,
            discount_rate           = r_s,
            npv_eur                 = npv_s,
            irr_pct                 = irr_s,
            payback_simple_yr       = pb_s,
            payback_discounted_yr   = pbd_s,
        ))

    # ── Investment score ──────────────────────────────────────────────────────
    score, score_bd, recommendation = _investment_score(
        irr_pct            = irr_pct,
        payback_simple_yr  = payback_simple,
        npv_eur            = npv,
        capex              = capex,
        self_consumption_rate = yr1.self_consumption_rate,
        analysis_years     = n_years,
    )

    return EconomicsResult(
        total_capex_eur                = round(capex, 2),
        pv_capex_eur                   = round(investment.pv_capex_eur, 2),
        battery_capex_eur              = round(investment.battery_capex_eur, 2),
        incentive_present_value_eur    = round(incentive_pv, 2),
        net_capex_after_incentives_eur = round(capex - incentive_pv, 2),
        year1_production_kWh           = round(yr1.pv_production_kWh, 1),
        year1_self_consumed_kWh        = round(yr1.self_consumed_kWh, 1),
        year1_self_consumption_rate    = round(yr1.self_consumption_rate, 4),
        year1_self_sufficiency_rate    = round(yr1.self_sufficiency_rate, 4),
        year1_saving_eur               = round(y1_saving, 2),
        year1_export_revenue_eur       = round(y1_export, 2),
        year1_net_benefit_eur          = round(y1_benefit, 2),
        year1_om_cost_eur              = round(opex.om_year1(capex), 2),
        lifetime_production_kWh        = round(lt_prod, 1),
        lifetime_saving_eur            = round(lt_saving, 2),
        lifetime_export_revenue_eur    = round(lt_export, 2),
        lifetime_om_cost_eur           = round(lt_om, 2),
        lifetime_replacement_cost_eur  = round(lt_repl, 2),
        npv_eur                        = npv,
        irr_pct                        = irr_pct,
        payback_simple_yr              = payback_simple,
        payback_discounted_yr          = payback_discounted,
        lcoe_eur_kWh                   = lcoe,
        roi_pct                        = roi_pct,
        benefit_cost_ratio             = bcr,
        cash_flows                     = cash_flows,
        sensitivity                    = sensitivity,
        score                          = score,
        recommendation                 = recommendation,
        score_breakdown                = score_bd,
        analysis_years                 = n_years,
        discount_rate                  = r,
        peak_power_kWp                 = investment.peak_power_kWp,
        battery_capacity_kWh           = investment.battery_capacity_kWh,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRINT SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def print_economics_summary(result: EconomicsResult) -> None:
    """Pretty-print the complete economic assessment."""
    SEP  = "─" * 72
    SEP2 = "─" * 56
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

    print(f"\n{'═'*72}")
    print(f"  TECHNO-ECONOMIC ASSESSMENT")
    print(f"  System: {result.peak_power_kWp} kWp PV"
          + (f"  +  {result.battery_capacity_kWh} kWh battery"
             if result.battery_capacity_kWh > 0 else ""))
    print(f"{'═'*72}")

    # ── Investment ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  INVESTMENT (CAPEX)")
    print(SEP)
    print(f"  PV system              : {result.pv_capex_eur:>12,.0f}  €")
    if result.battery_capacity_kWh > 0:
        print(f"  Battery system         : {result.battery_capex_eur:>12,.0f}  €")
    print(f"  Total CAPEX            : {result.total_capex_eur:>12,.0f}  €")
    if result.incentive_present_value_eur > 0:
        print(f"  Incentives (PV)        : {result.incentive_present_value_eur:>12,.0f}  €")
        print(f"  Net CAPEX              : {result.net_capex_after_incentives_eur:>12,.0f}  €")

    # ── Year 1 performance ────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  YEAR 1 PERFORMANCE")
    print(SEP)
    print(f"  PV production          : {result.year1_production_kWh:>10,.0f}  kWh/yr")
    print(f"  Self-consumed          : {result.year1_self_consumed_kWh:>10,.0f}  kWh/yr")
    print(f"  Self-consumption rate  : {result.year1_self_consumption_rate:>10.1%}")
    print(f"  Self-sufficiency rate  : {result.year1_self_sufficiency_rate:>10.1%}")
    print(f"  Energy saving          : {result.year1_saving_eur:>10,.0f}  €/yr")
    print(f"  Export revenue         : {result.year1_export_revenue_eur:>10,.0f}  €/yr")
    print(f"  O&M cost               : {result.year1_om_cost_eur:>10,.0f}  €/yr")
    print(f"  Net benefit (year 1)   : {result.year1_net_benefit_eur:>10,.0f}  €/yr")

    # ── Key financial KPIs ────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  KEY FINANCIAL KPIs")
    print(SEP)
    irr_str = f"{result.irr_pct:.1f}%" if result.irr_pct > -99 else "n/a (negative)"
    print(f"  NPV ({result.discount_rate:.0%} discount)     : {result.npv_eur:>12,.0f}  €")
    print(f"  IRR                    : {irr_str:>12}")
    print(f"  Simple payback         : {result.payback_simple_yr:>12.1f}  years")
    print(f"  Discounted payback     : {result.payback_discounted_yr:>12.1f}  years")
    print(f"  LCOE                   : {result.lcoe_eur_kWh:>12.4f}  €/kWh")
    print(f"  ROI (NPV/CAPEX)        : {result.roi_pct:>12.1f}  %")
    print(f"  Benefit/cost ratio     : {result.benefit_cost_ratio:>12.3f}")

    # ── Lifetime totals ───────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  LIFETIME TOTALS  ({result.analysis_years} years)")
    print(SEP)
    print(f"  PV production          : {result.lifetime_production_kWh:>12,.0f}  kWh")
    print(f"  Energy saving          : {result.lifetime_saving_eur:>12,.0f}  €")
    print(f"  Export revenue         : {result.lifetime_export_revenue_eur:>12,.0f}  €")
    print(f"  Total O&M cost         : {result.lifetime_om_cost_eur:>12,.0f}  €")
    if result.lifetime_replacement_cost_eur > 0:
        print(f"  Replacement costs      : {result.lifetime_replacement_cost_eur:>12,.0f}  €")

    # ── Cash flow table ───────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  ANNUAL CASH FLOWS")
    print(SEP)
    print(f"  {'Yr':>3} {'Saving':>8} {'Export':>7} {'Tax':>7}"
          f" {'O&M':>7} {'Repl':>6} {'Net CF':>9} {'Cum CF':>9} {'Disc CF':>8}")
    print(f"  {'':3} {'€':>8} {'€':>7} {'€':>7}"
          f" {'€':>7} {'€':>6} {'€':>9} {'€':>9} {'€':>8}")
    print(f"  {SEP2}")
    for cf in result.cash_flows:
        print(
            f"  {cf.year:>3}"
            f" {cf.energy_saving_eur:>8,.0f}"
            f" {cf.export_revenue_eur:>7,.0f}"
            f" {cf.tax_benefit_eur:>7,.0f}"
            f" {cf.om_cost_eur:>7,.0f}"
            f" {cf.replacement_cost_eur:>6,.0f}"
            f" {cf.net_cash_flow_eur:>9,.0f}"
            f" {cf.cumulative_cash_flow_eur:>9,.0f}"
            f" {cf.discounted_cash_flow_eur:>8,.0f}"
        )

    # ── Sensitivity analysis ──────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  SENSITIVITY ANALYSIS")
    print(SEP)
    print(f"  {'Scenario':<18} {'Esc':>5} {'Disc':>5}"
          f" {'NPV':>10} {'IRR':>6} {'PB':>5} {'PB disc':>7}")
    print(f"  {'':18} {'%/yr':>5} {'%':>5}"
          f" {'€':>10} {'%':>6} {'yr':>5} {'yr':>7}")
    print(f"  {SEP2}")
    for s in result.sensitivity:
        irr_s = f"{s.irr_pct:.1f}" if s.irr_pct > -99 else "n/a"
        pb_d  = f"{s.payback_discounted_yr:.1f}" if s.payback_discounted_yr < 99 else ">25"
        print(
            f"  {s.name:<18}"
            f" {s.energy_price_escalation*100:>5.1f}"
            f" {s.discount_rate*100:>5.1f}"
            f" {s.npv_eur:>10,.0f}"
            f" {irr_s:>6}"
            f" {s.payback_simple_yr:>5.1f}"
            f" {pb_d:>7}"
        )

    # ── Investment score ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  INVESTMENT SCORE")
    print(SEP)
    bar_full  = int(result.score)
    bar_half  = 1 if (result.score - bar_full) >= 0.5 else 0
    bar_empty = 10 - bar_full - bar_half
    bar = "█" * bar_full + ("▌" if bar_half else "") + "░" * bar_empty
    print(f"  Score:  {result.score:>4.1f} / 10   {bar}")
    print(f"  Verdict: {result.recommendation}")
    print(f"\n  Component breakdown:")
    labels = {
        "irr_score":             "IRR quality",
        "payback_score":         "Payback period",
        "npv_roi_score":         "NPV / CAPEX ratio",
        "self_consumption_score":"Self-consumption rate",
        "robustness_score":      "Payback robustness",
    }
    for key, label in labels.items():
        val = result.score_breakdown.get(key, 0)
        mini_bar = "█" * int(val) + ("▌" if (val - int(val)) >= 0.5 else "")
        print(f"    {label:<28} {val:.1f}/2.0  {mini_bar}")
    print(f"\n{SEP}\n")
