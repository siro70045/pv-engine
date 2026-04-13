"""
engine/energy_balance.py

Hourly energy balance between PV production and building demand.
Calculates self-consumption, grid exchange, and battery dispatch.
Provides system sizing optimisation to maximise economic return.

Position in the calculation chain
-----------------------------------
    solar.py  ──► SolarResult.hourly_production_Wh
    loads.py  ──► LoadsResult.hourly_demand_Wh
                       │
                       ▼
              energy_balance.py  ──► EnergyBalanceResult
                       │
                       ▼
              economics.py  (VAN, TIR, payback)

Physical models
---------------
Battery        : single-node equivalent circuit with Coulombic efficiency,
                 depth-of-discharge limit, calendar ageing (linear capacity
                 fade), and a simple self-consumption-maximising dispatch
                 strategy (charge from surplus, discharge into deficit).
                 Reference: IEC 62619:2022, PVSYST battery model.

Grid exchange  : net metering or feed-in tariff, configurable per direction.

Sizing          : parametric sweep over kWp (and optionally battery kWh)
                 to find the system that maximises Net Present Value.
                 Computationally cheap because it reuses the same hourly
                 production shape (linear scaling with kWp).

Standards
---------
    IEC 62619:2022    Safety requirements for secondary lithium cells
    IEC 61724-1:2021  PV system performance monitoring
    EN 50549:2019     Requirements for generating plants connected to LV grid
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — INPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BatterySystem:
    """
    Electrochemical storage (BESS) parameters.

    Parameters
    ----------
    capacity_kWh : float
        Nominal usable energy capacity [kWh].
        0 = no battery.
    max_charge_power_kW : float
        Maximum charge rate [kW].
        Typical C-rate for LFP: 0.5C → 10 kWh / 0.5 = 5 kW charge.
        Default: capacity / 2  (0.5C charge rate).
    max_discharge_power_kW : float
        Maximum discharge rate [kW].
        Default: capacity  (1C discharge rate).
    roundtrip_efficiency : float
        AC-to-AC round-trip efficiency [0–1].
        LFP chemistry: 0.92–0.95.  NMC: 0.90–0.93.
        This is split evenly: η_charge = η_discharge = sqrt(η_roundtrip).
    depth_of_discharge : float
        Maximum usable fraction of nominal capacity [0–1].
        LFP: 0.90  (10% reserve).  NMC: 0.80.
        Sets the practical energy window: usable = capacity × DoD.
    min_soc_fraction : float
        Minimum state-of-charge [0–1] — hard lower bound.
        Prevents deep discharge damage.  Typically 0.05–0.10.
    initial_soc_fraction : float
        State-of-charge at start of simulation [0–1].
    calendar_degradation_pct_per_yr : float
        Annual capacity fade [%/yr] due to calendar ageing.
        LFP: 1.5–2.5 %/yr.  NMC: 2–4 %/yr.
    cycle_degradation_pct_per_cycle : float
        Capacity fade per equivalent full cycle [%/cycle].
        LFP: 0.003 %/cycle (≈ 3000 cycles to 80% capacity).
    inverter_efficiency : float
        Battery inverter / hybrid inverter efficiency [0–1].
        Already included in roundtrip_efficiency if using AC-coupled.
        Set to 1.0 if roundtrip_efficiency is AC-AC end-to-end.
    chemistry : str
        Battery chemistry label for reference.
        One of: "LFP", "NMC", "lead_acid", "NaS".
    """
    capacity_kWh: float = 0.0
    max_charge_power_kW: float = 0.0        # 0 → auto (capacity / 2)
    max_discharge_power_kW: float = 0.0     # 0 → auto (capacity × 1)
    roundtrip_efficiency: float = 0.93
    depth_of_discharge: float = 0.90
    min_soc_fraction: float = 0.05
    initial_soc_fraction: float = 0.50
    calendar_degradation_pct_per_yr: float = 2.0
    cycle_degradation_pct_per_cycle: float = 0.003
    inverter_efficiency: float = 1.0
    chemistry: str = "LFP"

    def __post_init__(self) -> None:
        if self.max_charge_power_kW == 0 and self.capacity_kWh > 0:
            self.max_charge_power_kW = self.capacity_kWh / 2.0
        if self.max_discharge_power_kW == 0 and self.capacity_kWh > 0:
            self.max_discharge_power_kW = self.capacity_kWh * 1.0

    @property
    def usable_capacity_kWh(self) -> float:
        """Energy window available for dispatch [kWh]."""
        return self.capacity_kWh * (self.depth_of_discharge - self.min_soc_fraction)

    @property
    def eta_charge(self) -> float:
        """One-way charging efficiency (sqrt of round-trip)."""
        return math.sqrt(self.roundtrip_efficiency)

    @property
    def eta_discharge(self) -> float:
        """One-way discharging efficiency (sqrt of round-trip)."""
        return math.sqrt(self.roundtrip_efficiency)


@dataclass
class GridTariff:
    """
    Electricity tariff structure for grid import and export.

    Parameters
    ----------
    import_price_eur_kWh : float
        Price paid for electricity drawn from the grid [€/kWh].
        Can be a single flat rate, or use monthly/hourly arrays.
    export_price_eur_kWh : float
        Price received for electricity fed into the grid [€/kWh].
        - Net metering (scambio sul posto): effectively = import price
          (tracked separately as credits, settled annually).
          Set export_is_net_metering = True for correct accounting.
        - Feed-in tariff (incentivo dedicato ritiro): fixed per-kWh price,
          typically 0.04–0.12 €/kWh.
        - 0 if excess is curtailed (not allowed by grid operator).
    export_is_net_metering : bool
        If True, exported energy is credited at import price and offset
        against import bill (Italian "scambio sul posto" SSP mechanism).
        If False, export_price_eur_kWh is paid directly (Ritiro Dedicato).
    net_metering_annual_cap_kWh : float
        Maximum annual export credits under net metering [kWh].
        0 = no cap.  Italian SSP: no explicit cap for small systems.
    time_of_use : bool
        If True, import price varies by hour (peak / off-peak).
    peak_hours : list[int]
        Hours (0–23) with peak import price. Used only if time_of_use=True.
    peak_price_multiplier : float
        Multiplier applied to import_price_eur_kWh during peak hours.
    fixed_charges_eur_yr : float
        Fixed annual grid charges (capacity quota, taxes) [€/yr].
        These do NOT scale with consumption but are included in cost calculation.
    """
    import_price_eur_kWh: float = 0.25
    export_price_eur_kWh: float = 0.08
    export_is_net_metering: bool = False
    net_metering_annual_cap_kWh: float = 0.0
    time_of_use: bool = False
    peak_hours: list[int] = field(default_factory=lambda: list(range(8, 21)))
    peak_price_multiplier: float = 1.5
    fixed_charges_eur_yr: float = 0.0
    export_limit_pct: float = 100.0
    # Maximum export power as a percentage of PV peak power [0–100].
    # 100 = no limit (default).
    # 70  = Italian/German 70% rule for grid-connected systems > 100 kWp.
    #       Any PV production that would exceed this fraction of peak power
    #       AND cannot be stored or self-consumed is curtailed (wasted).
    # Set export_limit_pct < 100 for systems subject to grid export limits.
    demand_charge_eur_kW_month: float = 0.0
    # Monthly peak-power charge [€/kW/month].
    # Applied to the maximum 15-min demand in each calendar month.
    # Common in Italian industrial tariffs (BT other, MT):
    #   Small industrial (<100 kW): 0–2 €/kW/month
    #   Medium industrial (100–1000 kW): 3–8 €/kW/month
    # Used by the peak-shaving battery strategy and financial calculations.


@dataclass
class EnergyBalanceInput:
    """
    Complete input for the energy balance module.

    Parameters
    ----------
    pv_production_hourly_Wh : list[float]
        8 760 hourly PV AC production values [Wh] from solar.py.
    demand_hourly_Wh : list[float]
        8 760 hourly electrical demand values [Wh] from loads.py.
    battery : BatterySystem
        Battery storage parameters. Use BatterySystem(capacity_kWh=0)
        for no storage.
    tariff : GridTariff
        Electricity tariff for cost calculation.
    peak_power_kWp : float
        PV system peak power [kWp] — used for specific yield calculation.
    simulation_year : int
        Year of simulation (1-based). Used for battery ageing.
        Year 1 = new system, year 25 = aged system.
    """
    pv_production_hourly_Wh: list[float]
    demand_hourly_Wh: list[float]
    battery: BatterySystem
    tariff: GridTariff
    peak_power_kWp: float
    simulation_year: int = 1
    battery_strategy: str = "self_consumption"
    # Dispatch strategy for the battery:
    # "self_consumption" (default) — charge from PV surplus, discharge into deficit.
    #     Maximises self-consumption and self-sufficiency.
    # "peak_shaving"               — charge during low-demand hours (night),
    #     discharge to flatten the demand peak.  Reduces demand charges
    #     (€/kW/month) in industrial tariffs.  Requires tariff.demand_charge_eur_kW_month > 0.
    battery_replacement_threshold: float = 0.80
    # Battery capacity fraction below which replacement is flagged [0–1].
    # Standard warranty threshold: 0.80 (80% of nominal capacity).
    # When aged capacity falls below this, battery_needs_replacement = True
    # in the result, signalling the economics module to include replacement cost.


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES — OUTPUTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HourlyBalanceRecord:
    """Energy flows for a single hour [Wh]."""
    pv_production_Wh: float     # AC PV generation
    demand_Wh: float            # building demand
    self_consumed_Wh: float     # PV directly consumed by building
    battery_charge_Wh: float    # energy flowing INTO battery
    battery_discharge_Wh: float # energy flowing OUT of battery
    grid_import_Wh: float       # energy drawn from grid
    grid_export_Wh: float       # energy fed into grid
    soc_Wh: float               # battery state-of-charge at end of hour
    curtailed_Wh: float         # excess PV that cannot be stored or exported


@dataclass
class MonthlyBalance:
    """Monthly aggregated energy balance [kWh/month]."""
    pv_production_kWh: list[float]      # 12 values
    demand_kWh: list[float]
    self_consumed_kWh: list[float]
    battery_charge_kWh: list[float]
    battery_discharge_kWh: list[float]
    grid_import_kWh: list[float]
    grid_export_kWh: list[float]
    curtailed_kWh: list[float]


@dataclass
class EnergyBalanceResult:
    """
    Complete output of the energy balance simulation.
    """
    # ── Annual energy flows [kWh/yr] ──────────────────────────────────────
    pv_production_kWh: float
    total_demand_kWh: float
    self_consumed_kWh: float          # PV directly used + from battery
    direct_self_consumed_kWh: float   # PV directly used (no battery)
    battery_contribution_kWh: float   # demand covered by battery discharge
    grid_import_kWh: float            # energy bought from grid
    grid_export_kWh: float            # energy sold / credited to grid
    curtailed_kWh: float              # excess PV wasted (no storage, no export)

    # ── Self-consumption and self-sufficiency rates ───────────────────────
    self_consumption_rate: float   # self_consumed / pv_production  [0-1]
    self_sufficiency_rate: float   # self_consumed / total_demand   [0-1]
    # Note:
    #   self_consumption_rate: what fraction of PV is used on-site
    #   self_sufficiency_rate: what fraction of demand is covered by PV

    # ── Financial metrics (year 1) [€/yr] ────────────────────────────────
    grid_cost_without_pv_eur: float   # baseline: all demand from grid
    grid_cost_with_pv_eur: float      # actual cost with PV + battery
    annual_saving_eur: float          # difference
    export_revenue_eur: float         # income from grid export
    net_annual_benefit_eur: float     # saving + export revenue

    # ── Battery performance (if installed) ───────────────────────────────
    battery_cycles_equivalent: float  # equivalent full cycles in year
    battery_capacity_kWh: float       # nominal capacity
    battery_soc_avg: float            # average state-of-charge [0-1]

    # ── Monthly breakdown ─────────────────────────────────────────────────
    monthly: MonthlyBalance

    # ── Hourly data ───────────────────────────────────────────────────────
    hourly: list[HourlyBalanceRecord]

    # ── System info ───────────────────────────────────────────────────────
    peak_power_kWp: float
    simulation_year: int
    battery_aged_capacity_kWh: float  # capacity after ageing in simulation_year
    battery_needs_replacement: bool   # True when aged capacity < replacement threshold
    peak_demand_W: float              # highest hourly demand in the year [W]
    peak_demand_after_pv_W: float     # highest net demand (demand - PV) [W]
    demand_charge_saving_eur: float   # saving from peak-shaving (0 if no demand charge)
    curtailment_loss_kWh: float       # energy curtailed by export limit


@dataclass
class SizingResult:
    """
    Output of the system sizing optimisation.
    """
    # Optimal configuration
    optimal_kWp: float
    optimal_battery_kWh: float

    # Sweep results: one entry per (kWp, battery_kWh) combination
    sweep: list[dict]    # keys: kWp, battery_kWh, self_consumption_rate,
                         #       self_sufficiency_rate, annual_saving_eur,
                         #       net_annual_benefit_eur

    # Annual benefit at optimal configuration
    optimal_annual_benefit_eur: float
    optimal_self_consumption_rate: float
    optimal_self_sufficiency_rate: float


# ══════════════════════════════════════════════════════════════════════════════
# BATTERY DISPATCH MODEL
# ══════════════════════════════════════════════════════════════════════════════

def _battery_step(
    surplus_Wh: float,       # positive = PV surplus; negative = PV deficit
    soc_Wh: float,           # current state-of-charge [Wh]
    battery: BatterySystem,
    aged_capacity_kWh: float,
) -> tuple[float, float, float, float]:
    """
    Simulate one hour of battery operation.

    Strategy: self-consumption maximisation.
    - Surplus: charge battery up to max_charge_power and capacity limits.
    - Deficit: discharge battery down to min_soc.

    Parameters
    ----------
    surplus_Wh : float
        PV surplus (positive) or demand deficit (negative) [Wh].
    soc_Wh : float
        Battery energy content at start of hour [Wh].
    battery : BatterySystem
    aged_capacity_kWh : float
        Current (possibly degraded) capacity [kWh].

    Returns
    -------
    (charge_Wh, discharge_Wh, new_soc_Wh, net_surplus_Wh)
        charge_Wh    : energy stored into battery (positive) [Wh]
        discharge_Wh : energy retrieved from battery (positive) [Wh]
        new_soc_Wh   : state-of-charge at end of hour [Wh]
        net_surplus_Wh : remaining surplus after battery (positive = export)
                         or remaining deficit (negative = grid import)
    """
    cap_Wh     = aged_capacity_kWh * 1000.0
    max_soc_Wh = cap_Wh * battery.depth_of_discharge
    min_soc_Wh = cap_Wh * battery.min_soc_fraction
    P_ch_max_Wh  = battery.max_charge_power_kW  * 1000.0   # max charge per hour
    P_dis_max_Wh = battery.max_discharge_power_kW * 1000.0

    charge_Wh    = 0.0
    discharge_Wh = 0.0

    if surplus_Wh > 0:
        # ── Charging: store surplus into battery ──────────────────────────
        # Energy that can be stored accounting for charge efficiency
        headroom_Wh = max_soc_Wh - soc_Wh          # space available
        # Max charge limited by power rating and available headroom
        # Input energy = stored energy / eta_charge (we pay more to store)
        max_store_Wh = min(headroom_Wh, P_ch_max_Wh * battery.eta_charge)
        charge_input_Wh = min(surplus_Wh, max_store_Wh / battery.eta_charge)
        stored_Wh       = charge_input_Wh * battery.eta_charge
        charge_Wh       = charge_input_Wh
        soc_Wh          = min(soc_Wh + stored_Wh, max_soc_Wh)
        surplus_Wh     -= charge_input_Wh

    elif surplus_Wh < 0:
        # ── Discharging: cover deficit from battery ───────────────────────
        deficit_Wh   = -surplus_Wh
        available_Wh = soc_Wh - min_soc_Wh          # energy available to dispatch
        # Output energy = dispatched energy × eta_discharge
        # We can only dispatch what gives us the needed output
        max_dispatch_Wh = min(available_Wh, P_dis_max_Wh)
        dispatched_Wh   = min(max_dispatch_Wh, deficit_Wh / battery.eta_discharge)
        output_Wh       = dispatched_Wh * battery.eta_discharge
        discharge_Wh    = output_Wh
        soc_Wh          = max(soc_Wh - dispatched_Wh, min_soc_Wh)
        surplus_Wh      = output_Wh - deficit_Wh    # may still be negative

    return charge_Wh, discharge_Wh, soc_Wh, surplus_Wh


# ══════════════════════════════════════════════════════════════════════════════
# BATTERY AGEING
# ══════════════════════════════════════════════════════════════════════════════

def _battery_aged_capacity(
    battery: BatterySystem,
    simulation_year: int,
    cycles_per_year: float = 250.0,
) -> float:
    """
    Effective battery capacity after calendar and cycle ageing [kWh].

    Model: linear capacity fade from two independent mechanisms.
        C(t) = C_nom × (1 - f_cal × t) × (1 - f_cyc × n_cycles)

    where t = simulation_year - 1 (years elapsed since installation).

    Parameters
    ----------
    simulation_year : int
        1 = new, 2 = after first year, etc.
    cycles_per_year : float
        Estimated equivalent full cycles per year.
        Typical residential: 150–250.  C&I: 300–500.
    """
    if battery.capacity_kWh <= 0:
        return 0.0

    years_elapsed = simulation_year - 1
    f_cal  = battery.calendar_degradation_pct_per_yr / 100.0
    f_cyc  = battery.cycle_degradation_pct_per_cycle / 100.0
    n_cyc  = cycles_per_year * years_elapsed

    capacity = battery.capacity_kWh * max(1.0 - f_cal * years_elapsed, 0.0) * \
               max(1.0 - f_cyc * n_cyc, 0.0)
    return max(capacity, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# CORE HOURLY SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

# ── Peak-shaving helpers ─────────────────────────────────────────────────────

def _compute_peak_shaving_target(demand_hourly_Wh: list[float],
                                  battery: BatterySystem,
                                  aged_cap_kWh: float) -> list[float]:
    """
    Pre-compute a monthly target peak power [W] for peak-shaving strategy.

    The target is the demand level below which the battery will try to keep
    net grid draw: target_W = monthly_peak_W - shaveable_kW × 1000.

    We estimate how much peak can be shaved based on battery discharge power
    and typical peak duration (~2 h). This is a conservative static estimate;
    the hourly simulation enforces the actual physics.
    """
    _DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    targets = []
    hour = 0
    for m, days in enumerate(_DAYS):
        month_hours = days * 24
        month_demand = demand_hourly_Wh[hour: hour + month_hours]
        peak_W = max(month_demand) if month_demand else 0.0
        # Battery can shave: discharge_power_W for ~2 h per event
        # Shaveable power = min(discharge_power, (usable_capacity/2h))
        shaveable_W = min(
            battery.max_discharge_power_kW * 1000,
            aged_cap_kWh * 1000 * battery.depth_of_discharge / 2.0,
        )
        targets.append(max(peak_W - shaveable_W, peak_W * 0.5))
        hour += month_hours
    return targets


def _simulate_hourly(inp: EnergyBalanceInput) -> tuple[list[HourlyBalanceRecord], float]:
    """
    Run the 8 760-hour energy balance simulation.

    Implements two battery strategies:

    "self_consumption" (default):
        Charge from PV surplus → discharge into demand deficit.
        Maximises SCR and SSR.

    "peak_shaving":
        Charge during low-demand off-peak hours (night/early morning)
        when grid draw is below the monthly target threshold.
        Discharge when grid draw would exceed the monthly target.
        Reduces peak demand [W] and associated demand charges [€/kW/month].
        Note: a hybrid strategy (self-consumption + peak-shaving) is not
        implemented here; use "peak_shaving" when demand charges dominate.

    Returns (hourly_records, battery_cycles_equivalent).
    """
    n_hours = len(inp.pv_production_hourly_Wh)
    if n_hours != len(inp.demand_hourly_Wh):
        raise ValueError(
            f"pv_production and demand must both have 8760 values. "
            f"Got {n_hours} and {len(inp.demand_hourly_Wh)}."
        )

    battery      = inp.battery
    has_battery  = battery.capacity_kWh > 0
    aged_cap_kWh = _battery_aged_capacity(battery, inp.simulation_year)
    strategy     = inp.battery_strategy
    tariff       = inp.tariff

    # Export power limit [W]: e.g. 70% rule
    export_limit_frac = tariff.export_limit_pct / 100.0
    export_limit_W    = inp.peak_power_kWp * 1000.0 * export_limit_frac

    # Initial SoC
    soc_Wh = aged_cap_kWh * 1000.0 * battery.initial_soc_fraction if has_battery else 0.0

    # Pre-compute monthly peak-shaving targets if needed
    ps_targets_W: list[float] = []
    if strategy == "peak_shaving" and has_battery:
        ps_targets_W = _compute_peak_shaving_target(
            inp.demand_hourly_Wh, battery, aged_cap_kWh
        )

    # Map hour → month index for peak-shaving target lookup
    _DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    hour_to_month = []
    for m, days in enumerate(_DAYS):
        hour_to_month.extend([m] * (days * 24))

    records: list[HourlyBalanceRecord] = []
    cumulative_charge_Wh = 0.0

    for i in range(n_hours):
        pv_Wh     = max(inp.pv_production_hourly_Wh[i], 0.0)
        demand_Wh = max(inp.demand_hourly_Wh[i], 0.0)
        hour_of_day = i % 24
        month_idx   = hour_to_month[i] if i < len(hour_to_month) else 11

        # ── Step 1: direct self-consumption ──────────────────────────────────
        direct_Wh  = min(pv_Wh, demand_Wh)
        surplus_Wh = pv_Wh - demand_Wh   # >0 = PV excess, <0 = deficit

        # ── Step 2: battery dispatch ──────────────────────────────────────────
        charge_Wh = discharge_Wh = 0.0

        if has_battery and aged_cap_kWh > 0:
            if strategy == "self_consumption":
                # Standard: charge surplus, discharge deficit
                charge_Wh, discharge_Wh, soc_Wh, surplus_Wh = _battery_step(
                    surplus_Wh, soc_Wh, battery, aged_cap_kWh
                )
                cumulative_charge_Wh += charge_Wh

            elif strategy == "peak_shaving":
                # Peak shaving: discharge to keep net grid draw below monthly target.
                # Charge only from PV surplus (no grid-to-battery charging —
                # that would require energy arbitrage logic beyond this module's scope).
                target_W = ps_targets_W[month_idx] if ps_targets_W else 0.0

                net_draw_W = demand_Wh - pv_Wh

                if net_draw_W > target_W:
                    # Peak detected → discharge battery to cut the peak
                    discharge_needed_Wh = net_draw_W - target_W
                    ch, dis, soc_Wh, _ = _battery_step(
                        -discharge_needed_Wh, soc_Wh, battery, aged_cap_kWh
                    )
                    discharge_Wh = dis
                    surplus_Wh   = pv_Wh - demand_Wh + discharge_Wh

                elif surplus_Wh > 0:
                    # PV surplus → charge battery (same as self-consumption)
                    ch, dis, soc_Wh, surplus_Wh = _battery_step(
                        surplus_Wh, soc_Wh, battery, aged_cap_kWh
                    )
                    charge_Wh = ch

                cumulative_charge_Wh += charge_Wh

        # ── Step 3: export limit / curtailment ────────────────────────────────
        grid_export_Wh = max(surplus_Wh, 0.0)
        grid_import_Wh = max(-surplus_Wh, 0.0)
        curtailed_Wh   = 0.0

        if grid_export_Wh > 0 and export_limit_W < inp.peak_power_kWp * 1000.0:
            # Export is power-limited: excess that cannot be stored or fed in
            # is curtailed (lost).
            # Power limit applies to instantaneous export [W = Wh/h at 1h resolution]
            allowed_export_Wh = min(grid_export_Wh, export_limit_W)
            curtailed_Wh      = grid_export_Wh - allowed_export_Wh
            grid_export_Wh    = allowed_export_Wh

        records.append(HourlyBalanceRecord(
            pv_production_Wh    = pv_Wh,
            demand_Wh           = demand_Wh,
            self_consumed_Wh    = direct_Wh + discharge_Wh,
            battery_charge_Wh   = charge_Wh,
            battery_discharge_Wh= discharge_Wh,
            grid_import_Wh      = grid_import_Wh,
            grid_export_Wh      = grid_export_Wh,
            soc_Wh              = soc_Wh,
            curtailed_Wh        = curtailed_Wh,
        ))

    # Equivalent full cycles
    cycles = (cumulative_charge_Wh / 1000.0 / aged_cap_kWh
              if aged_cap_kWh > 0 else 0.0)

    return records, cycles


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

# Days per month (non-leap year) — month index 0 = January
_DAYS_PER_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

def _aggregate(
    hourly: list[HourlyBalanceRecord],
    inp: EnergyBalanceInput,
    cycles: float,
) -> EnergyBalanceResult:
    """Aggregate hourly records into monthly and annual totals."""

    n_months = 12
    pv_m   = [0.0] * n_months
    dem_m  = [0.0] * n_months
    sc_m   = [0.0] * n_months  # self-consumed
    bch_m  = [0.0] * n_months  # battery charge
    bdi_m  = [0.0] * n_months  # battery discharge
    imp_m  = [0.0] * n_months  # grid import
    exp_m  = [0.0] * n_months  # grid export
    cur_m  = [0.0] * n_months  # curtailed
    soc_sum = 0.0

    # Map hour index → month
    month_of_hour: list[int] = []
    for m, days in enumerate(_DAYS_PER_MONTH):
        month_of_hour.extend([m] * (days * 24))

    direct_sc_Wh = 0.0

    for i, rec in enumerate(hourly):
        m = month_of_hour[i] if i < len(month_of_hour) else 11
        pv_m[m]  += rec.pv_production_Wh
        dem_m[m] += rec.demand_Wh
        sc_m[m]  += rec.self_consumed_Wh
        bch_m[m] += rec.battery_charge_Wh
        bdi_m[m] += rec.battery_discharge_Wh
        imp_m[m] += rec.grid_import_Wh
        exp_m[m] += rec.grid_export_Wh
        cur_m[m] += rec.curtailed_Wh
        soc_sum  += rec.soc_Wh

        # Direct self-consumption = min(pv, demand) per hour
        direct_sc_Wh += min(rec.pv_production_Wh, rec.demand_Wh)

    # Convert Wh → kWh
    def to_kwh(lst: list[float]) -> list[float]:
        return [round(v / 1000.0, 1) for v in lst]

    pv_m_k  = to_kwh(pv_m)
    dem_m_k = to_kwh(dem_m)
    sc_m_k  = to_kwh(sc_m)
    bch_m_k = to_kwh(bch_m)
    bdi_m_k = to_kwh(bdi_m)
    imp_m_k = to_kwh(imp_m)
    exp_m_k = to_kwh(exp_m)
    cur_m_k = to_kwh(cur_m)

    pv_annual   = sum(pv_m_k)
    dem_annual  = sum(dem_m_k)
    sc_annual   = sum(sc_m_k)
    bdi_annual  = sum(bdi_m_k)
    imp_annual  = sum(imp_m_k)
    exp_annual  = sum(exp_m_k)
    cur_annual  = sum(cur_m_k)
    direct_sc   = round(direct_sc_Wh / 1000.0, 1)
    bat_contrib = round(bdi_annual, 1)

    scr = sc_annual / pv_annual   if pv_annual  > 0 else 0.0
    ssr = sc_annual / dem_annual  if dem_annual > 0 else 0.0

    # ── Financial calculation ─────────────────────────────────────────────
    tariff = inp.tariff

    # Baseline cost: all demand imported from grid
    # (uses average import price; TOU not applied to baseline for simplicity)
    baseline_cost = dem_annual * tariff.import_price_eur_kWh

    # Actual cost with PV: only grid_import is charged
    # Time-of-use pricing: apply hourly
    # Define hourly import price function (mirrors the one in _simulate_hourly)
    def _import_price_at(hour_of_day: int) -> float:
        if tariff.time_of_use and hour_of_day in tariff.peak_hours:
            return tariff.import_price_eur_kWh * tariff.peak_price_multiplier
        return tariff.import_price_eur_kWh

    if tariff.time_of_use:
        actual_cost = 0.0
        for i, rec in enumerate(hourly):
            h = i % 24
            actual_cost += rec.grid_import_Wh / 1000.0 * _import_price_at(h)
        actual_cost = round(actual_cost, 2)
    else:
        actual_cost = round(imp_annual * tariff.import_price_eur_kWh, 2)

    actual_cost += tariff.fixed_charges_eur_yr

    # Export revenue
    if tariff.export_is_net_metering:
        # Net metering: export credited at import price, offset against import
        # Cap applied if specified
        net_meter_credit = exp_annual
        if tariff.net_metering_annual_cap_kWh > 0:
            net_meter_credit = min(net_meter_credit, tariff.net_metering_annual_cap_kWh)
        export_revenue = net_meter_credit * tariff.import_price_eur_kWh
    else:
        export_revenue = round(exp_annual * tariff.export_price_eur_kWh, 2)

    annual_saving     = round(baseline_cost - actual_cost, 2)
    net_annual_benefit = round(annual_saving + export_revenue, 2)

    # Battery avg SoC
    bat_cap_Wh = inp.battery.capacity_kWh * 1000.0
    soc_avg    = (soc_sum / len(hourly) / bat_cap_Wh
                  if bat_cap_Wh > 0 else 0.0)

    aged_cap = _battery_aged_capacity(inp.battery, inp.simulation_year)

    # ── Battery replacement flag ──────────────────────────────────────────────
    needs_replacement = (
        inp.battery.capacity_kWh > 0
        and aged_cap < inp.battery.capacity_kWh * inp.battery_replacement_threshold
    )

    # ── Peak demand metrics ───────────────────────────────────────────────────
    peak_demand_W = max((rec.demand_Wh for rec in hourly), default=0.0)

    # Net demand after PV (before battery): what the grid sees at peak
    peak_net_demand_W = max(
        (rec.demand_Wh - rec.pv_production_Wh for rec in hourly), default=0.0
    )

    # Demand charge saving: difference in monthly peak before and after battery
    # Only meaningful when strategy == "peak_shaving"
    demand_charge_saving = 0.0
    if inp.tariff.demand_charge_eur_kW_month > 0:
        # Monthly peak net demand [W] — what the grid sees after PV and battery
        _DAYS_AGG = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        h_idx = 0
        for m, days in enumerate(_DAYS_AGG):
            month_recs = hourly[h_idx: h_idx + days * 24]
            # Peak grid draw this month [kW]
            peak_with_bat_kW  = max(
                (r.grid_import_Wh for r in month_recs), default=0.0
            ) / 1000.0
            # Peak without battery: demand - direct_PV
            peak_no_bat_kW = max(
                (r.demand_Wh - min(r.pv_production_Wh, r.demand_Wh)
                 for r in month_recs), default=0.0
            ) / 1000.0
            saving_kW = max(peak_no_bat_kW - peak_with_bat_kW, 0.0)
            demand_charge_saving += saving_kW * inp.tariff.demand_charge_eur_kW_month
            h_idx += days * 24

    net_annual_benefit_with_dc = round(net_annual_benefit + demand_charge_saving, 2)

    return EnergyBalanceResult(
        pv_production_kWh         = round(pv_annual,   1),
        total_demand_kWh          = round(dem_annual,  1),
        self_consumed_kWh         = round(sc_annual,   1),
        direct_self_consumed_kWh  = round(direct_sc,   1),
        battery_contribution_kWh  = bat_contrib,
        grid_import_kWh           = round(imp_annual,  1),
        grid_export_kWh           = round(exp_annual,  1),
        curtailed_kWh             = round(cur_annual,  1),
        self_consumption_rate     = round(scr, 4),
        self_sufficiency_rate     = round(ssr, 4),
        grid_cost_without_pv_eur  = round(baseline_cost, 2),
        grid_cost_with_pv_eur     = round(actual_cost,   2),
        annual_saving_eur         = round(annual_saving,  2),
        export_revenue_eur        = round(export_revenue, 2),
        net_annual_benefit_eur    = net_annual_benefit_with_dc,
        battery_cycles_equivalent = round(cycles, 1),
        battery_capacity_kWh      = inp.battery.capacity_kWh,
        battery_soc_avg           = round(soc_avg, 3),
        monthly                   = MonthlyBalance(
            pv_production_kWh     = pv_m_k,
            demand_kWh            = dem_m_k,
            self_consumed_kWh     = sc_m_k,
            battery_charge_kWh    = bch_m_k,
            battery_discharge_kWh = bdi_m_k,
            grid_import_kWh       = imp_m_k,
            grid_export_kWh       = exp_m_k,
            curtailed_kWh         = cur_m_k,
        ),
        hourly                    = hourly,
        peak_power_kWp            = inp.peak_power_kWp,
        simulation_year           = inp.simulation_year,
        battery_aged_capacity_kWh = round(aged_cap, 3),
        battery_needs_replacement = needs_replacement,
        peak_demand_W             = round(peak_demand_W, 1),
        peak_demand_after_pv_W    = round(peak_net_demand_W, 1),
        demand_charge_saving_eur  = round(demand_charge_saving, 2),
        curtailment_loss_kWh      = round(cur_annual, 1),
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE — SINGLE YEAR SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS — MODULE INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def extract_pv_profile(solar_result: object) -> list[float]:
    """
    Extract the 8 760-hour AC production profile [Wh/h] from a SolarResult.

    This is the bridge between solar.py and energy_balance.py.
    solar_result must be the object returned by solar.simulate_pv().

    Parameters
    ----------
    solar_result : SolarResult
        Object returned by engine.solar.simulate_pv().

    Returns
    -------
    list[float]
        8 760 hourly AC energy values [Wh] ready for EnergyBalanceInput.

    Example
    -------
    >>> from engine.solar import simulate_pv, PVSystem
    >>> from engine.energy_balance import extract_pv_profile, EnergyBalanceInput
    >>>
    >>> solar_result = simulate_pv(tmy, PVSystem(peak_power_kWp=100))
    >>> pv_profile   = extract_pv_profile(solar_result)
    >>> inp = EnergyBalanceInput(
    ...     pv_production_hourly_Wh = pv_profile,
    ...     demand_hourly_Wh        = loads_result.hourly_demand_Wh,
    ...     ...
    ... )
    """
    if not hasattr(solar_result, "hourly"):
        raise TypeError(
            "solar_result must be a SolarResult from engine.solar.simulate_pv(). "
            f"Got: {type(solar_result)}"
        )
    hourly = solar_result.hourly
    if not hourly:
        raise ValueError("solar_result.hourly is empty.")
    # HourlySolarResult.P_ac_W is AC power [W].
    # At 1-hour resolution: W × 1h = Wh.
    return [max(h.P_ac_W, 0.0) for h in hourly]


def find_battery_replacement_years(
    battery: BatterySystem,
    lifetime_years: int = 25,
    threshold: float = 0.80,
    cycles_per_year: float = 250.0,
) -> list[int]:
    """
    Find the years in which the battery capacity falls below the threshold.

    Returns a list of year numbers (1-based) where replacement is needed.
    In practice, the FIRST element is when replacement should be scheduled;
    subsequent elements would apply to the replacement battery.

    Parameters
    ----------
    battery : BatterySystem
    lifetime_years : int
    threshold : float
        Replacement threshold as fraction of nominal capacity (default 0.80).
    cycles_per_year : float
        Estimated annual equivalent full cycles.

    Returns
    -------
    list[int]
        Years (1-based) where capacity first falls below threshold.
        Empty list if battery never degrades below threshold in lifetime.

    Example
    -------
    >>> years = find_battery_replacement_years(BatterySystem(capacity_kWh=100), 25)
    >>> print(f"First replacement needed in year {years[0] if years else 'never'}")
    """
    if battery.capacity_kWh <= 0:
        return []

    replacement_years = []
    was_above = True

    for year in range(1, lifetime_years + 1):
        cap = _battery_aged_capacity(battery, year, cycles_per_year)
        fraction = cap / battery.capacity_kWh
        if fraction < threshold and was_above:
            replacement_years.append(year)
            was_above = False
        elif fraction >= threshold:
            was_above = True  # reset if somehow recovers (shouldn't happen with linear model)

    return replacement_years


def simulate_energy_balance(inp: EnergyBalanceInput) -> EnergyBalanceResult:
    """
    Run the full 8 760-hour energy balance for one simulation year.

    Parameters
    ----------
    inp : EnergyBalanceInput
        PV production profile, demand profile, battery, tariff.

    Returns
    -------
    EnergyBalanceResult
        Complete energy flows: self-consumption, grid exchange,
        battery dispatch, financial savings.

    Example
    -------
    >>> from engine.solar import simulate_pv, PVSystem
    >>> from engine.loads import calc_loads
    >>> from engine.energy_balance import *
    >>>
    >>> solar_result = simulate_pv(tmy, PVSystem(peak_power_kWp=120))
    >>> loads_result = calc_loads(loads_input)
    >>>
    >>> inp = EnergyBalanceInput(
    ...     pv_production_hourly_Wh=solar_result.hourly_production_Wh,
    ...     demand_hourly_Wh=loads_result.hourly_demand_Wh,
    ...     battery=BatterySystem(capacity_kWh=0),
    ...     tariff=GridTariff(import_price_eur_kWh=0.25),
    ...     peak_power_kWp=120.0,
    ... )
    >>> result = simulate_energy_balance(inp)
    >>> print(f"Self-consumption rate: {result.self_consumption_rate:.1%}")
    >>> print(f"Annual saving:         €{result.annual_saving_eur:.0f}")
    """
    if len(inp.pv_production_hourly_Wh) != 8760:
        raise ValueError(
            f"pv_production_hourly_Wh must have exactly 8760 values, "
            f"got {len(inp.pv_production_hourly_Wh)}."
        )
    hourly, cycles = _simulate_hourly(inp)
    return _aggregate(hourly, inp, cycles)


def simulate_lifetime(
    base_inp: EnergyBalanceInput,
    lifetime_years: int = 25,
    pv_degradation_pct_per_yr: float = 0.50,
) -> list[EnergyBalanceResult]:
    """
    Run the energy balance for each year of the system lifetime.

    PV production degrades by pv_degradation_pct_per_yr each year.
    Battery capacity degrades per its own ageing model.
    Tariff prices are held constant (economics.py applies escalation).

    Returns a list of EnergyBalanceResult, one per year (index 0 = year 1).

    Example
    -------
    >>> yearly = simulate_lifetime(base_inp, lifetime_years=25)
    >>> productions = [r.pv_production_kWh for r in yearly]
    >>> savings      = [r.net_annual_benefit_eur for r in yearly]
    """
    results: list[EnergyBalanceResult] = []
    deg = pv_degradation_pct_per_yr / 100.0

    for year in range(1, lifetime_years + 1):
        # Scale PV production for this year's degradation
        factor = max(1.0 - deg * (year - 1), 0.0)
        scaled_pv = [v * factor for v in base_inp.pv_production_hourly_Wh]

        year_inp = EnergyBalanceInput(
            pv_production_hourly_Wh = scaled_pv,
            demand_hourly_Wh        = base_inp.demand_hourly_Wh,
            battery                 = base_inp.battery,
            tariff                  = base_inp.tariff,
            peak_power_kWp          = base_inp.peak_power_kWp,
            simulation_year         = year,
        )
        results.append(simulate_energy_balance(year_inp))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM SIZING OPTIMISATION
# ══════════════════════════════════════════════════════════════════════════════

def optimise_system_size(
    base_pv_hourly_Wh: list[float],
    demand_hourly_Wh: list[float],
    tariff: GridTariff,
    kwp_range: list[float],
    battery_kwh_range: Optional[list[float]] = None,
    battery_template: Optional[BatterySystem] = None,
) -> SizingResult:
    """
    Parametric sweep to find the PV (and battery) size that maximises
    net annual benefit.

    The base_pv_hourly_Wh profile is assumed to be generated at 1 kWp
    (or any reference kWp — it will be linearly scaled).
    Pass solar_result.hourly for the actual system and divide by kWp
    to get the per-kWp profile.

    Parameters
    ----------
    base_pv_hourly_Wh : list[float]
        8760-hour PV profile at REFERENCE power (e.g. from simulate_pv
        at 1 kWp, or scaled from any kWp by dividing by peak_power_kWp).
    demand_hourly_Wh : list[float]
        8760-hour demand profile (fixed — does not scale with PV size).
    tariff : GridTariff
        Electricity tariff for financial evaluation.
    kwp_range : list[float]
        kWp values to evaluate, e.g. [50, 75, 100, 120, 150, 200].
    battery_kwh_range : list[float], optional
        Battery sizes to evaluate, e.g. [0, 50, 100, 200].
        If None or [0], only no-battery scenario is evaluated.
    battery_template : BatterySystem, optional
        Battery template whose capacity_kWh will be overridden for each
        point in battery_kwh_range. If None, default LFP parameters used.

    Returns
    -------
    SizingResult
        Optimal kWp and battery kWh, full sweep table.

    Example
    -------
    >>> # Generate 1-kWp reference profile
    >>> ref_system = PVSystem(peak_power_kWp=1.0, tilt_deg=15, azimuth_deg=180)
    >>> ref_solar  = simulate_pv(tmy, ref_system)
    >>> ref_profile = [h.P_ac_W for h in ref_solar.hourly]  # W = Wh per hour at 1 kWp
    >>>
    >>> sizing = optimise_system_size(
    ...     base_pv_hourly_Wh = ref_profile,
    ...     demand_hourly_Wh  = loads_result.hourly_demand_Wh,
    ...     tariff            = GridTariff(import_price_eur_kWh=0.25),
    ...     kwp_range         = [50, 75, 100, 120, 150, 200],
    ...     battery_kwh_range = [0, 100, 200],
    ... )
    >>> print(f"Optimal: {sizing.optimal_kWp} kWp + {sizing.optimal_battery_kWh} kWh battery")
    """
    if battery_kwh_range is None or len(battery_kwh_range) == 0:
        battery_kwh_range = [0.0]

    bat_tmpl = battery_template or BatterySystem()

    sweep: list[dict] = []
    best_benefit = -1e18
    best_kwp     = kwp_range[0]
    best_bat_kwh = battery_kwh_range[0]

    for kwp in kwp_range:
        scaled_pv = [v * kwp for v in base_pv_hourly_Wh]

        for bat_kwh in battery_kwh_range:
            # Build battery for this size
            bat = BatterySystem(
                capacity_kWh                  = bat_kwh,
                max_charge_power_kW           = bat_tmpl.max_charge_power_kW * (bat_kwh / bat_tmpl.capacity_kWh) if bat_kwh > 0 and bat_tmpl.capacity_kWh > 0 else bat_kwh / 2.0,
                max_discharge_power_kW        = bat_tmpl.max_discharge_power_kW * (bat_kwh / bat_tmpl.capacity_kWh) if bat_kwh > 0 and bat_tmpl.capacity_kWh > 0 else bat_kwh,
                roundtrip_efficiency          = bat_tmpl.roundtrip_efficiency,
                depth_of_discharge            = bat_tmpl.depth_of_discharge,
                min_soc_fraction              = bat_tmpl.min_soc_fraction,
                initial_soc_fraction          = bat_tmpl.initial_soc_fraction,
                calendar_degradation_pct_per_yr = bat_tmpl.calendar_degradation_pct_per_yr,
                cycle_degradation_pct_per_cycle = bat_tmpl.cycle_degradation_pct_per_cycle,
            ) if bat_kwh > 0 else BatterySystem(capacity_kWh=0.0)

            inp = EnergyBalanceInput(
                pv_production_hourly_Wh = scaled_pv,
                demand_hourly_Wh        = demand_hourly_Wh,
                battery                 = bat,
                tariff                  = tariff,
                peak_power_kWp          = kwp,
            )
            res = simulate_energy_balance(inp)

            entry = {
                "kWp":                        kwp,
                "battery_kWh":                bat_kwh,
                "self_consumption_rate":      res.self_consumption_rate,
                "self_sufficiency_rate":      res.self_sufficiency_rate,
                "pv_production_kWh":          res.pv_production_kWh,
                "grid_import_kWh":            res.grid_import_kWh,
                "grid_export_kWh":            res.grid_export_kWh,
                "annual_saving_eur":          res.annual_saving_eur,
                "export_revenue_eur":         res.export_revenue_eur,
                "net_annual_benefit_eur":     res.net_annual_benefit_eur,
            }
            sweep.append(entry)

            if res.net_annual_benefit_eur > best_benefit:
                best_benefit  = res.net_annual_benefit_eur
                best_kwp      = kwp
                best_bat_kwh  = bat_kwh

    # Best result details
    best_entries = [e for e in sweep
                    if e["kWp"] == best_kwp and e["battery_kWh"] == best_bat_kwh]
    best = best_entries[0] if best_entries else sweep[0]

    return SizingResult(
        optimal_kWp                  = best_kwp,
        optimal_battery_kWh          = best_bat_kwh,
        sweep                        = sweep,
        optimal_annual_benefit_eur   = round(best_benefit, 2),
        optimal_self_consumption_rate= best["self_consumption_rate"],
        optimal_self_sufficiency_rate= best["self_sufficiency_rate"],
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRINT SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def print_balance_summary(result: EnergyBalanceResult) -> None:
    """Pretty-print the energy balance results."""
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
    SEP = "─" * 74

    print(f"\n{SEP}")
    print(f"  ENERGY BALANCE — {result.peak_power_kWp} kWp PV"
          + (f"  +  {result.battery_capacity_kWh} kWh battery"
             if result.battery_capacity_kWh > 0 else "  (no battery)")
          + f"  |  year {result.simulation_year}")
    print(SEP)

    print(f"  PV production              : {result.pv_production_kWh:>9.0f}  kWh/yr")
    print(f"  Total demand               : {result.total_demand_kWh:>9.0f}  kWh/yr")
    print(f"  Self-consumed (direct)     : {result.direct_self_consumed_kWh:>9.0f}  kWh/yr")
    if result.battery_capacity_kWh > 0:
        print(f"  Battery contribution       : {result.battery_contribution_kWh:>9.0f}  kWh/yr")
    print(f"  Total self-consumed        : {result.self_consumed_kWh:>9.0f}  kWh/yr")
    print(f"  Grid import                : {result.grid_import_kWh:>9.0f}  kWh/yr")
    print(f"  Grid export                : {result.grid_export_kWh:>9.0f}  kWh/yr")
    if result.curtailed_kWh > 0:
        print(f"  Curtailed                  : {result.curtailed_kWh:>9.0f}  kWh/yr")

    print(f"\n{SEP}")
    print("  KEY PERFORMANCE INDICATORS")
    print(SEP)
    print(f"  Self-consumption rate      : {result.self_consumption_rate:>9.1%}")
    print(f"  Self-sufficiency rate      : {result.self_sufficiency_rate:>9.1%}")
    if result.battery_capacity_kWh > 0:
        print(f"  Battery equiv. cycles/yr   : {result.battery_cycles_equivalent:>9.1f}")
        print(f"  Battery avg SoC            : {result.battery_soc_avg:>9.1%}")
        print(f"  Battery aged capacity      : {result.battery_aged_capacity_kWh:>9.2f}  kWh")
        replacement_str = "YES — schedule replacement" if result.battery_needs_replacement else "no"
        print(f"  Battery needs replacement  :  {replacement_str}")

    print(f"\n{SEP}")
    print("  FINANCIAL SUMMARY  (year 1)")
    print(SEP)
    print(f"  Baseline grid cost         : {result.grid_cost_without_pv_eur:>9.0f}  €/yr")
    print(f"  Grid cost with PV          : {result.grid_cost_with_pv_eur:>9.0f}  €/yr")
    print(f"  Energy saving              : {result.annual_saving_eur:>9.0f}  €/yr")
    print(f"  Export revenue             : {result.export_revenue_eur:>9.0f}  €/yr")
    if result.demand_charge_saving_eur > 0:
        print(f"  Demand charge saving       : {result.demand_charge_saving_eur:>9.0f}  €/yr")
    print(f"  Net annual benefit         : {result.net_annual_benefit_eur:>9.0f}  €/yr")
    if result.curtailment_loss_kWh > 0:
        print(f"  Curtailed (export limit)   : {result.curtailment_loss_kWh:>9.0f}  kWh/yr  ← lost production")

    print(f"\n{SEP}")
    m = result.monthly
    print(f"  {'Month':<5} {'PV':>7} {'Demand':>7} {'Self-c':>7}"
          f" {'Bat-d':>6} {'Import':>7} {'Export':>7}")
    print(f"  {'':5} {'kWh':>7} {'kWh':>7} {'kWh':>7}"
          f" {'kWh':>6} {'kWh':>7} {'kWh':>7}")
    print(f"  {SEP}")
    for i in range(12):
        print(
            f"  {MONTHS[i]:<5}"
            f" {m.pv_production_kWh[i]:>7.0f}"
            f" {m.demand_kWh[i]:>7.0f}"
            f" {m.self_consumed_kWh[i]:>7.0f}"
            f" {m.battery_discharge_kWh[i]:>6.0f}"
            f" {m.grid_import_kWh[i]:>7.0f}"
            f" {m.grid_export_kWh[i]:>7.0f}"
        )
    print(f"  {SEP}")
    print(
        f"  {'YEAR':<5}"
        f" {result.pv_production_kWh:>7.0f}"
        f" {result.total_demand_kWh:>7.0f}"
        f" {result.self_consumed_kWh:>7.0f}"
        f" {result.battery_contribution_kWh:>6.0f}"
        f" {result.grid_import_kWh:>7.0f}"
        f" {result.grid_export_kWh:>7.0f}"
    )
    print(f"{SEP}\n")
