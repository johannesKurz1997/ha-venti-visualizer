from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

STATE_PATH = Path(os.environ.get("ADDON_STATE_PATH", "/data/state.json"))
MAX_HISTORY_ENTRIES = 500

_DEFAULT_STATE: dict[str, Any] = {"rooms": {}, "no_window_rooms": {}, "history": []}


class Persistence:
    """Einfache JSON-Persistenz unter /data für Stabilitäts-Zustand und Score-Historie.

    Zustand wird über Neustarts hinweg gehalten, damit das Stabilitäts-Fenster
    (Punkt 4 der fachlichen Logik) nicht bei jedem Container-Neustart verloren geht.
    """

    def __init__(self, path: Path = STATE_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return json.loads(json.dumps(_DEFAULT_STATE))
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, default in _DEFAULT_STATE.items():
                data.setdefault(key, json.loads(json.dumps(default)))
            return data
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(_DEFAULT_STATE))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False)
        os.replace(tmp_path, self._path)

    def get_room_state(self, room_slug: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._state["rooms"].get(room_slug, {}))

    def set_room_state(self, room_slug: str, room_state: dict[str, Any]) -> None:
        with self._lock:
            self._state["rooms"][room_slug] = room_state
            self._save()

    def get_no_window_state(self, room_slug: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._state["no_window_rooms"].get(room_slug, {}))

    def set_no_window_state(self, room_slug: str, room_state: dict[str, Any]) -> None:
        with self._lock:
            self._state["no_window_rooms"][room_slug] = room_state
            self._save()

    def append_history(self, entry: dict[str, Any]) -> None:
        with self._lock:
            history: list = self._state["history"]
            history.append(entry)
            if len(history) > MAX_HISTORY_ENTRIES:
                del history[: len(history) - MAX_HISTORY_ENTRIES]
            self._save()

    def get_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._state["history"][-limit:])
