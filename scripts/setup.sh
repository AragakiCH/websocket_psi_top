#!/usr/bin/env bash
# ==========================================================================
#  PSI Top · backend Siemens — crea el entorno virtual e instala todo (Linux/macOS)
#  Uso:  scripts/setup.sh          |   scripts/setup.sh dev
# ==========================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "[ERROR] No se encontró $PY. Instala Python 3.10-3.13."; exit 1; }
echo "[1/4] Python detectado: $($PY --version)"

if [ ! -d venv ]; then
    echo "[2/4] Creando entorno virtual en ./venv ..."
    "$PY" -m venv venv
else
    echo "[2/4] El entorno virtual ./venv ya existe; se reutiliza."
fi

echo "[3/4] Instalando dependencias ..."
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null
if [ "${1:-}" = "dev" ]; then
    pip install -r requirements-dev.txt
else
    pip install -r requirements.txt
fi

echo "[4/4] Preparando configuración ..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "      Creado .env a partir de .env.example. Edita PLC_API_KEY antes de usarlo en planta."
fi
mkdir -p datos

cat <<MSG

Listo. Para arrancar el servicio:
    scripts/run.sh
o bien:
    source venv/bin/activate && python run.py
Documentación interactiva: http://localhost:8000/docs
MSG
