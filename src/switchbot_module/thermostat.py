"""SwitchBot Thermostat controller.

Combines a Switch (typically the `viam:switchbot:bot` pointed at a
remote) with a Sensor (typically the `viam:switchbot:meter` reading
room temperature) into a list of temperature-triggered automations.

The controller holds an ordered list of named automations. On each
tick it walks the list, picks the first automation that is (a) enabled
and (b) whose active-hours window includes "now", and uses that
automation's thresholds to decide whether to press the Bot on or off.

Threshold semantics are per-automation and infer direction from the
relative position of the two values:

  - Cooling (A/C): `on_temp_c` > `off_temp_c` — turn on when temp
    rises above `on_temp_c`; turn off when it falls below `off_temp_c`.
  - Heating: `on_temp_c` < `off_temp_c` — turn on when temp falls
    below `on_temp_c`; turn off when it rises above `off_temp_c`.

Between the two thresholds nothing happens (hysteresis).

Automations, their enabled flag, and their ordering all persist in
the state file. Runtime edits via do_command override the initial
config seed on load.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from datetime import time as time_of_day
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


def _mode_from_thresholds(on_temp_c: float, off_temp_c: float) -> str:
    """'cooling' when on > off, 'heating' when on < off."""
    return "cooling" if on_temp_c > off_temp_c else "heating"


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


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _normalize_days(days: Any) -> list[int]:
    """Validate + dedupe 0..6 weekdays (Mon..Sun). Empty = every day.

    Viam serializes numeric config fields through protobuf's Value
    type, which stores everything as double — so `[0, 1, 2]` from the
    frontend arrives as `[0.0, 1.0, 2.0]`. Accept int-valued floats
    and coerce.
    """
    if days is None:
        return []
    if not isinstance(days, list):
        raise ValueError("`days_of_week` must be a list of integers 0..6")
    out = set()
    for d in days:
        if isinstance(d, bool):
            raise ValueError("`days_of_week` values must be integers 0..6")
        if isinstance(d, int):
            di = d
        elif isinstance(d, float) and d.is_integer():
            di = int(d)
        else:
            raise ValueError("`days_of_week` values must be integers 0..6")
        if not 0 <= di <= 6:
            raise ValueError("`days_of_week` values must be integers 0..6")
        out.add(di)
    return sorted(out)


def _empty_state() -> dict:
    return {
        "automations": [],
        "last_action_at": None,
        "last_action_position": None,
    }


def _normalize_automation(raw: dict, default_enabled: bool = True) -> dict:
    """Coerce an automation dict into the canonical shape.

    Two kinds: 'hysteresis' (temp thresholds + active window) and
    'scheduled' (one-shot fire at a time + days). Missing `kind`
    defaults to 'hysteresis' for backward compat with older configs.
    """
    kind = raw.get("kind") or "hysteresis"
    if kind not in ("hysteresis", "scheduled"):
        raise ValueError(f"unknown automation `kind`: {kind!r}")
    common = {
        "id": raw.get("id") or _new_id(),
        "name": str(raw.get("name") or "Automation"),
        "kind": kind,
        "enabled": bool(raw.get("enabled", default_enabled)),
        "days_of_week": _normalize_days(raw.get("days_of_week")),
    }
    if kind == "hysteresis":
        if "on_temp_c" not in raw or "off_temp_c" not in raw:
            raise ValueError("hysteresis automation must include `on_temp_c` and `off_temp_c`")
        on_temp = raw["on_temp_c"]
        off_temp = raw["off_temp_c"]
        if not isinstance(on_temp, int | float) or isinstance(on_temp, bool):
            raise ValueError("`on_temp_c` must be a number")
        if not isinstance(off_temp, int | float) or isinstance(off_temp, bool):
            raise ValueError("`off_temp_c` must be a number")
        if float(on_temp) == float(off_temp):
            raise ValueError(
                "`on_temp_c` and `off_temp_c` must differ "
                "(on > off = cooling, on < off = heating)"
            )
        active_start = raw.get("active_start") or None
        active_end = raw.get("active_end") or None
        _parse_hhmm(active_start)
        _parse_hhmm(active_end)
        if (active_start is None) != (active_end is None):
            raise ValueError("provide both `active_start` and `active_end`, or neither")
        return {
            **common,
            "on_temp_c": float(on_temp),
            "off_temp_c": float(off_temp),
            "active_start": active_start,
            "active_end": active_end,
        }
    # kind == "scheduled"
    action = raw.get("action")
    if action not in ("on", "off"):
        raise ValueError("scheduled automation `action` must be 'on' or 'off'")
    time_str = raw.get("time")
    if _parse_hhmm(time_str) is None:
        raise ValueError("scheduled automation `time` (HH:MM) is required")
    return {
        **common,
        "action": action,
        "time": time_str,
        "last_fired_at": raw.get("last_fired_at"),
    }


def _automation_view(auto: dict) -> dict:
    kind = auto.get("kind") or "hysteresis"
    if kind == "hysteresis":
        return {
            **auto,
            "kind": "hysteresis",
            "mode": _mode_from_thresholds(float(auto["on_temp_c"]), float(auto["off_temp_c"])),
        }
    return {**auto, "kind": "scheduled"}


def _migrate_legacy_state(loaded: dict) -> dict:
    """Convert a v0.0.5 single-automation state file into the new shape."""
    migrated = _empty_state()
    if loaded.get("last_action_at"):
        migrated["last_action_at"] = loaded["last_action_at"]
    if loaded.get("last_action_position") in (0, 1):
        migrated["last_action_position"] = loaded["last_action_position"]
    on_temp = loaded.get("on_temp_c")
    off_temp = loaded.get("off_temp_c")
    if isinstance(on_temp, int | float) and isinstance(off_temp, int | float):
        try:
            migrated["automations"].append(
                _normalize_automation(
                    {
                        "name": "Default",
                        "enabled": loaded.get("enabled", True),
                        "on_temp_c": on_temp,
                        "off_temp_c": off_temp,
                        "active_start": loaded.get("active_start"),
                        "active_end": loaded.get("active_end"),
                    }
                )
            )
        except ValueError as e:
            LOGGER.warning("legacy state migration dropped bad automation: %s", e)
    return migrated


class Thermostat(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "thermostat")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._bot: Switch | None = None
        self._meter: Sensor | None = None
        self._events_sensor: Sensor | None = None
        self._bot_name: str = ""
        self._meter_name: str = ""
        self._events_sensor_name: str = ""
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
        raw_automations = attrs.get("automations")
        if raw_automations is not None:
            if not isinstance(raw_automations, list):
                raise ValueError("`automations` must be a list")
            for entry in raw_automations:
                if not isinstance(entry, dict):
                    raise ValueError("each automation must be an object")
                _normalize_automation(entry)
        elif "on_temp_c" in attrs or "off_temp_c" in attrs:
            # Legacy single-automation config; normalize as a sanity check.
            _normalize_automation(
                {
                    "name": "Default",
                    "enabled": attrs.get("enabled", True),
                    "on_temp_c": attrs.get("on_temp_c"),
                    "off_temp_c": attrs.get("off_temp_c"),
                    "active_start": attrs.get("active_start"),
                    "active_end": attrs.get("active_end"),
                }
            )
        deps = [str(attrs["bot_name"]), str(attrs["meter_name"])]
        events_sensor = attrs.get("events_sensor")
        if events_sensor is not None:
            if not isinstance(events_sensor, str) or not events_sensor:
                raise ValueError("`events_sensor` must be a non-empty string")
            deps.append(events_sensor)
        return deps

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

        self._events_sensor_name = str(attrs.get("events_sensor") or "")

        self._bot = None
        self._meter = None
        self._events_sensor = None
        for name, resource in dependencies.items():
            if name.name == self._bot_name and isinstance(resource, Switch):
                self._bot = resource
            elif name.name == self._meter_name and isinstance(resource, Sensor):
                self._meter = resource
            elif (
                self._events_sensor_name
                and name.name == self._events_sensor_name
                and isinstance(resource, Sensor)
            ):
                self._events_sensor = resource
        if self._bot is None:
            raise RuntimeError(f"Switch dependency {self._bot_name!r} not found")
        if self._meter is None:
            raise RuntimeError(f"Sensor dependency {self._meter_name!r} not found")
        if self._events_sensor_name and self._events_sensor is None:
            LOGGER.warning(
                "events_sensor %r not found among dependencies; events will not be pushed",
                self._events_sensor_name,
            )

        self._state = self._load_and_migrate_state(attrs)

        self._state_lock = asyncio.Lock()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._bg_task = None

    def _load_and_migrate_state(self, attrs: dict) -> dict:
        loaded = self._load_state()
        if "automations" in loaded:
            state = loaded
        elif loaded:
            # v0.0.5-shaped state file — migrate to list form.
            state = _migrate_legacy_state(loaded)
        else:
            state = _empty_state()

        if not state.get("automations"):
            # No automations persisted yet; seed from config.
            state["automations"] = self._seed_automations_from_config(attrs)

        return state

    def _seed_automations_from_config(self, attrs: dict) -> list[dict]:
        raw = attrs.get("automations")
        if isinstance(raw, list):
            out = []
            for entry in raw:
                try:
                    out.append(_normalize_automation(entry))
                except ValueError as e:
                    LOGGER.warning("skipping bad config automation: %s", e)
            return out
        # Legacy single-automation config: build one "Default" from top-level.
        if "on_temp_c" in attrs and "off_temp_c" in attrs:
            try:
                return [
                    _normalize_automation(
                        {
                            "name": "Default",
                            "enabled": attrs.get("enabled", True),
                            "on_temp_c": attrs["on_temp_c"],
                            "off_temp_c": attrs["off_temp_c"],
                            "active_start": attrs.get("active_start"),
                            "active_end": attrs.get("active_end"),
                        }
                    )
                ]
            except ValueError as e:
                LOGGER.warning("legacy config automation invalid: %s", e)
        return []

    # ------------------------------------------------------------------
    # State persistence

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load state from %s: %s", path, e)
            return {}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _find_automation(self, aid: str) -> dict | None:
        for auto in self._state.get("automations", []):
            if auto.get("id") == aid:
                return auto
        return None

    def _active_hysteresis(self, now_local: datetime) -> dict | None:
        for auto in self._state.get("automations", []):
            if (auto.get("kind") or "hysteresis") != "hysteresis":
                continue
            if not auto.get("enabled"):
                continue
            days = auto.get("days_of_week") or []
            if days and now_local.weekday() not in days:
                continue
            start = _parse_hhmm(auto.get("active_start"))
            end = _parse_hhmm(auto.get("active_end"))
            if _within_active_window(now_local, start, end):
                return auto
        return None

    def _within_cooldown(self, now_utc: datetime) -> bool:
        last_iso = self._state.get("last_action_at")
        if not last_iso:
            return False
        try:
            last_dt = datetime.fromisoformat(last_iso)
        except ValueError:
            return False
        return (now_utc - last_dt).total_seconds() < self._cooldown_sec

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
            now_local = datetime.now().astimezone()

            if await self._fire_due_scheduled(now_local):
                return

            if self._within_cooldown(datetime.now(UTC)):
                return

            active = self._active_hysteresis(now_local)
            if active is None:
                return

            readings = await self._meter.get_readings()
            temp_c = None
            if isinstance(readings, dict):
                temp_c = readings.get("temperature_c") or readings.get("temperature")
            if not isinstance(temp_c, int | float):
                return

            on_temp = float(active["on_temp_c"])
            off_temp = float(active["off_temp_c"])
            position = await self._bot.get_position()

            if _mode_from_thresholds(on_temp, off_temp) == "cooling":
                should_turn_on = temp_c >= on_temp
                should_turn_off = temp_c <= off_temp
            else:
                should_turn_on = temp_c <= on_temp
                should_turn_off = temp_c >= off_temp

            if should_turn_on and position == 0:
                await self._bot.set_position(1)
                self._record_action(1)
            elif should_turn_off and position == 1:
                await self._bot.set_position(0)
                self._record_action(0)

    async def _fire_due_scheduled(self, now_local: datetime) -> bool:
        """Fire any scheduled automation whose moment has arrived today.

        Returns True if a press happened (bot needed to change position).
        Even when the bot was already at the target position, `last_fired_at`
        is stamped so we don't re-consider the same schedule until tomorrow.
        """
        now_utc = datetime.now(UTC)
        for auto in self._state.get("automations", []):
            if auto.get("kind") != "scheduled":
                continue
            if not auto.get("enabled"):
                continue
            days = auto.get("days_of_week") or []
            if days and now_local.weekday() not in days:
                continue
            t = _parse_hhmm(auto.get("time"))
            if t is None:
                continue
            fire_today = now_local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if now_local < fire_today:
                continue
            last_iso = auto.get("last_fired_at")
            if last_iso:
                try:
                    last = datetime.fromisoformat(last_iso).astimezone(now_local.tzinfo)
                    if last >= fire_today:
                        continue
                except ValueError:
                    pass
            target = 1 if auto["action"] == "on" else 0
            pressed = False
            try:
                position = await self._bot.get_position()
                if position != target:
                    if self._within_cooldown(now_utc):
                        continue
                    await self._bot.set_position(target)
                    self._record_action(target)
                    pressed = True
            except Exception as e:
                LOGGER.warning("scheduled automation %s fire failed: %s", auto.get("id"), e)
                continue
            auto["last_fired_at"] = now_utc.isoformat()
            self._save_state()
            if pressed:
                return True
        return False

    def _record_action(self, position: int) -> None:
        at = datetime.now(UTC).isoformat()
        self._state["last_action_at"] = at
        self._state["last_action_position"] = position
        self._save_state()
        # Fire-and-forget so the tick loop stays snappy even if the sensor is slow.
        asyncio.create_task(
            self._push_event(
                {
                    "event_type": "thermostat_on" if position == 1 else "thermostat_off",
                    "source": self.name,
                    "at": at,
                    "bot_position": position,
                }
            )
        )

    async def _push_event(self, event: dict) -> None:
        if self._events_sensor is None:
            return
        try:
            await self._events_sensor.do_command({"command": "push_event", "event": event})
        except Exception as e:
            LOGGER.warning("push_event failed: %s", e)

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
        active = self._active_hysteresis(now_local)
        return {
            "automations": [_automation_view(auto) for auto in self._state.get("automations", [])],
            "active_id": active["id"] if active else None,
            "temperature_c": temp_c,
            "humidity_pct": humidity,
            "bot_position": position,
            "last_action_at": self._state.get("last_action_at"),
            "last_action_position": self._state.get("last_action_position"),
        }

    async def _add_automation(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("automation payload must be an object")
        # Drop any client-supplied id so we always mint a fresh one.
        payload = {**payload, "id": _new_id()}
        automation = _normalize_automation(payload)
        assert self._state_lock is not None
        async with self._state_lock:
            self._state["automations"].append(automation)
            self._save_state()
        return {"ok": True, "automation": automation}

    async def _update_automation(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        assert self._state_lock is not None
        async with self._state_lock:
            existing = self._find_automation(str(payload["id"]))
            if existing is None:
                raise ValueError(f"no automation with id={payload['id']!r}")
            merged = {**existing, **{k: v for k, v in payload.items() if v is not None}}
            normalized = _normalize_automation(merged)
            # Preserve id + position in the list.
            normalized["id"] = existing["id"]
            for i, auto in enumerate(self._state["automations"]):
                if auto["id"] == existing["id"]:
                    self._state["automations"][i] = normalized
                    break
            self._save_state()
        return {"ok": True, "automation": normalized}

    async def _delete_automation(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        aid = str(payload["id"])
        assert self._state_lock is not None
        async with self._state_lock:
            before = len(self._state["automations"])
            self._state["automations"] = [
                a for a in self._state["automations"] if a.get("id") != aid
            ]
            if len(self._state["automations"]) == before:
                raise ValueError(f"no automation with id={aid!r}")
            self._save_state()
        return {"ok": True, "id": aid}

    async def _set_automation_enabled(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        aid = str(payload["id"])
        enabled = bool(payload.get("enabled"))
        assert self._state_lock is not None
        async with self._state_lock:
            auto = self._find_automation(aid)
            if auto is None:
                raise ValueError(f"no automation with id={aid!r}")
            auto["enabled"] = enabled
            self._save_state()
        return {"ok": True, "id": aid, "enabled": enabled}

    async def _reorder_automations(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
            raise ValueError("`ids` must be a list of automation ids")
        wanted = [str(x) for x in payload["ids"]]
        assert self._state_lock is not None
        async with self._state_lock:
            current = {a["id"]: a for a in self._state["automations"]}
            if set(wanted) != set(current.keys()):
                raise ValueError("`ids` must include every existing automation exactly once")
            self._state["automations"] = [current[aid] for aid in wanted]
            self._save_state()
        return {"ok": True, "order": wanted}

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
        if verb == "add_automation":
            return await self._add_automation(command.get("automation") or command)
        if verb == "update_automation":
            return await self._update_automation(command.get("automation") or command)
        if verb == "delete_automation":
            return await self._delete_automation(command)
        if verb == "set_automation_enabled":
            return await self._set_automation_enabled(command)
        if verb == "reorder_automations":
            return await self._reorder_automations(command)
        raise ValueError(f"Unknown command: {verb!r}")
