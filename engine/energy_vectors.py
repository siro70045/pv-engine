"""
engine/energy_vectors.py

Data structures for multi-vector energy systems.

Defines all input types needed to describe a building's current energy mix
(gas, electricity, oil, district heating) and the equipment being evaluated
(gas boiler, heat pump, bivalent system, thermal distribution).

These types are consumed by:
    heat_pump.py    — hourly COP and thermal demand simulation
    scenarios.py    — scenario comparison engine

CO₂ emission factors
---------------------
    Natural gas     :  0.202 kg CO₂/kWh_th  (combustion, lower heating value)
                       or 0.252 kg CO₂/kWh_th including upstream (ISPRA 2023)
    Diesel/gasoil   :  0.267 kg CO₂/kWh_th  (combustion)
    LPG/GPL         :  0.234 kg CO₂/kWh_th  (combustion)
    Electricity IT  :  0.233 kg CO₂/kWh_el  (ENTSO-E IT grid mix 2023)
                       0.175 kg CO₂/kWh_el  (ENTSO-E IT forecast 2030)
    District heat   :  0.070 kg CO₂/kWh_th  (typical CHP-based, varies by provider)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# FUEL AND CARRIER TYPES
# ══════════════════════════════════════════════════════════════════════════════

class FuelType(str, Enum):
    """Primary energy carrier."""
    NATURAL_GAS      = "natural_gas"
    ELECTRICITY      = "electricity"
    DIESEL           = "diesel"
    LPG              = "lpg"
    DISTRICT_HEAT    = "district_heat"
    WOOD_PELLETS     = "wood_pellets"
    NONE             = "none"


class TerminalType(str, Enum):
    """Hydronic heat distribution terminal — determines HP supply temperature."""
    FAN_COIL         = "fan_coil"         # 40–45°C supply — ideal for HP
    RADIANT_FLOOR    = "radiant_floor"    # 35–40°C supply — best for HP
    RADIANT_CEILING  = "radiant_ceiling"  # 35–40°C supply — good for HP
    LOW_TEMP_PANEL   = "low_temp_panel"   # 45–55°C supply — acceptable for HP
    HIGH_TEMP_PANEL  = "high_temp_panel"  # 65–75°C supply — poor for HP, needs HT-HP
    OLD_RADIATOR     = "old_radiator"     # 70–80°C supply — requires HT-HP or terminal replacement
    AIR_HANDLING     = "air_handling"     # direct air heating — moderate supply temp
    NONE             = "none"             # no wet system (air-to-air HP)


# Supply temperature ranges per terminal type [°C]
# Used to derive HP operating conditions
TERMINAL_SUPPLY_TEMP_C: dict[TerminalType, tuple[float, float]] = {
    TerminalType.FAN_COIL:        (40.0, 45.0),
    TerminalType.RADIANT_FLOOR:   (35.0, 40.0),
    TerminalType.RADIANT_CEILING: (35.0, 40.0),
    TerminalType.LOW_TEMP_PANEL:  (45.0, 55.0),
    TerminalType.HIGH_TEMP_PANEL: (60.0, 70.0),
    TerminalType.OLD_RADIATOR:    (70.0, 80.0),
    TerminalType.AIR_HANDLING:    (45.0, 55.0),
    TerminalType.NONE:            (30.0, 40.0),
}


# ══════════════════════════════════════════════════════════════════════════════
# ENERGY PRICES AND ESCALATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EnergyPrice:
    """
    Price and escalation assumptions for one energy carrier.

    Parameters
    ----------
    price_eur_per_unit : float
        Current price [€/unit]. Unit depends on carrier:
        - Electricity : €/kWh
        - Natural gas  : €/Sm³  (standard cubic metre, ~10.7 kWh/Sm³ LHV)
        - Diesel       : €/litre (~10.0 kWh/litre)
        - LPG          : €/kg    (~13.1 kWh/kg)
        - District heat: €/kWh_th
    unit_kwh_conversion : float
        Conversion factor: 1 unit = N kWh of useful energy.
        Used to normalise to €/kWh_th or €/kWh_el.
        Examples:
            Natural gas 10.7 kWh/Sm³ × boiler efficiency 0.90 = 9.63 kWh_th/Sm³
            Electricity: 1.0 kWh_el / kWh
            Diesel: 10.0 kWh/litre × efficiency 0.88 = 8.8 kWh_th/litre
    annual_escalation_rate : float
        Expected annual price increase [fraction]. E.g. 0.03 = 3%/yr.
        Historical Italy natural gas 2010–2023: ~4.5%/yr with high volatility.
        Conservative estimate for modelling: 0.02–0.04.
    co2_factor_kg_per_unit : float
        CO₂ emissions per unit [kg CO₂/unit].
        Set to 0 if emissions already included in unit_kwh_conversion-based factor.
    """
    price_eur_per_unit: float
    unit_kwh_conversion: float = 1.0
    annual_escalation_rate: float = 0.025
    co2_factor_kg_per_unit: float = 0.0

    @property
    def price_eur_per_kwh(self) -> float:
        """Normalised price [€/kWh] — comparable across carriers."""
        if self.unit_kwh_conversion <= 0:
            return 0.0
        return self.price_eur_per_unit / self.unit_kwh_conversion

    @property
    def co2_factor_kg_per_kwh(self) -> float:
        """Normalised CO₂ factor [kg CO₂/kWh]."""
        if self.unit_kwh_conversion <= 0:
            return 0.0
        return self.co2_factor_kg_per_unit / self.unit_kwh_conversion


# Default Italian prices 2024 — update from ARERA bulletins
DEFAULT_PRICES: dict[FuelType, EnergyPrice] = {
    FuelType.NATURAL_GAS: EnergyPrice(
        price_eur_per_unit      = 0.95,     # €/Sm³ — residential ARERA 2024
        unit_kwh_conversion     = 9.63,     # Sm³ × 10.7 kWh/Sm³ × 0.90 boiler eff
        annual_escalation_rate  = 0.03,
        co2_factor_kg_per_unit  = 1.96,     # kg CO₂/Sm³ combustion
    ),
    FuelType.ELECTRICITY: EnergyPrice(
        price_eur_per_unit      = 0.25,     # €/kWh — industrial BT Other
        unit_kwh_conversion     = 1.0,
        annual_escalation_rate  = 0.025,
        co2_factor_kg_per_unit  = 0.233,    # kg CO₂/kWh — IT grid 2023
    ),
    FuelType.DIESEL: EnergyPrice(
        price_eur_per_unit      = 1.45,     # €/litre — heating oil 2024
        unit_kwh_conversion     = 8.80,     # litre × 10.0 kWh × 0.88 boiler eff
        annual_escalation_rate  = 0.025,
        co2_factor_kg_per_unit  = 2.64,     # kg CO₂/litre
    ),
    FuelType.LPG: EnergyPrice(
        price_eur_per_unit      = 0.85,     # €/litre liquid
        unit_kwh_conversion     = 6.90,     # litre × 7.5 kWh × 0.92 boiler eff
        annual_escalation_rate  = 0.025,
        co2_factor_kg_per_unit  = 1.63,     # kg CO₂/litre
    ),
    FuelType.DISTRICT_HEAT: EnergyPrice(
        price_eur_per_unit      = 0.09,     # €/kWh_th — variable by provider
        unit_kwh_conversion     = 1.0,
        annual_escalation_rate  = 0.02,
        co2_factor_kg_per_unit  = 0.070,    # kg CO₂/kWh_th — CHP-based
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING HEATING SYSTEM (baseline)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GasBoilerSystem:
    """
    Existing gas (or other fuel) boiler system — the baseline to replace.

    Parameters
    ----------
    fuel_type : FuelType
        Primary fuel. Determines default CO₂ factor and price.
    annual_fuel_consumption_units : float
        Measured annual consumption in native units (Sm³ for gas, litres for diesel).
        If 0, estimated from building thermal demand.
    boiler_efficiency : float
        Seasonal efficiency (ηs) of the existing boiler [0–1].
        Condensing gas: 0.92–0.98.
        Non-condensing gas: 0.82–0.88.
        Old cast-iron gas: 0.72–0.80.
        Oil/diesel: 0.82–0.88.
    rated_power_kW : float
        Nominal heating power [kW]. Used to determine HP sizing.
        If 0, derived from peak thermal demand.
    terminal_type : TerminalType
        Heat distribution terminal (fan coil, radiators, etc.).
        Determines the HP supply temperature and COP impact.
    dhw_fraction : float
        Fraction of total fuel consumption used for domestic hot water [0–1].
        Remaining (1 - dhw_fraction) is space heating.
        Typical: residential 0.20–0.35, office/commercial 0.05–0.15.
    has_dhw_recirculation : bool
        True if the building has a DHW recirculation loop.
        Affects the baseline thermal loss calculation.
    """
    fuel_type: FuelType = FuelType.NATURAL_GAS
    annual_fuel_consumption_units: float = 0.0
    boiler_efficiency: float = 0.88
    rated_power_kW: float = 0.0
    terminal_type: TerminalType = TerminalType.FAN_COIL
    dhw_fraction: float = 0.10
    has_dhw_recirculation: bool = False

    # Reference efficiency embedded in DEFAULT_PRICES.unit_kwh_conversion
    # (natural gas: 0.90 assumed).  We correct for the actual boiler efficiency.
    _REF_EFFICIENCY: float = 0.90

    @property
    def annual_thermal_output_kWh(self) -> float:
        """Annual useful heat delivered [kWh_th], corrected for actual boiler efficiency."""
        price_obj = DEFAULT_PRICES.get(self.fuel_type)
        if not price_obj or self.annual_fuel_consumption_units <= 0:
            return 0.0
        # LHV = unit_kwh_conversion / reference_efficiency → rescale to actual efficiency
        gross_kwh_per_unit = price_obj.unit_kwh_conversion / self._REF_EFFICIENCY
        return self.annual_fuel_consumption_units * gross_kwh_per_unit * self.boiler_efficiency

    @property
    def annual_heating_kWh(self) -> float:
        """Annual space heating portion [kWh_th]."""
        return self.annual_thermal_output_kWh * (1.0 - self.dhw_fraction)

    @property
    def annual_fuel_cost_eur(self) -> float:
        """Annual fuel cost at current price [€]."""
        price_obj = DEFAULT_PRICES.get(self.fuel_type)
        if not price_obj:
            return 0.0
        return self.annual_fuel_consumption_units * price_obj.price_eur_per_unit

    @property
    def annual_co2_kg(self) -> float:
        """Annual CO₂ emissions from combustion [kg]."""
        price_obj = DEFAULT_PRICES.get(self.fuel_type)
        if not price_obj:
            return 0.0
        return self.annual_fuel_consumption_units * price_obj.co2_factor_kg_per_unit

    def nominal_supply_temp_c(self) -> float:
        """Nominal hot-water supply temperature for this terminal [°C]."""
        lo, hi = TERMINAL_SUPPLY_TEMP_C.get(self.terminal_type, (45.0, 55.0))
        return (lo + hi) / 2.0


# ══════════════════════════════════════════════════════════════════════════════
# HEAT PUMP SYSTEM (replacement / addition)
# ══════════════════════════════════════════════════════════════════════════════

class HeatPumpType(str, Enum):
    """Heat pump source/sink combination."""
    AIR_TO_WATER    = "air_to_water"     # most common for industrial/commercial
    AIR_TO_AIR      = "air_to_air"       # direct air heating — simpler
    GROUND_TO_WATER = "ground_to_water"  # most efficient, high upfront cost
    WATER_TO_WATER  = "water_to_water"   # groundwater or industrial waste heat


@dataclass
class HeatPumpSystem:
    """
    Heat pump system to evaluate as replacement or addition.

    Parameters
    ----------
    hp_type : HeatPumpType
        Source/sink combination. Air-to-water is the most common for renovation.
    rated_heating_power_kW : float
        Nominal heating capacity at A7/W35 standard conditions [kW].
        0 = auto-sized from peak thermal demand.
    terminal_type : TerminalType
        Distribution terminal — determines required supply temperature.
        Must match the existing system or account for terminal replacement.
    carnot_efficiency_factor : float
        η in COP(T_ext) = η × T_hot / (T_hot - T_ext).
        Calibrated per efficiency class:
            A+   class: 0.35–0.40
            A++  class: 0.40–0.46
            A+++ class: 0.46–0.52
        For air-to-water, Italian market 2024: typically 0.38–0.44.
    min_operating_temp_c : float
        Minimum outdoor temperature at which the HP can operate [°C].
        Below this temperature, only the backup system runs.
        Typical air-source: -20°C to -15°C (modern) or -10°C (older).
    defrost_loss_factor : float
        Fractional reduction in COP during defrost cycles [0–1].
        Applied when T_ext < 5°C. Typical: 0.05–0.12 (5–12% COP reduction).
    installed_cost_eur_kW : float
        Total installed cost per kW of heating capacity [€/kW].
        Includes HP unit, installation, hydronic connections, commissioning.
        Typical Italy 2024 air-to-water (fan-coil system, no terminal replacement):
            15–50 kW : 350–500 €/kW
            50–200 kW: 250–400 €/kW
            > 200 kW  : 200–320 €/kW
        Add 80–150 €/kW if terminals need replacement (old radiators → fan coils).
    terminal_replacement_cost_eur : float
        Additional cost for replacing distribution terminals [€].
        0 if terminals are already compatible (fan coil, radiant).
        Provided as absolute value (not per kW) for precision.
    hp_electricity_price : Optional[EnergyPrice]
        Electricity price for the HP. If None, uses the building's main tariff.
        Useful when the HP runs on a dedicated meter with a different tariff.
    cop_degradation_pct_per_yr : float
        Annual COP degradation due to fouling, ageing [%/yr].
        Typical: 0.5–1.5%/yr if not maintained.
        With annual maintenance: effectively 0.
    """
    hp_type: HeatPumpType = HeatPumpType.AIR_TO_WATER
    rated_heating_power_kW: float = 0.0
    terminal_type: TerminalType = TerminalType.FAN_COIL
    carnot_efficiency_factor: float = 0.42
    min_operating_temp_c: float = -15.0
    defrost_loss_factor: float = 0.08
    installed_cost_eur_kW: float = 380.0
    terminal_replacement_cost_eur: float = 0.0
    hp_electricity_price: Optional[EnergyPrice] = None
    cop_degradation_pct_per_yr: float = 0.5

    @property
    def supply_temp_c(self) -> float:
        """Nominal hot-water supply temperature [°C] for COP calculation."""
        lo, hi = TERMINAL_SUPPLY_TEMP_C.get(self.terminal_type, (40.0, 45.0))
        return (lo + hi) / 2.0

    def total_installed_cost_eur(self, rated_kW: float) -> float:
        """Total installed cost for a given rated capacity [€]."""
        kW = rated_kW if rated_kW > 0 else self.rated_heating_power_kW
        return kW * self.installed_cost_eur_kW + self.terminal_replacement_cost_eur


# ══════════════════════════════════════════════════════════════════════════════
# BIVALENT SYSTEM CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BivalentConfig:
    """
    Bivalent system: heat pump + backup boiler.

    A bivalent system is the standard engineering solution for industrial
    renovation because it avoids oversizing the HP for extreme cold.

    Three operating modes:
    - T_ext > T_bivalence:   HP covers 100% of load. Boiler off.
    - T_bivalence >= T_ext > min_operating_temp_c:  HP + boiler run in parallel.
    - T_ext <= min_operating_temp_c:  Boiler covers 100%. HP off.

    Parameters
    ----------
    bivalence_temp_c : float
        Temperature threshold below which backup activates [°C].
        Typical optimum: -2°C to +5°C.
        At +2°C: HP covers ~80% of annual heating energy.
        At +5°C: HP covers ~70% of annual heating energy.
        Lower = more HP coverage but larger HP needed.
    backup_system : GasBoilerSystem
        The backup heating system (usually existing boiler kept as standby).
        Can be the same as the replaced system with reduced usage.
    hp_fraction_at_bivalence : float
        Fraction of load covered by HP in parallel zone [0–1].
        1.0 = HP at full capacity, boiler covers the remainder.
        0.5 = HP and boiler split the load equally.
        Typically 1.0 (HP runs at full capacity, boiler tops up).
    keep_existing_boiler : bool
        True = existing boiler remains as backup (lower upfront cost).
        False = new backup heater installed (electric resistance or new boiler).
    """
    bivalence_temp_c: float = 2.0
    backup_system: GasBoilerSystem = field(default_factory=GasBoilerSystem)
    hp_fraction_at_bivalence: float = 1.0
    keep_existing_boiler: bool = True


# ══════════════════════════════════════════════════════════════════════════════
# CURRENT BUILDING ENERGY PROFILE (aggregate baseline)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BuildingEnergyProfile:
    """
    Complete current energy consumption profile of the building.

    This is the starting point for all scenario comparisons.
    Provide actual measured values from utility bills where available.

    Parameters
    ----------
    electricity_kwh_annual : float
        Annual electricity consumption [kWh/yr] — from electricity bill.
    electricity_monthly_kwh : list[float]
        Monthly electricity breakdown — 12 values [kWh/month].
        If empty, uniform distribution from annual is used.
    electricity_price : EnergyPrice
        Current electricity tariff.
    gas_consumption_annual_units : float
        Annual gas consumption in native units [Sm³/yr for natural gas].
        0 if no gas system.
    gas_monthly_units : list[float]
        Monthly gas consumption — 12 values. If empty, uses degree-day distribution.
    gas_system : GasBoilerSystem
        Existing gas (or other fuel) heating system.
    has_gas : bool
        True if building has a gas system. Shortcut flag.
    co2_electricity_kg_per_kwh : float
        CO₂ factor for grid electricity [kg CO₂/kWh_el].
        Default: Italian grid 2024 (ENTSO-E).
    """
    electricity_kwh_annual: float = 0.0
    electricity_monthly_kwh: list[float] = field(default_factory=list)
    electricity_price: EnergyPrice = field(
        default_factory=lambda: DEFAULT_PRICES[FuelType.ELECTRICITY]
    )
    gas_consumption_annual_units: float = 0.0
    gas_monthly_units: list[float] = field(default_factory=list)
    gas_system: GasBoilerSystem = field(default_factory=GasBoilerSystem)
    has_gas: bool = False
    co2_electricity_kg_per_kwh: float = 0.233

    @property
    def gas_price(self) -> EnergyPrice:
        return DEFAULT_PRICES.get(self.gas_system.fuel_type,
                                   DEFAULT_PRICES[FuelType.NATURAL_GAS])

    @property
    def annual_electricity_cost_eur(self) -> float:
        return self.electricity_kwh_annual * self.electricity_price.price_eur_per_kwh

    @property
    def annual_gas_cost_eur(self) -> float:
        if not self.has_gas or self.gas_consumption_annual_units <= 0:
            return 0.0
        return (self.gas_consumption_annual_units
                * self.gas_price.price_eur_per_unit)

    @property
    def annual_gas_thermal_kWh(self) -> float:
        """Annual useful thermal energy from gas [kWh_th]."""
        if not self.has_gas or self.gas_consumption_annual_units <= 0:
            return 0.0
        return (self.gas_consumption_annual_units
                * self.gas_price.unit_kwh_conversion)

    @property
    def annual_total_cost_eur(self) -> float:
        return self.annual_electricity_cost_eur + self.annual_gas_cost_eur

    @property
    def annual_electricity_co2_kg(self) -> float:
        return self.electricity_kwh_annual * self.co2_electricity_kg_per_kwh

    @property
    def annual_gas_co2_kg(self) -> float:
        if not self.has_gas:
            return 0.0
        return (self.gas_consumption_annual_units
                * self.gas_price.co2_factor_kg_per_unit)

    @property
    def annual_total_co2_kg(self) -> float:
        return self.annual_electricity_co2_kg + self.annual_gas_co2_kg

    def gas_price_breakeven_eur_sm3(
        self,
        hp_annual_electricity_kwh: float,
        hp_electricity_price_eur_kwh: float,
    ) -> float:
        """
        Break-even gas price above which heat pump is cheaper than boiler [€/Sm³].

        Sets cost of HP electricity equal to cost of equivalent gas thermal output:
            hp_kwh × elec_price = gas_units × break_even_price
            → break_even = hp_kwh × elec_price / gas_units

        Parameters
        ----------
        hp_annual_electricity_kwh : float
            Annual electricity consumed by the HP [kWh_el].
        hp_electricity_price_eur_kwh : float
            Effective electricity price for the HP [€/kWh_el].
        """
        if self.gas_consumption_annual_units <= 0:
            return 0.0
        hp_annual_cost = hp_annual_electricity_kwh * hp_electricity_price_eur_kwh
        return hp_annual_cost / self.gas_consumption_annual_units
