@echo off
REM Ejecuta las pruebas (requiere scripts\setup.bat dev).
setlocal
cd /d "%~dp0\.."
venv\Scripts\python.exe -m pytest -q %*
endlocal
