"""Shared PV CAPEX estimators (residential & non-residential).

Mirrors the tier-based functions previously living in app.py / app_school.py
so the API can return the same numbers the Streamlit wizards do, without
importing those Streamlit modules.
"""

from __future__ import annotations


# Residential turnkey cost = FIXED + VARIABLE × kWp, VAT 10 % included, no battery.
# Calibrated 20 nov 2026 on: GSE survey for IEA PVPS National Survey Report Italy
# 2024 (rooftop < 10 kW: 1,39 €/W excl. VAT, 1,53 €/W incl.), regional price list
# Piemonte 2026 (inverter almost entirely fixed), public spending caps (Reddito
# Energetico 2.000 € + 1.500 €/kW incl. 10 years of services = upper bound; CACER)
# and installer/comparator quotes as plausibility checks.
# FIXED: survey, design, base inverter, Modello Unico/grid connection, testing,
#        roof safety. VARIABLE: modules, mounting, cables, labour, margin.
# The previous tiers (2.100/1.800 €/kWp) were already all-in prices to which
# 2.300 € of fixed costs were added again (inverter and permits counted twice):
# about +31 % at 3 kWp and +33 % at 6 kWp versus this curve.
RESIDENTIAL_FIXED_EUR: dict[str, float] = {"low": 1_800.0, "central": 2_500.0, "high": 3_200.0}
RESIDENTIAL_EUR_PER_KWP: dict[str, float] = {"low": 1_000.0, "central": 1_150.0, "high": 1_450.0}


def _residential_cost(kwp: float, scenario: str) -> int:
    cost = RESIDENTIAL_FIXED_EUR[scenario] + RESIDENTIAL_EUR_PER_KWP[scenario] * float(kwp)
    return int(round(cost, -2))


def estimate_pv_cost_residential(kwp: float) -> int:
    """Central turnkey residential PV cost [€], VAT included, no battery."""
    return _residential_cost(kwp, "central")


def estimate_pv_cost_residential_range(kwp: float) -> tuple[int, int]:
    """(low, high) band of the residential cost for a quote comparison [€]."""
    return _residential_cost(kwp, "low"), _residential_cost(kwp, "high")


def estimate_pv_cost_school(kwp: float) -> tuple[int, int]:
    """Returns (total_capex_eur, structural_assessment_eur) for public buildings."""
    tiers = [
        (20,  900), (50,  820), (100, 780),
        (250, 720), (float("inf"), 670),
    ]
    cost = 0.0
    remaining = float(kwp)
    prev = 0.0
    for thresh, eur_kwp in tiers:
        if remaining <= 0:
            break
        in_tier = min(remaining, thresh - prev)
        cost += in_tier * eur_kwp
        remaining -= in_tier
        prev = thresh
    structural = max(2000.0, kwp * 15.0)
    cost += structural
    return int(round(cost, -2)), int(round(structural, -2))


def estimate_pv_cost(kwp: float, building_type: str) -> dict:
    """Unified estimator returning the same breakdown shape for both flavours."""
    bt = (building_type or "").lower()
    if bt == "school" or bt == "public":
        total, structural = estimate_pv_cost_school(kwp)
        equipment = int(round(total - structural - kwp * 18))
        return {
            "total_cost_eur":   total,
            "cost_per_kWp":     round(total / kwp, 1) if kwp > 0 else 0.0,
            "breakdown": {
                "equipment":           max(0, equipment),
                "installation":        int(round(kwp * 100)),
                "design_and_permits":  int(round(kwp * 18)),
                "structural":          structural,
                "other":               0,
            },
        }
    total = estimate_pv_cost_residential(kwp)
    fixed = int(RESIDENTIAL_FIXED_EUR["central"])
    variable = max(0, total - fixed)
    equipment = int(round(variable * 0.80))
    installation = variable - equipment
    permits = 800                       # part of the fixed cost
    other = max(0, fixed - permits)     # inverter, connection, testing, safety
    return {
        "total_cost_eur":   total,
        "cost_per_kWp":     round(total / kwp, 1) if kwp > 0 else 0.0,
        "breakdown": {
            "equipment":           equipment,
            "installation":        installation,
            "design_and_permits":  permits,
            "other":               other,
        },
    }


def auto_scenarios(
    annual_demand_kWh: float,
    kwp_max: float,
    min_kwp_floor: float = 1.5,
    yield_kwh_per_kwp: float = 1200.0,
) -> dict:
    """Three-scenario sizing identical to Streamlit wizards (minimal/recommended/maximum)."""
    Y = float(yield_kwh_per_kwp) or 1200.0
    mn = max(min_kwp_floor, round(annual_demand_kWh * 0.35 / Y * 2) / 2)
    op = min(kwp_max, max(mn, round(annual_demand_kWh * 0.75 / Y * 2) / 2))
    mx = max(op, kwp_max)
    mn = min(mn, mx)
    op = max(mn, min(op, mx))
    return {"minimal": mn, "recommended": op, "maximum": mx}
