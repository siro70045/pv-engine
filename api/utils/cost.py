"""Shared PV CAPEX estimators (residential & non-residential).

Mirrors the tier-based functions previously living in app.py / app_school.py
so the API can return the same numbers the Streamlit wizards do, without
importing those Streamlit modules.
"""

from __future__ import annotations


def estimate_pv_cost_residential(kwp: float) -> int:
    """All-in residential PV installation cost [€]."""
    tiers = [
        (3,    2100), (6,   1800), (20,  1500),
        (50,   1250), (100, 1050), (250,  900),
        (float("inf"), 800),
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
    cost += max(800.0, kwp * 18.0)   # design + permits + grid connection
    return int(round(cost, -2))


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
    equipment = int(round(total * 0.76))
    installation = int(round(total * 0.15))
    permits = max(800, int(round(kwp * 18)))
    other = max(0, total - equipment - installation - permits)
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
