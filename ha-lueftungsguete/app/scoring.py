from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger("lueftungsguete.scoring")


def saturation_vapor_pressure_hpa(temp_c: float) -> float:
    """Magnus-Formel, Sättigungsdampfdruck in hPa (gültig für -45..60°C)."""
    return 6.112 * math.exp((17.62 * temp_c) / (243.12 + temp_c))


def absolute_humidity_gm3(temp_c: float, rel_humidity_pct: float) -> float:
    """Absolute Feuchte in g/m³ aus Temperatur (°C) und relativer Feuchte (%)."""
    e_hpa = saturation_vapor_pressure_hpa(temp_c) * (rel_humidity_pct / 100.0)
    return 216.7 * (e_hpa / (temp_c + 273.15))


def relative_humidity_from_absolute_pct(temp_c: float, abs_humidity_gm3: float) -> float:
    """Kehrfunktion zu absolute_humidity_gm3: relative Feuchte (%) aus Temperatur + absoluter Feuchte."""
    e_hpa = abs_humidity_gm3 * (temp_c + 273.15) / 216.7
    return 100.0 * e_hpa / saturation_vapor_pressure_hpa(temp_c)


def guete_score(
    temp_c: float,
    humidity_rel_pct: float,
    ideal_temp: float,
    ideal_humidity_rel_pct: float,
    weight_temp: float,
    weight_humidity: float,
    sigma_temp: float,
    sigma_humidity_rel: float,
) -> float:
    """0-100, 100 exakt am Idealpunkt, 0 sobald die normierte Abweichung die
    Toleranzbreite (sigma) erreicht oder überschreitet - hart gedeckelt, nie negativ."""
    weight_sum = weight_temp + weight_humidity
    if weight_sum <= 0:
        logger.warning("weight_temp + weight_humidity <= 0, verwende 0.5/0.5 als Fallback-Gewichtung")
        w_temp, w_humidity = 0.5, 0.5
    else:
        w_temp = weight_temp / weight_sum
        w_humidity = weight_humidity / weight_sum

    d_temp = (temp_c - ideal_temp) / sigma_temp
    d_humidity = (humidity_rel_pct - ideal_humidity_rel_pct) / sigma_humidity_rel
    d_squared = w_temp * d_temp**2 + w_humidity * d_humidity**2
    return 100.0 * max(0.0, 1.0 - d_squared)


@dataclass
class BlendPoint:
    blend: int
    temp_c: float
    abs_humidity_gm3: float
    humidity_rel_pct: float
    guete: float

    def to_dict(self) -> dict:
        return {
            "blend": self.blend,
            "temp_c": round(self.temp_c, 2),
            "abs_humidity_gm3": round(self.abs_humidity_gm3, 3),
            "humidity_rel_pct": round(self.humidity_rel_pct, 2),
            "guete": round(self.guete, 2),
        }


def blend_curve(
    temp_in: float,
    abs_humidity_in: float,
    temp_out: float,
    abs_humidity_out: float,
    ideal_temp: float,
    ideal_humidity_rel: float,
    weight_temp: float,
    weight_humidity: float,
    sigma_temp: float,
    sigma_humidity_rel: float,
    step: int = 10,
) -> list[BlendPoint]:
    """Simuliert Mischverhältnisse Innen-/Außenluft von 0% (Ist-Zustand) bis 100%.

    Die Mischung selbst rechnet in absoluter Feuchte (physikalisch korrekt für
    Luftmassen), die Güte-Bewertung jedes Punkts erfolgt aber auf Basis der daraus
    zurückgerechneten relativen Feuchte (siehe relative_humidity_from_absolute_pct) -
    Gewichte/Toleranzen beziehen sich auf relative Feuchte, nicht auf g/m³.
    """
    points: list[BlendPoint] = []
    for blend in range(0, 101, step):
        ratio = blend / 100.0
        temp_sim = temp_in * (1 - ratio) + temp_out * ratio
        abs_humidity_sim = abs_humidity_in * (1 - ratio) + abs_humidity_out * ratio
        humidity_rel_sim = relative_humidity_from_absolute_pct(temp_sim, abs_humidity_sim)
        guete_sim = guete_score(
            temp_sim, humidity_rel_sim, ideal_temp, ideal_humidity_rel,
            weight_temp, weight_humidity, sigma_temp, sigma_humidity_rel,
        )
        points.append(BlendPoint(blend, temp_sim, abs_humidity_sim, humidity_rel_sim, guete_sim))
    return points
