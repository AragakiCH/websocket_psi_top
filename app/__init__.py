# -*- coding: utf-8 -*-
"""PSI Top · backend de conexión con PLCs Siemens (OPC UA + S7comm) → WebSocket."""
from pathlib import Path

try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text("utf-8").strip() or "0.0.0"
except OSError:
    __version__ = "0.0.0"
