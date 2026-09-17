"""SwitchBot Bot as a Viam switch.

Position 0 = off (turnOff), position 1 = on (turnOn). The Bot is a
two-position switch; number_of_positions is always 2.
"""

from typing import ClassVar, Mapping, Optional, Sequence

from viam.components.switch import Switch
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient


class Bot(Switch):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam-labs", "switchbot"), "bot")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: Optional[SwitchBotClient] = None
        self._device_id: str = ""

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

    async def set_position(self, position: int, **kwargs) -> None:
        assert self._client is not None
        command = "turnOn" if position else "turnOff"
        await self._client.send_command(self._device_id, command)

    async def get_position(self, **kwargs) -> int:
        assert self._client is not None
        status = await self._client.get_status(self._device_id)
        return 1 if status.get("power") == "on" else 0

    async def get_number_of_positions(self, **kwargs) -> int:
        return 2
