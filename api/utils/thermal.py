"""Residential building thermal correction factor (extracted from app.py).

Captures the heat-loss adjustment between detached/terraced/apartment buildings
and the floor of the apartment, so the API can return the same factor the
Streamlit wizard uses internally before calling `run_simulation()`.
"""

from __future__ import annotations


def thermal_factor(
    building_subtype: str,
    floor_number: int = 0,
    is_top_floor: bool = False,
) -> tuple[float, str]:
    """Return (factor, description) suitable for scaling annual gas demand."""
    sub = (building_subtype or "detached").lower()

    if sub == "detached":
        return 1.0, "Detached/standalone — full envelope exposed."

    if sub == "terraced":
        if is_top_floor:
            return 0.85, "Terraced, top floor — roof loss but shared walls."
        if floor_number == 0:
            return 0.82, "Terraced, ground floor — slab loss but shared walls."
        return 0.78, "Terraced, intermediate floor — only end walls exposed."

    # apartment
    if is_top_floor:
        return 0.70, "Apartment, top floor — roof loss but shared walls/floor."
    if floor_number == 0:
        return 0.65, "Apartment, ground floor — slab loss but shared walls/ceiling."
    return 0.55, "Apartment, intermediate floor — only end walls exposed."
