# -*- coding: utf-8 -*-
"""
opcua_driver.py
===============
Driver Siemens por **OPC UA** con la librería `asyncua` (async/await).

  ┌────────────────────┬───────────────────────────────────────────────────┐
  │ PLCs               │ S7-1500 (siempre). S7-1200 solo con FW >= 4.4 y   │
  │                    │ el servidor OPC UA ACTIVADO y licenciado.         │
  │ Transporte         │ opc.tcp://host:4840                               │
  │ Autenticación      │ Anónima por defecto; usuario/contraseña y         │
  │                    │ certificado opcionales (settings).                │
  │ Descubrir tags     │ Browse recursivo de DeviceSet/PLC_x/DataBlocksGlobal│
  │ Tiempo real        │ Subscription nativa (MonitoredItems). Sin polling.│
  │ Lectura en bloque  │ Un `Read` por lotes de 250 nodos (reconciliación) │
  │ Escritura          │ `write_value` con el Variant que declara el server│
  │                    │ y relectura para confirmar.                       │
  └────────────────────┴───────────────────────────────────────────────────┘

EN TIA PORTAL
  * CPU → Propiedades → OPC UA → Servidor → ☑ Activar servidor OPC UA.
  * En cada DB: los miembros deben estar marcados «Accesible desde OPC UA»
    (y «Escribible desde OPC UA» si se van a escribir).
  * Licencia runtime OPC UA cargada (S7-1200: SIMATIC OPC UA S7-1200 Basic).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from asyncua import Client, Node, ua

from app.config.settings import Settings
from app.drivers.escritura import convertir
from app.drivers.plc_driver import DataChangeCallback, PlcDriver, TagInfo, TagValue

logger = logging.getLogger("opcua_driver")

# Nodos por petición `Read` en `read_tags`. Muy por debajo del límite habitual
# del S7-1500 (MaxNodesPerRead ≈ 1000) para no rozarlo nunca.
LOTE_LECTURA = 250

# Nombres de nodos internos / metadata que NO son variables de datos reales.
NODOS_IGNORADOS = {"Icon", "InputArguments", "OutputArguments"}


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _a_serializable(valor: object) -> object:
    """
    Convierte el valor a algo que `json.dumps` acepte.

    Los ARRAYS del PLC llegan como lista y se dejan como lista. Un UDT ya
    decodificado por asyncua (`load_data_type_definitions`) se convierte en
    dict. Un ExtensionObject SIN decodificar no sirve en un cliente: None.
    """
    if isinstance(valor, (bool, int, float, str)) or valor is None:
        return valor
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, (bytes, bytearray)):
        return None
    if isinstance(valor, (list, tuple)):
        return [_a_serializable(v) for v in valor]
    if isinstance(valor, dict):
        return {str(k): _a_serializable(v) for k, v in valor.items()}
    if isinstance(valor, ua.ExtensionObject):
        return None
    campos = getattr(valor, "__dict__", None)
    if isinstance(campos, dict) and campos:
        return {k: _a_serializable(v) for k, v in campos.items() if not k.startswith("_")}
    return str(valor)


def _es_struct_crudo(valor: object) -> bool:
    """¿Es un UDT que no se ha podido decodificar (o una lista de ellos)?"""
    if isinstance(valor, ua.ExtensionObject):
        return True
    if isinstance(valor, (list, tuple)) and valor:
        return isinstance(valor[0], ua.ExtensionObject)
    return False


class _SubHandler:
    """
    Handler interno de asyncua para las subscriptions.

    En cada `datachange_notification` extrae el valor y las marcas de tiempo
    reales (SourceTimestamp/ServerTimestamp), calcula `delta_ms` respecto al
    cambio anterior del mismo tag y reenvía al callback del handler.
    """

    def __init__(self, driver: "OpcUaDriver", callback: DataChangeCallback) -> None:
        self._driver = driver
        self._callback = callback

    async def datachange_notification(self, node: Node, val, data) -> None:
        try:
            node_id = node.nodeid.to_string()
            info = self._driver.tag_por_nodeid.get(node_id)
            if info is None:
                return

            source_dt = server_dt = None
            try:
                dv = data.monitored_item.Value
                source_dt = dv.SourceTimestamp
                server_dt = dv.ServerTimestamp
            except Exception:  # noqa: BLE001
                pass

            ahora = datetime.now(timezone.utc)
            ref_dt = source_dt or ahora
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)

            delta_ms: Optional[float] = None
            anterior = self._driver.ultimo_cambio_ts.get(info.full_name)
            if anterior is not None:
                delta_ms = (ref_dt - anterior).total_seconds() * 1000.0
            self._driver.ultimo_cambio_ts[info.full_name] = ref_dt

            tv = TagValue(
                tag=info.full_name,
                value=_a_serializable(val),
                data_type=info.data_type,
                timestamp=ahora.isoformat(),
                node_id=node_id,
                source_ts=_dt_iso(source_dt),
                server_ts=_dt_iso(server_dt),
                delta_ms=round(delta_ms, 1) if delta_ms is not None else None,
            )
            self._driver.ultimos_valores[info.full_name] = tv
            await self._callback(tv)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error procesando datachange: %s", exc)

    def status_change_notification(self, status) -> None:
        """
        El servidor da la subscription por caducada (BadTimeout tras una
        pausa del equipo, p. ej.) dejando la SESIÓN viva: el watchdog de
        sesión no ve nada raro. Se marca aquí y el SubscriptionHandler la
        recrea en su siguiente vuelta.
        """
        logger.warning("La subscription cambió de estado: %s. Se recreará.", status)
        self._driver.subscription_caida = True


class OpcUaDriver(PlcDriver):
    """Driver OPC UA basado en asyncua."""

    modo_lectura = "subscription"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.modelo: str = ""
        self._client: Optional[Client] = None
        self._subscription = None
        self._handles: List[int] = []
        self._connected: bool = False
        self._ns_index: Optional[int] = None
        self.subscription_caida: bool = False

        # Estado compartido con el handler.
        self.tag_por_nodeid: Dict[str, TagInfo] = {}
        self.ultimos_valores: Dict[str, TagValue] = {}
        self.tags_descubiertos: List[TagInfo] = []
        self.ultimo_cambio_ts: Dict[str, datetime] = {}

    @property
    def endpoint(self) -> str:
        return self._settings.opcua_endpoint

    # ==================================================================== #
    # Conexión
    # ==================================================================== #
    async def connect(self) -> None:
        # `timeout` es el de CADA petición (Read, Publish, CreateSubscription…):
        # es lo que acota cuánto tarda el watchdog en notar un PLC apagado.
        self._client = Client(url=self.endpoint,
                              timeout=float(self._settings.opcua_request_timeout))

        if self._settings.security_policy and self._settings.security_mode:
            try:
                cadena = (f"{self._settings.security_policy},"
                          f"{self._settings.security_mode},"
                          f"{self._settings.client_cert_path},"
                          f"{self._settings.client_private_key_path}")
                if self._settings.server_cert_path:
                    cadena += f",{self._settings.server_cert_path}"
                await self._client.set_security_string(cadena)
                logger.info("Seguridad OPC UA aplicada (%s).", self._settings.security_policy)
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudo aplicar seguridad OPC UA: %s", exc)

        if self._settings.opcua_username:
            self._client.set_user(self._settings.opcua_username)
            if self._settings.opcua_password:
                self._client.set_password(self._settings.opcua_password)

        try:
            await asyncio.wait_for(self._client.connect(),
                                   timeout=float(self._settings.opcua_connect_timeout) + 2)
        except Exception as exc:  # noqa: BLE001
            # Un S7-1200 sin servidor OPC UA cae aquí con un "Connection
            # refused" que no orienta a nadie. Se dice lo que casi siempre es.
            raise ConnectionError(
                f"No se pudo abrir sesión OPC UA en {self.endpoint}: {exc}. "
                f"Si es un S7-1200, su servidor OPC UA solo existe desde FW 4.4 "
                f"y hay que activarlo (Propiedades → OPC UA → Servidor); si no, "
                f"conecta por S7comm (puerto 102). Usa POST /siemens/identificar "
                f"para ver el modelo.") from exc
        self._connected = True
        await self._leer_modelo()

        # Definiciones de los UDT del PLC: con esto un `Array of "UDT_x"`
        # llega decodificado campo a campo. S7-1500 las publica desde FW 2.5.
        try:
            await self._client.load_data_type_definitions()
            logger.debug("Definiciones de tipos (UDT) cargadas del servidor.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Sin definiciones de UDT del servidor: %s", exc)

        try:
            self._ns_index = await self._client.get_namespace_index(
                self._settings.opcua_namespace_uri)
        except Exception:  # noqa: BLE001
            self._ns_index = 3
            logger.warning("No se pudo resolver el namespace por URI; uso ns=3.")

        logger.info("Conectado a OPC UA en %s (%s)", self.endpoint, self.modelo or "modelo n/d")

    async def disconnect(self) -> None:
        await self._cerrar_subscription()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error al desconectar OPC UA: %s", exc)
        self._connected = False
        self._client = None

    async def _cerrar_subscription(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.delete()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error al eliminar subscription: %s", exc)
        self._subscription = None
        self._handles = []

    def is_connected(self) -> bool:
        return self._connected

    async def check_alive(self) -> bool:
        if self._client is None:
            return False
        try:
            await asyncio.wait_for(self._client.nodes.server_state.read_value(),
                                   timeout=float(self._settings.opcua_request_timeout) + 1)
            return True
        except Exception:  # noqa: BLE001
            self._connected = False
            return False

    # ==================================================================== #
    # Auto-descubrimiento (browse)
    # ==================================================================== #
    async def browse_tags(self) -> List[TagInfo]:
        if self._client is None:
            raise RuntimeError("browse_tags llamado sin conexión activa.")

        objects = self._client.nodes.objects
        # La ruta cruza VARIOS namespaces (DeviceSet=ns2, PLC_x=ns3): se busca
        # por BrowseName, no por NodeId.
        deviceset = await self._buscar_hijo_por_nombre(objects, self._settings.browse_device_set)
        if deviceset is None:
            raise RuntimeError(
                f"No se encontró '{self._settings.browse_device_set}' bajo Objects.")

        plc = await self._buscar_hijo_por_nombre(deviceset, self._settings.browse_plc_name)
        dbs_node = None
        if plc is not None:
            dbs_node = await self._buscar_hijo_por_nombre(plc, self._settings.browse_datablocks_node)

        if dbs_node is None:
            # El PLC no se llama como dice la configuración: se busca en todos.
            for hijo in await deviceset.get_children():
                candidato = await self._buscar_hijo_por_nombre(
                    hijo, self._settings.browse_datablocks_node)
                if candidato is not None:
                    dbs_node = candidato
                    nombre = (await hijo.read_browse_name()).Name
                    logger.info("DataBlocksGlobal encontrado bajo '%s'.", nombre)
                    break

        if dbs_node is None:
            raise RuntimeError("No se pudo localizar 'DataBlocksGlobal' en el servidor OPC UA.")

        filtro_dbs = self._settings.load_db_filter()
        if filtro_dbs:
            logger.info("Filtro de DBs activo: %s", filtro_dbs)

        tags: List[TagInfo] = []
        for db_node in await dbs_node.get_children():
            db_name = (await db_node.read_browse_name()).Name
            if db_name in NODOS_IGNORADOS:
                continue
            if filtro_dbs and db_name not in filtro_dbs:
                continue
            await self._browse_recursivo(db_node, db_name, db_name, tags)

        self.tags_descubiertos = tags
        self.tag_por_nodeid = {t.node_id: t for t in tags}
        logger.info("Descubiertos %d tags en %d Data Blocks.",
                    len(tags), len({t.db_name for t in tags}))
        return tags

    async def _leer_modelo(self) -> None:
        """Modelo de la CPU desde las propiedades DI del nodo del PLC. Solo informativo."""
        self.modelo = ""
        try:
            ds = await self._buscar_hijo_por_nombre(
                self._client.nodes.objects, self._settings.browse_device_set)
            if ds is None:
                return
            plc = await self._buscar_hijo_por_nombre(ds, self._settings.browse_plc_name)
            if plc is None:
                hijos = await ds.get_children()
                plc = hijos[0] if hijos else None
            if plc is None:
                return
            for prop in ("Model", "OrderNumber", "DeviceModel"):
                n = await self._buscar_hijo_por_nombre(plc, prop)
                if n is None:
                    continue
                v = await n.read_value()
                v = getattr(v, "Text", v)
                if v:
                    self.modelo = str(v)
                    return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Sin modelo de CPU por OPC UA: %s", exc)

    async def _buscar_hijo_por_nombre(self, node: Node, nombre: str) -> Optional[Node]:
        try:
            for child in await node.get_children():
                try:
                    if (await child.read_browse_name()).Name == nombre:
                        return child
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error buscando hijo '%s': %s", nombre, exc)
        return None

    async def _browse_recursivo(self, node, db_name, prefijo, acumulador) -> None:
        for child in await node.get_children():
            try:
                nombre = (await child.read_browse_name()).Name
            except Exception:  # noqa: BLE001
                continue
            if nombre in NODOS_IGNORADOS:
                continue
            try:
                node_class = await child.read_node_class()
            except Exception:  # noqa: BLE001
                continue
            if node_class == ua.NodeClass.Variable:
                # Un STRUCT/UDT es una Variable cuyo valor es el struct entero
                # y que ADEMÁS tiene como hijos sus miembros. Si tiene hijos,
                # se entra: lo que se quiere son los campos.
                hijos = await self._hijos_variables(child)
                if hijos:
                    await self._browse_recursivo(child, db_name, f"{prefijo}.{nombre}", acumulador)
                    continue
                await self._registrar_variable(child, db_name, prefijo, nombre, acumulador)
            elif node_class == ua.NodeClass.Object:
                await self._browse_recursivo(child, db_name, f"{prefijo}.{nombre}", acumulador)

    async def _hijos_variables(self, node: Node) -> List[Node]:
        try:
            hijos = await node.get_children()
        except Exception:  # noqa: BLE001
            return []
        salida: List[Node] = []
        for h in hijos:
            try:
                if await h.read_node_class() == ua.NodeClass.Variable:
                    salida.append(h)
            except Exception:  # noqa: BLE001
                continue
        return salida

    async def _registrar_variable(self, child, db_name, prefijo, nombre, acumulador) -> None:
        try:
            data_type = await self._nombre_tipo_dato(child)
            if data_type in ("ByteString", "Image", "ImagePNG"):
                return
            node_id = child.nodeid.to_string()
            full_name = f"{prefijo}.{nombre}" if prefijo != nombre else nombre

            # ¿Array? El ValueRank lo dice sin leer el valor.
            try:
                rank = (await child.read_attribute(ua.AttributeIds.ValueRank)).Value.Value
            except Exception:  # noqa: BLE001
                rank = -1
            if isinstance(rank, int) and rank >= 1:
                try:
                    dims = (await child.read_attribute(ua.AttributeIds.ArrayDimensions)).Value.Value
                    dims = list(dims or [])
                except Exception:  # noqa: BLE001
                    dims = []
                data_type = f"{data_type}[{'x'.join(str(d) for d in dims) if dims else ''}]"

            # Un struct sin miembros expuestos es un ExtensionObject opaco:
            # no se puede enlazar a nada. Se omite y se dice por qué.
            try:
                valor = await child.read_value()
            except Exception:  # noqa: BLE001
                valor = None
            if _es_struct_crudo(valor):
                logger.warning(
                    "Se omite '%s' (%s): estructura que el servidor no expone "
                    "campo a campo. En TIA Portal, marca sus miembros como "
                    "'Accesible desde OPC UA'.", full_name, data_type)
                return

            acumulador.append(TagInfo(name=nombre, full_name=full_name, node_id=node_id,
                                      data_type=data_type, db_name=db_name))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Se omite un nodo durante el browse: %s", exc)

    async def _nombre_tipo_dato(self, node: Node) -> str:
        try:
            dt_node = await node.read_data_type()
            if dt_node.NamespaceIndex == 0 and isinstance(dt_node.Identifier, int):
                nombre = ua.ObjectIdNames.get(dt_node.Identifier)
                if nombre:
                    return nombre
            return (await self._client.get_node(dt_node).read_browse_name()).Name
        except Exception:  # noqa: BLE001
            try:
                return type(await node.read_value()).__name__
            except Exception:  # noqa: BLE001
                return "Unknown"

    # ==================================================================== #
    # Subscriptions en tiempo real
    # ==================================================================== #
    async def subscribe(self, tags: List[TagInfo], callback: DataChangeCallback) -> None:
        if self._client is None:
            raise RuntimeError("subscribe llamado sin conexión activa.")
        if not tags:
            logger.warning("No hay tags para suscribir.")
            return

        await self._cerrar_subscription()
        self.subscription_caida = False

        handler = _SubHandler(self, callback)
        self._subscription = await self._client.create_subscription(
            period=self._settings.publishing_interval_ms, handler=handler)
        nodos = [self._client.get_node(t.node_id) for t in tags]
        muestreo = float(self._settings.sampling_interval_ms)
        try:
            handles = await self._subscription.subscribe_data_change(
                nodos, queuesize=self._settings.subscription_queue_size,
                sampling_interval=muestreo)
            self._handles = handles if isinstance(handles, list) else [handles]
            logger.info("Subscription: %d MonitoredItems (publish=%dms, sampling=%dms).",
                        len(nodos), self._settings.publishing_interval_ms,
                        self._settings.sampling_interval_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Suscripción en lote falló (%s). Suscribo uno a uno.", exc)
            self._handles = []
            for nodo, info in zip(nodos, tags):
                try:
                    h = await self._subscription.subscribe_data_change(
                        nodo, queuesize=self._settings.subscription_queue_size,
                        sampling_interval=muestreo)
                    self._handles.append(h if not isinstance(h, list) else h[0])
                except Exception as e2:  # noqa: BLE001
                    logger.warning("No se pudo suscribir %s: %s", info.full_name, e2)

    def subscription_viva(self) -> bool:
        return not self.subscription_caida

    # ==================================================================== #
    # Lectura puntual y en bloque
    # ==================================================================== #
    async def read_tags(self, node_ids: List[str]) -> List[Optional[TagValue]]:
        """Lee varios tags con UNA petición `Read` por lote de 250 nodos."""
        if self._client is None:
            raise RuntimeError("read_tags llamado sin conexión activa.")
        salida: List[Optional[TagValue]] = []
        ahora = _ahora_iso()
        for i in range(0, len(node_ids), LOTE_LECTURA):
            lote = node_ids[i:i + LOTE_LECTURA]
            try:
                valores = await self._client.read_values(
                    [self._client.get_node(n) for n in lote])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Lectura en bloque fallida (%d nodos): %s", len(lote), exc)
                salida.extend([None] * len(lote))
                continue
            for node_id, valor in zip(lote, valores):
                info = self.tag_por_nodeid.get(node_id)
                salida.append(TagValue(
                    tag=info.full_name if info else node_id,
                    value=_a_serializable(valor),
                    data_type=info.data_type if info else type(valor).__name__,
                    timestamp=ahora, node_id=node_id))
        return salida

    async def read_tag(self, node_id: str) -> TagValue:
        if self._client is None:
            raise RuntimeError("read_tag llamado sin conexión activa.")
        valor = await self._client.get_node(node_id).read_value()
        info = self.tag_por_nodeid.get(node_id)
        return TagValue(
            tag=info.full_name if info else node_id,
            value=_a_serializable(valor),
            data_type=info.data_type if info else type(valor).__name__,
            timestamp=_ahora_iso(), node_id=node_id)

    # ==================================================================== #
    # Escritura
    # ==================================================================== #
    async def write_tag(self, node_id: str, valor: object) -> TagValue:
        """
        Escribe en un tag y devuelve el valor RELEÍDO.

        El tipo se pregunta al servidor (`read_data_type_as_variant_type`)
        en vez de deducirlo del catálogo: el catálogo pudo quedarse viejo si
        alguien recargó el programa, y escribir con un Variant equivocado es
        justo lo que hay que evitar.
        """
        if self._client is None:
            raise RuntimeError("write_tag llamado sin conexión activa.")
        node = self._client.get_node(node_id)
        info = self.tag_por_nodeid.get(node_id)
        nombre = info.full_name if info else node_id

        try:
            tipo_variante = await node.read_data_type_as_variant_type()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"No se pudo leer el tipo de dato de '{nombre}' en el PLC: {exc}") from exc

        valor_convertido = convertir(valor, tipo_variante.name)
        try:
            await node.write_value(ua.DataValue(ua.Variant(valor_convertido, tipo_variante)))
        except ua.UaStatusCodeError as exc:
            raise PermissionError(
                f"El PLC rechazó la escritura en '{nombre}': {exc}. Si el código es "
                f"BadUserAccessDenied o BadNotWritable, la variable es de solo lectura "
                f"en el servidor OPC UA: en TIA Portal márcala como 'Escribible "
                f"desde OPC UA' en el DB.") from exc

        logger.info("Escrito %s = %r en %s", nombre, valor_convertido, self.endpoint)
        return await self.read_tag(node_id)
