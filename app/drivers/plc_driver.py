# -*- coding: utf-8 -*-
"""
plc_driver.py
=============
Interfaz abstracta (ABC) para cualquier driver de PLC.

El objetivo de esta capa es DESACOPLAR el resto de la aplicación (API,
WebSocket, SubscriptionHandler, PlcManager) del protocolo concreto con el
que se habla con el PLC. Hoy hay dos implementaciones Siemens:

  * `OpcUaDriver`   -> OPC UA (asyncua). S7-1500 y S7-1200 con FW >= 4.4.
  * `S7CommDriver`  -> S7comm / ISO-on-TCP, puerto 102 (python-snap7).
                       S7-1200 de cualquier FW, S7-300/400, ET 200SP.

Añadir un tercer protocolo (Modbus TCP, por ejemplo) es implementar esta
misma interfaz y registrarlo en `app/drivers/registry.py`; nada más del
sistema necesita tocarse.

Todos los métodos de I/O son asíncronos (async/await): el pipeline entero,
desde la lectura del PLC hasta el envío por WebSocket, es no bloqueante.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional


@dataclass
class TagInfo:
    """
    Descripción de un tag conocido en el PLC.

    Atributos:
        name:      Nombre corto del tag (ej. "temperatura").
        full_name: Nombre calificado incluyendo el DB (ej. "DB_Datos.temperatura").
                   Es el identificador que usa TODO el resto del sistema.
        node_id:   Identificador en el protocolo: NodeId OPC UA
                   (`ns=3;s="DB_Datos"."temperatura"`) o dirección absoluta
                   S7 (`DB1.DBD0`, `DB1.DBX4.0`).
        data_type: Tipo de dato legible ("Float", "Boolean", "Int16", "REAL"…).
        db_name:   Data Block al que pertenece.
    """

    name: str
    full_name: str
    node_id: str
    data_type: str
    db_name: str = ""


@dataclass
class TagValue:
    """Valor puntual de un tag en un instante dado (snapshot o cambio)."""

    tag: str                      # full_name del tag
    value: object                 # valor ya convertido a tipo Python serializable
    data_type: str                # tipo de dato legible
    timestamp: str                # ISO 8601 (UTC): cuándo lo recibió el backend
    node_id: str = ""
    # Cuándo cambió realmente el valor en el PLC (SourceTimestamp OPC UA).
    source_ts: str = ""
    # Marca de tiempo del servidor OPC UA (ServerTimestamp).
    server_ts: str = ""
    # Milisegundos desde el cambio anterior de ESTE tag. None en el primero.
    delta_ms: Optional[float] = None


# Firma del callback que recibe los cambios de valor desde el driver.
DataChangeCallback = Callable[[TagValue], Awaitable[None]]


class PlcDriver(ABC):
    """Contrato que debe cumplir cualquier driver de PLC."""

    #: Modelo de la CPU leído del PLC ("CPU 1214C DC/DC/Rly"). "" si no se pudo.
    modelo: str = ""
    #: Cómo llegan los datos: "subscription" (OPC UA) o "polling" (S7comm).
    modo_lectura: str = "subscription"

    @abstractmethod
    async def connect(self) -> None:
        """Establece la conexión/sesión con el PLC. Lanza excepción si falla."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Cierra limpiamente subscriptions y la sesión con el PLC."""
        raise NotImplementedError

    @abstractmethod
    async def browse_tags(self) -> List[TagInfo]:
        """
        Devuelve la lista de tags disponibles. En OPC UA se descubren solos;
        en S7comm salen de la lista que configuró el usuario.
        """
        raise NotImplementedError

    @abstractmethod
    async def subscribe(self, tags: List[TagInfo], callback: DataChangeCallback) -> None:
        """
        Empieza a entregar cambios de valor de `tags` invocando `callback`.
        OPC UA usa subscriptions nativas; S7comm hace polling emitiendo solo
        lo que cambia. Para quien llama es indistinguible.
        """
        raise NotImplementedError

    @abstractmethod
    async def read_tag(self, node_id: str) -> TagValue:
        """Lee de forma puntual (one-shot) el valor actual de un tag."""
        raise NotImplementedError

    @abstractmethod
    def is_connected(self) -> bool:
        """True si la conexión con el PLC está activa."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Métodos con implementación por defecto (opcionales para un driver)
    # ------------------------------------------------------------------ #
    async def read_tags(self, node_ids: List[str]) -> List[Optional[TagValue]]:
        """
        Lee de golpe varios tags. Devuelve una lista ALINEADA con `node_ids`:
        `None` en la posición de un tag que no se pudo leer. Lo usa el
        SubscriptionHandler para reconciliar el snapshot con el PLC.

        Esta versión lee uno a uno; los drivers reales la sobreescriben con
        una petición en bloque.
        """
        salida: List[Optional[TagValue]] = []
        for node_id in node_ids:
            try:
                salida.append(await self.read_tag(node_id))
            except Exception:  # noqa: BLE001
                salida.append(None)
        return salida

    async def check_alive(self) -> bool:
        """
        Watchdog: ¿sigue viva la sesión? Por defecto se fía de `is_connected`.
        Un driver real debería hacer una petición ligera al PLC.
        """
        return self.is_connected()

    def subscription_viva(self) -> bool:
        """
        ¿La subscription sigue entregando datos, hasta donde el driver sabe?
        OPC UA puede dar por caducada una subscription dejando la sesión
        abierta; el driver que reciba ese aviso devuelve False y el handler
        la recrea sin tirar la conexión.
        """
        return True

    async def write_tag(self, node_id: str, valor: object) -> TagValue:
        """
        Escribe un valor en un tag y DEVUELVE LO QUE QUEDÓ, releyéndolo.

        No es abstracto a propósito: un driver nuevo puede existir siendo
        solo de lectura. Quien no la implemente hereda este error.
        """
        raise NotImplementedError(
            f"El driver {type(self).__name__} no implementa escritura. "
            f"Solo puede leer tags."
        )

    def soporta_escritura(self) -> bool:
        """¿Este driver sabe escribir? Se mira si `write_tag` fue sobreescrito."""
        return type(self).write_tag is not PlcDriver.write_tag
