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

Position semantics: `set_position(1)` sends `turnOn`, `set_position(0)` sends `turnOff`.

`get_position()` returns the **last position we commanded**, persisted to `~/.viam/switchbot-bot-<name>-state.json`. This is intentional — SwitchBot's cloud `power` field for a Bot in "Press mode" is unreliable, and callers making decisions on `get_position` (Thermostat controller, dashboard) were seeing garbage. Persisted state is authoritative once we've commanded at least once; on first-ever call we fall back to SwitchBot's report as a seed.

Diverges if someone presses the physical Bot or toggles via the SwitchBot app — we don't reconcile automatically. A subsequent `set_position` from any caller overwrites the local state.

Extra `do_command` verbs:

| `command` | Effect |
|---|---|
| `state` | Returns `{ position, last_set_at, last_set_position }` from the state file |
| `resync` | Force-fetch from SwitchBot and overwrite the state file (use after out-of-band changes) |

### Curtain

Uses `rdk:component:generic` — control it through `DoCommand`.

Manual verbs:

| `command` | Extra fields | Effect |
|---|---|---|
| `open` | — | Curtain fully opens |
| `close` | — | Curtain fully closes |
| `pause` | — | Stops mid-travel |
| `set_position` | `position` (0-100) | Move to percentage (0 = fully open, 100 = fully closed) |
| `status` (alias `get_status`) | — | Returns `{ slide_position, battery, moving, calibrate, schedules, raw }` |

Optional config attributes:

```json
{
  "poll_interval_sec": 30,
  "schedules": [
    {
      "name": "Morning open",
      "action": "open",
      "time": "07:30",
      "days_of_week": [0, 1, 2, 3, 4],
      "enabled": true
    }
  ]
}
```

- `poll_interval_sec` (default 30) — how often the background loop checks if any schedule is due.
- `schedules` — optional seed list; only used when the state file is empty. Runtime edits via `do_command` become the source of truth. Runtime state persists to `~/.viam/switchbot-curtain-<name>-state.json`.

Each schedule:
- `id` — assigned automatically.
- `name` — display label.
- `action` — `open`, `close`, or `position`.
- `position` — required only when `action` is `position` (0-100, SwitchBot semantics).
- `time` — `HH:MM` local time.
- `days_of_week` — list of ints 0..6 (Mon..Sun). Empty = every day.
- `enabled` — boolean.

The background loop fires each schedule at most once per day: it compares `last_fired_at` to today's scheduled moment and only fires when today's window has arrived and hasn't been claimed yet.

Scheduling verbs:

`add_schedule`:
```json
{
  "command": "add_schedule",
  "schedule": {
    "name": "Bedtime close",
    "action": "close",
    "time": "22:00",
    "days_of_week": [],
    "enabled": true
  }
}
```

`update_schedule` (any field except `id`):
```json
{
  "command": "update_schedule",
  "schedule": { "id": "a1b2c3d4", "time": "22:30" }
}
```

`delete_schedule`:
```json
{ "command": "delete_schedule", "id": "a1b2c3d4" }
```

`set_schedule_enabled`:
```json
{ "command": "set_schedule_enabled", "id": "a1b2c3d4", "enabled": false }
```

`reorder_schedules` — `ids` must include every existing schedule exactly once:
```json
{ "command": "reorder_schedules", "ids": ["a1b2c3d4", "e5f6g7h8"] }
```

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

Higher-level automation: reads a Meter, presses a Bot when temperature crosses configured thresholds. Holds an **ordered list of named automations** — the loop picks the first one that's enabled and inside its active-hours window, and uses its thresholds. Lets you define e.g. a "Day" and "Night" automation with different setpoints. Runs on the Pi so it works 24/7 regardless of the dashboard.

Threshold direction is inferred per-automation from the relative position of the two values:

- **Cooling** (`on_temp_c` > `off_temp_c`): press Bot on when temp reaches `on_temp_c` or higher; off when it reaches `off_temp_c` or lower. Example: `on=25, off=22`.
- **Heating** (`on_temp_c` < `off_temp_c`): press Bot on when temp reaches `on_temp_c` or lower; off when it reaches `off_temp_c` or higher. Example: `on=18, off=21`.
- Thresholds are inclusive so hitting the setpoint exactly triggers a press. Between the two thresholds, nothing happens (hysteresis).

Config:

```json
{
  "name": "thermostat",
  "type": "generic",
  "model": "viam:switchbot:thermostat",
  "depends_on": ["ac_bot", "room_meter"],
  "attributes": {
    "bot_name": "ac_bot",
    "meter_name": "room_meter",
    "automations": [
      {
        "name": "Day",
        "on_temp_c": 25,
        "off_temp_c": 22,
        "active_start": "07:00",
        "active_end": "22:00"
      },
      {
        "name": "Night",
        "on_temp_c": 26,
        "off_temp_c": 24,
        "active_start": "22:00",
        "active_end": "07:00"
      }
    ],
    "poll_interval_sec": 60,
    "cooldown_sec": 300
  }
}
```

- `bot_name` / `meter_name` — resource names of the dependencies (also listed in `depends_on`).
- `automations` — ordered list. First automation that's enabled AND currently in its active window wins.
- Each automation: `{name, on_temp_c, off_temp_c, active_start?, active_end?, enabled?}`. Both `active_start` and `active_end` blank = always active. `start > end` wraps midnight.
- `poll_interval_sec` (default 60) — how often the loop wakes to check.
- `cooldown_sec` (default 300) — minimum interval between two Bot presses so an oscillating temperature doesn't cause rapid pressing.
- Legacy config with top-level `on_temp_c` / `off_temp_c` / `active_start` / `active_end` is auto-migrated to a single automation named "Default" on first load.

Runtime state (the automations list, their enabled flags, their order, plus last-action metadata) is persisted to `~/.viam/switchbot-thermostat-<name>-state.json`. Runtime edits via `do_command` become the source of truth; config values only seed an empty state file on first run.

#### Commands

`status` — reads meter + bot and returns the full state, including every automation:

```json
{ "command": "status" }
```

Response:
```json
{
  "automations": [
    {
      "id": "a1b2c3d4",
      "name": "Day",
      "enabled": true,
      "on_temp_c": 25.0,
      "off_temp_c": 22.0,
      "active_start": "07:00",
      "active_end": "22:00",
      "mode": "cooling"
    }
  ],
  "active_id": "a1b2c3d4",
  "temperature_c": 23.4,
  "humidity_pct": 48,
  "bot_position": 1,
  "last_action_at": "2026-09-17T18:03:00+00:00",
  "last_action_position": 1
}
```

`add_automation` — appends a new automation:
```json
{
  "command": "add_automation",
  "automation": {
    "name": "Night",
    "on_temp_c": 26,
    "off_temp_c": 24,
    "active_start": "22:00",
    "active_end": "07:00",
    "enabled": true
  }
}
```

`update_automation` — edits an existing automation (any field except `id`):
```json
{
  "command": "update_automation",
  "automation": { "id": "a1b2c3d4", "on_temp_c": 27 }
}
```

`delete_automation` — removes by id:
```json
{ "command": "delete_automation", "id": "a1b2c3d4" }
```

`set_automation_enabled` — per-automation on/off:
```json
{ "command": "set_automation_enabled", "id": "a1b2c3d4", "enabled": false }
```

`reorder_automations` — set the precedence order. `ids` must include every existing automation exactly once:
```json
{ "command": "reorder_automations", "ids": ["a1b2c3d4", "e5f6g7h8"] }
```

## Development

```bash
make setup    # create venv, install deps
make lint     # ruff + black --check
make test     # pytest
make package  # build module.tar.gz locally (same shape CI ships)
```

Releases happen automatically on merge to `main`: patch-bumps the tag, tars the module, uploads to the Viam registry. Requires `viam_key_id` and `viam_key_value` GitHub secrets on the repo.
