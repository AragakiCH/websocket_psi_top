# -*- coding: utf-8 -*-
"""Conversión segura de valores para escritura (drivers/escritura.py)."""
import pytest

from app.drivers.escritura import ErrorDeTipo, convertir, es_escribible, normalizar_tipo


def test_normaliza_iec_a_opcua():
    assert normalizar_tipo("REAL") == "Float"
    assert normalizar_tipo("bool") == "Boolean"
    assert normalizar_tipo("DINT") == "Int32"
    assert normalizar_tipo("Float") == "Float"       # OPC UA pasa intacto
    assert normalizar_tipo("Desconocido") == "Desconocido"


def test_booleanos_textos():
    assert convertir("false", "Boolean") is False     # bool("false") sería True
    assert convertir("0", "BOOL") is False
    assert convertir("on", "Boolean") is True
    assert convertir(1, "Boolean") is True
    with pytest.raises(ErrorDeTipo):
        convertir(7, "Boolean")
    with pytest.raises(ErrorDeTipo):
        convertir("quizá", "Boolean")


def test_enteros_rango_y_decimales():
    assert convertir("42", "Int16") == 42
    assert convertir(3.0, "INT") == 3
    with pytest.raises(ErrorDeTipo):
        convertir(40000, "Int16")
    with pytest.raises(ErrorDeTipo):
        convertir(3.7, "Int32")
    with pytest.raises(ErrorDeTipo):
        convertir(True, "Int16")
    with pytest.raises(ErrorDeTipo):
        convertir(-1, "Byte")


def test_reales_y_texto():
    assert convertir(5, "Float") == 5.0
    assert convertir("3,5", "REAL") == 3.5
    with pytest.raises(ErrorDeTipo):
        convertir(True, "Double")
    assert convertir(12, "String") == "12"
    assert convertir(True, "STRING") == "true"


def test_tipo_no_soportado():
    with pytest.raises(ErrorDeTipo):
        convertir(1, "ExtensionObject")
    assert es_escribible("Float[20]") is False
    assert es_escribible("REAL") is True
    assert es_escribible("Boolean") is True
