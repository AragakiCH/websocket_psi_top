# -*- coding: utf-8 -*-
"""
escritura_routes.py
===================
Escribir valores en el PLC, y decidir en qué se puede escribir.

    POST   /escritura                        escribir uno o varios tags   [API key]
    GET    /escritura/permitidos             lista blanca actual
    PUT    /escritura/permitidos             habilitar un tag / cambiar límites [API key]
    DELETE /escritura/permitidos/{plc}/{tag} deshabilitarlo               [API key]
    GET    /escritura/candidatos?plc_id=X    tags que se PODRÍAN habilitar

TRES CANDADOS
  1. CLAVE DE API (si está configurada): nadie sin ella escribe.
  2. LISTA BLANCA con rangos: el tag tiene que estar habilitado a mano.
  3. AUDITORÍA: cada escritura queda registrada con quién, qué, valor
     anterior y valor nuevo.

El tipo lo valida `drivers/escritura.py` antes de salir a la red, y el valor
escrito se RELEE del PLC para confirmarlo (`coincide`).
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import auditar, escritura_store_de, exigir_api_key, plc_manager_de
from app.drivers.escritura import ErrorDeTipo, es_escribible
from app.stores.escritura_store import ErrorDeRango, TagNoEscribible

router = APIRouter(prefix="/escritura", tags=["Escritura en el PLC"])


class UnaEscritura(BaseModel):
    plc_id: str = Field(..., description="Id del PLC, como en `GET /plcs`.")
    tag: str = Field(..., description="`full_name` (`DB_Datos.setpoint`) o `node_id`.")
    valor: Any = Field(..., description="Se convierte al tipo real del tag; si no encaja, se rechaza.")


class PeticionEscritura(BaseModel):
    escrituras: List[UnaEscritura] = Field(
        ..., description="Uno o varios tags. Si alguno falla, los ya escritos se restauran.")


class TagPermitido(BaseModel):
    plc_id: str
    tag: str
    minimo: Optional[float] = Field(default=None, description="`null` = sin límite. Solo numéricos.")
    maximo: Optional[float] = Field(default=None)
    descripcion: str = Field(default="", description="Texto para la interfaz.")


async def ejecutar_escritura(request: Request, escrituras: List[dict], usuario: str,
                             via: str) -> dict:
    """Lógica compartida por REST y WebSocket: escribir + auditar. Lanza HTTPException."""
    try:
        resultado = await plc_manager_de(request).escribir_tags(
            escrituras, usuario=usuario, permitidos=escritura_store_de(request))
    except TagNoEscribible as exc:
        auditar(request, "plc.escritura.denegada", usuario, "escritura",
                {"peticion": escrituras, "motivo": str(exc), "via": via}, "denegado")
        raise HTTPException(403, str(exc))
    except ErrorDeRango as exc:
        auditar(request, "plc.escritura.fuera_de_rango", usuario, "escritura",
                {"peticion": escrituras, "motivo": str(exc), "via": via}, "rechazado")
        raise HTTPException(400, str(exc))
    except ErrorDeTipo as exc:
        raise HTTPException(400, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    except (ConnectionError, NotImplementedError) as exc:
        raise HTTPException(409, str(exc))
    except PermissionError as exc:
        auditar(request, "plc.escritura.rechazada_por_plc", usuario, "escritura",
                {"peticion": escrituras, "motivo": str(exc), "via": via}, "error")
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    auditar(request, "plc.escritura" if resultado.get("ok") else "plc.escritura.fallida",
            usuario, "escritura",
            {"resultados": resultado.get("resultados"), "fallo_en": resultado.get("fallo_en"),
             "revertidos": resultado.get("revertidos"), "sin_revertir": resultado.get("sin_revertir"),
             "via": via},
            "ok" if resultado.get("ok") else "error")
    return resultado


@router.post(
    "", summary="Escribir uno o varios tags del PLC",
    description="**Antes de escribir nada** se valida TODO el lote: que cada tag exista, esté "
                "habilitado, el valor encaje con su tipo y respete sus límites. Si algo no cuadra, "
                "no se manda ni un paquete al PLC.\n\n**Si un tag del lote falla a mitad**, los ya "
                "escritos se restauran a su valor anterior en orden inverso.\n\n**Cada valor se "
                "relee del PLC**: `coincide` dice si el PLC guardó exactamente lo pedido.",
    responses={
        200: {"content": {"application/json": {"example": {
            "ok": True, "escrituras": 1,
            "resultados": [{"plc_id": "192.168.0.10", "tag": "DB_Datos.setpoint", "solicitado": 65,
                            "escrito": 65.0, "confirmado": 65.0, "data_type": "Float",
                            "anterior": 20.0, "coincide": True, "ok": True}],
            "usuario": "hugo", "timestamp": "2026-10-16T10:12:03+00:00"}}}},
        400: {"description": "Tipo incorrecto o fuera de rango."},
        403: {"description": "Tag no habilitado, o el PLC rechazó la escritura."},
        404: {"description": "No existe ese PLC o ese tag."},
        409: {"description": "El PLC no está conectado."},
    })
async def escribir(
    request: Request,
    cuerpo: PeticionEscritura = Body(..., examples=[{"escrituras": [
        {"plc_id": "192.168.0.10", "tag": "DB_Datos.setpoint", "valor": 65.0},
        {"plc_id": "192.168.0.10", "tag": "DB_Datos.marcha", "valor": True}]}]),
    usuario: str = Depends(exigir_api_key),
) -> dict:
    return await ejecutar_escritura(request, [e.model_dump() for e in cuerpo.escrituras],
                                    usuario, "rest")


@router.get("/permitidos", summary="Tags habilitados para escritura")
async def listar_permitidos(request: Request, plc_id: str = Query(default="")) -> dict:
    store = escritura_store_de(request)
    return {"tags": store.listar(plc_id), **store.estado()}


@router.put("/permitidos", summary="Habilitar un tag para escritura (o cambiar sus límites)",
            description="Se comprueba contra el PLC REAL que el tag exista y que su tipo se pueda "
                        "escribir. En tags booleanos o de texto los límites se ignoran.",
            responses={400: {"description": "Tipo no soportado, o mínimo mayor que el máximo."},
                       404: {"description": "Ese tag no existe en ese PLC."}})
async def habilitar(
    request: Request,
    cuerpo: TagPermitido = Body(..., examples=[{
        "plc_id": "192.168.0.10", "tag": "DB_Datos.setpoint", "minimo": 40.0, "maximo": 90.0,
        "descripcion": "Consigna de temperatura · Cuba 1"}]),
    usuario: str = Depends(exigir_api_key),
) -> dict:
    handler = plc_manager_de(request).handler(cuerpo.plc_id)
    if handler is None:
        raise HTTPException(404, f"No hay ningún PLC con id '{cuerpo.plc_id}'.")
    info = handler.buscar_tag(cuerpo.tag)
    if info is None:
        raise HTTPException(404, f"El tag '{cuerpo.tag}' no existe en el PLC '{cuerpo.plc_id}'. "
                                 f"Míralos con GET /tags?plc={cuerpo.plc_id}.")
    if not es_escribible(info.data_type):
        raise HTTPException(400, f"El tag '{info.full_name}' es de tipo '{info.data_type}', que no "
                                 f"se puede escribir (estructuras, arrays y binarios quedan fuera).")
    if not handler.soporta_escritura():
        raise HTTPException(400, f"El PLC '{cuerpo.plc_id}' usa un driver que no sabe escribir.")
    try:
        entrada = escritura_store_de(request).habilitar(
            plc_id=cuerpo.plc_id, tag=info.full_name, node_id=info.node_id,
            data_type=info.data_type, minimo=cuerpo.minimo, maximo=cuerpo.maximo,
            descripcion=cuerpo.descripcion, usuario=usuario)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    auditar(request, "plc.escritura.habilitada", usuario, info.full_name,
            {"plc_id": cuerpo.plc_id, "minimo": cuerpo.minimo, "maximo": cuerpo.maximo,
             "data_type": info.data_type})
    return {"ok": True, "tag": entrada.publico(),
            "mensaje": f"'{info.full_name}' habilitado para escritura."}


@router.delete("/permitidos/{plc_id}/{tag:path}", summary="Quitar un tag de la lista blanca")
async def deshabilitar(request: Request, plc_id: str, tag: str,
                       usuario: str = Depends(exigir_api_key)) -> dict:
    if not escritura_store_de(request).deshabilitar(plc_id, tag):
        raise HTTPException(404, f"'{tag}' no estaba habilitado en '{plc_id}'.")
    auditar(request, "plc.escritura.deshabilitada", usuario, tag, {"plc_id": plc_id})
    return {"ok": True, "mensaje": f"'{tag}' ya no se puede escribir."}


@router.get("/candidatos", summary="Tags que se podrían habilitar",
            description="Los tags del PLC cuyo tipo admite escritura, marcando cuáles ya lo están.")
async def candidatos(request: Request, plc_id: str = Query(...)) -> dict:
    handler = plc_manager_de(request).handler(plc_id)
    if handler is None:
        raise HTTPException(404, f"No hay ningún PLC con id '{plc_id}'.")
    store = escritura_store_de(request)
    salida = []
    for t in handler.tags:
        if not es_escribible(t.data_type):
            continue
        ya = store.obtener(plc_id, t.full_name)
        salida.append({"tag": t.full_name, "node_id": t.node_id, "data_type": t.data_type,
                       "db_name": t.db_name, "habilitado": ya is not None,
                       "minimo": ya.minimo if ya else None, "maximo": ya.maximo if ya else None,
                       "descripcion": ya.descripcion if ya else ""})
    return {"plc_id": plc_id, "num_candidatos": len(salida),
            "soporta_escritura": handler.soporta_escritura(),
            "candidatos": sorted(salida, key=lambda x: x["tag"])}
