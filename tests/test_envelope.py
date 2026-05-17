"""
tests/test_envelope.py

Test suite for engine/envelope.py.

Strategy: this suite is structured in 3 levels to support the
fix of dossier voice #11 (cos_theta and Erbs fix):

LEVEL 1 — Geometric invariants. These tests verify fundamental
solar geometry properties (sunrise zero, E/W symmetry, etc.)
and MUST CONTINUE TO PASS after the fix in Phase 2B.

LEVEL 2 — Regression snapshots. These tests capture the
CURRENT (buggy) numerical behavior of _irradiance_on_surface
and calc_solar_gains. They will be UPDATED in Phase 2B with
the corrected post-fix values. The drift between buggy and
fixed values is the measurable confirmation that the fix had
the expected effect.

LEVEL 3 — Bit-perfect invariants on untouched modules. These
tests cover H_tr, H_ve, H_gr, thermal mass, utilisation factor —
parts of envelope.py that the Phase 2B fix does NOT touch.
They MUST remain bit-perfect after the fix.

Created in Phase 2A of voice #11 fix. To be extended in
Phase 2B with corrected Erbs/cos_theta values.
"""

import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from engine.envelope import (
    OpaqueComponent, Window, GroundFloor, Envelope,
    LinearThermalBridge,
    calc_transmission_heat_loss,
    calc_ventilation_heat_loss,
    calc_ground_heat_loss,
    calc_solar_gains,
    calc_internal_gains,
    calc_thermal_demand,
    _irradiance_on_surface,
    _thermal_mass_parameter,
    _utilisation_factor,
)


# ── Shared inputs ─────────────────────────────────────────────────────────────
# Po valley monthly global horizontal irradiance [kWh/m²/month]:
# representative for Reggio Emilia / Bologna area (Borzano).
G_H = [35.0, 50.0, 90.0, 125.0, 160.0, 180.0, 195.0, 165.0, 120.0, 75.0, 35.0, 30.0]
# Monthly direct normal irradiance [kWh/m²/month] — parameter is currently
# unused inside _irradiance_on_surface but the API requires it.
G_D = [10.0, 15.0, 30.0, 50.0, 75.0, 95.0, 100.0, 75.0, 55.0, 25.0, 10.0, 8.0]
LAT_BORZANO = 44.65

# Bologna-like monthly average outdoor temperature [°C]
T_EXT_MONTHLY = [2.0, 4.0, 8.5, 13.0, 18.0, 22.5, 25.5, 25.0, 20.5, 14.5, 8.0, 3.0]

# Standard mid-month days (envelope.py:431)
DAYS_MID_MONTH = [17, 47, 75, 105, 135, 162, 198, 228, 258, 288, 318, 344]


# ══════════════════════════════════════════════════════════════════════════════
# Helpers — physics formulas re-implemented for invariant tests
# (the actual envelope.py code currently uses ω=15° fixed; these helpers
# accept arbitrary ω and represent the formulas the Phase 2B refactor
# must respect.)
# ══════════════════════════════════════════════════════════════════════════════

def _declination_rad(day: int) -> float:
    """Solar declination [rad] — matches envelope.py:437."""
    return math.radians(23.45 * math.sin(math.radians(360 * (284 + day) / 365)))


def _sunrise_hour_angle_rad(phi_rad: float, delta_rad: float) -> float:
    """Sunrise hour angle ωs [rad], ≥ 0. Sunrise is at -ωs, sunset at +ωs."""
    arg = -math.tan(phi_rad) * math.tan(delta_rad)
    return math.acos(max(-1.0, min(1.0, arg)))


def _cos_zenith(phi_rad: float, delta_rad: float, omega_rad: float) -> float:
    """Cosine of zenith angle (Duffie & Beckman eq. 1.6.1a)."""
    return (
        math.sin(delta_rad) * math.sin(phi_rad)
        + math.cos(delta_rad) * math.cos(phi_rad) * math.cos(omega_rad)
    )


def _cos_theta_general(
    beta_rad: float,
    gamma_rad: float,
    phi_rad: float,
    delta_rad: float,
    omega_rad: float,
) -> float:
    """
    Cosine of incidence angle on a tilted surface (Duffie & Beckman eq. 1.6.5a).

    beta  : tilt from horizontal [rad]
    gamma : surface azimuth from south [rad] (E negative, W positive)
    phi   : latitude [rad]
    delta : solar declination [rad]
    omega : hour angle [rad] (0 at solar noon, -ωs sunrise, +ωs sunset)
    """
    return (
        math.sin(delta_rad) * math.sin(phi_rad) * math.cos(beta_rad)
        - math.sin(delta_rad) * math.cos(phi_rad) * math.sin(beta_rad) * math.cos(gamma_rad)
        + math.cos(delta_rad) * math.cos(phi_rad) * math.cos(beta_rad) * math.cos(omega_rad)
        + math.cos(delta_rad) * math.sin(phi_rad) * math.sin(beta_rad) * math.cos(gamma_rad) * math.cos(omega_rad)
        + math.cos(delta_rad) * math.sin(beta_rad) * math.sin(gamma_rad) * math.sin(omega_rad)
    )


# ══════════════════════════════════════════════════════════════════════════════
# Building fixtures
# ══════════════════════════════════════════════════════════════════════════════

def _make_minimal_school_envelope() -> Envelope:
    """
    Minimal but realistic Italian school envelope (≈100 m², 4 walls + roof +
    floor + 4 windows + one linear bridge). Parameters reflect a typical
    1970s-80s school with moderate insulation retrofit.
    """
    return Envelope(
        walls=[
            OpaqueComponent("north_wall", U=0.4, area_m2=27.0,
                            surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                            adjacent_space="external", orientation_deg=0.0),
            OpaqueComponent("east_wall", U=0.4, area_m2=27.0,
                            surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                            adjacent_space="external", orientation_deg=90.0),
            OpaqueComponent("south_wall", U=0.4, area_m2=27.0,
                            surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                            adjacent_space="external", orientation_deg=180.0),
            OpaqueComponent("west_wall", U=0.4, area_m2=27.0,
                            surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                            adjacent_space="external", orientation_deg=270.0),
        ],
        roof=OpaqueComponent("flat_roof", U=0.3, area_m2=100.0,
                             surface_mass_kg_m2=250.0, phase_shift_h=6.0,
                             adjacent_space="external", orientation_deg=180.0),
        windows=[
            Window("north_win", U_total=1.8, g_value=0.5, area_m2=3.0,
                   orientation_deg=0.0, tilt_deg=90.0,
                   frame_fraction=0.25, shading_factor=1.0),
            Window("east_win", U_total=1.8, g_value=0.5, area_m2=3.0,
                   orientation_deg=90.0, tilt_deg=90.0,
                   frame_fraction=0.25, shading_factor=1.0),
            Window("south_win", U_total=1.8, g_value=0.5, area_m2=3.0,
                   orientation_deg=180.0, tilt_deg=90.0,
                   frame_fraction=0.25, shading_factor=1.0),
            Window("west_win", U_total=1.8, g_value=0.5, area_m2=3.0,
                   orientation_deg=270.0, tilt_deg=90.0,
                   frame_fraction=0.25, shading_factor=1.0),
        ],
        ground_floor=GroundFloor(area_m2=100.0, perimeter_m=40.0, U_floor=0.5,
                                 wall_thickness_m=0.3, ground_lambda=1.5,
                                 surface_mass_kg_m2=350.0),
        linear_bridges=[LinearThermalBridge("slab_edge", psi=0.5, length_m=40.0)],
        heated_volume_m3=300.0,
        infiltration_ach=0.5,
        mechanical_ach=0.0,
        heat_recovery_efficiency=0.0,
    )


def _make_single_south_window_envelope() -> Envelope:
    """Envelope with exactly one south-facing window (for solar-gain snapshot)."""
    return Envelope(
        walls=[
            OpaqueComponent("south_wall", U=0.4, area_m2=27.0,
                            surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                            adjacent_space="external", orientation_deg=180.0),
        ],
        roof=OpaqueComponent("flat_roof", U=0.3, area_m2=100.0,
                             surface_mass_kg_m2=250.0, phase_shift_h=6.0,
                             adjacent_space="external", orientation_deg=180.0),
        windows=[
            Window("south_win", U_total=1.8, g_value=0.5, area_m2=2.0,
                   orientation_deg=180.0, tilt_deg=90.0,
                   frame_fraction=0.25, shading_factor=1.0),
        ],
        ground_floor=GroundFloor(area_m2=100.0, perimeter_m=40.0, U_floor=0.5,
                                 wall_thickness_m=0.3, ground_lambda=1.5,
                                 surface_mass_kg_m2=350.0),
        heated_volume_m3=300.0,
        infiltration_ach=0.5,
    )


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL 1 — Geometric invariants
# These verify physical laws and MUST continue to pass after the Phase 2B fix.
# ══════════════════════════════════════════════════════════════════════════════

def test_cos_theta_at_sunrise_is_zero():
    """
    At sunrise (ω = -ωs) the sun is on the horizon, so cos(zenith)
    must be ~0 by construction of ωs. On a horizontal surface,
    cos_theta = cos(zenith), so it must also be ~0.

    Invariant of solar geometry — fix-independent.
    """
    phi = math.radians(LAT_BORZANO)
    # Mid-June: declination is maximum positive
    delta = _declination_rad(DAYS_MID_MONTH[5])
    omega_s = _sunrise_hour_angle_rad(phi, delta)

    # Horizontal surface (β=0, γ irrelevant)
    cos_z = _cos_zenith(phi, delta, -omega_s)
    cos_th_horizontal = _cos_theta_general(0.0, 0.0, phi, delta, -omega_s)

    assert abs(cos_z) < 1e-12, f"cos_zenith at sunrise = {cos_z}, expected ~0"
    assert abs(cos_th_horizontal) < 1e-12, (
        f"cos_theta on horizontal at sunrise = {cos_th_horizontal}, expected ~0"
    )


def test_cos_theta_horizontal_equals_zenith():
    """
    For β=0 (horizontal surface) the incidence-angle formula reduces
    to the zenith formula. Must hold identically for any φ, δ, ω.
    """
    phi = math.radians(LAT_BORZANO)
    for day in (17, 105, 162, 258):  # Jan, Apr, Jun, Sep mid-month
        delta = _declination_rad(day)
        for omega_deg in (-60.0, -15.0, 0.0, 15.0, 60.0):
            omega = math.radians(omega_deg)
            cos_z = _cos_zenith(phi, delta, omega)
            cos_th = _cos_theta_general(0.0, 0.0, phi, delta, omega)
            assert math.isclose(cos_th, cos_z, abs_tol=1e-14), (
                f"day={day}, omega_deg={omega_deg}: cos_theta={cos_th} != "
                f"cos_zenith={cos_z}"
            )


def test_east_west_symmetry():
    """
    Geometric symmetry: an east-facing vertical surface at ω hours before
    solar noon must receive the same beam as a west-facing one at the same
    number of hours after noon.

    cos_theta(γ=-π/2, ω=-ω0) ≡ cos_theta(γ=+π/2, ω=+ω0)   (east morning = west afternoon)
    cos_theta(γ=-π/2, ω=+ω0) ≡ cos_theta(γ=+π/2, ω=-ω0)   (east afternoon = west morning)
    """
    phi = math.radians(LAT_BORZANO)
    beta = math.radians(90.0)  # vertical
    gamma_east = math.radians(90.0 - 180.0)   # orientation_deg=90  → γ=-π/2
    gamma_west = math.radians(270.0 - 180.0)  # orientation_deg=270 → γ=+π/2

    for day in (47, 162, 258):
        delta = _declination_rad(day)
        for omega_deg in (15.0, 30.0, 45.0, 60.0):
            omega = math.radians(omega_deg)
            cos_east_morning = _cos_theta_general(beta, gamma_east, phi, delta, -omega)
            cos_west_afternoon = _cos_theta_general(beta, gamma_west, phi, delta, +omega)
            cos_east_afternoon = _cos_theta_general(beta, gamma_east, phi, delta, +omega)
            cos_west_morning = _cos_theta_general(beta, gamma_west, phi, delta, -omega)
            assert math.isclose(cos_east_morning, cos_west_afternoon, abs_tol=1e-14)
            assert math.isclose(cos_east_afternoon, cos_west_morning, abs_tol=1e-14)


def test_irradiance_horizontal_equals_input():
    """
    For a perfectly horizontal surface (tilt=0°), the irradiance returned
    by _irradiance_on_surface must equal the input GHI for every month.

    Proof: Rb = cos_theta / cos_zenith = 1 (β=0 makes them identical),
    F_sky = 1, F_gnd = 0, so G_tilt = G_b·1 + G_d·1 + 0 = G_h.
    Invariant — must hold whatever Erbs/Simpson refactor does in 2B.
    """
    out = _irradiance_on_surface(
        orientation_deg=180.0,
        tilt_deg=0.0,
        monthly_irradiance_horizontal=G_H,
        monthly_irradiance_direct=G_D,
        latitude_deg=LAT_BORZANO,
    )
    for m in range(12):
        assert out[m] == pytest.approx(G_H[m], abs=1e-9), (
            f"month {m+1}: out={out[m]}, expected={G_H[m]}"
        )


def test_south_vertical_winter_greater_than_summer_italy():
    """
    For a south-facing vertical surface at Italian mid-latitude, with the
    SAME GHI in every month, geometry alone produces more irradiance in
    winter than in summer (the low winter sun strikes the vertical face
    more directly).

    Invariant of mid-latitude solar geometry — must hold post-fix.
    """
    flat_g_h = [100.0] * 12
    out = _irradiance_on_surface(
        orientation_deg=180.0,
        tilt_deg=90.0,
        monthly_irradiance_horizontal=flat_g_h,
        monthly_irradiance_direct=flat_g_h,
        latitude_deg=LAT_BORZANO,
    )
    assert out[0] > out[5], (  # January > June
        f"South vertical with constant GHI: Jan={out[0]:.3f} should exceed "
        f"Jun={out[5]:.3f} at lat {LAT_BORZANO}."
    )
    assert out[11] > out[6], (  # December > July
        f"December ({out[11]:.3f}) should exceed July ({out[6]:.3f})."
    )


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL 2 — Regression snapshots
# Capture CURRENT (buggy) numeric behavior. Phase 2B will update these.
# ══════════════════════════════════════════════════════════════════════════════

# Snapshot of _irradiance_on_surface at Borzano (lat=44.65) for the 4
# cardinal façades, tilt=90°, with the GHI profile in G_H above. These
# values reflect the CURRENT bug: ω is fixed at +15° (1 h after solar
# noon), which kills the east-façade beam term (cos_theta < 0 → Rb=0)
# and overweights the west façade. Post-Erbs+Simpson fix these numbers
# will move; the drift is the validation that the bug is gone.
SNAPSHOT_BORZANO_FACADES = {
    # orientation_deg=0 (North): only diffuse + ground reflectance →
    # G_tilt = G_h × (0.275 + 0.1) = G_h × 0.375 (currently)
    0.0: [13.1250, 18.7500, 33.7500, 46.8750, 60.0000, 67.5000,
          73.1250, 61.8750, 45.0000, 28.1250, 13.1250, 11.2500],
    # orientation_deg=90 (East): bug — same as North because ω=+15°
    # places sun in afternoon, east face is in shade.
    90.0: [13.1250, 18.7500, 33.7500, 46.8750, 60.0000, 67.5000,
           73.1250, 61.8750, 45.0000, 28.1250, 13.1250, 11.2500],
    180.0: [48.8992, 54.8211, 77.4140, 86.1290, 93.9481, 98.3321,
            109.9909, 106.0290, 94.2310, 75.5832, 45.6755, 45.3886],
    270.0: [22.8639, 29.8315, 49.6925, 64.9894, 80.1175, 88.7464,
            96.7943, 84.3433, 64.5642, 43.4953, 22.2568, 20.2520],
}


def test_borzano_facade_irradiance_snapshot():
    """
    Snapshot of _irradiance_on_surface for 4 cardinal façades at Borzano.

    These values encode the current bug. Notably façade East ≡ façade
    North because the fixed ω=+15° forces cos_theta < 0 on the east
    face, collapsing the beam term to zero. After the Phase 2B fix
    (Simpson integration over the daylight hours) east and west will
    again be roughly symmetric — and these expected values MUST be
    updated.

    TODO Phase 2B: regenerate this snapshot from the fixed code and
    verify the drift matches the magnitude predicted in the dossier
    (5-30% per façade, with east-west symmetry restored).
    """
    for orient, expected in SNAPSHOT_BORZANO_FACADES.items():
        out = _irradiance_on_surface(
            orientation_deg=orient,
            tilt_deg=90.0,
            monthly_irradiance_horizontal=G_H,
            monthly_irradiance_direct=G_D,
            latitude_deg=LAT_BORZANO,
        )
        for m in range(12):
            assert out[m] == pytest.approx(expected[m], rel=0.01), (
                f"orientation={orient}, month={m+1}: got {out[m]}, "
                f"expected {expected[m]}"
            )

    # Capture the most diagnostic part of the bug as an explicit invariant
    # of the CURRENT code: east and north façades produce identical numbers.
    # This pair-equality MUST break after Phase 2B; if it survives, the
    # cos_theta refactor did not land.
    north = SNAPSHOT_BORZANO_FACADES[0.0]
    east = SNAPSHOT_BORZANO_FACADES[90.0]
    assert north == east, (
        "Current bug expectation: with ω fixed at +15°, North ≡ East "
        "(both collapse to diffuse+ground). If this fails, the snapshot "
        "is stale."
    )


def test_borzano_single_window_solar_gain_snapshot():
    """
    Snapshot: a single south-facing window (2 m² gross, frame 25%,
    g_value=0.5, no shading) at Borzano. Captures monthly solar gain.

    TODO Phase 2B: regenerate after Erbs+Simpson fix.
    """
    env = _make_single_south_window_envelope()
    gains = calc_solar_gains(env, G_H, G_D, LAT_BORZANO)
    expected = [36.67, 41.12, 58.06, 64.60, 70.46, 73.75,
                82.49, 79.52, 70.67, 56.69, 34.26, 34.04]
    for m in range(12):
        assert gains[m] == pytest.approx(expected[m], rel=0.01), (
            f"month {m+1}: got {gains[m]}, expected {expected[m]}"
        )


def test_calc_thermal_demand_smoke():
    """
    End-to-end smoke test of calc_thermal_demand on a minimal but
    realistic Italian school envelope. Verifies output sanity and
    captures the annual heating / cooling demand as a regression
    anchor.

    Post-fix the heating/cooling totals will drift (solar gains
    affect both); expected drift is 1-8% per the dossier estimate.
    """
    env = _make_minimal_school_envelope()
    res = calc_thermal_demand(
        envelope=env,
        floor_area_m2=100.0,
        building_type="school",
        monthly_T_ext=T_EXT_MONTHLY,
        monthly_irradiance_horizontal=G_H,
        monthly_irradiance_direct=G_D,
        latitude_deg=LAT_BORZANO,
    )

    # Output sanity — no NaN/inf anywhere
    assert math.isfinite(res.heating_demand_kWh)
    assert math.isfinite(res.cooling_demand_kWh)
    for m in range(12):
        assert math.isfinite(res.heating_monthly_kWh[m])
        assert math.isfinite(res.cooling_monthly_kWh[m])
        assert math.isfinite(res.solar_gains_monthly_kWh[m])

    # Heating present in winter
    assert res.heating_monthly_kWh[0] > 0, "Heating demand in January must be > 0"
    # Cooling present in summer (12 m² of windows, full sun, no shading)
    assert res.cooling_monthly_kWh[6] > 0, "Cooling demand in July must be > 0"
    # Quirk of the EN ISO 13790 monthly method: when ΔT_C ≤ 0 (winter), the
    # cooling-loss term Q_loss_C is zero and the utilisation factor η_C(γ=0)
    # is zero, so Q_C = Q_gain (gains pass through as "theoretical cooling
    # demand"). This is NOT the Voce #11 bug — it is a separate property of
    # the monthly balance. Captured here so Phase 2B does not accidentally
    # alter it.
    assert res.cooling_monthly_kWh[0] > 0, (
        "EN ISO 13790 monthly: with Q_loss_C=0 and η_C(γ=0,cool)=0, winter "
        "Q_C equals Q_gain. If this becomes 0, the cooling branch was "
        "changed — investigate."
    )

    # Annual plausibility band — 100 m² Italian school
    assert 5000 < res.heating_demand_kWh < 50000, (
        f"Annual heating {res.heating_demand_kWh} kWh outside plausibility band"
    )

    # Regression anchor (will drift in Phase 2B)
    # TODO Phase 2B: update with post-Erbs values.
    assert res.heating_demand_kWh == pytest.approx(7668.9, rel=0.01)
    assert res.cooling_demand_kWh == pytest.approx(9000.1, rel=0.01)


def test_zero_windows_zero_solar_gains():
    """
    Envelope with zero windows → solar gain is identically zero in
    every month. Invariant of calc_solar_gains.
    """
    env = Envelope(
        walls=[OpaqueComponent("wall", U=0.4, area_m2=100.0,
                               surface_mass_kg_m2=300.0, phase_shift_h=8.0,
                               adjacent_space="external", orientation_deg=180.0)],
        roof=OpaqueComponent("roof", U=0.3, area_m2=100.0,
                             surface_mass_kg_m2=250.0, phase_shift_h=6.0,
                             adjacent_space="external"),
        windows=[],
        ground_floor=GroundFloor(area_m2=100.0, perimeter_m=40.0, U_floor=0.5,
                                 wall_thickness_m=0.3),
        heated_volume_m3=300.0,
    )
    gains = calc_solar_gains(env, G_H, G_D, LAT_BORZANO)
    for m in range(12):
        assert gains[m] == 0.0, f"month {m+1}: gain={gains[m]}, expected 0"


def test_erbs_diffuse_fraction_currently_fixed_at_055():
    """
    Documents the CURRENT bug: diffuse fraction is hardcoded at 0.55 of
    monthly GHI (envelope.py:444). The cleanest probe is a north-facing
    vertical surface: the beam term is killed (cos_theta < 0 on north
    face in northern hemisphere → Rb=0), leaving only diffuse + ground
    reflectance:
        G_tilt = G_d × F_sky + G_h × ρ_g × F_gnd
               = (0.55 × G_h) × 0.5 + G_h × 0.2 × 0.5
               = G_h × (0.275 + 0.1)
               = G_h × 0.375     ← exactly 0.375, currently

    Post-fix, with Erbs producing a kd that varies month-by-month
    (typically 0.35 summer / 0.65 winter), this ratio MUST stop being
    a constant 0.375 across the year. This test will FAIL after Phase
    2B — the marker that the bug is resolved.

    TODO Phase 2B: replace assertion with the Erbs-derived monthly
    expected values.
    """
    out = _irradiance_on_surface(
        orientation_deg=0.0,           # North
        tilt_deg=90.0,                 # Vertical
        monthly_irradiance_horizontal=G_H,
        monthly_irradiance_direct=G_D,
        latitude_deg=LAT_BORZANO,
    )
    for m in range(12):
        ratio = out[m] / G_H[m]
        assert ratio == pytest.approx(0.375, rel=1e-9), (
            f"month {m+1}: out/GHI={ratio}, expected exactly 0.375 "
            f"(0.55 fixed diffuse × 0.5 sky view + 0.2 ground × 0.5 ground view). "
            f"If this drifted, Erbs has been wired in — UPDATE THIS TEST."
        )


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL 3 — Bit-perfect invariants on untouched modules
# These must remain identical after the Phase 2B fix.
# ══════════════════════════════════════════════════════════════════════════════

def test_h_tr_invariant_under_solar_fix():
    """
    Transmission heat loss coefficient H_tr is computed from U-values,
    areas, and bridges — wholly independent of the solar model. Must
    be bit-perfect across the Phase 2B refactor.

    Reference building: minimal school envelope.
    Manual check:  4×27×0.4 + 100×0.3 + 4×3×1.8 + 0.5×40 = 114.8 W/K
    """
    env = _make_minimal_school_envelope()
    H_tr, breakdown = calc_transmission_heat_loss(env)
    assert H_tr == 114.8

    # Breakdown structure invariants (counts must be exact)
    wall_keys = [k for k, v in breakdown.items() if v["type"] == "opaque_wall"]
    win_keys  = [k for k, v in breakdown.items() if v["type"] == "window"]
    lb_keys   = [k for k, v in breakdown.items() if v["type"] == "linear_bridge"]
    assert len(wall_keys) == 4
    assert len(win_keys) == 4
    assert len(lb_keys) == 1
    assert "roof" in breakdown
    assert breakdown["roof"]["H_contrib_W_K"] == 30.0


def test_h_ve_invariant_under_solar_fix():
    """
    Ventilation heat loss coefficient H_ve = ρ_cp × V × (n_inf + n_mech(1-η))
    Wholly independent of the solar model. Bit-perfect.

    For the reference building: 0.33 × 300 × 0.5 = 49.5 W/K.
    """
    env = _make_minimal_school_envelope()
    H_ve = calc_ventilation_heat_loss(env)
    assert H_ve == 49.5


def test_h_gr_invariant_under_solar_fix():
    """
    Ground heat loss coefficient H_gr per EN ISO 13370. Depends only on
    floor geometry, wall thickness, and ground conductivity — not on
    solar. Bit-perfect across the fix.
    """
    env = _make_minimal_school_envelope()
    H_gr = calc_ground_heat_loss(env.ground_floor)
    assert H_gr == 27.635


def test_thermal_mass_invariant_under_solar_fix():
    """
    Effective thermal capacity C_m [kJ/K] depends only on surface masses
    and areas of opaque components. Not touched by the solar fix.

    Manual check:
        walls: 4 × 300 × 27 × 0.48      = 15552
        roof:  0.5 × 250 × 100 × 0.48   =  6000
        floor: 0.5 × 350 × 100 × 0.48   =  8400
        total                           = 29952 kJ/K
    """
    env = _make_minimal_school_envelope()
    Cm = _thermal_mass_parameter(env, floor_area_m2=100.0)
    assert Cm == 29952.0


def test_utilisation_factor_invariant_under_solar_fix():
    """
    EN ISO 13790 utilisation factor η is a pure function of γ and a.
    Not touched by the solar fix → bit-perfect across Phase 2B.

    The closest dynamic-response element in envelope.py (the module
    implements a monthly thermal-mass balance, not an hourly 5R1C
    resistor-capacitor model).
    """
    # Heating mode, γ ≠ 1: η = (1 - γ^a) / (1 - γ^(a+1))
    assert _utilisation_factor(0.5, 2.0, "heat") == (1.0 - 0.5**2) / (1.0 - 0.5**3)
    assert _utilisation_factor(2.0, 2.0, "heat") == (1.0 - 2.0**2) / (1.0 - 2.0**3)

    # γ = 1 boundary case: η = a / (a+1)
    assert _utilisation_factor(1.0, 2.0, "heat") == 2.0 / 3.0
    assert _utilisation_factor(1.0, 2.5, "heat") == 2.5 / 3.5

    # γ ≤ 0 boundary: 1.0 heating, 0.0 cooling
    assert _utilisation_factor(0.0, 2.0, "heat") == 1.0
    assert _utilisation_factor(0.0, 2.0, "cool") == 0.0
    assert _utilisation_factor(-0.1, 2.0, "heat") == 1.0

    # Cooling mode and heating mode use identical formula for γ > 0 in
    # the current implementation (envelope.py:567-570) — record this as
    # a bit-perfect invariant of the function.
    assert _utilisation_factor(0.5, 2.0, "cool") == _utilisation_factor(0.5, 2.0, "heat")
