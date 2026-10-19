# -*- coding: utf-8 -*-
"""
simulador_opcua.py
==================
Servidor OPC UA que IMITA la estructura de un S7-1500 para probar el backend
sin un PLC real:

    Objects/DeviceSet/PLC_1/DataBlocksGlobal/DB_Datos/{temperatura, presion, marcha, contador, texto}
    Objects/DeviceSet/PLC_1/DataBlocksGlobal/DB_Recetas/{setpoint, velocidad}

Los valores cambian solos cada segundo (temperatura y presion oscilan, el
contador cuenta, marcha alterna). `setpoint` y `velocidad` son escribibles:
sirven para probar POST /escritura.

Uso (con el venv activado):
    python tools/simulador_opcua.py                # opc.tcp://0.0.0.0:4840
    python tools/simulador_opcua.py --puerto 4841

Luego, en otra terminal:
    python run.py
    curl -X POST http://localhost:8000/plcs -H "Content-Type: application/json" ^
         -H "X-API-Key: <clave>" -d "{\"host\": \"127.0.0.1\", \"puerto\": 4840}"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math

from asyncua import Server, ua

NS_URI = "http://www.siemens.com/simatic-s7-opcua"


async def main(puerto: int) -> None:
    logging.basicConfig(level=logging.WARNING)
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{puerto}")
    server.set_server_name("Simulador S7-1500")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    ns = await server.register_namespace(NS_URI)

    objects = server.nodes.objects
    deviceset = await objects.add_object(ns, "DeviceSet")
    plc = await deviceset.add_object(ns, "PLC_1")
    await plc.add_variable(ns, "Model", "CPU 1516-3 PN/DP (simulado)")
    dbs = await plc.add_object(ns, "DataBlocksGlobal")

    db_datos = await dbs.add_object(ns, "DB_Datos")
    temperatura = await db_datos.add_variable(ns, "temperatura", 20.0, ua.VariantType.Float)
    presion = await db_datos.add_variable(ns, "presion", 1.0, ua.VariantType.Float)
    marcha = await db_datos.add_variable(ns, "marcha", False, ua.VariantType.Boolean)
    contador = await db_datos.add_variable(ns, "contador", 0, ua.VariantType.Int32)
    texto = await db_datos.add_variable(ns, "texto", "Simulador", ua.VariantType.String)

    db_recetas = await dbs.add_object(ns, "DB_Recetas")
    setpoint = await db_recetas.add_variable(ns, "setpoint", 50.0, ua.VariantType.Float)
    velocidad = await db_recetas.add_variable(ns, "velocidad", 1200, ua.VariantType.Int16)
    await setpoint.set_writable()
    await velocidad.set_writable()

    print(f"Simulador S7-1500 escuchando en opc.tcp://0.0.0.0:{puerto}  (Ctrl+C para parar)")
    print("DBs: DB_Datos (temperatura, presion, marcha, contador, texto) · "
          "DB_Recetas (setpoint, velocidad: escribibles)")

    async with server:
        i = 0
        while True:
            await asyncio.sleep(1)
            i += 1
            # Variant explícito: un float de Python se mapearía a Double y el
            # servidor lo rechazaría con BadTypeMismatch.
            await temperatura.write_value(ua.Variant(round(20 + 5 * math.sin(i / 10), 2), ua.VariantType.Float))
            await presion.write_value(ua.Variant(round(1 + 0.2 * math.cos(i / 7), 3), ua.VariantType.Float))
            await contador.write_value(ua.Variant(i, ua.VariantType.Int32))
            if i % 5 == 0:
                await marcha.write_value(not await marcha.read_value())
            if i % 30 == 0:
                await texto.write_value(f"Ciclo {i}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Simulador OPC UA tipo S7-1500")
    ap.add_argument("--puerto", type=int, default=4840)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.puerto))
    except KeyboardInterrupt:
        pass
