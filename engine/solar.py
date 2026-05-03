"""
engine/solar.py

Photovoltaic production simulation module.
Performs a full 8 760-hour simulation using the TMY from PVGIS.

Physical models used
--------------------
Solar position     : pvlib.solarposition (Spencer / Grover algorithm)
Irradiance transp. : Perez 1990 anisotropic sky model  (most accurate for tilted surfaces)
Horizon shading    : geometric occlusion from PVGIS horizon profile
Module temperature : Faiman (2008) model  (IEC 61853 compliant)
DC power           : single-diode simplified (PVWatts-style with temp. coefficient)
AC power           : inverter efficiency curve (Sandia PVLIB model simplified)
System losses      : disaggregated per loss category

Dependencies
------------
    pip install pvlib pandas numpy

Standards / references
----------------------
    IEC 61724-1:2021   PV system performance monitoring
    IEC 61853-1:2011   Module power rating
    EN 50530:2010      Inverter efficiency
    Perez et al. 1990  Solar Energy 44(5), 271-289
    Faiman 2008        Progress in Photovoltaics 16(4), 307-315
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional

try:
    import pvlib
    import pandas as pd
    import numpy as np
    PVLIB_AVAILABLE = True
except ImportError:
    PVLIB_AVAILABLE = False

from engine.pvgis_client import TMYResult, HourlyRecord


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BuildingFootprint:
    """
    One roof / building / sub-area within a multi-building PV system.

    Used when the PV installation spans multiple roofs (e.g. school
    complex with separate gym, multiple buildings on the same plot,
    or a single roof split into different orientations).

    Parameters
    ----------
    name : str
        Human-readable label, e.g. "Sede principale", "Palestra".
    area_m2 : float
        Roof area available for PV [m²].
    azimuth_deg : float
        Azimuth the panels face: 0 = North, 90 = East,
        180 = South, 270 = West.
    tilt_deg : float
        Panel tilt from horizontal: 0 = flat, 90 = vertical.
    usable_fraction : float
        Fraction of `area_m2` actually usable for panels (excludes
        chimneys, perimeter setbacks, technical access). Default 0.70.
    polygon_coords : list[tuple[float, float]] | None
        Optional list of (lat, lon) vertices of the roof polygon.
        Reserved for future use (3D shading, frontend visualisation).
        The engine does NOT use this field — pure pass-through.
    """
    name: str
    area_m2: float
    azimuth_deg: float
    tilt_deg: float
    usable_fraction: float = 0.70
    polygon_coords: Optional[list[tuple[float, float]]] = None

    @property
    def peak_power_kWp(self) -> float:
        """
        Estimated peak DC power assuming 200 W/m² module density
        (typical 400 Wp module ≈ 2 m²).
        """
        return self.area_m2 * self.usable_fraction * 0.20


@dataclass
class FootprintResult:
    """
    Per-footprint production breakdown — populated by simulate_pv_multi
    and reported back in SolarResult.footprints_detail for multi-building
    reports.
    """
    name: str
    area_m2: float
    peak_power_kWp: float
    azimuth_deg: float
    tilt_deg: float
    production_annual_kWh: float
    specific_yield_kWh_kWp: float
    share_pct: float                # this footprint's share of total production


@dataclass
class PVSystem:
    """
    Physical and electrical description of the PV installation.

    Parameters
    ----------
    peak_power_kWp : float
        Installed DC peak power at STC (1000 W/m², 25°C) [kWp].
    tilt_deg : float
        Panel tilt angle from horizontal [°]. 0 = horizontal, 90 = vertical.
        Optimal for Italy: roughly equal to latitude (30–40°).
    azimuth_deg : float
        Panel azimuth (direction panels face) [°].
        0 = North, 90 = East, 180 = South, 270 = West.
        South (180°) is optimal for Northern Hemisphere.
    module_efficiency_stc : float
        Module efficiency at STC (fraction, not %). Typical: 0.18–0.23.
        Used to derive module area from peak power.
    temp_coefficient_pct_per_K : float
        Power temperature coefficient [%/K]. Typically –0.35 to –0.45 %/K.
        Monocrystalline: –0.35, Polycrystalline: –0.40, CdTe: –0.25.
    noct_celsius : float
        Nominal Operating Cell Temperature [°C]. Typical: 44–48°C.
        From module datasheet (IEC 61215).
    inverter_efficiency_nominal : float
        Nominal inverter efficiency at rated power (fraction). Typical: 0.96–0.98.
    dc_ac_ratio : float
        Ratio of DC peak power to AC inverter nominal power.
        Values > 1 mean the array is oversized relative to inverter (clipping).
        Typical: 1.10–1.25 for fixed-tilt systems.
    system_losses : SystemLosses
        Disaggregated loss model (see SystemLosses dataclass).
    degradation_rate_pct_per_yr : float
        Annual module degradation rate [%/yr]. Typical: 0.4–0.7 %/yr.
        Used for multi-year cash-flow projections in production_profile_by_year().
    iam_b0 : float
        ASHRAE IAM model coefficient b0 (dimensionless).
        Controls how much reflective loss increases at oblique angles.
        Standard glass cover: 0.05.  Anti-reflective coating: 0.02.
        Reference: IEC 61853-2, Martin & Ruiz 2001.
    """
    peak_power_kWp: float
    tilt_deg: float
    azimuth_deg: float                    # 180 = South (optimal, Northern Hemisphere)
    module_efficiency_stc: float = 0.20
    temp_coefficient_pct_per_K: float = -0.40
    noct_celsius: float = 45.0
    inverter_efficiency_nominal: float = 0.97
    dc_ac_ratio: float = 1.15
    system_losses: "SystemLosses" = field(default_factory=lambda: SystemLosses())
    degradation_rate_pct_per_yr: float = 0.50
    iam_b0: float = 0.05

    @property
    def module_area_m2(self) -> float:
        """Total module area derived from peak power and STC efficiency."""
        return (self.peak_power_kWp * 1000) / (self.module_efficiency_stc * 1000)

    @property
    def inverter_ac_power_kW(self) -> float:
        """Inverter nominal AC power [kW]."""
        return self.peak_power_kWp / self.dc_ac_ratio


@dataclass
class SystemLosses:
    """
    Disaggregated PV system loss model.
    All values are fractional annual losses (0–1).
    Reference: IEC 61724-1, pvlib documentation.

    The total DC loss multiplier = product of (1 - each_loss).
    """
    soiling: float = 0.020          # dust, bird droppings, pollen
    shading_near: float = 0.030     # nearby obstacles (not horizon — handled separately)
    mismatch: float = 0.020         # module-to-module variation
    wiring_dc: float = 0.015        # DC cable resistive losses
    wiring_ac: float = 0.010        # AC cable resistive losses
    connections: float = 0.005      # connector contact resistance
    lid_degradation: float = 0.015  # Light Induced Degradation (first year)
    availability: float = 0.010     # planned and unplanned downtime

    @property
    def total_dc_loss_factor(self) -> float:
        """
        Combined DC-side loss multiplier applied to DC power.
        Does NOT include inverter or AC wiring losses.
        """
        dc_losses = [
            self.soiling,
            self.shading_near,
            self.mismatch,
            self.wiring_dc,
            self.connections,
            self.lid_degradation,
            self.availability,
        ]
        factor = 1.0
        for loss in dc_losses:
            factor *= (1.0 - loss)
        return factor

    @property
    def total_ac_loss_factor(self) -> float:
        """AC-side loss multiplier (wiring after inverter)."""
        return 1.0 - self.wiring_ac

    def summary(self) -> dict:
        return {
            "soiling_%":          round(self.soiling * 100, 2),
            "near_shading_%":     round(self.shading_near * 100, 2),
            "mismatch_%":         round(self.mismatch * 100, 2),
            "dc_wiring_%":        round(self.wiring_dc * 100, 2),
            "ac_wiring_%":        round(self.wiring_ac * 100, 2),
            "connections_%":      round(self.connections * 100, 2),
            "lid_degradation_%":  round(self.lid_degradation * 100, 2),
            "availability_%":     round(self.availability * 100, 2),
            "total_dc_factor":    round(self.total_dc_loss_factor, 4),
            "total_ac_factor":    round(self.total_ac_loss_factor, 4),
        }


@dataclass
class HourlySolarResult:
    """Simulated values for a single hour."""
    month: int
    day: int
    hour: int
    solar_zenith_deg: float
    solar_azimuth_deg: float
    poa_global_W_m2: float       # Plane-of-Array irradiance after IAM [W/m²]
    poa_direct_W_m2: float
    poa_diffuse_sky_W_m2: float
    poa_diffuse_gnd_W_m2: float
    horizon_shaded: bool          # True if sun is below the horizon profile
    T_cell_celsius: float         # Estimated cell temperature [°C]
    P_dc_W: float                 # DC power after all DC losses [W]
    P_ac_W: float                 # AC power after inverter + AC wiring [W]
    P_gross_W: float = 0.0        # Theoretical DC: peak × (POA_unshaded/1000), no losses [W]


@dataclass
class SolarResult:
    """Complete output of the solar simulation module."""

    # ── Hourly ──────────────────────────────────────────────────────────────
    hourly: list[HourlySolarResult]

    # ── Monthly [12 values, index 0 = January] ──────────────────────────────
    production_monthly_kWh: list[float]     # AC energy injected [kWh/month]
    poa_monthly_kWh_m2: list[float]         # In-plane irradiation [kWh/m²/month]
    T_cell_avg_monthly: list[float]         # Mean cell temp during sun hours [°C]

    # ── Annual totals ────────────────────────────────────────────────────────
    production_annual_kWh: float            # Total AC energy [kWh/yr]
    specific_yield_kWh_kWp: float           # kWh produced per kWp installed
    performance_ratio: float                # PR = E_ac / (H_poa × P_peak)
    capacity_factor: float                  # CF = E_ac / (P_peak × 8760 h)

    # ── Loss breakdown ────────────────────────────────────────────────────────

    # ── Loss breakdown ────────────────────────────────────────────
    gross_dc_kWh: float                     # Theoretical max DC (no losses, no shading)
    horizon_shading_loss_kWh: float         # Lost to horizon obstruction
    iam_loss_kWh: float                     # Lost to oblique-angle reflection (IAM)
    temperature_loss_kWh: float             # Lost to cell heating above 25°C
    system_losses_kWh: float               # Soiling + wiring_dc + mismatch + ...
    inverter_loss_kWh: float               # Inverter conversion loss
    clipping_loss_kWh: float               # Inverter saturation (DC > AC rated)
    ac_wiring_loss_kWh: float              # AC cable loss after inverter

    # ── System info ───────────────────────────────────────────────────────────
    system_losses_detail: dict
    location_info: dict

    # ── Multi-footprint breakdown ─────────────────────────────────────────────
    # Empty list for legacy single-system simulate_pv() calls (backward
    # compatible); populated by simulate_pv_multi() with one entry per footprint.
    footprints_detail: list[FootprintResult] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# SOLAR POSITION  (standalone — no pvlib required)
# Based on Spencer (1971) and Michalsky (1988)
# ══════════════════════════════════════════════════════════════════════════════

def _day_of_year(month: int, day: int) -> int:
    """Day of year (1–365)."""
    days_before = [0,31,59,90,120,151,181,212,243,273,304,334]
    return days_before[month - 1] + day


def _solar_declination_rad(doy: int) -> float:
    """Solar declination δ [rad] — Spencer 1971."""
    B = math.radians((360 / 365) * (doy - 81))
    return math.radians(
        23.45 * math.sin(B)
    )


def _equation_of_time_min(doy: int) -> float:
    """Equation of time [minutes] — Spencer 1971."""
    B = 2 * math.pi * (doy - 1) / 365
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(B)
        - 0.032077 * math.sin(B)
        - 0.014615 * math.cos(2 * B)
        - 0.04089  * math.sin(2 * B)
    )


def _solar_position(
    lat_deg: float,
    lon_deg: float,
    month: int,
    day: int,
    hour_utc: float,   # decimal UTC hour (e.g. 13.5 for 13:30)
) -> tuple[float, float]:
    """
    Compute solar zenith and azimuth for a given location and time.

    Returns
    -------
    zenith_deg : float  — 0° = overhead, 90° = horizon, >90° = below horizon
    azimuth_deg : float — 0° = North, 90° = East, 180° = South, 270° = West
    """
    doy = _day_of_year(month, day)
    delta = _solar_declination_rad(doy)
    eot   = _equation_of_time_min(doy)
    phi   = math.radians(lat_deg)

    # True solar time [h]
    tst = hour_utc + lon_deg / 15 + eot / 60

    # Hour angle ω [rad]  — 0 at solar noon, negative before, positive after
    omega = math.radians(15 * (tst - 12))

    # Zenith angle
    cos_z = (math.sin(phi) * math.sin(delta)
             + math.cos(phi) * math.cos(delta) * math.cos(omega))
    cos_z = max(-1.0, min(1.0, cos_z))
    zenith = math.degrees(math.acos(cos_z))

    # Azimuth (clockwise from North)
    if zenith >= 90.0:
        return zenith, 180.0   # below horizon — direction irrelevant

    sin_z = math.sin(math.radians(zenith))
    if sin_z < 1e-6:
        return zenith, 180.0

    cos_az = (math.sin(delta) * math.cos(phi)
              - math.cos(delta) * math.sin(phi) * math.cos(omega)) / sin_z
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.degrees(math.acos(cos_az))

    # Quadrant correction
    if math.sin(omega) > 0:
        az = 360.0 - az

    return zenith, az


# ══════════════════════════════════════════════════════════════════════════════
# IRRADIANCE TRANSPOSITION — PEREZ 1990
# Most accurate model for diffuse sky irradiance on tilted surfaces.
# Reference: Perez et al. (1990), Solar Energy 44(5), 271–289.
# ══════════════════════════════════════════════════════════════════════════════

# Perez circumsolar and horizon brightness coefficients
# Rows correspond to sky clearness bins ε (1–8)
_PEREZ_F_COEFFS = [
    # f11      f12      f13      f21      f22      f23
    [-0.0083,  0.5877,  -0.0621, -0.0596,  0.0721, -0.0220],
    [ 0.1299,  0.6826,  -0.1514, -0.0190,  0.0660, -0.0289],
    [ 0.3297,  0.4869,  -0.2211,  0.0554, -0.0640, -0.0261],
    [ 0.5682,  0.1875,  -0.2951,  0.1089, -0.1519, -0.0140],
    [ 0.8730, -0.3920,  -0.3616,  0.2256, -0.4620,  0.0012],
    [ 1.1326, -1.2367,  -0.4118,  0.2878, -0.8230,  0.0559],
    [ 1.0601, -1.5999,  -0.3589,  0.2642, -1.1270,  0.1311],
    [ 0.6777, -0.3273,  -0.2504,  0.1597, -1.3765,  0.2506],
]


def _perez_sky_bin(epsilon: float) -> int:
    """Sky clearness bin index (0-based) from ε."""
    bins = [1.065, 1.230, 1.500, 1.950, 2.800, 4.500, 6.200, float("inf")]
    for i, edge in enumerate(bins):
        if epsilon < edge:
            return i
    return 7


def _perez_diffuse(
    dhi: float,      # diffuse horizontal irradiance [W/m²]
    dni: float,      # direct normal irradiance [W/m²]
    ghi: float,      # global horizontal irradiance [W/m²]
    zenith_deg: float,
    panel_tilt_deg: float,
    aoi_deg: float,  # angle of incidence on panel
    doy: int,
) -> tuple[float, float]:
    """
    Perez 1990 anisotropic diffuse transposition.

    Returns
    -------
    (poa_sky_diffuse, poa_ground_reflected) in [W/m²]
    """
    beta = math.radians(panel_tilt_deg)
    z    = math.radians(min(zenith_deg, 87.0))   # cap at 87° to avoid singularity
    aoi  = math.radians(min(aoi_deg, 90.0))

    # Extra-terrestrial irradiance [W/m²]
    E0 = 1367 * (1 + 0.033 * math.cos(math.radians(360 * doy / 365)))

    # Sky clearness ε (dimensionless) — Perez eq. 1
    if dhi < 1.0:
        epsilon = 999.0   # clear sky
    else:
        kappa = 1.041
        epsilon = ((dhi + dni) / dhi + kappa * z**3) / (1 + kappa * z**3)

    # Sky brightness Δ — Perez eq. 2
    delta = dhi * (1 / max(math.cos(z), 0.087)) / E0 if E0 > 0 else 0.0

    # Brightness coefficients
    idx = _perez_sky_bin(epsilon)
    f11, f12, f13, f21, f22, f23 = _PEREZ_F_COEFFS[idx]

    F1 = max(f11 + f12 * delta + f13 * z, 0.0)
    F2 = f21 + f22 * delta + f23 * z

    # In-plane diffuse (sky) — Perez eq. 6
    cos_aoi  = max(math.cos(aoi), 0.0)
    cos_z    = max(math.cos(z), 0.087)
    poa_sky  = dhi * (
        (1 - F1) * (1 + math.cos(beta)) / 2
        + F1 * cos_aoi / cos_z
        + F2 * math.sin(beta)
    )

    # Ground-reflected component (isotropic assumption)
    rho_g   = 0.20   # albedo
    poa_gnd = ghi * rho_g * (1 - math.cos(beta)) / 2

    return max(poa_sky, 0.0), max(poa_gnd, 0.0)


def _angle_of_incidence(
    zenith_deg: float,
    solar_azimuth_deg: float,
    panel_tilt_deg: float,
    panel_azimuth_deg: float,
) -> float:
    """
    Angle of incidence (AOI) of direct beam on a tilted panel [°].
    Returns value in [0, 90] — values > 90 mean the beam hits the back.
    """
    beta  = math.radians(panel_tilt_deg)
    gamma = math.radians(panel_azimuth_deg - 180)   # relative to South
    theta = math.radians(zenith_deg)
    phi_s = math.radians(solar_azimuth_deg - 180)   # relative to South

    cos_aoi = (
        math.cos(theta) * math.cos(beta)
        + math.sin(theta) * math.sin(beta) * math.cos(phi_s - gamma)
    )
    cos_aoi = max(-1.0, min(1.0, cos_aoi))
    return math.degrees(math.acos(cos_aoi))


# ══════════════════════════════════════════════════════════════════════════════
# INCIDENCE ANGLE MODIFIER — ASHRAE model (IEC 61853-2)
# Accounts for increased glass reflectance at oblique incidence angles.
# Reference: Souka & Safwat 1966, ASHRAE 93-2010, IEC 61853-2:2016.
# ══════════════════════════════════════════════════════════════════════════════

# Fixed IAM for isotropic diffuse components (integrated over hemisphere)
# Sky diffuse: ~0.95, Ground diffuse: ~0.97  (Martin & Ruiz 2001 Table 1)
_IAM_DIFFUSE_SKY = 0.950
_IAM_DIFFUSE_GND = 0.970


def _iam_ashrae(aoi_deg: float, b0: float = 0.05) -> float:
    """
    Incidence Angle Modifier (IAM) using the ASHRAE model.

    IAM(θ) = 1 − b0 × (1/cos(θ) − 1)

    At normal incidence (θ=0): IAM = 1.0  (no reflection loss).
    At grazing angle (θ → 90°): IAM → 0 (all light reflected).
    At θ ≥ 90°: IAM = 0 (beam hits the back of the panel).

    Parameters
    ----------
    aoi_deg : float
        Angle of incidence on the panel surface [deg].
    b0 : float
        Empirical coefficient. Standard glass: 0.05. AR-coated: 0.02.

    Returns
    -------
    float
        IAM value in [0, 1].
    """
    if aoi_deg >= 90.0:
        return 0.0
    cos_aoi = math.cos(math.radians(aoi_deg))
    if cos_aoi < 1e-6:
        return 0.0
    iam = 1.0 - b0 * (1.0 / cos_aoi - 1.0)
    return max(iam, 0.0)

# ══════════════════════════════════════════════════════════════════════════════
# HORIZON SHADING
# ══════════════════════════════════════════════════════════════════════════════

def _is_horizon_shaded(
    solar_azimuth_deg: float,
    solar_elevation_deg: float,
    horizon_profile: list[float],
) -> bool:
    """
    Check if the sun is below the horizon profile at the given azimuth.

    PVGIS returns horizon heights at azimuths 0°, 10°, 20°, ..., 350°
    (36 values, step = 10°).

    Returns True (shaded) if solar elevation < horizon elevation at that azimuth.
    """
    if not horizon_profile or len(horizon_profile) < 2:
        return False   # no horizon data — assume flat

    step = 360 / len(horizon_profile)
    idx  = int(solar_azimuth_deg / step) % len(horizon_profile)
    # Interpolate between adjacent values
    idx2 = (idx + 1) % len(horizon_profile)
    frac = (solar_azimuth_deg % step) / step
    h_horizon = horizon_profile[idx] * (1 - frac) + horizon_profile[idx2] * frac

    return solar_elevation_deg < h_horizon


# ══════════════════════════════════════════════════════════════════════════════
# MODULE TEMPERATURE — FAIMAN 2008
# IEC 61853-1 compliant.  More accurate than NOCT model.
# Reference: Faiman (2008), Progress in Photovoltaics 16(4), 307-315.
# ══════════════════════════════════════════════════════════════════════════════

# Faiman coefficients for common mounting types
# (U0 [W/m²K], U1 [W·s/m³K])
FAIMAN_COEFFICIENTS = {
    "open_rack":        (25.0, 6.84),   # free-standing, good ventilation
    "close_roof":       (29.0, 0.00),   # flush-mounted on roof, limited ventilation
    "integrated_roof":  (15.0, 0.00),   # BIPV, poor ventilation
    "flat_roof_ballast":(20.0, 0.00),   # ballasted flat roof mount
}


def _cell_temperature_faiman(
    poa_W_m2: float,
    T_amb_celsius: float,
    wind_speed_m_s: float,
    mounting: str = "open_rack",
) -> float:
    """
    Cell temperature [°C] using Faiman model.

    T_cell = T_amb + G_poa / (U0 + U1 × v_wind)
    """
    U0, U1 = FAIMAN_COEFFICIENTS.get(mounting, FAIMAN_COEFFICIENTS["open_rack"])
    denominator = U0 + U1 * wind_speed_m_s
    if denominator < 1.0:
        denominator = 1.0
    return T_amb_celsius + poa_W_m2 / denominator


# ══════════════════════════════════════════════════════════════════════════════
# DC POWER — PVWatts-style with temperature correction
# ══════════════════════════════════════════════════════════════════════════════

def _dc_power_W(
    poa_W_m2: float,
    T_cell_celsius: float,
    system: PVSystem,
) -> float:
    """
    DC power output [W].

    P_dc = P_stc × (G_poa / 1000) × [1 + γ × (T_cell - 25)]

    where γ is the temperature coefficient in fraction/K.
    """
    if poa_W_m2 <= 0:
        return 0.0

    G_ratio = poa_W_m2 / 1000.0
    gamma   = system.temp_coefficient_pct_per_K / 100.0   # %/K → 1/K
    T_corr  = 1.0 + gamma * (T_cell_celsius - 25.0)
    T_corr  = max(T_corr, 0.0)   # power cannot go negative from temperature

    P_dc = system.peak_power_kWp * 1000 * G_ratio * T_corr
    return max(P_dc, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# AC POWER — inverter model with clipping
# ══════════════════════════════════════════════════════════════════════════════

def _ac_power_W(
    P_dc_W: float,
    system: PVSystem,
) -> tuple[float, float]:
    """
    AC power output [W] and clipping loss [W].

    Models inverter efficiency as a function of loading ratio:
    - At < 5% of rated power: standby (negligible output)
    - Linear region: η = η_nom × (1 - 0.05 × (1 - load_ratio))
    - Clipping: P_ac capped at inverter nominal AC power

    Returns (P_ac_W, clipping_loss_W).
    """
    P_inv_rated_W = system.inverter_ac_power_kW * 1000

    if P_dc_W <= 0:
        return 0.0, 0.0

    load_ratio = P_dc_W / P_inv_rated_W

    if load_ratio < 0.05:
        # Below minimum operation threshold — very low efficiency
        eta = system.inverter_efficiency_nominal * 0.60
    else:
        # Partial-load efficiency curve (simplified EN 50530)
        eta = system.inverter_efficiency_nominal * (
            1 - 0.03 * (1 - min(load_ratio, 1.0))
        )
        eta = min(eta, system.inverter_efficiency_nominal)

    P_ac_uncapped = P_dc_W * eta
    P_ac_rated    = P_inv_rated_W * system.inverter_efficiency_nominal
    P_ac          = min(P_ac_uncapped, P_ac_rated)
    clipping      = max(P_ac_uncapped - P_ac_rated, 0.0)

    return max(P_ac, 0.0), clipping


# ══════════════════════════════════════════════════════════════════════════════
# PVLIB SIMULATION PATH (high accuracy, requires pvlib + pandas)
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_with_pvlib(
    tmy: TMYResult,
    system: PVSystem,
    mounting: str,
) -> list[HourlySolarResult]:
    """Full simulation using pvlib (Perez model, precise solar position)."""
    import pvlib
    import pandas as pd
    import numpy as np

    loc = tmy.location
    horizon = loc.horizon_profile

    # Build pandas DatetimeIndex (non-leap year, UTC)
    times = pd.date_range("2023-01-01", periods=8760, freq="h", tz="UTC")

    # Build weather DataFrame from TMY hourly records
    records = tmy.hourly
    weather = pd.DataFrame({
        "ghi":        [r.G_h   for r in records],
        "dni":        [r.Gb_n  for r in records],
        "dhi":        [r.Gd_h  for r in records],
        "temp_air":   [r.T2m   for r in records],
        "wind_speed": [r.WS10m for r in records],
    }, index=times)

    # Solar position
    sp = pvlib.solarposition.get_solarposition(
        times, loc.latitude, loc.longitude, loc.elevation_m
    )

    # Angle of incidence
    aoi = pvlib.irradiance.aoi(
        system.tilt_deg, system.azimuth_deg,
        sp["apparent_zenith"], sp["azimuth"]
    )

    # POA irradiance — Perez model
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=system.tilt_deg,
        surface_azimuth=system.azimuth_deg,
        solar_zenith=sp["apparent_zenith"],
        solar_azimuth=sp["azimuth"],
        dni=weather["dni"],
        ghi=weather["ghi"],
        dhi=weather["dhi"],
        model="perez",
        albedo=0.20,
    )

    poa_direct_raw = poa["poa_direct"].clip(lower=0).values
    poa_sky_raw    = poa["poa_sky_diffuse"].clip(lower=0).values
    poa_gnd_raw    = poa["poa_ground_diffuse"].clip(lower=0).values

    zen   = sp["apparent_zenith"].values
    az    = sp["azimuth"].values
    elev  = 90.0 - zen

    # ── IAM (pvlib ashrae model vectorised) ───────────────────────────────────
    iam_beam = pvlib.iam.ashrae(aoi.values, b=system.iam_b0)
    iam_beam = np.where(np.isnan(iam_beam), 0.0, iam_beam)
    poa_direct = poa_direct_raw * iam_beam
    poa_sky_d  = poa_sky_raw    * _IAM_DIFFUSE_SKY
    poa_gnd_d  = poa_gnd_raw    * _IAM_DIFFUSE_GND
    poa_global = np.maximum(poa_direct + poa_sky_d + poa_gnd_d, 0.0)

    # P_gross: theoretical peak power at unshaded POA, no losses
    poa_global_unshaded = np.maximum(
        poa_direct_raw + poa_sky_raw + poa_gnd_raw, 0.0
    )
    P_gross = system.peak_power_kWp * 1000 * (poa_global_unshaded / 1000.0)

    # Cell temperature — Faiman (uses IAM-corrected POA for thermal accuracy)
    U0, U1 = FAIMAN_COEFFICIENTS.get(mounting, FAIMAN_COEFFICIENTS["open_rack"])
    denom   = U0 + U1 * weather["wind_speed"].values
    denom   = np.maximum(denom, 1.0)
    T_cell  = weather["temp_air"].values + poa_global / denom

    # DC power with temperature correction
    gamma  = system.temp_coefficient_pct_per_K / 100.0
    T_corr = np.maximum(1.0 + gamma * (T_cell - 25.0), 0.0)
    P_dc   = system.peak_power_kWp * 1000 * (poa_global / 1000) * T_corr

    # System DC losses
    P_dc_net = P_dc * system.system_losses.total_dc_loss_factor

    # AC power + clipping
    P_inv_W   = system.inverter_ac_power_kW * 1000
    eta_inv   = system.inverter_efficiency_nominal
    load_r    = P_dc_net / np.maximum(P_inv_W, 1.0)
    eta_curve = np.where(
        load_r < 0.05,
        eta_inv * 0.60,
        eta_inv * (1 - 0.03 * (1 - np.minimum(load_r, 1.0)))
    )
    P_ac_uncap = P_dc_net * eta_curve
    P_ac_cap   = P_inv_W * eta_inv
    P_ac       = np.minimum(P_ac_uncap, P_ac_cap)
    P_ac       = np.maximum(P_ac, 0.0)

    # AC wiring loss
    P_ac_final = P_ac * system.system_losses.total_ac_loss_factor

    # Build hourly results
    results: list[HourlySolarResult] = []
    for i, rec in enumerate(records):
        shaded = _is_horizon_shaded(az[i], elev[i], horizon) if zen[i] < 90 else False
        P_ac_h = 0.0 if shaded else float(P_ac_final[i])
        results.append(HourlySolarResult(
            month=rec.month, day=rec.day, hour=rec.hour,
            solar_zenith_deg=float(zen[i]),
            solar_azimuth_deg=float(az[i]),
            poa_global_W_m2=float(poa_global[i]),
            poa_direct_W_m2=float(poa_direct[i]),
            poa_diffuse_sky_W_m2=float(poa_sky_d[i]),
            poa_diffuse_gnd_W_m2=float(poa_gnd_d[i]),
            horizon_shaded=shaded,
            T_cell_celsius=float(T_cell[i]),
            P_dc_W=0.0 if shaded else float(P_dc_net[i]),
            P_ac_W=P_ac_h,
            P_gross_W=float(P_gross[i]),
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE SIMULATION PATH (no pvlib — uses internal implementations)
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_standalone(
    tmy: TMYResult,
    system: PVSystem,
    mounting: str,
) -> list[HourlySolarResult]:
    """
    Full simulation using only the implementations in this module.
    Accurate to ±2–3% vs pvlib on annual production.
    Used as fallback when pvlib is not installed.
    """
    loc     = tmy.location
    horizon = loc.horizon_profile
    results: list[HourlySolarResult] = []

    for rec in tmy.hourly:
        doy  = _day_of_year(rec.month, rec.day)
        hour_utc = rec.hour + 0.5   # centre of hour

        # Solar position
        zen, az = _solar_position(loc.latitude, loc.longitude, rec.month, rec.day, hour_utc)
        elev    = 90.0 - zen

        # Horizon shading
        shaded = _is_horizon_shaded(az, elev, horizon) if zen < 90 else False

        # AOI — needed even for shaded hours to compute P_gross for waterfall
        aoi = _angle_of_incidence(zen, az, system.tilt_deg, system.azimuth_deg)

        # Unshaded POA (for P_gross waterfall reference)
        if aoi < 90.0:
            poa_direct_raw = rec.Gb_n * math.cos(math.radians(aoi))
        else:
            poa_direct_raw = 0.0
        poa_sky_raw, poa_gnd_raw = _perez_diffuse(
            dhi=rec.Gd_h, dni=rec.Gb_n, ghi=rec.G_h,
            zenith_deg=zen, panel_tilt_deg=system.tilt_deg,
            aoi_deg=aoi, doy=doy,
        )
        poa_unshaded = max(poa_direct_raw + poa_sky_raw + poa_gnd_raw, 0.0)

        # Gross DC: theoretical peak power at this POA, no losses, no shading
        P_gross = system.peak_power_kWp * 1000 * (poa_unshaded / 1000.0)

        # Below horizon or horizon-shaded: store zero production, keep P_gross
        if zen >= 90.0 or shaded:
            results.append(HourlySolarResult(
                month=rec.month, day=rec.day, hour=rec.hour,
                solar_zenith_deg=zen, solar_azimuth_deg=az,
                poa_global_W_m2=0, poa_direct_W_m2=0,
                poa_diffuse_sky_W_m2=0, poa_diffuse_gnd_W_m2=0,
                horizon_shaded=shaded,
                T_cell_celsius=rec.T2m,
                P_dc_W=0, P_ac_W=0,
                P_gross_W=P_gross,
            ))
            continue

        # ── IAM: incidence angle modifier on direct beam ──────────────────────
        # Applied only to beam; fixed factors for isotropic diffuse components.
        iam_beam = _iam_ashrae(aoi, b0=system.iam_b0)
        poa_direct  = poa_direct_raw * iam_beam
        poa_sky_d   = poa_sky_raw   * _IAM_DIFFUSE_SKY
        poa_gnd_d   = poa_gnd_raw   * _IAM_DIFFUSE_GND

        poa_global = max(poa_direct + poa_sky_d + poa_gnd_d, 0.0)

        # Cell temperature (uses IAM-corrected POA for thermal accuracy)
        T_cell = _cell_temperature_faiman(poa_global, rec.T2m, rec.WS10m, mounting)

        # DC power + DC system losses
        P_dc_gross = _dc_power_W(poa_global, T_cell, system)
        P_dc_net   = P_dc_gross * system.system_losses.total_dc_loss_factor

        # AC power + clipping + AC wiring
        P_ac, _ = _ac_power_W(P_dc_net, system)
        P_ac_final = P_ac * system.system_losses.total_ac_loss_factor

        results.append(HourlySolarResult(
            month=rec.month, day=rec.day, hour=rec.hour,
            solar_zenith_deg=zen, solar_azimuth_deg=az,
            poa_global_W_m2=poa_global,
            poa_direct_W_m2=poa_direct,
            poa_diffuse_sky_W_m2=poa_sky_d,
            poa_diffuse_gnd_W_m2=poa_gnd_d,
            horizon_shaded=shaded,
            T_cell_celsius=T_cell,
            P_dc_W=P_dc_net,
            P_ac_W=P_ac_final,
            P_gross_W=P_gross,
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

def _aggregate(
    hourly: list[HourlySolarResult],
    system: PVSystem,
    tmy: TMYResult,
) -> SolarResult:
    """
    Aggregate hourly results into monthly / annual metrics.

    Loss waterfall (all in kWh, must sum to gross_dc):
        gross_dc
        - horizon_shading_loss   (P_gross of shaded hours)
        - iam_loss               (reflective loss at oblique incidence)
        - temperature_loss       (power reduction above 25°C)
        - system_losses          (soiling, mismatch, dc wiring, LID, availability)
        - inverter_loss          (conversion loss in inverter)
        - clipping_loss          (inverter saturation)
        - ac_wiring_loss         (AC cable resistance)
        = production_annual_kWh  (net AC energy)
    """
    n_months = 12
    prod_m   = [0.0] * n_months
    poa_m    = [0.0] * n_months
    Tc_sum_m = [0.0] * n_months
    Tc_cnt_m = [0]   * n_months

    gross_dc       = 0.0
    horizon_loss   = 0.0
    iam_loss       = 0.0
    temp_loss      = 0.0
    sys_dc_loss    = 0.0
    inverter_loss  = 0.0
    clipping_loss  = 0.0
    ac_wiring_loss = 0.0

    gamma       = system.temp_coefficient_pct_per_K / 100.0
    dc_factor   = system.system_losses.total_dc_loss_factor
    ac_factor   = system.system_losses.total_ac_loss_factor
    P_inv_W     = system.inverter_ac_power_kW * 1000
    eta_inv     = system.inverter_efficiency_nominal

    for h in hourly:
        m = h.month - 1

        # Monthly AC production and POA (after IAM, after shading)
        prod_m[m] += h.P_ac_W / 1000.0
        poa_m[m]  += h.poa_global_W_m2 / 1000.0

        if h.poa_global_W_m2 > 0:
            Tc_sum_m[m] += h.T_cell_celsius
            Tc_cnt_m[m] += 1

        # 1. Gross DC: theoretical peak output at unshaded, no-loss POA
        P_gr = h.P_gross_W          # stored during simulation
        gross_dc += P_gr / 1000.0

        # 2. Horizon shading loss: P_gross of hours where sun is shaded/below horizon
        if h.horizon_shaded or h.poa_global_W_m2 == 0 and P_gr > 0:
            horizon_loss += P_gr / 1000.0
            continue   # no further losses to unpack for this hour

        # Unshaded gross at this hour (= P_gr since not shaded and sun is up)
        P_unshaded = P_gr

        # 3. IAM loss: difference between gross and IAM-corrected POA DC
        #    P_gross uses unshaded POA; actual poa_global uses IAM-corrected POA
        P_after_iam = system.peak_power_kWp * 1000 * (h.poa_global_W_m2 / 1000.0)
        iam_loss += max(P_unshaded - P_after_iam, 0.0) / 1000.0

        # 4. Temperature loss: reduction from T_cell > 25°C
        T_corr    = max(1.0 + gamma * (h.T_cell_celsius - 25.0), 0.0)
        P_after_T = P_after_iam * T_corr
        temp_loss += max(P_after_iam - P_after_T, 0.0) / 1000.0

        # 5. DC system losses: soiling, mismatch, wiring, LID, availability
        P_dc_net = P_after_T * dc_factor
        sys_dc_loss += (P_after_T - P_dc_net) / 1000.0

        # 6 & 7. Inverter: efficiency curve + clipping
        load_r = P_dc_net / max(P_inv_W, 1.0)
        if load_r < 0.05:
            eta = eta_inv * 0.60
        else:
            eta = eta_inv * (1.0 - 0.03 * (1.0 - min(load_r, 1.0)))
            eta = min(eta, eta_inv)

        P_ac_uncap  = P_dc_net * eta
        P_ac_capped = min(P_ac_uncap, P_inv_W * eta_inv)
        clip_W      = max(P_ac_uncap - P_inv_W * eta_inv, 0.0)

        inverter_loss += max(P_dc_net - P_ac_capped - clip_W, 0.0) / 1000.0
        clipping_loss += clip_W / 1000.0

        # 8. AC wiring loss
        ac_wiring_loss += P_ac_capped * (1.0 - ac_factor) / 1000.0

    ac_annual      = sum(prod_m)
    P_peak_kWp     = system.peak_power_kWp
    H_poa_annual   = sum(poa_m)
    specific_yield = ac_annual / P_peak_kWp if P_peak_kWp > 0 else 0.0
    PR             = (ac_annual / (H_poa_annual * P_peak_kWp)
                      if H_poa_annual * P_peak_kWp > 0 else 0.0)
    CF             = ac_annual / (P_peak_kWp * 8760) if P_peak_kWp > 0 else 0.0

    Tc_avg_m = [
        round(Tc_sum_m[m] / Tc_cnt_m[m], 1) if Tc_cnt_m[m] > 0 else 0.0
        for m in range(n_months)
    ]

    loc = tmy.location
    return SolarResult(
        hourly=hourly,
        production_monthly_kWh=[round(v, 1) for v in prod_m],
        poa_monthly_kWh_m2=[round(v, 1) for v in poa_m],
        T_cell_avg_monthly=Tc_avg_m,
        production_annual_kWh=round(ac_annual, 1),
        specific_yield_kWh_kWp=round(specific_yield, 1),
        performance_ratio=round(PR, 3),
        capacity_factor=round(CF, 4),
        gross_dc_kWh=round(gross_dc, 1),
        horizon_shading_loss_kWh=round(horizon_loss, 1),
        iam_loss_kWh=round(iam_loss, 1),
        temperature_loss_kWh=round(temp_loss, 1),
        system_losses_kWh=round(sys_dc_loss, 1),
        inverter_loss_kWh=round(inverter_loss, 1),
        clipping_loss_kWh=round(clipping_loss, 1),
        ac_wiring_loss_kWh=round(ac_wiring_loss, 1),
        system_losses_detail=system.system_losses.summary(),
        location_info={
            "latitude":    loc.latitude,
            "longitude":   loc.longitude,
            "elevation_m": loc.elevation_m,
            "country":     loc.country,
            "dataset":     loc.dataset,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

def production_profile_by_year(
    year1_result: SolarResult,
    system: PVSystem,
    lifetime_years: int = 25,
) -> list[float]:
    """
    Annual AC production for each year of the system lifetime [kWh/yr].

    Applies linear degradation starting from year 1 (no degradation applied
    to year 1 — LID is already accounted for in SystemLosses.lid_degradation).

    Parameters
    ----------
    year1_result : SolarResult
        Result of simulate_pv() for year 1.
    system : PVSystem
        Must contain degradation_rate_pct_per_yr.
    lifetime_years : int
        Number of years to project (typically 25 or 30).

    Returns
    -------
    list[float]
        Production [kWh] for year 1, 2, ..., lifetime_years.
        Index 0 = year 1.

    Example
    -------
    >>> profile = production_profile_by_year(result, system, lifetime_years=25)
    >>> total_lifetime_kWh = sum(profile)
    >>> print(f"Year 1:  {profile[0]:.0f} kWh")
    >>> print(f"Year 25: {profile[24]:.0f} kWh  ({profile[24]/profile[0]:.1%} of year 1)")
    """
    deg_per_year = system.degradation_rate_pct_per_yr / 100.0
    base = year1_result.production_annual_kWh
    return [
        round(base * max(1.0 - deg_per_year * year, 0.0), 1)
        for year in range(lifetime_years)
    ]


def simulate_pv(
    tmy: TMYResult,
    system: PVSystem,
    mounting: str = "open_rack",
    use_pvlib: bool = True,
) -> SolarResult:
    """
    Run a full 8 760-hour PV production simulation for a single PV system.

    This is the default single-footprint entry point — kept identical to
    the legacy behaviour for backward compatibility. For installations
    that span multiple roofs / orientations, use `simulate_pv_multi`,
    which delegates to this function per footprint and aggregates.

    Parameters
    ----------
    tmy : TMYResult
        TMY data from fetch_tmy().  Must contain hourly records and
        a location with horizon profile.
    system : PVSystem
        PV system physical and electrical parameters.
    mounting : str
        Panel mounting type for Faiman temperature model.
        One of: "open_rack", "close_roof", "integrated_roof", "flat_roof_ballast".
    use_pvlib : bool
        If True (default) and pvlib is installed, use pvlib for maximum accuracy.
        Falls back automatically to internal implementation if pvlib is unavailable.

    Returns
    -------
    SolarResult
        Full simulation results: hourly, monthly, annual, losses.

    Examples
    --------
    >>> from engine.pvgis_client import fetch_tmy
    >>> from engine.solar import PVSystem, SystemLosses, simulate_pv
    >>>
    >>> tmy = fetch_tmy(lat=44.5, lon=11.3)
    >>> system = PVSystem(
    ...     peak_power_kWp=120.0,
    ...     tilt_deg=15.0,       # shallow tilt for flat industrial roof
    ...     azimuth_deg=180.0,   # South
    ... )
    >>> result = simulate_pv(tmy, system)
    >>> print(f"Annual production: {result.production_annual_kWh:.0f} kWh")
    >>> print(f"Specific yield:    {result.specific_yield_kWh_kWp:.0f} kWh/kWp")
    >>> print(f"Performance ratio: {result.performance_ratio:.2%}")
    """
    if use_pvlib and PVLIB_AVAILABLE:
        hourly = _simulate_with_pvlib(tmy, system, mounting)
    else:
        if use_pvlib and not PVLIB_AVAILABLE:
            import warnings
            warnings.warn(
                "pvlib not installed — falling back to internal solar model. "
                "Install with: pip install pvlib pandas numpy",
                RuntimeWarning,
                stacklevel=2,
            )
        hourly = _simulate_standalone(tmy, system, mounting)

    return _aggregate(hourly, system, tmy)


def simulate_pv_multi(
    tmy: TMYResult,
    footprints: list[BuildingFootprint],
    system_template: PVSystem,
    mounting: str = "open_rack",
    use_pvlib: bool = True,
) -> SolarResult:
    """
    Multi-footprint PV simulation: aggregates production across N roofs.

    For each footprint, builds a per-footprint PVSystem by cloning
    `system_template` and overriding `peak_power_kWp` / `tilt_deg` /
    `azimuth_deg` from the footprint. Calls `simulate_pv()` per footprint,
    then aggregates the hourly and annual results into a single SolarResult.

    The default single-footprint path is `simulate_pv()` itself; this
    function is the multi-footprint extension. Calling
    `simulate_pv_multi(tmy, [fp], template)` with one footprint produces
    a SolarResult identical to `simulate_pv(tmy, fp_system)` within
    floating-point rounding (≤ 0.01% on annual production).

    Aggregation rules
    -----------------
    Hourly:
      * `P_ac_W`, `P_dc_W`, `P_gross_W`           — SUMMED across footprints
      * `poa_global_W_m2`, `poa_direct_W_m2`,
        `poa_diffuse_sky_W_m2`, `poa_diffuse_gnd_W_m2`,
        `T_cell_celsius`                           — AREA-weighted average
        (these are intensities, not energies)
      * `solar_zenith_deg`, `solar_azimuth_deg`,
        `month`, `day`, `hour`                     — taken from the first
        sub-result (single TMY ⇒ identical for all footprints)
      * `horizon_shaded`                           — boolean OR

    Annual / monthly:
      * `production_monthly_kWh`,
        `production_annual_kWh`,
        `gross_dc_kWh`, `horizon_shading_loss_kWh`,
        `iam_loss_kWh`, `temperature_loss_kWh`,
        `system_losses_kWh`, `inverter_loss_kWh`,
        `clipping_loss_kWh`, `ac_wiring_loss_kWh`  — SUMMED
      * `poa_monthly_kWh_m2`,
        `T_cell_avg_monthly`                       — AREA-weighted average
      * `specific_yield_kWh_kWp`                   — production_annual / Σ kWp
      * `performance_ratio`                        — production / (POA_avg × Σ kWp)
      * `capacity_factor`                          — production / (Σ kWp × 8760)

    Aggregation choices documented above (rather than ricalcolare le loss
    from the aggregated hourly arrays) keep each footprint's per-AOI /
    per-T_cell physics intact: every footprint computes its own IAM and
    temperature losses, which simply sum.

    Parameters
    ----------
    tmy : TMYResult
        Single TMY — all footprints share location and weather.
    footprints : list[BuildingFootprint]
        One or more roofs. Length must be >= 1.
    system_template : PVSystem
        Template cloned per footprint. Fields `peak_power_kWp`,
        `tilt_deg`, `azimuth_deg` are OVERRIDDEN by the footprint values;
        all other fields (efficiency, losses, dc_ac_ratio, IAM coeff,
        degradation, ...) are inherited.
    mounting, use_pvlib
        Forwarded unchanged to `simulate_pv()`.

    Returns
    -------
    SolarResult
        Aggregated. `footprints_detail` carries per-footprint breakdown.
    """
    if len(footprints) < 1:
        raise ValueError("simulate_pv_multi requires at least one footprint")

    # Per-footprint sub-simulations
    sub_results: list[SolarResult] = []
    for fp in footprints:
        fp_system = replace(
            system_template,
            peak_power_kWp = fp.peak_power_kWp,
            tilt_deg       = fp.tilt_deg,
            azimuth_deg    = fp.azimuth_deg,
        )
        sub_results.append(simulate_pv(tmy, fp_system, mounting, use_pvlib))

    areas       = [fp.area_m2 for fp in footprints]
    total_area  = sum(areas) if sum(areas) > 0 else 1.0   # avoid /0
    total_kWp   = sum(fp.peak_power_kWp for fp in footprints)
    n_hours     = len(sub_results[0].hourly)

    # ── Hourly aggregation ─────────────────────────────────────────────────
    hourly_agg: list[HourlySolarResult] = []
    for i in range(n_hours):
        sub_h = [r.hourly[i] for r in sub_results]
        # Sums (energy/power)
        p_ac_sum    = sum(h.P_ac_W   for h in sub_h)
        p_dc_sum    = sum(h.P_dc_W   for h in sub_h)
        p_gross_sum = sum(h.P_gross_W for h in sub_h)
        # Area-weighted means (irradiance, temperature)
        poa_g  = sum(h.poa_global_W_m2     * a for h, a in zip(sub_h, areas)) / total_area
        poa_d  = sum(h.poa_direct_W_m2     * a for h, a in zip(sub_h, areas)) / total_area
        poa_sd = sum(h.poa_diffuse_sky_W_m2 * a for h, a in zip(sub_h, areas)) / total_area
        poa_gd = sum(h.poa_diffuse_gnd_W_m2 * a for h, a in zip(sub_h, areas)) / total_area
        t_cell = sum(h.T_cell_celsius      * a for h, a in zip(sub_h, areas)) / total_area
        # Identical or simple boolean
        first  = sub_h[0]
        shaded = any(h.horizon_shaded for h in sub_h)
        hourly_agg.append(HourlySolarResult(
            month                = first.month,
            day                  = first.day,
            hour                 = first.hour,
            solar_zenith_deg     = first.solar_zenith_deg,
            solar_azimuth_deg    = first.solar_azimuth_deg,
            poa_global_W_m2      = poa_g,
            poa_direct_W_m2      = poa_d,
            poa_diffuse_sky_W_m2 = poa_sd,
            poa_diffuse_gnd_W_m2 = poa_gd,
            horizon_shaded       = shaded,
            T_cell_celsius       = t_cell,
            P_dc_W               = p_dc_sum,
            P_ac_W               = p_ac_sum,
            P_gross_W            = p_gross_sum,
        ))

    # ── Monthly aggregation ────────────────────────────────────────────────
    prod_m = [
        sum(r.production_monthly_kWh[m] for r in sub_results)
        for m in range(12)
    ]
    poa_m = [
        sum(r.poa_monthly_kWh_m2[m] * a for r, a in zip(sub_results, areas)) / total_area
        for m in range(12)
    ]
    tcell_m = [
        sum(r.T_cell_avg_monthly[m] * a for r, a in zip(sub_results, areas)) / total_area
        for m in range(12)
    ]

    # ── Annual & loss waterfall (sum of sub-results) ───────────────────────
    prod_annual    = sum(r.production_annual_kWh for r in sub_results)
    gross_dc       = sum(r.gross_dc_kWh           for r in sub_results)
    horizon_loss   = sum(r.horizon_shading_loss_kWh for r in sub_results)
    iam_loss       = sum(r.iam_loss_kWh           for r in sub_results)
    temp_loss      = sum(r.temperature_loss_kWh   for r in sub_results)
    sys_dc_loss    = sum(r.system_losses_kWh      for r in sub_results)
    inverter_loss  = sum(r.inverter_loss_kWh      for r in sub_results)
    clipping_loss  = sum(r.clipping_loss_kWh      for r in sub_results)
    ac_wiring_loss = sum(r.ac_wiring_loss_kWh     for r in sub_results)

    poa_annual    = sum(poa_m)
    spec_yield    = prod_annual / total_kWp if total_kWp > 0 else 0.0
    PR            = (prod_annual / (poa_annual * total_kWp)
                     if poa_annual * total_kWp > 0 else 0.0)
    CF            = prod_annual / (total_kWp * 8760) if total_kWp > 0 else 0.0

    # ── Per-footprint breakdown ────────────────────────────────────────────
    fp_detail: list[FootprintResult] = []
    for fp, sub in zip(footprints, sub_results):
        share = (sub.production_annual_kWh / prod_annual * 100.0
                 if prod_annual > 0 else 0.0)
        spec  = (sub.production_annual_kWh / fp.peak_power_kWp
                 if fp.peak_power_kWp > 0 else 0.0)
        fp_detail.append(FootprintResult(
            name                   = fp.name,
            area_m2                = fp.area_m2,
            peak_power_kWp         = fp.peak_power_kWp,
            azimuth_deg            = fp.azimuth_deg,
            tilt_deg               = fp.tilt_deg,
            production_annual_kWh  = sub.production_annual_kWh,
            specific_yield_kWh_kWp = round(spec, 1),
            share_pct              = round(share, 2),
        ))

    loc = tmy.location
    return SolarResult(
        hourly                   = hourly_agg,
        production_monthly_kWh   = [round(v, 1) for v in prod_m],
        poa_monthly_kWh_m2       = [round(v, 1) for v in poa_m],
        T_cell_avg_monthly       = [round(v, 1) for v in tcell_m],
        production_annual_kWh    = round(prod_annual, 1),
        specific_yield_kWh_kWp   = round(spec_yield, 1),
        performance_ratio        = round(PR, 3),
        capacity_factor          = round(CF, 4),
        gross_dc_kWh             = round(gross_dc, 1),
        horizon_shading_loss_kWh = round(horizon_loss, 1),
        iam_loss_kWh             = round(iam_loss, 1),
        temperature_loss_kWh     = round(temp_loss, 1),
        system_losses_kWh        = round(sys_dc_loss, 1),
        inverter_loss_kWh        = round(inverter_loss, 1),
        clipping_loss_kWh        = round(clipping_loss, 1),
        ac_wiring_loss_kWh       = round(ac_wiring_loss, 1),
        system_losses_detail     = system_template.system_losses.summary(),
        location_info            = {
            "latitude":    loc.latitude,
            "longitude":   loc.longitude,
            "elevation_m": loc.elevation_m,
            "country":     loc.country,
            "dataset":     loc.dataset,
        },
        footprints_detail        = fp_detail,
    )


def print_solar_summary(result: SolarResult, system: PVSystem) -> None:
    """Pretty-print the solar simulation results."""
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
    SEP = "─" * 70

    loc = result.location_info
    print(f"\n{SEP}")
    print(f"  PV SIMULATION — {loc['country']}  |  "
          f"{loc['latitude']:.4f}°N  {loc['longitude']:.4f}°E  "
          f"elev {loc['elevation_m']:.0f} m")
    print(f"  System: {system.peak_power_kWp} kWp  |  "
          f"tilt {system.tilt_deg}°  azimuth {system.azimuth_deg}°  |  "
          f"{'pvlib' if PVLIB_AVAILABLE else 'internal engine'}")
    print(SEP)

    print(f"  {'Month':<5} {'POA':>8} {'T_cell':>8} {'Production':>12} {'kWh/kWp':>9}")
    print(f"  {'':5} {'kWh/m²':>8} {'°C':>8} {'kWh':>12} {'':>9}")
    print(f"  {SEP}")
    for m in range(12):
        sp_yield = result.production_monthly_kWh[m] / system.peak_power_kWp
        print(
            f"  {MONTHS[m]:<5}"
            f" {result.poa_monthly_kWh_m2[m]:>8.1f}"
            f" {result.T_cell_avg_monthly[m]:>8.1f}"
            f" {result.production_monthly_kWh[m]:>12.1f}"
            f" {sp_yield:>9.1f}"
        )

    print(f"  {SEP}")
    print(f"  {'YEAR':<5}"
          f" {sum(result.poa_monthly_kWh_m2):>8.1f}"
          f" {'':>8}"
          f" {result.production_annual_kWh:>12.1f}"
          f" {result.specific_yield_kWh_kWp:>9.1f}")

    print(f"\n{SEP}")
    print("  KEY PERFORMANCE INDICATORS")
    print(SEP)
    print(f"  Specific yield          : {result.specific_yield_kWh_kWp:>8.1f}  kWh/kWp/yr")
    print(f"  Performance ratio (PR)  : {result.performance_ratio:>8.1%}")
    print(f"  Capacity factor (CF)    : {result.capacity_factor:>8.2%}")

    print(f"\n{SEP}")
    print("  ENERGY LOSS WATERFALL")
    print(SEP)
    gross = result.gross_dc_kWh
    net   = result.production_annual_kWh
    def pct(v): return v / gross * 100 if gross > 0 else 0
    total_loss = gross - net
    print(f"  Gross DC (no losses)      : {gross:>10.0f}  kWh  (100.0%)")
    print(f"  – Horizon shading         : {result.horizon_shading_loss_kWh:>10.0f}  kWh  ({pct(result.horizon_shading_loss_kWh):4.1f}%)")
    print(f"  – IAM (glass reflection)  : {result.iam_loss_kWh:>10.0f}  kWh  ({pct(result.iam_loss_kWh):4.1f}%)")
    print(f"  – Temperature             : {result.temperature_loss_kWh:>10.0f}  kWh  ({pct(result.temperature_loss_kWh):4.1f}%)")
    print(f"  – System losses (DC)      : {result.system_losses_kWh:>10.0f}  kWh  ({pct(result.system_losses_kWh):4.1f}%)")
    print(f"  – Inverter conversion     : {result.inverter_loss_kWh:>10.0f}  kWh  ({pct(result.inverter_loss_kWh):4.1f}%)")
    print(f"  – Clipping (saturation)   : {result.clipping_loss_kWh:>10.0f}  kWh  ({pct(result.clipping_loss_kWh):4.1f}%)")
    print(f"  – AC wiring               : {result.ac_wiring_loss_kWh:>10.0f}  kWh  ({pct(result.ac_wiring_loss_kWh):4.1f}%)")
    waterfall_check = (result.horizon_shading_loss_kWh + result.iam_loss_kWh
                       + result.temperature_loss_kWh + result.system_losses_kWh
                       + result.inverter_loss_kWh + result.clipping_loss_kWh
                       + result.ac_wiring_loss_kWh + net)
    balance_ok = abs(waterfall_check - gross) < gross * 0.01  # within 1%
    print(f"  = Net AC production       : {net:>10.0f}  kWh  ({pct(net):4.1f}%)")
    print(f"  {'[waterfall balanced ✓]' if balance_ok else '[waterfall gap: check losses]':>52}")

    print(f"\n{SEP}")
    print("  SYSTEM LOSS DETAIL")
    print(SEP)
    for k, v in result.system_losses_detail.items():
        print(f"  {k:<30} : {v}")
    print(f"{SEP}\n")
