# switchbot

Viam modular components for [SwitchBot](https://www.switch-bot.com/) devices, via the SwitchBot OpenAPI v1.1.

Models in this module:

| Model | API | Device |
|---|---|---|
| `viam:switchbot:bot` | `rdk:component:switch` | SwitchBot Bot (button pusher) |
| `viam:switchbot:curtain` | `rdk:component:generic` | SwitchBot Curtain (2/3) |
| `viam:switchbot:meter` | `rdk:component:sensor` | SwitchBot Meter, Meter Plus, and Hub 2's built-in sensor |

Cloud control of Bot and Curtain requires a paired **SwitchBot Hub** (Hub Mini, Hub 2, or Hub 3) with Cloud Services enabled.

## Getting a token and secret

Every model needs a token and secret from your SwitchBot account:

1. Open the SwitchBot app (v6.14 or later)
2. Profile → Preferences → About
3. Tap **App Version** ten times → Developer Options appears
4. Copy the Token and the Secret

These credentials cover every device on your account. SwitchBot enforces a per-token limit of **10,000 API requests per day** — don't set a high `data_capture_rate_hz` on the meter or you'll blow through it.

## Configuration

Each component takes the same three attributes:

```json
{
  "token": "...",
  "secret": "...",
  "device_id": "E2F6032048AB"
}
```

`device_id` is the SwitchBot device ID. You can find it in the app under a device's settings, or with `GET /v1.1/devices` if you'd rather list them programmatically.

### Bot

Example machine config entry:

```json
{
  "name": "ac_button",
  "namespace": "rdk",
  "type": "switch",
  "model": "viam:switchbot:bot",
  "attributes": {
    "token": "...",
    "secret": "...",
    "device_id": "..."
  }
}
```

Position semantics: `set_position(1)` sends `turnOn`, `set_position(0)` sends `turnOff`. `get_position()` polls the current on/off state.

### Curtain

Uses `rdk:component:generic` — control it through `DoCommand`:

| `command` | Extra fields | Effect |
|---|---|---|
| `open` | — | Curtain fully opens |
| `close` | — | Curtain fully closes |
| `pause` | — | Stops mid-travel |
| `set_position` | `position` (0-100) | Move to percentage (0 = fully open, 100 = fully closed) |
| `get_status` | — | Returns raw SwitchBot status body (`slidePosition`, `battery`, …) |

### Meter

Readings shape:

```json
{
  "temperature_c": 22.5,
  "humidity_pct": 50,
  "battery_pct": 87
}
```

Works for the standalone Meter, Meter Plus, and the sensor built into the Hub 2 — all three use the same `/status` response shape.

### Thermostat

Higher-level automation: reads a Meter, presses a Bot when temperature crosses configured thresholds. Runs on the Pi in a background loop so it works 24/7, regardless of whether the dashboard is open.

Direction is inferred from the relative position of the two thresholds — the same code drives cooling (A/C) and heating (heat pump / space heater).

- **Cooling** (`on_temp_c` > `off_temp_c`): press Bot on when temp rises above `on_temp_c`; off when it falls below `off_temp_c`. Example: `on=25, off=22`.
- **Heating** (`on_temp_c` < `off_temp_c`): press Bot on when temp falls below `on_temp_c`; off when it rises above `off_temp_c`. Example: `on=18, off=21`.
- Between the two thresholds, do nothing (hysteresis prevents rapid on/off).

Config (A/C example):

```json
{
  "name": "thermostat",
  "type": "generic",
  "model": "viam:switchbot:thermostat",
  "depends_on": ["ac_bot", "room_meter"],
  "attributes": {
    "bot_name": "ac_bot",
    "meter_name": "room_meter",
    "on_temp_c": 25,
    "off_temp_c": 22,
    "active_start": "07:00",
    "active_end": "22:00",
    "enabled": true,
    "poll_interval_sec": 60,
    "cooldown_sec": 300
  }
}
```

- `bot_name` / `meter_name` — resource names of the dependencies (also listed in `depends_on`).
- `on_temp_c` / `off_temp_c` — Celsius. Must differ; relative order determines direction (cooling vs heating).
- `active_start` / `active_end` — 24-hour `HH:MM` window when the controller acts. Both blank = always active. `start > end` wraps midnight (e.g. `22:00` → `06:00`).
- `enabled` — initial master switch state. Can be toggled at runtime via `do_command`.
- `poll_interval_sec` (default 60) — how often the loop wakes to check.
- `cooldown_sec` (default 300) — minimum interval between two actions, so an oscillating temperature doesn't cause rapid pressing.

Runtime state (enabled, thresholds, active hours, last-action metadata) is persisted to `~/.viam/switchbot-thermostat-<name>-state.json`. Runtime changes override the config values on load.

#### Commands

`status` — reads meter + bot and returns the full state:

```json
{ "command": "status" }
```

Response:
```json
{
  "enabled": true,
  "on_temp_c": 25.0,
  "off_temp_c": 22.0,
  "mode": "cooling",
  "active_start": "07:00",
  "active_end": "22:00",
  "within_active_window": true,
  "temperature_c": 23.4,
  "humidity_pct": 48,
  "bot_position": 1,
  "last_action_at": "2026-09-17T18:03:00+00:00",
  "last_action_position": 1
}
```

`mode` is inferred: `"cooling"` if `on_temp_c > off_temp_c`, `"heating"` otherwise.

`set_enabled` — master on/off:
```json
{ "command": "set_enabled", "enabled": false }
```

`set_thresholds` — hot-update the on/off temperatures. Swapping which is higher switches modes.
```json
{ "command": "set_thresholds", "on_c": 25, "off_c": 21 }
```

`set_active_hours` — hot-update the active window (both empty = always active):
```json
{ "command": "set_active_hours", "start": "08:00", "end": "23:00" }
```

## Development

```bash
make setup    # create venv, install deps
make lint     # ruff + black --check
make test     # pytest
make package  # build module.tar.gz locally (same shape CI ships)
```

Releases happen automatically on merge to `main`: patch-bumps the tag, tars the module, uploads to the Viam registry. Requires `viam_key_id` and `viam_key_value` GitHub secrets on the repo.
