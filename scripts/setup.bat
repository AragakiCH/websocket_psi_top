@echo off
REM ==========================================================================
REM  PSI Top - backend Siemens - crea el entorno virtual e instala todo (Windows)
REM  Uso:  scripts\setup.bat            (desde la raiz del proyecto)
REM        scripts\setup.bat dev        (ademas instala pytest, ruff...)
REM ==========================================================================
setlocal
cd /d "%~dp0\.."

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" ( where python >nul 2>nul && set PY=python )
if "%PY%"=="" (
    echo [ERROR] No se encontro Python. Instala Python 3.10-3.13 desde https://www.python.org
    echo         y marca "Add python.exe to PATH" en el instalador.
    exit /b 1
)

echo [1/4] Python detectado:
%PY% --version || exit /b 1

if not exist venv (
    echo [2/4] Creando entorno virtual en .\venv ...
    %PY% -m venv venv || exit /b 1
) else (
    echo [2/4] El entorno virtual .\venv ya existe; se reutiliza.
)

echo [3/4] Instalando dependencias ...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip wheel >nul
if /i "%1"=="dev" (
    pip install -r requirements-dev.txt || exit /b 1
) else (
    pip install -r requirements.txt || exit /b 1
)

echo [4/4] Preparando configuracion ...
if not exist .env (
    copy .env.example .env >nul
    echo       Creado .env a partir de .env.example. Edita PLC_API_KEY antes de usarlo en planta.
)
if not exist datos mkdir datos

echo.
echo Listo. Para arrancar el servicio:
echo     scripts\run.bat
echo o bien:
echo     venv\Scripts\activate  ^&^&  python run.py
echo Documentacion interactiva: http://localhost:8000/docs
endlocal
