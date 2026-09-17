# CLAUDE.md

Notes for future Claude sessions working on this module.

## Purpose

Viam modular components wrapping the SwitchBot OpenAPI v1.1. Three models today: `bot` (switch), `curtain` (generic), `meter` (sensor). One shared `SwitchBotClient` handles auth + HTTP.

## Ground rules

- **Never test against a real SwitchBot device from CI.** Use `respx` to mock `httpx` calls. There's a 10,000 req/day/token limit and running the suite in a loop would blow it.
- **Do not commit tokens or secrets.** Credentials live in the machine's Viam config, per-component.
- **The HMAC signing lives in `src/switchbot_module/client.py` and only there.** If a new model is added, it reuses that client. Don't reimplement signing per-model.

## When adding a new model

1. New file in `src/switchbot_module/`, following the shape of `bot.py` / `curtain.py` / `meter.py`.
2. Register it in `main.py` (both `Registry.register_resource_creator` and `module.add_model_from_registry`).
3. Add it to `meta.json`'s `models` array with the right `api`.
4. Document config attributes in `README.md`.
5. Add a test file under `tests/`.

## Release flow

Merging to `main` auto-triggers `.github/workflows/release.yml`, which patch-bumps the tag, packages the module, and uploads to the Viam registry via `viamrobotics/upload-module@v1`. No manual step required beyond merging.

For explicit version bumps (minor / major), use `workflow_dispatch` with the `version` input like `v0.1.0`.

## Known quirks

- SwitchBot's docs are inconsistent about whether the HMAC signature is upper-cased. The official Python sample and working community clients use base64 without upper — that's what we do. If auth ever breaks, this is the first thing to check.
- Curtain position 0 = fully open, 100 = fully closed. The API's `setPosition` parameter format is `"mode,index,percent"` — we hard-code `"0,ff,{n}"` (performance mode, default index).
- Bot's `get_position` reads `power` from the status body. Bot devices in "press" mode (single-press only) may not report a stable power state — set them to switch mode in the app if you need `get_position` to be meaningful.
