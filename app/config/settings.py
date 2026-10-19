# -*- coding: utf-8 -*-
"""
settings.py
===========
Configuración centralizada del servicio. Todo se lee de variables de entorno
(o del fichero `.env`) con pydantic-settings, prefijo `PLC_`. Nada queda
hardcodeado en la lógica de negocio.

Ejemplos:
    PLC_API_PORT=8000
    PLC_SAMPLING_INTERVAL_MS=1000
    PLC_SIEMENS_TRANSPORTE=s7comm

Cada PLC gestionado recibe una COPIA de estos settings con sus propios
valores (endpoint, transporte, rack/slot, lista de tags) mediante
`Settings.para_plc(...)`, así la configuración de uno no contamina a otro.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TRANSPORTES_SIEMENS = ("opcua", "s7comm")


class Settings(BaseSettings):
    """Modelo de configuración de la aplicación."""

    # ------------------------------------------------------------------ #
    # Servidor web
    # ------------------------------------------------------------------ #
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    log_level: str = Field(default="INFO", description="DEBUG | INFO | WARNING | ERROR")
    # Orígenes CORS permitidos (coma). "*" = cualquiera (desarrollo).
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    # Clave de API para los endpoints que MODIFICAN algo (alta/baja de PLC,
    # escritura, lista blanca). Vacía = sin protección (solo para desarrollo).
    # Se manda en la cabecera `X-API-Key` (REST) o `?api_key=` (WebSocket).
    api_key: str = Field(default="")
    # Carpeta donde se persiste el estado (plcs.json, tags_plc.json,
    # escritura_permitida.json, auditoria.jsonl). Vacío = <raíz>/datos.
    datos_dir: str = Field(default="")

    # ------------------------------------------------------------------ #
    # Arranque y descubrimiento de PLCs
    # ------------------------------------------------------------------ #
    # Si False, el servicio arranca con los PLCs guardados en plcs.json y
    # nada más: no usa el endpoint semilla ni escanea la red. Los PLCs se
    # agregan con POST /plcs.
    autostart_plcs: bool = Field(default=False)
    # Endpoints fijos que se conectan solos al arrancar (requiere autostart).
    # Env: PLC_STATIC_ENDPOINTS=["opc.tcp://192.168.0.1:4840","s7://192.168.0.2:102/0/1"]
    static_endpoints: Optional[List[str]] = Field(default=None)
    # Escaneo de subred en el puerto OPC UA.
    discovery_enabled: bool = Field(default=False)
    discovery_subnet: Optional[str] = Field(default=None, description="CIDR, ej. 192.168.0.0/24")
    discovery_port: int = Field(default=4840)
    discovery_tcp_timeout: float = Field(default=0.8)
    discovery_opcua_timeout: float = Field(default=4.0)
    discovery_concurrency: int = Field(default=64)
    # Re-escaneo periódico (s). 0 = nunca.
    discovery_interval: float = Field(default=0.0)
    # Lista blanca de PLCs descubiertos a los que conectarse (nombre/host/endpoint).
    include_plcs: Optional[List[str]] = Field(default=None)

    # ------------------------------------------------------------------ #
    # Siemens: transporte por defecto
    # ------------------------------------------------------------------ #
    # 'opcua'  -> OpcUaDriver: S7-1500; S7-1200 con FW >= 4.4 y servidor OPC UA
    #             activado. Descubre los DB solo.
    # 's7comm' -> S7CommDriver: S7-1200 (cualquier FW), S7-300/400, ET 200SP.
    #             Sin descubrimiento: la lista de tags la da el usuario.
    siemens_transporte: str = Field(default="opcua")

    # ------------------------------------------------------------------ #
    # OPC UA
    # ------------------------------------------------------------------ #
    opcua_endpoint: str = Field(
        default="opc.tcp://192.168.0.1:4840",
        description="Endpoint del PLC. En multi-PLC es el de ESTE PLC "
                    "(se sobreescribe por PLC).")
    opcua_connect_timeout: float = Field(default=8.0, description="Al abrir la sesión (s).")
    opcua_request_timeout: float = Field(
        default=4.0, description="Por petición (Read, Publish…). Acota cuánto tarda el "
                                 "watchdog en detectar un PLC apagado.")
    opcua_namespace_uri: str = Field(default="http://www.siemens.com/simatic-s7-opcua")
    # Ruta de navegación hacia los Data Blocks globales (BrowseNames).
    browse_device_set: str = Field(default="DeviceSet")
    browse_plc_name: str = Field(default="PLC_1")
    browse_datablocks_node: str = Field(default="DataBlocksGlobal")
    # Seguridad (por defecto "No security" + anónimo).
    security_policy: Optional[str] = Field(default=None, description="Ej: Basic256Sha256")
    security_mode: Optional[str] = Field(default=None, description="Ej: SignAndEncrypt")
    client_cert_path: Optional[str] = Field(default=None)
    client_private_key_path: Optional[str] = Field(default=None)
    server_cert_path: Optional[str] = Field(default=None)
    opcua_username: Optional[str] = Field(default=None)
    opcua_password: Optional[str] = Field(default=None)
    # Subscription.
    publishing_interval_ms: int = Field(default=500)
    sampling_interval_ms: int = Field(
        default=1000,
        description="Muestreo por MonitoredItem. El S7-1500 tiene un mínimo "
                    "de 100 ms; el S7-1200, de 1000 ms.")
    subscription_queue_size: int = Field(default=10)
    # Filtro opcional de Data Blocks: env (lista) > YAML > todos.
    tags_filter_dbs: Optional[List[str]] = Field(default=None)
    tags_filter_yaml_path: str = Field(
        default=os.path.join(os.path.dirname(__file__), "tags_filter.yaml"))

    # ------------------------------------------------------------------ #
    # S7comm (python-snap7, puerto 102)
    # ------------------------------------------------------------------ #
    s7_rack: int = Field(default=0)
    s7_slot: int = Field(default=1, description="1 en S7-1200/1500, 2 en S7-300.")
    s7_connect_timeout: float = Field(default=5.0)
    s7_poll_interval_ms: int = Field(default=250)
    # Lista de tags (una por línea): nombre;DB1;offset;TIPO
    s7_tags: str = Field(default="")

    # ------------------------------------------------------------------ #
    # Reconexión y watchdog
    # ------------------------------------------------------------------ #
    reconnect_initial_delay: float = Field(default=2.0)
    reconnect_max_delay: float = Field(default=30.0)
    reconnect_backoff_factor: float = Field(default=2.0)
    healthcheck_interval: float = Field(default=5.0)

    model_config = SettingsConfigDict(
        env_prefix="PLC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Utilidades
    # ------------------------------------------------------------------ #
    def load_db_filter(self) -> Optional[List[str]]:
        """Lista de DBs a incluir. Prioridad: env > YAML > None (todos)."""
        if self.tags_filter_dbs:
            return [d.strip() for d in self.tags_filter_dbs if d.strip()]
        path = self.tags_filter_yaml_path
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                dbs = data.get("include_dbs")
                if dbs:
                    return [str(d).strip() for d in dbs if str(d).strip()]
            except Exception:  # noqa: BLE001
                pass  # un YAML mal formado no debe tumbar el arranque
        return None

    def load_static_endpoints(self) -> List[str]:
        """Endpoints fijos, sin duplicados."""
        salida: List[str] = []
        for e in self.static_endpoints or []:
            e = e.strip()
            if e and e not in salida:
                salida.append(e)
        return salida

    def resolve_subnet(self) -> Optional[str]:
        """Subred CIDR a escanear; si no se configuró, se deriva del endpoint semilla (/24)."""
        if self.discovery_subnet:
            return self.discovery_subnet
        try:
            host = self.opcua_endpoint.split("//", 1)[1].split(":", 1)[0]
            partes = host.split(".")
            if len(partes) == 4:
                return f"{partes[0]}.{partes[1]}.{partes[2]}.0/24"
        except Exception:  # noqa: BLE001
            pass
        return None

    def para_plc(self, endpoint: str, transporte: str = "", rack: Optional[int] = None,
                 slot: Optional[int] = None, s7_tags: str = "",
                 usuario: str = "", password: str = "") -> "Settings":
        """
        Copia de settings personalizada para UN PLC. Se usa `model_copy`
        para no contaminar la configuración global ni la de otros PLCs.
        """
        transporte = (transporte or self.siemens_transporte or "opcua").strip().lower()
        update: dict = {"opcua_endpoint": endpoint, "siemens_transporte": transporte}
        if transporte == "s7comm":
            update.update({
                "s7_rack": int(rack if rack is not None else self.s7_rack),
                "s7_slot": int(slot if slot else self.s7_slot),
                "s7_tags": s7_tags or self.s7_tags,
            })
        if usuario:
            update["opcua_username"] = usuario
            update["opcua_password"] = password or None
        return self.model_copy(update=update)


@lru_cache
def get_settings() -> Settings:
    """Instancia única (cacheada) de Settings."""
    return Settings()
