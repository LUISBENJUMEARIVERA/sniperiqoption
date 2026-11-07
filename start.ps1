# AutoTrader Quantum 10X Launcher
Write-Host "Iniciando AutoTrader Quantum 10X en cuenta DEMO..." -ForegroundColor Cyan
if (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" main.py
} else {
    python main.py
}
