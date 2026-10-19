# -*- coding: utf-8 -*-
"""
escritura.py
============
Conversión de un valor que llega en JSON al tipo EXACTO que espera el PLC.

POR QUÉ ES UN MÓDULO APARTE
---------------------------
Leer y escribir no son simétricos. Si al leer se interpreta mal un tipo, se
ve un número raro en pantalla y alguien lo nota. Si al escribir se convierte
mal, **se mueve algo en la planta**, y puede hacerlo sin dar error.

Los dos drivers Siemens (OPC UA y S7comm) comparten este módulo: la
conversión es la misma y no debe divergir entre ellos.

LAS TRAMPAS QUE EVITA
---------------------
1. `bool("false")` en Python es **True**. Una conversión ingenua ARRANCARÍA el
   motor que se pretendía parar. Los booleanos se tratan con una lista
   explícita de textos aceptados.
2. Los enteros tienen ANCHO. Escribir 40000 en un Int16 (máximo 32767) puede
   desbordar en silencio. Se comprueba el rango antes de salir a la red.
3. `int` no vale donde el PLC espera `Float`: asyncua es estricto con el
   Variant. Se convierte explícitamente.
4. `3.7` en un entero se truncaría a 3 sin avisar: se rechaza.
"""
from __future__ import annotations

from typing import Any

# Rango de cada entero (nombres OPC UA).
RANGOS_ENTEROS = {
    "SByte": (-128, 127),
    "Byte": (0, 255),
    "Int16": (-32768, 32767),
    "UInt16": (0, 65535),
    "Int32": (-2147483648, 2147483647),
    "UInt32": (0, 4294967295),
    "Int64": (-9223372036854775808, 9223372036854775807),
    "UInt64": (0, 18446744073709551615),
}

TIPOS_REALES = {"Float", "Double"}
TIPOS_TEXTO = {"String", "LocalizedText", "XmlElement"}

TEXTO_VERDADERO = {"true", "1", "on", "si", "sí", "verdadero", "activo", "high"}
TEXTO_FALSO = {"false", "0", "off", "no", "falso", "inactivo", "low"}

# Dos vocabularios para los mismos tipos: el driver S7comm habla IEC 61131
# (BOOL, REAL, DINT…) porque es lo que el programador lee en TIA; el OPC UA
# deja el nombre del servidor (Boolean, Float, Int32). La traducción se hace
# AQUÍ, a la entrada de `convertir()` y `es_escribible()`, no en cada llamador.
ALIAS_IEC = {
    "BOOL": "Boolean",
    "SINT": "SByte", "USINT": "Byte", "BYTE": "Byte", "CHAR": "SByte",
    "INT": "Int16", "UINT": "UInt16", "WORD": "UInt16",
    "DINT": "Int32", "UDINT": "UInt32", "DWORD": "UInt32", "TIME": "Int32",
    "LINT": "Int64", "ULINT": "UInt64", "LWORD": "UInt64",
    "REAL": "Float", "LREAL": "Double",
    "STRING": "String", "WSTRING": "String",
}


def normalizar_tipo(data_type: str) -> str:
    """Nombre OPC UA del tipo, venga escrito en OPC UA o en IEC 61131."""
    tipo = (data_type or "").strip()
    return ALIAS_IEC.get(tipo.upper(), tipo)


class ErrorDeTipo(ValueError):
    """El valor no encaja con el tipo del tag. Lleva un mensaje accionable."""


def convertir(valor: Any, data_type: str) -> Any:
    """
    Devuelve `valor` convertido al tipo Python que corresponde a `data_type`.
    Lanza `ErrorDeTipo` si no encaja. Nunca adivina: ante la duda, falla.
    """
    tipo = normalizar_tipo(data_type)

    # ---- Booleanos ------------------------------------------------------
    if tipo == "Boolean":
        if isinstance(valor, bool):
            return valor
        if isinstance(valor, (int, float)):
            if valor in (0, 1):
                return bool(valor)
            raise ErrorDeTipo(
                f"'{valor}' no es un booleano. Para un tag Boolean se admite "
                f"true/false, o los números 0 y 1.")
        if isinstance(valor, str):
            v = valor.strip().lower()
            if v in TEXTO_VERDADERO:
                return True
            if v in TEXTO_FALSO:
                return False
            raise ErrorDeTipo(
                f"'{valor}' no es un booleano reconocible. Usa true/false.")
        raise ErrorDeTipo(f"No se puede escribir {type(valor).__name__} en un tag Boolean.")

    # ---- Enteros --------------------------------------------------------
    if tipo in RANGOS_ENTEROS:
        minimo, maximo = RANGOS_ENTEROS[tipo]
        if isinstance(valor, bool):
            raise ErrorDeTipo(
                f"El tag es {tipo} (entero) y se recibió un booleano. "
                f"Revisa a qué variable está enlazado.")
        if isinstance(valor, float):
            if not valor.is_integer():
                raise ErrorDeTipo(
                    f"El tag es {tipo} (entero) y {valor} tiene decimales. "
                    f"Mándalo ya redondeado.")
            valor = int(valor)
        if isinstance(valor, str):
            try:
                valor = int(valor.strip())
            except ValueError:
                raise ErrorDeTipo(f"'{valor}' no es un número entero válido.")
        if not isinstance(valor, int):
            raise ErrorDeTipo(f"No se puede escribir {type(valor).__name__} en un tag {tipo}.")
        if not (minimo <= valor <= maximo):
            raise ErrorDeTipo(
                f"{valor} se sale del rango de un {tipo} ({minimo} a {maximo}). "
                f"El PLC podría desbordarlo en silencio, así que se rechaza aquí.")
        return valor

    # ---- Reales ---------------------------------------------------------
    if tipo in TIPOS_REALES:
        if isinstance(valor, bool):
            raise ErrorDeTipo(
                f"El tag es {tipo} (decimal) y se recibió un booleano.")
        if isinstance(valor, (int, float)):
            return float(valor)
        if isinstance(valor, str):
            try:
                return float(valor.strip().replace(",", "."))
            except ValueError:
                raise ErrorDeTipo(f"'{valor}' no es un número válido.")
        raise ErrorDeTipo(f"No se puede escribir {type(valor).__name__} en un tag {tipo}.")

    # ---- Texto ----------------------------------------------------------
    if tipo in TIPOS_TEXTO:
        if isinstance(valor, bool):
            return "true" if valor else "false"
        if isinstance(valor, (str, int, float)):
            return str(valor)
        raise ErrorDeTipo(f"No se puede escribir {type(valor).__name__} en un tag de texto.")

    # ---- Desconocido ----------------------------------------------------
    raise ErrorDeTipo(
        f"Tipo de dato '{data_type}' no soportado para escritura. "
        f"Soportados: Boolean, {', '.join(sorted(RANGOS_ENTEROS))}, "
        f"{', '.join(sorted(TIPOS_REALES))}, {', '.join(sorted(TIPOS_TEXTO))}.")


def es_escribible(data_type: str) -> bool:
    """¿Sabe este módulo convertir a ese tipo? Para avisar al configurar."""
    tipo = normalizar_tipo(data_type)
    return (tipo == "Boolean" or tipo in RANGOS_ENTEROS
            or tipo in TIPOS_REALES or tipo in TIPOS_TEXTO)
