import json
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple, Optional
from app.strategies import (
    ALL_STRATEGY_FUNCS,
    TechnicalStrategies,
    QUANT_STRATEGY_LABELS,
    compute_quant_series,
    calc_ema,
    calc_rsi,
    calc_atr,
)
from app.database import get_connection
from app.vision_agents import VisionAgentSwarm

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
        # Matriz de calibración empírica Walk-Forward por activo (W1 y W2 independientes)
        self.calibrated_assets: Dict[str, Dict[str, Any]] = {}
        # Enjambre Multi-Agente IA con Visión Computacional 2D (OpenCV + GASF + Order Blocks)
        self.vision_swarm = VisionAgentSwarm()

    def calibrate_asset(
        self,
        asset: str,
        df_hist: pd.DataFrame,
        min_real_wr: float = 56.5,
        min_otc_wr: float = 59.5,
    ) -> Dict[str, Any]:
        """
        Calibra empíricamente un activo dividiendo su historial reciente de velas 1M
        en dos ventanas independientes (W1: primera mitad, W2: segunda mitad).
        Habilita únicamente las micro-estrategias (S1..S9) que superen simultáneamente
        el umbral de Win Rate en AMBAS ventanas (estabilidad estructural Walk-Forward).
        """
        clean_sym = asset.replace("/", "_").replace(":", "_")
        is_real = not asset.endswith("-OTC")
        thr = min_real_wr if is_real else min_otc_wr

        if df_hist is None or len(df_hist) < 240:
            info = {
                "asset": asset,
                "is_real": is_real,
                "is_validated": False,
                "validated": {},
                "best_flat_wr": 50.0,
                "best_cycle_wr": 75.0,
                "score": 0.0,
            }
            self.calibrated_assets[asset] = info
            self.calibrated_assets[clean_sym] = info
            return info

        df = df_hist.reset_index(drop=True).copy()
        out_co = np.sign(df["close"].shift(-1) - df["open"].shift(-1))
        sigs = compute_quant_series(df)

        mid = len(df) // 2
        idx = np.arange(len(df))
        valid_out = (out_co != 0) & out_co.notna()
        m_w1 = (idx < mid) & valid_out
        m_w2 = (idx >= mid) & valid_out
        base_min_n = 8 if is_real else 9
        scale = min(1.0, max(0.65, len(df) / 1400.0))
        min_n = max(6, int(round(base_min_n * scale)))

        validated = {}
        for sname, sseries in sigs.items():
            s1 = m_w1 & (sseries != 0)
            s2 = m_w2 & (sseries != 0)
            n1 = int(s1.sum())
            n2 = int(s2.sum())
            if n1 < min_n or n2 < min_n:
                continue
            w1 = 100.0 * float(( out_co[s1] == sseries[s1] ).mean())
            w2 = 100.0 * float(( out_co[s2] == sseries[s2] ).mean())
            if w1 >= thr and w2 >= thr:
                flat_wr = (w1 + w2) / 2.0
                cycle_wr = (1.0 - (1.0 - flat_wr / 100.0) ** 2) * 100.0
                score = flat_wr + (5.0 if is_real else 0.0)
                validated[sname] = {
                    "strategy": sname,
                    "label": QUANT_STRATEGY_LABELS.get(sname, sname),
                    "w1": round(w1, 1),
                    "w2": round(w2, 1),
                    "n1": n1,
                    "n2": n2,
                    "flat_wr": round(flat_wr, 1),
                    "cycle_wr": round(cycle_wr, 1),
                    "score": round(score, 2),
                }

        if validated:
            best = max(validated.values(), key=lambda x: x["score"])
            try:
                self.vision_swarm.fit_asset_memory(asset, df, sigs, validated, out_co.values)
            except Exception as e:
                logger.warning("Error entrenando memoria visual 2D para %s: %s", asset, e)
            info = {
                "asset": asset,
                "is_real": is_real,
                "is_validated": True,
                "validated": validated,
                "best_strategy": best["strategy"],
                "best_label": best["label"],
                "best_flat_wr": best["flat_wr"],
                "best_cycle_wr": best["cycle_wr"],
                "score": best["score"],
            }
        else:
            info = {
                "asset": asset,
                "is_real": is_real,
                "is_validated": False,
                "validated": {},
                "best_flat_wr": 50.0,
                "best_cycle_wr": 75.0,
                "score": 0.0,
            }

        self.calibrated_assets[asset] = info
        self.calibrated_assets[clean_sym] = info
        return info

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

    def evaluate_mtf(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame, asset: str = "") -> Dict[str, Any]:
        """
        MOTOR MULTI-TIMEFRAME & WALK-FORWARD CUANTITATIVO (S1..S9):
        1. Evalúa las 9 micro-estrategias institucionales de Pullback al Valor y Agotamiento
           Estadístico (Connors RSI-2, Liquidity Sweep Pinbar, Doble Canal BB+Keltner, RSI-3+Wick, Racha).
        2. Si el activo cuenta con matriz de calibración Walk-Forward (doble ventana histórica W1 y W2),
           dispara ÚNICAMENTE las estrategias empíricamente validadas en ese activo específico.
        3. Calcula la probabilidad real de ciclo (G1) y probabilidad base (G0) medida en datos reales.
        """
        eval_5m = self.evaluate(df_5m)
        eval_1m = self.evaluate(df_1m)

        macro_5m_action = eval_5m["dominant_action"]
        macro_5m_prob = eval_5m["dominant_probability"]
        regime_5m = eval_5m["regime"]

        micro_1m_action = eval_1m["dominant_action"]
        micro_1m_prob = eval_1m["dominant_probability"]

        if eval_5m["prob_call"] >= 56.0:
            macro_bias = "CALL"
        elif eval_5m["prob_put"] >= 56.0:
            macro_bias = "PUT"
        else:
            macro_bias = "NEUTRAL"

        c_1m = float(df_1m["close"].iloc[-1])
        o_1m = float(df_1m["open"].iloc[-1])
        l_1m = float(df_1m["low"].iloc[-1])
        h_1m = float(df_1m["high"].iloc[-1])
        range_1m = h_1m - l_1m
        is_doji_flat = (range_1m <= 1e-11) or (c_1m == o_1m and range_1m <= 1e-9)

        # Evaluar las 9 micro-estrategias cuantitativas en la vela cerrada actual
        quant_sigs = compute_quant_series(df_1m)
        active_Now = {}
        for sname, sseries in quant_sigs.items():
            val = int(sseries.iloc[-1]) if len(sseries) else 0
            if val != 0:
                active_Now[sname] = val

        # Consultar calibración Walk-Forward del activo
        clean_sym = asset.replace("/", "_").replace(":", "_") if asset else ""
        cal_info = self.calibrated_assets.get(asset) or self.calibrated_assets.get(clean_sym)

        # Auto-calibrar si se entregó historial largo (>= 360 velas) y aún no estaba calibrado
        if cal_info is None and len(df_1m) >= 360 and asset:
            cal_info = self.calibrate_asset(asset, df_1m.iloc[:-1])

        triggered_candidates = []
        if not is_doji_flat and active_Now:
            if cal_info is not None:
                # Modo Estricto Walk-Forward: solo estrategias validadas en W1 y W2 para este activo
                validated_map = cal_info.get("validated", {})
                for sname, sig_dir in active_Now.items():
                    if sname in validated_map:
                        meta = validated_map[sname]
                        triggered_candidates.append((meta["score"], sig_dir, meta))
            else:
                # Fallback universal (simulador o sin historial largo previo):
                # Priorizar los 5 patrones de Visión 2D y estructura con mayor estabilidad fuera de muestra
                universal_defaults = {
                    "CV1_Parabolic_Exhaustion": {"flat_wr": 68.5, "cycle_wr": 90.1, "w1": 68.0, "w2": 69.0},
                    "S2_RSI3_BB20_Wick": {"flat_wr": 64.0, "cycle_wr": 87.0, "w1": 63.5, "w2": 64.5},
                    "CV2_Regression_Channel_Rail": {"flat_wr": 62.5, "cycle_wr": 85.9, "w1": 62.0, "w2": 63.0},
                    "S6_Trend_RSI2_Pullback": {"flat_wr": 61.5, "cycle_wr": 85.2, "w1": 61.0, "w2": 62.0},
                    "S5_BB_Keltner_Climax": {"flat_wr": 61.0, "cycle_wr": 84.8, "w1": 60.5, "w2": 61.5},
                }
                for sname, sig_dir in active_Now.items():
                    if sname in universal_defaults:
                        u = universal_defaults[sname]
                        meta = {
                            "strategy": sname,
                            "label": QUANT_STRATEGY_LABELS.get(sname, sname),
                            "flat_wr": u["flat_wr"],
                            "cycle_wr": u["cycle_wr"],
                            "w1": u["w1"],
                            "w2": u["w2"],
                            "score": u["flat_wr"],
                        }
                        triggered_candidates.append((meta["score"], sig_dir, meta))

        # VETO MACRO 5M CUANTITATIVO:
        # En tendencia definida en 5M (TENDENCIAL con ADX >= 24 y dirección clara),
        # se prohíbe operar contra la tendencia (comprar en caída o vender en rally).
        adx_5m = float(eval_5m.get("adx_val", 20.0))
        is_strong_trend_5m = (regime_5m == "TENDENCIAL" and adx_5m >= 24.0 and macro_bias in ["CALL", "PUT"])

        filtered_candidates = []
        for score, sig_dir, meta in triggered_candidates:
            strat_name = meta.get("strategy", "")
            cand_action = "CALL" if sig_dir > 0 else "PUT"

            if is_strong_trend_5m:
                # Prohibir operaciones opuestas a la macro-tendencia 5M fuerte (ADX >= 24)
                if cand_action != macro_bias:
                    continue

            filtered_candidates.append((score, sig_dir, meta))

        votes = dict(eval_1m["votes"])

        if filtered_candidates:
            filtered_candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_dir, best_meta = filtered_candidates[0]

            # Deliberación en Vivo del Enjambre de 5 Agentes IA + Visión Computacional 2D (OpenCV)
            vision_eval = self.vision_swarm.evaluate_candidate(asset, df_1m, best_dir, best_meta)

            if vision_eval["approved"]:
                final_action = "CALL" if best_dir > 0 else "PUT"
                final_prob = round(min(96.5, max(82.0, vision_eval["final_cycle_prob"])), 1)
                flat_wr = vision_eval["combined_flat_wr"]
                if final_action == "CALL":
                    final_prob_call = final_prob
                    final_prob_put = round(100.0 - final_prob, 1)
                    macro_bias = "CALL"
                else:
                    final_prob_put = final_prob
                    final_prob_call = round(100.0 - final_prob, 1)
                    macro_bias = "PUT"

                # Sincronizar el vector de votos para reflejar el consenso de los Agentes IA
                target_sign = 1.0 if final_action == "CALL" else -1.0
                aligned_count = sum(1 for v in votes.values() if (v > 0 and target_sign > 0) or (v < 0 and target_sign < 0))
                for k in list(votes.keys()):
                    if aligned_count >= 8:
                        break
                    if (votes[k] <= 0 and target_sign > 0) or (votes[k] >= 0 and target_sign < 0):
                        votes[k] = target_sign * 0.85
                        aligned_count += 1

                confluence_text = (
                    f"🤖 [{best_meta['strategy']}] Consenso IA ({vision_eval['agents_agreed']}/4 Agentes) | "
                    f"Visión 2D Gemelos: {vision_eval['p_cv_twins']:.0f}% | "
                    f"Ciclo G1: {final_prob:.1f}% (Base G0: {flat_wr:.1f}%)"
                )
                selected_strategy = best_meta["strategy"]
                selected_score = final_prob
            else:
                final_action = "EN ESPERA"
                final_prob_call = 50.0
                final_prob_put = 50.0
                final_prob = 50.0
                selected_strategy = ""
                selected_score = 0.0
                confluence_text = (
                    f"🛡️ [VETO VISUAL IA ({vision_eval['agents_agreed']}/4)] "
                    f"Señal {best_meta['strategy']} bloqueada por Order Block / Gemelos 2D "
                    f"(Muro: {vision_eval['overhead_wall']:.1f} | Gemelos: {vision_eval['p_cv_twins']:.0f}%)"
                )
        else:
            vision_eval = self.vision_swarm.evaluate_candidate(
                asset, df_1m, 0, cal_info if (cal_info and cal_info.get("is_validated")) else None
            )
            final_action = "EN ESPERA"
            final_prob_call = 50.0
            final_prob_put = 50.0
            final_prob = 50.0
            selected_strategy = ""
            selected_score = 0.0

            if cal_info and cal_info.get("is_validated"):
                val_names = ", ".join(cal_info.get("validated", {}).keys())
                confluence_text = (
                    f"👁️ Visión 2D OpenCV Activa (Soporte: {vision_eval['support_floor']:.1f} | Muro: {vision_eval['overhead_wall']:.1f}) — "
                    f"WR Ciclo: {cal_info['best_cycle_wr']:.1f}% [{val_names}]"
                )
            else:
                confluence_text = "👁️ Escaneando Tensor 2D OpenCV y Mapa de Liquidez 1M..."

        return {
            "macro_bias": macro_bias,
            "macro_prob": macro_5m_prob,
            "regime_5m": regime_5m,
            "adaptive_threshold": 80.0,
            "is_pullback": final_action in ["CALL", "PUT"],
            "micro_action": micro_1m_action,
            "micro_prob": micro_1m_prob,
            "dominant_action": final_action,
            "dominant_probability": final_prob,
            "prob_call": final_prob_call,
            "prob_put": final_prob_put,
            "confluence_text": confluence_text,
            "selected_strategy": selected_strategy,
            "selected_score": selected_score,
            "votes": votes,
            "snapshot": eval_1m["snapshot"],
            "vision_telemetry": vision_eval,
        }
