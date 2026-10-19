# -*- coding: utf-8 -*-
"""
websocket_routes.py
===================
Endpoint WebSocket `/ws`: el canal de BAJADA de datos en tiempo real.

Parámetros de query:
    plc=<id>        recibir SOLO ese PLC (y su snapshot). Sin él, todos.
    api_key=<k>     obligatorio para el comando `write` si PLC_API_KEY está
                    configurada (la API WebSocket del navegador no admite
                    cabeceras personalizadas, por eso va en la URL).
    usuario=<nombre> identidad para la presencia y la auditoría (opcional).

Al conectarse, el cliente recibe un `snapshot` con todos los PLCs y tags, y
a partir de ahí: cambios de valor, `status`, `plc_removed`, `presence`.

COMANDOS DE SUBIDA (JSON):
    {"tipo": "ping"}                       -> {"tipo": "pong"}
    {"tipo": "snapshot"}                   -> reenvía el snapshot
    {"tipo": "write", "id": "...",
     "plc_id": "...", "tag": "...", "valor": ...}
    {"tipo": "write", "id": "...",
     "escrituras": [{"plc_id","tag","valor"}, ...]}
                                           -> {"tipo": "write.result", "id", "ok", ...}

La escritura por WS pasa por las MISMAS comprobaciones que `POST /escritura`
(clave, lista blanca, rangos, tipos, auditoría). No hay camino rápido.
Cualquier otro mensaje se ignora en silencio: un JSON mal formado no puede
tumbar la conexión de datos de un HMI en marcha.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.deps import clave_valida
from app.api.escritura_routes import ejecutar_escritura

logger = logging.getLogger("websocket_routes")

router = APIRouter()

_CODIGOS = {400: "invalido", 401: "no_autorizado", 403: "denegado",
            404: "no_existe", 409: "sin_conexion"}


async def _atender_escritura(websocket: WebSocket, msg: dict, usuario: str,
                             autorizado: bool) -> None:
    eco = {"tipo": "write.result", "id": msg.get("id")}

    async def responder(**extra) -> None:
        await websocket.send_text(json.dumps({**eco, **extra}, ensure_ascii=False))

    if not autorizado:
        await responder(ok=False, codigo="no_autorizado",
                        error="Para escribir hace falta la clave de API (?api_key=).")
        return

    escrituras = msg.get("escrituras")
    if escrituras is None:
        if msg.get("tag") is None:
            await responder(ok=False, codigo="invalido", error="Falta 'tag' o 'escrituras'.")
            return
        escrituras = [{"plc_id": msg.get("plc_id"), "tag": msg.get("tag"), "valor": msg.get("valor")}]

    try:
        resultado = await ejecutar_escritura(websocket, escrituras, usuario, "websocket")
    except HTTPException as exc:
        await responder(ok=False, codigo=_CODIGOS.get(exc.status_code, "error"), error=exc.detail)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error inesperado escribiendo por WS")
        await responder(ok=False, codigo="error", error=str(exc))
        return
    await responder(**resultado)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, plc: Optional[str] = None,
                             api_key: Optional[str] = None,
                             usuario: Optional[str] = None) -> None:
    manager = websocket.app.state.manager
    plc_manager = websocket.app.state.plc_manager
    settings = websocket.app.state.settings

    usuario = (usuario or "").strip()[:80]
    autorizado = clave_valida(settings, api_key)
    datos_usuario = {"usuario": usuario} if usuario else None

    await manager.connect(websocket, plc_filter=plc, usuario=datos_usuario)
    try:
        await manager.send_personal(plc_manager.build_snapshot_message(plc), websocket)
        await manager.difundir_presencia()

        while True:
            crudo = await websocket.receive_text()
            try:
                msg = json.loads(crudo)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            tipo = msg.get("tipo")
            if tipo == "ping":
                await websocket.send_text(json.dumps({"tipo": "pong", "id": msg.get("id")}))
            elif tipo == "snapshot":
                await manager.send_personal(plc_manager.build_snapshot_message(plc), websocket)
            elif tipo == "write":
                await _atender_escritura(websocket, msg, usuario, autorizado)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error en la conexión WS: %s", exc)
    finally:
        await manager.disconnect(websocket)
        try:
            await manager.difundir_presencia()
        except Exception:  # noqa: BLE001
            pass
