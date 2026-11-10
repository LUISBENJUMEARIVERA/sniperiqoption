import os
import time
import threading
import asyncio
from typing import Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import BotConfig, BASE_DIR, DEFAULT_ASSETS, load_saved_credentials, save_credentials_to_file
from app.database import init_db, get_kpi_summary, get_recent_trades, get_strategy_metrics, get_historical_analysis, clear_trade_history
from app.engine import TradingEngine
from app.optimizer import StrategyOptimizer

# Inicializar Base de Datos al arranque
init_db()

app = FastAPI(title="AutoTrader Quantitative AI - Demo Trading Engine", version="1.0.0")

# Motor de trading global
engine = TradingEngine()
optimizer = StrategyOptimizer()

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

def compute_live_candle_timing(timeframe_min: int = 1):
    now_ts = time.time()
    tf_sec = max(60, timeframe_min * 60)
    sec = int(now_ts) % tf_sec
    rem = tf_sec - sec
    is_win = (rem <= 3 or sec <= 1)
    return {
        "second": sec,
        "seconds_remaining": rem,
        "is_trigger_window": is_win,
        "status_text": f"🔥 VENTANA DE CIERRE (restan {rem}s)" if is_win else f"⏳ En formación ({rem}s para cierre)"
    }

class AccountModeRequest(BaseModel):
    mode: str = "PRACTICE"

class StartRequest(BaseModel):
    email: str = ""
    password: str = ""
    assets: Optional[List[str]] = None
    asset: str = "EURUSD"
    timeframe_min: int = 1
    amount: float = 10.0
    min_probability: float = 80.0
    mode: str = "PRACTICE"
    use_simulator: bool = False
    use_martingale: bool = True
    martingale_levels: int = 1
    use_adaptive_threshold: bool = True
    use_confidence_scaling: bool = True
    use_sniper_pullback: bool = True
    min_payout: float = 80.0

@app.get("/api/credentials")
def get_credentials():
    return load_saved_credentials()

@app.get("/api/status")
def get_status():
    kpi = get_kpi_summary()
    timing = compute_live_candle_timing(engine.config.timeframe_min)
    engine.current_state["candle_timing"] = timing
    return {
        "engine": engine.current_state,
        "kpi": kpi,
        "active_trades_count": len(engine.active_trades),
        "min_probability": engine.config.min_probability
    }

@app.get("/api/stats")
def get_stats():
    return get_kpi_summary()

@app.get("/api/trades")
def get_trades(limit: int = 50):
    return get_recent_trades(limit=limit)

@app.get("/api/strategies")
def get_strategies():
    return get_strategy_metrics()

@app.post("/api/account_mode")
def set_account_mode(req: AccountModeRequest):
    """Cambia dinámicamente entre cuenta DEMO (PRACTICE) y CUENTA REAL."""
    return engine.switch_account_mode(req.mode)

@app.post("/api/clear_history")
def reset_history():
    """Blanquea todo el historial para una nueva investigación estadística limpia."""
    clear_trade_history()
    engine.active_trades.clear()
    engine.martingale_state.clear()
    engine.ensemble.reload_weights()
    return {"success": True, "message": "Historial blanqueado y pesos de estrategias restablecidos a valores óptimos."}

@app.get("/api/history")
def get_history(start_date: Optional[str] = None, end_date: Optional[str] = None):
    return get_historical_analysis(start_date=start_date, end_date=end_date)

@app.post("/api/start")
def start_bot(req: StartRequest):
    print("\n========================================================")
    print(">> [ORDEN] Solicitud de INICIO recibida desde el navegador.")
    print(f">> [USUARIO] Cuenta: {req.email}")
    if req.email and req.password:
        save_credentials_to_file(req.email, req.password)
        print(">> [GUARDADO] Credenciales guardadas exitosamente en data/credentials.json.")
    
    is_auto_mode = not req.assets or len(req.assets) == 0
    selected_assets = req.assets if not is_auto_mode else []
    mode_text = "SIMULADOR LOCAL DE RESPALDO" if req.use_simulator else "CONEXIÓN A IQ OPTION (DEMO)"
    print(f">> [MODO SELECCIONADO] {mode_text}")
    if is_auto_mode:
        print(">> [PARES ACTIVOS] 🤖 MODO AUTÓNOMO ACTIVADO (Auto-descubrimiento y rotación de los mejores 8 a 10 pares por Payout)")
    else:
        print(f">> [PARES ACTIVOS] {', '.join(selected_assets)}")
    print(f">> [UMBRAL DISPARO] >= {req.min_probability}%")
    if req.use_martingale:
        print(f">> [MARTINGALA ACTIVA] Reentradas de recuperación: hasta {req.martingale_levels} nivel(es) (x2)")
    print("========================================================\n")

    cfg = BotConfig(
        email=req.email,
        password=req.password,
        assets=selected_assets,
        asset=selected_assets[0] if selected_assets else "EURUSD",
        timeframe_min=req.timeframe_min,
        amount=req.amount,
        min_probability=req.min_probability,
        mode=req.mode,
        use_simulator=req.use_simulator,
        use_martingale=req.use_martingale,
        martingale_levels=min(1, max(1, req.martingale_levels)),
        use_adaptive_threshold=req.use_adaptive_threshold,
        use_confidence_scaling=req.use_confidence_scaling,
        use_sniper_pullback=req.use_sniper_pullback,
        min_payout=req.min_payout
    )
    engine.start(cfg)
    return {
        "success": True, 
        "message": f"Motor iniciado en {mode_text} con {len(selected_assets)} pares."
    }

@app.post("/api/stop")
def stop_bot():
    print("\n>> [DETENCIÓN] Solicitud de parada recibida. Deteniendo escáner...\n")
    engine.stop()
    return {"success": True, "message": "Motor detenido."}

@app.post("/api/shutdown")
def shutdown_server():
    """Detiene el motor, libera el puerto y apaga completamente el proceso del servidor."""
    print("\n========================================================")
    print(">> [APAGADO TOTAL] Solicitud de apagado de servidor recibida.")
    print(">> Deteniendo motor, cerrando procesos y liberando puerto 8000...")
    print("========================================================\n")
    try:
        engine.stop()
    except Exception:
        pass

    def _delayed_exit():
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {
        "success": True,
        "message": "Servidor apagado exitosamente y puerto 8000 liberado."
    }

@app.post("/api/optimize")
def optimize_strategies():
    """Ejecuta el análisis de autoaprendizaje y reajuste dinámico de estrategias."""
    result = optimizer.analyze_and_optimize()
    if result["success"]:
        # Recargar pesos en el ensamble del motor
        engine.ensemble.reload_weights()
    return result

# WebSocket para transmisión en tiempo real de señales al Dashboard
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            kpi = get_kpi_summary()
            engine.current_state["candle_timing"] = compute_live_candle_timing(engine.config.timeframe_min)
            trades_list = get_recent_trades(limit=50)
            payload = {
                "engine": engine.current_state,
                "kpi": kpi,
                "active_trades": list(engine.active_trades.values()),
                "trades": trades_list
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1) # Pulso cada segundo
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

# Servir archivos estáticos del Dashboard
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
