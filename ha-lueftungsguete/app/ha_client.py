from __future__ import annotations

import itertools
import json
import logging
import os

import httpx
import websockets

logger = logging.getLogger("lueftungsguete.ha_client")

CORE_API_BASE = "http://supervisor/core/api"
CORE_WS_URL = "ws://supervisor/core/websocket"


class HAClient:
    """Kommuniziert mit der Home-Assistant-Core-API über den Supervisor-Proxy.

    Nutzt SUPERVISOR_TOKEN (von homeassistant_api: true injiziert), keinen
    manuell verwalteten Long-Lived Token.
    """

    def __init__(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            logger.warning("SUPERVISOR_TOKEN nicht gesetzt - lokale Entwicklung?")
        self._token = token or ""
        self._client = httpx.AsyncClient(
            base_url=CORE_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        self._ws_id_counter = itertools.count(1)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_all_states(self) -> dict[str, dict]:
        resp = await self._client.get("/states")
        resp.raise_for_status()
        return {entry["entity_id"]: entry for entry in resp.json()}

    async def get_state(self, entity_id: str) -> dict | None:
        resp = await self._client.get(f"/states/{entity_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def set_state(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        payload = {"state": state, "attributes": attributes or {}}
        resp = await self._client.post(f"/states/{entity_id}", json=payload)
        resp.raise_for_status()

    async def call_service(self, domain: str, service: str, service_data: dict | None = None) -> None:
        resp = await self._client.post(f"/services/{domain}/{service}", json=service_data or {})
        resp.raise_for_status()

    async def get_registries(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Area-/Device-/Entity-Registry über die Core-WebSocket-API.

        Diese Registries sind nur über die WS-API abrufbar, nicht über die
        REST-States-API - daher eine eigene, kurzlebige Verbindung pro Aufruf
        statt eines dauerhaften WS-Clients (wird nur selten gebraucht, siehe
        area_discovery_interval_minutes).
        """
        async with websockets.connect(CORE_WS_URL, max_size=8 * 1024 * 1024) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_result = json.loads(await ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"WebSocket-Auth fehlgeschlagen: {auth_result}")

            areas = await self._ws_call(ws, "config/area_registry/list")
            devices = await self._ws_call(ws, "config/device_registry/list")
            entities = await self._ws_call(ws, "config/entity_registry/list")
            return areas, devices, entities

    async def _ws_call(self, ws, command_type: str) -> list[dict]:
        msg_id = next(self._ws_id_counter)
        await ws.send(json.dumps({"id": msg_id, "type": command_type}))
        while True:
            response = json.loads(await ws.recv())
            if response.get("id") != msg_id:
                continue
            if not response.get("success"):
                raise RuntimeError(f"WS-Kommando {command_type} fehlgeschlagen: {response.get('error')}")
            return response["result"]
