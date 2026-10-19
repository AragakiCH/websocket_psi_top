# -*- coding: utf-8 -*-
"""
tags_store.py
=============
Catálogo de tags de cada PLC, guardado en `datos/tags_plc.json`.

Con el PLC apagado (o tras reiniciar el servicio sin PLC a la vista) la
lista de tags sigue disponible: los clientes pueden listar variables y
mantener sus enlaces. Lo que NO se guarda son los VALORES: un valor viejo
en una pantalla de planta es peor que ninguno, así que sin conexión se
manda `None`.

Cada browse correcto REEMPLAZA el catálogo de ese PLC. Un browse que falla
no toca nada.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List

from app.drivers.plc_driver import TagInfo
from app.stores.base import escribir_json_atomico, leer_json_tolerante

logger = logging.getLogger("tags_store")

CAMPOS = ("name", "full_name", "node_id", "data_type", "db_name")


class TagsStore:
    def __init__(self, carpeta: Path) -> None:
        self.ruta = carpeta / "tags_plc.json"
        self._lock = threading.Lock()
        crudo = leer_json_tolerante(self.ruta, {})
        self._datos: Dict[str, List[dict]] = (
            {str(k): v for k, v in crudo.items() if isinstance(v, list)}
            if isinstance(crudo, dict) else {})

    def cargar(self, plc_id: str) -> List[TagInfo]:
        salida: List[TagInfo] = []
        for d in self._datos.get(plc_id, []):
            try:
                salida.append(TagInfo(
                    name=str(d["name"]), full_name=str(d["full_name"]),
                    node_id=str(d["node_id"]), data_type=str(d.get("data_type") or ""),
                    db_name=str(d.get("db_name") or "")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tag ilegible en el catálogo de %s: %s", plc_id, exc)
        return salida

    def guardar(self, plc_id: str, tags: List[TagInfo]) -> None:
        """Reemplaza el catálogo de un PLC. No escribe si no cambió nada."""
        nuevo = [{c: getattr(t, c, "") for c in CAMPOS} for t in tags]
        with self._lock:
            if self._datos.get(plc_id) == nuevo:
                return
            self._datos[plc_id] = nuevo
            self._volcar()

    def borrar(self, plc_id: str) -> None:
        with self._lock:
            if self._datos.pop(plc_id, None) is not None:
                self._volcar()

    def _volcar(self) -> None:
        try:
            escribir_json_atomico(self.ruta, self._datos, indent=0)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo guardar tags_plc.json: %s", exc)
