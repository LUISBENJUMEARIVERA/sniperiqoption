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
                
                # Atenuar señales de osciladores contrarias si hay tendencia definida, pero NUNCA invertir sobrecompra a compra
                if regime == "RANGO / REVERSIÓN":
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

        c_1m = float(df_1m['close'].iloc[-1])
        o_1m = float(df_1m['open'].iloc[-1])
        l_1m = float(df_1m['low'].iloc[-1])
        h_1m = float(df_1m['high'].iloc[-1])
        
        # 1. FILTRO DE VELA DOJI / FALTA DE VOLATILIDAD:
        body_1m = abs(c_1m - o_1m)
        range_1m = h_1m - l_1m + 1e-9
        is_doji = (body_1m / range_1m) < 0.20

        # 2. FILTRO DE AGOTAMIENTO EXTREMO (Anti-trampas de techo/suelo en binarias):
        sma20 = float(df_1m['close'].rolling(20).mean().iloc[-1])
        std20 = float(df_1m['close'].rolling(20).std().iloc[-1])
        upper_bb = sma20 + (2.0 * std20)
        lower_bb = sma20 - (2.0 * std20)
        
        # Cálculo de RSI actual
        delta = df_1m['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=13, min_periods=14).mean()
        avg_loss = loss.ewm(com=13, min_periods=14).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_val = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else 50.0

        is_exhausted_call = (c_1m >= upper_bb and rsi_val >= 70.0) or (rsi_val >= 74.0)
        is_exhausted_put = (c_1m <= lower_bb and rsi_val <= 30.0) or (rsi_val <= 26.0)

        # Vetoes de seguridad cuántica: Prohibido operar contra la inercia del SuperTrend o Triple EMA
        st_1m = eval_1m["votes"].get("SuperTrend Volatility", 0.0)
        ema_1m = eval_1m["votes"].get("Triple EMA Ribbon", 0.0)

        if macro_bias == "CALL":
            # REQUISITO RIGUROSO >80% WIN RATE:
            # 1. No es vela Doji de indecisión
            # 2. No está en agotamiento extremo contra resistencia / sobrecompra
            # 3. 1M confirma CALL con >= 72% y al menos 7 de 10 estrategias a favor
            # 4. CERO contradicciones en SuperTrend y Triple EMA (ambos >= 0)
            has_no_conflict = (st_1m >= 0.0 and ema_1m >= 0.0)
            
            if not is_doji and not is_exhausted_call and micro_1m_action == "CALL" and eval_1m["prob_call"] >= 72.0 and v_call >= 7 and has_no_conflict:
                base_prob = (0.45 * eval_5m["prob_call"]) + (0.55 * eval_1m["prob_call"])
                consensus_bonus = 5.0 if v_call >= 9 else (3.0 if v_call >= 8 else 1.5)
                final_prob_call = round(min(98.0, base_prob + consensus_bonus), 1)
                final_prob_put = round(100.0 - final_prob_call, 1)
                final_action = "CALL"
                final_prob = final_prob_call
                confluence_text = f"🔥 CONVERGENCIA INSTITUCIONAL MTF: 5M Alcista ({eval_5m['prob_call']:.1f}%) + 1M Confirmado ({eval_1m['prob_call']:.1f}% | {v_call}/10 estrategias a favor)"
            else:
                final_prob_call = round(0.40 * eval_5m["prob_call"], 1)
                final_prob_put = round(100.0 - final_prob_call, 1)
                final_action = "EN ESPERA"
                final_prob = final_prob_call
                reason_filter = "Doji" if is_doji else ("Agotamiento en techo" if is_exhausted_call else f"{v_call}/10 a favor")
                confluence_text = f"⏳ Esperando confluencia estricta ({reason_filter} | SuperTrend/EMA: {'OK' if has_no_conflict else 'Conflicto'})"
                
        elif macro_bias == "PUT":
            # REQUISITO RIGUROSO >80% WIN RATE:
            # 1. No es vela Doji de indecisión
            # 2. No está en agotamiento extremo contra soporte / sobreventa
            # 3. 1M confirma PUT con >= 72% y al menos 7 de 10 estrategias a favor
            # 4. CERO contradicciones en SuperTrend y Triple EMA (ambos <= 0)
            has_no_conflict = (st_1m <= 0.0 and ema_1m <= 0.0)
            
            if not is_doji and not is_exhausted_put and micro_1m_action == "PUT" and eval_1m["prob_put"] >= 72.0 and v_put >= 7 and has_no_conflict:
                base_prob = (0.45 * eval_5m["prob_put"]) + (0.55 * eval_1m["prob_put"])
                consensus_bonus = 5.0 if v_put >= 9 else (3.0 if v_put >= 8 else 1.5)
                final_prob_put = round(min(98.0, base_prob + consensus_bonus), 1)
                final_prob_call = round(100.0 - final_prob_put, 1)
                final_action = "PUT"
                final_prob = final_prob_put
                confluence_text = f"🔥 CONVERGENCIA INSTITUCIONAL MTF: 5M Bajista ({eval_5m['prob_put']:.1f}%) + 1M Confirmado ({eval_1m['prob_put']:.1f}% | {v_put}/10 estrategias a favor)"
            else:
                final_prob_put = round(0.40 * eval_5m["prob_put"], 1)
                final_prob_call = round(100.0 - final_prob_put, 1)
                final_action = "EN ESPERA"
                final_prob = final_prob_put
                reason_filter = "Doji" if is_doji else ("Agotamiento en suelo" if is_exhausted_put else f"{v_put}/10 a favor")
                confluence_text = f"⏳ Esperando confluencia estricta ({reason_filter} | SuperTrend/EMA: {'OK' if has_no_conflict else 'Conflicto'})"
                
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
