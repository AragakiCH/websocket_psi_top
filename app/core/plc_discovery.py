# -*- coding: utf-8 -*-
"""
plc_discovery.py
================
Descubrimiento de PLCs Siemens en la red.

  1) SONDEO TCP rápido de la subred (CIDR) en el puerto OPC UA (4840).
  2) VALIDACIÓN OPC UA: a cada host que responde se le piden los endpoints
     (canal seguro, SIN sesión) para confirmar que es un servidor OPC UA y
     leer su ApplicationName (ej. "PLC_1").

Se combina con los endpoints FIJOS de la configuración (`PLC_STATIC_ENDPOINTS`),
que pueden ser `opc.tcp://...` o `s7://host:102/rack/slot`.

Un S7-1200 sin servidor OPC UA NO aparece en el escaneo (no abre el 4840):
para esos está `POST /siemens/identificar`, que sondea el puerto 102.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import List, Optional

from asyncua import Client

from app.config.settings import Settings
from app.core.endpoint import PUERTO_OPCUA, EndpointPlc, host_de, puerto_de

logger = logging.getLogger("plc_discovery")


async def tcp_abierto(host: str, port: int, timeout: float) -> bool:
    """Comprueba si el puerto TCP está abierto (sondeo rápido)."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def validar_opcua(endpoint: str, timeout: float) -> Optional[str]:
    """
    Valida que el endpoint sea un servidor OPC UA y devuelve su nombre de
    aplicación ("" si no lo publica). None si no responde como OPC UA.
    """
    client = Client(url=endpoint, timeout=timeout)
    try:
        endpoints = await client.connect_and_get_server_endpoints()
        nombre = ""
        if endpoints:
            try:
                nombre = endpoints[0].Server.ApplicationName.Text or ""
            except Exception:  # noqa: BLE001
                nombre = ""
        return nombre
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def endpoint_desde_texto(texto: str, settings: Settings) -> EndpointPlc:
    """`opc.tcp://host:4840` o `s7://host:102/rack/slot` -> EndpointPlc."""
    from app.drivers.s7comm_driver import PUERTO_S7, parsear_endpoint_s7
    t = texto.strip()
    if t.startswith("s7://"):
        host, rack, slot = parsear_endpoint_s7(t)
        return EndpointPlc(endpoint=t, host=host, port=PUERTO_S7, origen="static",
                           transporte="s7comm", rack=rack, slot=slot, s7_tags=settings.s7_tags)
    if not t.startswith("opc.tcp://"):
        t = f"opc.tcp://{t}:{PUERTO_OPCUA}" if ":" not in t else f"opc.tcp://{t}"
    return EndpointPlc(endpoint=t, host=host_de(t), port=puerto_de(t, PUERTO_OPCUA),
                       origen="static", transporte="opcua")


async def _sondear_host(host: str, port: int, settings: Settings,
                        sem: asyncio.Semaphore) -> Optional[EndpointPlc]:
    async with sem:
        if not await tcp_abierto(host, port, settings.discovery_tcp_timeout):
            return None
        endpoint = f"opc.tcp://{host}:{port}"
        nombre = await validar_opcua(endpoint, settings.discovery_opcua_timeout)
        if nombre is None:
            logger.debug("Puerto abierto pero no es OPC UA: %s", endpoint)
            return None
        logger.info("PLC OPC UA detectado: %s (%s)", endpoint, nombre or "sin nombre")
        return EndpointPlc(endpoint=endpoint, host=host, port=port, nombre=nombre,
                           origen="scan", transporte="opcua")


async def descubrir_plcs(settings: Settings, escanear: Optional[bool] = None) -> List[EndpointPlc]:
    """
    Endpoints FIJOS (siempre) + los hallados por escaneo de la subred (si
    `discovery_enabled`, o si `escanear=True` lo fuerza). Sin duplicados.
    """
    resultados: dict = {}

    for texto in settings.load_static_endpoints():
        try:
            ep = endpoint_desde_texto(texto, settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Endpoint fijo mal formado, se ignora: %s (%s)", texto, exc)
            continue
        if ep.transporte == "opcua":
            ep.nombre = await validar_opcua(ep.endpoint, settings.discovery_opcua_timeout) or ""
        resultados[f"{ep.host}:{ep.port}"] = ep

    if escanear if escanear is not None else settings.discovery_enabled:
        subred = settings.resolve_subnet()
        if subred:
            logger.info("Escaneando subred %s en puerto %d...", subred, settings.discovery_port)
            try:
                red = ipaddress.ip_network(subred, strict=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("Subred inválida '%s': %s", subred, exc)
                red = None
            if red is not None:
                sem = asyncio.Semaphore(settings.discovery_concurrency)
                tareas = [_sondear_host(str(ip), settings.discovery_port, settings, sem)
                          for ip in red.hosts()]
                for h in await asyncio.gather(*tareas, return_exceptions=True):
                    if isinstance(h, EndpointPlc):
                        resultados.setdefault(f"{h.host}:{h.port}", h)
        else:
            logger.warning("No hay subred para escanear (define PLC_DISCOVERY_SUBNET).")

    lista = list(resultados.values())
    logger.info("Descubrimiento finalizado: %d PLC(s).", len(lista))
    return lista
