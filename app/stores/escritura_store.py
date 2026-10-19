# -*- coding: utf-8 -*-
"""
escritura_store.py
==================
QUÉ tags se pueden escribir, y DENTRO DE QUÉ LÍMITES.

La regla es al revés de lo habitual: **nada es escribible hasta que alguien
lo habilita a mano**, dejando por escrito quién lo hizo y con qué límites.
Un servidor OPC UA declara escribibles muchas más variables de las que
tiene sentido tocar desde un cliente; basta un enlace equivocado para mandar
un valor a algo que no debía moverse.

Los límites `minimo`/`maximo` son la segunda red: el tipo ya lo valida
`drivers/escritura.py` (que un Int16 no desborde), pero que un valor quepa
en un Int16 no significa que la máquina lo admita.

Fichero: `datos/escritura_permitida.json`.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.stores.base import escribir_json_atomico, leer_json_tolerante

logger = logging.getLogger("escritura_store")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TagEscribible:
    plc_id: str
    tag: str                      # full_name
    node_id: str = ""
    data_type: str = ""
    minimo: Optional[float] = None
    maximo: Optional[float] = None
    descripcion: str = ""
    habilitado_por: str = ""
    habilitado_en: str = ""
    actualizado_en: str = ""

    def clave(self) -> str:
        return f"{self.plc_id}|{self.tag}"

    def publico(self) -> dict:
        return asdict(self)


class ErrorDeRango(ValueError):
    """El valor está fuera de los límites configurados para ese tag."""


class TagNoEscribible(PermissionError):
    """Ese tag no está en la lista blanca."""


class EscrituraStore:
    def __init__(self, carpeta: Path) -> None:
        self.ruta = carpeta / "escritura_permitida.json"
        self.tags: Dict[str, TagEscribible] = {}
        self.cargar()

    def cargar(self) -> int:
        """
        Relee el fichero. Si está corrupto se arranca con la lista VACÍA:
        "no se puede escribir en nada" es el fallo seguro.
        """
        self.tags = {}
        doc = leer_json_tolerante(self.ruta, {})
        try:
            for fila in doc.get("tags", []):
                t = TagEscribible(**fila)
                self.tags[t.clave()] = t
        except Exception as exc:  # noqa: BLE001
            logger.error("Lista de tags escribibles corrupta (%s). Se arranca SIN "
                         "permitir ninguna escritura.", exc)
            self.tags = {}
        logger.info("Tags habilitados para escritura: %d.", len(self.tags))
        return len(self.tags)

    def _escribir(self) -> None:
        escribir_json_atomico(self.ruta, {
            "version": 1, "actualizado_en": _ahora_iso(),
            "tags": [asdict(t) for t in self.tags.values()]})

    def listar(self, plc_id: str = "") -> List[dict]:
        tags = sorted(self.tags.values(), key=lambda t: (t.plc_id, t.tag))
        return [t.publico() for t in tags if not plc_id or t.plc_id == plc_id]

    def obtener(self, plc_id: str, tag: str) -> Optional[TagEscribible]:
        return self.tags.get(f"{plc_id}|{tag}")

    def habilitar(self, plc_id: str, tag: str, node_id: str = "", data_type: str = "",
                  minimo: Optional[float] = None, maximo: Optional[float] = None,
                  descripcion: str = "", usuario: str = "") -> TagEscribible:
        if not plc_id or not tag:
            raise ValueError("Hacen falta el plc_id y el tag.")
        if minimo is not None and maximo is not None and minimo > maximo:
            raise ValueError(f"El mínimo ({minimo}) es mayor que el máximo ({maximo}).")
        anterior = self.tags.get(f"{plc_id}|{tag}")
        entrada = TagEscribible(
            plc_id=plc_id, tag=tag, node_id=node_id, data_type=data_type,
            minimo=minimo, maximo=maximo, descripcion=descripcion,
            habilitado_por=(anterior.habilitado_por if anterior else usuario) or usuario,
            habilitado_en=(anterior.habilitado_en if anterior else _ahora_iso()),
            actualizado_en=_ahora_iso())
        self.tags[entrada.clave()] = entrada
        self._escribir()
        logger.info("Tag '%s' del PLC '%s' habilitado para escritura por %s (rango %s..%s).",
                    tag, plc_id, usuario or "?", minimo, maximo)
        return entrada

    def deshabilitar(self, plc_id: str, tag: str) -> bool:
        clave = f"{plc_id}|{tag}"
        if clave not in self.tags:
            return False
        del self.tags[clave]
        self._escribir()
        logger.info("Tag '%s' del PLC '%s' YA NO es escribible.", tag, plc_id)
        return True

    def borrar_plc(self, plc_id: str) -> int:
        """Al quitar un PLC se olvidan también sus permisos de escritura."""
        claves = [k for k, t in self.tags.items() if t.plc_id == plc_id]
        for k in claves:
            del self.tags[k]
        if claves:
            self._escribir()
        return len(claves)

    def validar(self, plc_id: str, tag: str, valor: object) -> TagEscribible:
        """Comprueba que el tag esté habilitado y el valor en rango; si no, lanza."""
        entrada = self.obtener(plc_id, tag)
        if entrada is None:
            raise TagNoEscribible(
                f"El tag '{tag}' del PLC '{plc_id}' no está habilitado para escritura. "
                f"Habilítalo primero con PUT /escritura/permitidos indicando sus límites.")
        # Los límites solo aplican a numéricos: `True > 5` daría False sin error.
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            return entrada
        if entrada.minimo is not None and valor < entrada.minimo:
            raise ErrorDeRango(f"{valor} está por debajo del mínimo permitido para "
                               f"'{tag}' ({entrada.minimo}).")
        if entrada.maximo is not None and valor > entrada.maximo:
            raise ErrorDeRango(f"{valor} supera el máximo permitido para '{tag}' "
                               f"({entrada.maximo}).")
        return entrada

    def estado(self) -> dict:
        return {
            "num_tags": len(self.tags),
            "fichero": str(self.ruta),
            "por_plc": {plc: sum(1 for t in self.tags.values() if t.plc_id == plc)
                        for plc in sorted({t.plc_id for t in self.tags.values()})},
        }
