# Extender el backend

## Añadir un protocolo (driver)

Ejemplo: Modbus TCP. Nada de `core/` ni `api/` cambia.

1. **Implementa `PlcDriver`** en `app/drivers/modbus_driver.py`:

```python
from app.drivers.plc_driver import PlcDriver, TagInfo, TagValue, DataChangeCallback

class ModbusDriver(PlcDriver):
    modo_lectura = "polling"

    def __init__(self, settings):            # recibe la copia de Settings de ESTE PLC
        self._settings = settings            # settings.opcua_endpoint trae el endpoint

    async def connect(self) -> None: ...     # lanza ConnectionError con mensaje accionable
    async def disconnect(self) -> None: ...
    async def browse_tags(self) -> list[TagInfo]: ...   # descubre o parsea una lista
    async def subscribe(self, tags, callback: DataChangeCallback) -> None: ...
        # llama `await callback(TagValue(...))` en cada cambio (polling en una tarea)
    async def read_tag(self, node_id) -> TagValue: ...
    async def read_tags(self, node_ids) -> list[TagValue | None]: ...   # en bloque, alineada
    async def check_alive(self) -> bool: ...                            # watchdog
    def is_connected(self) -> bool: ...
    async def write_tag(self, node_id, valor) -> TagValue: ...   # OPCIONAL; convierte con
                                                                 # escritura.convertir y RELEE
```

   Contrato que el `SubscriptionHandler` espera: `subscribe` entrega solo
   cambios (o todo, el snapshot es idempotente); `read_tags` devuelve una
   lista alineada con `None` donde falle; `connect` levanta excepción si no
   puede (el supervisor reintenta con backoff); los `data_type` deben ser
   nombres que `escritura.normalizar_tipo` entienda si se va a escribir.

2. **Regístralo** en `app/drivers/registry.py`:
   ```python
   _REGISTRO["modbus"] = "app.drivers.modbus_driver:ModbusDriver"
   ```
   La importación es diferida: si falta `pymodbus`, el servicio arranca y
   el error aparece al dar de alta un PLC Modbus.

3. **Settings**: añade sus campos a `Settings` (`modbus_unit_id`…) y, si
   el endpoint tiene formato propio (`modbus://host:502/1`), enséñale a
   `PlcManager.construir_endpoint` a reconocerlo y a `Settings.para_plc` a
   copiar sus campos. Añade el transporte a la validación de `construir_endpoint`.

4. **Dependencia** en `requirements.txt` y una prueba en `tests/` con un
   servidor simulado (como `tools/simulador_s7.py`).

## Añadir un consumidor interno (historizador, alarmas, MQTT)

No abras una segunda sesión con el PLC: suscríbete al flujo que ya existe.

```python
# en main.py, tras crear `manager`
import queue
cola = queue.Queue(maxsize=10000)

def observador(msg: dict) -> None:          # SÍNCRONO y rápido: solo encolar
    if "tag" in msg:                         # cambios de valor; ignora snapshot/status
        try:
            cola.put_nowait(msg)
        except queue.Full:
            pass

manager.registrar_observador(observador)
# … y un hilo/tarea aparte vacía `cola` hacia SQL, InfluxDB, MQTT…
```

El observador recibe **todos** los mensajes difundidos, aunque no haya
ningún cliente WebSocket conectado. Formato en `docs/API.md`.

## Sustituir la clave de API por un sistema de usuarios

Toda la protección pasa por `app/api/deps.py`:

* `exigir_api_key(request, X-API-Key, X-Usuario) -> str` es la dependencia
  de todos los endpoints que modifican. Devuelve el nombre de usuario para
  la auditoría.
* `clave_valida(settings, clave)` la usa el WebSocket.

Para integrar un login propio (JWT, sesión de la aplicación de arriba,
LDAP…): reescribe `exigir_api_key` para validar el token y devolver el
usuario, y `clave_valida` para el WS. Los routers no cambian. Si además
quieres roles (solo *supervisores* escriben), añade la comprobación dentro
de esa misma dependencia o crea `exigir_rol("supervisor")` y cámbiala en
`escritura_routes.py`.

## Añadir un endpoint

1. Router en `app/api/<nombre>_routes.py` con `APIRouter(prefix=…, tags=[…])`.
2. Accede a los componentes por `request.app.state` (`plc_manager`,
   `manager`, `escritura_store`, `auditoria`, `settings`, `carpeta_datos`)
   o por las funciones de `deps.py`.
3. `app.include_router(...)` en `main.py`.
4. Documenta con `summary`/`description`/`responses`: aparece en `/docs`.

## Cambiar la persistencia (JSON → base de datos)

Los stores (`app/stores/`) tienen una interfaz mínima:
`PlcStore.cargar()/guardar(lista)`, `TagsStore.cargar(plc_id)/guardar/borrar`,
`EscrituraStore.listar/obtener/habilitar/deshabilitar/validar/borrar_plc`.
Implementa las mismas firmas sobre SQLAlchemy y cámbialas en
`PlcManager.__init__` y `main.py`. Nada más las conoce.

## Empaquetar como ejecutable

PyInstaller funciona (`pyinstaller --onedir run.py --name PsiTop --add-data
app/config/tags_filter.yaml;app/config --hidden-import snap7 --hidden-import
asyncua`). Con `sys.frozen`, `config/rutas.py` mueve la carpeta de datos a
`C:\ProgramData\PsiTop\datos`, que sobrevive a reinstalaciones.
