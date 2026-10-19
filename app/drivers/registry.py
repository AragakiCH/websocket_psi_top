# -*- coding: utf-8 -*-
"""
registry.py
===========
Fábrica de drivers: dado el transporte de un PLC, devuelve la clase que lo
implementa. Es el ÚNICO sitio que conoce todas las implementaciones.

Para añadir un protocolo nuevo (Modbus TCP, por ejemplo):

    1. Implementa `PlcDriver` en `app/drivers/modbus_driver.py`.
    2. Regístralo aquí:  registrar("modbus", "app.drivers.modbus_driver:ModbusDriver")
    3. Añade sus settings a `app/config/settings.py` si los necesita.

El resto (SubscriptionHandler, PlcManager, API, WebSocket) no cambia.

Las clases se importan de forma DIFERIDA (por ruta "modulo:Clase") para que
un driver cuya dependencia no esté instalada (python-snap7, por ejemplo) no
impida arrancar el servicio: el error aparece al intentar usar ESE driver,
con un mensaje que dice qué falta.
"""
from __future__ import annotations

import importlib
from typing import Dict, Type

from app.config.settings import Settings
from app.drivers.plc_driver import PlcDriver

_REGISTRO: Dict[str, str] = {
    "opcua": "app.drivers.opcua_driver:OpcUaDriver",
    "s7comm": "app.drivers.s7comm_driver:S7CommDriver",
}


def registrar(transporte: str, ruta: str) -> None:
    """Registra un driver nuevo: `registrar("modbus", "paquete.modulo:Clase")`."""
    _REGISTRO[transporte.strip().lower()] = ruta


def transportes() -> list:
    return sorted(_REGISTRO)


def clase_driver(transporte: str) -> Type[PlcDriver]:
    clave = (transporte or "opcua").strip().lower()
    if clave not in _REGISTRO:
        raise ValueError(
            f"Transporte desconocido '{transporte}'. Disponibles: {', '.join(transportes())}.")
    modulo, _, nombre = _REGISTRO[clave].partition(":")
    return getattr(importlib.import_module(modulo), nombre)


def crear_driver(transporte: str, settings_plc: Settings) -> PlcDriver:
    """Instancia el driver que corresponde al transporte con los settings de ESE PLC."""
    return clase_driver(transporte)(settings_plc)
