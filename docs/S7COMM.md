# Siemens por S7comm (`S7CommDriver`)

Transporte para los PLC que **no tienen servidor OPC UA**: **S7-1200**
con firmware < 4.4 (o ≥ 4.4 sin activarlo/licenciarlo), **S7-300/400**,
**ET 200SP CPU**. También sirve para un S7-1500 si no se quiere abrir el
4840. Protocolo S7comm (ISO-on-TCP, puerto **102**) con `python-snap7` 3.x,
que trae la librería nativa dentro del wheel (no hay DLL que instalar).

## Preparar el PLC en TIA Portal

Sin esto el PLC conecta pero **no lee nada** (y el error que da el PLC es
críptico: *Address out of range* / *Item not available*):

1. **PUT/GET**: Propiedades de la CPU → *Protección y seguridad* →
   *Mecanismos de conexión* → ☑ *Permitir acceso vía comunicación PUT/GET
   desde el interlocutor remoto*. (En un S7-1200 V3.x no existe: está
   siempre abierto. En S7-300/400 tampoco hace falta.)
2. **Acceso estándar en cada DB** que se vaya a leer: Propiedades del DB →
   *Atributos* → ☐ *Acceso optimizado al bloque* **desmarcado**. Un DB
   optimizado no tiene direcciones absolutas y S7comm no puede leerlo.
3. **Compilar y cargar**. Tras compilar, la columna *Offset* del editor del
   DB muestra la dirección de cada variable: eso es lo que necesita la lista
   de tags (o déjaselo calcular a `/siemens/importar`).

Rack/slot: **0/1** en S7-1200 y S7-1500, **0/2** en S7-300 (el slot de la
CPU en el bastidor), 0/0 ó 0/3 en algunos S7-400. `/siemens/identificar`
sin `slot` prueba 1, 2 y 0.

## La lista de tags (`s7_tags`)

S7comm lee bytes por dirección absoluta: **no sabe nombres ni tipos**. Por
eso cada PLC S7comm lleva su lista, una línea por tag:

```
nombre;DB;offset;TIPO
temperatura;DB1;0;REAL
marcha;DB1;4.0;BOOL            ← BOOL lleva byte.bit
paro;DB1;4.1;BOOL
contador;DB1;6;DINT
texto;DB1;10;STRING[20]        ← STRING con longitud máxima
velocidad;DB1.DBW32;INT        ← también vale la dirección absoluta
# las líneas con # se ignoran
```

Separador `;` o `,`. Tipos: `BOOL BYTE CHAR SINT USINT WORD INT UINT DWORD
DINT UDINT REAL TIME LWORD LINT ULINT LREAL STRING[n]`. `TIME` se expone
como `DINT` (ms). `DTL`, `DATE_AND_TIME`, `WSTRING` no se soportan como
valor (el importador reserva su espacio y avisa).

Validar antes del alta: `POST /siemens/validar-tags`. El error lleva el
número de línea.

El `full_name` del tag queda `DB<n>.<nombre>` (ej. `DB1.temperatura`) y el
`node_id` es la dirección absoluta (`DB1.DBD0`, `DB1.DBX4.0`, `DB1.DBB10`).

### Calcular la lista desde TIA (recomendado)

Copiar offsets a mano es la fuente de errores número uno. Dos caminos:

**A) Fuentes de bloque.** En TIA, selecciona los DB (y los UDT/FB que usen)
→ clic derecho → *Generar fuente a partir de bloques* (`.db`, `.udt`,
`.scl`). Manda el texto a `POST /siemens/importar`:

```json
{"host":"192.168.0.20","rack":0,"slot":1,
 "fuentes":[{"nombre":"Data_block_1.db","contenido":"…"},{"nombre":"UDT_Analog.udt","contenido":"…"}]}
```

El backend recalcula los offsets con las reglas de un DB de acceso estándar
(las mismas que aplica TIA):

* `BOOL` ocupa un bit; los BOOL consecutivos se empaquetan en un byte.
* 1 byte (`BYTE CHAR SINT USINT`) → siguiente byte libre.
* ≥ 2 bytes (`INT REAL DINT LREAL`…) → empieza en byte **par**.
* `STRING[n]` → n + 2 bytes, empieza en byte par.
* `ARRAY` y `STRUCT`/UDT → empiezan en par y rellenan hasta par al acabar;
  `ARRAY of BOOL` empaqueta bits.
* El DB entero termina en byte par.

Y si llega `host`, **sondea el PLC** (`db_read` de 1 byte + búsqueda binaria
del tamaño) para saber qué DB existen y emparejar cada bloque con su
número: por el nombre (`Data_block_1` → DB1) o por tamaño único. Un DB con
`S7_Optimized_Access := 'TRUE'` sale marcado y no se incluye en `s7_tags`.

**B) Tabla pegada.** En el editor del DB: Ctrl+A, Ctrl+C, y pega el texto
como `contenido` (con o sin cabecera). Si la columna *Offset* trae números
se usan tal cual; si no (DB sin compilar), se calculan.

La respuesta trae `s7_tags` listo para `POST /plcs`.

## Qué hace el driver

| Paso | Detalle |
|---|---|
| Conectar | `snap7.client.Client().connect(host, rack, slot, 102)` con `PingTimeout`/`RecvTimeout` = `PLC_S7_CONNECT_TIMEOUT`. Lee el modelo con `get_cpu_info` (SZL); un S7-1200 suele no responderlo. |
| «Browse» | Parsea `s7_tags`, construye el catálogo y hace **una lectura de prueba**: si un DB es optimizado o PUT/GET está cerrado, el alta falla con un mensaje que dice qué marcar en TIA, en vez de dejar un PLC «conectado» sin datos. |
| Lectura | Por cada DB, **un solo `db_read`** del primer al último byte usado por sus tags, y se decodifica cada tag del búfer (big-endian, `struct`). Independiente de `snap7.util`. |
| Tiempo real | **Polling** cada `PLC_S7_POLL_INTERVAL_MS` (mín. 50). Solo se emite lo que cambia respecto al último valor. `source_ts` = hora de lectura. Tres fallos seguidos → se da la conexión por caída y el supervisor reconecta. |
| Hilos | snap7 es síncrono: cada llamada va al executor con un lock por driver. |
| Escritura | `convertir(valor, tipo)` → `db_write` de los bytes del tag. Un `BOOL` hace lectura-modificación-escritura de su byte (los bits vecinos no se tocan). `STRING` escribe `[máx][len][chars]` truncando a la longitud. Luego **relee**. |
| Identificar | `identificar(host, rack, slot)`: conecta como OP (no ocupa la conexión PG de TIA), SZL → modelo, nombre, serie, estado RUN/STOP. Funciona sin PUT/GET ni DBs estándar. |

## Rendimiento y límites

* PDU del S7-1200: 240 bytes; S7-300: 240; S7-1500: 480–960. snap7 trocea
  automáticamente un `db_read` mayor, pero cada trozo es un viaje: para un
  DB de 2 KB a 250 ms de polling en un S7-1200, cuenta ~9 peticiones por
  ciclo. Agrupa los tags en pocos DB y no incluyas los que no necesitas.
* Un S7-1200 admite pocas conexiones simultáneas (8 en total con HMI y
  PG): este backend usa UNA por PLC.
* `PLC_S7_POLL_INTERVAL_MS` por debajo de 100 ms rara vez aporta: el ciclo
  de scan del PLC y la latencia Ethernet mandan.

## Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `No se pudo abrir S7comm… Connection refused` | IP/slot incorrectos, firewall en el 102 | Ping; `/siemens/identificar`; slot 1 (1200/1500) o 2 (300) |
| `Conectado… pero no se pueden leer los DB` | DB optimizado o PUT/GET cerrado | Pasos 1-2 de TIA; compilar y cargar |
| Valores absurdos (números enormes, booleanos cambiados) | Offsets o tipos equivocados | Recalcular con `/siemens/importar`; comparar con la columna Offset de TIA |
| `Línea N: …` al dar de alta | Sintaxis de `s7_tags` | `/siemens/validar-tags` |
| STRING sale vacío o cortado | Longitud declarada distinta a la del DB | Usa la misma `STRING[n]` que en TIA |
| Modelo vacío en `/health` | S7-1200 no publica SZL | Normal; la familia se asume S7-1200 |
| Lee bien y de repente «reconectando» | Otro cliente agotó las conexiones del 1200, o cable | El supervisor reintenta con backoff; mira `ultimo_error` |
| Escritura rechazada | PUT/GET cerrado, DB optimizado, o el programa sobrescribe la variable cada ciclo | `coincide:false` en la respuesta delata lo último |
