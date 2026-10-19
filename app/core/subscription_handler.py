# -*- coding: utf-8 -*-
"""
subscription_handler.py
=======================
Orquesta el ciclo de vida de la conexión con UN PLC y el puente hacia los
clientes WebSocket. Hay una instancia por PLC; el PlcManager las agrega.

  * Conecta el driver, obtiene el catálogo de tags y crea la subscription.
  * Recibe los cambios del driver (callback) y los difunde etiquetados con
    `plc_id`, manteniendo un snapshot en memoria (último valor por tag).
  * Supervisa la conexión (watchdog) y, ante caídas, reconecta con backoff
    exponencial recreando la subscription.
  * RECONCILIA cada pocos segundos lo que dice la subscription con lo que
    hay de verdad en el PLC (lectura en bloque), por si la subscription se
    queda muda sin que la sesión se caiga.

Trabaja contra la interfaz `PlcDriver`, no contra un protocolo concreto:
OPC UA y S7comm pasan por aquí sin distinción.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from app.config.settings import Settings
from app.core.connection_manager import ConnectionManager
from app.drivers.plc_driver import PlcDriver, TagInfo, TagValue

logger = logging.getLogger("subscription_handler")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubscriptionHandler:
    """Puente de UN PLC hacia el WebSocket, con reconexión automática."""

    def __init__(self, driver: PlcDriver, manager: ConnectionManager, settings: Settings,
                 plc_id: str, endpoint: str = "", plc_nombre: str = "",
                 transporte: str = "opcua",
                 snapshot_global: Optional[Callable[[], dict]] = None,
                 tags_store=None) -> None:
        self._driver = driver
        self._manager = manager
        self._settings = settings
        self._snapshot_global = snapshot_global

        self.plc_id = plc_id
        self.endpoint = endpoint
        self.plc_nombre = plc_nombre
        self.transporte = transporte

        self._snapshot: Dict[str, TagValue] = {}
        self._tags_store = tags_store
        self._tags: List[TagInfo] = []
        if tags_store is not None:
            try:
                self._tags = tags_store.cargar(plc_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] No se pudo cargar el catálogo de tags: %s", plc_id, exc)
        self._avisado_conectado: bool = False

        self._running: bool = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self.estado_conexion: str = "desconectado"
        self.ultimo_error: str = ""
        self.conectado_desde: str = ""

        # Vigilancia de que la subscription ENTREGA.
        self._ultimo_dato_sub: float = 0.0
        self._cambios_sin_sub: int = 0
        self._ultima_vuelta: float = 0.0
        # Contadores para /health.
        self.mensajes_emitidos: int = 0
        self.reconexiones: int = 0

    def _log(self, nivel: int, msg: str, *args) -> None:
        logger.log(nivel, f"[{self.plc_id}] " + msg, *args)

    # ------------------------------------------------------------------ #
    # Arranque / parada
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor())
        self._log(logging.INFO, "Handler iniciado (endpoint=%s).", self.endpoint)

    async def stop(self) -> None:
        self._running = False
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
        try:
            await self._driver.disconnect()
        except Exception as exc:  # noqa: BLE001
            self._log(logging.WARNING, "Error al cerrar el driver: %s", exc)
        self.estado_conexion = "desconectado"
        self._log(logging.INFO, "Handler detenido.")

    # ------------------------------------------------------------------ #
    # Callback de cambios (lo invoca el driver)
    # ------------------------------------------------------------------ #
    async def on_data_change(self, tag_value: TagValue) -> None:
        self._ultimo_dato_sub = time.monotonic()
        await self._difundir_valor(tag_value)

    async def _difundir_valor(self, tag_value: TagValue) -> None:
        self._snapshot[tag_value.tag] = tag_value
        self.mensajes_emitidos += 1
        await self._manager.broadcast({
            "timestamp": tag_value.timestamp,
            "plc": self.plc_id,
            "tag": tag_value.tag,
            "value": tag_value.value,
            "type": tag_value.data_type,
            "source_ts": tag_value.source_ts,
            "server_ts": tag_value.server_ts,
            "delta_ms": tag_value.delta_ms,
        })

    # ------------------------------------------------------------------ #
    # Estado expuesto (lo consume el PlcManager)
    # ------------------------------------------------------------------ #
    def _entrada(self, info: Optional[TagInfo], fv: Optional[TagValue]) -> dict:
        return {
            "plc": self.plc_id,
            "tag": fv.tag if fv else info.full_name,
            "value": fv.value if fv else None,
            "type": fv.data_type if fv else info.data_type,
            "timestamp": fv.timestamp if fv else None,
            "source_ts": fv.source_ts if fv else None,
            "server_ts": fv.server_ts if fv else None,
            "delta_ms": fv.delta_ms if fv else None,
        }

    def snapshot_entries(self) -> Dict[str, dict]:
        """Entradas '<plc_id>|<tag>' -> valor. Sin conexión, los valores van a None."""
        salida: Dict[str, dict] = {}
        en_linea = self._en_linea()
        for info in self._tags:
            fv = self._snapshot.get(info.full_name) if en_linea else None
            if fv is None:
                salida[f"{self.plc_id}|{info.full_name}"] = self._entrada(info, None)
        if not en_linea:
            return salida
        for fv in self._snapshot.values():
            salida[f"{self.plc_id}|{fv.tag}"] = self._entrada(None, fv)
        return salida

    def get_tags_con_valor(self) -> List[dict]:
        salida: List[dict] = []
        en_linea = self._en_linea()
        for info in self._tags:
            fv = self._snapshot.get(info.full_name) if en_linea else None
            salida.append({
                "plc": self.plc_id, "tag": info.full_name, "name": info.name,
                "db": info.db_name, "node_id": info.node_id, "type": info.data_type,
                "value": fv.value if fv else None,
                "timestamp": fv.timestamp if fv else None,
                "source_ts": fv.source_ts if fv else None,
                "delta_ms": fv.delta_ms if fv else None,
            })
        return salida

    def get_browse_tree(self) -> dict:
        arbol: Dict[str, List[dict]] = {}
        for info in self._tags:
            arbol.setdefault(info.db_name, []).append({
                "name": info.name, "full_name": info.full_name,
                "node_id": info.node_id, "type": info.data_type})
        return {"plc": self.plc_id, "nombre": self.plc_nombre, "transporte": self.transporte,
                "endpoint": self.endpoint, "data_blocks": arbol}

    def health(self) -> dict:
        return {
            "plc": self.plc_id,
            "nombre": self.plc_nombre,
            "transporte": self.transporte,
            "endpoint": self.endpoint,
            "conectado": self._driver.is_connected(),
            "estado_conexion": self.estado_conexion,
            "conectado_desde": self.conectado_desde,
            "num_tags": len(self._tags),
            "modo_lectura": getattr(self._driver, "modo_lectura", "subscription"),
            "modelo": getattr(self._driver, "modelo", "") or "",
            "ultimo_error": self.ultimo_error,
            "reconexiones": self.reconexiones,
            "mensajes_emitidos": self.mensajes_emitidos,
            "sampling_interval_ms": (self._settings.s7_poll_interval_ms
                                     if self.transporte == "s7comm"
                                     else self._settings.sampling_interval_ms),
            "publishing_interval_ms": self._settings.publishing_interval_ms,
        }

    # ------------------------------------------------------------------ #
    # Lectura puntual y escritura (lo usa PlcManager.escribir_tags)
    # ------------------------------------------------------------------ #
    def buscar_tag(self, nombre: str) -> Optional[TagInfo]:
        """Por `full_name` o por `node_id`: son los dos vocabularios reales del sistema."""
        if not nombre:
            return None
        for t in self._tags:
            if t.full_name == nombre or t.node_id == nombre:
                return t
        return None

    @property
    def tags(self) -> List[TagInfo]:
        return list(self._tags)

    def soporta_escritura(self) -> bool:
        return self._driver.soporta_escritura()

    def is_plc_connected(self) -> bool:
        return self._driver.is_connected()

    async def leer(self, node_id: str) -> TagValue:
        """Lee AHORA, sin esperar a la siguiente notificación (para poder deshacer)."""
        if not self._driver.is_connected():
            raise ConnectionError(f"El PLC '{self.plc_id}' ({self.endpoint}) no está conectado.")
        return await self._driver.read_tag(node_id)

    async def escribir(self, node_id: str, valor: object) -> TagValue:
        """Escribe y actualiza el snapshot con lo RELEÍDO, no con lo pedido."""
        if not self._driver.is_connected():
            raise ConnectionError(f"El PLC '{self.plc_id}' ({self.endpoint}) no está conectado.")
        tv = await self._driver.write_tag(node_id, valor)
        if tv is not None:
            self._snapshot[tv.tag] = tv
        return tv

    def num_tags(self) -> int:
        return len(self._tags)

    def _en_linea(self) -> bool:
        return self._driver.is_connected() and self.estado_conexion != "reconectando"

    async def _avisar_caida(self) -> None:
        if not self._avisado_conectado:
            return
        self._avisado_conectado = False
        try:
            await self._manager.broadcast({"timestamp": _ahora_iso(), "type": "status",
                                           "plc": self.plc_id, "status": "reconectando",
                                           "error": self.ultimo_error})
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "No se pudo avisar de la caída: %s", exc)

    def _fijar_catalogo(self, tags: List[TagInfo]) -> None:
        """Adopta un browse CORRECTO como catálogo. Reemplaza, no mezcla."""
        self._tags = tags
        nombres = {t.full_name for t in tags}
        for clave in [k for k in self._snapshot if k not in nombres]:
            del self._snapshot[clave]
        if self._tags_store is not None:
            try:
                self._tags_store.guardar(self.plc_id, tags)
            except Exception as exc:  # noqa: BLE001
                self._log(logging.WARNING, "No se pudo guardar el catálogo de tags: %s", exc)

    # ------------------------------------------------------------------ #
    # Supervisor: conexión + reconexión con backoff exponencial
    # ------------------------------------------------------------------ #
    async def _supervisor(self) -> None:
        delay = self._settings.reconnect_initial_delay
        while self._running:
            try:
                self.estado_conexion = "conectando"
                self._log(logging.INFO, "Intentando conectar con el PLC...")
                await self._conectar_y_suscribir()
                delay = self._settings.reconnect_initial_delay
                self.estado_conexion = "conectado"
                self.ultimo_error = ""
                self.conectado_desde = _ahora_iso()
                self._log(logging.INFO, "PLC conectado y suscripción activa.")
                await self._vigilar_conexion()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.estado_conexion = "reconectando"
                self.ultimo_error = f"{type(exc).__name__}: {exc}"[:400]
                self._log(logging.ERROR, "Fallo de conexión con el PLC: %s", exc)
                await self._avisar_caida()

            if not self._running:
                break
            self.reconexiones += 1
            self._log(logging.INFO, "Reintentando en %.1f s...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * self._settings.reconnect_backoff_factor,
                        self._settings.reconnect_max_delay)

    async def _conectar_y_suscribir(self) -> None:
        """conectar -> browse -> subscribe -> snapshot inicial -> avisar."""
        try:
            await self._driver.disconnect()
        except Exception:  # noqa: BLE001
            pass

        await self._driver.connect()
        self._fijar_catalogo(await self._driver.browse_tags())
        self._log(logging.INFO, "Catálogo: %d tags.", len(self._tags))

        await self._driver.subscribe(self._tags, self.on_data_change)
        self._ultimo_dato_sub = time.monotonic()
        self._ultima_vuelta = self._ultimo_dato_sub
        self._cambios_sin_sub = 0

        await self._cargar_snapshot_inicial()

        # Difundir el snapshot GLOBAL a las vistas ya abiertas: la mayoría de
        # tags de un Siemens real son consignas que no cambian en toda la
        # jornada, y sin esto solo se verían al cambiar de valor.
        if self._snapshot_global is not None:
            try:
                await self._manager.broadcast(self._snapshot_global())
            except Exception as exc:  # noqa: BLE001
                self._log(logging.WARNING, "No se pudo difundir el snapshot al conectar: %s", exc)

        await self._manager.broadcast({"timestamp": _ahora_iso(), "type": "status",
                                       "plc": self.plc_id, "status": "conectado"})
        self._avisado_conectado = True

    async def _cargar_snapshot_inicial(self) -> None:
        """Una lectura en bloque para tener valores sin esperar al primer cambio."""
        if not self._tags:
            return
        try:
            leidos = await self._driver.read_tags([t.node_id for t in self._tags])
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "Snapshot inicial no disponible: %s", exc)
            return
        for info, tv in zip(self._tags, leidos):
            if tv is not None:
                self._snapshot[info.full_name] = tv

    async def _vigilar_conexion(self) -> None:
        """Watchdog: comprueba la sesión y reconcilia; al detectar caída, sale."""
        while self._running:
            await asyncio.sleep(self._settings.healthcheck_interval)
            try:
                alive = await self._driver.check_alive()
            except Exception:  # noqa: BLE001
                alive = False
            if not alive:
                self.estado_conexion = "reconectando"
                self.ultimo_error = "Watchdog: el PLC dejó de responder."
                self._log(logging.WARNING, "Watchdog: conexión perdida, se forzará reconexión.")
                self._avisado_conectado = True
                await self._avisar_caida()
                return
            await self._reconciliar()

    async def _reconciliar(self) -> None:
        """
        Red de seguridad de la subscription: lee TODOS los tags en bloque y
        difunde los que no coincidan con el snapshot. Si acumula DOS cambios
        que solo vio la lectura directa sin que la subscription entregara
        nada entre medias —o el driver avisa de que el servidor la dio por
        muerta— la recrea sin tirar la conexión.
        """
        if not self._tags:
            return
        inicio = time.monotonic()
        entrego_desde_la_ultima = self._ultimo_dato_sub >= self._ultima_vuelta
        self._ultima_vuelta = inicio

        try:
            leidos = await self._driver.read_tags([t.node_id for t in self._tags])
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "Reconciliación sin lectura: %s", exc)
            return

        diferencias = 0
        for info, tv in zip(self._tags, leidos):
            if tv is None:
                continue
            previo = self._snapshot.get(info.full_name)
            if previo is not None and previo.value == tv.value:
                continue
            diferencias += 1
            await self._difundir_valor(tv)
        if diferencias:
            self._log(logging.INFO, "Reconciliación: %d tag(s) habían cambiado sin pasar "
                                    "por la subscription.", diferencias)

        viva = self._driver.subscription_viva()
        if entrego_desde_la_ultima:
            self._cambios_sin_sub = 0
        self._cambios_sin_sub += diferencias
        if viva and self._cambios_sin_sub < 2:
            return

        self._log(logging.WARNING, "La subscription no entrega cambios (%s). Se recrea.",
                  "el servidor la dio por caducada" if not viva
                  else f"{self._cambios_sin_sub} cambios solo vistos por lectura directa")
        self._cambios_sin_sub = 0
        await self._driver.subscribe(self._tags, self.on_data_change)
        self._ultimo_dato_sub = time.monotonic()
