import json
import logging
import numpy as np
from datetime import datetime
from typing import Dict, Any, List
from app.database import get_connection, update_strategy_weights

logger = logging.getLogger("AutoTrader.Optimizer")

class StrategyOptimizer:
    def __init__(self, learning_rate: float = 0.5):
        self.learning_rate = learning_rate

    def analyze_and_optimize(self) -> Dict[str, Any]:
        """
        Analiza el historial de operaciones cerradas, evalúa la precisión
        real de cada una de las 10 estrategias, y optimiza los pesos dinámicos
        del ensamble premiando los aciertos y penalizando los fallos.
        """
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
        SELECT trade_uid, action, result, profit, strategy_votes, probability
        FROM trades
        WHERE result IN ('WIN', 'LOSS')
        """)
        trades = cursor.fetchall()
        
        if len(trades) < 3:
            conn.close()
            return {
                "success": False,
                "message": f"Se requieren al menos 3 operaciones cerradas para optimizar (actuales: {len(trades)}). Continúe la operativa demo.",
                "trades_analyzed": len(trades)
            }
            
        cursor.execute("SELECT strategy_name, weight FROM strategy_metrics")
        current_weights = {r["strategy_name"]: r["weight"] for r in cursor.fetchall()}
        conn.close()
        
        # Estadísticas de acierto por estrategia
        strategy_stats = {name: {"hits": 0, "misses": 0, "neutral": 0, "total_voted": 0} for name in current_weights}
        combo_stats = {} # Para encontrar las mejores combinaciones de estrategias

        for t in trades:
            action = t["action"]
            is_win = (t["result"] == "WIN")
            votes = json.loads(t["strategy_votes"]) if t["strategy_votes"] else {}
            
            aligned_strategies = []
            for s_name, vote in votes.items():
                if s_name not in strategy_stats:
                    continue
                    
                voted_call = (vote > 0)
                voted_put = (vote < 0)
                
                if (action == "CALL" and voted_call) or (action == "PUT" and voted_put):
                    strategy_stats[s_name]["total_voted"] += 1
                    aligned_strategies.append(s_name)
                    if is_win:
                        strategy_stats[s_name]["hits"] += 1
                    else:
                        strategy_stats[s_name]["misses"] += 1
                else:
                    strategy_stats[s_name]["neutral"] += 1
                    
            # Analizar combos de 2 o 3 estrategias que estuvieron activas
            if len(aligned_strategies) >= 2:
                combo_key = " + ".join(sorted(aligned_strategies[:3]))
                if combo_key not in combo_stats:
                    combo_stats[combo_key] = {"wins": 0, "losses": 0}
                if is_win:
                    combo_stats[combo_key]["wins"] += 1
                else:
                    combo_stats[combo_key]["losses"] += 1

        # Re-ponderación mediante regla exponencial basada en Win Rate
        new_weights = {}
        performance_report = []
        
        for name, stats in strategy_stats.items():
            total = stats["total_voted"]
            accuracy = (stats["hits"] / total * 100.0) if total > 0 else 50.0
            
            # Factor de actualización: base 50%
            delta = (accuracy - 50.0) / 100.0
            updated_raw = current_weights[name] * np.exp(self.learning_rate * delta)
            # Evitar pesos cero o excesivos
            new_weights[name] = max(0.02, min(0.35, updated_raw))
            
            performance_report.append({
                "strategy": name,
                "total_voted": total,
                "hits": stats["hits"],
                "misses": stats["misses"],
                "win_rate": round(accuracy, 1),
                "old_weight": round(current_weights[name], 4)
            })

        # Normalizar para que sumen 1.0
        w_sum = sum(new_weights.values())
        new_weights = {k: round(v / w_sum, 4) for k, v in new_weights.items()}

        # Agregar nuevos pesos al reporte
        for item in performance_report:
            item["new_weight"] = new_weights[item["strategy"]]

        # Ordenar estrategias por tasa de acierto
        performance_report.sort(key=lambda x: x["win_rate"], reverse=True)
        
        # Encontrar los mejores combos descubiertos
        top_combos = []
        for combo, cstats in combo_stats.items():
            tot = cstats["wins"] + cstats["losses"]
            if tot >= 2:
                c_wr = round((cstats["wins"] / tot) * 100.0, 1)
                top_combos.append({
                    "combination": combo,
                    "wins": cstats["wins"],
                    "losses": cstats["losses"],
                    "win_rate": c_wr
                })
        top_combos.sort(key=lambda x: (x["win_rate"], x["wins"]), reverse=True)

        summary_msg = f"Optimización completada sobre {len(trades)} operaciones. "
        if performance_report:
            top_strat = performance_report[0]
            summary_msg += f"Mejor estrategia: '{top_strat['strategy']}' con {top_strat['win_rate']}% de acierto. "
        if top_combos:
            summary_msg += f"Mejor sinergia: '{top_combos[0]['combination']}' ({top_combos[0]['win_rate']}% WR)."

        # Guardar en base de datos
        update_strategy_weights(new_weights, len(trades), summary_msg)

        return {
            "success": True,
            "message": summary_msg,
            "trades_analyzed": len(trades),
            "performance_report": performance_report,
            "top_combos": top_combos[:5],
            "optimized_weights": new_weights
        }
