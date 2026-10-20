# ==========================================================================
#  PSI Top · backend Siemens (OPC UA + S7comm) -> WebSocket
#  Imagen para Container Station (QNAP) o cualquier Docker x86_64 / arm64.
#
#  Construir:   docker build -t psi-top-backend:1.0.0 .
#  Ejecutar:    docker compose up -d     (ver docker-compose.yml)
# ==========================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Lima \
    PLC_API_HOST=0.0.0.0 \
    PLC_API_PORT=8000 \
    PLC_DATOS_DIR=/data

WORKDIR /app

# Dependencias primero (capa cacheada mientras requirements.txt no cambie)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Código de la aplicación
COPY app ./app
COPY run.py VERSION ./

# Usuario sin privilegios + carpeta de datos persistente
RUN useradd --create-home --uid 1000 psitop \
 && mkdir -p /data \
 && chown -R psitop:psitop /data /app
USER psitop

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PLC_API_PORT','8000'), timeout=4)" || exit 1

CMD ["python", "run.py"]
