# -*- coding: utf-8 -*-
"""
auditoria.py
============
Registro de quién hizo qué y cuándo (`datos/auditoria.jsonl`).

El día que un cliente ESCRIBE valores en el PLC, la trazabilidad es un
requisito: alguien va a preguntar quién cambió una consigna a las 3 de la
mañana. Se registran altas/bajas de PLC, cambios en la lista blanca y cada
escritura (con valor anterior y nuevo).

Formato JSONL: una línea JSON por evento. Se escribe con `append` (coste
constante), un corte a media escritura corrompe UNA línea, y se lee con
`tail`/`grep`/Excel. No bloquea: la escritura va a un hilo aparte.
Rotación: al pasar de MAX_MB se renombra a `.1` y se empieza uno nuevo.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("auditoria")

MAX_MB = 20
MAX_COLA = 5000


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Auditoria:
    def __init__(self, carpeta: Path) -> None:
        self.ruta = carpeta / "auditoria.jsonl"
        self._cola: "queue.Queue[dict]" = queue.Queue(maxsize=MAX_COLA)
        self._hilo: Optional[threading.Thread] = None
        self._parar = threading.Event()

    def start(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, daemon=True, name="auditoria")
        self._hilo.start()
        logger.info("Auditoría activa en %s", self.ruta)

    def stop(self) -> None:
        self._parar.set()
        if self._hilo and self._hilo.is_alive():
            try:
                self._cola.put_nowait({"__fin__": True})
            except queue.Full:
                pass
            self._hilo.join(timeout=5)

    def registrar(self, accion: str, usuario: str = "", recurso: str = "",
                  detalle: Optional[Dict[str, Any]] = None, resultado: str = "ok") -> None:
        """Encola un evento. NUNCA lanza ni bloquea."""
        evento = {"ts": _ahora_iso(), "usuario": usuario or "anónimo",
                  "accion": accion, "recurso": recurso, "resultado": resultado}
        if detalle:
            evento["detalle"] = {
                k: (v if not isinstance(v, str) or len(v) <= 300 else v[:300] + "…")
                for k, v in detalle.items()}
        try:
            self._cola.put_nowait(evento)
        except queue.Full:
            logger.warning("Cola de auditoría llena; se descarta un evento.")

    def _bucle(self) -> None:
        while not self._parar.is_set() or not self._cola.empty():
            try:
                evento = self._cola.get(timeout=0.5)
            except queue.Empty:
                continue
            if evento.get("__fin__"):
                continue
            try:
                self._rotar_si_toca()
                with open(self.ruta, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(evento, ensure_ascii=False, default=str) + "\n")
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo escribir en la auditoría: %s", exc)

    def _rotar_si_toca(self) -> None:
        try:
            if self.ruta.is_file() and self.ruta.stat().st_size > MAX_MB * 1024 * 1024:
                anterior = self.ruta.with_suffix(".jsonl.1")
                anterior.unlink(missing_ok=True)
                self.ruta.rename(anterior)
        except OSError as exc:
            logger.warning("No se pudo rotar la auditoría: %s", exc)

    def leer(self, limite: int = 200, usuario: str = "", accion: str = "") -> List[dict]:
        """Últimos eventos, del más reciente al más antiguo."""
        if not self.ruta.is_file():
            return []
        salida: List[dict] = []
        try:
            with open(self.ruta, "r", encoding="utf-8") as fh:
                for linea in fh:
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        ev = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    if usuario and ev.get("usuario") != usuario:
                        continue
                    if accion and not str(ev.get("accion", "")).startswith(accion):
                        continue
                    salida.append(ev)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo leer la auditoría: %s", exc)
            return []
        return list(reversed(salida))[:limite]
