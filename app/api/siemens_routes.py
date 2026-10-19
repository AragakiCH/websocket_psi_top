# -*- coding: utf-8 -*-
"""
siemens_routes.py
=================
Utilidades Siemens previas al alta de un PLC:

  POST /siemens/identificar   ¿qué CPU es y por qué transporte conectar?
  POST /siemens/sondear-dbs   ¿qué DB existen en el PLC y de qué tamaño? (S7comm)
  POST /siemens/importar      fuentes de TIA (.db/.udt/.scl o tabla pegada)
                              -> lista de tags con offsets para S7comm
  POST /siemens/validar-tags  comprueba la sintaxis de una lista s7_tags

Ninguno registra nada: abren una conexión temporal y la cierran.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from app.core.endpoint import host_de

router = APIRouter(prefix="/siemens", tags=["Siemens"])


# ====================================================================== #
# Identificar
# ====================================================================== #
class SondeoSiemens(BaseModel):
    host: str = Field(..., examples=["192.168.0.10"])
    rack: int = Field(default=0, ge=0, le=7)
    slot: Optional[int] = Field(default=None, ge=0, le=31, description="Vacío = probar 1, 2 y 0.")
    timeout_ms: int = Field(default=2500, ge=200, le=20000)


async def _puerto_abierto(host: str, puerto: int, timeout: float = 2.0) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, puerto), timeout=timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


@router.post(
    "/identificar", summary="Identificar una CPU Siemens y decir por qué transporte conectar",
    description="Pregunta la identidad de la CPU por S7comm (puerto 102, que responde en "
                "cualquier S7 aunque el OPC UA no exista) y comprueba si hay servidor OPC UA "
                "en el 4840. Devuelve modelo, familia, estado RUN/STOP y el transporte sugerido.",
    responses={
        200: {"content": {"application/json": {"example": {
            "ok": True, "modelo": "CPU 1214C DC/DC/Rly", "familia": "S7-1200",
            "nombre": "PLC_1", "estado": "Run", "rack": 0, "slot": 1,
            "opcua": False, "s7comm": True, "transporte_sugerido": "s7comm",
            "mensaje": "Es un S7-1200 y no responde en el 4840: ..."}}}},
        502: {"description": "No responde ni en el 102 ni en el 4840."},
    })
async def identificar(cuerpo: SondeoSiemens) -> dict:
    from app.drivers.s7comm_driver import identificar as identificar_s7

    host = host_de(cuerpo.host)
    if not host:
        raise HTTPException(400, "Indica la IP del PLC.")

    tarea_opcua = asyncio.create_task(_puerto_abierto(host, 4840))
    info: dict = {}
    error_s7 = ""
    try:
        info = await identificar_s7(host, cuerpo.rack, cuerpo.slot, cuerpo.timeout_ms)
    except Exception as exc:  # noqa: BLE001
        error_s7 = str(exc)
    opcua = await tarea_opcua

    if "No module named 'snap7'" in error_s7 or "ModuleNotFoundError" in error_s7:
        raise HTTPException(500, "Falta el paquete 'python-snap7' en el entorno: ejecuta "
                                 "`pip install python-snap7` y reinicia el servicio.")
    if not info and not opcua:
        raise HTTPException(502, f"{host} no responde ni por S7comm (puerto 102) ni por OPC UA "
                                 f"(4840). Revisa la IP y la red (¿hace ping?). Detalle: {error_s7}")

    familia = info.get("familia", "S7")
    modelo = info.get("modelo", "")
    if info and not modelo:
        modelo = "CPU Siemens (responde por S7comm; no publica su modelo, típico del S7-1200)"
    if opcua:
        sugerido = "opcua"
        mensaje = f"{modelo or 'La CPU'} tiene servidor OPC UA activo: conecta por OPC UA y descubre los DB solo."
    elif familia == "S7-1200":
        sugerido = "s7comm"
        mensaje = (f"{modelo} es un S7-1200 y no responde en el 4840: o el firmware es anterior a 4.4 "
                   f"o el servidor OPC UA no está activado (Propiedades → OPC UA → Servidor, con "
                   f"licencia). Conecta por S7comm: en TIA marca 'Permitir acceso PUT/GET' en la CPU, "
                   f"desmarca 'Acceso optimizado' en los DB a leer, y manda la lista de tags.")
    elif familia in ("S7-300", "S7-400"):
        sugerido = "s7comm"
        mensaje = f"{modelo}: sin OPC UA. Conecta por S7comm (slot {info.get('slot')})."
    else:
        sugerido = "s7comm"
        mensaje = f"{modelo or 'La CPU'} no responde en el 4840. Activa el servidor OPC UA o conecta por S7comm."
    return {"ok": True, **info, "modelo": modelo, "opcua": opcua, "s7comm": bool(info),
            "transporte_sugerido": sugerido, "mensaje": mensaje, "error_s7comm": error_s7}


# ====================================================================== #
# Sondear DBs
# ====================================================================== #
class SondeoDbs(BaseModel):
    host: str = Field(..., examples=["192.168.0.10"])
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    dbs: Optional[List[int]] = Field(default=None, description="DB a probar. Vacío = 1..40.")
    timeout_ms: int = Field(default=2500, ge=200, le=20000)


@router.post("/sondear-dbs", summary="Qué DB existen en el PLC y de qué tamaño (S7comm)",
             description="Prueba `db_read` en cada DB candidato y acota su tamaño por búsqueda "
                         "binaria. Un DB con acceso optimizado NO aparece (no se puede leer).")
async def sondear(cuerpo: SondeoDbs) -> dict:
    from app.drivers.s7comm_driver import sondear_dbs
    host = host_de(cuerpo.host)
    if not host:
        raise HTTPException(400, "Indica la IP del PLC.")
    try:
        dbs = await sondear_dbs(host, cuerpo.rack, cuerpo.slot, cuerpo.dbs, cuerpo.timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo sondear {host} por S7comm: {exc}")
    return {"ok": True, "host": host, "dbs": {str(k): v for k, v in sorted(dbs.items())}}


# ====================================================================== #
# Importar fuentes de TIA
# ====================================================================== #
class FuenteTia(BaseModel):
    nombre: str = Field(..., description="Nombre del fichero (.db, .udt, .scl) o cualquier etiqueta.")
    contenido: str = Field(..., max_length=4_000_000,
                           description="Texto del fichero, o las filas del editor del DB pegadas.")


class ImportarSiemens(BaseModel):
    host: str = Field(default="", description="IP del PLC para sondear qué DB existen. Vacío = no se sondea.")
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    fuentes: List[FuenteTia] = Field(..., min_length=1, max_length=50)


@router.post(
    "/importar", summary="Calcular los tags de S7comm a partir de las fuentes de TIA Portal",
    description="Recibe los ficheros que genera TIA con «Generate source from blocks» (.db, "
                ".udt, .scl) —o las filas del editor del DB copiadas y pegadas—, calcula el "
                "offset de cada variable con las reglas de un DB de acceso estándar y, si llega "
                "la IP, sondea el PLC para emparejar cada bloque con su número de DB. El campo "
                "`s7_tags` de cada bloque se puede mandar tal cual a `POST /plcs`.",
    responses={200: {"content": {"application/json": {"example": {
        "ok": True,
        "bloques": [{"nombre": "Data_block_1", "clase": "global", "optimizado": False,
                     "tamano": 10, "db": 1, "db_como": "nombre",
                     "tags": [{"nombre": "temperatura", "offset": 0, "bit": 0, "tipo": "REAL",
                               "longitud": 0, "linea": "temperatura;DB1;0;REAL"}],
                     "s7_tags": "temperatura;DB1;0;REAL", "avisos": []}],
        "dbs_plc": {"1": 10}, "avisos": [], "s7_tags": "temperatura;DB1;0;REAL"}}}}})
async def importar(cuerpo: ImportarSiemens) -> dict:
    from app.drivers.s7_fuentes import numero_en_nombre, parsear_fuentes
    from app.drivers.s7comm_driver import sondear_dbs

    bloques, avisos = parsear_fuentes([(f.nombre, f.contenido) for f in cuerpo.fuentes])

    dbs_plc: Dict[int, int] = {}
    host = host_de(cuerpo.host)
    if host:
        candidatos = set(range(1, 41))
        for b in bloques:
            n = numero_en_nombre(b.nombre)
            if n:
                candidatos.add(n)
        try:
            dbs_plc = await sondear_dbs(host, cuerpo.rack, cuerpo.slot, sorted(candidatos))
        except Exception as exc:  # noqa: BLE001
            avisos.append(f"No se pudo sondear los DB del PLC ({exc}); asigna el número de DB a mano.")

    # Emparejar cada bloque con un número de DB: por nombre y existe; por
    # tamaño único; por nombre sin sondear; o que lo elija el usuario.
    usados: set = set()
    salida_bloques = []
    todas_lineas: List[str] = []
    for b in bloques:
        db: Optional[int] = None
        como = ""
        por_nombre = numero_en_nombre(b.nombre) if b.clase != "pegado" else None
        if por_nombre and por_nombre in dbs_plc and por_nombre not in usados:
            db, como = por_nombre, "nombre"
        elif dbs_plc and b.tamano:
            iguales = [n for n, t in dbs_plc.items() if t == b.tamano and n not in usados]
            if len(iguales) == 1:
                db, como = iguales[0], "tamaño"
        if db is None and por_nombre and not dbs_plc:
            db, como = por_nombre, "nombre (sin sondear)"
        if db is not None:
            usados.add(db)
            if dbs_plc and b.tamano and dbs_plc.get(db) not in (None, b.tamano):
                b.avisos.append(f"El DB{db} del PLC mide {dbs_plc[db]} bytes y la fuente calcula "
                                f"{b.tamano}: puede que el PLC tenga otra versión del bloque.")
        lineas = [t.linea(db or 0) for t in b.tags]
        if db is not None and not b.optimizado:
            todas_lineas.extend(lineas)
        salida_bloques.append({
            "nombre": b.nombre, "clase": b.clase, "base": b.base,
            "optimizado": b.optimizado, "tamano": b.tamano, "db": db, "db_como": como,
            "tags": [{"nombre": t.nombre, "offset": t.offset, "bit": t.bit, "tipo": t.tipo,
                      "longitud": t.longitud, "linea": l} for t, l in zip(b.tags, lineas)],
            "s7_tags": "\n".join(lineas),
            "avisos": b.avisos,
        })
    return {"ok": True, "bloques": salida_bloques,
            "dbs_plc": {str(k): v for k, v in sorted(dbs_plc.items())},
            "avisos": avisos, "s7_tags": "\n".join(todas_lineas)}


# ====================================================================== #
# Validar una lista de tags
# ====================================================================== #
class ListaTags(BaseModel):
    s7_tags: str = Field(..., examples=["temperatura;DB1;0;REAL\nmarcha;DB1;4.0;BOOL"])


@router.post("/validar-tags", summary="Comprobar la sintaxis de una lista s7_tags")
async def validar_tags(cuerpo: ListaTags = Body(...)) -> dict:
    from app.drivers.s7comm_driver import TIPO_IEC, parsear_tags
    try:
        lista = parsear_tags(cuerpo.s7_tags)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "num_tags": len(lista),
            "dbs": sorted({t.db for t in lista}),
            "tags": [{"nombre": t.nombre, "db": t.db, "direccion": t.direccion,
                      "tipo": t.tipo, "tipo_iec": TIPO_IEC[t.tipo], "bytes": t.tamano}
                     for t in lista]}
