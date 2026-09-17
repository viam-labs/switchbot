# switchbot

Viam modular components for [SwitchBot](https://www.switch-bot.com/) devices, via the SwitchBot OpenAPI v1.1.

Models in this module:

| Model | API | Device |
|---|---|---|
| `viam-labs:switchbot:bot` | `rdk:component:switch` | SwitchBot Bot (button pusher) |
| `viam-labs:switchbot:curtain` | `rdk:component:generic` | SwitchBot Curtain (2/3) |
| `viam-labs:switchbot:meter` | `rdk:component:sensor` | SwitchBot Meter, Meter Plus, and Hub 2's built-in sensor |

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
  "model": "viam-labs:switchbot:bot",
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

## Development

```bash
make setup    # create venv, install deps
make lint     # ruff + black --check
make test     # pytest
make package  # build module.tar.gz locally (same shape CI ships)
```

Releases happen automatically on merge to `main`: patch-bumps the tag, tars the module, uploads to the Viam registry. Requires `viam_key_id` and `viam_key_value` GitHub secrets on the repo.
