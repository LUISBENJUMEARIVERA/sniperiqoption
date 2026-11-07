@echo off
title AUTOTRADER AI - Terminal Cuantitativo Demo
echo Iniciando AutoTrader Quantum 10X...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
