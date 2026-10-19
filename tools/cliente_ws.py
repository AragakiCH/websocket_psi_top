# -*- coding: utf-8 -*-
"""
cliente_ws.py
=============
Cliente WebSocket de consola: se conecta a `/ws`, imprime el snapshot y
después cada mensaje que llega (cambios de tags, status, presencia).

Uso (con el venv activado):
    python tools/cliente_ws.py                          # ws://localhost:8000/ws
    python tools/cliente_ws.py --url ws://10.0.0.5:8000/ws --plc 192.168.0.10
    python tools/cliente_ws.py --escribir 192.168.0.10 DB_Recetas.setpoint 65 --api-key <clave>
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def main(args) -> None:
    url = args.url
    sep = "&" if "?" in url else "?"
    if args.plc:
        url += f"{sep}plc={args.plc}"
        sep = "&"
    if args.api_key:
        url += f"{sep}api_key={args.api_key}"
        sep = "&"
    url += f"{sep}usuario=cliente_ws"

    async with websockets.connect(url, max_size=None) as ws:
        print(f"Conectado a {url}")
        if args.escribir:
            plc_id, tag, valor = args.escribir
            await ws.send(json.dumps({"tipo": "write", "id": "cli", "plc_id": plc_id,
                                      "tag": tag, "valor": _auto(valor)}))
        async for crudo in ws:
            msg = json.loads(crudo)
            if msg.get("type") == "snapshot":
                plcs = ", ".join(f"{k} [{v['estado']}]" for k, v in msg["plcs"].items()) or "ninguno"
                print(f"SNAPSHOT · PLCs: {plcs} · {len(msg['tags'])} tags")
                for clave, t in sorted(msg["tags"].items())[: args.max_tags]:
                    print(f"   {clave:60s} = {t['value']!r:20} ({t['type']})")
                if len(msg["tags"]) > args.max_tags:
                    print(f"   ... y {len(msg['tags']) - args.max_tags} más")
            elif "tag" in msg:
                print(f"{msg['timestamp'][11:23]} {msg['plc']:>14} | {msg['tag']:45s} = "
                      f"{msg['value']!r:20} {msg['type']:10s} Δ{msg.get('delta_ms')} ms")
            elif msg.get("tipo") == "write.result":
                print("WRITE ->", json.dumps(msg, ensure_ascii=False))
                if args.escribir and not args.seguir:
                    return
            else:
                print("CONTROL ->", json.dumps(msg, ensure_ascii=False))


def _auto(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Cliente WebSocket de prueba")
    ap.add_argument("--url", default="ws://localhost:8000/ws")
    ap.add_argument("--plc", default="", help="Recibir solo este plc_id")
    ap.add_argument("--api-key", default="", help="Clave para poder escribir")
    ap.add_argument("--escribir", nargs=3, metavar=("PLC_ID", "TAG", "VALOR"))
    ap.add_argument("--seguir", action="store_true", help="Tras escribir, seguir escuchando")
    ap.add_argument("--max-tags", type=int, default=40)
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass
