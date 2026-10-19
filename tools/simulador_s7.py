# -*- coding: utf-8 -*-
"""
simulador_s7.py
===============
Servidor S7comm (snap7) que IMITA un S7-1200 sin OPC UA, para probar el
driver S7comm sin un PLC real. Publica el DB1 con esta estructura
(acceso estándar, offsets reales):

    temperatura;DB1;0;REAL      oscila
    marcha;DB1;4.0;BOOL         alterna cada 5 s
    paro;DB1;4.1;BOOL           fijo
    contador;DB1;6;DINT         cuenta
    setpoint;DB1;10;REAL        escribible (POST /escritura)
    velocidad;DB1;14;INT        escribible
    texto;DB1;16;STRING[20]     fijo

Uso (con el venv activado):
    python tools/simulador_s7.py                  # puerto 102 (en Linux requiere root o CAP_NET_BIND)
    python tools/simulador_s7.py --puerto 1102    # el driver siempre usa el 102: en Linux
                                                  # redirige con iptables, o ejecuta como root

Alta en el backend:
    POST /plcs  {"host": "127.0.0.1", "transporte": "s7comm", "slot": 1, "nombre": "Sim1200",
                 "s7_tags": "temperatura;DB1;0;REAL\\nmarcha;DB1;4.0;BOOL\\nparo;DB1;4.1;BOOL\\n"
                            "contador;DB1;6;DINT\\nsetpoint;DB1;10;REAL\\nvelocidad;DB1;14;INT\\n"
                            "texto;DB1;16;STRING[20]"}
"""
from __future__ import annotations

import argparse
import math
import struct
import time

import snap7
from snap7.type import SrvArea

S7_TAGS = ("temperatura;DB1;0;REAL\nmarcha;DB1;4.0;BOOL\nparo;DB1;4.1;BOOL\n"
           "contador;DB1;6;DINT\nsetpoint;DB1;10;REAL\nvelocidad;DB1;14;INT\n"
           "texto;DB1;16;STRING[20]")


def main(puerto: int) -> None:
    # Un bytearray: el servidor de python-snap7 3.x lo guarda por referencia,
    # así que lo que se escriba aquí (o lo que escriba un cliente) se ve al instante.
    datos = bytearray(40)
    # Valores iniciales.
    struct.pack_into(">f", datos, 10, 50.0)       # setpoint
    struct.pack_into(">h", datos, 14, 1200)       # velocidad
    texto = b"Simulador S7"
    datos[16] = 20
    datos[17] = len(texto)
    for k, b in enumerate(texto):
        datos[18 + k] = b

    server = snap7.server.Server()
    server.register_area(SrvArea.DB, 1, datos)
    server.start(tcp_port=puerto)
    print(f"Simulador S7-1200 (snap7) escuchando en el puerto {puerto} · DB1 de 40 bytes "
          f"(Ctrl+C para parar)")
    print("Lista de tags para POST /plcs (s7_tags):")
    print(S7_TAGS)
    i = 0
    try:
        while True:
            time.sleep(1)
            i += 1
            struct.pack_into(">f", datos, 0, round(20 + 5 * math.sin(i / 10), 2))
            struct.pack_into(">i", datos, 6, i)
            if i % 5 == 0:
                datos[4] ^= 0b01     # marcha alterna; paro (bit 1) no se toca
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        server.destroy()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Simulador S7comm tipo S7-1200")
    ap.add_argument("--puerto", type=int, default=102)
    main(ap.parse_args().puerto)
