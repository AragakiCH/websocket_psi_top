#!/usr/bin/env bash
# Arranca el backend con el venv del proyecto.   scripts/run.sh [--reload]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x venv/bin/python ] || { echo "[ERROR] No existe el venv. Ejecuta primero: scripts/setup.sh"; exit 1; }
exec venv/bin/python run.py "$@"
