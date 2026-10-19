# Escritura en el PLC

Todo lo demás de este backend lee. Esto es lo único que **mueve cosas en
la planta**, así que va en su propio módulo (`api/escritura_routes.py`,
`stores/escritura_store.py`, `drivers/escritura.py`) y con tres candados.

## Los tres candados

### 1. Clave de API
Con `PLC_API_KEY` configurada, `POST /escritura` exige `X-API-Key` y el
comando `write` del WebSocket exige `?api_key=`. Sin clave configurada, el
backend está abierto: solo para desarrollo.

### 2. Lista blanca con límites
**Nada es escribible hasta que alguien lo habilita a mano**, al revés de
lo que declara el servidor OPC UA (que marca escribibles muchas más
variables de las que tiene sentido tocar desde un cliente). Habilitar es
una decisión de ingeniería que queda escrita: quién, cuándo, con qué
límites y con qué descripción.

```
PUT /escritura/permitidos  {"plc_id":"Linea1","tag":"DB1.setpoint","minimo":0,"maximo":90,"descripcion":"Consigna cuba 1"}
```

Los límites `minimo`/`maximo` son la segunda red: que un valor quepa en un
`Int16` no significa que la máquina lo admita (32000 cabe de sobra en una
consigna de temperatura cuyo máximo real son 90 °C). En booleanos y textos
se ignoran. Se guarda en `datos/escritura_permitida.json`; si ese fichero
se corrompe, el backend arranca con la lista **vacía** (fallo seguro). Al
quitar un PLC se borran sus permisos.

### 3. Auditoría
Cada intento queda en `datos/auditoria.jsonl` con usuario (cabecera
`X-Usuario` / `?usuario=`), tags, valor anterior, valor escrito, valor
confirmado y resultado: `plc.escritura`, `plc.escritura.denegada`,
`plc.escritura.fuera_de_rango`, `plc.escritura.rechazada_por_plc`,
`plc.escritura.fallida`, `plc.escritura.habilitada`, `plc.escritura.deshabilitada`.
Consulta: `GET /auditoria?accion=plc.escritura`.

## Conversión de tipos (`drivers/escritura.py`)

El valor llega en JSON y se convierte al tipo **exacto** del tag antes de
salir a la red. Nunca adivina: ante la duda, rechaza con un mensaje claro.

| Tipo del tag | Acepta | Rechaza |
|---|---|---|
| `Boolean` / `BOOL` | `true`/`false`, `0`/`1`, `"true" "false" "on" "off" "si" "no" "1" "0"`… | `7`, `"quizá"`, cualquier texto fuera de la lista |
| Enteros (`Int16`, `DINT`…) | enteros y textos numéricos **dentro del rango** del ancho; `3.0` | `3.7` (decimales), `40000` en Int16, booleanos |
| `Float` / `Double` / `REAL` | números, `"3.5"`, `"3,5"` | booleanos |
| `String` | textos y números (se convierten) | — |
| Arrays, structs, `ExtensionObject` | — | siempre (`es_escribible` = false; no se pueden habilitar) |

Las trampas que esto evita: `bool("false")` es `True` en Python (arrancaría
lo que se quería parar); un entero fuera de rango puede desbordar en
silencio en el PLC; un `int` contra un nodo `Float` falla en asyncua; un
decimal en una consigna entera se truncaría sin avisar.

Los nombres de tipo se aceptan en los dos vocabularios (`REAL` ↔ `Float`,
`DINT` ↔ `Int32`…): el driver S7comm habla IEC 61131 y el OPC UA habla OPC UA.

## Un lote, cuatro fases (`PlcManager.escribir_tags`)

```
POST /escritura {"escrituras":[{…},{…},{…}]}
  1. VALIDAR TODO sin tocar el PLC: existe, conectado, tipo, lista blanca, rango.
     → Si el tercero está fuera de rango, no se ha escrito el primero. 400/403/404/409.
  2. LEER los valores actuales (sin esto no hay a dónde volver).
  3. ESCRIBIR en orden. Cada driver RELEE: `confirmado` y `coincide`.
  4. Si uno falla al escribir → RESTAURAR los ya escritos en orden INVERSO.
     → 200 con ok:false, fallo_en, revertidos, sin_revertir.
```

Esto **no es una transacción**: entre la escritura y la vuelta atrás el
programa del PLC pudo actuar. Deja las *variables* como estaban, no la
máquina. Lo que de verdad protege es la lista blanca y los rangos: no
haber escrito nunca un valor imposible.

`coincide: false` significa que el PLC aceptó la escritura pero guardó
otra cosa: un `Real` que recorta precisión, o un bloque del programa que
sobrescribe la consigna en el siguiente ciclo de scan. La interfaz debe
enseñar `confirmado`, no `solicitado`.

## Por WebSocket

```json
{"tipo":"write","id":"btn-marcha","plc_id":"Linea1","tag":"DB1.marcha","valor":true}
```
→ `{"tipo":"write.result","id":"btn-marcha","ok":true,"resultados":[…]}` o
`{"tipo":"write.result","id":"btn-marcha","ok":false,"codigo":"denegado","error":"…"}`.

Pasa por **las mismas** comprobaciones y auditoría que el REST (misma
función `ejecutar_escritura`). El `id` vuelve tal cual para que el cliente
case la respuesta con la pulsación; un botón que no recibe respuesta se
queda «pulsando» para siempre, por eso se responde siempre, también al
fallar. Códigos: `no_autorizado`, `denegado`, `invalido`, `no_existe`,
`sin_conexion`, `error`.

## Recomendaciones de uso

* Habilita solo consignas y mandos pensados para operarse desde fuera;
  nunca salidas físicas directas ni variables de seguridad.
* Pon siempre `minimo`/`maximo` en consignas numéricas.
* Usa `descripcion`: «Consigna de temperatura · Cuba 1» dice más que
  `DB1.sp_t1` en un diálogo de confirmación.
* Manda siempre `X-Usuario` (o `?usuario=`) desde la aplicación que sabe
  quién está delante; sin él la auditoría dirá «anónimo».
* En S7comm, recuerda que un BOOL se escribe con lectura-modificación-
  escritura del byte: si el PLC cambia un bit vecino justo en medio, puede
  perderse. Agrupa los BOOL escribibles en bytes que el programa no toque.
