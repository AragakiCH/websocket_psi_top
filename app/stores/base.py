# -*- coding: utf-8 -*-
"""
base.py
=======
Utilidades comunes a todos los almacenes en disco:

  * `escribir_json_atomico`: `.tmp` + `replace`, nunca queda un fichero a
    medias aunque se vaya la luz a mitad de escritura.
  * `leer_json_tolerante`: un JSON corrupto NO impide arrancar el servicio;
    se avisa en el log y se arranca con el valor por defecto.
  * `Cifrador`: Fernet con una clave compartida en `datos/.clave`, para que
    ninguna contraseña (usuario OPC UA, por ejemplo) quede en claro en un JSON.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("stores")

PREFIJO_CIFRADO = "enc:"


def escribir_json_atomico(ruta: Path, datos: Any, indent: int = 2) -> None:
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    tmp.write_text(json.dumps(datos, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ruta)


def leer_json_tolerante(ruta: Path, por_defecto: Any) -> Any:
    if not ruta.is_file():
        return por_defecto
    try:
        return json.loads(ruta.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("%s ilegible (%s); se usa el valor por defecto.", ruta.name, exc)
        return por_defecto


def cargar_o_crear_clave(ruta_clave: Path) -> bytes:
    """Lee la clave Fernet; si no existe, la genera y restringe sus permisos."""
    if ruta_clave.is_file():
        return ruta_clave.read_bytes().strip()
    ruta_clave.parent.mkdir(parents=True, exist_ok=True)
    clave = Fernet.generate_key()
    ruta_clave.write_bytes(clave)
    try:
        os.chmod(ruta_clave, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    logger.info("Clave de cifrado creada en %s", ruta_clave)
    return clave


class Cifrador:
    """Cifra/descifra textos con la clave compartida de la carpeta de datos."""

    def __init__(self, carpeta: Path) -> None:
        self._fernet = Fernet(cargar_o_crear_clave(carpeta / ".clave"))

    def cifrar(self, texto: str) -> str:
        if not texto:
            return ""
        return PREFIJO_CIFRADO + self._fernet.encrypt(texto.encode()).decode()

    def descifrar(self, valor: str) -> str:
        if not valor:
            return ""
        if not valor.startswith(PREFIJO_CIFRADO):
            return valor  # alguien lo puso en claro a mano
        try:
            return self._fernet.decrypt(valor[len(PREFIJO_CIFRADO):].encode()).decode()
        except InvalidToken:
            logger.error("No se pudo descifrar un valor: la clave no corresponde. "
                         "¿Se copió el JSON sin su .clave?")
            return ""
