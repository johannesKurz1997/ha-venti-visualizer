from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import AddonOptions, NoWindowRoomConfig, RoomConfig, load_options
from discovery import DiscoveredRoom, discover_rooms
from ha_client import HAClient
from persistence import Persistence
from scoring import absolute_humidity_gm3, blend_curve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lueftungsguete.main")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_float(state: dict | None) -> float | None:
    if state is None:
        return None
    try:
        return float(state["state"])
    except (KeyError, TypeError, ValueError):
        return None


def _luften_attributes(room_name: str, delta_guete: float, stable_since: str | None) -> dict[str, Any]:
    """Attribute für binary_sensor.luften_<raum> - bewusst OHNE device_class.

    Das ist eine Lüftungsempfehlung, kein Fensterkontakt und kein Feuchtemelder;
    jede device_class (z.B. "opening"/"window" -> "Geöffnet"/"Geschlossen",
    "moisture" -> "Trocken"/"Feucht") würde HA zu einem irreführenden
    Zustandstext verleiten statt schlicht "An"/"Aus" anzuzeigen. Einheitlich für
    alle Räume - keine raumabhängige Sonderbehandlung.
    """
    return {
        "friendly_name": f"Lüften {room_name}",
        "delta_guete": round(delta_guete, 2),
        "stable_since": stable_since,
    }


def _outdoor_from_weather(state: dict | None) -> tuple[float | None, float | None]:
    if state is None:
        return None, None
    attrs = state.get("attributes", {})
    return attrs.get("temperature"), attrs.get("humidity")


class Controller:
    def __init__(self, options: AddonOptions) -> None:
        self.options = options
        self.ha = HAClient()
        self.persistence = Persistence()
        self.lock = asyncio.Lock()
        self.latest_rooms: dict[str, dict[str, Any]] = {}
        self.latest_no_window_rooms: dict[str, dict[str, Any]] = {}
        self.latest_outdoor: dict[str, Any] | None = None
        self._room_configs: dict[str, RoomConfig] = {}
        self._discovered: dict[str, DiscoveredRoom] = {}
        self._last_discovery: datetime | None = None

    async def aclose(self) -> None:
        await self.ha.aclose()

    async def poll_once(self) -> None:
        states = await self.ha.get_all_states()

        if self.options.auto_discover_rooms:
            await self._maybe_refresh_discovery(states)

        effective_rooms, effective_no_window, outdoor_entities = self._build_effective_rooms()

        temp_out, hum_out, outdoor_source = self._resolve_outdoor(states, outdoor_entities)
        if temp_out is None or hum_out is None:
            logger.warning("Außenwerte nicht verfügbar - überspringe Zyklus")
            return
        abs_out = absolute_humidity_gm3(temp_out, hum_out)

        present_persons: set[str] = {
            target.person_entity
            for target in self.options.notify_targets
            if (p := states.get(target.person_entity)) is not None and p.get("state") == "home"
        }

        rooms_result: dict[str, Any] = {}
        room_configs: dict[str, RoomConfig] = {}
        for room in effective_rooms:
            room_configs[room.slug] = room
            result = await self._process_room(room, states, temp_out, hum_out, abs_out, present_persons)
            if result is not None:
                rooms_result[room.slug] = result

        no_window_result: dict[str, Any] = {}
        for room in effective_no_window:
            result = await self._process_no_window_room(room, states)
            if result is not None:
                no_window_result[room.slug] = result

        async with self.lock:
            self.latest_rooms = rooms_result
            self.latest_no_window_rooms = no_window_result
            self._room_configs = room_configs
            self.latest_outdoor = {
                "name": self.options.outdoor_area_name,
                "temp": round(temp_out, 2),
                "humidity_rel": round(hum_out, 2),
                "abs_humidity": round(abs_out, 3),
                "source": outdoor_source,
                "updated_at": _now_iso(),
            }

    async def _maybe_refresh_discovery(self, states: dict[str, dict]) -> None:
        now = datetime.now(timezone.utc)
        interval = timedelta(minutes=self.options.area_discovery_interval_minutes)
        if self._last_discovery is not None and now - self._last_discovery < interval:
            return
        try:
            areas, devices, entities = await self.ha.get_registries()
        except Exception:
            logger.exception("Area-Discovery fehlgeschlagen, verwende zuletzt bekannten Stand")
            return
        self._discovered = discover_rooms(areas, devices, entities, states)
        self._last_discovery = now
        logger.info(
            "Area-Discovery: %d Bereich(e) mit Temperatur+Feuchte-Sensorpaar gefunden (%s)",
            len(self._discovered), ", ".join(sorted(self._discovered)) or "keine",
        )

    def _build_effective_rooms(
        self,
    ) -> tuple[list[RoomConfig], list[NoWindowRoomConfig], tuple[str, str] | None]:
        """Baut die effektive Raumliste; der `outdoor_area_name`-Raum (Default "Aussen")
        wird dabei nie als eigener Lüftungs-/Schimmelrisiko-Raum geführt, sondern als
        Außenreferenz (temp_entity, humidity_entity) zurückgegeben (siehe _resolve_outdoor).
        """
        outdoor_name = self.options.outdoor_area_name.strip().lower()
        no_window_area_names = {a.strip().lower() for a in self.options.no_window_areas}
        manual_room_names = {r.name.strip().lower() for r in self.options.rooms}
        manual_no_window_names = {r.name.strip().lower() for r in self.options.no_window_rooms}

        rooms = [r for r in self.options.rooms if r.name.strip().lower() != outdoor_name]
        no_window_rooms = [r for r in self.options.no_window_rooms if r.name.strip().lower() != outdoor_name]

        outdoor_entities: tuple[str, str] | None = None
        for r in self.options.rooms:
            if r.name.strip().lower() == outdoor_name:
                outdoor_entities = (r.temp_entity, r.humidity_entity)

        if self.options.auto_discover_rooms:
            for name, discovered in self._discovered.items():
                key = name.strip().lower()
                if key == outdoor_name:
                    if outdoor_entities is None:  # manuelle Angabe hat Vorrang
                        outdoor_entities = (discovered.temp_entity, discovered.humidity_entity)
                    continue
                if key in manual_room_names or key in manual_no_window_names:
                    continue  # manuelle Angabe hat immer Vorrang vor Auto-Discovery
                if key in no_window_area_names:
                    no_window_rooms.append(
                        NoWindowRoomConfig(
                            name=discovered.name,
                            temp_entity=discovered.temp_entity,
                            humidity_entity=discovered.humidity_entity,
                        )
                    )
                else:
                    rooms.append(
                        RoomConfig(
                            name=discovered.name,
                            temp_entity=discovered.temp_entity,
                            humidity_entity=discovered.humidity_entity,
                            ideal_temp=self.options.default_ideal_temp,
                            ideal_humidity_rel=self.options.default_ideal_humidity_rel,
                            weight_temp=self.options.default_weight_temp,
                            weight_humidity=self.options.default_weight_humidity,
                        )
                    )

        rooms = [self._apply_ideal_override(r) for r in rooms]
        return rooms, no_window_rooms, outdoor_entities

    def _apply_ideal_override(self, room: RoomConfig) -> RoomConfig:
        """Wendet zur Laufzeit per API/UI gesetzte Idealwerte an (siehe /api/rooms/{room}/ideal-override).
        Gilt für auto-erkannte wie manuell konfigurierte Räume gleichermaßen und hat
        Vorrang vor beidem, da es ein expliziter, aktueller Nutzereingriff ist."""
        override = self.persistence.get_ideal_override(room.slug)
        if not override:
            return room
        return room.model_copy(update=override)

    def _resolve_outdoor(
        self, states: dict[str, dict], outdoor_entities: tuple[str, str] | None
    ) -> tuple[float | None, float | None, str]:
        """Außenwerte bevorzugt vom `outdoor_area_name`-Raum (echte Sensoren), sonst
        Fallback auf `outdoor_weather_entity` (Wetter-Vorhersage-Attribute)."""
        if outdoor_entities is not None:
            temp_entity, humidity_entity = outdoor_entities
            temp = _parse_float(states.get(temp_entity))
            hum = _parse_float(states.get(humidity_entity))
            if temp is not None and hum is not None:
                return temp, hum, "area"
            logger.warning(
                "Außenreferenz-Raum '%s' liefert keine Sensordaten, "
                "verwende outdoor_weather_entity als Fallback",
                self.options.outdoor_area_name,
            )

        if self.options.outdoor_weather_entity:
            temp, hum = _outdoor_from_weather(states.get(self.options.outdoor_weather_entity))
            if temp is not None and hum is not None:
                return temp, hum, "weather"

        return None, None, "unavailable"

    async def _process_room(
        self,
        room: RoomConfig,
        states: dict[str, dict],
        temp_out: float,
        hum_out: float,
        abs_out: float,
        present_persons: set[str],
    ) -> dict[str, Any] | None:
        temp_in = _parse_float(states.get(room.temp_entity))
        hum_in = _parse_float(states.get(room.humidity_entity))
        if temp_in is None or hum_in is None:
            logger.warning("Raum %s: Sensordaten nicht verfügbar, überspringe", room.name)
            return None

        abs_in = absolute_humidity_gm3(temp_in, hum_in)
        ideal_abs = absolute_humidity_gm3(room.ideal_temp, room.ideal_humidity_rel)
        sigma_temp = room.sigma_temp if room.sigma_temp is not None else self.options.default_sigma_temp
        sigma_humidity_rel = (
            room.sigma_humidity_rel
            if room.sigma_humidity_rel is not None
            else self.options.default_sigma_humidity_rel
        )

        curve = blend_curve(
            temp_in, abs_in, temp_out, abs_out,
            room.ideal_temp, room.ideal_humidity_rel, room.weight_temp, room.weight_humidity,
            sigma_temp, sigma_humidity_rel,
            step=self.options.blend_steps,
        )
        guete_0 = curve[0].guete
        guete_100 = curve[-1].guete
        delta = guete_100 - guete_0

        recommend, rising_edge, stable_since = self._update_stability(room.slug, delta)

        await self.ha.set_state(
            room.binary_sensor_entity_id,
            "on" if recommend else "off",
            _luften_attributes(room.name, delta, stable_since),
        )

        if rising_edge and self._should_notify(room.slug):
            await self._notify(
                present_persons,
                f"Lüften empfohlen: {room.name} (ΔGüte {delta:.2f}, seit "
                f"{self.options.stability_minutes} Min. stabil)",
            )

        self.persistence.append_history(
            {
                "ts": _now_iso(),
                "room": room.name,
                "guete_0": round(guete_0, 2),
                "guete_100": round(guete_100, 2),
                "delta_guete": round(delta, 2),
                "luften_empfohlen": recommend,
            }
        )

        return {
            "name": room.name,
            "slug": room.slug,
            "temp_entity": room.temp_entity,
            "humidity_entity": room.humidity_entity,
            "binary_sensor": room.binary_sensor_entity_id,
            "temp_in": round(temp_in, 2),
            "humidity_rel_in": round(hum_in, 2),
            "abs_humidity_in": round(abs_in, 3),
            "ideal_temp": room.ideal_temp,
            "ideal_humidity_rel": room.ideal_humidity_rel,
            "ideal_override": bool(self.persistence.get_ideal_override(room.slug)),
            "ideal_abs_humidity": round(ideal_abs, 3),
            "sigma_temp": sigma_temp,
            "sigma_humidity_rel": sigma_humidity_rel,
            "outdoor_temp": round(temp_out, 2),
            "outdoor_humidity_rel": round(hum_out, 2),
            "outdoor_abs_humidity": round(abs_out, 3),
            "guete_current": round(guete_0, 2),
            "guete_full_outside": round(guete_100, 2),
            "delta_guete": round(delta, 2),
            "delta_guete_threshold": self.options.delta_guete_threshold,
            "luften_empfohlen": recommend,
            "stable_since": stable_since,
            "updated_at": _now_iso(),
        }

    def _update_stability(self, room_slug: str, delta: float) -> tuple[bool, bool, str | None]:
        state = self.persistence.get_room_state(room_slug)
        positive_since = state.get("positive_since")
        was_on = bool(state.get("luften_on", False))

        now = datetime.now(timezone.utc)
        if delta > self.options.delta_guete_threshold:
            if positive_since is None:
                positive_since = now.isoformat()
            stable_seconds = (now - datetime.fromisoformat(positive_since)).total_seconds()
            recommend = stable_seconds >= self.options.stability_minutes * 60
        else:
            positive_since = None
            recommend = False

        # In-place aktualisieren statt mit einem frischen Dict zu überschreiben -
        # sonst ginge z.B. last_notified_at aus _should_notify bei jedem Poll verloren.
        state["positive_since"] = positive_since
        state["luften_on"] = recommend
        self.persistence.set_room_state(room_slug, state)
        rising_edge = recommend and not was_on
        return recommend, rising_edge, positive_since

    def _should_notify(self, room_slug: str) -> bool:
        """Begrenzt Push-Notifications pro Raum auf höchstens eine pro
        `notify_cooldown_minutes`, auch wenn die Lüftungsempfehlung (z.B. durch
        Sensorrauschen um die Schwelle) mehrfach kurz hintereinander an-/ausgeht."""
        state = self.persistence.get_room_state(room_slug)
        last_notified = state.get("last_notified_at")
        now = datetime.now(timezone.utc)
        if last_notified is not None:
            elapsed = (now - datetime.fromisoformat(last_notified)).total_seconds()
            if elapsed < self.options.notify_cooldown_minutes * 60:
                return False
        state["last_notified_at"] = now.isoformat()
        self.persistence.set_room_state(room_slug, state)
        return True

    async def _process_no_window_room(self, room: NoWindowRoomConfig, states: dict[str, dict]) -> dict[str, Any] | None:
        temp = _parse_float(states.get(room.temp_entity))
        hum = _parse_float(states.get(room.humidity_entity))
        if temp is None or hum is None:
            logger.warning("%s: Sensordaten nicht verfügbar, überspringe", room.name)
            return None

        abs_humidity = absolute_humidity_gm3(temp, hum)
        threshold = room.mold_risk_threshold if room.mold_risk_threshold is not None else self.options.mold_risk_threshold
        hysteresis = (
            room.mold_risk_hysteresis if room.mold_risk_hysteresis is not None else self.options.mold_risk_hysteresis
        )

        state = self.persistence.get_no_window_state(room.slug)
        risk_on = bool(state.get("risk_on", False))

        if not risk_on and abs_humidity >= threshold:
            risk_on = True
        elif risk_on and abs_humidity <= threshold - hysteresis:
            risk_on = False

        self.persistence.set_no_window_state(room.slug, {"risk_on": risk_on})

        await self.ha.set_state(
            room.binary_sensor_entity_id,
            "on" if risk_on else "off",
            {
                "friendly_name": f"Schimmelrisiko {room.name}",
                "device_class": "problem",
                "abs_humidity": round(abs_humidity, 3),
                "threshold": threshold,
            },
        )

        return {
            "name": room.name,
            "slug": room.slug,
            "temp_entity": room.temp_entity,
            "humidity_entity": room.humidity_entity,
            "temp": round(temp, 2),
            "humidity_rel": round(hum, 2),
            "abs_humidity": round(abs_humidity, 3),
            "mold_risk_threshold": threshold,
            "mold_risk_hysteresis": hysteresis,
            "schimmelrisiko": risk_on,
            "binary_sensor": room.binary_sensor_entity_id,
            "updated_at": _now_iso(),
        }

    async def _notify(self, present_persons: set[str], message: str) -> None:
        for target in self.options.notify_targets:
            if target.person_entity not in present_persons:
                continue
            service_name = target.service.split(".", 1)[-1]
            try:
                await self.ha.call_service("notify", service_name, {"message": message})
            except Exception:
                logger.exception("Notify an %s fehlgeschlagen", target.service)

    def find_room(self, room_key: str) -> RoomConfig | None:
        key = room_key.strip().lower()
        for room in self._room_configs.values():
            if room.slug == key or room.name.lower() == key:
                return room
        return None


async def _poll_loop(controller: Controller) -> None:
    while True:
        try:
            await controller.poll_once()
        except Exception:
            logger.exception("Fehler im Poll-Zyklus")
        await asyncio.sleep(controller.options.poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    options = load_options()
    controller = Controller(options)
    app.state.controller = controller
    task = asyncio.create_task(_poll_loop(controller))
    try:
        await controller.poll_once()
    except Exception:
        logger.exception("Initialer Poll-Zyklus fehlgeschlagen")
    yield
    task.cancel()
    await controller.aclose()


app = FastAPI(title="Lüftungsgüte", lifespan=lifespan)


def _controller(app_: FastAPI) -> Controller:
    return app_.state.controller


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
async def api_config() -> JSONResponse:
    controller = _controller(app)
    return JSONResponse({"poll_interval_seconds": controller.options.poll_interval_seconds})


@app.get("/api/rooms")
async def api_rooms() -> JSONResponse:
    controller = _controller(app)
    async with controller.lock:
        return JSONResponse(list(controller.latest_rooms.values()))


@app.get("/api/no-window-rooms")
async def api_no_window_rooms() -> JSONResponse:
    controller = _controller(app)
    async with controller.lock:
        return JSONResponse(list(controller.latest_no_window_rooms.values()))


@app.get("/api/outdoor")
async def api_outdoor() -> JSONResponse:
    controller = _controller(app)
    async with controller.lock:
        if controller.latest_outdoor is None:
            raise HTTPException(status_code=503, detail="Noch keine Außenwerte verfügbar")
        return JSONResponse(controller.latest_outdoor)


@app.get("/api/rooms/{room_key}/blend-curve")
async def api_blend_curve(room_key: str) -> JSONResponse:
    controller = _controller(app)
    room = controller.find_room(room_key)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Raum: {room_key}")

    async with controller.lock:
        cached = controller.latest_rooms.get(room.slug)
    if cached is None:
        raise HTTPException(status_code=503, detail="Noch keine Daten für diesen Raum verfügbar")

    ideal_abs = absolute_humidity_gm3(room.ideal_temp, room.ideal_humidity_rel)
    curve = blend_curve(
        cached["temp_in"], cached["abs_humidity_in"],
        cached["outdoor_temp"], cached["outdoor_abs_humidity"],
        room.ideal_temp, room.ideal_humidity_rel, room.weight_temp, room.weight_humidity,
        cached["sigma_temp"], cached["sigma_humidity_rel"],
        step=controller.options.blend_steps,
    )
    return JSONResponse(
        {
            "room": room.name,
            "ideal_temp": room.ideal_temp,
            "ideal_humidity_rel": room.ideal_humidity_rel,
            "ideal_abs_humidity": round(ideal_abs, 3),
            "sigma_temp": cached["sigma_temp"],
            "sigma_humidity_rel": cached["sigma_humidity_rel"],
            "points": [p.to_dict() for p in curve],
        }
    )


class IdealOverrideRequest(BaseModel):
    ideal_temp: float | None = Field(default=None, ge=-30, le=60)
    ideal_humidity_rel: float | None = Field(default=None, ge=0, le=100)


@app.get("/api/rooms/{room_key}/ideal-override")
async def api_get_ideal_override(room_key: str) -> JSONResponse:
    controller = _controller(app)
    room = controller.find_room(room_key)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Raum: {room_key}")
    override = controller.persistence.get_ideal_override(room.slug)
    return JSONResponse(
        {
            "ideal_temp": room.ideal_temp,
            "ideal_humidity_rel": room.ideal_humidity_rel,
            "has_override": bool(override),
        }
    )


@app.put("/api/rooms/{room_key}/ideal-override")
async def api_set_ideal_override(room_key: str, body: IdealOverrideRequest) -> JSONResponse:
    controller = _controller(app)
    room = controller.find_room(room_key)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Raum: {room_key}")
    if body.ideal_temp is None and body.ideal_humidity_rel is None:
        raise HTTPException(status_code=400, detail="Mindestens ideal_temp oder ideal_humidity_rel angeben")

    controller.persistence.set_ideal_override(
        room.slug, {"ideal_temp": body.ideal_temp, "ideal_humidity_rel": body.ideal_humidity_rel}
    )
    await controller.poll_once()  # sofort wirksam statt erst beim nächsten regulären Poll
    return JSONResponse({"status": "ok"})


@app.delete("/api/rooms/{room_key}/ideal-override")
async def api_clear_ideal_override(room_key: str) -> JSONResponse:
    controller = _controller(app)
    room = controller.find_room(room_key)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Raum: {room_key}")
    controller.persistence.clear_ideal_override(room.slug)
    await controller.poll_once()
    return JSONResponse({"status": "reset"})


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Lüftungsgüte</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  html, body { overflow-x: hidden; }
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 1.3rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; }
  th { color: #999; font-weight: 600; font-size: 0.85rem; }
  .badge { padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }
  .on { background: #1f6f3a; color: #d7ffe0; }
  .off { background: #333; color: #aaa; }
  .g-good { background: #1f6f3a; color: #d7ffe0; }
  .g-mid { background: #6b5b1f; color: #ffe9b3; }
  .g-bad { background: #6f1f1f; color: #ffd7d7; }
  .muted { color: #888; font-size: 0.85rem; }
  .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  .tab-btn {
    background: #1b1e24; color: #ccc; border: 1px solid #333; padding: 0.4rem 1rem;
    border-radius: 6px; cursor: pointer; font-size: 0.9rem; min-height: 44px; flex: 1;
  }
  .tab-btn.active { background: #2b3f6b; color: #fff; border-color: #3b5aa0; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .hint-short { color: #888; font-size: 0.85rem; line-height: 1.35; margin: 0 0 0.4rem; }
  .hint { font-size: 0.85rem; line-height: 1.35; color: #ccc; margin: 0 0 0.6rem; }
  .hint summary { cursor: pointer; color: #9db4ff; min-height: 32px; display: flex; align-items: center; }
  .hint p { margin: 0.5rem 0 0; color: #aaa; }
  .plot-toolbar { display: flex; gap: 0.5rem; margin-bottom: 0.6rem; flex-wrap: wrap; }
  .tool-btn {
    background: #1b1e24; color: #ccc; border: 1px solid #333; border-radius: 6px;
    padding: 0.5rem 0.9rem; min-height: 40px; font-size: 0.85rem; cursor: pointer;
  }
  .tool-btn:hover { color: #fff; }
  .tool-btn.mobile-only { display: none; }
  #plot3d-wrap { width: 100%; }
  #plot3d { width: 100%; height: clamp(320px, 55vh, 520px); }
  #plot3d.touch-guard { pointer-events: none; touch-action: pan-y; }
  #plot3d.touch-guard.rotate-active { pointer-events: auto; touch-action: none; }
  .icon-btn {
    background: none; border: none; color: #9db4ff; cursor: pointer;
    font-size: 0.9rem; margin-left: 0.35rem; padding: 0 0.15rem;
  }
  .icon-btn:hover { color: #fff; }
  @media (max-width: 700px) {
    body { margin: 0.75rem; }
    .tool-btn.mobile-only { display: inline-block; }
  }
</style>
</head>
<body>
<h1>Lüftungsgüte</h1>
<p class="muted">Aktualisiert sich alle paar Sekunden. Voller Datenzugriff über <code>/api/rooms</code>, <code>/api/no-window-rooms</code>, <code>/api/outdoor</code> und <code>/api/rooms/{room}/blend-curve</code>.</p>

<div class="tabs">
  <button class="tab-btn active" data-tab="table">Tabelle</button>
  <button class="tab-btn" data-tab="plot3d">3D-Ansicht</button>
</div>

<div id="tab-table" class="tab-panel active">
<p id="outdoor-status" class="muted">Außen: –</p>
<h2>Räume (mit Fenster)</h2>
<table id="rooms"><thead><tr>
  <th>Raum</th><th>T innen</th><th>F rel innen</th><th>Idealwerte</th><th>Güte (0-100)</th><th>ΔGüte</th><th>Lüften?</th>
</tr></thead><tbody></tbody></table>
<h2>Räume ohne Fenster (Schimmelrisiko)</h2>
<table id="no-window"><thead><tr>
  <th>Raum</th><th>T</th><th>F rel</th><th>F abs</th><th>Schimmelrisiko</th>
</tr></thead><tbody></tbody></table>
</div>

<div id="tab-plot3d" class="tab-panel">
<p class="hint-short">Punkt = jetzt, Pfeil = nach voller Lüftung, Diamant = Idealpunkt.</p>
<details class="hint" id="plot-hint">
  <summary>Legende &amp; Erklärung</summary>
  <p>
    X = Temperatur, Y = relative Feuchte, Z = Güte (0-100) - Achsen passen sich
    automatisch eng an die aktuellen Werte an. Linie je Raum: großer Punkt =
    aktueller Zustand, Pfeilspitze = nach vollständiger Lüftung mit Außenluft;
    Farbverlauf zeigt Verbesserung (grün) oder Verschlechterung (rot) ggü.
    jetzt. Diamant = Idealpunkt des Raums. Die Punktfarbe in der Legende ist
    eine feste, raumspezifische Kennfarbe - unabhängig vom Grün/Rot der Kurve.
    Bad und die Außenreferenz erscheinen hier nicht (kein Güte-Wert für sie),
    siehe Tabellen-Tab.
  </p>
</details>
<div class="plot-toolbar">
  <button id="plot-rotate-toggle" class="tool-btn mobile-only" type="button">Drehen aktivieren</button>
  <button id="plot-reset-view" class="tool-btn" type="button">Ansicht zurücksetzen</button>
</div>
<div id="plot3d-wrap">
  <div id="plot3d"></div>
</div>
</div>

<script>
const isMobile = window.matchMedia('(max-width: 700px)').matches;
if (!isMobile) {
  const hint = document.getElementById('plot-hint');
  if (hint) hint.open = true;
}
function guteClass(v) {
  if (v > 70) return 'g-good';
  if (v >= 40) return 'g-mid';
  return 'g-bad';
}

async function refresh() {
  try {
    const outdoor = await (await fetch('api/outdoor')).json();
    const outdoorEl = document.getElementById('outdoor-status');
    const sourceLabel = outdoor.source === 'weather' ? ' (Wetter-Fallback)' : '';
    outdoorEl.textContent = `Außen (${outdoor.name}): ${outdoor.temp} °C, ${outdoor.humidity_rel} % rel.${sourceLabel}`;
  } catch (e) {
    console.error(e);
  }

  try {
    const rooms = await (await fetch('api/rooms')).json();
    const roomsBody = document.querySelector('#rooms tbody');
    roomsBody.innerHTML = rooms.map(r => `<tr>
      <td>${r.name}</td>
      <td>${r.temp_in} °C</td>
      <td>${r.humidity_rel_in} %</td>
      <td>
        ${r.ideal_temp} °C / ${r.ideal_humidity_rel} %
        <button class="icon-btn" title="Idealwerte bearbeiten" onclick="editIdealValues('${r.slug}', ${r.ideal_temp}, ${r.ideal_humidity_rel})">&#9998;</button>
        ${r.ideal_override ? `<button class="icon-btn" title="Auf Standard zurücksetzen" onclick="resetIdealValues('${r.slug}')">&#8634;</button>` : ''}
      </td>
      <td><span class="badge ${guteClass(r.guete_current)}">${r.guete_current.toFixed(1)}</span></td>
      <td>${r.delta_guete.toFixed(1)}</td>
      <td><span class="badge ${r.luften_empfohlen ? 'on' : 'off'}">${r.luften_empfohlen ? 'ja' : 'nein'}</span></td>
    </tr>`).join('');

    const noWindow = await (await fetch('api/no-window-rooms')).json();
    const noWindowBody = document.querySelector('#no-window tbody');
    noWindowBody.innerHTML = noWindow.map(r => `<tr>
      <td>${r.name}</td>
      <td>${r.temp} °C</td>
      <td>${r.humidity_rel} %</td>
      <td>${r.abs_humidity} g/m³</td>
      <td><span class="badge ${r.schimmelrisiko ? 'on' : 'off'}">${r.schimmelrisiko ? 'Risiko' : 'ok'}</span></td>
    </tr>`).join('');
  } catch (e) {
    console.error(e);
  }
}
refresh();
setInterval(refresh, 15000);

async function editIdealValues(slug, currentTemp, currentHumidity) {
  const tempInput = prompt('Ideal-Temperatur (°C):', currentTemp);
  if (tempInput === null) return;
  const humInput = prompt('Ideale relative Feuchte (%):', currentHumidity);
  if (humInput === null) return;

  const ideal_temp = parseFloat(tempInput.replace(',', '.'));
  const ideal_humidity_rel = parseFloat(humInput.replace(',', '.'));
  if (Number.isNaN(ideal_temp) || Number.isNaN(ideal_humidity_rel)) {
    alert('Ungültige Zahl.');
    return;
  }

  try {
    const res = await fetch(`api/rooms/${slug}/ideal-override`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ideal_temp, ideal_humidity_rel }),
    });
    if (!res.ok) throw new Error(await res.text());
    refresh();
  } catch (e) {
    console.error(e);
    alert('Speichern fehlgeschlagen.');
  }
}

async function resetIdealValues(slug) {
  if (!confirm('Idealwerte für diesen Raum auf den konfigurierten Standard zurücksetzen?')) return;
  try {
    const res = await fetch(`api/rooms/${slug}/ideal-override`, { method: 'DELETE' });
    if (!res.ok) throw new Error(await res.text());
    refresh();
  } catch (e) {
    console.error(e);
    alert('Zurücksetzen fehlgeschlagen.');
  }
}

// --- 3D-Ansicht (Plotly) ---

// Cone-Größe = CONE_SIZE_FACTOR * größere Achsen-Spannweite (X/Y), auf Mobil
// zusätzlich reduziert - leicht nachjustierbar, ohne die Formel suchen zu müssen.
const CONE_SIZE_FACTOR = 0.06;
const CONE_SIZE_FACTOR_MOBILE = CONE_SIZE_FACTOR * 0.7;

const DEFAULT_CAMERA = {
  eye: isMobile ? { x: 1.6, y: 1.6, z: 0.9 } : { x: 1.25, y: 1.25, z: 1.1 },
};

const PLOTLY_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: isMobile
    ? ['toImage', 'sendDataToCloud', 'hoverClosest3d', 'orbitRotation', 'resetCameraLastSave3d']
    : ['sendDataToCloud'],
  displayModeBar: isMobile ? 'hover' : true,
};

function lerpColor(a, b, t) {
  const ah = parseInt(a.slice(1), 16), bh = parseInt(b.slice(1), 16);
  const ar = (ah >> 16) & 0xff, ag = (ah >> 8) & 0xff, ab = ah & 0xff;
  const br = (bh >> 16) & 0xff, bg = (bh >> 8) & 0xff, bb = bh & 0xff;
  const rr = Math.round(ar + (br - ar) * t);
  const rg = Math.round(ag + (bg - ag) * t);
  const rb = Math.round(ab + (bb - ab) * t);
  return `rgb(${rr},${rg},${rb})`;
}

function deltaColor(delta) {
  // delta = Güte an diesem Punkt minus Güte am 0%-Punkt (aktueller Zustand).
  // Grau = keine Änderung, Grün = Verbesserung, Rot = Verschlechterung.
  // Volle Sättigung schon ab ±25 Güte-Punkten (statt ±100) für kräftigere,
  // klar erkennbare Farben statt eines blassen Verlaufs.
  const t = Math.max(-1, Math.min(1, delta / 25));
  if (t >= 0) return lerpColor('#4b5563', '#16a34a', t);
  return lerpColor('#4b5563', '#dc2626', -t);
}

function baseColorForRoom(slug) {
  // Hue-Bereich bewusst auf Cyan/Blau/Violett/Magenta beschränkt (190-330) und
  // damit weg von Grün/Rot (0-20, 90-150, 340-360) - die Legendenfarbe ist eine
  // feste Raum-Kennfarbe, keine Güte-Bewertung, und darf daher nicht zufällig
  // mit der Grün=besser/Rot=schlechter-Semantik der Kurven kollidieren.
  let hash = 0;
  for (const ch of slug) hash = (hash * 31 + ch.charCodeAt(0)) % 140;
  return `hsl(${190 + hash}, 70%, 60%)`;
}

let plot3dLoaded = false;
let plot3dFirstRender = true;
let plot3dTimer = null;
let plot3dPollMs = 60000;

async function loadPollInterval() {
  try {
    const cfg = await (await fetch('api/config')).json();
    if (cfg.poll_interval_seconds) plot3dPollMs = cfg.poll_interval_seconds * 1000;
  } catch (e) {
    console.error(e);
  }
}

function pointHover(name, blend, p) {
  // Kurz und einzeilig halten - mehrzeilige Tooltips laufen auf dem Handy aus dem Bild.
  return `${name} (${blend}%) · T=${p.temp_c}°C · rF=${p.humidity_rel_pct}% · Güte=${p.guete}<extra></extra>`;
}

// Achsenbereich eng um die tatsächlichen Werte legen (statt fester 0-100-artiger
// Skalen), damit der Plot fokussiert bleibt statt viel leeren Raum zu zeigen.
// minSpan verhindert einen zu enger/entarteten Bereich bei sehr ähnlichen Werten,
// hardMin/hardMax kappen nur das Padding an den physikalisch sinnvollen Rändern.
function paddedRange(values, padFrac, minSpan, hardMin, hardMax) {
  if (!values.length) return [hardMin, hardMax];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  let span = hi - lo;
  if (span < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
    span = minSpan;
  }
  const pad = span * padFrac;
  lo -= pad;
  hi += pad;
  if (hardMin !== undefined) lo = Math.max(lo, hardMin);
  if (hardMax !== undefined) hi = Math.min(hi, hardMax);
  return [lo, hi];
}

async function refreshPlot3d() {
  const gd = document.getElementById('plot3d');
  let rooms;
  try {
    rooms = await (await fetch('api/rooms')).json();
  } catch (e) {
    console.error(e);
    return;
  }

  const curves = await Promise.all(rooms.map(async r => {
    try {
      return await (await fetch(`api/rooms/${r.slug}/blend-curve`)).json();
    } catch (e) {
      console.error(e);
      return null;
    }
  }));

  const traces = [];
  const segmentSteps = [];
  const finalStepExtras = [];
  const coneTraces = [];
  const xs = [], ys = [], zs = [];
  function trackBounds(x, y, z) {
    xs.push(x); ys.push(y); zs.push(z);
  }

  rooms.forEach((room, i) => {
    const curveData = curves[i];
    if (!curveData || !curveData.points || curveData.points.length < 2) return;
    const points = curveData.points;
    const guete0 = points[0].guete;
    const baseColor = baseColorForRoom(room.slug);
    points.forEach(p => trackBounds(p.temp_c, p.humidity_rel_pct, p.guete));
    trackBounds(room.ideal_temp, room.ideal_humidity_rel, 100);

    traces.push({
      type: 'scatter3d', mode: 'markers',
      x: [points[0].temp_c], y: [points[0].humidity_rel_pct], z: [points[0].guete],
      marker: { size: 9, color: baseColor, symbol: 'circle' },
      name: room.name,
      hovertemplate: pointHover(room.name, points[0].blend, points[0]),
      showlegend: true,
    });

    traces.push({
      type: 'scatter3d', mode: 'markers',
      x: [room.ideal_temp], y: [room.ideal_humidity_rel], z: [100],
      marker: { size: 6, symbol: 'diamond', color: baseColor },
      hoverinfo: 'skip',
      showlegend: false,
    });

    for (let s = 0; s < points.length - 1; s++) {
      const p0 = points[s], p1 = points[s + 1];
      const color = deltaColor(p1.guete - guete0);
      const idx = traces.length;
      traces.push({
        type: 'scatter3d', mode: 'lines+markers',
        x: [p0.temp_c, p1.temp_c], y: [p0.humidity_rel_pct, p1.humidity_rel_pct], z: [p0.guete, p1.guete],
        line: { color: color, width: 6 },
        marker: { size: 3, color: color },
        hovertemplate: [pointHover(room.name, p0.blend, p0), pointHover(room.name, p1.blend, p1)],
        showlegend: false,
        visible: false,
      });
      (segmentSteps[s] = segmentSteps[s] || []).push(idx);
    }

    const last = points[points.length - 1];
    const prev = points[points.length - 2];
    const coneColor = deltaColor(last.guete - guete0);
    const coneIdx = traces.length;
    const cone = {
      type: 'cone',
      x: [last.temp_c], y: [last.humidity_rel_pct], z: [last.guete],
      u: [last.temp_c - prev.temp_c], v: [last.humidity_rel_pct - prev.humidity_rel_pct], w: [last.guete - prev.guete],
      anchor: 'tip', sizemode: 'absolute', sizeref: 1, // Platzhalter, unten anhand Achsen-Spannweite gesetzt
      colorscale: [[0, coneColor], [1, coneColor]], showscale: false,
      hoverinfo: 'skip',
      visible: false,
    };
    traces.push(cone);
    coneTraces.push(cone);
    finalStepExtras.push(coneIdx);
  });

  // Mindest-Ranges verhindern, dass die Kurven bei fast identischen Werten zu
  // einer Linie kollabieren (2 °C / 10 %-Punkte / 20 Güte-Punkte).
  const xRange = paddedRange(xs, 0.15, 2, 0, 40);
  const yRange = paddedRange(ys, 0.15, 10, 0, 100);
  const zRange = paddedRange(zs, 0.1, 20, 0, 100);

  const coneSizeref = (isMobile ? CONE_SIZE_FACTOR_MOBILE : CONE_SIZE_FACTOR)
    * Math.max(xRange[1] - xRange[0], yRange[1] - yRange[0]);
  coneTraces.forEach(t => { t.sizeref = coneSizeref; });

  const axisStyle = { gridcolor: '#333', zerolinecolor: '#333', color: '#e6e6e6', backgroundcolor: 'rgba(0,0,0,0)',
    tickfont: { size: isMobile ? 9 : 11 }, nticks: isMobile ? 4 : 8, tickangle: 0 };
  const titleFont = { size: isMobile ? 10 : 12 };
  const legend = isMobile
    ? { orientation: 'h', x: 0, xanchor: 'left', y: -0.02, yanchor: 'top', font: { size: 11, color: '#e6e6e6' }, itemwidth: 30 }
    : { orientation: 'v', x: 1, xanchor: 'right', y: 1, font: { color: '#e6e6e6' } };
  const layout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#e6e6e6' },
    margin: { l: 0, r: 0, t: 0, b: 0 },
    uirevision: 'lueftungsguete-3d',
    hovermode: 'closest',
    hoverlabel: { font: { size: isMobile ? 11 : 13 } },
    legend: legend,
    scene: {
      aspectmode: 'cube',
      camera: DEFAULT_CAMERA,
      xaxis: Object.assign({ title: { text: 'Temp (°C)', font: titleFont }, range: xRange }, axisStyle),
      yaxis: Object.assign({ title: { text: 'Feuchte (%)', font: titleFont }, range: yRange }, axisStyle),
      zaxis: Object.assign({ title: { text: 'Güte', font: titleFont }, range: zRange }, axisStyle),
    },
  };

  await Plotly.react(gd, traces, layout, PLOTLY_CONFIG);

  let step = 0;
  const totalSteps = segmentSteps.length;
  const revealTimer = setInterval(() => {
    if (step >= totalSteps) {
      clearInterval(revealTimer);
      return;
    }
    const indices = segmentSteps[step] || [];
    if (indices.length) Plotly.restyle(gd, { visible: true }, indices);
    if (step === totalSteps - 1 && finalStepExtras.length) {
      Plotly.restyle(gd, { visible: true }, finalStepExtras);
    }
    step++;
  }, 120);

  if (plot3dFirstRender) {
    plot3dFirstRender = false;
    startAutoRotate(gd);
    setupPlot3dInteractions(gd);
  }
}

function startAutoRotate(gd) {
  let active = true;
  let programmatic = false;
  const start = performance.now();
  const duration = 8000;

  function frame(now) {
    if (!active) return;
    const elapsed = now - start;
    if (elapsed > duration) { active = false; return; }
    const angle = (elapsed / duration) * Math.PI * 1.2;
    programmatic = true;
    Plotly.relayout(gd, { 'scene.camera.eye': { x: 1.8 * Math.cos(angle), y: 1.8 * Math.sin(angle), z: 0.9 } })
      .then(() => requestAnimationFrame(frame));
  }
  requestAnimationFrame(frame);

  gd.on('plotly_relayout', () => {
    if (programmatic) { programmatic = false; return; }
    active = false;
  });
}

function safeResize(gd) {
  try {
    Plotly.Plots.resize(gd);
  } catch (e) {
    console.error(e);
  }
}

// Einmalig nach dem ersten erfolgreichen Render eingerichtet: Resize/Touch-
// Verhalten, "Ansicht zurücksetzen" und (nur mobil) der Dreh-Toggle.
function setupPlot3dInteractions(gd) {
  if (window.ResizeObserver) {
    new ResizeObserver(() => safeResize(gd)).observe(document.getElementById('plot3d-wrap'));
  }

  window.addEventListener('orientationchange', () => {
    // WebViews melden die neue Größe teils verzögert.
    setTimeout(() => safeResize(gd), 150);
  });

  const resetBtn = document.getElementById('plot-reset-view');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      Plotly.relayout(gd, { 'scene.camera': DEFAULT_CAMERA });
    });
  }

  // Sichtbarkeit von .mobile-only regelt die CSS-Mediaquery; hier nur das
  // tatsächliche Touch-Verhalten - auf Desktop bleibt der Plot immer interaktiv.
  if (isMobile) {
    gd.classList.add('touch-guard');
    const rotateToggle = document.getElementById('plot-rotate-toggle');
    if (rotateToggle) {
      rotateToggle.addEventListener('click', () => {
        const active = gd.classList.toggle('rotate-active');
        rotateToggle.textContent = active ? 'Drehen beenden' : 'Drehen aktivieren';
      });
    }
  }
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'plot3d' && !plot3dLoaded) {
      plot3dLoaded = true;
      loadPollInterval().then(() => {
        refreshPlot3d();
        plot3dTimer = setInterval(refreshPlot3d, plot3dPollMs);
      });
    } else if (btn.dataset.tab === 'plot3d') {
      // Ein Plot, der zuvor in einem display:none-Container gerendert wurde,
      // behält sonst Größe 0 - beim Zurückwechseln einmal neu einpassen.
      safeResize(document.getElementById('plot3d'));
    }
  });
});
</script>
</body>
</html>
"""
