"""SwitchBot Meter / Hub 2 as a Viam sensor.

Returns temperature (celsius) and humidity (percent). Hub 2's built-in
sensor uses the same /devices/{id}/status response shape, so the same
model backs both.
"""

from typing import Any, ClassVar, Mapping, Optional, Sequence

from viam.components.sensor import Sensor
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient


class Meter(Sensor):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam-labs", "switchbot"), "meter")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: Optional[SwitchBotClient] = None
        self._device_id: str = ""

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Meter":
        m = cls(config.name)
        m.reconfigure(config, dependencies)
        return m

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

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Mapping[str, Any]:
        assert self._client is not None
        s = await self._client.get_status(self._device_id)
        return {
            "temperature_c": s.get("temperature"),
            "humidity_pct": s.get("humidity"),
            "battery_pct": s.get("battery"),
        }
