"""SwitchBot Curtain as a Viam generic component.

DoCommand verbs: open, close, pause, set_position (0-100), status
(alias: get_status). Position uses SwitchBot's "setPosition" with
parameter "0,ff,{n}" where n is percent 0..100 (0 = fully open, 100
= fully closed).
"""

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient


class Curtain(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "curtain")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: SwitchBotClient | None = None
        self._device_id: str = ""

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Curtain":
        c = cls(config.name)
        c.reconfigure(config, dependencies)
        return c

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

    async def do_command(
        self, command: Mapping[str, Any], *, timeout: float | None = None, **kwargs
    ) -> Mapping[str, Any]:
        assert self._client is not None
        verb = command.get("command")
        if verb == "open":
            result = await self._client.send_command(self._device_id, "turnOn")
        elif verb == "close":
            result = await self._client.send_command(self._device_id, "turnOff")
        elif verb == "pause":
            result = await self._client.send_command(self._device_id, "pause")
        elif verb == "set_position":
            position = int(command["position"])
            if not 0 <= position <= 100:
                raise ValueError("position must be 0..100")
            result = await self._client.send_command(
                self._device_id, "setPosition", parameter=f"0,ff,{position}"
            )
        elif verb in ("status", "get_status"):
            raw = await self._client.get_status(self._device_id)
            slide_position = raw.get("slidePosition")
            return {
                "slide_position": slide_position,
                "battery": raw.get("battery"),
                "moving": raw.get("moving"),
                "calibrate": raw.get("calibrate"),
                "raw": dict(raw),
            }
        else:
            raise ValueError(f"unknown command: {verb!r}")
        return {"ok": True, "result": dict(result)}
