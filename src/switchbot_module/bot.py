"""SwitchBot Bot as a Viam switch.

Position 0 = off (turnOff), position 1 = on (turnOn). The Bot is a
two-position switch; number_of_positions is always 2.

State is persisted to a local JSON file so `get_position` returns the
last commanded value, not whatever SwitchBot's cloud reports. A Bot in
"Press mode" doesn't hold physical position, so SwitchBot's `power`
field for it is unreliable — callers (the Thermostat controller, the
frontend) making decisions on `get_position` were getting garbage.

The persisted state is authoritative once the module has commanded at
least once. On first-ever call (no state file), we fall back to
SwitchBot's reported value as a best-guess seed.

If someone physically presses the Bot or toggles via the SwitchBot app,
our state diverges — we don't try to reconcile. A subsequent
`set_position` from any caller writes over it. This is documented as
a limitation.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from viam.components.switch import Switch
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/switchbot-bot-{name}-state.json"


class Bot(Switch):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "bot")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: SwitchBotClient | None = None
        self._device_id: str = ""
        self._state_path: str = ""
        self._state: dict = {}
        self._state_lock: asyncio.Lock | None = None

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Bot":
        b = cls(config.name)
        b.reconfigure(config, dependencies)
        return b

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        for field in ("token", "secret", "device_id"):
            if not attrs.get(field):
                raise ValueError(f"'{field}' is required")
        return []

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._client = SwitchBotClient(attrs["token"], attrs["secret"])
        self._device_id = attrs["device_id"]
        self._state_path = str(
            attrs.get("state_path") or DEFAULT_STATE_PATH.format(name=config.name)
        )
        self._state = self._load_state()
        self._state_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # State persistence

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load bot state from %s: %s", path, e)
            return {}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _record(self, position: int) -> None:
        self._state["position"] = position
        self._state["last_set_at"] = datetime.now(UTC).isoformat()
        self._state["last_set_position"] = position
        self._save_state()

    # ------------------------------------------------------------------
    # Switch API

    async def set_position(self, position: int, **kwargs) -> None:
        assert self._client is not None
        assert self._state_lock is not None
        command = "turnOn" if position else "turnOff"
        async with self._state_lock:
            await self._client.send_command(self._device_id, command)
            self._record(1 if position else 0)

    async def get_position(self, **kwargs) -> int:
        assert self._client is not None
        # Prefer our persisted state — SwitchBot's `power` field for a
        # Press-mode Bot is unreliable. Fall back to the API only when
        # we've never commanded (empty state file), and seed on success.
        stored = self._state.get("position")
        if stored in (0, 1):
            return stored
        status = await self._client.get_status(self._device_id)
        position = 1 if status.get("power") == "on" else 0
        assert self._state_lock is not None
        async with self._state_lock:
            self._record(position)
        return position

    async def get_number_of_positions(self, **kwargs) -> int:
        return 2

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs,
    ) -> Mapping[str, Any]:
        verb = command.get("command")
        if verb == "state":
            # Return the persisted state so callers can show
            # "last set ON at X" across devices.
            return {
                "position": self._state.get("position"),
                "last_set_at": self._state.get("last_set_at"),
                "last_set_position": self._state.get("last_set_position"),
            }
        if verb == "resync":
            # Force-refresh from SwitchBot in case someone pressed the
            # physical button or toggled from the SwitchBot app.
            assert self._client is not None
            assert self._state_lock is not None
            status = await self._client.get_status(self._device_id)
            position = 1 if status.get("power") == "on" else 0
            async with self._state_lock:
                self._record(position)
            return {"position": position, "resynced": True}
        raise ValueError(f"Unknown command: {verb!r}")
