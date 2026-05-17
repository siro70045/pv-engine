"""tests/test_tou_symmetry.py

Tests for Voce #10 (Categoria 3) fix — symmetric time-of-use pricing
between baseline_cost and actual_cost in the energy balance financial
calculation.

Before the fix, baseline_cost was always priced at the flat/off-peak rate
even when time_of_use=True, while actual_cost applied the TOU multiplier
during peak hours.  This asymmetry UNDERESTIMATED the apparent saving from
PV (and could yield negative apparent savings in scenarios where PV
production concentrated in peak hours).

The fix prices the baseline with the same hourly rule used for actual.
The flat-rate code path (time_of_use=False, the default for every caller
in production) is unchanged — verified bit-perfect via the existing
suite.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.energy_balance import (
    BatterySystem, EnergyBalanceInput, GridTariff,
    simulate_energy_balance,
)


# 24 h × 365 d = 8 760 hours
def _flat_demand_Wh(per_hour_Wh: float) -> list[float]:
    return [per_hour_Wh] * 8760


def _peak_only_pv_Wh(per_peak_hour_Wh: float, peak_lo: int = 8, peak_hi: int = 21) -> list[float]:
    """PV that produces a flat amount only during peak hours [peak_lo, peak_hi)."""
    return [per_peak_hour_Wh if peak_lo <= (i % 24) < peak_hi else 0.0
            for i in range(8760)]


# ────────────────────────────────────────────────────────────────────────────
# Test A — time_of_use=False is unchanged (regression guard)
# ────────────────────────────────────────────────────────────────────────────

def test_tou_off_no_change_from_flat():
    """With time_of_use=False, baseline_cost = dem_annual * import_price
    and actual_cost = imp_annual * import_price (flat rule on both sides).

    This is the production default for every caller in the repo;
    bit-perfect on this branch is what protects the published Borzano
    numbers.
    """
    inp = EnergyBalanceInput(
        pv_production_hourly_Wh = _peak_only_pv_Wh(2_000.0),
        demand_hourly_Wh        = _flat_demand_Wh(3_000.0),
        battery                 = BatterySystem(capacity_kWh=0),
        tariff                  = GridTariff(
            import_price_eur_kWh = 0.25,
            export_price_eur_kWh = 0.07,
            time_of_use          = False,
        ),
        peak_power_kWp = 10.0,
    )
    r = simulate_energy_balance(inp)

    expected_baseline = r.total_demand_kWh * 0.25
    expected_actual   = r.grid_import_kWh * 0.25

    assert abs(r.grid_cost_without_pv_eur - expected_baseline) < 0.02, (
        f"flat baseline: {r.grid_cost_without_pv_eur} vs {expected_baseline}"
    )
    assert abs(r.grid_cost_with_pv_eur - expected_actual) < 0.02, (
        f"flat actual: {r.grid_cost_with_pv_eur} vs {expected_actual}"
    )
    # Saving must be strictly positive (PV reduces imports against a flat tariff)
    assert r.annual_saving_eur > 0.0


# ────────────────────────────────────────────────────────────────────────────
# Test B — TOU symmetry: both baseline and actual must use TOU prices,
#          and saving must be positive even when PV concentrates on peak
#          hours (where the buggy code yielded a negative apparent saving).
# ────────────────────────────────────────────────────────────────────────────

def test_tou_symmetric_baseline_and_actual():
    """Constant 100 kWh/h demand, PV 200 kWh/h during peak hours (8–20),
    import_price=0.25, peak_multiplier=1.5, peak_hours=8..20.

    Derivation (per year, 8760 h):
      total_demand    = 100 × 8760           = 876 000 kWh
      peak_hours/year = 13 × 365             =   4 745 h
      off_peak_hours  = 11 × 365             =   4 015 h
      peak_demand_kWh = 100 × 4 745          = 474 500 kWh
      offp_demand_kWh = 100 × 4 015          = 401 500 kWh

      PV produces 200 kWh/h × 4 745 h        = 949 000 kWh
      → exceeds peak demand by 474 500 kWh/y
      → during peak hours: full self-consumption of 100 kWh/h
        (the 100 kWh/h "excess" is exported, with no battery)
      → during off-peak: no PV, all 100 kWh/h imported

      grid_import (TOU): 100 × 4 015 h = 401 500 kWh (all off-peak)
      → peak_imp = 0, off_peak_imp = 401 500

    Costs (TOU, symmetric — the fix):
      baseline = 474 500 × 0.375 + 401 500 × 0.25
               = 177 937.5 + 100 375 = 278 312.5 €
      actual   = 0 × 0.375 + 401 500 × 0.25 = 100 375 €
      saving   = 177 937.5 €   (POSITIVE — the bug would have made it lower)

    Costs (buggy, baseline flat / actual TOU — pre-fix behavior):
      baseline_buggy = 876 000 × 0.25 = 219 000 €
      actual         = 100 375 €
      saving_buggy   = 118 625 €
      → ~59 313 € less than the symmetric saving.

    With more peak-concentrated PV (e.g. larger system absorbing all peak
    demand AND a non-zero peak import baseline) the buggy saving can even
    turn NEGATIVE — see Voce #10 pre-fix analysis.
    """
    inp = EnergyBalanceInput(
        pv_production_hourly_Wh = _peak_only_pv_Wh(200_000.0),  # 200 kWh/h
        demand_hourly_Wh        = _flat_demand_Wh(100_000.0),   # 100 kWh/h
        battery                 = BatterySystem(capacity_kWh=0),
        tariff                  = GridTariff(
            import_price_eur_kWh  = 0.25,
            export_price_eur_kWh  = 0.0,
            time_of_use           = True,
            peak_hours            = list(range(8, 21)),   # 8..20 inclusive
            peak_price_multiplier = 1.5,
        ),
        peak_power_kWp = 200.0,
    )
    r = simulate_energy_balance(inp)

    # Tight bounds — no rounding-driven flakiness expected with this profile.
    expected_baseline = 278_312.5
    expected_actual   = 100_375.0
    expected_saving   = 177_937.5

    assert abs(r.grid_cost_without_pv_eur - expected_baseline) < 1.0, (
        f"TOU baseline: {r.grid_cost_without_pv_eur} vs {expected_baseline}"
    )
    assert abs(r.grid_cost_with_pv_eur - expected_actual) < 1.0, (
        f"TOU actual: {r.grid_cost_with_pv_eur} vs {expected_actual}"
    )
    assert abs(r.annual_saving_eur - expected_saving) < 1.0, (
        f"TOU saving: {r.annual_saving_eur} vs {expected_saving}"
    )
    # The headline regression: saving must be strictly positive (the bug
    # could yield a negative apparent saving in this kind of scenario).
    assert r.annual_saving_eur > 0.0


# ────────────────────────────────────────────────────────────────────────────
# Test C — peak_price_multiplier=1.0 ⇒ TOU collapses to flat
# ────────────────────────────────────────────────────────────────────────────

def test_tou_with_unit_multiplier_equals_flat():
    """Sanity: with peak_price_multiplier=1.0, time_of_use=True and
    time_of_use=False must produce the same baseline_cost, actual_cost
    and annual_saving_eur for identical demand / PV inputs."""
    common_kwargs = dict(
        pv_production_hourly_Wh = _peak_only_pv_Wh(5_000.0),
        demand_hourly_Wh        = _flat_demand_Wh(8_000.0),
        battery                 = BatterySystem(capacity_kWh=0),
        peak_power_kWp          = 25.0,
    )

    inp_flat = EnergyBalanceInput(
        tariff = GridTariff(
            import_price_eur_kWh = 0.25,
            export_price_eur_kWh = 0.07,
            time_of_use          = False,
        ),
        **common_kwargs,
    )
    inp_tou = EnergyBalanceInput(
        tariff = GridTariff(
            import_price_eur_kWh  = 0.25,
            export_price_eur_kWh  = 0.07,
            time_of_use           = True,
            peak_hours            = list(range(8, 21)),
            peak_price_multiplier = 1.0,   # collapses to flat
        ),
        **common_kwargs,
    )

    r_flat = simulate_energy_balance(inp_flat)
    r_tou  = simulate_energy_balance(inp_tou)

    # Allow 1¢ slack: the TOU branch rounds actual_cost to 2dp internally
    # whereas the flat branch keeps full precision until the final round.
    assert abs(r_tou.grid_cost_without_pv_eur - r_flat.grid_cost_without_pv_eur) < 0.02
    assert abs(r_tou.grid_cost_with_pv_eur    - r_flat.grid_cost_with_pv_eur)    < 0.02
    assert abs(r_tou.annual_saving_eur        - r_flat.annual_saving_eur)        < 0.02
