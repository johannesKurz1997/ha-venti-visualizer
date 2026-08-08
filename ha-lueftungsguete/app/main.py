from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from config import AddonOptions, RoomConfig, load_options
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
        self.latest_bad: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self.ha.aclose()

    async def poll_once(self) -> None:
        states = await self.ha.get_all_states()

        weather_state = states.get(self.options.outdoor_weather_entity)
        temp_out, hum_out = _outdoor_from_weather(weather_state)
        if temp_out is None or hum_out is None:
            logger.warning(
                "Außenwerte nicht verfügbar (%s) - überspringe Zyklus", self.options.outdoor_weather_entity
            )
            return
        abs_out = absolute_humidity_gm3(temp_out, hum_out)

        present_persons: set[str] = {
            target.person_entity
            for target in self.options.notify_targets
            if (p := states.get(target.person_entity)) is not None and p.get("state") == "home"
        }

        rooms_result: dict[str, Any] = {}
        for room in self.options.rooms:
            result = await self._process_room(room, states, temp_out, hum_out, abs_out, present_persons)
            if result is not None:
                rooms_result[room.slug] = result

        bad_result = await self._process_bad(states)

        async with self.lock:
            self.latest_rooms = rooms_result
            if bad_result is not None:
                self.latest_bad = bad_result

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

        curve = blend_curve(
            temp_in, abs_in, temp_out, abs_out,
            room.ideal_temp, ideal_abs, room.weight_temp, room.weight_humidity,
            step=self.options.blend_steps,
        )
        guete_0 = curve[0].guete
        guete_100 = curve[-1].guete
        delta = guete_100 - guete_0

        recommend, rising_edge, stable_since = self._update_stability(room.slug, delta)

        await self.ha.set_state(
            room.binary_sensor_entity_id,
            "on" if recommend else "off",
            {
                "friendly_name": f"Lüften {room.name}",
                "device_class": "opening",
                "delta_guete": round(delta, 4),
                "stable_since": stable_since,
            },
        )

        if rising_edge:
            await self._notify(
                present_persons,
                f"Lüften empfohlen: {room.name} (ΔGüte {delta:.2f}, seit "
                f"{self.options.stability_minutes} Min. stabil)",
            )

        self.persistence.append_history(
            {
                "ts": _now_iso(),
                "room": room.name,
                "guete_0": round(guete_0, 4),
                "guete_100": round(guete_100, 4),
                "delta_guete": round(delta, 4),
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
            "ideal_abs_humidity": round(ideal_abs, 3),
            "outdoor_temp": round(temp_out, 2),
            "outdoor_humidity_rel": round(hum_out, 2),
            "outdoor_abs_humidity": round(abs_out, 3),
            "guete_current": round(guete_0, 4),
            "guete_full_outside": round(guete_100, 4),
            "delta_guete": round(delta, 4),
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

        self.persistence.set_room_state(
            room_slug, {"positive_since": positive_since, "luften_on": recommend}
        )
        rising_edge = recommend and not was_on
        return recommend, rising_edge, positive_since

    async def _process_bad(self, states: dict[str, dict]) -> dict[str, Any] | None:
        bad = self.options.bad
        temp_bad = _parse_float(states.get(bad.temp_entity))
        hum_bad = _parse_float(states.get(bad.humidity_entity))
        if temp_bad is None or hum_bad is None:
            logger.warning("Bad: Sensordaten nicht verfügbar, überspringe")
            return None

        abs_bad = absolute_humidity_gm3(temp_bad, hum_bad)
        state = self.persistence.get_bad_state()
        risk_on = bool(state.get("risk_on", False))

        if not risk_on and abs_bad >= bad.mold_risk_threshold:
            risk_on = True
        elif risk_on and abs_bad <= bad.mold_risk_threshold - bad.mold_risk_hysteresis:
            risk_on = False

        self.persistence.set_bad_state({"risk_on": risk_on})

        await self.ha.set_state(
            "binary_sensor.schimmelrisiko_bad",
            "on" if risk_on else "off",
            {
                "friendly_name": "Schimmelrisiko Bad",
                "device_class": "problem",
                "abs_humidity": round(abs_bad, 3),
                "threshold": bad.mold_risk_threshold,
            },
        )

        return {
            "temp_entity": bad.temp_entity,
            "humidity_entity": bad.humidity_entity,
            "temp": round(temp_bad, 2),
            "humidity_rel": round(hum_bad, 2),
            "abs_humidity": round(abs_bad, 3),
            "mold_risk_threshold": bad.mold_risk_threshold,
            "mold_risk_hysteresis": bad.mold_risk_hysteresis,
            "schimmelrisiko": risk_on,
            "binary_sensor": "binary_sensor.schimmelrisiko_bad",
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
        for room in self.options.rooms:
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


@app.get("/api/rooms")
async def api_rooms() -> JSONResponse:
    controller = _controller(app)
    async with controller.lock:
        return JSONResponse(list(controller.latest_rooms.values()))


@app.get("/api/bad")
async def api_bad() -> JSONResponse:
    controller = _controller(app)
    async with controller.lock:
        return JSONResponse(controller.latest_bad)


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
        room.ideal_temp, ideal_abs, room.weight_temp, room.weight_humidity,
        step=controller.options.blend_steps,
    )
    return JSONResponse(
        {
            "room": room.name,
            "ideal_temp": room.ideal_temp,
            "ideal_abs_humidity": round(ideal_abs, 3),
            "points": [p.to_dict() for p in curve],
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Lüftungsgüte</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 1.3rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; }
  th { color: #999; font-weight: 600; font-size: 0.85rem; }
  .badge { padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }
  .on { background: #1f6f3a; color: #d7ffe0; }
  .off { background: #333; color: #aaa; }
  .muted { color: #888; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Lüftungsgüte</h1>
<p class="muted">Aktualisiert sich alle paar Sekunden. Voller Datenzugriff über <code>/api/rooms</code> und <code>/api/rooms/{room}/blend-curve</code>.</p>
<h2>Räume</h2>
<table id="rooms"><thead><tr>
  <th>Raum</th><th>T innen</th><th>F rel innen</th><th>Güte</th><th>ΔGüte</th><th>Lüften?</th>
</tr></thead><tbody></tbody></table>
<h2>Bad</h2>
<table id="bad"><thead><tr>
  <th>T</th><th>F rel</th><th>F abs</th><th>Schimmelrisiko</th>
</tr></thead><tbody></tbody></table>
<script>
async function refresh() {
  try {
    const rooms = await (await fetch('api/rooms')).json();
    const roomsBody = document.querySelector('#rooms tbody');
    roomsBody.innerHTML = rooms.map(r => `<tr>
      <td>${r.name}</td>
      <td>${r.temp_in} °C</td>
      <td>${r.humidity_rel_in} %</td>
      <td>${r.guete_current}</td>
      <td>${r.delta_guete}</td>
      <td><span class="badge ${r.luften_empfohlen ? 'on' : 'off'}">${r.luften_empfohlen ? 'ja' : 'nein'}</span></td>
    </tr>`).join('');

    const bad = await (await fetch('api/bad')).json();
    const badBody = document.querySelector('#bad tbody');
    if (bad && bad.temp !== undefined) {
      badBody.innerHTML = `<tr>
        <td>${bad.temp} °C</td>
        <td>${bad.humidity_rel} %</td>
        <td>${bad.abs_humidity} g/m³</td>
        <td><span class="badge ${bad.schimmelrisiko ? 'on' : 'off'}">${bad.schimmelrisiko ? 'Risiko' : 'ok'}</span></td>
      </tr>`;
    }
  } catch (e) {
    console.error(e);
  }
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
