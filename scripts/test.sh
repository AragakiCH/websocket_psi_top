#!/usr/bin/env bash
# Ejecuta las pruebas (requiere scripts/setup.sh dev).
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/python -m pytest -q "$@"
