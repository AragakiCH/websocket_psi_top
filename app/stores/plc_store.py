# -*- coding: utf-8 -*-
"""
plc_store.py
============
Persistencia de la lista de PLCs dados de alta (`datos/plcs.json`).

Sin esto, los PLCs viven solo en memoria y un reinicio del servicio obliga a
darlos de alta otra vez. Se guarda la CONFIGURACIÓN (endpoint, transporte,
rack/slot, lista de tags S7), nunca el estado de ejecución (conectado,
número de tags, último error), que se recalcula al reconectar.

Las contraseñas se cifran con la clave Fernet de `datos/.clave`.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List

from app.core.endpoint import EndpointPlc
from app.stores.base import Cifrador, escribir_json_atomico, leer_json_tolerante

logger = logging.getLogger("plc_store")

CAMPOS = ("endpoint", "host", "port", "nombre", "transporte", "rack", "slot",
          "s7_tags", "usuario", "password")


class PlcStore:
    def __init__(self, carpeta: Path) -> None:
        self.ruta = carpeta / "plcs.json"
        self._cifrador = Cifrador(carpeta)
        self._lock = threading.Lock()

    def cargar(self) -> List[EndpointPlc]:
        """PLCs guardados. Lista vacía si no hay fichero o está corrupto."""
        crudo = leer_json_tolerante(self.ruta, [])
        salida: List[EndpointPlc] = []
        for d in crudo if isinstance(crudo, list) else []:
            try:
                salida.append(EndpointPlc(
                    endpoint=d["endpoint"],
                    host=d.get("host", ""),
                    port=int(d.get("port") or 4840),
                    nombre=d.get("nombre", "") or "",
                    origen="manual",
                    transporte=(d.get("transporte") or "opcua").lower(),
                    rack=int(d.get("rack") or 0),
                    slot=int(d.get("slot") or 1),
                    s7_tags=d.get("s7_tags", "") or "",
                    usuario=d.get("usuario", "") or "",
                    password=self._cifrador.descifrar(d.get("password", "") or ""),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Entrada de PLC ilegible en plcs.json: %s", exc)
        if salida:
            logger.info("PlcStore: %d PLC(s) recuperado(s) del disco.", len(salida))
        return salida

    def guardar(self, endpoints: List[EndpointPlc]) -> None:
        with self._lock:
            datos = []
            for ep in endpoints:
                d = {campo: getattr(ep, campo, "") for campo in CAMPOS}
                d["password"] = self._cifrador.cifrar(ep.password or "")
                datos.append(d)
            try:
                escribir_json_atomico(self.ruta, datos)
            except Exception as exc:  # noqa: BLE001
                # No poder persistir NO debe tumbar el alta: el PLC ya está
                # conectado y funcionando en memoria.
                logger.error("No se pudo guardar plcs.json: %s", exc)
