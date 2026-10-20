# Despliegue en Docker (QNAP Container Station)

Archivos: `Dockerfile`, `.dockerignore`, `docker-compose.yml`.
La imagen se construye **en el NAS** (así coincide la arquitectura del CPU).

## 1. Empaquetar en Windows (PowerShell)

```powershell
cd "D:\proyecto psi"
tar -czf psitop.tgz --exclude=websocket_psi_top/.git --exclude=websocket_psi_top/venv --exclude=websocket_psi_top/datos --exclude=websocket_psi_top/.env websocket_psi_top
scp psitop.tgz Christian@161.132.235.184:/share/Container/
```

## 2. En el NAS (SSH)

```sh
uname -m                     # x86_64 o aarch64: ambos sirven
docker --version
docker compose version       # si falla, prueba: docker-compose version

cd /share/Container
tar -xzf psitop.tgz
cd websocket_psi_top
cp .env.example .env
vi .env                      # como mínimo: PLC_API_KEY=... y PLC_CORS_ORIGINS
docker compose up -d --build # o: docker-compose up -d --build
docker compose logs -f       # Ctrl+C para salir
curl http://localhost:8000/health
```

Si `docker` dice "permission denied", antepone `sudo`.

## 3. Actualizar a una versión nueva

Repite el paso 1 y luego en el NAS:

```sh
cd /share/Container && tar -xzf psitop.tgz && cd websocket_psi_top
docker compose up -d --build
```

El `.env` no se pisa (no va en el paquete) y los datos viven en el volumen
`psitop_datos`, así que los PLC dados de alta y la clave `.clave` se conservan.

## 4. Copia de seguridad de los datos

```sh
docker run --rm -v websocket_psi_top_psitop_datos:/data -v /share/Container:/backup \
  python:3.12-slim tar -czf /backup/psitop_datos_$(date +%F).tgz -C /data .
```

## Notas

* **Puerto**: el compose publica `8000:8000`. Si el 8000 del NAS está ocupado,
  cambia solo el de la izquierda (p. ej. `"8010:8000"`).
* **Red con los PLC**: el contenedor sale a la red por el NAS. Comprueba desde el NAS
  que llegas al PLC: `nc -zv 192.168.0.10 4840` (OPC UA) o `nc -zv 192.168.0.20 102` (S7comm).
* **Seguridad**: si el puerto se ve desde Internet, `PLC_API_KEY` es obligatoria y
  conviene abrirlo solo a las IP necesarias o ponerlo detrás de un proxy HTTPS.
* `PLC_DATOS_DIR`, `PLC_API_HOST` y `PLC_API_PORT` los fija el compose; no hace
  falta tocarlos en el `.env`.
