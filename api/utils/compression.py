"""Compression helpers for hourly arrays returned over JSON.

8760 floats with full precision = ~150 kB per array. Rounding to integer Wh
or 1-decimal °C cuts the payload by 3–4× without affecting downstream charts.
"""

from __future__ import annotations

from typing import Iterable


def compress_hourly_Wh(hourly: Iterable[float]) -> list[int]:
    return [int(round(float(v))) for v in hourly]


def compress_hourly_temp(hourly: Iterable[float]) -> list[float]:
    return [round(float(v), 1) for v in hourly]


def compress_monthly(monthly: Iterable[float], decimals: int = 1) -> list[float]:
    return [round(float(v), decimals) for v in monthly]
