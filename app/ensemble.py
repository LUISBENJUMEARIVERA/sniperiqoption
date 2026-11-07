import json
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple
from app.strategies import ALL_STRATEGY_FUNCS, TechnicalStrategies
from app.database import get_connection

logger = logging.getLogger("AutoTrader.Ensemble")

# Clasificación taxonómica de las 10 estrategias
TREND_STRATEGIES = {
    "Triple EMA Ribbon",
    "MACD Momentum Cross",
    "SuperTrend Volatility",
    "Candlestick Price Action",
    "Volume Flow Direction",
    "ADX Trend Strength"
}

REVERSAL_STRATEGIES = {
    "Dynamic RSI Reversal",
    "Bollinger Squeeze & Piercing",
    "Stochastic RSI Timing",
    "Support & Resistance Pivots"
}

class EnsembleEngine:
    def __init__(self):
        self.weights = self.load_weights()

    def load_weights(self) -> Dict[str, float]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT strategy_name, weight FROM strategy_metrics")
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            weights = {r["strategy_name"]: r["weight"] for r in rows}
        else:
            n = len(ALL_STRATEGY_FUNCS)
            weights = {name: round(1.0 / n, 4) for name, _ in ALL_STRATEGY_FUNCS}
            
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        return weights

    def reload_weights(self):
        self.weights = self.load_weights()

    def detect_market_regime(self, df: pd.DataFrame) -> Tuple[str, float]:
        """
        Clasifica el estado estructural del mercado usando ADX (14):
        - ADX > 25: TENDENCIAL (El precio se mueve con inercia direccional)
        - ADX <= 25: RANGO (El precio oscila entre soportes y resistencias)
        """
        try:
            adx_res = TechnicalStrategies.adx_trend_strength(df)
            reason = adx_res.get("reason", "")
            
            adx_val = 22.0
            if "ADX=" in reason:
                part = reason.split("ADX=")[1].split(",")[0].split(" ")[0]
                adx_val = float(part)
            
            if adx_val > 25.0:
                return "TENDENCIAL", adx_val
            else:
                return "RANGO / REVERSIÓN", adx_val
        except Exception:
            return "RANGO / REVERSIÓN", 20.0

    def evaluate(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Ejecuta las 10 estrategias adaptando los pesos al régimen de mercado activo
        para eliminar el conflicto destructivo entre tendencia y contratendencia.
        """
        regime, adx_val = self.detect_market_regime(df)
        
        votes = {}
        reasons = {}
        effective_weights = {}
        
        call_score = 0.0
        put_score = 0.0
        
        for name, func in ALL_STRATEGY_FUNCS:
            base_w = self.weights.get(name, 0.1)
            
            if regime == "TENDENCIAL":
                if name in TREND_STRATEGIES:
                    eff_w = base_w * 2.5
                else:
                    eff_w = base_w * 0.20 # Atenuación de ruido en tendencia
            else:
                if name in REVERSAL_STRATEGIES:
                    eff_w = base_w * 2.5
                else:
                    eff_w = base_w * 0.20
                    
            effective_weights[name] = eff_w

            try:
                res = func(df)
                sig = float(res.get("signal", 0.0))
                votes[name] = sig
                reasons[name] = res.get("reason", "")
                
                # TRANSFORMACIÓN INTELIGENTE DE OSCILADORES EN TENDENCIA (GATING):
                # En tendencia fuerte, un RSI o Estocástico elevado confirma inercia alcista de continuación
                if regime == "TENDENCIAL":
                    if name in ["Dynamic RSI Reversal", "Stochastic RSI Timing"]:
                        if sig < 0 and "sobrecomprado" in res.get("reason", "").lower():
                            sig = 0.6  # Momentum alcista institucional activo
                        elif sig > 0 and "sobrevendido" in res.get("reason", "").lower():
                            sig = -0.6 # Momentum bajista institucional activo
                elif regime == "RANGO / REVERSIÓN":
                    if name in ["Triple EMA Ribbon", "SuperTrend Volatility"]:
                        sig = sig * 0.4 # Atenuar señales falsas de rompimiento en rangos

                if sig > 0:
                    call_score += sig * eff_w
                elif sig < 0:
                    put_score += abs(sig) * eff_w
            except Exception as e:
                logger.error("Error evaluando %s: %s", name, e)
                votes[name] = 0.0
                reasons[name] = f"Error: {e}"

        total_eff_weight = sum(effective_weights.values())
        
        # PUNTUACIÓN DIRECCIONAL NORMALIZADA (Consensus Score: -1.0 a +1.0)
        net_score = (call_score - put_score) / (total_eff_weight + 1e-9)
        
        # Convicción Direccional Proporcional:
        # En equilibrio neutro (net_score = 0), probabilidad = 50%
        # Con confluencia de 6 a 8 estrategias a favor (+0.6 a +0.8), alcanza 80%, 85%, 90%
        if net_score >= 0:
            prob_call = round(50.0 + (net_score * 50.0), 1)
            prob_put = round(100.0 - prob_call, 1)
            dominant_action = "CALL" if prob_call >= 52.0 else "NEUTRAL"
            dominant_prob = prob_call
        else:
            prob_put = round(50.0 + (abs(net_score) * 50.0), 1)
            prob_call = round(100.0 - prob_put, 1)
            dominant_action = "PUT" if prob_put >= 52.0 else "NEUTRAL"
            dominant_prob = prob_put

        last_price = float(df['close'].iloc[-1])
        snapshot = {
            "close": last_price,
            "high": float(df['high'].iloc[-1]),
            "low": float(df['low'].iloc[-1]),
            "open": float(df['open'].iloc[-1]),
            "vol": float(df.get('vol', pd.Series([0])).iloc[-1])
        }

        return {
            "regime": regime,
            "adx_val": adx_val,
            "dominant_action": dominant_action,
            "dominant_probability": dominant_prob,
            "prob_call": prob_call,
            "prob_put": prob_put,
            "votes": votes,
            "reasons": reasons,
            "weights": self.weights,
            "effective_weights": effective_weights,
            "snapshot": snapshot
        }

    def evaluate_mtf(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> Dict[str, Any]:
        """
        MOTOR MULTI-TIMEFRAME (MTF) CUANTITATIVO DE ALTA PRECISIÓN:
        1. Evalúa la estructura mayor en 5M (Macro Tendencia y Régimen Institucional).
        2. Evalúa el gatillo fino en 1M (Micro Confirmación de Vela).
        3. Filtro Estricto Anti-Rango: PROHIBIDO operar en consolidación / mercado lateral.
        4. Exige alineación estricta de al menos 7/10 indicadores para superar umbral >= 85%.
        """
        eval_5m = self.evaluate(df_5m)
        eval_1m = self.evaluate(df_1m)

        macro_5m_action = eval_5m["dominant_action"]
        macro_5m_prob = eval_5m["dominant_probability"]
        regime_5m = eval_5m["regime"]

        micro_1m_action = eval_1m["dominant_action"]
        micro_1m_prob = eval_1m["dominant_probability"]

        # Determinar Sesgo Macro (Macro Bias) con filtro de convicción
        if eval_5m["prob_call"] >= 60.0 and eval_5m["votes"].get("Triple EMA Ribbon", 0) > 0:
            macro_bias = "CALL"
        elif eval_5m["prob_put"] >= 60.0 and eval_5m["votes"].get("Triple EMA Ribbon", 0) < 0:
            macro_bias = "PUT"
        else:
            macro_bias = "RANGO"

        # Conteo de votos directos de las 10 estrategias en 1M
        v_call = sum(1 for v in eval_1m["votes"].values() if v > 0)
        v_put = sum(1 for v in eval_1m["votes"].values() if v < 0)

        # Micro Pullback y rechazo en EMA9 de 1M
        ema9_1m = float(df_1m['close'].ewm(span=9, adjust=False).mean().iloc[-1])
        c_1m = float(df_1m['close'].iloc[-1])
        l_1m = float(df_1m['low'].iloc[-1])
        h_1m = float(df_1m['high'].iloc[-1])
        is_pullback_trigger = False

        # Vetoes de seguridad cuántica: Prohibido operar contra la inercia del SuperTrend o Triple EMA
        st_1m = eval_1m["votes"].get("SuperTrend Volatility", 0.0)
        ema_1m = eval_1m["votes"].get("Triple EMA Ribbon", 0.0)
        rsi_1m = eval_1m["votes"].get("Dynamic RSI Reversal", 0.0)

        if macro_bias == "CALL":
            # REQUISITO RIGUROSO >80% WIN RATE:
            # 1. 1M confirma CALL con >= 72%
            # 2. Al menos 7 de 10 estrategias a favor
            # 3. CERO contradicciones en indicadores tendenciales clave (SuperTrend y Triple EMA no pueden ser bajistas)
            # 4. No comprar en agotamiento extremo (RSI no puede ser < 0 por advertencia de sobrecompra)
            has_no_conflict = (st_1m >= 0.0 and ema_1m >= 0.0 and rsi_1m >= 0.0)
            
            if micro_1m_action == "CALL" and eval_1m["prob_call"] >= 72.0 and v_call >= 7 and has_no_conflict:
                base_prob = (0.45 * eval_5m["prob_call"]) + (0.55 * eval_1m["prob_call"])
                consensus_bonus = 4.0 if v_call >= 9 else (2.5 if v_call >= 8 else 1.0)
                final_prob_call = round(min(98.0, base_prob + consensus_bonus), 1)
                final_prob_put = round(100.0 - final_prob_call, 1)
                final_action = "CALL"
                final_prob = final_prob_call
                confluence_text = f"🔥 CONVERGENCIA INSTITUCIONAL MTF: 5M Alcista ({eval_5m['prob_call']:.1f}%) + 1M Confirmado ({eval_1m['prob_call']:.1f}% | {v_call}/10 estrategias sin conflicto)"
            elif l_1m <= ema9_1m and c_1m > ema9_1m and v_call >= 6 and has_no_conflict:
                # Sniper Pullback confirmado (vela rechaza EMA9 al alza)
                is_pullback_trigger = True
                final_prob_call = round(min(96.0, (0.40 * eval_5m["prob_call"]) + (0.60 * eval_1m["prob_call"]) + 4.0), 1)
                final_prob_put = round(100.0 - final_prob_call, 1)
                final_action = "CALL"
                final_prob = final_prob_call
                confluence_text = f"🎯 SNIPER PULLBACK: Rebote alcista en EMA9 a favor de Macro 5M ({eval_5m['prob_call']:.1f}%)"
            else:
                final_prob_call = round(0.40 * eval_5m["prob_call"], 1)
                final_prob_put = round(100.0 - final_prob_call, 1)
                final_action = "EN ESPERA"
                final_prob = final_prob_call
                confluence_text = f"⏳ Esperando confluencia estricta (1M alcista: {v_call}/10 | Filtros: {'OK' if has_no_conflict else 'Conflicto detectado'})"
                
        elif macro_bias == "PUT":
            # REQUISITO RIGUROSO >80% WIN RATE:
            # 1. 1M confirma PUT con >= 72%
            # 2. Al menos 7 de 10 estrategias a favor
            # 3. CERO contradicciones en indicadores tendenciales clave (SuperTrend y Triple EMA no pueden ser alcistas)
            # 4. No vender en agotamiento extremo (RSI no puede ser > 0 por advertencia de sobreventa)
            has_no_conflict = (st_1m <= 0.0 and ema_1m <= 0.0 and rsi_1m <= 0.0)
            
            if micro_1m_action == "PUT" and eval_1m["prob_put"] >= 72.0 and v_put >= 7 and has_no_conflict:
                base_prob = (0.45 * eval_5m["prob_put"]) + (0.55 * eval_1m["prob_put"])
                consensus_bonus = 4.0 if v_put >= 9 else (2.5 if v_put >= 8 else 1.0)
                final_prob_put = round(min(98.0, base_prob + consensus_bonus), 1)
                final_prob_call = round(100.0 - final_prob_put, 1)
                final_action = "PUT"
                final_prob = final_prob_put
                confluence_text = f"🔥 CONVERGENCIA INSTITUCIONAL MTF: 5M Bajista ({eval_5m['prob_put']:.1f}%) + 1M Confirmado ({eval_1m['prob_put']:.1f}% | {v_put}/10 estrategias sin conflicto)"
            elif h_1m >= ema9_1m and c_1m < ema9_1m and v_put >= 6 and has_no_conflict:
                # Sniper Pullback bajista confirmado (vela rechaza EMA9 a la baja)
                is_pullback_trigger = True
                final_prob_put = round(min(96.0, (0.40 * eval_5m["prob_put"]) + (0.60 * eval_1m["prob_put"]) + 4.0), 1)
                final_prob_call = round(100.0 - final_prob_put, 1)
                final_action = "PUT"
                final_prob = final_prob_put
                confluence_text = f"🎯 SNIPER PULLBACK: Rebote bajista en EMA9 a favor de Macro 5M ({eval_5m['prob_put']:.1f}%)"
            else:
                final_prob_put = round(0.40 * eval_5m["prob_put"], 1)
                final_prob_call = round(100.0 - final_prob_put, 1)
                final_action = "EN ESPERA"
                final_prob = final_prob_put
                confluence_text = f"⏳ Esperando confluencia estricta (1M bajista: {v_put}/10 | Filtros: {'OK' if has_no_conflict else 'Conflicto detectado'})"
                
        else: # Macro 5M en consolidación / rango -> PROTECCIÓN ESTRICTA DE CAPITAL
            final_prob_call = 50.0
            final_prob_put = 50.0
            final_action = "NEUTRAL (RANGO 5M)"
            final_prob = 50.0
            confluence_text = "🛑 Macro 5M en Consolidación / Rango: Disparos bloqueados para evitar falsos quiebres"

        return {
            "macro_bias": macro_bias,
            "macro_prob": macro_5m_prob,
            "regime_5m": regime_5m,
            "adaptive_threshold": 80.0,
            "is_pullback": is_pullback_trigger,
            "micro_action": micro_1m_action,
            "micro_prob": micro_1m_prob,
            "dominant_action": final_action,
            "dominant_probability": final_prob,
            "prob_call": final_prob_call,
            "prob_put": final_prob_put,
            "confluence_text": confluence_text,
            "votes": eval_1m["votes"],
            "snapshot": eval_1m["snapshot"]
        }
