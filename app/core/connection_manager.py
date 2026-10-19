# -*- coding: utf-8 -*-
"""
connection_manager.py
=====================
Gestiona el conjunto de clientes WebSocket conectados y difunde los mensajes
(cambios de tags, snapshots, estados) a todos ellos.

Es agnóstico al origen de los datos: solo sabe de sockets y de enviar JSON.
Un cliente lento o caído no afecta a los demás:

  * El envío es CONCURRENTE (`gather`): el lento solo se retrasa a sí mismo.
  * Cada envío tiene un timeout; el que no responde se cierra y se depura.
  * El JSON se serializa UNA vez para todos.

Además admite OBSERVADORES internos: funciones del propio backend que
reciben una copia de cada mensaje sin ser clientes WebSocket. Es el gancho
para un historizador, un motor de alarmas o un puente MQTT, sin abrir una
segunda sesión con el PLC.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger("connection_manager")

#: Segundos que se le dan a UN cliente para aceptar un mensaje.
TIMEOUT_ENVIO = 5.0


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ConnectionManager:
    """Administra las conexiones WebSocket activas y el broadcast."""

    def __init__(self) -> None:
        self._active: Set[WebSocket] = set()
        # Filtro opcional por conexión: websocket -> plc_id (None = todos).
        self._filtros: Dict[WebSocket, Optional[str]] = {}
        # Identidad opcional detrás de cada socket (para presencia/auditoría).
        self._usuarios: Dict[WebSocket, dict] = {}
        self._lock = asyncio.Lock()
        self._observadores: List[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    # Alta / baja
    # ------------------------------------------------------------------ #
    async def connect(self, websocket: WebSocket, plc_filter: Optional[str] = None,
                      usuario: Optional[dict] = None) -> None:
        await websocket.accept()
        async with self._lock:
            self._active.add(websocket)
            self._filtros[websocket] = plc_filter
            if usuario:
                self._usuarios[websocket] = usuario
        logger.info("Cliente WS conectado (usuario=%s, filtro=%s). Total: %d",
                    (usuario or {}).get("usuario", "anónimo"), plc_filter or "todos",
                    len(self._active))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._active.discard(websocket)
            self._filtros.pop(websocket, None)
            self._usuarios.pop(websocket, None)
        logger.info("Cliente WS desconectado. Total: %d", len(self._active))

    def count(self) -> int:
        return len(self._active)

    def presentes(self) -> List[dict]:
        """Quién está mirando ahora mismo, deduplicado por nombre."""
        vistos: Dict[str, dict] = {}
        for u in self._usuarios.values():
            nombre = u.get("usuario")
            if nombre and nombre not in vistos:
                vistos[nombre] = {"usuario": nombre}
        anonimos = len(self._active) - len(vistos)
        salida = sorted(vistos.values(), key=lambda x: x["usuario"])
        if anonimos > 0:
            salida.append({"usuario": f"{anonimos} anónimo(s)"})
        return salida

    async def difundir_presencia(self) -> None:
        await self.broadcast({"timestamp": _ahora_iso(), "type": "presence",
                              "usuarios": self.presentes(), "num_clientes": len(self._active)})

    # ------------------------------------------------------------------ #
    # Observadores internos
    # ------------------------------------------------------------------ #
    def registrar_observador(self, callback: Callable[[dict], None]) -> None:
        """
        El callback recibe CADA mensaje difundido. Debe ser SÍNCRONO y muy
        rápido (encolar y volver): corre dentro del bucle de broadcast.
        """
        if callback not in self._observadores:
            self._observadores.append(callback)
            logger.info("Observador interno registrado (total: %d).", len(self._observadores))

    def quitar_observador(self, callback: Callable[[dict], None]) -> None:
        if callback in self._observadores:
            self._observadores.remove(callback)

    # ------------------------------------------------------------------ #
    # Envío
    # ------------------------------------------------------------------ #
    async def send_personal(self, message: dict, websocket: WebSocket) -> None:
        try:
            await asyncio.wait_for(websocket.send_json(message), timeout=TIMEOUT_ENVIO)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fallo enviando mensaje personal, se desconecta: %s", exc)
            await self.disconnect(websocket)

    async def broadcast(self, message: dict) -> None:
        """
        Envía un mensaje JSON a los clientes. Si un cliente definió un filtro
        de PLC, solo recibe mensajes de ese PLC (los mensajes sin campo
        'plc' llegan a todos). Los que fallen se cierran y se eliminan.
        """
        for obs in self._observadores:
            try:
                obs(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Observador interno falló: %s", exc)

        plc_msg = message.get("plc")
        async with self._lock:
            destinatarios = list(self._active)
            filtros = dict(self._filtros)

        objetivo = [ws for ws in destinatarios
                    if not (filtros.get(ws) and plc_msg and filtros.get(ws) != plc_msg)]
        if not objetivo:
            return

        try:
            texto = json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.warning("Mensaje no serializable, no se difunde: %s", exc)
            return

        async def enviar(ws: WebSocket) -> None:
            await asyncio.wait_for(ws.send_text(texto), timeout=TIMEOUT_ENVIO)

        resultados = await asyncio.gather(*(enviar(ws) for ws in objetivo),
                                          return_exceptions=True)
        caidos = [ws for ws, r in zip(objetivo, resultados) if isinstance(r, Exception)]
        if caidos:
            async with self._lock:
                for ws in caidos:
                    self._active.discard(ws)
                    self._filtros.pop(ws, None)
                    self._usuarios.pop(ws, None)
            logger.info("Depurados %d clientes WS caídos. Total: %d", len(caidos), len(self._active))
            for ws in caidos:
                try:
                    await asyncio.wait_for(ws.close(code=1011), timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
