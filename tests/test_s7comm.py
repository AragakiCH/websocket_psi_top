# -*- coding: utf-8 -*-
"""Driver S7comm: parseo de la lista de tags, direcciones, endpoints y codificación."""
import pytest

from app.core.endpoint import EndpointPlc, host_de, plc_id_desde, puerto_de
from app.drivers.s7comm_driver import (
    TIPO_IEC,
    codificar,
    decodificar,
    endpoint_s7,
    familia_de,
    parsear_endpoint_s7,
    parsear_tags,
)


def test_parsear_tags_formatos():
    lista = parsear_tags(
        "temperatura;DB1;0;REAL\n"
        "marcha;DB1;4.0;BOOL\n"
        "# comentario\n"
        "texto;DB1;6;STRING[20]\n"
        "contador;DB1.DBD30;DINT\n"
        "nivel, DB 2, 10, INT\n")
    assert [t.nombre for t in lista] == ["temperatura", "marcha", "texto", "contador", "nivel"]
    assert lista[0].direccion == "DB1.DBD0"
    assert lista[1].direccion == "DB1.DBX4.0" and lista[1].bit == 0
    assert lista[2].tamano == 22
    assert lista[3].db == 1 and lista[3].offset == 30
    assert lista[4].db == 2 and lista[4].direccion == "DB2.DBW10"
    assert TIPO_IEC[lista[0].tipo] == "REAL"


@pytest.mark.parametrize("texto", [
    "sin_tipo;DB1;0",
    "raro;DB1;0;FLOAT",
    "cadena;DB1;0;STRING",          # sin longitud
    "entero;DB1;4.3;INT",           # bit en no-BOOL
    "dup;DB1;0;INT\ndup;DB1;2;INT",  # repetido
])
def test_parsear_tags_errores(texto):
    with pytest.raises(ValueError):
        parsear_tags(texto)


def test_endpoint_s7():
    assert endpoint_s7("192.168.0.20", 0, 1) == "s7://192.168.0.20:102/0/1"
    assert parsear_endpoint_s7("s7://192.168.0.20:102/0/2") == ("192.168.0.20", 0, 2)
    assert parsear_endpoint_s7("192.168.0.20") == ("192.168.0.20", 0, 1)
    assert host_de("opc.tcp://10.0.0.5:4840") == "10.0.0.5"
    assert puerto_de("opc.tcp://10.0.0.5:4841", 4840) == 4841
    assert puerto_de("10.0.0.5", 4840) == 4840


def test_familia():
    assert familia_de("CPU 1214C DC/DC/Rly") == "S7-1200"
    assert familia_de("CPU 1516-3 PN/DP") == "S7-1500"
    assert familia_de("CPU 315-2 PN/DP") == "S7-300"
    assert familia_de("") == "S7"


def test_plc_id_unico():
    usados = set()
    a = plc_id_desde(EndpointPlc(endpoint="x", host="192.168.0.10", port=4840, nombre="PLC 1"), usados)
    b = plc_id_desde(EndpointPlc(endpoint="y", host="192.168.0.11", port=4840, nombre="PLC 1"), usados)
    c = plc_id_desde(EndpointPlc(endpoint="z", host="192.168.0.12", port=4840), usados)
    assert a == "PLC_1" and b == "PLC_1_2" and c == "192.168.0.12"


def test_codificar_decodificar_roundtrip():
    lista = parsear_tags("r;DB1;0;REAL\nb;DB1;4.1;BOOL\nd;DB1;6;DINT\ns;DB1;10;STRING[8]\nl;DB1;20;LINT\nx;DB1;28;LREAL")
    buf = bytearray(40)
    for t, v in zip(lista, [12.5, True, -77, "hola", -5_000_000_000, 2.5]):
        datos = codificar(t, v, bytearray(buf[t.offset:t.offset + 1]))
        buf[t.offset:t.offset + len(datos)] = datos
    assert decodificar(buf, 0, lista[0]) == 12.5
    assert decodificar(buf, 0, lista[1]) is True
    assert decodificar(buf, 0, lista[2]) == -77
    assert decodificar(buf, 0, lista[3]) == "hola"
    assert decodificar(buf, 0, lista[4]) == -5_000_000_000
    assert decodificar(buf, 0, lista[5]) == 2.5
    # Compatible con el formato que produce snap7.util.
    from snap7 import util as u
    assert u.get_real(buf, 0) == 12.5 and u.get_string(buf, 10) == "hola" and u.get_dint(buf, 6) == -77
    # El BOOL respeta los bits vecinos: bit 1 puesto, bit 0 intacto.
    assert buf[4] == 0b00000010
