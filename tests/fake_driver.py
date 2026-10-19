# -*- coding: utf-8 -*-
"""
Driver falso para las pruebas de la API: cumple `PlcDriver` sin red.
Expone dos tags y permite forzar cambios con `FakeDriver.ultimo.simular(...)`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.drivers.escritura import convertir
from app.drivers.plc_driver import DataChangeCallback, PlcDriver, TagInfo, TagValue

TAGS = [
    TagInfo(name="temperatura", full_name="DB_Datos.temperatura",
            node_id='ns=3;s="DB_Datos"."temperatura"', data_type="Float", db_name="DB_Datos"),
    TagInfo(name="marcha", full_name="DB_Datos.marcha",
            node_id='ns=3;s="DB_Datos"."marcha"', data_type="Boolean", db_name="DB_Datos"),
    TagInfo(name="estado", full_name="DB_Datos.estado",
            node_id='ns=3;s="DB_Datos"."estado"', data_type="ExtensionObject", db_name="DB_Datos"),
]


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeDriver(PlcDriver):
    ultimo: Optional["FakeDriver"] = None
    fallar_conexion: bool = False

    def __init__(self, settings) -> None:
        self.settings = settings
        self.valores: Dict[str, object] = {TAGS[0].node_id: 20.0, TAGS[1].node_id: False,
                                           TAGS[2].node_id: None}
        self._callback: Optional[DataChangeCallback] = None
        self._connected = False
        self.modelo = "CPU 1516-3 PN/DP (fake)"
        self.escrituras: List[tuple] = []
        self.rechazar: set = set()        # full_names cuya escritura "rechaza el PLC"
        FakeDriver.ultimo = self

    async def connect(self) -> None:
        if FakeDriver.fallar_conexion:
            raise ConnectionError("fake: conexión rechazada")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def browse_tags(self) -> List[TagInfo]:
        return list(TAGS)

    async def subscribe(self, tags, callback) -> None:
        self._callback = callback

    def _tv(self, node_id: str) -> TagValue:
        info = next(t for t in TAGS if t.node_id == node_id)
        return TagValue(tag=info.full_name, value=self.valores[node_id], data_type=info.data_type,
                        timestamp=_ahora(), node_id=node_id, source_ts=_ahora())

    async def read_tag(self, node_id: str) -> TagValue:
        if node_id not in self.valores:
            raise KeyError(node_id)
        return self._tv(node_id)

    async def read_tags(self, node_ids):
        return [self._tv(n) if n in self.valores else None for n in node_ids]

    async def write_tag(self, node_id: str, valor: object) -> TagValue:
        info = next(t for t in TAGS if t.node_id == node_id)
        if info.full_name == "DB_Datos.estado" or info.full_name in self.rechazar:
            raise PermissionError("fake: BadNotWritable")
        self.valores[node_id] = convertir(valor, info.data_type)
        self.escrituras.append((node_id, self.valores[node_id]))
        return self._tv(node_id)

    def is_connected(self) -> bool:
        return self._connected

    async def simular(self, node_id: str, valor: object) -> None:
        """Simula un cambio llegado del PLC."""
        self.valores[node_id] = valor
        if self._callback:
            await self._callback(self._tv(node_id))
