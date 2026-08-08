from __future__ import annotations

import math
from dataclasses import dataclass


def saturation_vapor_pressure_hpa(temp_c: float) -> float:
    """Magnus-Formel, Sättigungsdampfdruck in hPa (gültig für -45..60°C)."""
    return 6.112 * math.exp((17.62 * temp_c) / (243.12 + temp_c))


def absolute_humidity_gm3(temp_c: float, rel_humidity_pct: float) -> float:
    """Absolute Feuchte in g/m³ aus Temperatur (°C) und relativer Feuchte (%)."""
    e_hpa = saturation_vapor_pressure_hpa(temp_c) * (rel_humidity_pct / 100.0)
    return 216.7 * (e_hpa / (temp_c + 273.15))


def guete_score(
    temp_c: float,
    abs_humidity_gm3: float,
    ideal_temp: float,
    ideal_abs_humidity_gm3: float,
    weight_temp: float,
    weight_humidity: float,
) -> float:
    """Höher = besser. 0 am Idealpunkt, negativ mit wachsendem Abstand."""
    return -(
        weight_temp * (temp_c - ideal_temp) ** 2
        + weight_humidity * (abs_humidity_gm3 - ideal_abs_humidity_gm3) ** 2
    )


@dataclass
class BlendPoint:
    blend: int
    temp_c: float
    abs_humidity_gm3: float
    guete: float

    def to_dict(self) -> dict:
        return {
            "blend": self.blend,
            "temp_c": round(self.temp_c, 2),
            "abs_humidity_gm3": round(self.abs_humidity_gm3, 3),
            "guete": round(self.guete, 4),
        }


def blend_curve(
    temp_in: float,
    abs_humidity_in: float,
    temp_out: float,
    abs_humidity_out: float,
    ideal_temp: float,
    ideal_abs_humidity_gm3: float,
    weight_temp: float,
    weight_humidity: float,
    step: int = 10,
) -> list[BlendPoint]:
    """Simuliert Mischverhältnisse Innen-/Außenluft von 0% (Ist-Zustand) bis 100%."""
    points: list[BlendPoint] = []
    for blend in range(0, 101, step):
        ratio = blend / 100.0
        temp_sim = temp_in * (1 - ratio) + temp_out * ratio
        abs_humidity_sim = abs_humidity_in * (1 - ratio) + abs_humidity_out * ratio
        guete_sim = guete_score(
            temp_sim, abs_humidity_sim, ideal_temp, ideal_abs_humidity_gm3, weight_temp, weight_humidity
        )
        points.append(BlendPoint(blend, temp_sim, abs_humidity_sim, guete_sim))
    return points
