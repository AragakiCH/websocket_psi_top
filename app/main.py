# -*- coding: utf-8 -*-
"""
main.py
=======
Punto de entrada de la aplicación FastAPI.

  * Configura el logging.
  * Construye los componentes (ConnectionManager, PlcManager, stores,
    auditoría) e inyecta las dependencias en `app.state`.
  * Arranca los supervisores de PLC al iniciar (lifespan startup) y cierra
    todo limpiamente al apagar (lifespan shutdown).
  * Registra los routers REST y el WebSocket.

Arrancar con:
    python run.py
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import escritura_routes, plc_routes, siemens_routes, websocket_routes
from app.config.rutas import describir, resolver_carpeta_datos
from app.config.settings import get_settings
from app.core.auditoria import Auditoria
from app.core.connection_manager import ConnectionManager
from app.core.plc_manager import PlcManager
from app.stores.escritura_store import EscrituraStore


def configurar_logging(nivel: str) -> None:
    """
    asyncua es MUY verboso en INFO (cada PublishResult): se sube a WARNING
    salvo que se pida DEBUG explícitamente.
    """
    logging.basicConfig(
        level=getattr(logging, nivel.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    nivel_libs = logging.DEBUG if nivel.upper() == "DEBUG" else logging.WARNING
    for nombre in ("asyncua", "asyncua.client", "asyncua.common.subscription",
                   "asyncua.client.ua_client", "asyncua.uaprotocol", "snap7"):
        logging.getLogger(nombre).setLevel(nivel_libs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configurar_logging(settings.log_level)
    logger = logging.getLogger("main")

    carpeta: Path = resolver_carpeta_datos(settings.datos_dir or None)
    manager = ConnectionManager()
    plc_manager = PlcManager(manager, settings, carpeta)
    escritura_store = EscrituraStore(carpeta)
    auditoria = Auditoria(carpeta)

    app.state.settings = settings
    app.state.carpeta_datos = carpeta
    app.state.manager = manager
    app.state.plc_manager = plc_manager
    app.state.escritura_store = escritura_store
    app.state.auditoria = auditoria

    logger.info("=== PSI Top · backend Siemens (OPC UA + S7comm) v%s ===", __version__)
    logger.info("Datos en: %s | API key: %s | autostart=%s",
                carpeta, "activa" if settings.api_key else "SIN proteger",
                settings.autostart_plcs)

    auditoria.start()
    auditoria.registrar("servicio.arranque", detalle={"version": __version__})
    # Los supervisores corren en segundo plano: la API arranca aunque ningún
    # PLC esté disponible todavía.
    await plc_manager.start()
    try:
        yield
    finally:
        logger.info("=== Apagando servicio: cierre limpio ===")
        auditoria.registrar("servicio.parada")
        await plc_manager.stop()
        auditoria.stop()


_DESCRIPCION = """
Backend de conexión con PLCs **Siemens** y transmisión en tiempo real por **WebSocket**.

| Transporte | PLCs | Cómo obtiene los tags | Tiempo real |
|---|---|---|---|
| **OPC UA** (puerto 4840) | S7-1500 · S7-1200 FW ≥ 4.4 con servidor activado | Browse automático de `DataBlocksGlobal` | Subscription nativa |
| **S7comm** (puerto 102) | S7-1200 (cualquier FW) · S7-300/400 · ET 200SP | Lista `nombre;DB;offset;TIPO` (calculable desde las fuentes de TIA) | Polling, solo cambios |

## Flujo de uso

1. `POST /siemens/identificar` → qué CPU es y qué transporte usar.
2. (S7comm) `POST /siemens/importar` → calcular la lista de tags desde las fuentes de TIA.
3. `POST /plcs` → dar de alta el PLC.
4. `ws://<host>:8000/ws` → recibir snapshot y cambios en vivo.
5. `GET /health` · `GET /tags` → estado y últimos valores.
6. `PUT /escritura/permitidos` + `POST /escritura` → escribir (con lista blanca y auditoría).

## WebSocket `/ws`

Mensajes que envía el servidor (la clave de `tags` es `"<plc_id>|<tag>"`):

```json
{"type":"snapshot","plcs":{"192.168.0.10":{"estado":"conectado","modelo":"CPU 1516-3 PN/DP"}},
 "tags":{"192.168.0.10|DB_Datos.temperatura":{"plc":"192.168.0.10","tag":"DB_Datos.temperatura",
 "value":53.09,"type":"Float","timestamp":"...","source_ts":"...","delta_ms":512}}}
```
```json
{"timestamp":"...","plc":"192.168.0.10","tag":"DB_Datos.temperatura","value":49.36,
 "type":"Float","source_ts":"...","server_ts":"...","delta_ms":480}
```
```json
{"type":"status","plc":"192.168.0.10","status":"conectado|reconectando","timestamp":"..."}
{"type":"plc_removed","plc_removed":"192.168.0.10","timestamp":"..."}
{"type":"presence","usuarios":[...],"num_clientes":3}
```

Si el mensaje trae `tag`, es un cambio de valor; si no, es de control.
Comandos del cliente: `{"tipo":"ping"}`, `{"tipo":"snapshot"}`,
`{"tipo":"write","plc_id":"...","tag":"...","valor":...}` (requiere `?api_key=`).

Documentación completa en la carpeta `docs/` del proyecto.
"""

app = FastAPI(
    title="PSI Top · Backend Siemens OPC UA / S7comm → WebSocket",
    description=_DESCRIPCION,
    version=__version__,
    lifespan=lifespan,
)

_settings_cors = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings_cors.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(plc_routes.router)
app.include_router(siemens_routes.router)
app.include_router(escritura_routes.router)
app.include_router(websocket_routes.router, tags=["WebSocket"])


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({
        "servicio": "PSI Top · backend Siemens",
        "version": __version__,
        "docs": "/docs",
        "endpoints": ["/health", "/plcs", "/tags", "/browse", "/siemens/identificar",
                      "/siemens/importar", "/escritura", "/ws"],
    })


@app.get("/sistema", tags=["Sistema"], summary="Información del servicio y la carpeta de datos")
async def sistema():
    s = app.state.settings
    return {
        "version": __version__,
        "datos": describir(app.state.carpeta_datos),
        "api_key_activa": bool(s.api_key),
        "autostart_plcs": s.autostart_plcs,
        "siemens_transporte_por_defecto": s.siemens_transporte,
        "intervalos": {
            "publishing_interval_ms": s.publishing_interval_ms,
            "sampling_interval_ms": s.sampling_interval_ms,
            "s7_poll_interval_ms": s.s7_poll_interval_ms,
            "healthcheck_interval_s": s.healthcheck_interval,
        },
    }
