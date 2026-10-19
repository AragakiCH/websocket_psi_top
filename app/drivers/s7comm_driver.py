# -*- coding: utf-8 -*-
"""
s7comm_driver.py
================
Driver Siemens por **S7comm (ISO-on-TCP, puerto 102)** con `python-snap7`.
Es el camino para los PLC que NO tienen servidor OPC UA:

  * **S7-1200** con firmware < 4.4, o >= 4.4 sin licencia / sin activar
    el servidor OPC UA.
  * S7-300 / S7-400 / ET 200SP CPU, que nunca lo tuvieron.
  * Un S7-1500 al que no se quiera abrir el 4840.

  ┌────────────────────┬───────────────────────────────────────────────────┐
  │ Transporte         │ S7comm · TCP 102 · rack/slot (0/1 en 1200 y 1500, │
  │                    │ 0/2 en 300). Endpoint: s7://host:102/<rack>/<slot>│
  │ Autenticación      │ Ninguna                                           │
  │ Descubrir tags     │ NO EXISTE: S7comm lee bytes por dirección         │
  │                    │ absoluta. La lista la da el usuario (o se calcula │
  │                    │ desde las fuentes de TIA, ver s7_fuentes.py):     │
  │                    │   nombre;DB1;0;REAL   nombre;DB1;4.0;BOOL         │
  │                    │   nombre;DB1;6;STRING[20]   nombre;DB1.DBD10;DINT │
  │ Lectura            │ Un `db_read` por DB (del primer al último byte    │
  │                    │ usado) y se decodifica cada tag del búfer         │
  │ Tiempo real        │ POLLING (S7comm no tiene subscription); solo se   │
  │                    │ emiten los cambios                                │
  │ Escritura          │ `db_write` de los bytes del tag (un BOOL hace     │
  │                    │ lectura-modificación-escritura de su byte)        │
  └────────────────────┴───────────────────────────────────────────────────┘

REQUISITOS EN TIA PORTAL (o no lee nada)
  1. CPU → Propiedades → Protección y seguridad → Mecanismos de conexión →
     ☑ "Permitir acceso vía comunicación PUT/GET desde el interlocutor remoto".
  2. En CADA DB que se lea: Propiedades → Atributos → ☐ "Acceso optimizado
     al bloque" DESMARCADO (si no, el DB no tiene direcciones absolutas).
  3. Compilar y cargar. Los offsets salen de la columna "Offset" del DB.

`identificar()` funciona SIN nada de eso: la identidad de la CPU (SZL) la
responde cualquier S7 con el 102 abierto.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config.settings import Settings
from app.drivers.escritura import convertir
from app.drivers.plc_driver import DataChangeCallback, PlcDriver, TagInfo, TagValue

logger = logging.getLogger("s7comm_driver")

PUERTO_S7 = 102

#: Tamaño en bytes de cada tipo S7. BOOL ocupa un bit (se trata aparte).
TAMANO: Dict[str, int] = {
    "BOOL": 1, "BYTE": 1, "CHAR": 1, "SINT": 1, "USINT": 1,
    "WORD": 2, "INT": 2, "UINT": 2,
    "DWORD": 4, "DINT": 4, "UDINT": 4, "REAL": 4, "TIME": 4,
    "LWORD": 8, "LINT": 8, "ULINT": 8, "LREAL": 8,
}

#: Tipo S7 -> nombre que entiende `escritura.convertir` y el cliente.
TIPO_IEC = {
    "BOOL": "BOOL", "BYTE": "BYTE", "CHAR": "SINT", "SINT": "SINT",
    "USINT": "USINT", "WORD": "WORD", "INT": "INT", "UINT": "UINT",
    "DWORD": "DWORD", "DINT": "DINT", "UDINT": "UDINT", "REAL": "REAL",
    "TIME": "DINT", "LWORD": "LWORD", "LINT": "LINT", "ULINT": "ULINT",
    "LREAL": "LREAL", "STRING": "STRING",
}

_RE_LINEA = re.compile(
    r"^\s*([^;,\s]+)\s*[;,]\s*(DB\s*)?(\d+)(?:\.DB[XBWD])?\s*[;,]?\s*(\d+)(?:\.(\d))?"
    r"\s*[;,]\s*([A-Za-z]+)(?:\[(\d+)\])?\s*$", re.I)
_RE_ABS = re.compile(
    r"^\s*([^;,\s]+)\s*[;,]\s*DB\s*(\d+)\.DB([XBWD])\s*(\d+)(?:\.(\d))?"
    r"\s*[;,]\s*([A-Za-z]+)(?:\[(\d+)\])?\s*$", re.I)


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ====================================================================== #
# Endpoint s7://
# ====================================================================== #
def endpoint_s7(host: str, rack: int = 0, slot: int = 1) -> str:
    return f"s7://{host}:{PUERTO_S7}/{int(rack)}/{int(slot)}"


def parsear_endpoint_s7(texto: str) -> Tuple[str, int, int]:
    """
    `s7://192.168.0.10:102/0/1` -> ("192.168.0.10", 0, 1).
    Admite también una IP pelada (rack 0, slot 1).
    """
    v = (texto or "").strip()
    if v.startswith("s7://"):
        v = v[5:]
    partes = v.split("/")
    host = partes[0].split(":")[0].strip()
    rack = int(partes[1]) if len(partes) > 1 and partes[1].strip() else 0
    slot = int(partes[2]) if len(partes) > 2 and partes[2].strip() else 1
    return host, rack, slot


@dataclass
class TagS7:
    nombre: str
    db: int
    offset: int
    bit: int            # solo BOOL
    tipo: str           # S7 (REAL, BOOL, STRING...)
    longitud: int       # STRING[n]

    @property
    def tamano(self) -> int:
        if self.tipo == "STRING":
            return self.longitud + 2
        return TAMANO[self.tipo]

    @property
    def direccion(self) -> str:
        if self.tipo == "BOOL":
            return f"DB{self.db}.DBX{self.offset}.{self.bit}"
        letra = {1: "B", 2: "W", 4: "D"}.get(self.tamano, "B")
        return f"DB{self.db}.DB{letra}{self.offset}"


def parsear_tags(texto: str) -> List[TagS7]:
    """
    Una línea por tag. Se aceptan las dos formas:
        temperatura;DB1;0;REAL
        marcha;DB1;4.0;BOOL
        texto;DB1;6;STRING[20]
        contador;DB1.DBD10;DINT
    Separador `;` o `,`. Líneas vacías o que empiezan por # se ignoran.
    Lanza ValueError con el número de línea si alguna no se entiende.
    """
    salida: List[TagS7] = []
    vistos = set()
    for n, linea in enumerate((texto or "").splitlines(), 1):
        l = linea.strip()
        if not l or l.startswith("#"):
            continue
        m = _RE_ABS.match(l)
        if m:
            nombre, db, _letra, off, bit, tipo, lon = m.groups()
        else:
            m = _RE_LINEA.match(l)
            if not m:
                raise ValueError(
                    f"Línea {n}: no se entiende '{l}'. Formato: nombre;DB1;offset;TIPO "
                    f"(p. ej. temperatura;DB1;0;REAL, marcha;DB1;4.0;BOOL, "
                    f"texto;DB1;6;STRING[20])")
            nombre, _pref, db, off, bit, tipo, lon = m.groups()
        tipo = tipo.upper()
        if tipo not in TAMANO and tipo != "STRING":
            raise ValueError(f"Línea {n}: tipo '{tipo}' desconocido. Válidos: "
                             f"{', '.join(sorted(TAMANO))}, STRING[n]")
        if tipo == "STRING" and not lon:
            raise ValueError(f"Línea {n}: STRING necesita longitud: STRING[20]")
        if tipo != "BOOL" and bit:
            raise ValueError(f"Línea {n}: solo un BOOL lleva bit ({off}.{bit})")
        if nombre in vistos:
            raise ValueError(f"Línea {n}: el nombre '{nombre}' está repetido")
        vistos.add(nombre)
        salida.append(TagS7(nombre=nombre, db=int(db), offset=int(off),
                            bit=int(bit or 0), tipo=tipo, longitud=int(lon or 0)))
    return salida


# ====================================================================== #
# Codificación / decodificación (big-endian, como el S7)
# ====================================================================== #
# Se implementa con `struct` y no con `snap7.util` a propósito: util cambia
# entre versiones (3.0 no trae set_lint/set_ulint, por ejemplo) y un driver
# de escritura no puede depender de qué helpers trae la librería del día.
_FORMATO: Dict[str, str] = {
    "BYTE": ">B", "CHAR": ">b", "SINT": ">b", "USINT": ">B",
    "WORD": ">H", "INT": ">h", "UINT": ">H",
    "DWORD": ">I", "DINT": ">i", "UDINT": ">I", "REAL": ">f", "TIME": ">i",
    "LWORD": ">Q", "LINT": ">q", "ULINT": ">Q", "LREAL": ">d",
}


def decodificar(buf: bytearray, base: int, tag: TagS7) -> Any:
    """Valor Python del tag a partir del búfer leído desde `base`."""
    i = tag.offset - base
    t = tag.tipo
    if t == "BOOL":
        return bool(buf[i] & (1 << tag.bit))
    if t == "STRING":
        # S7 STRING: [longitud máxima][longitud actual][caracteres...]
        longitud = min(buf[i + 1], buf[i], tag.longitud)
        return bytes(buf[i + 2:i + 2 + longitud]).decode("latin-1")
    v = struct.unpack_from(_FORMATO[t], buf, i)[0]
    if t == "CHAR":
        return chr(v & 0xFF)
    return round(v, 6) if t == "REAL" else v


def codificar(tag: TagS7, valor: Any, actual: bytearray) -> bytearray:
    """Bytes a escribir. Para BOOL modifica el byte actual (respeta los otros bits)."""
    t = tag.tipo
    if t == "BOOL":
        buf = bytearray(actual[:1] or b"\x00")
        if valor:
            buf[0] |= (1 << tag.bit)
        else:
            buf[0] &= ~(1 << tag.bit) & 0xFF
        return buf
    if t == "STRING":
        datos = str(valor).encode("latin-1", errors="replace")[:tag.longitud]
        buf = bytearray(tag.longitud + 2)
        buf[0] = tag.longitud
        buf[1] = len(datos)
        buf[2:2 + len(datos)] = datos
        return buf
    if t == "CHAR":
        valor = ord(str(valor)[:1] or "\x00")
        if valor > 127:
            valor -= 256
    return bytearray(struct.pack(_FORMATO[t], valor))


# ====================================================================== #
# Identificación (SZL): funciona aunque el OPC UA no exista
# ====================================================================== #
def familia_de(modelo: str) -> str:
    m = (modelo or "").upper()
    for clave, fam in (("121", "S7-1200"), ("151", "S7-1500"), ("ET 200", "ET 200SP"),
                       ("31", "S7-300"), ("41", "S7-400")):
        if f"CPU {clave}" in m or m.startswith(clave) or clave in m:
            return fam
    return "S7"


def _nuevo_cliente(timeout_ms: int):
    import snap7
    cli = snap7.client.Client()
    try:
        cli.set_param(snap7.type.Parameter.PingTimeout, timeout_ms)
        cli.set_param(snap7.type.Parameter.RecvTimeout, timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    return cli


def _identificar_sync(host: str, rack: int, slot: int, timeout_ms: int) -> dict:
    cli = _nuevo_cliente(timeout_ms)
    try:
        cli.set_connection_type(3)   # OP: no ocupa la conexión PG de TIA
    except Exception:  # noqa: BLE001
        pass
    cli.connect(host, rack, slot, PUERTO_S7)
    try:
        # LA CONEXIÓN YA ES EL DATO. Lo de abajo (SZL) lo responden los
        # S7-300/400 y los 1500; un S7-1200 contesta "Object does not exist"
        # a casi todas las SZL. Un fallo aquí NO significa que el PLC no esté.
        modelo = nombre = serie = estado = ""
        sin_szl = ""
        try:
            info = cli.get_cpu_info()
            modelo = bytes(info.ModuleTypeName).decode("latin-1").strip("\x00 ")
            nombre = bytes(info.ASName).decode("latin-1").strip("\x00 ")
            serie = bytes(info.SerialNumber).decode("latin-1").strip("\x00 ")
        except Exception as exc:  # noqa: BLE001
            sin_szl = str(exc)
        try:
            estado = str(cli.get_cpu_state()).replace("S7Cpu", "").replace("Status", "")
        except Exception:  # noqa: BLE001
            pass
        familia = familia_de(modelo) if modelo else "S7-1200"
        return {"modelo": modelo, "familia": familia, "nombre": nombre,
                "serie": serie, "estado": estado, "rack": rack, "slot": slot,
                "sin_identidad": not modelo, "detalle_szl": sin_szl}
    finally:
        try:
            cli.disconnect()
            cli.destroy()
        except Exception:  # noqa: BLE001
            pass


async def identificar(host: str, rack: int = 0, slot: Optional[int] = None,
                      timeout_ms: int = 2500) -> dict:
    """
    Identidad de la CPU por S7comm. Si no se indica slot, prueba 1 (1200,
    1500) y luego 2 (300) y 0. Lanza ConnectionError si el 102 no responde.
    """
    bucle = asyncio.get_running_loop()
    slots = [slot] if slot is not None else [1, 2, 0]
    ultimo: Optional[Exception] = None
    for s in slots:
        try:
            return await bucle.run_in_executor(None, _identificar_sync, host, rack, s, timeout_ms)
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
    raise ConnectionError(f"{type(ultimo).__name__}: {ultimo}")


def _sondear_dbs_sync(host: str, rack: int, slot: int, candidatos: List[int],
                      timeout_ms: int) -> Dict[int, int]:
    """
    {db: tamaño_en_bytes} de los DB que EXISTEN y se pueden leer.

    S7comm no lista bloques en un S7-1200, así que se prueba: un `db_read`
    de 1 byte en el offset 0 dice si el DB existe (y si no es optimizado);
    el tamaño se acota por búsqueda exponencial + binaria sobre el offset.
    """
    cli = _nuevo_cliente(timeout_ms)
    cli.connect(host, rack, slot, PUERTO_S7)
    salida: Dict[int, int] = {}
    try:
        def se_lee(db: int, off: int) -> bool:
            try:
                cli.db_read(db, off, 1)
                return True
            except Exception:  # noqa: BLE001
                return False

        for db in candidatos:
            if not se_lee(db, 0):
                continue
            lo, hi = 0, 1
            while hi < 65536 and se_lee(db, hi):
                lo, hi = hi, hi * 2
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if se_lee(db, mid):
                    lo = mid
                else:
                    hi = mid
            salida[db] = hi
    finally:
        try:
            cli.disconnect()
            cli.destroy()
        except Exception:  # noqa: BLE001
            pass
    return salida


async def sondear_dbs(host: str, rack: int = 0, slot: int = 1,
                      candidatos: Optional[List[int]] = None,
                      timeout_ms: int = 2500) -> Dict[int, int]:
    """Versión async de `_sondear_dbs_sync`. Por defecto prueba DB 1..40."""
    bucle = asyncio.get_running_loop()
    lista = sorted(set(candidatos or range(1, 41)))
    return await bucle.run_in_executor(None, _sondear_dbs_sync, host, rack, slot, lista, timeout_ms)


# ====================================================================== #
# El driver
# ====================================================================== #
class S7CommDriver(PlcDriver):
    """Siemens S7 por S7comm (snap7): polling de DBs por dirección absoluta."""

    modo_lectura = "polling"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cli: Any = None
        self._lock = threading.Lock()
        self._connected = False
        self._callback: Optional[DataChangeCallback] = None
        self._tarea_polling: Optional[asyncio.Task] = None
        self.modelo: str = ""
        self.info_plc: dict = {}

        self._tags_s7: Dict[str, TagS7] = {}      # node_id -> TagS7
        self.tag_por_nodeid: Dict[str, TagInfo] = {}
        self.ultimos_valores: Dict[str, TagValue] = {}
        self.tags_descubiertos: List[TagInfo] = []
        self.ultimo_cambio_ts: Dict[str, datetime] = {}

    # ---- datos de conexión ---------------------------------------------- #
    @property
    def _host(self) -> str:
        return parsear_endpoint_s7(self._settings.opcua_endpoint)[0]

    @property
    def _rack(self) -> int:
        return int(self._settings.s7_rack)

    @property
    def _slot(self) -> int:
        return int(self._settings.s7_slot)

    async def _en_hilo(self, fn, *args):
        """Ejecuta una llamada bloqueante de snap7 en el executor, serializada."""
        bucle = asyncio.get_running_loop()

        def con_lock():
            with self._lock:
                return fn(*args)
        return await bucle.run_in_executor(None, con_lock)

    # ================================================================== #
    # Conexión
    # ================================================================== #
    async def connect(self) -> None:
        try:
            import snap7  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Falta el paquete 'python-snap7' (pip install python-snap7). "
                "Es el cliente S7comm para PLCs Siemens sin OPC UA.") from exc
        host, rack, slot = self._host, self._rack, self._slot
        timeout_ms = int(float(self._settings.s7_connect_timeout) * 1000)

        def abrir():
            cli = _nuevo_cliente(timeout_ms)
            cli.connect(host, rack, slot, PUERTO_S7)
            try:
                info = cli.get_cpu_info()
                modelo = bytes(info.ModuleTypeName).decode("latin-1").strip("\x00 ")
            except Exception:  # noqa: BLE001
                modelo = ""
            return cli, modelo

        try:
            self._cli, self.modelo = await self._en_hilo(abrir)
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                f"No se pudo abrir S7comm con {host} (rack {rack}, slot {slot}, "
                f"puerto 102): {exc}. Revisa la IP, el slot (1 en S7-1200/1500, "
                f"2 en S7-300) y que no haya un firewall en el 102.") from exc
        self._connected = True
        self.info_plc = {"modelo": self.modelo, "familia": familia_de(self.modelo)}
        logger.info("S7comm conectado a %s (rack %d, slot %d): %s",
                    host, rack, slot, self.modelo or "CPU desconocida")

    async def disconnect(self) -> None:
        await self._detener_lectura()
        if self._cli is not None:
            cli = self._cli

            def cerrar():
                try:
                    cli.disconnect()
                finally:
                    try:
                        cli.destroy()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                await self._en_hilo(cerrar)
            except Exception:  # noqa: BLE001
                pass
        self._cli = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._cli is not None

    async def check_alive(self) -> bool:
        if self._cli is None:
            return False
        try:
            ok = await self._en_hilo(self._cli.get_connected)
            if not ok:
                self._connected = False
            return bool(ok)
        except Exception:  # noqa: BLE001
            self._connected = False
            return False

    # ================================================================== #
    # "Descubrimiento": la lista del usuario
    # ================================================================== #
    async def browse_tags(self) -> List[TagInfo]:
        lista = parsear_tags(self._settings.s7_tags or "")
        if not lista:
            raise RuntimeError(
                "S7comm no puede descubrir tags: hay que indicar la lista "
                "(nombre;DB;offset;TIPO, una por línea) al dar de alta el PLC, "
                "o calcularla desde las fuentes de TIA con POST /siemens/importar.")
        self._tags_s7.clear()
        self.tag_por_nodeid.clear()
        self.tags_descubiertos = []
        for t in lista:
            node_id = t.direccion
            info = TagInfo(name=t.nombre, full_name=f"DB{t.db}.{t.nombre}", node_id=node_id,
                           data_type=TIPO_IEC[t.tipo], db_name=f"DB{t.db}")
            self._tags_s7[node_id] = t
            self.tag_por_nodeid[node_id] = info
            self.tags_descubiertos.append(info)

        # Una lectura de prueba: si un DB tiene acceso optimizado o PUT/GET
        # está cerrado, mejor saberlo AHORA con un mensaje que explique qué
        # marcar en TIA, que descubrirlo por un PLC "conectado" sin datos.
        try:
            await self._leer_todo(list(self.tags_descubiertos))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Conectado a {self._host}, pero no se pueden leer los DB: {exc}. "
                f"En TIA Portal: (1) CPU → Protección → 'Permitir acceso PUT/GET' "
                f"marcado; (2) en cada DB, 'Acceso optimizado al bloque' "
                f"DESMARCADO; (3) compilar y cargar.") from exc
        logger.info("S7comm %s: %d tags en %d DB(s).", self._host,
                    len(self.tags_descubiertos), len({t.db for t in lista}))
        return list(self.tags_descubiertos)

    # ================================================================== #
    # Lectura en bloque por DB
    # ================================================================== #
    def _rangos(self, tags: List[TagInfo]) -> Dict[int, Tuple[int, int]]:
        """Por DB: (primer byte, tamaño) que cubre todos sus tags."""
        rangos: Dict[int, Tuple[int, int]] = {}
        for info in tags:
            t = self._tags_s7.get(info.node_id)
            if t is None:
                continue
            ini, fin = rangos.get(t.db, (t.offset, t.offset + t.tamano))
            rangos[t.db] = (min(ini, t.offset), max(fin, t.offset + t.tamano))
        return {db: (ini, fin - ini) for db, (ini, fin) in rangos.items()}

    async def _leer_todo(self, tags: List[TagInfo]) -> Dict[str, Any]:
        """{node_id: valor} leyendo cada DB una sola vez."""
        rangos = self._rangos(tags)
        cli = self._cli

        def leer():
            return {db: (ini, bytearray(cli.db_read(db, ini, tam)))
                    for db, (ini, tam) in rangos.items()}
        bufs = await self._en_hilo(leer)
        salida: Dict[str, Any] = {}
        for info in tags:
            t = self._tags_s7.get(info.node_id)
            if t is None or t.db not in bufs:
                continue
            base, buf = bufs[t.db]
            try:
                salida[info.node_id] = decodificar(buf, base, t)
            except Exception as exc:  # noqa: BLE001
                logger.debug("No se pudo decodificar %s: %s", info.node_id, exc)
        return salida

    # ================================================================== #
    # Tiempo real: polling
    # ================================================================== #
    async def subscribe(self, tags: List[TagInfo], callback: DataChangeCallback) -> None:
        if self._cli is None:
            raise RuntimeError("subscribe llamado sin conexión activa.")
        await self._detener_lectura()
        self._callback = callback
        await self._leer_y_emitir(tags, solo_cambios=False)
        self._tarea_polling = asyncio.create_task(self._bucle_polling(tags))

    async def _bucle_polling(self, tags: List[TagInfo]) -> None:
        intervalo = max(50, int(self._settings.s7_poll_interval_ms))
        fallos = 0
        try:
            while self._connected:
                t0 = time.monotonic()
                try:
                    await self._leer_y_emitir(tags, solo_cambios=True)
                    fallos = 0
                except Exception as exc:  # noqa: BLE001
                    fallos += 1
                    logger.warning("S7comm %s: fallo de lectura (%d): %s", self._host, fallos, exc)
                    if fallos >= 3:
                        # Tres fallos seguidos: se da la conexión por caída y
                        # el watchdog del handler reconecta con backoff.
                        self._connected = False
                        return
                resto = intervalo / 1000.0 - (time.monotonic() - t0)
                await asyncio.sleep(resto if resto > 0 else 0.01)
        except asyncio.CancelledError:
            pass

    async def _leer_y_emitir(self, tags: List[TagInfo], solo_cambios: bool) -> None:
        if not self._callback:
            return
        leidos = await self._leer_todo(tags)
        for info in tags:
            if info.node_id not in leidos:
                continue
            valor = leidos[info.node_id]
            if solo_cambios:
                ant = self.ultimos_valores.get(info.full_name)
                if ant is not None and ant.value == valor:
                    continue
            await self._emitir(info, valor)

    async def _emitir(self, info: TagInfo, valor: Any) -> None:
        if not self._callback:
            return
        ahora = datetime.now(timezone.utc)
        delta_ms: Optional[float] = None
        anterior = self.ultimo_cambio_ts.get(info.full_name)
        if anterior is not None:
            delta_ms = (ahora - anterior).total_seconds() * 1000.0
        self.ultimo_cambio_ts[info.full_name] = ahora
        tv = TagValue(tag=info.full_name, value=valor, data_type=info.data_type,
                      timestamp=ahora.isoformat(), node_id=info.node_id,
                      source_ts=ahora.isoformat(),
                      delta_ms=round(delta_ms, 1) if delta_ms is not None else None)
        self.ultimos_valores[info.full_name] = tv
        await self._callback(tv)

    async def _detener_lectura(self) -> None:
        if self._tarea_polling is not None:
            self._tarea_polling.cancel()
            try:
                await self._tarea_polling
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tarea_polling = None

    # ================================================================== #
    # Lectura puntual y escritura
    # ================================================================== #
    async def read_tags(self, node_ids: List[str]) -> List[Optional[TagValue]]:
        if self._cli is None:
            raise RuntimeError("read_tags llamado sin conexión activa.")
        infos = [self.tag_por_nodeid[n] for n in node_ids if n in self.tag_por_nodeid]
        try:
            leidos = await self._leer_todo(infos)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lectura en bloque fallida: %s", exc)
            return [None] * len(node_ids)
        ahora = _ahora_iso()
        salida: List[Optional[TagValue]] = []
        for n in node_ids:
            info = self.tag_por_nodeid.get(n)
            if info is None or n not in leidos:
                salida.append(None)
                continue
            salida.append(TagValue(tag=info.full_name, value=leidos[n],
                                   data_type=info.data_type, timestamp=ahora, node_id=n))
        return salida

    async def read_tag(self, node_id: str) -> TagValue:
        vals = await self.read_tags([node_id])
        if not vals or vals[0] is None:
            raise KeyError(f"No se pudo leer '{node_id}'.")
        return vals[0]

    async def write_tag(self, node_id: str, valor: object) -> TagValue:
        if self._cli is None:
            raise RuntimeError("write_tag llamado sin conexión activa.")
        info = self.tag_por_nodeid.get(node_id)
        t = self._tags_s7.get(node_id)
        if info is None or t is None:
            raise KeyError(f"El tag '{node_id}' no existe en este PLC.")
        convertido = convertir(valor, info.data_type)
        cli = self._cli

        def escribir():
            actual = bytearray()
            if t.tipo == "BOOL":
                actual = bytearray(cli.db_read(t.db, t.offset, 1))
            cli.db_write(t.db, t.offset, codificar(t, convertido, actual))
        try:
            await self._en_hilo(escribir)
        except Exception as exc:  # noqa: BLE001
            raise PermissionError(
                f"El PLC rechazó la escritura en {node_id}: {exc}. Revisa que "
                f"'Permitir acceso PUT/GET' esté marcado en la CPU y que el DB "
                f"no tenga 'Acceso optimizado al bloque'.") from exc
        logger.info("Escrito %s = %r en %s", info.full_name, convertido, self._host)
        return await self.read_tag(node_id)
