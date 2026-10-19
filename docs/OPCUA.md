# Siemens por OPC UA (`OpcUaDriver`)

Transporte para **S7-1500** (siempre tiene servidor OPC UA) y **S7-1200 con
FW ≥ 4.4** (hay que activarlo y licenciarlo). Un S7-300/400 no lo tiene
nunca: para esos, `docs/S7COMM.md`.

## Preparar el PLC en TIA Portal

1. **Activar el servidor**: Propiedades de la CPU → *OPC UA* → *Servidor* →
   ☑ *Activar servidor OPC UA*. Anota el puerto (4840 por defecto).
2. **Licencia runtime**: *OPC UA* → *Licencias* → elige la que corresponda
   (S7-1200: *SIMATIC OPC UA S7-1200 Basic*; S7-1500: small/medium/large
   según CPU). Sin ella el servidor no arranca.
3. **Exponer los DB**: en cada DB, columna *Accesible desde OPC UA* marcada
   en las variables que quieras leer, y *Escribible desde OPC UA* en las que
   quieras escribir. Por defecto vienen marcadas en DB nuevos.
4. **Seguridad**: por defecto el backend conecta con *None* + anónimo. Si
   el PLC solo acepta sesiones seguras, ver «Seguridad» abajo.
5. **Intervalo mínimo**: *OPC UA* → *Servidor* → *Configuración* → *Intervalo
   de muestreo mínimo*. S7-1500: 100 ms; S7-1200: 1000 ms. El backend pide
   `PLC_SAMPLING_INTERVAL_MS`; el servidor lo sube a su mínimo si hace falta.
6. Compilar y cargar (hardware y software).

Comprobación rápida: `POST /siemens/identificar {"host": "…"}` → `"opcua": true`.

## Qué hace el driver

| Paso | Detalle |
|---|---|
| Conectar | `asyncua.Client(url, timeout=PLC_OPCUA_REQUEST_TIMEOUT)`; usuario/contraseña y política de seguridad si están configurados. Lee el modelo (`DeviceSet/PLC_x/Model` u `OrderNumber`). Carga las definiciones de UDT del servidor (S7-1500 FW ≥ 2.5) para decodificar estructuras. |
| Browse | `Objects/DeviceSet/<PLC_BROWSE_PLC_NAME>/DataBlocksGlobal/<DB>/…` buscando por *BrowseName* (la ruta cruza namespaces). Si el nombre del PLC no coincide, busca `DataBlocksGlobal` bajo cualquier hijo de `DeviceSet`. Recursivo: entra en Objects y en Variables con hijos (structs/UDT). Filtro opcional de DBs (`PLC_TAGS_FILTER_DBS` o `tags_filter.yaml`). |
| Tags | `full_name = DB.miembro.submiembro`, `node_id = ns=3;s="DB"."miembro"`, `data_type` del servidor (`Float`, `Boolean`, `Int16`, `String`…). Arrays: `Float[20]`. Structs no decodificables (ExtensionObject opaco) se omiten con aviso. |
| Subscription | Una por PLC: `create_subscription(period=PLC_PUBLISHING_INTERVAL_MS)` + `subscribe_data_change(todos los nodos, sampling_interval=PLC_SAMPLING_INTERVAL_MS, queuesize=PLC_SUBSCRIPTION_QUEUE_SIZE)`. Si el lote falla, uno a uno. |
| Cambios | Cada `datachange_notification` produce un `TagValue` con `source_ts` (SourceTimestamp: cuándo cambió en el PLC), `server_ts` y `delta_ms` (ms desde el cambio anterior del mismo tag). |
| Lectura en bloque | `read_values` en lotes de 250 nodos (reconciliación y snapshot inicial). |
| Watchdog | `server_state.read_value()` cada `PLC_HEALTHCHECK_INTERVAL`, acotado por `PLC_OPCUA_REQUEST_TIMEOUT`. Un PLC apagado se detecta en ≈ intervalo + timeout. |
| Subscription caducada | El servidor manda `StatusChangeNotification` (BadTimeout) y deja la sesión viva; el driver marca `subscription_caida` y el handler la recrea sin reconectar. |
| Escritura | Pregunta el tipo al servidor (`read_data_type_as_variant_type`), convierte con `escritura.convertir`, `write_value(DataValue(Variant(v, tipo)))` y **relee**. `BadUserAccessDenied`/`BadNotWritable` → `PermissionError` con instrucción para TIA. |

## Tipos OPC UA ↔ S7

| S7 | OPC UA (`data_type`) | Escritura |
|---|---|---|
| Bool | `Boolean` | true/false, 0/1, "on"/"off"… |
| SInt / USInt / Byte | `SByte` / `Byte` | entero en rango |
| Int / UInt / Word | `Int16` / `UInt16` | entero en rango |
| DInt / UDInt / DWord / Time | `Int32` / `UInt32` | entero en rango |
| LInt / ULInt / LWord | `Int64` / `UInt64` | entero en rango |
| Real / LReal | `Float` / `Double` | número (también "3,5") |
| String / WString | `String` | texto |
| Char / WChar | `Byte` / `UInt16` | — |
| Date, TOD, DTL, DT | `UInt16`, `UInt32`, struct… | no |
| Array of X | `X[n]` | no |
| Struct / UDT | se expande en sus miembros | cada miembro por separado |

## Seguridad y autenticación

Por defecto: política *None*, modo *None*, sesión anónima. Para un PLC
que exija más:

```ini
PLC_OPCUA_USERNAME=operador
PLC_OPCUA_PASSWORD=secreto
PLC_SECURITY_POLICY=Basic256Sha256
PLC_SECURITY_MODE=SignAndEncrypt
PLC_CLIENT_CERT_PATH=C:\certs\client_cert.pem
PLC_CLIENT_PRIVATE_KEY_PATH=C:\certs\client_key.pem
# PLC_SERVER_CERT_PATH=C:\certs\plc_cert.der   (opcional)
```

Generar un certificado de cliente autofirmado (requiere `openssl`):
```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout client_key.pem -out client_cert.pem -days 3650 \
   -subj "/CN=PsiTop" -addext "subjectAltName=URI:urn:psi:top,DNS:localhost"
```
Luego, en TIA: *OPC UA* → *Servidor* → *Seguridad* → certificados de
cliente de confianza → importar `client_cert.pem`. Usuario/contraseña por
PLC se pueden mandar también en `POST /plcs` (`usuario`, `password`; la
contraseña se guarda cifrada).

## Rendimiento

* El coste principal es la subscription: N MonitoredItems a `sampling_interval`.
  Un S7-1500 maneja miles; un S7-1200, unos cientos a 1000 ms.
* La reconciliación lee TODO en bloque cada 5 s: para 500 tags en un
  S7-1500 son unos milisegundos; en un S7-1200 con 1000 tags puede notarse
  — sube `PLC_HEALTHCHECK_INTERVAL` a 15–30 s.
* Un DB con cientos de variables que no interesan: usa el filtro de DBs.
* `delta_ms` en los mensajes es la forma más directa de ver el ritmo real
  que entrega el PLC.

## Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `Connection refused` al conectar | No hay servidor OPC UA (S7-1200 FW < 4.4, servidor no activado, sin licencia) | `/siemens/identificar`; activar en TIA o usar S7comm |
| Conecta pero `No se pudo localizar 'DataBlocksGlobal'` | Ningún DB marcado accesible, o el servidor no es un S7 | Marcar *Accesible desde OPC UA* en los DB |
| Conectado pero no llegan cambios | Subscription muda (pausa del equipo, programa recargado) | La reconciliación lo corrige en ≤ 2 vueltas del watchdog; mira el log «Se recrea» |
| `BadNotWritable` al escribir | Variable no marcada *Escribible desde OPC UA* | Marcarla en el DB, compilar, cargar |
| `BadTooManyMonitoredItems` | Límite del servidor | Filtrar DBs o repartir en dos PLC lógicos no es posible: reducir tags |
| Se omiten structs con aviso | UDT sin miembros expuestos / sin definiciones de tipo | Marcar sus miembros accesibles, o FW ≥ 2.5 en S7-1500 |
| Tags duplicados entre PLCs | Dos PLC con el mismo `nombre` | Los `plc_id` se desambiguan solos (`PLC_1`, `PLC_1_2`); mejor dar nombres distintos |
