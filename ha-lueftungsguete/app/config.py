from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

OPTIONS_PATH = Path(os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json"))

_UMLAUT_MAP = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})


def slugify(name: str) -> str:
    """Reproduziert HA's Umlaut-Konvertierung (ü -> u, nicht ue) für Entity-IDs."""
    lowered = name.strip().lower().translate(_UMLAUT_MAP)
    return "".join(c if c.isalnum() else "_" for c in lowered).strip("_")


class RoomConfig(BaseModel):
    name: str
    temp_entity: str
    humidity_entity: str
    ideal_temp: float
    ideal_humidity_rel: float
    weight_temp: float = 1.0
    weight_humidity: float = 1.0
    binary_sensor_suffix: str | None = None

    @property
    def slug(self) -> str:
        return self.binary_sensor_suffix or slugify(self.name)

    @property
    def binary_sensor_entity_id(self) -> str:
        return f"binary_sensor.luften_{self.slug}"


class BadConfig(BaseModel):
    temp_entity: str
    humidity_entity: str
    mold_risk_threshold: float = 12.0
    mold_risk_hysteresis: float = 1.5


class NotifyTarget(BaseModel):
    service: str
    person_entity: str


class AddonOptions(BaseModel):
    rooms: list[RoomConfig] = Field(default_factory=list)
    bad: BadConfig
    outdoor_weather_entity: str
    blend_steps: int = 10
    delta_guete_threshold: float = 0.5
    stability_minutes: int = 10
    poll_interval_seconds: int = 60
    notify_targets: list[NotifyTarget] = Field(default_factory=list)

    @field_validator("blend_steps")
    @classmethod
    def _blend_steps_divides_100(cls, v: int) -> int:
        if v <= 0 or 100 % v != 0:
            raise ValueError("blend_steps muss 100 ohne Rest teilen (z.B. 5, 10, 20, 25)")
        return v


def load_options(path: Path = OPTIONS_PATH) -> AddonOptions:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return AddonOptions.model_validate(raw)
