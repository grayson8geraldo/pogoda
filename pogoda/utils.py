"""Utility functions: temperature conversions, rounding."""

from __future__ import annotations


def c_to_f(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (fahrenheit - 32.0) * 5.0 / 9.0


def convert_temp(temp_c: float, target_unit: str) -> float:
    """Convert temperature from Celsius to the target unit."""
    if target_unit.upper() == "F":
        return c_to_f(temp_c)
    return temp_c


def round_temp(temp: float) -> int:
    """Round temperature to nearest integer (matching market bin granularity).

    Weather Underground reports whole degrees, so we match that.
    """
    return round(temp)
