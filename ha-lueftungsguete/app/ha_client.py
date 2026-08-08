from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("lueftungsguete.ha_client")

CORE_API_BASE = "http://supervisor/core/api"


class HAClient:
    """Kommuniziert mit der Home-Assistant-Core-API über den Supervisor-Proxy.

    Nutzt SUPERVISOR_TOKEN (von homeassistant_api: true injiziert), keinen
    manuell verwalteten Long-Lived Token.
    """

    def __init__(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            logger.warning("SUPERVISOR_TOKEN nicht gesetzt - lokale Entwicklung?")
        self._client = httpx.AsyncClient(
            base_url=CORE_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

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
