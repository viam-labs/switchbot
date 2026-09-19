"""SwitchBot Curtain as a Viam generic component.

DoCommand verbs:
  Manual: open, close, pause, set_position (0-100), status (alias
    get_status).
  Scheduling: add_schedule, update_schedule, delete_schedule,
    set_schedule_enabled, reorder_schedules.

A background loop polls once per minute and fires any schedule that
is enabled, whose day-of-week matches today, whose time-of-day has
arrived, and that hasn't fired yet today. Schedules persist to
~/.viam/switchbot-curtain-<name>-state.json.

Position uses SwitchBot's "setPosition" with parameter "0,ff,{n}"
where n is percent 0..100 (0 = fully open, 100 = fully closed).
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
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .client import SwitchBotClient

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/switchbot-curtain-{name}-state.json"
DEFAULT_POLL_INTERVAL_SEC = 30
VALID_ACTIONS = ("open", "close", "position")


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


def _normalize_schedule(raw: dict) -> dict:
    """Coerce a schedule dict into canonical shape."""
    action = raw.get("action")
    if action not in VALID_ACTIONS:
        raise ValueError(f"`action` must be one of {VALID_ACTIONS!r}")
    position = None
    if action == "position":
        pos = raw.get("position")
        if not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos <= 100:
            raise ValueError("`position` (0..100) is required for the `position` action")
        position = pos
    time_str = raw.get("time")
    if _parse_hhmm(time_str) is None:
        raise ValueError("`time` is required (HH:MM)")
    return {
        "id": raw.get("id") or _new_id(),
        "name": str(raw.get("name") or action.capitalize()),
        "action": action,
        "position": position,
        "time": time_str,
        "days_of_week": _normalize_days(raw.get("days_of_week")),
        "enabled": bool(raw.get("enabled", True)),
        "last_fired_at": raw.get("last_fired_at"),
    }


def _empty_state() -> dict:
    return {"schedules": []}


class Curtain(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "switchbot"), "curtain")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._client: SwitchBotClient | None = None
        self._device_id: str = ""
        self._state_path: str = ""
        self._state: dict = _empty_state()
        self._state_lock: asyncio.Lock | None = None
        self._bg_task: asyncio.Task | None = None
        self._poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC

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
        schedules = attrs.get("schedules")
        if schedules is not None:
            if not isinstance(schedules, list):
                raise ValueError("`schedules` must be a list")
            for entry in schedules:
                if not isinstance(entry, dict):
                    raise ValueError("each schedule must be an object")
                _normalize_schedule(entry)
        return []

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._client = SwitchBotClient(attrs["token"], attrs["secret"])
        self._device_id = attrs["device_id"]
        self._poll_interval_sec = int(attrs.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC)
        self._state_path = str(
            attrs.get("state_path") or DEFAULT_STATE_PATH.format(name=config.name)
        )

        loaded = self._load_state()
        self._state = _empty_state()
        if isinstance(loaded.get("schedules"), list):
            self._state["schedules"] = loaded["schedules"]
        if not self._state["schedules"]:
            self._state["schedules"] = self._seed_schedules_from_config(attrs)

        self._state_lock = asyncio.Lock()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._bg_task = None

    def _seed_schedules_from_config(self, attrs: dict) -> list[dict]:
        raw = attrs.get("schedules")
        if not isinstance(raw, list):
            return []
        out = []
        for entry in raw:
            try:
                out.append(_normalize_schedule(entry))
            except ValueError as e:
                LOGGER.warning("skipping bad config schedule: %s", e)
        return out

    # ------------------------------------------------------------------
    # State persistence

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load curtain state from %s: %s", path, e)
            return {}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _find_schedule(self, sid: str) -> dict | None:
        for s in self._state.get("schedules", []):
            if s.get("id") == sid:
                return s
        return None

    # ------------------------------------------------------------------
    # Background loop

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("curtain schedule tick failed: %s", e)
            await asyncio.sleep(self._poll_interval_sec)

    async def _tick(self) -> None:
        assert self._state_lock is not None
        async with self._state_lock:
            now_local = datetime.now().astimezone()
            fired = False
            for schedule in list(self._state.get("schedules", [])):
                if not schedule.get("enabled"):
                    continue
                if not self._is_due(schedule, now_local):
                    continue
                try:
                    await self._execute_schedule(schedule)
                    schedule["last_fired_at"] = datetime.now(UTC).isoformat()
                    fired = True
                except Exception as e:
                    LOGGER.warning(
                        "curtain schedule %s fire failed: %s",
                        schedule.get("id"),
                        e,
                    )
            if fired:
                self._save_state()

    def _is_due(self, schedule: dict, now_local: datetime) -> bool:
        target_t = _parse_hhmm(schedule.get("time"))
        if target_t is None:
            return False
        dows = schedule.get("days_of_week") or []
        if dows and now_local.weekday() not in dows:
            return False
        target_today = now_local.replace(
            hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0
        )
        if now_local < target_today:
            return False
        last_fired_iso = schedule.get("last_fired_at")
        if not last_fired_iso:
            return True
        try:
            last_fired = datetime.fromisoformat(last_fired_iso)
        except ValueError:
            return True
        return last_fired < target_today.astimezone(UTC)

    async def _execute_schedule(self, schedule: dict) -> None:
        assert self._client is not None
        action = schedule["action"]
        if action == "open":
            await self._client.send_command(self._device_id, "turnOn")
        elif action == "close":
            await self._client.send_command(self._device_id, "turnOff")
        elif action == "position":
            position = int(schedule["position"])
            await self._client.send_command(
                self._device_id, "setPosition", parameter=f"0,ff,{position}"
            )

    # ------------------------------------------------------------------
    # do_command

    async def _add_schedule(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("schedule payload must be an object")
        schedule = _normalize_schedule({**payload, "id": _new_id()})
        assert self._state_lock is not None
        async with self._state_lock:
            self._state["schedules"].append(schedule)
            self._save_state()
        return {"ok": True, "schedule": schedule}

    async def _update_schedule(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        assert self._state_lock is not None
        async with self._state_lock:
            existing = self._find_schedule(str(payload["id"]))
            if existing is None:
                raise ValueError(f"no schedule with id={payload['id']!r}")
            merged = {**existing}
            for k, v in payload.items():
                if v is not None or k in ("position", "days_of_week"):
                    merged[k] = v
            normalized = _normalize_schedule(merged)
            normalized["id"] = existing["id"]
            normalized["last_fired_at"] = existing.get("last_fired_at")
            for i, s in enumerate(self._state["schedules"]):
                if s["id"] == existing["id"]:
                    self._state["schedules"][i] = normalized
                    break
            self._save_state()
        return {"ok": True, "schedule": normalized}

    async def _delete_schedule(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        sid = str(payload["id"])
        assert self._state_lock is not None
        async with self._state_lock:
            before = len(self._state["schedules"])
            self._state["schedules"] = [s for s in self._state["schedules"] if s.get("id") != sid]
            if len(self._state["schedules"]) == before:
                raise ValueError(f"no schedule with id={sid!r}")
            self._save_state()
        return {"ok": True, "id": sid}

    async def _set_schedule_enabled(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        sid = str(payload["id"])
        enabled = bool(payload.get("enabled"))
        assert self._state_lock is not None
        async with self._state_lock:
            s = self._find_schedule(sid)
            if s is None:
                raise ValueError(f"no schedule with id={sid!r}")
            s["enabled"] = enabled
            self._save_state()
        return {"ok": True, "id": sid, "enabled": enabled}

    async def _reorder_schedules(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
            raise ValueError("`ids` must be a list of schedule ids")
        wanted = [str(x) for x in payload["ids"]]
        assert self._state_lock is not None
        async with self._state_lock:
            current = {s["id"]: s for s in self._state["schedules"]}
            if set(wanted) != set(current.keys()):
                raise ValueError("`ids` must include every existing schedule exactly once")
            self._state["schedules"] = [current[sid] for sid in wanted]
            self._save_state()
        return {"ok": True, "order": wanted}

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs,
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
            return {
                "slide_position": raw.get("slidePosition"),
                "battery": raw.get("battery"),
                "moving": raw.get("moving"),
                "calibrate": raw.get("calibrate"),
                "schedules": list(self._state.get("schedules", [])),
                "raw": dict(raw),
            }
        elif verb == "add_schedule":
            return await self._add_schedule(command.get("schedule") or command)
        elif verb == "update_schedule":
            return await self._update_schedule(command.get("schedule") or command)
        elif verb == "delete_schedule":
            return await self._delete_schedule(command)
        elif verb == "set_schedule_enabled":
            return await self._set_schedule_enabled(command)
        elif verb == "reorder_schedules":
            return await self._reorder_schedules(command)
        else:
            raise ValueError(f"unknown command: {verb!r}")
        return {"ok": True, "result": dict(result)}
