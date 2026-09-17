#!/usr/bin/env bash
# Viam module entrypoint. Bootstraps a venv on first run, then execs the
# module server. Runs from the extracted tarball's own directory.
#
# The install guard checks for the *imported package*, not just the
# .venv directory. `python3 -m venv .venv` creates the venv before
# `pip install` has run, so if pip fails partway (network flake, PyPI
# hiccup, missing ARM wheel for a transitive dep) the venv is left
# behind empty. A directory-existence check would then skip the
# reinstall on every subsequent boot and viam-server would fail with
# "No module named 'switchbot_module'" forever. Checking for the
# import instead makes the bootstrap idempotent: a retry that starts
# from a partial venv finishes the install.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

if ! ./.venv/bin/python -c "import switchbot_module" 2>/dev/null; then
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install .
fi

exec ./.venv/bin/python -m switchbot_module.main "$@"
