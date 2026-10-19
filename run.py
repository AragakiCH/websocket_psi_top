# -*- coding: utf-8 -*-
"""
run.py
======
Arranque del servicio con la configuración del `.env`:

    python run.py                 # host/puerto de PLC_API_HOST / PLC_API_PORT
    python run.py --reload        # recarga automática en desarrollo
"""
import sys

import uvicorn

from app.config.settings import get_settings

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.api_host,
        port=s.api_port,
        reload="--reload" in sys.argv,
        log_level=s.log_level.lower(),
    )
