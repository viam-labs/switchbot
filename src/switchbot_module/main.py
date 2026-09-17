"""SwitchBot module entrypoint. Registers models and starts the server."""

import asyncio

from viam.components.generic import Generic
from viam.components.sensor import Sensor
from viam.components.switch import Switch
from viam.module.module import Module
from viam.resource.registry import Registry, ResourceCreatorRegistration

from .bot import Bot
from .curtain import Curtain
from .meter import Meter


def _register() -> None:
    Registry.register_resource_creator(
        Switch.API,
        Bot.MODEL,
        ResourceCreatorRegistration(Bot.new, Bot.validate_config),
    )
    Registry.register_resource_creator(
        Generic.API,
        Curtain.MODEL,
        ResourceCreatorRegistration(Curtain.new, Curtain.validate_config),
    )
    Registry.register_resource_creator(
        Sensor.API,
        Meter.MODEL,
        ResourceCreatorRegistration(Meter.new, Meter.validate_config),
    )


async def main() -> None:
    _register()
    module = Module.from_args()
    module.add_model_from_registry(Switch.API, Bot.MODEL)
    module.add_model_from_registry(Generic.API, Curtain.MODEL)
    module.add_model_from_registry(Sensor.API, Meter.MODEL)
    await module.start()


if __name__ == "__main__":
    asyncio.run(main())
