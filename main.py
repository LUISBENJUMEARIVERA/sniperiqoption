import sys
import socket

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Solución definitiva para WinError 10054 en Windows (Python 3.13 / Asyncio Proactor)
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        def _clean_call_connection_lost(self, exc):
            if self._called_connection_lost:
                return
            try:
                self._protocol.connection_lost(exc)
            finally:
                if hasattr(self._sock, 'shutdown') and self._sock.fileno() != -1:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except (ConnectionResetError, OSError):
                        pass
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except (ConnectionResetError, OSError):
                        pass
                    self._sock = None
                server = self._server
                if server is not None:
                    server._detach(self)
                    self._server = None
                self._called_connection_lost = True

        _ProactorBasePipeTransport._call_connection_lost = _clean_call_connection_lost
    except Exception:
        pass

import uvicorn
import webbrowser
import threading
import time

def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://localhost:8000")

if __name__ == "__main__":
    print("================================================================")
    print(" AUTOTRADER QUANTUM 10X - SISTEMA DE TRADING AUTÓNOMO DEMO")
    print("================================================================")
    print(" Servidor Web y WebSocket activo en: http://localhost:8000")
    print(" O en: http://127.0.0.1:8000")
    print(" Modos Soportados: DEMO (PRACTICE) y CUENTA REAL (Bajo confirmación explícita)")
    print(" Presione Ctrl+C en esta consola para detener el servidor.")
    print("================================================================")
    
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False, access_log=False, log_level="warning")
