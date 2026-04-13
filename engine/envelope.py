"""
engine/envelope.py

Building envelope thermal model.
Standards: EN ISO 13790 (monthly energy balance),
           EN ISO 13370 (ground heat transfer),
           EN ISO 14683 (thermal bridges).

Output: monthly and annual heating / cooling demand [kWh].
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import math


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OpaqueComponent:
    """
    A wall, roof, or any opaque envelope element.

    Parameters
    ----------
    name : str
        Descriptive label (e.g. "north wall", "flat roof").
    U : float
        Thermal transmittance [W/m²K].
    area_m2 : float
        Net area (excluding windows and doors) [m²].
    surface_mass_kg_m2 : float
        Surface mass (areal density) [kg/m²] — used for thermal inertia.
    phase_shift_h : float
        Thermal phase shift (decrement delay) [h].
    adjacent_space : str
        What is on the other side:
        "external" | "unheated" | "heated" | "ground".
    orientation_deg : float
        Azimuth of the outward normal [°]: 0=N, 90=E, 180=S, 270=W.
        Used for opaque solar absorption on walls (optional, pass 0 if unused).
    solar_absorption : float
        Absorptance of the outer surface (0–1). Typical: 0.6 light, 0.9 dark.
    """
    name: str
    U: float
    area_m2: float
    surface_mass_kg_m2: float
    phase_shift_h: float
    adjacent_space: str
    orientation_deg: float = 180.0
    solar_absorption: float = 0.6


@dataclass
class LinearThermalBridge:
    """
    A linear thermal bridge (e.g. slab edge, window reveal, wall corner).
    Reference: EN ISO 14683 — catalogue values for Ψ.

    Parameters
    ----------
    name : str
    psi : float
        Linear thermal transmittance [W/mK].
    length_m : float
        Total length of the junction [m].
    """
    name: str
    psi: float
    length_m: float


@dataclass
class PointThermalBridge:
    """
    A point thermal bridge (e.g. metal fastener, structural pin).
    Reference: EN ISO 10211.

    Parameters
    ----------
    name : str
    chi : float
        Point thermal transmittance [W/K].
    count : int
        Number of identical elements.
    """
    name: str
    chi: float
    count: int


@dataclass
class Window:
    """
    A glazed element (window, skylight, curtain wall section).

    Parameters
    ----------
    name : str
    U_total : float
        Total window U-value (glass + frame combined) [W/m²K].
    g_value : float
        Total solar energy transmittance (0–1). Typical: 0.3–0.65.
    area_m2 : float
        Gross area including frame [m²].
    orientation_deg : float
        Azimuth of glazing normal [°]: 0=N, 90=E, 180=S, 270=W.
    tilt_deg : float
        Tilt from horizontal [°]: 90=vertical, 0=horizontal skylight.
    frame_fraction : float
        Fraction of total area occupied by the frame (0–1). Typical: 0.2–0.35.
    shading_factor : float
        Combined shading correction (0–1): obstructions, overhangs, fins.
        1.0 = no shading. Compute per EN ISO 13790 Annex F if needed.
    """
    name: str
    U_total: float
    g_value: float
    area_m2: float
    orientation_deg: float
    tilt_deg: float = 90.0
    frame_fraction: float = 0.25
    shading_factor: float = 1.0

    @property
    def glazed_area_m2(self) -> float:
        """Net transparent area [m²]."""
        return self.area_m2 * (1.0 - self.frame_fraction)


@dataclass
class GroundFloor:
    """
    Floor in contact with the ground.
    Heat loss calculated per EN ISO 13370 simplified slab-on-grade method.

    Parameters
    ----------
    area_m2 : float
        Floor area [m²].
    perimeter_m : float
        Exposed perimeter of the floor (external walls in contact with ground) [m].
    U_floor : float
        Floor construction U-value (without ground correction) [W/m²K].
    wall_thickness_m : float
        Thickness of external walls [m].
    ground_lambda : float
        Thermal conductivity of ground [W/mK]. Typical: 1.5 (clay), 2.0 (rock).
    edge_insulation_R : float
        Additional edge insulation R-value [m²K/W]. 0 if none.
    """
    area_m2: float
    perimeter_m: float
    U_floor: float
    wall_thickness_m: float
    ground_lambda: float = 1.5
    edge_insulation_R: float = 0.0


@dataclass
class Envelope:
    """
    Complete building envelope.

    Parameters
    ----------
    walls : list[OpaqueComponent]
    roof : OpaqueComponent
    windows : list[Window]
    ground_floor : GroundFloor
    linear_bridges : list[LinearThermalBridge]
    point_bridges : list[PointThermalBridge]
    heated_volume_m3 : float
        Internal volume of the conditioned space [m³].
    infiltration_ach : float
        Air infiltration rate [h⁻¹] at normal pressure difference.
    mechanical_ach : float
        Mechanical ventilation rate [h⁻¹]. 0 if no HVAC system.
    heat_recovery_efficiency : float
        Heat recovery efficiency of ventilation unit (0–1). 0 if none.
    """
    walls: list[OpaqueComponent]
    roof: OpaqueComponent
    windows: list[Window]
    ground_floor: GroundFloor
    linear_bridges: list[LinearThermalBridge] = field(default_factory=list)
    point_bridges: list[PointThermalBridge] = field(default_factory=list)
    heated_volume_m3: float = 0.0
    infiltration_ach: float = 0.5
    mechanical_ach: float = 0.0
    heat_recovery_efficiency: float = 0.0


@dataclass
class EnvelopeResult:
    """All outputs produced by the envelope module."""
    # Heat loss coefficients
    H_tr_W_K: float          # Transmission heat loss coefficient [W/K]
    H_gr_W_K: float          # Ground heat loss coefficient [W/K]
    H_ve_W_K: float          # Ventilation heat loss coefficient [W/K]
    H_total_W_K: float       # Total heat loss coefficient [W/K]
    # Annual totals
    heating_demand_kWh: float
    cooling_demand_kWh: float
    # Monthly profiles [12 values, index 0 = January]
    heating_monthly_kWh: list[float]
    cooling_monthly_kWh: list[float]
    solar_gains_monthly_kWh: list[float]
    internal_gains_monthly_kWh: list[float]
    # Diagnostics
    component_breakdown: dict
    specific_heat_loss_W_m2K: float   # H_total / floor area — quality indicator


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Volumetric heat capacity of air [Wh/m³K]
RHO_CP_AIR = 0.33

# Rsi + Rse (internal + external surface resistance) [m²K/W]
# EN ISO 6946: horizontal heat flow (walls)
R_SURFACE_WALL = 0.13 + 0.04

# Monthly hours [h] — standard year
HOURS_PER_MONTH = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]

# Temperature reduction factors b for unheated adjacent spaces
# EN ISO 13789 Table 3
ADJACENT_SPACE_B = {
    "external":   1.00,
    "unheated":   0.50,   # unheated buffer zone (attic, garage, corridor)
    "heated":     0.00,   # no heat loss to adjacent heated space
}


# ══════════════════════════════════════════════════════════════════════════════
# GROUND FLOOR — EN ISO 13370 SIMPLIFIED SLAB-ON-GRADE
# ══════════════════════════════════════════════════════════════════════════════

def _ground_equivalent_thickness(gf: GroundFloor) -> float:
    """
    Equivalent thickness of the floor slab for EN ISO 13370.
    dt = w + lambda_g × (Rsi + Rf + Rse)
    where Rf = 1/U_floor - Rsi - Rse.
    """
    Rf = max(1.0 / gf.U_floor - R_SURFACE_WALL, 0.0)
    dt = gf.wall_thickness_m + gf.ground_lambda * (R_SURFACE_WALL + Rf + gf.edge_insulation_R)
    return dt


def calc_ground_heat_loss(gf: GroundFloor) -> float:
    """
    Ground heat loss coefficient H_g [W/K] per EN ISO 13370 eq. 1.

    For dt < B':
        H_g = (2 × lambda_g / pi × B' + dt) × ln(pi × B' / dt + 1)
    For dt >= B':
        H_g = 2 × lambda_g × A / (0.457 × B' + dt)

    B' = A / (0.5 × P)  — characteristic floor dimension
    """
    gf_A = gf.area_m2
    gf_P = gf.perimeter_m
    lam = gf.ground_lambda
    dt = _ground_equivalent_thickness(gf)

    B_prime = gf_A / (0.5 * gf_P) if gf_P > 0 else 1e6

    if dt < B_prime:
        H_g = (2 * lam * gf_A / (math.pi * B_prime + dt)) * math.log(math.pi * B_prime / dt + 1)
    else:
        H_g = 2 * lam * gf_A / (0.457 * B_prime + dt)

    return round(H_g, 3)


# ══════════════════════════════════════════════════════════════════════════════
# TRANSMISSION HEAT LOSS — EN ISO 13789
# ══════════════════════════════════════════════════════════════════════════════

def calc_transmission_heat_loss(env: Envelope) -> tuple[float, dict]:
    """
    Total transmission heat loss coefficient H_tr [W/K].

    H_tr = Σ(b_i × U_i × A_i)  [opaque + windows]
         + Σ(Ψ_j × L_j)         [linear bridges]
         + Σ(χ_k × n_k)         [point bridges]

    Returns H_tr and a detailed breakdown dict.
    """
    breakdown = {}
    H_tr = 0.0

    # ── Opaque walls ──
    for w in env.walls:
        if w.adjacent_space == "ground":
            continue  # handled separately by EN ISO 13370
        b = ADJACENT_SPACE_B.get(w.adjacent_space)
        if b is None:
            raise ValueError(
                f"Unknown adjacent_space '{w.adjacent_space}' in '{w.name}'. "
                f"Valid values: {list(ADJACENT_SPACE_B.keys())} or 'ground'."
            )
        contrib = b * w.U * w.area_m2
        H_tr += contrib
        breakdown[w.name] = {
            "type": "opaque_wall",
            "U_W_m2K": w.U,
            "area_m2": w.area_m2,
            "b_factor": b,
            "H_contrib_W_K": round(contrib, 3),
        }

    # ── Roof ──
    b_roof = ADJACENT_SPACE_B.get(env.roof.adjacent_space, 1.0)
    contrib = b_roof * env.roof.U * env.roof.area_m2
    H_tr += contrib
    breakdown["roof"] = {
        "type": "roof",
        "U_W_m2K": env.roof.U,
        "area_m2": env.roof.area_m2,
        "b_factor": b_roof,
        "H_contrib_W_K": round(contrib, 3),
    }

    # ── Windows ──
    for win in env.windows:
        contrib = win.U_total * win.area_m2
        H_tr += contrib
        breakdown[win.name] = {
            "type": "window",
            "U_total_W_m2K": win.U_total,
            "area_m2": win.area_m2,
            "b_factor": 1.0,
            "H_contrib_W_K": round(contrib, 3),
        }

    # ── Linear thermal bridges ──
    total_linear = 0.0
    for lb in env.linear_bridges:
        contrib = lb.psi * lb.length_m
        total_linear += contrib
        breakdown[lb.name] = {
            "type": "linear_bridge",
            "psi_W_mK": lb.psi,
            "length_m": lb.length_m,
            "H_contrib_W_K": round(contrib, 3),
        }
    H_tr += total_linear

    # ── Point thermal bridges ──
    total_point = 0.0
    for pb in env.point_bridges:
        contrib = pb.chi * pb.count
        total_point += contrib
        breakdown[pb.name] = {
            "type": "point_bridge",
            "chi_W_K": pb.chi,
            "count": pb.count,
            "H_contrib_W_K": round(contrib, 3),
        }
    H_tr += total_point

    return round(H_tr, 3), breakdown


# ══════════════════════════════════════════════════════════════════════════════
# VENTILATION HEAT LOSS — EN ISO 13789
# ══════════════════════════════════════════════════════════════════════════════

def calc_ventilation_heat_loss(env: Envelope) -> float:
    """
    Ventilation + infiltration heat loss coefficient H_ve [W/K].

    H_ve = rho_cp × [ V × n_inf  +  V × n_mech × (1 - eta_hr) ]

    Infiltration and mechanical ventilation are treated separately
    because heat recovery only applies to mechanical flow.
    """
    if env.heated_volume_m3 <= 0:
        raise ValueError("heated_volume_m3 must be > 0.")

    V = env.heated_volume_m3
    H_inf  = RHO_CP_AIR * V * env.infiltration_ach
    H_mech = RHO_CP_AIR * V * env.mechanical_ach * (1.0 - env.heat_recovery_efficiency)
    return round(H_inf + H_mech, 3)


# ══════════════════════════════════════════════════════════════════════════════
# SOLAR GAINS THROUGH WINDOWS — EN ISO 13790 §11.3
# ══════════════════════════════════════════════════════════════════════════════

def _irradiance_on_surface(
    orientation_deg: float,
    tilt_deg: float,
    monthly_irradiance_horizontal: list[float],
    monthly_irradiance_direct: list[float],
    latitude_deg: float,
) -> list[float]:
    """
    Estimate monthly irradiance on a tilted, oriented surface [kWh/m²/month]
    using the isotropic sky model (Liu & Jordan).

    Parameters
    ----------
    orientation_deg : azimuth of surface normal (0=N, 180=S) [°]
    tilt_deg : tilt from horizontal [°]
    monthly_irradiance_horizontal : global horizontal irradiance [kWh/m²/month]
    monthly_irradiance_direct : direct normal irradiance [kWh/m²/month]
    latitude_deg : site latitude [°]
    """
    beta = math.radians(tilt_deg)
    gamma = math.radians(orientation_deg - 180)  # surface azimuth vs South
    phi = math.radians(latitude_deg)

    days_mid_month = [17, 47, 75, 105, 135, 162, 198, 228, 258, 288, 318, 344]

    result = []
    for m in range(12):
        # Solar declination for mid-month day
        day = days_mid_month[m]
        delta = math.radians(23.45 * math.sin(math.radians(360 * (284 + day) / 365)))

        # Monthly average daily hours of daylight (sunrise hour angle)
        omega_s = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
        N = 2 * math.degrees(omega_s) / 15  # daylight hours

        G_h = monthly_irradiance_horizontal[m]  # kWh/m²/month
        G_d_approx = G_h * 0.55               # diffuse fraction (rough)
        G_b = max(G_h - G_d_approx, 0)        # beam component

        # View factor sky–surface and ground–surface
        F_sky = (1 + math.cos(beta)) / 2
        F_gnd = (1 - math.cos(beta)) / 2
        rho_g = 0.2  # ground reflectance

        # Beam component on tilted surface (monthly average)
        cos_theta = (
            math.sin(delta) * math.sin(phi) * math.cos(beta)
            - math.sin(delta) * math.cos(phi) * math.sin(beta) * math.cos(gamma)
            + math.cos(delta) * math.cos(phi) * math.cos(beta) * math.cos(math.radians(15))
            + math.cos(delta) * math.sin(phi) * math.sin(beta) * math.cos(gamma) * math.cos(math.radians(15))
            + math.cos(delta) * math.sin(beta) * math.sin(gamma) * math.sin(math.radians(15))
        )
        cos_theta_z = math.sin(delta) * math.sin(phi) + math.cos(delta) * math.cos(phi) * math.cos(math.radians(15))
        cos_theta_z = max(cos_theta_z, 0.001)

        Rb = max(cos_theta, 0) / cos_theta_z

        G_tilt = G_b * Rb + G_d_approx * F_sky + G_h * rho_g * F_gnd
        result.append(max(G_tilt, 0))

    return result


def calc_solar_gains(
    env: Envelope,
    monthly_irradiance_horizontal: list[float],
    monthly_irradiance_direct: list[float],
    latitude_deg: float,
) -> list[float]:
    """
    Monthly solar heat gains through all windows [kWh/month].

    Q_sol,w = Σ_windows( F_sh × (1 - F_f) × g_value × A_w × I_tilt )

    where:
        F_sh  = shading_factor
        F_f   = frame_fraction (already in glazed_area_m2)
        g_value = total solar transmittance
        I_tilt = monthly irradiance on the window surface [kWh/m²]

    Note: g_value should already account for normal incidence.
    An incidence angle modifier (IAM) could be applied for more precision
    but is omitted here as EN ISO 13790 §11.3 uses average monthly irradiance.
    """
    gains = [0.0] * 12
    for win in env.windows:
        I_tilt = _irradiance_on_surface(
            orientation_deg=win.orientation_deg,
            tilt_deg=win.tilt_deg,
            monthly_irradiance_horizontal=monthly_irradiance_horizontal,
            monthly_irradiance_direct=monthly_irradiance_direct,
            latitude_deg=latitude_deg,
        )
        for m in range(12):
            q = win.shading_factor * win.glazed_area_m2 * win.g_value * I_tilt[m]
            gains[m] += q

    return [round(g, 2) for g in gains]


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL GAINS — EN ISO 13790 §9.4 / UNI TS 11300
# ══════════════════════════════════════════════════════════════════════════════

# Specific internal heat gain [W/m²] by building type
# Source: UNI TS 11300-1 Table A.3 / EN ISO 13790 §9.4
INTERNAL_GAIN_W_M2: dict[str, float] = {
    "residential":        4.0,   # 3.5–6 W/m² depending on occupancy
    "office":            10.0,   # 8–12 W/m²
    "warehouse":          3.0,   # low occupancy, minimal lighting load
    "factory_light":      6.0,   # light manufacturing
    "factory_heavy":     12.0,   # heavy equipment, motors
    "retail":            10.0,
    "school":             7.0,
}

def calc_internal_gains(
    floor_area_m2: float,
    building_type: str = "residential",
) -> list[float]:
    """
    Monthly internal heat gains [kWh/month].
    Uses average occupancy and a constant specific power [W/m²].

    For buildings with known process loads (factories), the caller
    should override these values with measured data.
    """
    q_int = INTERNAL_GAIN_W_M2.get(building_type)
    if q_int is None:
        raise ValueError(
            f"Unknown building_type '{building_type}'. "
            f"Valid options: {list(INTERNAL_GAIN_W_M2.keys())}"
        )
    total_W = q_int * floor_area_m2
    return [round(total_W * h / 1000, 2) for h in HOURS_PER_MONTH]


# ══════════════════════════════════════════════════════════════════════════════
# UTILISATION FACTOR — EN ISO 13790 §12.2 / §13.2
# ══════════════════════════════════════════════════════════════════════════════

def _utilisation_factor(gamma: float, a: float, mode: str) -> float:
    """
    EN ISO 13790 utilisation factor η.

    For heating (mode='heat'):
        η_H = (1 - γ^a) / (1 - γ^(a+1))    if γ ≠ 1
        η_H = a / (a + 1)                    if γ = 1
        η_H = 1 / γ                          if γ < 0 (rare)

    For cooling (mode='cool'): symmetric formula with γ = losses/gains.

    γ : gain-to-loss ratio (heat mode) or loss-to-gain ratio (cool mode)
    a : dimensionless parameter related to thermal mass
    """
    if gamma <= 0:
        return 1.0 if mode == "heat" else 0.0
    if abs(gamma - 1.0) < 1e-6:
        return a / (a + 1.0)
    if mode == "heat":
        return (1.0 - gamma**a) / (1.0 - gamma**(a + 1.0))
    else:  # cool
        return (1.0 - gamma**a) / (1.0 - gamma**(a + 1.0))


def _thermal_mass_parameter(env: Envelope, floor_area_m2: float) -> float:
    """
    Dimensionless numerical parameter a (EN ISO 13790 eq. 61).

    a = a_0 + tau / tau_0
    a_0 = 1.0, tau_0 = 15 h (reference time constant)

    tau = C_m / (3.6 × H_total)   [h]  — building time constant
    C_m = effective heat capacity of the building [kJ/K]

    C_m is estimated from surface mass of internal-facing elements.
    Per EN ISO 13790 §12.3.1.2:
        C_m = Σ(k_j × A_j)
    where k_j is the internal heat capacity per unit area [kJ/m²K].

    Simplified: we use surface_mass × 0.48 kJ/kgK (specific heat of concrete/brick).
    """
    C_specific_kJ_kgK = 0.48  # concrete / brick
    C_m_kJ_K = sum(
        w.surface_mass_kg_m2 * w.area_m2 * C_specific_kJ_kgK
        for w in env.walls
    )
    # Add roof and floor contributions (typically lighter effect — use 50% weight)
    C_m_kJ_K += 0.5 * env.roof.surface_mass_kg_m2 * env.roof.area_m2 * C_specific_kJ_kgK
    C_m_kJ_K += 0.5 * env.ground_floor.area_m2 * 200 * C_specific_kJ_kgK  # approx slab

    return C_m_kJ_K  # caller computes tau using H_total


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY ENERGY BALANCE — EN ISO 13790
# ══════════════════════════════════════════════════════════════════════════════

def calc_thermal_demand(
    envelope: Envelope,
    floor_area_m2: float,
    building_type: str,
    monthly_T_ext: list[float],
    monthly_irradiance_horizontal: list[float],
    monthly_irradiance_direct: list[float],
    latitude_deg: float,
    T_heating_setpoint: float = 20.0,
    T_cooling_setpoint: float = 26.0,
) -> EnvelopeResult:
    """
    Compute monthly heating and cooling demand using EN ISO 13790 method.

    Heating demand (month m):
        Q_H,nd = Q_loss,H - η_H × Q_gains
        Q_loss,H = H_total × (T_set,H - T_ext) × t  [kWh]

    Cooling demand (month m):
        Q_C,nd = Q_gains - η_C × Q_loss,C
        Q_loss,C = H_total × (T_ext - T_set,C) × t  [kWh]

    Parameters
    ----------
    envelope : Envelope
    floor_area_m2 : float
        Conditioned floor area [m²].
    building_type : str
        See INTERNAL_GAIN_W_M2 keys.
    monthly_T_ext : list[float]
        12 monthly average outdoor temperatures [°C].
    monthly_irradiance_horizontal : list[float]
        12 monthly global horizontal irradiance values [kWh/m²/month].
    monthly_irradiance_direct : list[float]
        12 monthly direct normal irradiance values [kWh/m²/month].
    latitude_deg : float
        Site latitude [°].
    T_heating_setpoint : float
        Indoor heating set-point [°C].
    T_cooling_setpoint : float
        Indoor cooling set-point [°C].
    """
    if len(monthly_T_ext) != 12:
        raise ValueError("monthly_T_ext must have exactly 12 values.")

    # ── Heat loss coefficients ──
    H_tr, breakdown = calc_transmission_heat_loss(envelope)
    H_gr = calc_ground_heat_loss(envelope.ground_floor)
    H_ve = calc_ventilation_heat_loss(envelope)
    H_total = H_tr + H_gr + H_ve

    breakdown["ground_floor_EN13370"] = {
        "type": "ground_floor",
        "area_m2": envelope.ground_floor.area_m2,
        "H_contrib_W_K": round(H_gr, 3),
    }
    breakdown["ventilation"] = {
        "type": "ventilation",
        "infiltration_ach": envelope.infiltration_ach,
        "mechanical_ach": envelope.mechanical_ach,
        "heat_recovery": envelope.heat_recovery_efficiency,
        "H_contrib_W_K": round(H_ve, 3),
    }

    # ── Gains ──
    Q_sol = calc_solar_gains(
        envelope,
        monthly_irradiance_horizontal,
        monthly_irradiance_direct,
        latitude_deg,
    )
    Q_int = calc_internal_gains(floor_area_m2, building_type)

    # ── Thermal mass parameter ──
    C_m_kJ_K = _thermal_mass_parameter(envelope, floor_area_m2)
    # a_0 = 1.0, tau_0 = 15 h (EN ISO 13790 Table 12)
    a0, tau0 = 1.0, 15.0

    heat_monthly: list[float] = []
    cool_monthly: list[float] = []

    for m in range(12):
        hours = HOURS_PER_MONTH[m]
        T_ext = monthly_T_ext[m]
        Q_gain = Q_sol[m] + Q_int[m]

        # Time constant τ [h] — EN ISO 13790 eq. 62
        H_eff = H_total if H_total > 0 else 1e-6
        tau = C_m_kJ_K / (3.6 * H_eff)
        a = a0 + tau / tau0

        # ── Heating ──
        dT_H = T_heating_setpoint - T_ext
        Q_loss_H = H_total * max(dT_H, 0.0) * hours / 1000.0  # kWh

        if Q_loss_H > 1e-6:
            gamma_H = Q_gain / Q_loss_H
            eta_H = _utilisation_factor(gamma_H, a, mode="heat")
            Q_H = max(Q_loss_H - eta_H * Q_gain, 0.0)
        else:
            Q_H = 0.0

        # ── Cooling ──
        dT_C = T_ext - T_cooling_setpoint
        Q_loss_C = H_total * max(dT_C, 0.0) * hours / 1000.0  # kWh
        # For cooling, gains drive demand; losses are beneficial
        if Q_gain > 1e-6:
            gamma_C = Q_loss_C / Q_gain if Q_gain > 0 else 0.0
            eta_C = _utilisation_factor(gamma_C, a, mode="cool")
            Q_C = max(Q_gain - eta_C * Q_loss_C, 0.0)
        else:
            Q_C = 0.0

        heat_monthly.append(round(Q_H, 1))
        cool_monthly.append(round(Q_C, 1))

    return EnvelopeResult(
        H_tr_W_K=H_tr,
        H_gr_W_K=H_gr,
        H_ve_W_K=H_ve,
        H_total_W_K=round(H_total, 3),
        heating_demand_kWh=round(sum(heat_monthly), 1),
        cooling_demand_kWh=round(sum(cool_monthly), 1),
        heating_monthly_kWh=heat_monthly,
        cooling_monthly_kWh=cool_monthly,
        solar_gains_monthly_kWh=Q_sol,
        internal_gains_monthly_kWh=Q_int,
        component_breakdown=breakdown,
        specific_heat_loss_W_m2K=round(H_total / floor_area_m2, 3),
    )
