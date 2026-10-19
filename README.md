# PSI Top · Backend Siemens (OPC UA + S7comm) → WebSocket

Servicio backend en Python que se conecta a PLCs **Siemens**, obtiene sus
tags y transmite los valores en **tiempo real** a cualquier cliente vía
**WebSocket**, con escritura controlada (lista blanca + auditoría). Solo
backend: no incluye ninguna vista.

| | **OPC UA** (puerto 4840) | **S7comm** (puerto 102) |
|---|---|---|
| PLCs | S7-1500 · S7-1200 con FW ≥ 4.4 y servidor OPC UA activado | S7-1200 (cualquier FW) · S7-300/400 · ET 200SP |
| Driver | `OpcUaDriver` (asyncua) | `S7CommDriver` (python-snap7) |
| Tags | **Descubrimiento automático** (browse de `DataBlocksGlobal`) | Lista `nombre;DB;offset;TIPO`, **calculable desde las fuentes de TIA** |
| Tiempo real | Subscription nativa (MonitoredItems) | Polling en bloque por DB, solo emite cambios |
| Escritura | `write_value` con el Variant del servidor + relectura | `db_write` de los bytes del tag + relectura |
| Endpoint | `opc.tcp://192.168.0.10:4840` | `s7://192.168.0.20:102/0/1` |

Varios PLCs (de los dos transportes) conviven a la vez; cada uno tiene su
driver, su supervisor y su reconexión. Un PLC caído no afecta a los demás.

---

## Instalación (venv)

Requisitos: **Python 3.10 – 3.13** (probado con 3.11 y 3.13). Nada más:
`python-snap7` 3.x trae la librería nativa dentro del wheel.

**Windows**
```bat
scripts\setup.bat          REM crea .\venv, instala requirements.txt y copia .env.example a .env
scripts\run.bat            REM arranca en http://localhost:8000
```

**Linux / macOS**
```bash
scripts/setup.sh
scripts/run.sh
```

**A mano**
```bash
python -m venv venv
venv\Scripts\activate            # Windows   |   source venv/bin/activate   # Linux
pip install -r requirements.txt
copy .env.example .env           # y edita PLC_API_KEY
python run.py                    # o: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Documentación interactiva (Swagger): **http://localhost:8000/docs**

Pruebas y linter (instalan pytest y ruff):
```bash
scripts\setup.bat dev      # o scripts/setup.sh dev
scripts\test.bat           # o scripts/test.sh   (29 pruebas, sin red ni PLC)
```

---

## Probar sin PLC

Dos simuladores incluidos (en dos terminales, con el venv activado):

```bash
python tools/simulador_opcua.py          # imita un S7-1500 en opc.tcp://localhost:4840
python tools/simulador_s7.py             # imita un S7-1200 por S7comm en el puerto 102 (Windows: sin más; Linux: como root)
python run.py
```

```bash
# Alta del PLC OPC UA
curl -X POST http://localhost:8000/plcs -H "Content-Type: application/json" -H "X-API-Key: <clave>" ^
     -d "{\"host\": \"127.0.0.1\", \"nombre\": \"Simulado\"}"

# Alta del PLC S7comm (la lista de tags la imprime el simulador al arrancar)
curl -X POST http://localhost:8000/plcs -H "Content-Type: application/json" -H "X-API-Key: <clave>" ^
     -d "{\"host\": \"127.0.0.1\", \"transporte\": \"s7comm\", \"slot\": 1, \"s7_tags\": \"temperatura;DB1;0;REAL\\nmarcha;DB1;4.0;BOOL\"}"

# Ver los datos en vivo
python tools/cliente_ws.py
```

---

## Flujo de uso con un PLC real

1. **`POST /siemens/identificar`** `{"host": "192.168.0.10"}` → modelo de CPU,
   si tiene OPC UA y el **transporte sugerido**.
2. Si es S7comm: **`POST /siemens/importar`** con las fuentes de TIA
   (*Generate source from blocks* → `.db`/`.udt`) o las filas del editor del
   DB pegadas → devuelve `s7_tags` con los offsets calculados.
3. **`POST /plcs`** → da de alta el PLC (queda guardado en `datos/plcs.json`
   y se reconecta solo al reiniciar).
4. **`ws://host:8000/ws`** → `snapshot` inicial + cambios en vivo.
5. **`PUT /escritura/permitidos`** + **`POST /escritura`** → escribir.

Lo que hay que preparar en **TIA Portal** está en
[`docs/OPCUA.md`](docs/OPCUA.md) y [`docs/S7COMM.md`](docs/S7COMM.md).

---

## Estructura del proyecto

```
app/
  main.py                   FastAPI + lifespan (arranque/parada limpia), CORS, routers
  config/
    settings.py             Configuración (env / .env, prefijo PLC_) + copia por PLC
    rutas.py                Carpeta de datos (desarrollo: ./datos; empaquetado: ProgramData)
    tags_filter.yaml        Filtro opcional de DBs para OPC UA
  drivers/                  ← protocolo. Lo único que sabe de OPC UA o de snap7
    plc_driver.py           Interfaz abstracta PlcDriver + TagInfo / TagValue
    opcua_driver.py         Siemens por OPC UA (browse, subscription, read/write)
    s7comm_driver.py        Siemens por S7comm (polling por DB, identificar, sondear DBs)
    s7_fuentes.py           Fuentes de TIA → offsets (parser .db/.udt/.scl y tabla pegada)
    escritura.py            Conversión segura JSON → tipo del PLC (rangos, booleanos…)
    registry.py             Fábrica: transporte → clase de driver (punto de extensión)
  core/                     ← orquestación, independiente del protocolo
    plc_manager.py          Multi-PLC: alta/baja/cambio, snapshot, health, escritura con vuelta atrás
    subscription_handler.py Por PLC: conectar → browse → subscribe, watchdog, reconexión, reconciliación
    connection_manager.py   Clientes WebSocket, broadcast concurrente, observadores internos
    plc_discovery.py        Escaneo de subred + validación OPC UA, endpoints fijos
    endpoint.py             EndpointPlc (lo que se persiste) y utilidades de host/puerto/id
    auditoria.py            Registro JSONL de quién hizo qué (hilo aparte, rotación)
  stores/                   ← persistencia en JSON, escritura atómica, tolerante a corrupción
    plc_store.py            datos/plcs.json (contraseñas cifradas con Fernet)
    tags_store.py           datos/tags_plc.json (catálogo de tags por PLC)
    escritura_store.py      datos/escritura_permitida.json (lista blanca con límites)
  api/                      ← HTTP / WebSocket
    plc_routes.py           /health /plcs /discover /tags /browse /auditoria
    siemens_routes.py       /siemens/identificar /sondear-dbs /importar /validar-tags
    escritura_routes.py     /escritura y /escritura/permitidos
    websocket_routes.py     /ws
    deps.py                 Inyección de dependencias y clave de API
tests/                      pytest: conversión, S7comm, fuentes TIA y API de punta a punta (driver falso)
tools/                      simulador_opcua.py · simulador_s7.py · cliente_ws.py
scripts/                    setup / run / test para Windows y Linux
docs/                       ARQUITECTURA · API · OPCUA · S7COMM · ESCRITURA · EXTENDER
```

---

## Configuración

Todo por variables de entorno o `.env` con prefijo `PLC_`
([`.env.example`](.env.example) documenta cada una). Las importantes:

| Variable | Defecto | Para qué |
|---|---|---|
| `PLC_API_PORT` | `8000` | Puerto HTTP/WS |
| `PLC_API_KEY` | *(vacía)* | Clave para todo lo que modifica (cabecera `X-API-Key`, o `?api_key=` en WS). **Ponla en planta.** |
| `PLC_DATOS_DIR` | `./datos` | Dónde se guardan PLCs, tags, lista blanca y auditoría |
| `PLC_SIEMENS_TRANSPORTE` | `opcua` | Transporte por defecto (`opcua` / `s7comm`) |
| `PLC_SAMPLING_INTERVAL_MS` | `1000` | Muestreo OPC UA (mín. 100 ms en S7-1500, 1000 en S7-1200) |
| `PLC_PUBLISHING_INTERVAL_MS` | `500` | Publicación OPC UA |
| `PLC_S7_POLL_INTERVAL_MS` | `250` | Polling S7comm |
| `PLC_HEALTHCHECK_INTERVAL` | `5.0` | Watchdog + reconciliación (s) |
| `PLC_AUTOSTART_PLCS` | `false` | Conectar también `PLC_STATIC_ENDPOINTS` / escanear al arrancar |

---

## Mensajes del WebSocket (resumen)

```jsonc
// Al conectar (y cada vez que se agrega/conecta un PLC). Clave: "<plc_id>|<tag>"
{"type":"snapshot","plcs":{"Linea1":{"estado":"conectado","modelo":"CPU 1214C DC/DC/Rly","transporte":"s7comm"}},
 "tags":{"Linea1|DB1.temperatura":{"plc":"Linea1","tag":"DB1.temperatura","value":24.4,"type":"REAL","timestamp":"…","delta_ms":1001}}}

// Cambio de valor (si trae "tag", es dato; si no, es control)
{"timestamp":"…","plc":"Linea1","tag":"DB1.temperatura","value":24.9,"type":"REAL","source_ts":"…","server_ts":"","delta_ms":250}

// Control
{"type":"status","plc":"Linea1","status":"reconectando","error":"…"}
{"type":"plc_removed","plc_removed":"Linea1"}
{"type":"presence","usuarios":[{"usuario":"ana"}],"num_clientes":2}
```

Comandos del cliente: `{"tipo":"ping"}`, `{"tipo":"snapshot"}` y
`{"tipo":"write","id":"w1","plc_id":"Linea1","tag":"DB1.setpoint","valor":65}`
(responde `{"tipo":"write.result","id":"w1","ok":true,…}`). Detalle completo
en [`docs/API.md`](docs/API.md).

---

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) | Capas, flujo de datos, ciclo de vida de un PLC, decisiones de diseño, escalabilidad |
| [`docs/API.md`](docs/API.md) | Todos los endpoints REST y el contrato del WebSocket, con ejemplos |
| [`docs/OPCUA.md`](docs/OPCUA.md) | S7-1500 / S7-1200 por OPC UA: TIA Portal, browse, subscriptions, tipos, seguridad |
| [`docs/S7COMM.md`](docs/S7COMM.md) | S7-1200/300/400 por S7comm: TIA Portal, formato de tags, importar fuentes, offsets |
| [`docs/ESCRITURA.md`](docs/ESCRITURA.md) | Lista blanca, conversión de tipos, vuelta atrás, auditoría |
| [`docs/EXTENDER.md`](docs/EXTENDER.md) | Cómo añadir un driver (Modbus…), un observador (historizador) o tu propio sistema de usuarios |
| [`docs/DESPLIEGUE.md`](docs/DESPLIEGUE.md) | Servicio de Windows / systemd, carpeta de datos, copias de seguridad, problemas frecuentes |
