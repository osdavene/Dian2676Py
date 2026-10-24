@echo off
title Simulador DIAN 2676 - Python FastAPI
cd /d "%~dp0"
echo ============================================================
echo   SIMULADOR CONCURSO DIAN 2676 - PYTHON (FastAPI + PostgreSQL)
echo ============================================================
echo.
echo Iniciando servidor web en http://127.0.0.1:8000 ...
echo Presiona Ctrl+C para detener el servidor.
echo.
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
pause
