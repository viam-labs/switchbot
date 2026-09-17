"""SwitchBot Thermostat controller.

Combines a Switch (typically the `viam:switchbot:bot` pointed at an A/C
remote) with a Sensor (typically the `viam:switchbot:meter` reading
room temperature) into a temperature-triggered automation. The
controller polls the meter on an interval and presses the switch when
the reading crosses configured thresholds.

Semantics (matched to A/C usage):
  - When temperature rises above `above_temp_c` and the switch is
    currently off (position 0), set position 1 (turnOn).
  - When temperature falls below `below_temp_c` and the switch is
    currently on (position 1), set position 0 (turnOff).
  - Between the two thresholds, do nothing (hysteresis prevents
    rapid on/off oscillation).

Master switch (`enabled`) and active-hours window (`active_start`,
`active_end`) can be toggled at runtime via do_command; runtime
changes persist in the state file and override config on reload.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time as time_of_day
from pathlib import Path
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.components.sensor import Sensor
from viam.components.switch import Switch
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/switchbot-thermostat-{name}-state.json"
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_COOLDOWN_SEC = 300


def _empty_state() -> dict:
    return {
        "enabled": True,
        "above_temp_c": None,
        "below_temp_c": None,
        "active_start": None,
        "active_end": None,
        "last_action_at": None,
        "last_action_position": None,
    }


def _parse_hhmm(value: Any) -> time_of_day | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"expected HH:MM string, got {value!r}")
    h, m = value.split(":", 1)
    hi, mi = int(h), int(m)
    if not (0 <= hi < 24 and 0 <= mi < 60):
        raise ValueError(f"time out of range: {value!r}")
    return time_of_day(hi, mi)


def _within_active_window(
    now: datetime, start: time_of_day | None, end: time_of_day | None
) -> bool:
    if start is None or end is None:
        return True
    now_t = now.time()
    if start == end:
        return True  # zero-length window = always active by convention
    if start < end:
        return start <= now_t < end
    # Window wraps midnight (e.g. 22:00 -> 06:00): active if outside [end, start).
    return now_t >= start or now_t < end


class Thermostat(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "thermostat")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._bot: Switch | None = None
        self._meter: Sensor | None = None
        self._bot_name: str = ""
        self._meter_name: str = ""
        self._poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
        self._cooldown_sec: int = DEFAULT_COOLDOWN_SEC
        self._state_path: str = ""
        self._state: dict = _empty_state()
        self._state_lock: asyncio.Lock | None = None
        self._bg_task: asyncio.Task | None = None

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Thermostat":
        t = cls(config.name)
        t.reconfigure(config, dependencies)
        return t

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        for field in ("bot_name", "meter_name"):
            if not attrs.get(field):
                raise ValueError(f"`{field}` is required")
        for field in ("above_temp_c", "below_temp_c"):
            value = attrs.get(field)
            if value is None:
                raise ValueError(f"`{field}` is required")
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise ValueError(f"`{field}` must be a number")
        if float(attrs["above_temp_c"]) <= float(attrs["below_temp_c"]):
            raise ValueError("`above_temp_c` must be greater than `below_temp_c`")
        for field in ("active_start", "active_end"):
            _parse_hhmm(attrs.get(field))
        # Return the depends_on names so Viam brings them up first.
        return [str(attrs["bot_name"]), str(attrs["meter_name"])]

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._bot_name = str(attrs["bot_name"])
        self._meter_name = str(attrs["meter_name"])
        self._poll_interval_sec = int(attrs.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC)
        self._cooldown_sec = int(attrs.get("cooldown_sec") or DEFAULT_COOLDOWN_SEC)
        self._state_path = str(
            attrs.get("state_path") or DEFAULT_STATE_PATH.format(name=config.name)
        )

        self._bot = None
        self._meter = None
        for name, resource in dependencies.items():
            if name.name == self._bot_name and isinstance(resource, Switch):
                self._bot = resource
            elif name.name == self._meter_name and isinstance(resource, Sensor):
                self._meter = resource
        if self._bot is None:
            raise RuntimeError(f"Switch dependency {self._bot_name!r} not found")
        if self._meter is None:
            raise RuntimeError(f"Sensor dependency {self._meter_name!r} not found")

        # Load persisted state; config values seed anything runtime hasn't
        # overridden yet.
        loaded = self._load_state()
        self._state = _empty_state()
        self._state.update({k: v for k, v in loaded.items() if k in self._state})
        if self._state["above_temp_c"] is None:
            self._state["above_temp_c"] = float(attrs["above_temp_c"])
        if self._state["below_temp_c"] is None:
            self._state["below_temp_c"] = float(attrs["below_temp_c"])
        if self._state["active_start"] is None:
            self._state["active_start"] = attrs.get("active_start") or None
        if self._state["active_end"] is None:
            self._state["active_end"] = attrs.get("active_end") or None
        if "enabled" in attrs and "enabled" not in loaded:
            self._state["enabled"] = bool(attrs["enabled"])

        self._state_lock = asyncio.Lock()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._bg_task = None

    # ------------------------------------------------------------------
    # State persistence

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load state from %s (using empty): %s", path, e)
            return {}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    # ------------------------------------------------------------------
    # Background loop

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("thermostat tick failed: %s", e)
            await asyncio.sleep(self._poll_interval_sec)

    async def _tick(self) -> None:
        assert self._state_lock is not None
        async with self._state_lock:
            if not self._state.get("enabled"):
                return
            now_local = datetime.now().astimezone()
            start = _parse_hhmm(self._state.get("active_start"))
            end = _parse_hhmm(self._state.get("active_end"))
            if not _within_active_window(now_local, start, end):
                return

            last_action_iso = self._state.get("last_action_at")
            if last_action_iso:
                try:
                    last_dt = datetime.fromisoformat(last_action_iso)
                except ValueError:
                    last_dt = None
                if last_dt is not None:
                    elapsed = (datetime.now(UTC) - last_dt).total_seconds()
                    if elapsed < self._cooldown_sec:
                        return

            readings = await self._meter.get_readings()
            temp_c = None
            if isinstance(readings, dict):
                temp_c = readings.get("temperature_c") or readings.get("temperature")
            if not isinstance(temp_c, int | float):
                return

            above = float(self._state["above_temp_c"])
            below = float(self._state["below_temp_c"])
            position = await self._bot.get_position()

            if temp_c > above and position == 0:
                await self._bot.set_position(1)
                self._record_action(1)
            elif temp_c < below and position == 1:
                await self._bot.set_position(0)
                self._record_action(0)

    def _record_action(self, position: int) -> None:
        self._state["last_action_at"] = datetime.now(UTC).isoformat()
        self._state["last_action_position"] = position
        self._save_state()

    # ------------------------------------------------------------------
    # do_command

    async def _status(self) -> dict:
        assert self._state_lock is not None
        temp_c: float | None = None
        humidity: float | None = None
        position: int | None = None
        try:
            readings = await self._meter.get_readings()
            if isinstance(readings, dict):
                raw_temp = readings.get("temperature_c") or readings.get("temperature")
                raw_humidity = readings.get("humidity_pct") or readings.get("humidity")
                if isinstance(raw_temp, int | float):
                    temp_c = float(raw_temp)
                if isinstance(raw_humidity, int | float):
                    humidity = float(raw_humidity)
        except Exception as e:
            LOGGER.warning("status meter read failed: %s", e)
        try:
            position = int(await self._bot.get_position())
        except Exception as e:
            LOGGER.warning("status bot read failed: %s", e)

        now_local = datetime.now().astimezone()
        start = _parse_hhmm(self._state.get("active_start"))
        end = _parse_hhmm(self._state.get("active_end"))
        return {
            "enabled": bool(self._state.get("enabled")),
            "above_temp_c": self._state.get("above_temp_c"),
            "below_temp_c": self._state.get("below_temp_c"),
            "active_start": self._state.get("active_start"),
            "active_end": self._state.get("active_end"),
            "within_active_window": _within_active_window(now_local, start, end),
            "temperature_c": temp_c,
            "humidity_pct": humidity,
            "bot_position": position,
            "last_action_at": self._state.get("last_action_at"),
            "last_action_position": self._state.get("last_action_position"),
        }

    async def _set_enabled(self, enabled: bool) -> dict:
        assert self._state_lock is not None
        async with self._state_lock:
            self._state["enabled"] = bool(enabled)
            self._save_state()
        return {"ok": True, "enabled": bool(enabled)}

    async def _set_thresholds(self, above: Any, below: Any) -> dict:
        if not isinstance(above, int | float) or not isinstance(below, int | float):
            raise ValueError("`above_c` and `below_c` must be numbers")
        if float(above) <= float(below):
            raise ValueError("`above_c` must be greater than `below_c`")
        assert self._state_lock is not None
        async with self._state_lock:
            self._state["above_temp_c"] = float(above)
            self._state["below_temp_c"] = float(below)
            self._save_state()
        return {"ok": True, "above_temp_c": float(above), "below_temp_c": float(below)}

    async def _set_active_hours(self, start: Any, end: Any) -> dict:
        # Both empty/None disables the window (always active).
        start_norm = None if start in (None, "") else str(start)
        end_norm = None if end in (None, "") else str(end)
        _parse_hhmm(start_norm)
        _parse_hhmm(end_norm)
        if (start_norm is None) != (end_norm is None):
            raise ValueError("provide both `active_start` and `active_end`, or neither")
        assert self._state_lock is not None
        async with self._state_lock:
            self._state["active_start"] = start_norm
            self._state["active_end"] = end_norm
            self._save_state()
        return {"ok": True, "active_start": start_norm, "active_end": end_norm}

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
        if verb == "set_enabled":
            return await self._set_enabled(bool(command.get("enabled")))
        if verb == "set_thresholds":
            return await self._set_thresholds(command.get("above_c"), command.get("below_c"))
        if verb == "set_active_hours":
            return await self._set_active_hours(command.get("start"), command.get("end"))
        raise ValueError(f"Unknown command: {verb!r}")
