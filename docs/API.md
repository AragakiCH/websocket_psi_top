# API REST y WebSocket

Base: `http://<host>:8000`. Swagger interactivo en `/docs`, OpenAPI en
`/openapi.json`. Todos los cuerpos y respuestas son JSON.

## Autenticación

| Tipo de endpoint | Requisito |
|---|---|
| Lectura (`GET`, `/siemens/*`) | Ninguno |
| Modificación (`POST/PUT/DELETE /plcs`, `/discover`, `/escritura*`) | Cabecera `X-API-Key: <PLC_API_KEY>` si está configurada |
| WebSocket comando `write` | `?api_key=<PLC_API_KEY>` en la URL |

Cabecera opcional `X-Usuario: <nombre>` (o `?usuario=` en WS): identidad
que se graba en la auditoría. Sin `PLC_API_KEY` configurada todo está
abierto (desarrollo).

Errores: `{"detail": "mensaje en castellano"}` con el código HTTP que
corresponda (400 datos inválidos · 401 clave · 403 no permitido · 404 no
existe · 409 PLC no conectado · 502 el PLC no responde).

---

## Sistema

### `GET /health`
```json
{"status":"ok","num_plcs":2,"plcs_conectados":2,"total_tags":14,"clientes_ws":1,
 "plcs":[{"plc":"Linea1","nombre":"Linea1","transporte":"s7comm","endpoint":"s7://192.168.0.20:102/0/1",
          "conectado":true,"estado_conexion":"conectado","conectado_desde":"2026-10-16T17:37:31+00:00",
          "num_tags":7,"modo_lectura":"polling","modelo":"CPU 1214C DC/DC/Rly","ultimo_error":"",
          "reconexiones":0,"mensajes_emitidos":1532,"sampling_interval_ms":250,"publishing_interval_ms":500}]}
```
`estado_conexion`: `desconectado` · `conectando` · `conectado` · `reconectando`.
`ultimo_error` explica POR QUÉ un PLC no conecta.

### `GET /sistema`
Versión, carpeta de datos (ruta, escribible, ficheros), si la clave de API
está activa y los intervalos configurados.

### `GET /auditoria?limite=200&usuario=&accion=`
Últimos eventos (más reciente primero). `accion` filtra por prefijo
(`plc.escritura` trae también `plc.escritura.denegada`…).

---

## PLCs

### `GET /plcs`
```json
{"ids":["Linea1"],
 "plcs":[{"plc_id":"Linea1","endpoint":"s7://192.168.0.20:102/0/1","host":"192.168.0.20","port":102,
          "nombre":"Linea1","origen":"manual","transporte":"s7comm","rack":0,"slot":1,
          "s7_tags":"temperatura;DB1;0;REAL\n…","usuario":"", …campos de /health… }]}
```

### `POST /plcs` — dar de alta
| Campo | Tipo | Notas |
|---|---|---|
| `host` | str | IP, hostname, `opc.tcp://ip:4840` o `s7://ip:102/rack/slot` |
| `puerto` | int | OPC UA (defecto 4840). Se ignora en S7comm |
| `transporte` | `opcua` \| `s7comm` | Vacío = `PLC_SIEMENS_TRANSPORTE`. Se deduce del prefijo de `host` |
| `nombre` | str | Nombre amigable; se usa como `plc_id` (saneado). Vacío = la IP |
| `rack`, `slot` | int | S7comm. Slot 1 en S7-1200/1500, 2 en S7-300 |
| `s7_tags` | str | S7comm, obligatorio. Una línea por tag: `nombre;DB1;offset;TIPO` |
| `usuario`, `password` | str | OPC UA con autenticación (opcional) |

```json
→ {"ok":true,"plc_id":"Linea1","endpoint":"s7://192.168.0.20:102/0/1","transporte":"s7comm",
   "mensaje":"PLC Linea1 (s7comm) añadido; conectando..."}
```
Responde al instante; la conexión se intenta en segundo plano. Si el
endpoint ya existe: `{"ok":false,"plc_id":"…","mensaje":"Ese PLC ya está gestionado"}`
(200). Lista `s7_tags` mal formada → 400 con el número de línea.

### `PUT /plcs/{plc_id}` — cambiar configuración
Mismos campos que el alta, todos opcionales; solo se aplican los que lleguen.
Conserva el `plc_id` y reconecta. Uso típico: actualizar `s7_tags` tras
cambiar el programa del PLC.

### `POST /plcs/{plc_id}/reconectar`
Para y arranca el handler sin tocar la configuración.

### `DELETE /plcs/{plc_id}`
Cierra la conexión, olvida el catálogo de tags y los permisos de escritura.
Difunde `{"type":"plc_removed"}`.

### `POST /discover`
Escanea `PLC_DISCOVERY_SUBNET` (o la /24 del endpoint semilla) en el puerto
4840 y añade los servidores OPC UA nuevos. `{"ok":true,"encontrados":2,"nuevos":["PLC_1"]}`.

---

## Tags

### `GET /tags?plc=<id>`
```json
{"plc":"Linea1","tags":[{"plc":"Linea1","tag":"DB1.temperatura","name":"temperatura","db":"DB1",
  "node_id":"DB1.DBD0","type":"REAL","value":24.46,"timestamp":"…","source_ts":"…","delta_ms":1001.5}]}
```
Sin conexión, `value`, `timestamp` y `delta_ms` son `null`, pero el tag
sigue listado (catálogo persistido).

### `GET /browse?plc=<id>`
Árbol por PLC y Data Block: `{"plcs":[{"plc":"…","data_blocks":{"DB1":[{"name","full_name","node_id","type"}]}}]}`.

---

## Siemens (previo al alta)

### `POST /siemens/identificar`
`{"host":"192.168.0.10","rack":0,"slot":null,"timeout_ms":2500}` (slot vacío = prueba 1, 2 y 0).
```json
{"ok":true,"modelo":"CPU 1214C DC/DC/Rly","familia":"S7-1200","nombre":"PLC_1","serie":"S C-…",
 "estado":"Run","rack":0,"slot":1,"sin_identidad":false,"opcua":false,"s7comm":true,
 "transporte_sugerido":"s7comm","mensaje":"… es un S7-1200 y no responde en el 4840: …"}
```
Un S7-1200 responde por el 102 pero a menudo **no publica su modelo**
(`sin_identidad: true`); se asume familia S7-1200. 502 si no responde por
ninguno de los dos puertos.

### `POST /siemens/sondear-dbs`
`{"host":"…","rack":0,"slot":1,"dbs":[1,2,3]}` → `{"dbs":{"1":36,"3":120}}`.
Qué DB existen y de qué tamaño (los optimizados no aparecen). Vacío = 1..40.

### `POST /siemens/importar`
```json
{"host":"192.168.0.20","rack":0,"slot":1,
 "fuentes":[{"nombre":"Data_block_1.db","contenido":"DATA_BLOCK \"Data_block_1\" …"},
            {"nombre":"UDT_Analog.udt","contenido":"TYPE \"UDT_Analog\" …"}]}
```
```json
{"ok":true,
 "bloques":[{"nombre":"Data_block_1","clase":"global","optimizado":false,"tamano":36,"db":1,"db_como":"nombre",
             "tags":[{"nombre":"marcha","offset":0,"bit":0,"tipo":"BOOL","longitud":0,"linea":"marcha;DB1;0.0;BOOL"},…],
             "s7_tags":"marcha;DB1;0.0;BOOL\ntemperatura;DB1;2;REAL\n…","avisos":[]}],
 "dbs_plc":{"1":36},"avisos":[],
 "s7_tags":"marcha;DB1;0.0;BOOL\ntemperatura;DB1;2;REAL\n…"}
```
`s7_tags` (global, con todos los bloques emparejados y no optimizados) se
manda tal cual a `POST /plcs`. `db_como`: `nombre` (el nombre decía el
número y existe), `tamaño` (único DB con ese tamaño), `nombre (sin sondear)`
o vacío (elegir a mano). El `contenido` puede ser también las filas del
editor del DB copiadas de TIA (Ctrl+A, Ctrl+C).

### `POST /siemens/validar-tags`
`{"s7_tags":"…"}` → `{"ok":true,"num_tags":2,"dbs":[1],"tags":[{"nombre","db","direccion","tipo","tipo_iec","bytes"}]}`
o `{"ok":false,"error":"Línea 3: …"}`.

---

## Escritura

Ver `docs/ESCRITURA.md` para el modelo de seguridad.

### `GET /escritura/candidatos?plc_id=<id>`
Tags del PLC cuyo tipo admite escritura, con `habilitado`, `minimo`, `maximo`.

### `GET /escritura/permitidos?plc_id=`
Lista blanca actual.

### `PUT /escritura/permitidos`
`{"plc_id":"Linea1","tag":"DB1.setpoint","minimo":0,"maximo":90,"descripcion":"Consigna"}`
→ `{"ok":true,"tag":{…,"habilitado_por":"hugo","habilitado_en":"…"}}`.
Se comprueba contra el PLC real (404 si el tag no existe, 400 si su tipo no
se puede escribir o `minimo > maximo`).

### `DELETE /escritura/permitidos/{plc_id}/{tag}`

### `POST /escritura`
```json
{"escrituras":[{"plc_id":"Linea1","tag":"DB1.setpoint","valor":65},
               {"plc_id":"Linea1","tag":"DB1.marcha","valor":true}]}
```
```json
{"ok":true,"escrituras":2,"usuario":"hugo","timestamp":"…",
 "resultados":[{"plc_id":"Linea1","tag":"DB1.setpoint","solicitado":65,"escrito":65.0,"confirmado":65.0,
                "data_type":"REAL","anterior":50.0,"coincide":true,"ok":true}, …]}
```
Si un tag del lote falla al escribir (200 con `ok:false`):
`{"ok":false,"fallo_en":"DB1.marcha","motivo":"…","revertidos":["DB1.setpoint"],"sin_revertir":[],"resultados":[…]}`.
Si falla la validación previa, no se escribe nada y responde 400/403/404/409.

---

## WebSocket `/ws`

`ws://host:8000/ws?plc=<id>&api_key=<k>&usuario=<nombre>` (todos opcionales).

**Servidor → cliente**

| Mensaje | Cuándo | Forma |
|---|---|---|
| `snapshot` | al conectar; al agregar/conectar/reconectar un PLC; al pedirlo | `{"type":"snapshot","timestamp","plcs":{id:{nombre,transporte,endpoint,estado,conectado,modelo,modo_lectura,sampling_interval_ms,publishing_interval_ms}},"tags":{"id\|tag":{plc,tag,value,type,timestamp,source_ts,server_ts,delta_ms}}}` |
| cambio de valor | cada cambio en el PLC | `{"timestamp","plc","tag","value","type","source_ts","server_ts","delta_ms"}` — **si trae `tag` es dato** |
| `status` | cambia el estado de un PLC | `{"type":"status","plc","status":"conectado"\|"reconectando","error"?}` |
| `plc_removed` | alguien quitó un PLC | `{"type":"plc_removed","plc_removed":"id"}` |
| `presence` | entra/sale un cliente | `{"type":"presence","usuarios":[{"usuario"}],"num_clientes"}` |
| `pong` | respuesta a `ping` | `{"tipo":"pong","id"}` |
| `write.result` | respuesta a `write` | `{"tipo":"write.result","id","ok",…igual que POST /escritura…}` o `{"ok":false,"codigo":"denegado"\|"invalido"\|"no_existe"\|"sin_conexion"\|"no_autorizado"\|"error","error":"…"}` |

Con `?plc=<id>` el cliente solo recibe los mensajes de ese PLC (los de
control sin campo `plc` llegan a todos). El cliente debe reemplazar su
lista entera de tags en cada `snapshot` y aplicar los cambios sueltos
sobre ella. `type` en un cambio de valor es el **tipo de dato**, no el
tipo de mensaje.

**Cliente → servidor** (JSON; cualquier otra cosa se ignora)

```json
{"tipo":"ping","id":1}
{"tipo":"snapshot"}
{"tipo":"write","id":"w1","plc_id":"Linea1","tag":"DB1.setpoint","valor":65}
{"tipo":"write","id":"w2","escrituras":[{"plc_id":"Linea1","tag":"DB1.setpoint","valor":65}, …]}
```

Ejemplo mínimo desde un navegador:
```js
const ws = new WebSocket("ws://localhost:8000/ws?usuario=ana");
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.tag) console.log(m.plc, m.tag, m.value); };
```
