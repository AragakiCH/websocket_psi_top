# -*- coding: utf-8 -*-
"""Pruebas de punta a punta de la API REST y el WebSocket con un driver falso."""
import json

from tests.conftest import API_KEY, esperar, plc_conectado
from tests.fake_driver import TAGS, FakeDriver

NODO_TEMP = TAGS[0].node_id


def _alta(cliente, host="192.168.0.10", **extra) -> str:
    r = cliente.post("/plcs", json={"host": host, **extra})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["ok"], cuerpo
    plc_id = cuerpo["plc_id"]
    assert esperar(lambda: plc_conectado(cliente, plc_id)), cliente.get("/health").json()
    return plc_id


def test_raiz_y_sistema(cliente):
    assert cliente.get("/").json()["docs"] == "/docs"
    s = cliente.get("/sistema").json()
    assert s["api_key_activa"] is True and s["datos"]["escribible"] is True
    assert cliente.get("/health").json()["num_plcs"] == 0


def test_api_key_obligatoria_para_modificar(cliente):
    r = cliente.post("/plcs", json={"host": "192.168.0.10"}, headers={"X-API-Key": "mala"})
    assert r.status_code == 401
    assert cliente.get("/plcs").status_code == 200   # lectura libre


def test_alta_tags_y_baja(cliente):
    plc_id = _alta(cliente, nombre="Linea 1")
    assert plc_id == "Linea_1"

    plcs = cliente.get("/plcs").json()
    assert plcs["ids"] == ["Linea_1"]
    assert plcs["plcs"][0]["modelo"].startswith("CPU 1516")
    assert plcs["plcs"][0]["transporte"] == "opcua"
    assert "password" not in plcs["plcs"][0]

    tags = cliente.get("/tags", params={"plc": plc_id}).json()["tags"]
    por_nombre = {t["tag"]: t for t in tags}
    assert por_nombre["DB_Datos.temperatura"]["value"] == 20.0
    assert por_nombre["DB_Datos.marcha"]["value"] is False

    arbol = cliente.get("/browse").json()["plcs"][0]["data_blocks"]
    assert "DB_Datos" in arbol and len(arbol["DB_Datos"]) == 3

    # Duplicado: mismo endpoint.
    r = cliente.post("/plcs", json={"host": "192.168.0.10"}).json()
    assert r["ok"] is False and "ya está gestionado" in r["mensaje"]

    r = cliente.delete(f"/plcs/{plc_id}").json()
    assert r["ok"]
    assert cliente.get("/health").json()["num_plcs"] == 0
    assert cliente.delete(f"/plcs/{plc_id}").status_code == 404

    acciones = [e["accion"] for e in cliente.get("/auditoria").json()["eventos"]]
    assert "plc.alta" in acciones and "plc.baja" in acciones


def test_persistencia_en_disco(cliente, tmp_path):
    _alta(cliente)
    plcs_json = json.loads((tmp_path / "datos" / "plcs.json").read_text("utf-8"))
    assert plcs_json[0]["endpoint"] == "opc.tcp://192.168.0.10:4840"
    tags_json = json.loads((tmp_path / "datos" / "tags_plc.json").read_text("utf-8"))
    assert len(tags_json["192.168.0.10"]) == 3


def test_s7comm_valida_lista_de_tags(cliente):
    r = cliente.post("/plcs", json={"host": "192.168.0.20", "transporte": "s7comm"})
    assert r.status_code == 400 and "lista de tags" in r.json()["detail"]
    r = cliente.post("/plcs", json={"host": "192.168.0.20", "transporte": "s7comm",
                                    "s7_tags": "x;DB1;0;FLOAT"})
    assert r.status_code == 400 and "Línea 1" in r.json()["detail"]
    r = cliente.post("/siemens/validar-tags", json={"s7_tags": "t;DB1;0;REAL\nm;DB1;4.0;BOOL"}).json()
    assert r["ok"] and r["num_tags"] == 2 and r["tags"][1]["direccion"] == "DB1.DBX4.0"


def test_importar_fuentes_sin_plc(cliente):
    fuente = ('DATA_BLOCK "Data_block_3"\n{ S7_Optimized_Access := \'FALSE\' }\n'
              'STRUCT\n a : Bool;\n b : Real;\nEND_STRUCT;\nBEGIN\nEND_DATA_BLOCK\n')
    r = cliente.post("/siemens/importar", json={"fuentes": [{"nombre": "x.db", "contenido": fuente}]})
    assert r.status_code == 200, r.text
    b = r.json()["bloques"][0]
    assert b["db"] == 3 and b["db_como"].startswith("nombre")
    assert r.json()["s7_tags"] == "a;DB3;0.0;BOOL\nb;DB3;2;REAL"


def test_escritura_con_lista_blanca_y_vuelta_atras(cliente):
    plc_id = _alta(cliente)

    # 1) Sin habilitar: 403 y auditado.
    r = cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": 30}]})
    assert r.status_code == 403

    # 2) Candidatos: el ExtensionObject queda fuera.
    cand = cliente.get("/escritura/candidatos", params={"plc_id": plc_id}).json()
    assert {c["tag"] for c in cand["candidatos"]} == {"DB_Datos.temperatura", "DB_Datos.marcha"}

    # 3) Habilitar con límites.
    r = cliente.put("/escritura/permitidos", json={"plc_id": plc_id, "tag": "DB_Datos.temperatura",
                                                   "minimo": 0, "maximo": 90})
    assert r.status_code == 200, r.text
    assert cliente.put("/escritura/permitidos",
                       json={"plc_id": plc_id, "tag": "DB_Datos.marcha"}).status_code == 200
    assert cliente.put("/escritura/permitidos",
                       json={"plc_id": plc_id, "tag": "DB_Datos.estado"}).status_code == 400
    assert cliente.put("/escritura/permitidos",
                       json={"plc_id": plc_id, "tag": "no.existe"}).status_code == 404

    # 4) Fuera de rango -> 400 sin tocar el PLC.
    r = cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": 95}]})
    assert r.status_code == 400 and FakeDriver.ultimo.escrituras == []

    # 5) Tipo incorrecto -> 400.
    r = cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.marcha", "valor": "quizá"}]})
    assert r.status_code == 400

    # 6) Lote correcto: relee y confirma.
    r = cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": "65"},
        {"plc_id": plc_id, "tag": "DB_Datos.marcha", "valor": "true"}]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] and res["escrituras"] == 2
    assert res["resultados"][0]["confirmado"] == 65.0 and res["resultados"][0]["coincide"]
    assert res["resultados"][0]["anterior"] == 20.0
    assert res["resultados"][1]["confirmado"] is True
    assert cliente.get("/tags", params={"plc": plc_id}).json()["tags"][0]["value"] == 65.0

    # 7) Vuelta atrás: el segundo tag pasa la validación pero el PLC lo rechaza
    #    al escribir; el primero, ya escrito, se restaura a su valor anterior.
    FakeDriver.ultimo.rechazar.add("DB_Datos.marcha")
    FakeDriver.ultimo.escrituras.clear()
    r = cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": 10},
        {"plc_id": plc_id, "tag": "DB_Datos.marcha", "valor": False}]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is False and res["fallo_en"] == "DB_Datos.marcha"
    assert res["revertidos"] == ["DB_Datos.temperatura"]
    assert FakeDriver.ultimo.valores[NODO_TEMP] == 65.0
    FakeDriver.ultimo.rechazar.clear()

    # 8) Deshabilitar.
    assert cliente.delete(f"/escritura/permitidos/{plc_id}/DB_Datos.temperatura").json()["ok"]
    assert cliente.post("/escritura", json={"escrituras": [
        {"plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": 30}]}).status_code == 403

    acciones = [e["accion"] for e in cliente.get("/auditoria").json()["eventos"]]
    assert "plc.escritura" in acciones and "plc.escritura.denegada" in acciones
    assert "plc.escritura.fuera_de_rango" in acciones and "plc.escritura.fallida" in acciones


def test_websocket_snapshot_cambios_y_write(cliente):
    plc_id = _alta(cliente)
    cliente.put("/escritura/permitidos", json={"plc_id": plc_id, "tag": "DB_Datos.temperatura"})

    with cliente.websocket_connect(f"/ws?api_key={API_KEY}&usuario=ana") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["plcs"][plc_id]["conectado"] is True
        assert snap["tags"][f"{plc_id}|DB_Datos.temperatura"]["value"] == 20.0

        # Presencia tras conectar.
        msg = ws.receive_json()
        assert msg["type"] == "presence" and msg["usuarios"][0]["usuario"] == "ana"

        # ping / pong
        ws.send_json({"tipo": "ping", "id": 1})
        assert ws.receive_json() == {"tipo": "pong", "id": 1}

        # Un cambio desde el "PLC" llega como mensaje de valor.
        cliente.portal.call(FakeDriver.ultimo.simular, NODO_TEMP, 42.5)
        msg = ws.receive_json()
        assert msg["plc"] == plc_id and msg["tag"] == "DB_Datos.temperatura" and msg["value"] == 42.5

        # Escritura por WS.
        ws.send_json({"tipo": "write", "id": "w1", "plc_id": plc_id,
                      "tag": "DB_Datos.temperatura", "valor": 50})
        res = ws.receive_json()
        assert res["tipo"] == "write.result" and res["id"] == "w1" and res["ok"], res
        assert res["resultados"][0]["confirmado"] == 50.0

        # Tag no habilitado por WS -> denegado.
        ws.send_json({"tipo": "write", "id": "w2", "plc_id": plc_id,
                      "tag": "DB_Datos.marcha", "valor": True})
        res = ws.receive_json()
        assert res["ok"] is False and res["codigo"] == "denegado"

    # Sin clave: no puede escribir pero sí leer.
    with cliente.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.receive_json()  # presence
        ws.send_json({"tipo": "write", "plc_id": plc_id, "tag": "DB_Datos.temperatura", "valor": 1})
        assert ws.receive_json()["codigo"] == "no_autorizado"

    # Filtro por PLC: un id distinto no trae tags.
    with cliente.websocket_connect("/ws?plc=otro") as ws:
        assert ws.receive_json()["tags"] == {}


def test_reconexion_tras_fallo(cliente):
    FakeDriver.fallar_conexion = True
    r = cliente.post("/plcs", json={"host": "192.168.0.30"}).json()
    plc_id = r["plc_id"]
    assert esperar(lambda: any(p["plc"] == plc_id and p["estado_conexion"] == "reconectando"
                               for p in cliente.get("/health").json()["plcs"]))
    h = next(p for p in cliente.get("/health").json()["plcs"] if p["plc"] == plc_id)
    assert "conexión rechazada" in h["ultimo_error"]
    # Sin conexión, los tags conocidos van con valor None (no hay catálogo aún aquí).
    FakeDriver.fallar_conexion = False
    assert esperar(lambda: plc_conectado(cliente, plc_id), timeout=8)
    h = next(p for p in cliente.get("/health").json()["plcs"] if p["plc"] == plc_id)
    assert h["reconexiones"] >= 1 and h["ultimo_error"] == ""
