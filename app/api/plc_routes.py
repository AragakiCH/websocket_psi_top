# -*- coding: utf-8 -*-
"""
plc_routes.py
=============
Gestión de PLCs y consulta de tags. Documentación interactiva en /docs.

  GET    /health              estado agregado (PLCs, tags, clientes WS)
  GET    /plcs                PLCs gestionados con su configuración y estado
  POST   /plcs                añade un PLC (OPC UA o S7comm)          [API key]
  PUT    /plcs/{id}           cambia su configuración (p. ej. tags S7) [API key]
  POST   /plcs/{id}/reconectar fuerza reconexión                      [API key]
  DELETE /plcs/{id}           quita un PLC                            [API key]
  POST   /discover            escanea la subred y añade los nuevos    [API key]
  GET    /tags?plc=X          tags con su último valor
  GET    /browse?plc=X        árbol de tags por PLC y Data Block
  GET    /auditoria           últimos eventos registrados
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import auditar, exigir_api_key, plc_manager_de

router = APIRouter()
TAG_PLC = ["PLCs"]
TAG_TAGS = ["Tags"]


class NuevoPlc(BaseModel):
    """
    Cuerpo de POST /plcs.

    OPC UA (S7-1500, S7-1200 FW >= 4.4): basta `host`.
    S7comm (S7-1200 sin OPC UA, S7-300/400): `transporte="s7comm"`, `slot`
    (1 en 1200/1500, 2 en 300) y la lista `s7_tags`.
    """

    host: str = Field(..., description="IP, hostname, `opc.tcp://host:4840` o `s7://host:102/rack/slot`.",
                      examples=["192.168.0.10"])
    puerto: int = Field(default=4840, ge=1, le=65535,
                        description="Puerto OPC UA. Se ignora en S7comm (siempre 102).")
    transporte: str = Field(default="", description="`opcua` (defecto) o `s7comm`.",
                            examples=["opcua", "s7comm"])
    nombre: str = Field(default="", description="Nombre amigable (opcional). Sirve de id si se indica.")
    rack: int = Field(default=0, ge=0, le=7, description="Solo S7comm.")
    slot: int = Field(default=0, ge=0, le=31,
                      description="Solo S7comm: 1 en S7-1200/1500, 2 en S7-300. 0 = el por defecto.")
    s7_tags: str = Field(default="",
                         description="Solo S7comm: lista de tags, una por línea: "
                                     "`nombre;DB1;offset;TIPO` (p. ej. `temperatura;DB1;0;REAL`, "
                                     "`marcha;DB1;4.0;BOOL`, `texto;DB1;6;STRING[20]`).")
    usuario: str = Field(default="", description="Solo OPC UA con autenticación (opcional).")
    password: str = Field(default="", description="Solo OPC UA con autenticación (opcional).")


class CambioPlc(BaseModel):
    """Cuerpo de PUT /plcs/{id}. Solo se aplican los campos que lleguen."""

    host: Optional[str] = None
    puerto: Optional[int] = Field(default=None, ge=1, le=65535)
    transporte: Optional[str] = None
    nombre: Optional[str] = None
    rack: Optional[int] = Field(default=None, ge=0, le=7)
    slot: Optional[int] = Field(default=None, ge=0, le=31)
    s7_tags: Optional[str] = None
    usuario: Optional[str] = None
    password: Optional[str] = None


@router.get("/health", tags=["Sistema"], summary="Estado general del servicio",
            description="Cuántos PLCs hay, cuáles están conectados, modelo de CPU, modo de "
                        "lectura, número de tags, reconexiones y clientes WebSocket.")
async def health(request: Request) -> dict:
    return plc_manager_de(request).get_health()


@router.get("/plcs", tags=TAG_PLC, summary="Listar PLCs gestionados",
            description="Configuración (sin contraseñas) y estado de cada PLC. Los `plc_id` "
                        "se usan en `?plc=` de /tags, /browse y en `/ws?plc=<id>`.")
async def listar_plcs(request: Request) -> dict:
    m = plc_manager_de(request)
    return {"plcs": m.list_plcs(), "ids": m.list_plc_ids()}


@router.post(
    "/plcs", tags=TAG_PLC, summary="Agregar un PLC Siemens",
    description="Añade un PLC en caliente. Responde de inmediato; la conexión se intenta en "
                "segundo plano con reintentos automáticos. Todos los clientes WebSocket "
                "reciben un snapshot actualizado.\n\n"
                "Si no sabes qué transporte usar, llama antes a `POST /siemens/identificar`.",
    responses={200: {"content": {"application/json": {"examples": {
        "opcua": {"summary": "S7-1500 por OPC UA", "value": {
            "ok": True, "plc_id": "192.168.0.10", "endpoint": "opc.tcp://192.168.0.10:4840",
            "transporte": "opcua", "mensaje": "PLC 192.168.0.10 (opcua) añadido; conectando..."}},
        "s7comm": {"summary": "S7-1200 por S7comm", "value": {
            "ok": True, "plc_id": "Linea1", "endpoint": "s7://192.168.0.20:102/0/1",
            "transporte": "s7comm", "mensaje": "PLC Linea1 (s7comm) añadido; conectando..."}},
        "duplicado": {"summary": "Ya existía", "value": {
            "ok": False, "plc_id": "192.168.0.10", "endpoint": "opc.tcp://192.168.0.10:4840",
            "mensaje": "Ese PLC ya está gestionado (id=192.168.0.10)."}},
    }}}}})
async def agregar_plc(
    request: Request,
    cuerpo: NuevoPlc = Body(..., examples=[
        {"host": "192.168.0.10", "puerto": 4840, "transporte": "opcua"},
        {"host": "192.168.0.20", "transporte": "s7comm", "slot": 1, "nombre": "Linea1",
         "s7_tags": "temperatura;DB1;0;REAL\nmarcha;DB1;4.0;BOOL\ncontador;DB1;6;DINT"},
    ]),
    usuario: str = Depends(exigir_api_key),
) -> dict:
    resultado = await plc_manager_de(request).add_plc(**cuerpo.model_dump())
    auditar(request, "plc.alta", usuario, cuerpo.host,
            {"transporte": cuerpo.transporte, "resultado": resultado.get("mensaje")},
            "ok" if resultado.get("ok") else "error")
    if not resultado.get("ok") and "ya está gestionado" not in resultado.get("mensaje", ""):
        raise HTTPException(400, resultado.get("mensaje"))
    return resultado


@router.put("/plcs/{plc_id}", tags=TAG_PLC, summary="Cambiar la configuración de un PLC",
            description="Reemplaza host, transporte, rack/slot o la lista de tags S7comm de un "
                        "PLC conservando su `plc_id`. Se reconecta con la nueva configuración.")
async def cambiar_plc(request: Request, plc_id: str, cuerpo: CambioPlc,
                      usuario: str = Depends(exigir_api_key)) -> dict:
    cambios = {k: v for k, v in cuerpo.model_dump().items() if v is not None}
    resultado = await plc_manager_de(request).update_plc(plc_id, **cambios)
    auditar(request, "plc.cambio", usuario, plc_id, {"campos": sorted(cambios)},
            "ok" if resultado.get("ok") else "error")
    if not resultado.get("ok"):
        raise HTTPException(404 if "No existe" in resultado.get("mensaje", "") else 400,
                            resultado.get("mensaje"))
    return resultado


@router.post("/plcs/{plc_id}/reconectar", tags=TAG_PLC, summary="Forzar la reconexión de un PLC")
async def reconectar_plc(request: Request, plc_id: str,
                         usuario: str = Depends(exigir_api_key)) -> dict:
    resultado = await plc_manager_de(request).reconnect_plc(plc_id)
    auditar(request, "plc.reconexion", usuario, plc_id)
    if not resultado.get("ok"):
        raise HTTPException(404, resultado.get("mensaje"))
    return resultado


@router.delete("/plcs/{plc_id}", tags=TAG_PLC, summary="Quitar un PLC",
               description="Cierra la conexión, olvida su catálogo de tags y sus permisos de "
                           "escritura. Los clientes WebSocket reciben `type: plc_removed`.")
async def quitar_plc(request: Request, plc_id: str,
                     usuario: str = Depends(exigir_api_key)) -> dict:
    resultado = await plc_manager_de(request).remove_plc(plc_id)
    store = getattr(request.app.state, "escritura_store", None)
    if resultado.get("ok") and store is not None:
        store.borrar_plc(plc_id)
    auditar(request, "plc.baja", usuario, plc_id, resultado=("ok" if resultado.get("ok") else "error"))
    if not resultado.get("ok"):
        raise HTTPException(404, resultado.get("mensaje"))
    return resultado


@router.post("/discover", tags=TAG_PLC, summary="Escanear la red buscando PLCs OPC UA",
             description="Escanea la subred (PLC_DISCOVERY_SUBNET, o la derivada del endpoint "
                         "semilla) en el puerto 4840 y añade los servidores OPC UA nuevos. "
                         "Puede tardar varios segundos. Un S7-1200 sin OPC UA no aparece aquí: "
                         "usa `POST /siemens/identificar`.")
async def redescubrir(request: Request, usuario: str = Depends(exigir_api_key)) -> dict:
    resultado = await plc_manager_de(request).rescan()
    auditar(request, "plc.discover", usuario, "", {"nuevos": resultado.get("nuevos")})
    return resultado


@router.get("/tags", tags=TAG_TAGS, summary="Tags con su último valor",
            description="Todos los tags conocidos con el último valor recibido (`null` si el PLC "
                        "está desconectado). Filtra con `?plc=<id>`.")
async def tags(request: Request, plc: Optional[str] = Query(default=None)) -> dict:
    return {"plc": plc, "tags": plc_manager_de(request).get_tags(plc)}


@router.get("/browse", tags=TAG_TAGS, summary="Árbol de tags por Data Block")
async def browse(request: Request, plc: Optional[str] = Query(default=None)) -> dict:
    return plc_manager_de(request).get_browse(plc)


@router.get("/auditoria", tags=["Sistema"], summary="Últimos eventos de auditoría")
async def auditoria(request: Request, limite: int = Query(default=200, ge=1, le=5000),
                    usuario: str = Query(default=""), accion: str = Query(default="")) -> dict:
    aud = getattr(request.app.state, "auditoria", None)
    eventos: List[dict] = aud.leer(limite, usuario, accion) if aud else []
    return {"eventos": eventos, "num": len(eventos)}
