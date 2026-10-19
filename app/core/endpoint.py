# -*- coding: utf-8 -*-
"""
endpoint.py
===========
`EndpointPlc`: la descripción de UN PLC Siemens gestionado. Es lo que se
persiste en `plcs.json` y lo que recibe el PlcManager para crear el driver.

Formato del endpoint según transporte:
    opcua   -> opc.tcp://192.168.0.10:4840
    s7comm  -> s7://192.168.0.10:102/<rack>/<slot>
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PUERTO_OPCUA = 4840
PUERTO_S7 = 102


@dataclass
class EndpointPlc:
    endpoint: str              # opc.tcp://host:puerto  |  s7://host:102/rack/slot
    host: str
    port: int
    nombre: str = ""           # ApplicationName del servidor OPC UA (si se leyó)
    origen: str = "manual"     # 'manual' | 'scan' | 'static'
    transporte: str = "opcua"  # 'opcua' | 's7comm'
    rack: int = 0              # solo s7comm
    slot: int = 1              # solo s7comm (1 en 1200/1500, 2 en 300)
    s7_tags: str = ""          # solo s7comm: "nombre;DB1;offset;TIPO" por línea
    usuario: str = ""          # solo opcua con autenticación (opcional)
    password: str = ""         # solo opcua con autenticación (opcional)

    def publico(self) -> dict:
        """Representación sin contraseña, para la API."""
        return {
            "endpoint": self.endpoint, "host": self.host, "port": self.port,
            "nombre": self.nombre, "origen": self.origen,
            "transporte": self.transporte, "rack": self.rack, "slot": self.slot,
            "s7_tags": self.s7_tags, "usuario": self.usuario,
        }


def host_de(texto: str) -> str:
    """`opc.tcp://192.168.0.10:4840` / `s7://192.168.0.10:102/0/1` / `192.168.0.10` -> host."""
    v = (texto or "").strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    return v.split("/", 1)[0].split(":", 1)[0].strip()


def puerto_de(texto: str, por_defecto: int) -> int:
    v = (texto or "").strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    m = re.match(r"^[^/:]+:(\d+)", v)
    return int(m.group(1)) if m else por_defecto


def plc_id_desde(ep: EndpointPlc, usados: set) -> str:
    """
    Identificador de PLC estable y único.
    Prioridad: nombre de aplicación saneado -> host. Sufijo si colisiona.
    """
    base = (ep.nombre or "").strip() or ep.host
    base = re.sub(r"[^A-Za-z0-9_.\-]", "_", base)
    plc_id = base
    i = 2
    while plc_id in usados:
        plc_id = f"{base}_{i}"
        i += 1
    usados.add(plc_id)
    return plc_id
