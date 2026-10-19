# Despliegue y operación

## Instalación en un puesto / servidor

```bat
git clone … websocket_psi_top     (o copiar la carpeta)
cd websocket_psi_top
scripts\setup.bat                 REM venv + dependencias + .env
notepad .env                      REM PLC_API_KEY, PLC_API_PORT, PLC_DATOS_DIR
scripts\run.bat
```

Comprobación: `http://localhost:8000/health` → `{"status":"ok", …}`.

Lo que necesita la máquina: Python 3.10–3.13 y acceso de red a los PLC
(TCP 4840 para OPC UA, TCP 102 para S7comm). No hace falta TIA Portal, ni
RSLinx, ni la DLL de snap7 (va dentro del wheel de `python-snap7` 3.x).

## Ejecutar como servicio

### Windows (NSSM)
```bat
nssm install PsiTop "D:\Proyecto_psi\websocket_psi_top\venv\Scripts\python.exe" "run.py"
nssm set PsiTop AppDirectory "D:\Proyecto_psi\websocket_psi_top"
nssm set PsiTop AppStdout "C:\ProgramData\PsiTop\psitop.log"
nssm set PsiTop AppStderr "C:\ProgramData\PsiTop\psitop.log"
nssm start PsiTop
```
Pon `PLC_DATOS_DIR=C:\ProgramData\PsiTop\datos` en el `.env`: la cuenta de
servicio no debe escribir dentro de `Program Files`, y así los datos
sobreviven a reinstalaciones.

### Linux (systemd)
```ini
# /etc/systemd/system/psitop.service
[Unit]
Description=PSI Top backend Siemens
After=network-online.target

[Service]
User=psitop
WorkingDirectory=/opt/websocket_psi_top
EnvironmentFile=/opt/websocket_psi_top/.env
ExecStart=/opt/websocket_psi_top/venv/bin/python run.py
Restart=always
RestartSec=3
# S7comm usa el puerto 102 del PLC (saliente): no hace falta privilegio.

[Install]
WantedBy=multi-user.target
```
`systemctl enable --now psitop`.

## Carpeta de datos

| Fichero | Contenido | Si se pierde |
|---|---|---|
| `plcs.json` | PLCs dados de alta (contraseñas cifradas) | Hay que darlos de alta otra vez |
| `tags_plc.json` | Catálogo de tags de cada PLC | Se regenera al conectar |
| `escritura_permitida.json` | Lista blanca de escritura | Nada es escribible hasta rehabilitar |
| `auditoria.jsonl` (+ `.1`) | Registro de acciones; rota a 20 MB | Se pierde el histórico |
| `.clave` | Clave Fernet de las contraseñas | Las contraseñas guardadas dejan de poder leerse |

**Copia de seguridad**: la carpeta entera, incluida `.clave`. Restaurar =
copiar encima con el servicio parado.

## Varios procesos / varias plantas

* Un PLC → un solo proceso. Dos backends sobre el mismo PLC abren dos
  sesiones (el S7-1200 tiene pocas).
* Un proceso por línea/planta, cada uno con su `PLC_API_PORT` y su
  `PLC_DATOS_DIR`, es la forma más simple de aislar fallos.
* Si muchos visores (> 50) deben recibir lo mismo, mejor un relay
  (Redis pub/sub, MQTT) alimentado por un observador (`docs/EXTENDER.md`)
  que un solo proceso sirviendo a todos.

## Red y seguridad

* Expón el puerto HTTP solo a la red de supervisión; pon `PLC_API_KEY`.
* `PLC_CORS_ORIGINS` con los orígenes reales de la aplicación web en
  producción (`["*"]` es solo para desarrollo).
* Para HTTPS/WSS, un proxy inverso (nginx, Caddy, IIS ARR) delante de
  uvicorn con soporte WebSocket (`Upgrade`/`Connection` headers).
* Las contraseñas OPC UA en `plcs.json` van cifradas; la auditoría recorta
  los valores largos y nunca guarda contraseñas.

## Logs

Formato `fecha [NIVEL] módulo: mensaje`. Con `PLC_LOG_LEVEL=DEBUG` se
abre también el log de asyncua y snap7 (muy verboso). Mensajes clave:

| Mensaje | Significado |
|---|---|
| `[id] PLC conectado y suscripción activa.` | Todo bien |
| `[id] Fallo de conexión con el PLC: …` | No conecta; el texto dice por qué; reintenta con backoff |
| `[id] Watchdog: conexión perdida` | Estaba conectado y dejó de responder |
| `[id] Reconciliación: N tag(s) habían cambiado sin pasar por la subscription.` | La subscription OPC UA perdió cambios; se corrigieron |
| `[id] La subscription no entrega cambios (…). Se recrea.` | Se recreó sin reconectar |
| `Depurados N clientes WS caídos.` | Visores que no respondían |
| `Se omite 'X' (…): estructura que el servidor no expone campo a campo.` | UDT opaco en OPC UA |

## Problemas frecuentes de instalación

| Síntoma | Solución |
|---|---|
| `pip install` falla en `cryptography` | Actualiza pip (`python -m pip install -U pip`) para que use wheels; en Linux muy viejo instala `build-essential libffi-dev` |
| `ModuleNotFoundError: snap7` | El venv no es el del proyecto, o faltó `pip install -r requirements.txt` |
| `OSError: … snap7 … not found` (Linux ARM / Alpine) | El wheel no trae binario para esa plataforma: `apt install libsnap7-1 libsnap7-dev` o compila snap7 |
| `ZoneInfo … No time zone found` en Windows | `pip install tzdata` (ya está en requirements) |
| Puerto 8000 ocupado | `PLC_API_PORT=8001` |
| El `.exe`/servicio arranca pero no guarda nada | La carpeta de datos no es escribible: mira `GET /sistema` → `datos.escribible` |
| El navegador no conecta al WS desde otra máquina | `PLC_API_HOST=0.0.0.0` (defecto) y firewall de Windows abierto para el puerto |
