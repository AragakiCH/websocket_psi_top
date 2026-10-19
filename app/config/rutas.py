# -*- coding: utf-8 -*-
"""
rutas.py
========
Dónde vive el estado persistente de la aplicación.

En la carpeta de datos se guardan:
    plcs.json                 PLCs dados de alta (sobreviven al reinicio)
    tags_plc.json             catálogo de tags de cada PLC
    escritura_permitida.json  lista blanca de escritura
    auditoria.jsonl           quién hizo qué
    .clave                    clave Fernet con la que se cifran contraseñas

Orden de resolución:
    1. Argumento explícito (`settings.datos_dir`)
    2. Variable de entorno PLC_DATOS_DIR
    3. Por defecto: en desarrollo `<raíz del proyecto>/datos`; empaquetado
       (PyInstaller), la carpeta de datos del SISTEMA
       (`C:\\ProgramData\\PsiTop\\datos` o `/var/lib/psitop/datos`), para
       que actualizar o desinstalar no se lleve la configuración.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

APP = "PsiTop"


def raiz_proyecto() -> Path:
    """Raíz del repositorio (tres niveles por encima de este archivo)."""
    return Path(__file__).resolve().parent.parent.parent


def empaquetado() -> bool:
    """True si corre como ejecutable de PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def carpeta_por_defecto() -> Path:
    if not empaquetado():
        return raiz_proyecto() / "datos"
    if os.name == "nt":
        base = os.getenv("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / APP / "datos"
    sistema = Path("/var/lib") / APP.lower() / "datos"
    try:
        sistema.parent.mkdir(parents=True, exist_ok=True)
        prueba = sistema.parent / ".escritura"
        prueba.touch()
        prueba.unlink()
        return sistema
    except Exception:  # noqa: BLE001
        return Path.home() / ".local" / "share" / APP.lower() / "datos"


def resolver_carpeta_datos(carpeta: Optional[str] = None) -> Path:
    """Carpeta de datos definitiva, creada si no existe."""
    raiz = carpeta or os.getenv("PLC_DATOS_DIR") or str(carpeta_por_defecto())
    ruta = Path(raiz)
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def describir(carpeta: Path) -> dict:
    """Estado de la carpeta de datos (para GET /sistema)."""
    escribible = False
    try:
        prueba = carpeta / ".escritura"
        prueba.touch()
        prueba.unlink()
        escribible = True
    except Exception:  # noqa: BLE001
        pass
    ficheros = []
    try:
        for p in sorted(carpeta.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                ficheros.append({"nombre": p.name, "bytes": p.stat().st_size})
    except Exception:  # noqa: BLE001
        pass
    return {"ruta": str(carpeta), "escribible": escribible,
            "empaquetado": empaquetado(), "ficheros": ficheros}
