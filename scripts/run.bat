@echo off
REM Arranca el backend con el venv del proyecto (Windows).
REM     scripts\run.bat             produccion local
REM     scripts\run.bat --reload    desarrollo (recarga al guardar)
setlocal
cd /d "%~dp0\.."
if not exist venv\Scripts\python.exe (
    echo [ERROR] No existe el entorno virtual. Ejecuta primero: scripts\setup.bat
    exit /b 1
)
venv\Scripts\python.exe run.py %*
endlocal
