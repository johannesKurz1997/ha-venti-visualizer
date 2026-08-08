from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger("lueftungsguete.discovery")


@dataclass
class DiscoveredRoom:
    name: str
    temp_entity: str
    humidity_entity: str


def _find_sensor_entity(entity_ids: list[str], states: dict[str, dict], device_class: str) -> str | None:
    """Erster (nach entity_id sortierter) sensor.*-Kandidat mit passender device_class.

    Bei mehreren Treffern wird bewusst nur der erste genommen (siehe README) -
    für einen genauen Wunsch-Sensor kann der Raum per manuellem `rooms`- bzw.
    `no_window_rooms`-Eintrag mit gleichem Namen überschrieben werden.
    """
    matches = []
    for entity_id in sorted(entity_ids):
        if not entity_id.startswith("sensor."):
            continue
        state = states.get(entity_id)
        if state is None:
            continue
        if state.get("attributes", {}).get("device_class") == device_class:
            matches.append(entity_id)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Mehrere %s-Sensoren gefunden, verwende %s (Alternativen: %s)",
            device_class, matches[0], ", ".join(matches[1:]),
        )
    return matches[0]


def discover_rooms(
    areas: list[dict],
    devices: list[dict],
    entities: list[dict],
    states: dict[str, dict],
) -> dict[str, DiscoveredRoom]:
    """Findet pro HA-Bereich ein Temperatur-/Feuchte-Sensorpaar.

    Bereiche ohne beide Sensoren werden stillschweigend übersprungen (kein
    Fehler - der Nutzer hat dort ggf. einfach noch keinen Sensor platziert).
    """
    device_area: dict[str, str | None] = {d["id"]: d.get("area_id") for d in devices}

    entities_by_area: dict[str, list[str]] = defaultdict(list)
    for entity in entities:
        if entity.get("disabled_by") or entity.get("hidden_by"):
            continue
        area_id = entity.get("area_id") or device_area.get(entity.get("device_id"))
        if area_id:
            entities_by_area[area_id].append(entity["entity_id"])

    discovered: dict[str, DiscoveredRoom] = {}
    for area in areas:
        area_id = area.get("area_id")
        name = area.get("name")
        if not area_id or not name:
            continue
        candidates = entities_by_area.get(area_id, [])
        temp_entity = _find_sensor_entity(candidates, states, "temperature")
        humidity_entity = _find_sensor_entity(candidates, states, "humidity")
        if temp_entity and humidity_entity:
            discovered[name] = DiscoveredRoom(name=name, temp_entity=temp_entity, humidity_entity=humidity_entity)
        else:
            logger.debug("Bereich '%s' übersprungen (kein Temp+Feuchte-Sensorpaar gefunden)", name)

    return discovered
