#!/usr/bin/env bash
# Viam module entrypoint. Bootstraps a venv on first run, then execs the
# module server. Runs from the extracted tarball's own directory.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install .
fi

exec ./.venv/bin/python -m switchbot_module.main "$@"
