# -*- coding: utf-8 -*-
"""
s7_fuentes.py
=============
De un fichero fuente de TIA Portal (`Generate source from blocks` → `.db`,
`.udt`, `.scl`) a la lista de tags con OFFSETS para el driver S7comm.

POR QUÉ EXISTE
--------------
S7comm no lleva nombres ni tipos: lee bytes de un DB por dirección absoluta.
Pedirle al usuario que copie offsets de TIA es inaceptable. Pero TIA exporta
la declaración del DB en un clic, y con ella se puede calcular dónde cae cada
variable con las reglas de un DB de "acceso estándar" (no optimizado):

  * BOOL ocupa un bit; los BOOL consecutivos se empaquetan en un byte.
  * Los tipos de 1 byte (BYTE, CHAR, SINT, USINT) van al siguiente byte.
  * Los de 2 o más bytes (INT, REAL, LREAL...) empiezan en byte PAR.
  * STRING[n] ocupa n+2 bytes y empieza en byte par.
  * ARRAY y STRUCT empiezan en byte par y se rellenan hasta byte par al
    terminar. Un ARRAY of BOOL empaqueta bits.
  * Un DB entero termina en byte par.

Es la misma regla que aplica TIA al rellenar la columna "Offset".

LO QUE ENTIENDE
---------------
    DATA_BLOCK "Data_block_1"
    { S7_Optimized_Access := 'FALSE' }
    VERSION : 0.1
    NON_RETAIN
       STRUCT
          prueba_variable : Bool;
          real_prueba : LReal := 1.0;
          entradas : Array[1..20] of Bool;
          nombre { ExternalAccessible := 'True' } : String[20];
          motor : Struct
             velocidad : Real;
             marcha : Bool;
          END_STRUCT;
          medida : "UDT_Analog";
       END_STRUCT;
    BEGIN
    END_DATA_BLOCK

    TYPE "UDT_Analog"
    VERSION : 0.1
       STRUCT
          valor : Real;
          alarma : Bool;
       END_STRUCT;
    END_TYPE

Un DB de instancia (`DATA_BLOCK "X" "FB_Motor"`) o un DB "de tipo"
(`DATA_BLOCK "X" "UDT_y"`) se resuelve si el FB/UDT viene en los ficheros;
si no, se avisa y se salta. Un DB con `S7_Optimized_Access := 'TRUE'` se
devuelve marcado: TIA no le da offsets y S7comm no lo puede leer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Tipos elementales: nombre TIA (en mayúsculas) -> (tipo del driver, bytes)
ELEMENTALES: Dict[str, Tuple[str, int]] = {
    "BOOL": ("BOOL", 0),
    "BYTE": ("BYTE", 1), "CHAR": ("CHAR", 1), "SINT": ("SINT", 1), "USINT": ("USINT", 1),
    "WORD": ("WORD", 2), "INT": ("INT", 2), "UINT": ("UINT", 2),
    "DATE": ("WORD", 2), "S5TIME": ("WORD", 2),
    "DWORD": ("DWORD", 4), "DINT": ("DINT", 4), "UDINT": ("UDINT", 4),
    "REAL": ("REAL", 4), "TIME": ("TIME", 4), "TIME_OF_DAY": ("DWORD", 4), "TOD": ("DWORD", 4),
    "LWORD": ("LWORD", 8), "LINT": ("LINT", 8), "ULINT": ("ULINT", 8), "LREAL": ("LREAL", 8),
}

#: Tipos que ocupan sitio pero no se pueden enseñar como un valor simple.
#: Se saltan (reservando su espacio) y se avisa.
OPACOS: Dict[str, int] = {"DTL": 12, "DATE_AND_TIME": 8, "DT": 8, "LTIME": 8,
                          "LDT": 8, "LTOD": 8, "WCHAR": 2}


@dataclass
class TagCalculado:
    nombre: str
    offset: int
    bit: int
    tipo: str
    longitud: int = 0     # STRING[n]

    def linea(self, db: int) -> str:
        off = f"{self.offset}.{self.bit}" if self.tipo == "BOOL" else str(self.offset)
        t = f"STRING[{self.longitud}]" if self.tipo == "STRING" else self.tipo
        return f"{self.nombre};DB{db};{off};{t}"


@dataclass
class BloqueCalculado:
    nombre: str
    optimizado: bool
    tamano: int
    tags: List[TagCalculado] = field(default_factory=list)
    avisos: List[str] = field(default_factory=list)
    # "instancia": DATA_BLOCK "X" "FB"; "tipo": DATA_BLOCK "X" "UDT"; "global"
    clase: str = "global"
    base: str = ""


class ErrorFuente(ValueError):
    pass


# ====================================================================== #
# Tokenizador
# ====================================================================== #
_RE_COMENTARIO_BLOQUE = re.compile(r"\(\*.*?\*\)", re.S)
_RE_COMENTARIO_LINEA = re.compile(r"//[^\n]*")
_RE_ATRIBUTOS = re.compile(r"\{[^{}]*\}")
_RE_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|:=|\.\.|[:;,\[\]()=+\-*/<>#.]')


def _tokens(texto: str) -> List[str]:
    texto = _RE_COMENTARIO_BLOQUE.sub(" ", texto)
    texto = _RE_COMENTARIO_LINEA.sub(" ", texto)
    return _RE_TOKEN.findall(texto)


_RE_CABECERA = re.compile(r"\b(DATA_BLOCK|TYPE|FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK)\s+\"([^\"]+)\"", re.I)


def _optimizados(texto: str) -> Dict[str, Optional[bool]]:
    """
    {nombre_de_bloque: True/False/None} leyendo el atributo de CADA bloque.
    Un fichero puede traer varios DB y cada uno lleva el suyo; mirar el
    primero del fichero para todos marcaría mal a los demás.
    """
    salida: Dict[str, Optional[bool]] = {}
    cabeceras = list(_RE_CABECERA.finditer(texto))
    for i, m in enumerate(cabeceras):
        fin = cabeceras[i + 1].start() if i + 1 < len(cabeceras) else len(texto)
        trozo = texto[m.start():fin]
        a = re.search(r"S7_Optimized_Access\s*:=\s*'(TRUE|FALSE)'", trozo, re.I)
        salida[m.group(2)] = (a.group(1).upper() == "TRUE") if a else None
    return salida


# ====================================================================== #
# Parser de declaraciones
# ====================================================================== #
class _Parser:
    """Recorre los tokens de un STRUCT y devuelve un árbol de miembros."""

    def __init__(self, toks: List[str]) -> None:
        self.t = toks
        self.i = 0

    def ver(self, k: int = 0) -> str:
        j = self.i + k
        return self.t[j] if j < len(self.t) else ""

    def coger(self) -> str:
        tok = self.ver()
        self.i += 1
        return tok

    def esperar(self, tok: str) -> None:
        if self.ver().upper() != tok.upper():
            raise ErrorFuente(f"Se esperaba '{tok}' y llegó '{self.ver()}' (token {self.i}).")
        self.i += 1

    def saltar_hasta(self, *fines: str) -> None:
        fines_u = {f.upper() for f in fines}
        while self.ver() and self.ver().upper() not in fines_u:
            self.i += 1

    # ---- gramática ------------------------------------------------------ #
    def miembros(self) -> List[dict]:
        """Lee `nombre : tipo [:= init];` hasta END_STRUCT (que consume)."""
        salida: List[dict] = []
        while True:
            tok = self.ver()
            if not tok:
                raise ErrorFuente("STRUCT sin END_STRUCT.")
            if tok.upper() == "END_STRUCT":
                self.coger()
                if self.ver() == ";":
                    self.coger()
                return salida
            nombre = self.coger().strip('"')
            # Atributos de miembro ya se quitaron con _RE_ATRIBUTOS.
            self.esperar(":")
            tipo = self.tipo()
            # Valor inicial: se salta hasta el ';' de cierre (puede llevar
            # expresiones, arrays [1, 2, 3], strings con ';' dentro no).
            if self.ver() == ":=":
                self.saltar_hasta(";")
            if self.ver() == ";":
                self.coger()
            salida.append({"nombre": nombre, **tipo})

    def tipo(self) -> dict:
        tok = self.coger()
        u = tok.upper()
        if u == "ARRAY":
            self.esperar("[")
            dims: List[Tuple[int, int]] = []
            while True:
                a = int(self.coger())
                self.esperar("..")
                b = int(self.coger())
                dims.append((a, b))
                if self.ver() == ",":
                    self.coger()
                    continue
                break
            self.esperar("]")
            self.esperar("OF")
            return {"clase": "array", "dims": dims, "de": self.tipo()}
        if u == "STRUCT":
            return {"clase": "struct", "miembros": self.miembros()}
        if u in ("STRING", "WSTRING"):
            n = 254
            if self.ver() == "[":
                self.coger()
                n = int(self.coger())
                self.esperar("]")
            return {"clase": "string", "longitud": n, "ancho": 2 if u == "WSTRING" else 1}
        if tok.startswith('"'):
            # OJO: la clave es `udt`, no `nombre`: `nombre` es el del MIEMBRO
            # y se sobreescribe al expandir arrays.
            return {"clase": "udt", "udt": tok.strip('"')}
        if u in ELEMENTALES:
            return {"clase": "elemental", "tipo": u}
        if u in OPACOS:
            return {"clase": "opaco", "tipo": u, "bytes": OPACOS[u]}
        # Tipo desconocido: ocupa 0 y se avisa al colocar.
        return {"clase": "desconocido", "tipo": tok}


def _extraer_bloques(texto: str) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Devuelve ([{nombre, base, optimizado, miembros}], {udt_nombre: miembros}).
    Un fichero puede traer varios bloques (TIA los concatena).
    """
    optimizados = _optimizados(texto)
    limpio = _RE_ATRIBUTOS.sub(" ", texto)
    toks = _tokens(limpio)
    p = _Parser(toks)
    bloques: List[dict] = []
    udts: Dict[str, dict] = {}
    while p.ver():
        tok = p.coger().upper()
        if tok == "DATA_BLOCK":
            nombre = p.coger().strip('"')
            base = ""
            if p.ver().startswith('"'):
                base = p.coger().strip('"')
            # Hasta STRUCT (DB global) o hasta BEGIN (DB de instancia/tipo,
            # sin declaración propia).
            miembros: Optional[List[dict]] = None
            while p.ver():
                u = p.ver().upper()
                if u == "STRUCT":
                    p.coger()
                    miembros = p.miembros()
                    break
                if u in ("BEGIN", "END_DATA_BLOCK"):
                    break
                p.coger()
            p.saltar_hasta("END_DATA_BLOCK")
            if p.ver():
                p.coger()
            bloques.append({"nombre": nombre, "base": base,
                            "optimizado": optimizados.get(nombre),
                            "miembros": miembros})
        elif tok == "TYPE":
            nombre = p.coger().strip('"')
            p.saltar_hasta("STRUCT")
            p.coger()
            udts[nombre] = {"miembros": p.miembros()}
            p.saltar_hasta("END_TYPE")
            if p.ver():
                p.coger()
        elif tok == "FUNCTION_BLOCK":
            # Su interfaz (VAR_INPUT/OUTPUT/IN_OUT/STATIC) es el layout del
            # DB de instancia. Se recoge en ese orden, que es el que usa TIA.
            nombre = p.coger().strip('"')
            miembros: List[dict] = []
            while p.ver() and p.ver().upper() not in ("BEGIN", "END_FUNCTION_BLOCK"):
                u = p.coger().upper()
                if u in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR", "VAR_STAT", "VAR_STATIC"):
                    seccion = _Parser(p.t)
                    seccion.i = p.i
                    # Los miembros terminan en END_VAR, no END_STRUCT.
                    while seccion.ver() and seccion.ver().upper() != "END_VAR":
                        nom = seccion.coger().strip('"')
                        seccion.esperar(":")
                        tipo = seccion.tipo()
                        if seccion.ver() == ":=":
                            seccion.saltar_hasta(";")
                        if seccion.ver() == ";":
                            seccion.coger()
                        miembros.append({"nombre": nom, **tipo})
                    if seccion.ver():
                        seccion.coger()
                    p.i = seccion.i
                elif u == "VAR_TEMP":
                    p.saltar_hasta("END_VAR")
                    p.coger()
            p.saltar_hasta("END_FUNCTION_BLOCK")
            if p.ver():
                p.coger()
            udts[nombre] = {"miembros": miembros, "fb": True}
        # cualquier otro token de cabecera se ignora
    return bloques, udts


# ====================================================================== #
# Colocación (offsets)
# ====================================================================== #
class _Layout:
    def __init__(self) -> None:
        self.byte = 0
        self.bit = 0

    def a_byte(self) -> None:
        if self.bit:
            self.byte += 1
            self.bit = 0

    def a_palabra(self) -> None:
        self.a_byte()
        if self.byte % 2:
            self.byte += 1

    def colocar(self, prefijo: str, m: dict, udts: Dict[str, dict],
                salida: List[TagCalculado], avisos: List[str], pila: Tuple[str, ...] = ()) -> None:
        clase = m["clase"]
        nombre = f"{prefijo}{m.get('nombre', '')}" if m.get("nombre") else prefijo

        if clase == "elemental":
            tipo, tam = ELEMENTALES[m["tipo"]]
            if tipo == "BOOL":
                salida.append(TagCalculado(nombre, self.byte, self.bit, "BOOL"))
                self.bit += 1
                if self.bit == 8:
                    self.byte += 1
                    self.bit = 0
                return
            if tam == 1:
                self.a_byte()
            else:
                self.a_palabra()
            salida.append(TagCalculado(nombre, self.byte, 0, tipo))
            self.byte += tam
            return

        if clase == "string":
            self.a_palabra()
            if m.get("ancho", 1) == 1:
                salida.append(TagCalculado(nombre, self.byte, 0, "STRING", m["longitud"]))
            else:
                avisos.append(f"'{nombre}': WSTRING no se puede mostrar; se reserva su sitio.")
            self.byte += (m["longitud"] + 2) * m.get("ancho", 1)
            return

        if clase == "opaco":
            self.a_palabra()
            avisos.append(f"'{nombre}' ({m['tipo']}) no se puede mostrar como valor; se salta.")
            self.byte += m["bytes"]
            return

        if clase == "desconocido":
            avisos.append(f"'{nombre}': tipo '{m['tipo']}' desconocido; se salta y "
                          f"los offsets posteriores pueden estar mal.")
            return

        if clase == "array":
            self.a_palabra()
            de = m["de"]
            indices = [range(a, b + 1) for a, b in m["dims"]]
            # Un índice: nombre[i]. Varios: nombre[i,j], en orden de fila
            # (el mismo que TIA).
            if len(indices) == 1:
                for i in indices[0]:
                    self.colocar(f"{nombre}[{i}]", {**de, "nombre": ""}, udts, salida, avisos, pila)
            else:
                import itertools
                for combo in itertools.product(*indices):
                    idx = ",".join(str(c) for c in combo)
                    self.colocar(f"{nombre}[{idx}]", {**de, "nombre": ""}, udts, salida, avisos, pila)
            self.a_palabra()
            return

        if clase in ("struct", "udt"):
            if clase == "udt":
                u = udts.get(m["udt"])
                if u is None:
                    avisos.append(f"'{nombre}': el tipo \"{m['udt']}\" no viene en los "
                                  f"ficheros; exporta también su fuente (PLC data types). "
                                  f"Se salta y los offsets posteriores pueden estar mal.")
                    return
                if m["udt"] in pila:
                    avisos.append(f"'{nombre}': tipo recursivo \"{m['udt']}\"; se salta.")
                    return
                miembros = u["miembros"]
                pila = pila + (m["udt"],)
            else:
                miembros = m["miembros"]
            self.a_palabra()
            for hijo in miembros:
                self.colocar(f"{nombre}." if nombre else "", hijo, udts, salida, avisos, pila)
            self.a_palabra()
            return


def _colocar_bloque(b: dict, udts: Dict[str, dict]) -> BloqueCalculado:
    salida = BloqueCalculado(nombre=b["nombre"], optimizado=bool(b.get("optimizado")),
                             tamano=0, base=b.get("base", ""))
    miembros = b.get("miembros")
    if miembros is None and b.get("base"):
        u = udts.get(b["base"])
        if u is None:
            salida.clase = "instancia"
            salida.avisos.append(
                f"«{b['nombre']}» es un DB basado en \"{b['base']}\" y ese bloque no "
                f"viene en los ficheros: exporta también su fuente.")
            return salida
        salida.clase = "instancia" if u.get("fb") else "tipo"
        miembros = u["miembros"]
    if miembros is None:
        salida.avisos.append(f"«{b['nombre']}» no tiene declaración STRUCT.")
        return salida
    if salida.optimizado:
        salida.avisos.append(
            f"«{b['nombre']}» tiene 'Acceso optimizado al bloque': S7comm no puede "
            f"leerlo. En TIA: Propiedades → Atributos → desmarcar, compilar y cargar.")
    lay = _Layout()
    for m in miembros:
        lay.colocar("", m, udts, salida.tags, salida.avisos)
    lay.a_palabra()
    salida.tamano = lay.byte
    return salida


# ====================================================================== #
# Tabla pegada desde el editor del DB (Ctrl+A, Ctrl+C en TIA → Ctrl+V)
# ====================================================================== #
#
# TIA copia las filas del DB como texto tabulado con las columnas del editor:
#     Name  Data type  Offset  Start value  Retain  Accessible from HMI ...
# (con o sin cabecera; el Offset puede ser "..." si no se ha compilado, y
# las filas "Static" / "Struct" son contenedores). Es la forma que menos le
# pide a quien no sabe qué es un offset: seleccionar, copiar, pegar.
#
# Si la columna Offset trae números se USAN tal cual (es lo que dice TIA);
# si no, se calculan con el mismo motor que para las fuentes.

_TIPOS_TABLA = {t.lower(): t for t in list(ELEMENTALES) + list(OPACOS) + ["STRING", "WSTRING"]}
_RE_ARRAY_TABLA = re.compile(r"^array\s*\[(.+)\]\s*of\s+(.+)$", re.I)
_RE_STRING_TABLA = re.compile(r"^(w?string)\s*(?:\[\s*(\d+)\s*\])?$", re.I)


def es_tabla(texto: str) -> bool:
    """¿Parece una tabla pegada del editor (y no una fuente .db)?"""
    if re.search(r"\b(DATA_BLOCK|TYPE|FUNCTION_BLOCK)\b", texto):
        return False
    lineas = [l for l in texto.splitlines() if l.strip()]
    if not lineas:
        return False
    # Tabuladores (lo normal al pegar de TIA) o columnas separadas por dos o
    # más espacios (si el texto pasó por algún sitio que convirtió los tabs).
    con_cols = sum(1 for l in lineas if "\t" in l or re.search(r"\S\s{2,}\S", l))
    return con_cols >= max(1, len(lineas) // 2)


def _tipo_tabla(txt: str) -> dict:
    """'Array[1..20] of Bool' / 'String[20]' / '"UDT_x"' / 'LReal' -> nodo de tipo."""
    t = (txt or "").strip()
    m = _RE_ARRAY_TABLA.match(t)
    if m:
        dims = []
        for parte in m.group(1).split(","):
            a, _, b = parte.partition("..")
            dims.append((int(a.strip()), int(b.strip())))
        return {"clase": "array", "dims": dims, "de": _tipo_tabla(m.group(2))}
    m = _RE_STRING_TABLA.match(t)
    if m:
        return {"clase": "string", "longitud": int(m.group(2) or 254),
                "ancho": 2 if m.group(1).lower().startswith("w") else 1}
    if t.startswith('"'):
        return {"clase": "udt", "udt": t.strip('"')}
    if t.lower() == "struct":
        return {"clase": "struct_tabla"}
    u = _TIPOS_TABLA.get(t.lower())
    if u in ELEMENTALES:
        return {"clase": "elemental", "tipo": u}
    if u in OPACOS:
        return {"clase": "opaco", "tipo": u, "bytes": OPACOS[u]}
    return {"clase": "desconocido", "tipo": t}


def parsear_tabla(nombre_fichero: str, texto: str) -> BloqueCalculado:
    """
    Filas del editor del DB → bloque con offsets.

    Anidamiento: TIA no conserva la sangría en el texto, así que una fila
    `Struct` abre un prefijo y se cierra cuando llega una fila cuyo offset
    (si lo hay) ya no cae dentro del struct, o al terminar. Sin offsets, el
    struct se cierra en la siguiente fila de nivel 0 imposible de detectar:
    se deja el prefijo hasta el final y se avisa.
    """
    salida = BloqueCalculado(nombre=nombre_fichero, optimizado=False, tamano=0,
                             clase="pegado")
    filas: List[Tuple[str, str, Optional[float]]] = []
    for linea in texto.splitlines():
        if not linea.strip():
            continue
        cols = [c.strip() for c in linea.split("\t")]
        if len(cols) < 2:
            cols = re.split(r"\s{2,}", linea.strip())
        # TIA pega columnas VACÍAS delante (la del icono, la de la sangría):
        # el nombre es la primera celda con texto, no la celda 0.
        cols = [c for c in cols if c != ""]
        if not cols:
            continue
        nombre = cols[0].strip('"')
        if nombre.lower() in ("name", "nombre"):
            continue  # cabecera
        if len(cols) < 2:
            continue  # "Static", "Input"...: fila de sección, sin tipo
        # El tipo es la PRIMERA celda posterior que parezca un tipo; el
        # offset, el primer número "n" o "n.b" que venga después de él.
        tipo = ""
        idx_tipo = -1
        for i, c in enumerate(cols[1:], 1):
            if _tipo_tabla(c)["clase"] != "desconocido":
                tipo, idx_tipo = c, i
                break
        if not tipo:
            salida.avisos.append(f"Fila «{nombre}»: no se reconoció su tipo ({cols[1]}); se salta.")
            continue
        off: Optional[float] = None
        for c in cols[idx_tipo + 1:]:
            m = re.match(r"^(\d+)(?:\.(\d))?$", c)
            if m:
                off = float(m.group(1)) + (int(m.group(2)) / 10.0 if m.group(2) else 0.0)
                break
            if c == "...":
                break  # sin compilar: no hay offset
        filas.append((nombre, tipo, off))

    if not filas:
        salida.avisos.append("No se reconoció ninguna fila 'nombre / tipo'. Copia las "
                             "filas del editor del DB (Ctrl+A, Ctrl+C) y pégalas.")
        return salida

    hay_offsets = any(o is not None for _, _, o in filas)
    if not hay_offsets:
        salida.avisos.append("Las filas no traen Offset (el DB no está compilado o es "
                             "optimizado): se han calculado. Compila y carga el DB en "
                             "TIA para estar seguro.")

    lay = _Layout()
    pila: List[Tuple[str, Optional[float], int]] = []   # (prefijo, offset_ini, tam_aprox)
    for nombre, tipo, off in filas:
        nodo = _tipo_tabla(tipo)
        # Un Struct pegado no dice dónde acaba (TIA no conserva la sangría
        # en el texto). Se cierra si una fila cae ANTES de su inicio (no
        # puede ser suya); si no, se mantiene abierto y se avisa al final.
        if off is not None:
            while pila and pila[-1][1] is not None and off < pila[-1][1]:
                pila.pop()
        prefijo = ".".join(p[0] for p in pila)
        prefijo = f"{prefijo}." if prefijo else ""
        if off is not None:
            lay.byte = int(off)
            lay.bit = int(round((off - int(off)) * 10))
        if nodo["clase"] == "struct_tabla":
            lay.a_palabra()
            pila.append((nombre, off, 0))
            continue
        antes = len(salida.tags)
        lay.colocar(prefijo, {**nodo, "nombre": nombre}, {}, salida.tags, salida.avisos)
        if nodo["clase"] == "udt":
            salida.avisos.append(f"'{prefijo}{nombre}' es un tipo \"{nodo['udt']}\": pega "
                                 f"también sus filas desplegadas, o usa la fuente .udt.")
        _ = antes
    lay.a_palabra()
    salida.tamano = lay.byte
    if pila:
        salida.avisos.append(
            f"Las filas posteriores a «{pila[-1][0]}» (Struct) se han tomado como "
            f"miembros suyos; si no lo son, sube la fuente .db (conserva la estructura) "
            f"o pega ese Struct por separado.")
    return salida


def parsear_fuentes(ficheros: List[Tuple[str, str]]) -> Tuple[List[BloqueCalculado], List[str]]:
    """
    `ficheros`: [(nombre_fichero, contenido)]. Devuelve (bloques, avisos).
    Primero se recogen TODOS los UDT/FB de todos los ficheros, y después se
    colocan los DB, para que el orden de subida no importe.
    """
    todos_bloques: List[dict] = []
    todos_udts: Dict[str, dict] = {}
    avisos: List[str] = []
    tablas: List[BloqueCalculado] = []
    for nombre_f, contenido in ficheros:
        if es_tabla(contenido):
            tablas.append(parsear_tabla(nombre_f, contenido))
            continue
        try:
            bloques, udts = _extraer_bloques(contenido)
        except ErrorFuente as exc:
            avisos.append(f"{nombre_f}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            avisos.append(f"{nombre_f}: no se pudo leer ({type(exc).__name__}: {exc}).")
            continue
        todos_udts.update(udts)
        for b in bloques:
            b["fichero"] = nombre_f
        todos_bloques.extend(bloques)
    if not todos_bloques and not tablas:
        avisos.append("No se reconoció ningún DB. Copia las filas del editor del DB en "
                      "TIA (Ctrl+A, Ctrl+C) y pégalas, o sube la fuente .db.")
    return [_colocar_bloque(b, todos_udts) for b in todos_bloques] + tablas, avisos


def numero_en_nombre(nombre: str) -> Optional[int]:
    """'Data_block_1' -> 1, 'DB7_Recetas' -> 7, 'Datos' -> None."""
    m = re.search(r"(?:^|[^A-Za-z0-9])DB\s*_?(\d+)", nombre, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*$", nombre)
    return int(m.group(1)) if m else None
