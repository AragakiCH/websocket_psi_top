# Arquitectura

## Capas

```
┌──────────────────────────────────────────────────────────────────────────┐
│  api/            FastAPI: REST (/plcs, /tags, /escritura, /siemens/…)    │
│                  + WebSocket (/ws). Valida cuerpos, mapea errores a HTTP. │
├──────────────────────────────────────────────────────────────────────────┤
│  core/           PlcManager ──┬── SubscriptionHandler (uno por PLC)        │
│                               │       · conectar → browse → subscribe      │
│                               │       · watchdog + reconexión (backoff)    │
│                               │       · reconciliación (lectura en bloque) │
│                               │       · snapshot en memoria                │
│                               └── ConnectionManager (clientes WS,          │
│                                   broadcast concurrente, observadores)     │
├──────────────────────────────────────────────────────────────────────────┤
│  drivers/        PlcDriver (ABC) ← OpcUaDriver (asyncua)                   │
│                                  ← S7CommDriver (python-snap7)             │
│                  registry.py: transporte → clase                          │
│                  escritura.py: JSON → tipo exacto del PLC                 │
│                  s7_fuentes.py: fuentes TIA → offsets                     │
├──────────────────────────────────────────────────────────────────────────┤
│  stores/         JSON atómico + Fernet: plcs, tags, lista blanca          │
│  config/         Settings (pydantic-settings, prefijo PLC_), rutas        │
└──────────────────────────────────────────────────────────────────────────┘
```

Regla de dependencias: **cada capa solo conoce la de abajo**. `core/` habla
con `PlcDriver`, nunca con asyncua ni snap7; `api/` habla con `PlcManager`,
nunca con un handler ni un driver directamente (salvo `handler.buscar_tag`
para validar). Así, cambiar de protocolo no toca la API, y cambiar de
framework web no toca los drivers.

## Flujo de un dato (PLC → cliente)

```
PLC ──(subscription OPC UA / polling S7comm)──▶ Driver
   Driver.callback(TagValue)
      ▶ SubscriptionHandler.on_data_change
          · actualiza snapshot[tag]
          · ConnectionManager.broadcast({"plc","tag","value","type","source_ts","delta_ms"})
              · observadores internos (síncronos; historizador, alarmas…)
              · json.dumps UNA vez
              · asyncio.gather(send a cada socket, timeout 5 s)
              · sockets caídos → cerrados y depurados
```

Un cliente nuevo recibe primero `PlcManager.build_snapshot_message()`: el
último valor conocido de TODOS los tags de TODOS los PLCs, con clave
`"<plc_id>|<tag>"` para que no colisionen nombres entre PLCs.

## Ciclo de vida de un PLC

```
POST /plcs ──▶ PlcManager.add_plc
                 · construir_endpoint()  (normaliza IP / opc.tcp:// / s7://; valida s7_tags)
                 · registry.crear_driver(transporte, settings.para_plc(...))
                 · SubscriptionHandler.start()  → tarea asyncio "supervisor"
                 · PlcStore.guardar()           → datos/plcs.json
                 · broadcast(snapshot)

supervisor:
   loop:
     estado = "conectando"
     driver.connect()                      ← ConnectionError con mensaje accionable
     tags = driver.browse_tags()           ← OPC UA descubre; S7comm parsea la lista + lectura de prueba
     tags_store.guardar(plc_id, tags)      ← catálogo en disco (sobrevive al PLC apagado)
     driver.subscribe(tags, on_data_change)
     snapshot inicial = driver.read_tags(...)
     broadcast(snapshot global) + status "conectado"
     estado = "conectado"
     watchdog:
        cada healthcheck_interval:
           driver.check_alive()  → False → status "reconectando", salir del watchdog
           reconciliar(): read_tags en bloque, difundir diferencias,
                          recrear subscription si está muda
   excepción → estado "reconectando", ultimo_error, dormir backoff (2 s → 4 → 8 … máx 30), reintentar
```

`stop()` cancela el supervisor y cierra el driver. `remove_plc` además
olvida el catálogo y los permisos de escritura de ese PLC.

## Flujo de una escritura

```
POST /escritura  (o WS {"tipo":"write"})
  deps.exigir_api_key                         ─ candado 1: clave
  PlcManager.escribir_tags(lista, usuario, permitidos)
     Fase 1 · VALIDAR TODO sin tocar el PLC:
        handler existe · driver soporta escritura · PLC conectado · tag existe
        escritura.convertir(valor, tipo)       ─ tipo, rango del entero, booleanos estrictos
        escritura_store.validar(plc, tag, v)   ─ candado 2: lista blanca + mínimo/máximo
     Fase 2 · LEER valores actuales (para poder deshacer)
     Fase 3 · ESCRIBIR en orden; cada driver RELEE y devuelve lo que quedó
     Fase 4 · si uno falla → RESTAURAR los anteriores en orden inverso
  auditoria.registrar(...)                      ─ candado 3: quién, qué, antes, después
```

## Decisiones de diseño

* **Una sesión por PLC, sin importar cuántos clientes.** El backend es el
  único que habla con el PLC; diez pantallas son diez WebSockets, no diez
  sesiones OPC UA.
* **Subscription + reconciliación.** La subscription OPC UA es el camino
  rápido, pero puede quedarse muda sin que la sesión caiga (el servidor la
  da por caducada tras una pausa, un firmware la pierde al recargar el
  programa). Cada `healthcheck_interval` se lee todo en bloque y se difunde
  lo que difiera; si acumula dos cambios que solo vio la lectura directa, se
  recrea la subscription sin tirar la conexión.
* **S7comm es polling y se presenta igual.** El handler no distingue: el
  driver emite solo cambios con el mismo `TagValue`.
* **El catálogo de tags se persiste; los valores no.** Con el PLC apagado
  los tags siguen listados (los clientes mantienen sus enlaces) pero con
  `value: null`: un valor viejo en una pantalla de planta es peor que ninguno.
* **Nada es escribible hasta que alguien lo habilita.** Lo contrario de lo
  que declara el servidor OPC UA. Ver `docs/ESCRITURA.md`.
* **Errores accionables.** Un "Connection refused" en un S7-1200 dice que
  probablemente no tiene servidor OPC UA y que use `/siemens/identificar`;
  un DB ilegible por S7comm dice qué desmarcar en TIA.
* **Persistencia tolerante.** JSON con escritura atómica (`.tmp` + `replace`);
  un fichero corrupto se ignora con aviso y el servicio arranca. La lista
  blanca corrupta arranca **vacía** (fallo seguro: nada se puede escribir).
* **Sin bloqueo del bucle.** snap7 es síncrono: cada llamada va al executor
  con un lock por driver. La auditoría escribe en un hilo aparte con cola.

## Escalabilidad

| Dimensión | Cómo escala | Límite práctico |
|---|---|---|
| Nº de PLCs | Un handler (tarea asyncio) por PLC, independientes | Decenas por proceso sin problema; el límite lo pone la red |
| Nº de tags | OPC UA: lectura en bloque de 250 nodos por petición; S7comm: un `db_read` por DB | Cientos–miles por PLC. En S7-1200 la PDU es de 240 B: DBs grandes = varias lecturas |
| Nº de clientes WS | Serialización única + envío concurrente con timeout | Decenas de visores por proceso; para cientos, poner un relay (Redis pub/sub) tras el observador |
| Frecuencia | `sampling_interval_ms` / `s7_poll_interval_ms` por PLC | OPC UA S7-1500: 100 ms; S7-1200: 1000 ms. S7comm: 50 ms mínimo, 250 recomendado |
| Extensiones | Observadores del `ConnectionManager` (historizador, alarmas, MQTT) sin segunda sesión con el PLC | — |
| Protocolos | `drivers/registry.py` | — |

Para varios procesos (alta disponibilidad), cada uno debe gestionar PLCs
distintos: dos procesos sobre el mismo PLC abrirían dos sesiones. Un
balanceador delante de los WebSockets necesita afinidad o un relay.

## Concurrencia y seguridad de hilos

* Todo lo de `core/` y `api/` corre en el bucle asyncio de uvicorn (un solo
  hilo): no hay carreras entre handlers.
* `S7CommDriver._en_hilo()` ejecuta snap7 en el `ThreadPoolExecutor` por
  defecto con un `threading.Lock` por driver: nunca dos peticiones
  simultáneas al mismo cliente snap7.
* Los stores usan `threading.Lock` solo para serializar la escritura a disco.
* `Auditoria` tiene un hilo escritor y una cola acotada: si la cola se llena
  se pierde el evento antes que frenar la operación.
