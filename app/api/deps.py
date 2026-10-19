# -*- coding: utf-8 -*-
"""
deps.py
=======
Dependencias comunes de los routers: acceso a los componentes que `main.py`
deja en `app.state`, y la protección por clave de API.

SEGURIDAD
---------
Este backend no lleva gestor de usuarios (eso vive en la aplicación que lo
consuma). Lo que sí lleva es una barrera mínima para lo que MODIFICA algo:

  * Si `PLC_API_KEY` está vacía, todo está abierto (desarrollo).
  * Si tiene valor, los endpoints que alteran estado (alta/baja de PLC,
    escritura, lista blanca) exigen la cabecera `X-API-Key` (REST) o el
    parámetro `?api_key=` (WebSocket). Los de solo lectura siguen abiertos.

La identidad para la auditoría llega en la cabecera opcional `X-Usuario`:
el sistema de arriba, que sí sabe quién está delante, la rellena.

Para integrar un sistema de usuarios propio basta con sustituir
`exigir_api_key` por una dependencia que valide el token y devuelva el
usuario: todos los routers la reciben por inyección.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request

from app.config.settings import Settings


def settings_de(request: Request) -> Settings:
    return request.app.state.settings


def plc_manager_de(request: Request):
    m = getattr(request.app.state, "plc_manager", None)
    if m is None:
        raise HTTPException(503, "El gestor de PLCs no está disponible.")
    return m


def escritura_store_de(request: Request):
    s = getattr(request.app.state, "escritura_store", None)
    if s is None:
        raise HTTPException(503, "El almacén de escritura no está disponible.")
    return s


def auditar(request: Request, accion: str, usuario: str = "", recurso: str = "",
            detalle: Optional[dict] = None, resultado: str = "ok") -> None:
    aud = getattr(request.app.state, "auditoria", None)
    if aud is not None:
        aud.registrar(accion, usuario=usuario, recurso=recurso, detalle=detalle, resultado=resultado)


def clave_valida(settings: Settings, clave: Optional[str]) -> bool:
    """Comparación en tiempo constante. Sin clave configurada, todo pasa."""
    if not settings.api_key:
        return True
    return bool(clave) and hmac.compare_digest(clave, settings.api_key)


async def exigir_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_usuario: Optional[str] = Header(default=None, alias="X-Usuario"),
) -> str:
    """
    Dependencia para los endpoints que MODIFICAN algo. Devuelve el nombre de
    usuario para la auditoría ("" si no se mandó).
    """
    if not clave_valida(settings_de(request), x_api_key):
        raise HTTPException(401, "Clave de API inválida o ausente (cabecera X-API-Key).")
    return (x_usuario or "").strip()[:80]
