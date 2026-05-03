"""
tests/test_multi_footprint.py

Multi-footprint PV simulation tests for engine/solar.py and engine/scenarios.py.

Verifies:
  1. Single-footprint result matches legacy simulate_pv() within 0.01%
     (backward compatibility — the most important guarantee).
  2. Three identical footprints produce 3× the energy of one footprint.
  3. Mixed orientations (S, E, W) sum correctly and South wins.
  4. ScenarioConfig without pv_footprints behaves identically to before.

All inputs are synthetic — no PVGIS network access required.
"""

import sys
import os
import math
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from engine.pvgis_client import (
    HourlyRecord, MonthlyClimate, PVGISLocation, TMYResult,
)
from engine.solar import (
    PVSystem, SystemLosses, BuildingFootprint, FootprintResult,
    simulate_pv, simulate_pv_multi,
)
from engine.scenarios import (
    ScenarioConfig, compare_scenarios,
    SCENARIO_BASELINE, SCENARIO_SOLO_PV,
)
from engine.energy_vectors import (
    BuildingEnergyProfile, GasBoilerSystem, FuelType,
    DEFAULT_PRICES,
)
from engine.energy_balance import BatterySystem, GridTariff
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters, ItalianIncentives,
)


# ── Synthetic TMY ─────────────────────────────────────────────────────────────
DAYS_M        = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
T_EXT_MONTHLY = [2.0, 4.0, 8.5, 13.0, 18.0, 22.5, 25.5, 25.0, 20.5, 14.5, 8.0, 3.0]


def _make_synthetic_tmy(
    lat: float = 44.5,
    lon: float = 0.0,
    elev_m: float = 200.0,
) -> TMYResult:
    """
    Build an 8 760-hour synthetic TMY suitable for offline tests.

    GHI/DNI/DHI follow a simple sinusoidal daytime curve peaking at
    12:00 UTC; T2m is the monthly mean from `T_EXT_MONTHLY`; wind
    speed is a flat 2 m/s. No horizon profile (clear horizon) so all
    tests focus on aggregation behaviour rather than shading.

    `lon=0.0` is deliberate: it aligns the synthetic-irradiance peak
    (12:00 UTC) with local solar noon, so East- and West-facing panels
    receive symmetric direct-beam timing — required for the mixed-
    orientation symmetry assertion in `test_mixed_orientations_aggregation`.
    """
    records: list[HourlyRecord] = []
    for m, days in enumerate(DAYS_M, start=1):
        t_amb = T_EXT_MONTHLY[m - 1]
        for d in range(1, days + 1):
            for h in range(24):
                # Daylight bell roughly 6:00 – 18:00 (UTC), peak ~12:00.
                if 6 <= h <= 18:
                    sun_factor = math.sin(math.pi * (h - 6) / 12)
                    ghi = max(0.0, 800.0 * sun_factor)
                    dni = max(0.0, 700.0 * sun_factor)
                    dhi = max(0.0, 200.0 * sun_factor)
                else:
                    ghi = dni = dhi = 0.0
                records.append(HourlyRecord(
                    month = m, day = d, hour = h,
                    T2m   = t_amb,
                    RH    = 60.0,
                    G_h   = ghi,
                    Gb_n  = dni,
                    Gd_h  = dhi,
                    IR_h  = 0.0,
                    WS10m = 2.0,
                    WD10m = 180.0,
                    SP    = 101325.0,
                ))
    assert len(records) == 8760

    # Monthly aggregates: GHI/DNI/DHI per-month sums, mean temperatures.
    ghi_m: list[float] = [0.0] * 12
    dni_m: list[float] = [0.0] * 12
    dhi_m: list[float] = [0.0] * 12
    for r in records:
        ghi_m[r.month - 1] += r.G_h  / 1000.0   # Wh → kWh
        dni_m[r.month - 1] += r.Gb_n / 1000.0
        dhi_m[r.month - 1] += r.Gd_h / 1000.0

    monthly = MonthlyClimate(
        T_avg       = list(T_EXT_MONTHLY),
        T_min       = [t - 5.0 for t in T_EXT_MONTHLY],
        T_max       = [t + 5.0 for t in T_EXT_MONTHLY],
        GHI_monthly = ghi_m,
        DNI_monthly = dni_m,
        DHI_monthly = dhi_m,
        WS_avg      = [2.0] * 12,
        RH_avg      = [60.0] * 12,
    )
    location = PVGISLocation(
        latitude        = lat,
        longitude       = lon,
        elevation_m     = elev_m,
        country         = "IT",
        dataset         = "PVGIS-SARAH3",
        horizon_profile = [],
    )
    return TMYResult(location=location, hourly=records, monthly=monthly)


# Shared synthetic TMY for all tests in this module
TMY = _make_synthetic_tmy()


def _close(a: float, b: float, tol_pct: float) -> bool:
    """True if |a-b| / max(|b|,1) < tol_pct/100."""
    if b == 0:
        return abs(a) < tol_pct / 100.0
    return abs(a - b) / abs(b) < tol_pct / 100.0


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

def test_single_footprint_matches_legacy_simulate_pv():
    """One footprint via simulate_pv_multi must match legacy simulate_pv()
       within 0.01% on annual production AND every monthly bucket."""
    template = PVSystem(
        peak_power_kWp = 50.0,
        tilt_deg       = 15.0,
        azimuth_deg    = 180.0,
    )
    # 250 m² × 1.0 usable × 0.20 kW/m² = 50 kWp — matches the template.
    fp = BuildingFootprint(
        name            = "Test single",
        area_m2         = 250.0,
        azimuth_deg     = 180.0,
        tilt_deg        = 15.0,
        usable_fraction = 1.0,
    )
    assert _close(fp.peak_power_kWp, 50.0, 0.001)

    legacy = simulate_pv(TMY, template)
    multi  = simulate_pv_multi(TMY, [fp], template)

    # Annual must agree to 0.01%
    assert _close(multi.production_annual_kWh, legacy.production_annual_kWh, 0.01), (
        f"annual mismatch: legacy={legacy.production_annual_kWh:.4f}, "
        f"multi={multi.production_annual_kWh:.4f}"
    )
    # Each month must agree to 0.01% (allowing absolute slack of 0.1 kWh
    # because the legacy aggregator rounds monthly values to 1 decimal,
    # so two independent rounds can differ by up to 0.1 kWh)
    for m in range(12):
        a, b = multi.production_monthly_kWh[m], legacy.production_monthly_kWh[m]
        diff = abs(a - b)
        assert diff < max(0.1, abs(b) * 0.0001), (
            f"month {m+1}: legacy={b:.2f}, multi={a:.2f}, diff={diff:.4f}"
        )

    # Footprints detail: 1 entry, 100% share
    assert len(multi.footprints_detail) == 1
    assert _close(multi.footprints_detail[0].share_pct, 100.0, 0.01)
    # Legacy has empty footprints_detail (back-compat)
    assert legacy.footprints_detail == []


def test_three_identical_footprints_triple_production():
    """Three identical footprints aggregate to 3× the production of one."""
    template = PVSystem(peak_power_kWp=20.0, tilt_deg=20.0, azimuth_deg=180.0)
    fp = BuildingFootprint(
        name            = "Identical",
        area_m2         = 100.0,
        azimuth_deg     = 180.0,
        tilt_deg        = 20.0,
        usable_fraction = 1.0,
    )

    one   = simulate_pv_multi(TMY, [fp], template)
    three = simulate_pv_multi(TMY, [fp, fp, fp], template)

    # 3× annual energy (within 0.5% tolerance per spec)
    assert _close(three.production_annual_kWh, 3 * one.production_annual_kWh, 0.5), (
        f"3× check: 1fp={one.production_annual_kWh:.1f}, "
        f"3fp={three.production_annual_kWh:.1f}, "
        f"ratio={three.production_annual_kWh / max(one.production_annual_kWh, 1):.4f}"
    )

    # 3 footprint entries, ~33.33% each
    assert len(three.footprints_detail) == 3
    for det in three.footprints_detail:
        assert abs(det.share_pct - 100.0 / 3.0) < 0.5, (
            f"share_pct ≈ 33.33%: {det.share_pct:.2f}"
        )
    # Specific yield identical (same orientation/tilt → same kWh/kWp)
    sy = [d.specific_yield_kWh_kWp for d in three.footprints_detail]
    assert max(sy) - min(sy) < 1.0, f"specific yields should match: {sy}"


def test_mixed_orientations_aggregation():
    """Mixed S+E+W footprints: South wins, E ≈ W, total = sum of parts."""
    template = PVSystem(peak_power_kWp=20.0, tilt_deg=15.0, azimuth_deg=180.0)
    fp_S = BuildingFootprint("Sud",   100.0, azimuth_deg=180, tilt_deg=15, usable_fraction=1.0)
    fp_E = BuildingFootprint("Est",   100.0, azimuth_deg= 90, tilt_deg=15, usable_fraction=1.0)
    fp_W = BuildingFootprint("Ovest", 100.0, azimuth_deg=270, tilt_deg=15, usable_fraction=1.0)

    result = simulate_pv_multi(TMY, [fp_S, fp_E, fp_W], template)

    detail_by_name = {d.name: d for d in result.footprints_detail}
    p_S = detail_by_name["Sud"].production_annual_kWh
    p_E = detail_by_name["Est"].production_annual_kWh
    p_W = detail_by_name["Ovest"].production_annual_kWh

    # South must outperform East and West
    assert p_S > p_E, f"S > E: {p_S:.0f} vs {p_E:.0f}"
    assert p_S > p_W, f"S > W: {p_S:.0f} vs {p_W:.0f}"
    # E and W roughly symmetric. The synthetic-irradiance peak is at
    # 12:00 UTC (lon=0) but the actual solar noon shifts by ±15 min
    # over the year (equation-of-time), introducing a structural
    # E/W asymmetry of ~5–8% even with a symmetric input curve.
    # The 10% tolerance is qualitative — the assertion guards "roughly
    # symmetric", not a numeric equality.
    assert _close(p_E, p_W, 10.0), (
        f"E ≈ W within 10%: E={p_E:.0f}, W={p_W:.0f}, "
        f"diff={abs(p_E-p_W)/max(p_W,1)*100:.2f}%"
    )
    # Sum check (within 0.01%; exact in theory, allow tiny rounding)
    sum_parts = p_S + p_E + p_W
    assert _close(result.production_annual_kWh, sum_parts, 0.01), (
        f"total = sum(parts): total={result.production_annual_kWh:.2f}, "
        f"sum_parts={sum_parts:.2f}"
    )
    # Shares add to ~100%
    total_share = sum(d.share_pct for d in result.footprints_detail)
    assert abs(total_share - 100.0) < 0.5, f"shares sum ≈ 100%: {total_share:.2f}"


def test_scenario_config_legacy_path_unchanged():
    """ScenarioConfig without pv_footprints must produce no warnings and
       the same result structure as before the refactor."""
    building = BuildingEnergyProfile(
        electricity_kwh_annual       = 80000.0,
        electricity_price            = DEFAULT_PRICES[FuelType.ELECTRICITY],
        gas_consumption_annual_units = 0.0,
        has_gas                      = False,
    )
    pv_h  = [800.0 if 8 <= (i % 24) < 17 else 0.0 for i in range(8760)]
    dem_h = [4000.0] * 8760
    t_ext = []
    for t, days in zip(T_EXT_MONTHLY, DAYS_M):
        t_ext.extend([t] * (days * 24))

    cfg = ScenarioConfig(
        building_profile        = building,
        pv_production_hourly_Wh = pv_h,
        base_demand_hourly_Wh   = dem_h,
        t_ext_hourly            = t_ext,
        pv_peak_kWp             = 50.0,
        # pv_footprints intentionally omitted → legacy path
        battery                 = BatterySystem(capacity_kWh=0),
        tariff                  = GridTariff(
            import_price_eur_kWh=0.25, export_price_eur_kWh=0.07,
        ),
        investment_pv           = InvestmentCost(
            pv_cost_eur_kWp=950, peak_power_kWp=50.0,
        ),
        opex_pv                 = OperatingCost(om_pct_of_capex_per_yr=1.0),
        financial               = FinancialParameters(
            discount_rate=0.05,
            energy_price_escalation=0.025,
            analysis_years=25,
        ),
        incentives_pv           = ItalianIncentives(),
        incentives_hp           = ItalianIncentives(),
        run_solo_pv             = True,
        run_solo_hp             = False,
        run_pv_hp               = False,
        run_pv_hp_bivalente     = False,
    )

    # total_pv_kWp must equal pv_peak_kWp in legacy mode
    assert cfg.total_pv_kWp == 50.0
    # pv_footprints default is None
    assert cfg.pv_footprints is None

    # No warning about pv_footprints should be emitted
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        comparison = compare_scenarios(cfg, lifetime_years=25)
        footprint_warnings = [
            w for w in caught if "pv_footprints" in str(w.message)
        ]
        assert footprint_warnings == [], (
            f"unexpected pv_footprints warning: {footprint_warnings}"
        )

    # Sanity: scenarios computed, baseline plus solo PV present
    assert SCENARIO_BASELINE in comparison.scenarios
    assert SCENARIO_SOLO_PV in comparison.scenarios
    pv = comparison.scenarios[SCENARIO_SOLO_PV]
    assert pv.pv_production_kWh > 0
    assert pv.capex_eur > 0
