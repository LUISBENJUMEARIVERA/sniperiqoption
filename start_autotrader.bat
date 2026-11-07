@echo off
title AutoTrader Quantum 10X - Servidor Local
chcp 65001 >nul
cd /d "C:\Users\luisa\OneDrive\Escritorio\SKILLS\AUTOTRADER"

echo ================================================================
echo  AUTOTRADER QUANTUM 10X - INICIANDO SISTEMA
echo ================================================================
echo.

:: Liberar puerto 8000 si quedo algun proceso previo colgado
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo [AVISO] Liberando puerto 8000 ocupado por proceso PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)

:: Abrir Chrome especificamente apuntando a localhost:8000
start "" powershell -NoProfile -Command "Start-Sleep -Milliseconds 1800; if (Test-Path 'C:\Program Files\Google\Chrome\Application\chrome.exe') { Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' 'http://localhost:8000' } else { Start-Process 'http://localhost:8000' }"

:: Ejecutar servidor con el entorno virtual del proyecto
echo [INFO] Levantando servidor AutoTrader en http://localhost:8000 ...
if exist ".\.venv\Scripts\python.exe" (
    ".\.venv\Scripts\python.exe" main.py
) else (
    uv run python main.py
)
