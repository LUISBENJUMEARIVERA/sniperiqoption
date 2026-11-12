import sqlite3
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from app.config import DB_PATH, WEIGHTS_PATH
import os

logger = logging.getLogger("AutoTrader.DB")

DEFAULT_STRATEGIES = [
    "Triple EMA Ribbon",
    "Dynamic RSI Reversal",
    "MACD Momentum Cross",
    "Bollinger Squeeze & Piercing",
    "Stochastic RSI Timing",
    "SuperTrend Volatility",
    "Candlestick Price Action",
    "Support & Resistance Pivots",
    "Volume Flow Direction",
    "ADX Trend Strength"
]

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Tabla principal de operaciones (trades)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_uid TEXT UNIQUE,
        timestamp TEXT NOT NULL,
        asset TEXT NOT NULL,
        action TEXT NOT NULL,
        amount REAL NOT NULL,
        expiration_min INTEGER NOT NULL,
        payout REAL NOT NULL,
        probability REAL NOT NULL,
        result TEXT NOT NULL, -- 'PENDING', 'WIN', 'LOSS', 'EQUAL'
        profit REAL NOT NULL,
        strategy_votes TEXT NOT NULL, -- JSON
        market_snapshot TEXT NOT NULL, -- JSON
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Tabla de métricas por estrategia
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS strategy_metrics (
        strategy_name TEXT PRIMARY KEY,
        weight REAL NOT NULL,
        total_signals INTEGER DEFAULT 0,
        winning_signals INTEGER DEFAULT 0,
        losing_signals INTEGER DEFAULT 0,
        accuracy REAL DEFAULT 0.0,
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Tabla de registros de optimización y aprendizaje
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS optimization_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        trades_analyzed INTEGER,
        old_weights TEXT,
        new_weights TEXT,
        improvement_summary TEXT
    )
    """)
    
    # Inicializar pesos por defecto si la tabla está vacía
    cursor.execute("SELECT COUNT(*) FROM strategy_metrics")
    if cursor.fetchone()[0] == 0:
        calibrated_weights = {
            "SuperTrend Volatility": 0.18,
            "Triple EMA Ribbon": 0.18,
            "Candlestick Price Action": 0.16,
            "ADX Trend Strength": 0.14,
            "Bollinger Squeeze & Piercing": 0.12,
            "Dynamic RSI Reversal": 0.08,
            "Stochastic RSI Timing": 0.06,
            "Support & Resistance Pivots": 0.04,
            "MACD Momentum Cross": 0.03,
            "Volume Flow Direction": 0.01
        }
        for name, w in calibrated_weights.items():
            cursor.execute("""
            INSERT INTO strategy_metrics (strategy_name, weight, total_signals, winning_signals, losing_signals, accuracy)
            VALUES (?, ?, 0, 0, 0, 80.0)
            """, (name, w))
            
    conn.commit()
    conn.close()
    logger.info("Base de datos SQLite inicializada correctamente en %s", DB_PATH)

def clear_trade_history():
    """Blanquea completamente el registro de operaciones y resetea las métricas para nueva investigación."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM trades")
    cursor.execute("DELETE FROM optimization_logs")
    
    calibrated_weights = {
        "SuperTrend Volatility": 0.18,
        "Triple EMA Ribbon": 0.18,
        "Candlestick Price Action": 0.16,
        "ADX Trend Strength": 0.14,
        "Bollinger Squeeze & Piercing": 0.12,
        "Dynamic RSI Reversal": 0.08,
        "Stochastic RSI Timing": 0.06,
        "Support & Resistance Pivots": 0.04,
        "MACD Momentum Cross": 0.03,
        "Volume Flow Direction": 0.01
    }
    
    cursor.execute("DELETE FROM strategy_metrics")
    for name, w in calibrated_weights.items():
        cursor.execute("""
        INSERT INTO strategy_metrics (strategy_name, weight, total_signals, winning_signals, losing_signals, accuracy)
        VALUES (?, ?, 0, 0, 0, 80.0)
        """, (name, w))
        
    conn.commit()
    conn.close()
    
    try:
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(calibrated_weights, f, indent=2)
    except Exception:
        pass
        
    logger.info("Histórico de operaciones blanqueado y métricas restablecidas exitosamente.")

def record_trade(
    trade_uid: str,
    asset: str,
    action: str,
    amount: float,
    expiration_min: int,
    payout: float,
    probability: float,
    strategy_votes: Dict[str, Any],
    market_snapshot: Dict[str, Any]
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO trades (
        trade_uid, timestamp, asset, action, amount, expiration_min, 
        payout, probability, result, profit, strategy_votes, market_snapshot
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0.0, ?, ?)
    """, (
        trade_uid, now_str, asset, action, amount, expiration_min,
        payout, probability, json.dumps(strategy_votes), json.dumps(market_snapshot)
    ))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def resolve_trade(trade_uid: str, result: str, profit: float):
    """Actualiza el resultado de la operación (WIN, LOSS) y calcula el profit."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE trades 
    SET result = ?, profit = ?
    WHERE trade_uid = ?
    """, (result.upper(), profit, trade_uid))
    
    # Obtener el trade para actualizar métricas de estrategias individuales
    cursor.execute("SELECT action, strategy_votes FROM trades WHERE trade_uid = ?", (trade_uid,))
    row = cursor.fetchone()
    if row:
        action = row["action"]
        votes = json.loads(row["strategy_votes"]) if row["strategy_votes"] else {}
        for s_name, vote in votes.items():
            # Si votó a favor de la acción tomada
            voted_for_trade = (action == "CALL" and vote > 0) or (action == "PUT" and vote < 0)
            if voted_for_trade:
                is_win = (result.upper() == "WIN")
                win_inc = 1 if is_win else 0
                loss_inc = 0 if is_win else 1
                cursor.execute("""
                UPDATE strategy_metrics
                SET total_signals = total_signals + 1,
                    winning_signals = winning_signals + ?,
                    losing_signals = losing_signals + ?,
                    accuracy = ROUND((winning_signals + ?) * 100.0 / (total_signals + 1), 2),
                    last_updated = CURRENT_TIMESTAMP
                WHERE strategy_name = ?
                """, (win_inc, loss_inc, win_inc, s_name))
                
    conn.commit()
    conn.close()

def mark_trade_recovered(trade_uid: str):
    """
    Marca una operación G0 inicial como 'RECOVERED' cuando su reentrada G1 gana,
    manteniendo intacto su profit negativo para que SUM(profit) sea 100% exacto al centavo,
    mientras el Win Rate refleja la tasa real de ciclos ganadores.
    """
    if not trade_uid:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE trades
    SET result = 'RECOVERED'
    WHERE trade_uid = ? AND result = 'LOSS'
    """, (trade_uid,))
    conn.commit()
    conn.close()

def get_kpi_summary() -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE result != 'PENDING'")
    total_completed = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE result = 'WIN'")
    total_wins = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE result = 'LOSS'")
    total_losses = cursor.fetchone()[0]
    
    cursor.execute("SELECT COALESCE(SUM(profit), 0.0) FROM trades WHERE result != 'PENDING'")
    net_profit = round(cursor.fetchone()[0], 2)
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE result = 'PENDING'")
    active_pending = cursor.fetchone()[0]
    
    effective_cycles = total_wins + total_losses
    win_rate = round((total_wins / effective_cycles * 100.0), 1) if effective_cycles > 0 else 0.0
    
    conn.close()
    return {
        "total_trades": total_completed,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": win_rate,
        "net_profit": net_profit,
        "active_pending": active_pending
    }

def get_recent_trades(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, trade_uid, timestamp, asset, action, amount, expiration_min, 
           payout, probability, result, profit, strategy_votes, market_snapshot
    FROM trades
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    trades = []
    for r in rows:
        trades.append({
            "id": r["id"],
            "trade_uid": r["trade_uid"],
            "timestamp": r["timestamp"],
            "asset": r["asset"],
            "action": r["action"],
            "amount": r["amount"],
            "expiration_min": r["expiration_min"],
            "payout": r["payout"],
            "probability": r["probability"],
            "result": r["result"],
            "profit": r["profit"],
            "strategy_votes": json.loads(r["strategy_votes"]) if r["strategy_votes"] else {},
            "market_snapshot": json.loads(r["market_snapshot"]) if r["market_snapshot"] else {}
        })
    conn.close()
    return trades

def get_historical_analysis(start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
    """Consulta histórica con filtros por rango de fechas persistida en SQLite."""
    conn = get_connection()
    cursor = conn.cursor()
    
    where_clauses = ["result != 'PENDING'"]
    params = []
    
    if start_date:
        where_clauses.append("timestamp >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where_clauses.append("timestamp <= ?")
        params.append(f"{end_date} 23:59:59")
        
    where_sql = " AND ".join(where_clauses)
    
    cursor.execute(f"SELECT COUNT(*) FROM trades WHERE {where_sql}", params)
    total_trades = cursor.fetchone()[0]
    
    cursor.execute(f"SELECT COUNT(*) FROM trades WHERE {where_sql} AND result = 'WIN'", params)
    total_wins = cursor.fetchone()[0]
    
    cursor.execute(f"SELECT COUNT(*) FROM trades WHERE {where_sql} AND result = 'LOSS'", params)
    total_losses = cursor.fetchone()[0]
    
    cursor.execute(f"SELECT COALESCE(SUM(profit), 0.0) FROM trades WHERE {where_sql}", params)
    net_profit = round(cursor.fetchone()[0], 2)
    
    effective_cycles = total_wins + total_losses
    win_rate = round((total_wins / effective_cycles * 100.0), 1) if effective_cycles > 0 else 0.0
    
    # Operaciones del rango (incluyendo pendientes)
    trade_where = []
    trade_params = []
    if start_date:
        trade_where.append("timestamp >= ?")
        trade_params.append(f"{start_date} 00:00:00")
    if end_date:
        trade_where.append("timestamp <= ?")
        trade_params.append(f"{end_date} 23:59:59")
        
    trade_where_sql = f"WHERE {' AND '.join(trade_where)}" if trade_where else ""
    cursor.execute(f"""
    SELECT id, trade_uid, timestamp, asset, action, amount, expiration_min, 
           payout, probability, result, profit, strategy_votes, market_snapshot
    FROM trades
    {trade_where_sql}
    ORDER BY id DESC
    LIMIT 500
    """, trade_params)
    
    rows = cursor.fetchall()
    trades = []
    for r in rows:
        trades.append({
            "id": r["id"],
            "trade_uid": r["trade_uid"],
            "timestamp": r["timestamp"],
            "asset": r["asset"],
            "action": r["action"],
            "amount": r["amount"],
            "expiration_min": r["expiration_min"],
            "payout": r["payout"],
            "probability": r["probability"],
            "result": r["result"],
            "profit": r["profit"],
            "strategy_votes": json.loads(r["strategy_votes"]) if r["strategy_votes"] else {},
            "market_snapshot": json.loads(r["market_snapshot"]) if r["market_snapshot"] else {}
        })
        
    conn.close()
    return {
        "kpi": {
            "total_trades": total_trades,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": win_rate,
            "net_profit": net_profit,
            "start_date": start_date or "Inicio",
            "end_date": end_date or "Actual"
        },
        "trades": trades
    }

def get_strategy_metrics() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT strategy_name, weight, total_signals, winning_signals, losing_signals, accuracy
    FROM strategy_metrics
    ORDER BY weight DESC
    """)
    rows = cursor.fetchall()
    metrics = []
    for r in rows:
        metrics.append({
            "strategy_name": r["strategy_name"],
            "weight": round(r["weight"], 4),
            "total_signals": r["total_signals"],
            "winning_signals": r["winning_signals"],
            "losing_signals": r["losing_signals"],
            "accuracy": r["accuracy"]
        })
    conn.close()
    return metrics

def update_strategy_weights(new_weights: Dict[str, float], trades_analyzed: int, summary: str):
    conn = get_connection()
    cursor = conn.cursor()
    
    # Guardar log de optimización
    cursor.execute("SELECT strategy_name, weight FROM strategy_metrics")
    old_weights = {r["strategy_name"]: r["weight"] for r in cursor.fetchall()}
    
    cursor.execute("""
    INSERT INTO optimization_logs (trades_analyzed, old_weights, new_weights, improvement_summary)
    VALUES (?, ?, ?, ?)
    """, (trades_analyzed, json.dumps(old_weights), json.dumps(new_weights), summary))
    
    for name, w in new_weights.items():
        cursor.execute("UPDATE strategy_metrics SET weight = ? WHERE strategy_name = ?", (w, name))
        
    conn.commit()
    conn.close()
    
    # También guardar copia JSON para persistencia rápida
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(new_weights, f, indent=2)
