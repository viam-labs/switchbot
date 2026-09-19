"""SwitchBot Clicker — a Bot in Press mode exposed as a momentary tap.

DoCommand verbs:
- status → {kind, last_clicked_at, battery, hold_ms, ready}
- click  → send SwitchBot `press`; stamps last_clicked_at.

Bot should be configured in Press Mode via the SwitchBot app so a
single `press` call does a tap. `hold_ms` is reserved for a future
turnOn/turnOff style if a bot is in Switch mode; Press mode ignores
it.

Deliberately generic — the meaning of the click (door intercom,
doorbell, garage remote) is a concern for whatever frontend wraps
it, not this module.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/switchbot-clicker-{name}-state.json"
DEFAULT_HOLD_MS = 500


class Clicker(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "clicker")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: SwitchBotClient | None = None
        self._device_id: str = ""
        self._hold_ms: int = DEFAULT_HOLD_MS
        self._state_path: str = ""
        self._state: dict = {"last_clicked_at": None}
        self._state_lock: asyncio.Lock | None = None

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Clicker":
        c = cls(config.name)
        c.reconfigure(config, dependencies)
        return c

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        for field in ("token", "secret", "device_id"):
            if not attrs.get(field):
                raise ValueError(f"'{field}' is required")
        hold_ms = attrs.get("hold_ms")
        if hold_ms is not None and (
            not isinstance(hold_ms, int | float) or isinstance(hold_ms, bool) or hold_ms < 0
        ):
            raise ValueError("`hold_ms` must be a non-negative number if set")
        return []

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._client = SwitchBotClient(attrs["token"], attrs["secret"])
        self._device_id = attrs["device_id"]
        hold_ms = attrs.get("hold_ms")
        self._hold_ms = int(hold_ms) if hold_ms is not None else DEFAULT_HOLD_MS
        self._state_path = str(
            attrs.get("state_path") or DEFAULT_STATE_PATH.format(name=config.name)
        )
        self._state = self._load_state()
        self._state_lock = asyncio.Lock()

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {"last_clicked_at": None}
        try:
            loaded = json.loads(path.read_text())
            return {"last_clicked_at": loaded.get("last_clicked_at")}
        except Exception as e:
            LOGGER.warning("failed to load clicker state from %s: %s", path, e)
            return {"last_clicked_at": None}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    async def _status(self) -> dict:
        assert self._client is not None
        battery = None
        try:
            raw = await self._client.get_status(self._device_id)
            if isinstance(raw, dict):
                battery = raw.get("battery")
        except Exception as e:
            LOGGER.warning("clicker status battery read failed: %s", e)
        return {
            "kind": "clicker",
            "last_clicked_at": self._state.get("last_clicked_at"),
            "battery": battery,
            "hold_ms": self._hold_ms,
            "ready": True,
        }

    async def _click(self) -> dict:
        assert self._client is not None
        assert self._state_lock is not None
        await self._client.send_command(self._device_id, "press")
        fired_at = datetime.now(UTC).isoformat()
        async with self._state_lock:
            self._state["last_clicked_at"] = fired_at
            self._save_state()
        return {"ok": True, "last_clicked_at": fired_at}

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs,
    ) -> Mapping[str, Any]:
        verb = command.get("command")
        if verb == "status":
            return await self._status()
        if verb == "click":
            return await self._click()
        raise ValueError(f"unknown command: {verb!r}")
