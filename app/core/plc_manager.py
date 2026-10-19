# -*- coding: utf-8 -*-
"""
plc_manager.py
==============
Gestor MULTI-PLC. Es el nivel superior que:

  * Restaura los PLCs guardados en disco y, si se pide, descubre otros.
  * Crea el DRIVER que corresponde al transporte de cada PLC (OPC UA o
    S7comm, vía `drivers/registry.py`) y un SubscriptionHandler por cada uno.
  * Agrega el estado de todos para el snapshot inicial y los endpoints REST.
  * Centraliza la ESCRITURA con validación previa, lista blanca y vuelta atrás.

Un PLC caído o inaccesible NO afecta a los demás: cada handler es
independiente. Como cada tag va prefijado con `plc_id`, varios PLCs
conviven sin colisiones.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.config.settings import Settings
from app.core.connection_manager import ConnectionManager
from app.core.endpoint import PUERTO_OPCUA, EndpointPlc, host_de, plc_id_desde, puerto_de
from app.core.plc_discovery import descubrir_plcs
from app.core.subscription_handler import SubscriptionHandler
from app.drivers.escritura import convertir
from app.drivers.registry import crear_driver
from app.stores.plc_store import PlcStore
from app.stores.tags_store import TagsStore

logger = logging.getLogger("plc_manager")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlcManager:
    """Administra N PLCs Siemens en simultáneo."""

    def __init__(self, manager: ConnectionManager, settings: Settings, carpeta_datos: Path) -> None:
        self._manager = manager
        self._settings = settings
        self._handlers: Dict[str, SubscriptionHandler] = {}
        self._endpoints: Dict[str, EndpointPlc] = {}
        self._ids_usados: set = set()
        self._running = False
        self._rescan_task: Optional[asyncio.Task] = None
        self._store = PlcStore(carpeta_datos)
        self._tags_store = TagsStore(carpeta_datos)

    # ------------------------------------------------------------------ #
    # Arranque / parada
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._running = True

        # 1) PLCs guardados en disco: SIEMPRE, con o sin autostart.
        for ep in self._store.cargar():
            try:
                await self._añadir_plc(ep)
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudo restaurar el PLC %s: %s", ep.endpoint, exc)
        if self._handlers:
            logger.info("Restaurados %d PLC(s) de la sesión anterior.", len(self._handlers))

        # 2) Endpoints fijos y/o escaneo, solo con autostart.
        if self._settings.autostart_plcs:
            endpoints = await descubrir_plcs(self._settings)
            ya_estan = {h.endpoint for h in self._handlers.values()}
            endpoints = [ep for ep in endpoints if ep.endpoint not in ya_estan]
            incluir = {x.strip() for x in (self._settings.include_plcs or []) if x.strip()}
            if incluir:
                endpoints = [ep for ep in endpoints
                             if ep.nombre in incluir or ep.host in incluir or ep.endpoint in incluir]
            for ep in endpoints:
                try:
                    await self._añadir_plc(ep)
                except Exception as exc:  # noqa: BLE001
                    logger.error("No se pudo añadir el PLC %s: %s", ep.endpoint, exc)
            self._persistir()
        else:
            logger.info("autostart_plcs=False: sin descubrimiento automático. "
                        "Agrega PLCs con POST /plcs o POST /discover.")

        if self._settings.discovery_interval and self._settings.discovery_interval > 0:
            self._rescan_task = asyncio.create_task(self._bucle_reescaneo())
        logger.info("PlcManager iniciado con %d PLC(s).", len(self._handlers))

    async def stop(self) -> None:
        self._running = False
        if self._rescan_task is not None:
            self._rescan_task.cancel()
            try:
                await self._rescan_task
            except asyncio.CancelledError:
                pass
        await asyncio.gather(*(h.stop() for h in self._handlers.values()), return_exceptions=True)
        logger.info("PlcManager detenido.")

    def _persistir(self) -> None:
        self._store.guardar(list(self._endpoints.values()))

    async def _añadir_plc(self, ep: EndpointPlc) -> str:
        """Crea driver + handler para un endpoint, lo arranca y devuelve su id."""
        plc_id = plc_id_desde(ep, self._ids_usados)
        settings_plc = self._settings.para_plc(
            endpoint=ep.endpoint, transporte=ep.transporte, rack=ep.rack, slot=ep.slot,
            s7_tags=ep.s7_tags, usuario=ep.usuario, password=ep.password)
        driver = crear_driver(ep.transporte, settings_plc)
        handler = SubscriptionHandler(
            driver=driver, manager=self._manager, settings=settings_plc, plc_id=plc_id,
            endpoint=ep.endpoint, plc_nombre=ep.nombre, transporte=ep.transporte,
            snapshot_global=self.build_snapshot_message, tags_store=self._tags_store)
        self._handlers[plc_id] = handler
        self._endpoints[plc_id] = ep
        await handler.start()
        logger.info("PLC añadido: id=%s transporte=%s endpoint=%s", plc_id, ep.transporte, ep.endpoint)
        return plc_id

    # ------------------------------------------------------------------ #
    # Gestión en caliente (REST)
    # ------------------------------------------------------------------ #
    @staticmethod
    def construir_endpoint(host: str, puerto: int = PUERTO_OPCUA, transporte: str = "",
                           rack: int = 0, slot: int = 0, s7_tags: str = "",
                           usuario: str = "", password: str = "", nombre: str = "",
                           settings: Optional[Settings] = None) -> EndpointPlc:
        """
        Normaliza lo que manda el cliente (IP pelada, `opc.tcp://`, `s7://`)
        a un EndpointPlc. Lanza ValueError con un mensaje accionable.
        """
        from app.drivers.s7comm_driver import PUERTO_S7, parsear_endpoint_s7, parsear_tags

        host = (host or "").strip()
        if not host:
            raise ValueError("Indica una IP o endpoint.")
        transporte = (transporte or "").strip().lower()
        if host.startswith("s7://"):
            transporte = "s7comm"
        elif host.startswith("opc.tcp://"):
            transporte = "opcua"
        if not transporte:
            transporte = (settings.siemens_transporte if settings else "opcua") or "opcua"
        if transporte not in ("opcua", "s7comm"):
            raise ValueError(f"Transporte desconocido '{transporte}'. Usa: opcua | s7comm.")

        if transporte == "s7comm":
            if host.startswith("s7://"):
                host_solo, r, s = parsear_endpoint_s7(host)
                rack, slot = (rack or r), (slot or s)
            else:
                host_solo = host_de(host)
            rack = int(rack or 0)
            slot = int(slot) if slot else int(settings.s7_slot if settings else 1)
            lista = parsear_tags(s7_tags)   # ValueError con número de línea
            if not lista:
                raise ValueError(
                    "S7comm necesita la lista de tags (nombre;DB1;offset;TIPO, una por "
                    "línea): no puede descubrirlos solo como el OPC UA. Calcúlala con "
                    "POST /siemens/importar a partir de las fuentes de TIA.")
            return EndpointPlc(endpoint=f"s7://{host_solo}:{PUERTO_S7}/{rack}/{slot}",
                               host=host_solo, port=PUERTO_S7, nombre=nombre, origen="manual",
                               transporte="s7comm", rack=rack, slot=slot, s7_tags=s7_tags)

        if host.startswith("opc.tcp://"):
            endpoint = host
            host_solo, puerto = host_de(host), puerto_de(host, PUERTO_OPCUA)
        else:
            host_solo = host_de(host)
            puerto = int(puerto or PUERTO_OPCUA)
            endpoint = f"opc.tcp://{host_solo}:{puerto}"
        return EndpointPlc(endpoint=endpoint, host=host_solo, port=puerto, nombre=nombre,
                           origen="manual", transporte="opcua",
                           usuario=usuario or "", password=password or "")

    async def add_plc(self, **campos) -> dict:
        """Añade un PLC en caliente. Devuelve {ok, plc_id, endpoint, transporte, mensaje}."""
        try:
            ep = self.construir_endpoint(settings=self._settings, **campos)
        except ValueError as exc:
            return {"ok": False, "mensaje": str(exc)}

        for pid, h in self._handlers.items():
            if h.endpoint == ep.endpoint:
                return {"ok": False, "plc_id": pid, "endpoint": ep.endpoint,
                        "mensaje": f"Ese PLC ya está gestionado (id={pid})."}

        plc_id = await self._añadir_plc(ep)
        self._persistir()
        await self._manager.broadcast(self.build_snapshot_message())
        return {"ok": True, "plc_id": plc_id, "endpoint": ep.endpoint,
                "transporte": ep.transporte,
                "mensaje": f"PLC {plc_id} ({ep.transporte}) añadido; conectando..."}

    async def update_plc(self, plc_id: str, **campos) -> dict:
        """
        Reemplaza la configuración de un PLC (p. ej. su lista de tags S7comm)
        conservando el id: se para el handler y se crea otro con la nueva.
        """
        ep_actual = self._endpoints.get(plc_id)
        if ep_actual is None:
            return {"ok": False, "mensaje": f"No existe el PLC '{plc_id}'."}
        base = ep_actual.publico()
        base["password"] = ep_actual.password
        base["puerto"] = base.pop("port")
        base.pop("endpoint", None)
        base.pop("origen", None)
        base.update({k: v for k, v in campos.items() if v is not None})
        try:
            ep = self.construir_endpoint(settings=self._settings, **base)
        except ValueError as exc:
            return {"ok": False, "mensaje": str(exc)}

        handler = self._handlers.pop(plc_id)
        try:
            await handler.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error deteniendo handler %s: %s", plc_id, exc)
        self._ids_usados.discard(plc_id)
        self._endpoints.pop(plc_id, None)
        ep.nombre = ep.nombre or ep_actual.nombre
        # Se conserva el mismo id a propósito: los clientes lo tienen enlazado.
        self._ids_usados.add(plc_id)
        settings_plc = self._settings.para_plc(
            endpoint=ep.endpoint, transporte=ep.transporte, rack=ep.rack, slot=ep.slot,
            s7_tags=ep.s7_tags, usuario=ep.usuario, password=ep.password)
        nuevo = SubscriptionHandler(
            driver=crear_driver(ep.transporte, settings_plc), manager=self._manager,
            settings=settings_plc, plc_id=plc_id, endpoint=ep.endpoint, plc_nombre=ep.nombre,
            transporte=ep.transporte, snapshot_global=self.build_snapshot_message,
            tags_store=self._tags_store)
        self._handlers[plc_id] = nuevo
        self._endpoints[plc_id] = ep
        await nuevo.start()
        self._persistir()
        await self._manager.broadcast(self.build_snapshot_message())
        return {"ok": True, "plc_id": plc_id, "endpoint": ep.endpoint,
                "transporte": ep.transporte, "mensaje": f"PLC {plc_id} actualizado; reconectando..."}

    async def remove_plc(self, plc_id: str) -> dict:
        handler = self._handlers.pop(plc_id, None)
        if handler is None:
            return {"ok": False, "mensaje": f"No existe el PLC '{plc_id}'."}
        try:
            await handler.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error deteniendo handler %s: %s", plc_id, exc)
        self._ids_usados.discard(plc_id)
        self._endpoints.pop(plc_id, None)
        self._persistir()
        self._tags_store.borrar(plc_id)
        await self._manager.broadcast({"timestamp": _ahora_iso(), "type": "plc_removed",
                                       "plc_removed": plc_id})
        logger.info("PLC eliminado: id=%s", plc_id)
        return {"ok": True, "plc_id": plc_id, "mensaje": f"PLC {plc_id} eliminado."}

    async def reconnect_plc(self, plc_id: str) -> dict:
        """Fuerza una reconexión (parar y arrancar el handler) sin tocar la configuración."""
        ep = self._endpoints.get(plc_id)
        if ep is None:
            return {"ok": False, "mensaje": f"No existe el PLC '{plc_id}'."}
        return await self.update_plc(plc_id)

    async def rescan(self) -> dict:
        """Escanea la red una vez y añade los PLCs OPC UA nuevos que encuentre."""
        endpoints = await descubrir_plcs(self._settings, escanear=True)
        existentes = {h.endpoint for h in self._handlers.values()}
        nuevos: List[str] = []
        for ep in endpoints:
            if ep.endpoint not in existentes:
                nuevos.append(await self._añadir_plc(ep))
        if nuevos:
            self._persistir()
            await self._manager.broadcast(self.build_snapshot_message())
        return {"ok": True, "encontrados": len(endpoints), "nuevos": nuevos,
                "mensaje": (f"{len(nuevos)} PLC(s) nuevo(s) añadido(s)." if nuevos
                            else "Sin PLCs nuevos en la red.")}

    async def _bucle_reescaneo(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._settings.discovery_interval)
                if self._running:
                    await self.rescan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error en re-escaneo: %s", exc)

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #
    def handler(self, plc_id: str) -> Optional[SubscriptionHandler]:
        return self._handlers.get(plc_id)

    def list_plc_ids(self) -> List[str]:
        return list(self._handlers.keys())

    def list_plcs(self) -> List[dict]:
        """Configuración + estado de cada PLC (sin contraseñas)."""
        salida = []
        for plc_id, h in self._handlers.items():
            ep = self._endpoints.get(plc_id)
            salida.append({"plc_id": plc_id, **(ep.publico() if ep else {}), **h.health()})
        return salida

    def num_plcs(self) -> int:
        return len(self._handlers)

    def build_snapshot_message(self, plc: Optional[str] = None) -> dict:
        tags: Dict[str, dict] = {}
        plcs: Dict[str, dict] = {}
        for plc_id, h in self._handlers.items():
            if plc and plc_id != plc:
                continue
            tags.update(h.snapshot_entries())
            hl = h.health()
            plcs[plc_id] = {
                "nombre": h.plc_nombre, "transporte": h.transporte, "endpoint": h.endpoint,
                "estado": h.estado_conexion, "conectado": h.is_plc_connected(),
                "modelo": hl["modelo"], "modo_lectura": hl["modo_lectura"],
                "sampling_interval_ms": hl["sampling_interval_ms"],
                "publishing_interval_ms": hl["publishing_interval_ms"],
            }
        return {"timestamp": _ahora_iso(), "type": "snapshot", "plcs": plcs, "tags": tags}

    def get_tags(self, plc: Optional[str] = None) -> List[dict]:
        salida: List[dict] = []
        for plc_id, h in self._handlers.items():
            if plc and plc_id != plc:
                continue
            salida.extend(h.get_tags_con_valor())
        return salida

    def get_browse(self, plc: Optional[str] = None) -> dict:
        return {"timestamp": _ahora_iso(),
                "plcs": [h.get_browse_tree() for plc_id, h in self._handlers.items()
                         if not plc or plc_id == plc]}

    def get_health(self) -> dict:
        plcs = [h.health() for h in self._handlers.values()]
        return {
            "status": "ok",
            "num_plcs": len(plcs),
            "plcs_conectados": sum(1 for p in plcs if p["conectado"]),
            "total_tags": sum(p["num_tags"] for p in plcs),
            "clientes_ws": self._manager.count(),
            "plcs": plcs,
        }

    # ------------------------------------------------------------------ #
    # Escritura en el PLC
    # ------------------------------------------------------------------ #
    async def escribir_tags(self, escrituras: List[dict], usuario: str = "",
                            permitidos=None) -> dict:
        """
        Escribe uno o varios tags, con vuelta atrás si alguno falla.

        1. VALIDAR TODO antes de tocar el PLC (tag existe, PLC conectado,
           tipo correcto, lista blanca y rangos). Si el séptimo valor de una
           receta se sale de rango, no puede haber escrito ya los seis primeros.
        2. LEER los valores actuales: sin esto no hay a dónde volver.
        3. ESCRIBIR en orden, anotando lo hecho.
        4. Si algo falla, RESTAURAR en orden INVERSO.

        No es una transacción: entre la escritura y la vuelta atrás el
        programa del PLC pudo actuar. Lo que de verdad protege es la lista
        blanca y los rangos: no haber escrito nunca un valor imposible.
        """
        if not escrituras:
            raise ValueError("No se indicó ningún tag que escribir.")

        plan: List[dict] = []
        for i, e in enumerate(escrituras):
            plc_id = (e.get("plc_id") or "").strip()
            tag = (e.get("tag") or "").strip()
            valor = e.get("valor")
            if not plc_id or not tag:
                raise ValueError(f"Entrada {i}: hacen falta 'plc_id' y 'tag'.")
            handler = self._handlers.get(plc_id)
            if handler is None:
                raise KeyError(f"No hay ningún PLC con id '{plc_id}'. Los gestionados son: "
                               f"{', '.join(self.list_plc_ids()) or '(ninguno)'}.")
            if not handler.soporta_escritura():
                raise NotImplementedError(f"El PLC '{plc_id}' usa un driver que no sabe escribir.")
            if not handler.is_plc_connected():
                raise ConnectionError(f"El PLC '{plc_id}' no está conectado ahora mismo.")
            info = handler.buscar_tag(tag)
            if info is None:
                raise KeyError(f"El tag '{tag}' no existe en el PLC '{plc_id}'. "
                               f"Comprueba el nombre con GET /tags?plc={plc_id}.")
            valor_convertido = convertir(valor, info.data_type)
            if permitidos is not None:
                permitidos.validar(plc_id, info.full_name, valor_convertido)
            plan.append({"plc_id": plc_id, "handler": handler, "info": info,
                         "valor": valor_convertido, "pedido": valor})

        for p in plan:
            try:
                p["anterior"] = (await p["handler"].leer(p["info"].node_id)).value
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo leer el valor previo de %s: %s", p["info"].full_name, exc)
                p["anterior"] = None

        hechas: List[dict] = []
        resultados: List[dict] = []
        for p in plan:
            try:
                tv = await p["handler"].escribir(p["info"].node_id, p["valor"])
                hechas.append(p)
                resultados.append({
                    "plc_id": p["plc_id"], "tag": p["info"].full_name,
                    "solicitado": p["pedido"], "escrito": p["valor"],
                    "confirmado": tv.value if tv else None,
                    "data_type": p["info"].data_type, "anterior": p["anterior"],
                    "coincide": (tv is not None and tv.value == p["valor"]),
                    "ok": True,
                })
            except Exception as exc:  # noqa: BLE001
                revertidos, fallos_reversion = [], []
                for h in reversed(hechas):
                    if h["anterior"] is None:
                        fallos_reversion.append(f"{h['info'].full_name} (sin valor previo)")
                        continue
                    try:
                        await h["handler"].escribir(h["info"].node_id, h["anterior"])
                        revertidos.append(h["info"].full_name)
                    except Exception as exc2:  # noqa: BLE001
                        fallos_reversion.append(f"{h['info'].full_name}: {exc2}")
                logger.error("Escritura fallida en %s (%s). Revertidos: %s. Sin revertir: %s",
                             p["info"].full_name, exc, revertidos or "ninguno",
                             fallos_reversion or "ninguno")
                return {"ok": False, "escrituras": len(escrituras),
                        "fallo_en": p["info"].full_name, "motivo": str(exc),
                        "revertidos": revertidos, "sin_revertir": fallos_reversion,
                        "resultados": resultados, "usuario": usuario, "timestamp": _ahora_iso()}

        logger.info("%s escribió %d tag(s) correctamente.", usuario or "?", len(resultados))
        return {"ok": True, "escrituras": len(resultados), "resultados": resultados,
                "usuario": usuario, "timestamp": _ahora_iso()}
