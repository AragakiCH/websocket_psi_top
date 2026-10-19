# -*- coding: utf-8 -*-
"""
Fixtures compartidas. El backend se levanta ENTERO (lifespan incluido) con
el TestClient de FastAPI, con el driver OPC UA sustituido por uno falso y
la carpeta de datos en un directorio temporal, así que las pruebas de la
API no tocan la red ni los datos del usuario.
"""
from __future__ import annotations

import time

import pytest

API_KEY = "clave-de-prueba"


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("PLC_DATOS_DIR", str(tmp_path / "datos"))
    monkeypatch.setenv("PLC_API_KEY", API_KEY)
    monkeypatch.setenv("PLC_AUTOSTART_PLCS", "false")
    monkeypatch.setenv("PLC_HEALTHCHECK_INTERVAL", "0.2")
    monkeypatch.setenv("PLC_RECONNECT_INITIAL_DELAY", "0.2")
    monkeypatch.setenv("PLC_LOG_LEVEL", "WARNING")

    from app.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()

    from app.drivers import registry
    monkeypatch.setitem(registry._REGISTRO, "opcua", "tests.fake_driver:FakeDriver")

    from tests.fake_driver import FakeDriver
    FakeDriver.fallar_conexion = False
    FakeDriver.ultimo = None

    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": API_KEY, "X-Usuario": "tester"})
        yield c
    settings_mod.get_settings.cache_clear()


def esperar(cond, timeout: float = 5.0, paso: float = 0.05) -> bool:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(paso)
    return False


def plc_conectado(cliente, plc_id: str) -> bool:
    h = cliente.get("/health").json()
    return any(p["plc"] == plc_id and p["conectado"] for p in h["plcs"])
