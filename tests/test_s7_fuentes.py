# -*- coding: utf-8 -*-
"""Cálculo de offsets a partir de las fuentes de TIA Portal (drivers/s7_fuentes.py)."""
from app.drivers.s7_fuentes import numero_en_nombre, parsear_fuentes

FUENTE_DB = '''
DATA_BLOCK "Data_block_1"
{ S7_Optimized_Access := 'FALSE' }
VERSION : 0.1
NON_RETAIN
   STRUCT
      marcha : Bool;
      paro : Bool;
      temperatura : Real := 20.0;
      contador : DInt;
      nombre : String[10];
      entradas : Array[1..10] of Bool;
      motor : Struct
         velocidad : Real;
         activo : Bool;
      END_STRUCT;
      medida : "UDT_Analog";
   END_STRUCT;
BEGIN
END_DATA_BLOCK
'''

FUENTE_UDT = '''
TYPE "UDT_Analog"
VERSION : 0.1
   STRUCT
      valor : Real;
      alarma : Bool;
   END_STRUCT;
END_TYPE
'''


def test_offsets_db_estandar():
    bloques, avisos = parsear_fuentes([("Data_block_1.db", FUENTE_DB), ("UDT_Analog.udt", FUENTE_UDT)])
    assert not avisos
    assert len(bloques) == 1
    b = bloques[0]
    assert b.nombre == "Data_block_1" and not b.optimizado
    por_nombre = {t.nombre: t for t in b.tags}

    # Dos BOOL consecutivos comparten el byte 0.
    assert (por_nombre["marcha"].offset, por_nombre["marcha"].bit) == (0, 0)
    assert (por_nombre["paro"].offset, por_nombre["paro"].bit) == (0, 1)
    # Un Real empieza en byte par: el 2.
    assert por_nombre["temperatura"].offset == 2 and por_nombre["temperatura"].tipo == "REAL"
    assert por_nombre["contador"].offset == 6
    # STRING[10] en byte par, ocupa 12.
    assert por_nombre["nombre"].offset == 10 and por_nombre["nombre"].longitud == 10
    # Array of Bool empieza en byte par (22) y empaqueta bits.
    assert (por_nombre["entradas[1]"].offset, por_nombre["entradas[1]"].bit) == (22, 0)
    assert (por_nombre["entradas[9]"].offset, por_nombre["entradas[9]"].bit) == (23, 0)
    # Struct anidado alineado a par tras el array (24).
    assert por_nombre["motor.velocidad"].offset == 24
    assert (por_nombre["motor.activo"].offset, por_nombre["motor.activo"].bit) == (28, 0)
    # UDT resuelto desde el otro fichero, en byte par (30).
    assert por_nombre["medida.valor"].offset == 30
    assert (por_nombre["medida.alarma"].offset, por_nombre["medida.alarma"].bit) == (34, 0)
    assert b.tamano == 36
    assert por_nombre["marcha"].linea(1) == "marcha;DB1;0.0;BOOL"
    assert por_nombre["nombre"].linea(1) == "nombre;DB1;10;STRING[10]"


def test_db_optimizado_queda_marcado():
    fuente = FUENTE_DB.replace("'FALSE'", "'TRUE'")
    bloques, _ = parsear_fuentes([("x.db", fuente), ("u.udt", FUENTE_UDT)])
    assert bloques[0].optimizado
    assert any("optimizado" in a.lower() for a in bloques[0].avisos)


def test_udt_ausente_avisa():
    bloques, _ = parsear_fuentes([("x.db", FUENTE_DB)])
    assert any("UDT_Analog" in a for a in bloques[0].avisos)


def test_tabla_pegada_del_editor():
    tabla = ("Name\tData type\tOffset\tStart value\n"
             "Static\n"
             "marcha\tBool\t0.0\tfalse\n"
             "temperatura\tReal\t2.0\t0.0\n"
             "contador\tDInt\t6.0\t0\n")
    bloques, _ = parsear_fuentes([("DB7", tabla)])
    b = bloques[0]
    assert b.clase == "pegado"
    por_nombre = {t.nombre: t for t in b.tags}
    assert por_nombre["temperatura"].offset == 2
    assert por_nombre["contador"].offset == 6


def test_numero_en_nombre():
    assert numero_en_nombre("Data_block_1") == 1
    assert numero_en_nombre("DB7_Recetas") == 7
    assert numero_en_nombre("Datos") is None
