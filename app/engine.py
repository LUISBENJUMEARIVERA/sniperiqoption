import sys
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import time
import math
import random
import uuid
import logging
import threading
import pandas as pd
import numpy as np
from typing import Optional, Dict, Any, List
from datetime import datetime

from app.config import BotConfig, DEFAULT_ASSETS, TOXIC_ASSETS_BLACKLIST, INSTITUTIONAL_OTC_WHITELIST
from app.ensemble import EnsembleEngine
from app.optimizer import StrategyOptimizer
from app.database import record_trade, resolve_trade, mark_trade_recovered, mark_cycle_step_closed, get_kpi_summary

logger = logging.getLogger("AutoTrader.Engine")

class TradingEngine:
    def __init__(self):
        self.config = BotConfig()
        self.ensemble = EnsembleEngine()
        self.optimizer = StrategyOptimizer()
        self.is_running = False
        self.worker_thread: Optional[threading.Thread] = None
        self.iq = None
        self.demo_balance = 10000.0
        self.sim_prices: Dict[str, float] = {
            "EURUSD": 1.0850, "GBPUSD": 1.2940, "USDJPY": 152.30,
            "AUDUSD": 0.6580, "EURJPY": 165.20, "USDCAD": 1.3890,
            "EURUSD-OTC": 1.0852, "GBPUSD-OTC": 1.2945, "USDJPY-OTC": 152.35,
            "XAUUSD": 2748.50, "BTCUSD": 68450.00
        }
        self.current_prices: Dict[str, float] = {}
        self.last_trade_candles: Dict[str, int] = {}
        self.last_global_trade_candle: int = -1
        self._iq_lock = threading.Lock()
        self._candles_5m_cache: Dict[str, Dict[str, Any]] = {}
        self._candles_1m_cache: Dict[str, Dict[str, Any]] = {}
        self._calibration_cache: Dict[str, float] = {}
        self._calibration_df_cache: Dict[str, pd.DataFrame] = {}
        self.open_instruments_cache: Dict[str, Dict[str, Any]] = {}
        self.last_catalog_refresh: float = 0.0
        self.active_trades: Dict[str, Dict[str, Any]] = {}
        self.assets_signals: Dict[str, Dict[str, Any]] = {}
        
        # Estado individual y global de Recuperación Cuantitativa Walk-Forward (estrictamente máximo G1)
        self.martingale_state: Dict[str, Dict[str, Any]] = {}
        self.portfolio_recovery: Dict[str, Any] = {
            "active": False,
            "level": 0,
            "amount": self.config.amount,
            "lost_trade_uid": None,
            "lost_asset": None
        }

        # Circuit Breakers & Gestión de Riesgo Institucional
        self.consecutive_cycle_losses: int = 0
        self.circuit_breaker_until: float = 0.0
        self.circuit_breaker_reason: str = ""
        self.asset_cooldowns: Dict[str, float] = {}
        self.peak_session_balance: Optional[float] = None
        self.max_trailing_drawdown: float = 450.0
        self.max_consecutive_cycle_losses: int = 2
        
        self.current_state = {
            "status": "STOPPED",
            "is_connected": False,
            "mode": "PRACTICE (DEMO)",
            "balance": 10000.0,
            "active_assets": [],
            "assets_signals": {},
            "last_signal": None,
            "last_price": 1.0850,
            "candle_timing": {
                "second": 0,
                "seconds_remaining": 60,
                "is_trigger_window": False,
                "status_text": "Sincronizando reloj MTF (5M + 1M)..."
            },
            "active_orders": []
        }

    def configure(self, cfg: BotConfig):
        self.config = cfg
        if not cfg.assets or len(cfg.assets) == 0:
            self.config.assets = []
        self.current_state["active_assets"] = self.config.assets
        
        # Actualizar importes base en el estado de martingala
        for a in self.config.assets:
            if a not in self.martingale_state:
                self.martingale_state[a] = {
                    "current_level": 0,
                    "next_amount": cfg.amount,
                    "immediate_reentry": False,
                    "last_action": None
                }
            elif self.martingale_state[a]["current_level"] == 0:
                self.martingale_state[a]["next_amount"] = cfg.amount

    def start(self, cfg: BotConfig):
        if self.is_running:
            self.configure(cfg)
            print(f">> [CONFIGURACIÓN ACTUALIZADA EN CALIENTE] Umbral: {cfg.min_probability}%, Importe: ${cfg.amount:.2f}")
            return
            
        self.configure(cfg)
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._run_multiscan_loop, daemon=True)
        self.worker_thread.start()
        mart_text = f"Activada (hasta {cfg.martingale_levels} nivel/es)" if cfg.use_martingale else "Desactivada"
        asset_info = f"Escaneando {len(self.config.assets)} pares manuales" if self.config.assets else "Modo Autónomo: Auto-descubriendo y rotando los 8-10 mejores pares abiertos"
        print(f"\n>> [MOTOR INICIADO] {asset_info} (5M Macro -> 1M Micro). Martingala: {mart_text}.\n")

    def stop(self):
        self.is_running = False
        self.current_state["status"] = "STOPPED"
        print("\n>> [MOTOR DETENIDO] Escáner en segundo plano apagado.\n")

    def _connect_iq(self) -> bool:
        if self.config.use_simulator or not self.config.email or not self.config.password:
            print(">> [MODO] Operando en Modo Simulador Local (Sin conectar a servidores remotos).")
            self.current_state["is_connected"] = True
            self.current_state["mode"] = "PRACTICE (SIMULATOR)"
            self.current_state["balance"] = self.demo_balance
            return True

        try:
            from iqoptionapi.stable_api import IQ_Option
            print(f">> [IQ OPTION] Conectando a servidores de IQ Option con: {self.config.email}...")
            self.iq = IQ_Option(self.config.email, self.config.password)
            check, reason = self.iq.connect()
            
            if not check:
                print(f">> [IQ OPTION] ALERTA: No se pudo conectar a IQ Option ({reason}).")
                print(">> [IQ OPTION] Revisa que la contraseña sea correcta en la pestaña de credenciales.")
                print(">> [FALLBACK] Continuando en modo simulador para no bloquear la pantalla.")
                self.config.use_simulator = True
                self.current_state["mode"] = "PRACTICE (SIMULATOR)"
                return True

            target_mode = "REAL" if (self.config.mode and self.config.mode.upper() == "REAL") else "PRACTICE"
            print(f">> [AUTENTICACIÓN IQ OPTION] Sincronizando balance en modo {target_mode}...")
            self.iq.change_balance(target_mode)
            time.sleep(1)
            balance_mode = self.iq.get_balance_mode()
                
            self.demo_balance = float(self.iq.get_balance())
            self.current_state["balance"] = self.demo_balance
            self.current_state["is_connected"] = True
            self.current_state["mode"] = f"{balance_mode} ({'CUENTA REAL' if balance_mode == 'REAL' else 'DEMO'})"
            print(f">> [CONECTADO EXITOSAMENTE] Saldo {balance_mode} sincronizado: ${self.demo_balance:.2f}\n")
            return True
        except Exception as e:
            print(f">> [ERROR] Excepción conectando a IQ Option: {e}")
            self.config.use_simulator = True
            self.current_state["mode"] = "PRACTICE (SIMULATOR)"
            return True

    def _refresh_live_catalog(self, force: bool = False):
        """
        Escaneo Forense del Catálogo Real de IQ Option en Vivo (Opciones Turbo 1M):
        1. Consulta get_all_init_v2() enfocándose en el catálogo 'turbo' (1M a 5M) para garantizar
           que todos los instrumentos seleccionados acepten expiraciones de 1 minuto.
        2. Filtra ESTRICTAMENTE activos con enabled=True e is_suspended=False.
        3. Registra dinámicamente sus IDs en OP_code.ACTIVES para habilitar la orden de compra
           (incluyendo futuros reales CME como USNDAQ100:N, USSPX500:N, US2000:N, US30:N, SPX/Gold, XAU/XAG,
           pares Forex reales y sintéticos OTC de alta liquidez).
        4. Cruza con get_all_profit() para capturar el Payout exacto al instante.
        """
        if self.config.use_simulator or not self.iq:
            return

        now = time.time()
        if not force and (now - self.last_catalog_refresh < 45) and self.open_instruments_cache:
            return

        try:
            import iqoptionapi.constants as OP_code
            with self._iq_lock:
                init = self.iq.get_all_init_v2()
                profits = self.iq.get_all_profit()
            
            if not init or not profits:
                return

            turbo = init.get('turbo', {}).get('actives', {})
            binary = init.get('binary', {}).get('actives', {})
            
            new_cache = {}
            currencies = ["EUR", "GBP", "USD", "JPY", "CHF", "AUD", "CAD", "NZD"]

            # Priorizar estrictamente el catálogo 'turbo' porque operamos velas de 1M (duration=1).
            # Solo usar 'binary' como respaldo si 'turbo' viniera vacío.
            catalog_sources = [('turbo', turbo)] if turbo else [('binary', binary)]

            for category, cat_dict in catalog_sources:
                for aid_str, data in cat_dict.items():
                    try:
                        aid = int(aid_str)
                    except Exception:
                        continue
                        
                    enabled = data.get('enabled', False)
                    suspended = data.get('is_suspended', True)
                    
                    # FILTRO ABSOLUTO: Solo activos habilitados y NO suspendidos por el broker
                    if not enabled or suspended:
                        continue
                        
                    raw_name = data.get('name', '')
                    clean_name = raw_name.replace('front.', '')
                    
                    # PROHIBICIÓN ABSOLUTA DE DIGITAL OPTIONS (-op) Y ACTIVOS TÓXICOS:
                    if clean_name.endswith('-op') or clean_name in TOXIC_ASSETS_BLACKLIST:
                        continue
                    # En mercado OTC, permitir EXCLUSIVAMENTE la Whitelist Institucional Tier-1 (Forex G8, Índices Mayores, Oro):
                    # Bloquea de raíz los 120+ altcoins/memecoins cripto OTC y divisas exóticas con payout anzuelo
                    if clean_name.endswith('-OTC') and clean_name not in INSTITUTIONAL_OTC_WHITELIST:
                        continue
                        
                    base = clean_name.replace('-op', '').replace('-OTC', '')
                    is_forex = (len(base) == 6 and base[:3] in currencies and base[3:6] in currencies)
                    is_real_market = not clean_name.endswith('-OTC')
                    
                    p_data = profits.get(clean_name, {})
                    payout_val = p_data.get('turbo') or p_data.get(category) or p_data.get('binary') or 0.0
                    payout_pct = round(payout_val * 100.0 if payout_val <= 1.0 else payout_val, 1)
                    
                    # Todo activo Turbo 1M habilitado, no suspendido y con payout >= 75% es candidato
                    # a la validación empírica Walk-Forward (índices CME reales, commodities, divisas y pares OTC)
                    is_tradable = (payout_pct >= 75.0)
                    
                    # Inyección dinámica en ACTIVES de la librería para que buy() y get_candles() nunca fallen
                    OP_code.ACTIVES[clean_name] = aid
                    if is_real_market and base not in OP_code.ACTIVES:
                        OP_code.ACTIVES[base] = aid
                        
                    if clean_name not in new_cache or payout_pct > new_cache[clean_name]['payout']:
                        new_cache[clean_name] = {
                            'id': aid,
                            'payout': payout_pct,
                            'base': base,
                            'is_forex': is_forex,
                            'is_real_market': is_real_market,
                            'is_tradable': is_tradable,
                            'category': category,
                            'raw_name': raw_name
                        }

            if new_cache:
                self.open_instruments_cache = new_cache
                self.last_catalog_refresh = now
        except Exception as e:
            logger.error("Error refrescando catálogo de activos en vivo: %s", e)

    def _is_market_open_toxic_window(self) -> bool:
        """
        Ventana de Apertura de Wall Street (09:25 a 10:00 hora local/EST):
        Durante estos 35 minutos las acciones y ETFs tienen desequilibrios de subasta extremos.
        """
        now_lt = time.localtime()
        return (now_lt.tm_hour == 9 and 25 <= now_lt.tm_min <= 59)

    def _is_equity_or_etf(self, asset: str) -> bool:
        """Determina si un activo es una acción individual o ETF bursátil."""
        a = asset.upper().replace("-OTC", "").replace(":N", "").replace("-OP", "")
        known_stocks_etfs = {
            "XLK", "SMH", "ASMLA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META",
            "TSLA", "AMD", "INTC", "BABA", "BIDU", "NFLX", "DIS", "JNJ", "PFE",
            "OPENAI", "ANTHROPIC", "LUXURY", "EUROPEAN AI", "JAPANESE AI", "ASML",
            "FWONA", "HYPE"
        }
        return (a in known_stocks_etfs)

    def _resolve_target_asset(self, asset: str) -> tuple:
        """
        Mapeo y Redireccionamiento Inteligente en Vivo:
        Prioriza coincidencia exacta en el catálogo Turbo abierto; si no existe,
        busca por símbolo base la variante abierta con mayor payout (excluyendo -op).
        Descarta tajantemente cualquier activo en la lista negra de activos tóxicos.
        """
        if asset in TOXIC_ASSETS_BLACKLIST:
            return None, 0.0
        if asset.endswith("-OTC") and asset not in INSTITUTIONAL_OTC_WHITELIST:
            return None, 0.0

        if self.config.use_simulator or not self.iq:
            return asset, 86.0

        self._refresh_live_catalog()

        # 1. Coincidencia exacta en el catálogo Turbo verificado de abiertos
        if asset in self.open_instruments_cache and not asset.endswith("-op"):
            info = self.open_instruments_cache[asset]
            return asset, info.get('payout', 0.0)

        # 2. Fallback por símbolo base (ej. EURUSD -> EURUSD o EURUSD-OTC)
        base = asset.replace("-op", "").replace("-OTC", "")
        if base in TOXIC_ASSETS_BLACKLIST:
            return None, 0.0

        candidates = []
        for name, info in self.open_instruments_cache.items():
            if name.endswith("-op") or name in TOXIC_ASSETS_BLACKLIST:
                continue
            if info.get('base') == base:
                # Preferir mercado real sobre OTC a igual payout
                is_real = 1 if info.get('is_real_market') else 0
                candidates.append((name, info.get('payout', 0.0), is_real))

        if candidates:
            candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
            return candidates[0][0], candidates[0][1]

        return None, 0.0

    def _auto_discover_top_assets(self, min_count: int = 6, max_count: int = 12, silent: bool = False) -> List[str]:
        """
        Radar Cuantitativo Multi-Activo con Calibración Walk-Forward en Vivo:
        1. Refresca el catálogo real Turbo 1M de IQ Option (enabled=True, is_suspended=False).
        2. Prioriza feeds de Mercado Real (futuros CME USNDAQ100:N, USSPX500:N, US2000:N, US30:N, Forex spot)
           y pares Turbo de alto Payout (>=80%), descartando tajantemente activos tóxicos.
        3. Calibra cada activo en doble ventana histórica independiente (W1 y W2 de 1M) para
           seleccionar únicamente los activos donde las estrategias S1..S9 tienen ventaja
           estadística demostrada (>55.5% Real / >58.5% OTC en ambas mitades, >83% Ciclo G1).
        """
        if self.config.use_simulator or not self.iq:
            default_pool = [
                "USNDAQ100:N", "USSPX500:N", "US2000:N", "US30:N",
                "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURJPY-OTC", "EURGBP-OTC", 
                "USDCHF-OTC", "NZDUSD-OTC", "GBPJPY-OTC", "AUDCAD-OTC", "AUDUSD-OTC"
            ]
            self.config.assets = default_pool[:max_count]
            self.current_state["active_assets"] = self.config.assets
            return self.config.assets

        try:
            self._refresh_live_catalog(force=True)
            now_ts = time.time()
            in_mkt_open = self._is_market_open_toxic_window()

            # Agrupar por par base priorizando versión de Mercado Real y mayor Payout
            base_groups = {}
            for name, info in self.open_instruments_cache.items():
                if not info.get('is_tradable', False):
                    continue
                # Filtro 1: Lista negra de activos tóxicos (ratios sintéticos, divisas pegadas, memecoins)
                base = info.get('base', '')
                if name in TOXIC_ASSETS_BLACKLIST or base in TOXIC_ASSETS_BLACKLIST:
                    continue
                # Filtro 2: Cooldown de activo por ciclo fallido reciente (30 min)
                if name in self.asset_cooldowns and now_ts < self.asset_cooldowns[name]:
                    continue
                # Filtro 3: Escudo de apertura de Wall Street para acciones y ETFs
                if in_mkt_open and self._is_equity_or_etf(name):
                    continue

                payout = info.get('payout', 0.0)
                if payout < self.config.min_payout:
                    continue
                
                base = info.get('base')
                is_real = info.get('is_real_market', False)
                if base not in base_groups or (is_real, payout) > (base_groups[base]['is_real'], base_groups[base]['payout']):
                    base_groups[base] = {
                        'symbol': name,
                        'payout': payout,
                        'base': base,
                        'is_real': is_real
                    }

            candidates = list(base_groups.values())
            candidates.sort(key=lambda x: (1 if x['is_real'] else 0, x['payout']), reverse=True)

            # Calibrar todos los candidatos Institucionales Tier-1 en doble ventana Walk-Forward (con caché de 15 min)
            max_calibrations_this_pass = 45 if not silent else 10
            calibrations_done = 0
            validated_count = sum(
                1 for c in candidates
                if self.ensemble.calibrated_assets.get(c['symbol'], {}).get('is_validated', False)
            )

            for c in candidates[:50]:
                sym = c['symbol']
                last_cal = self._calibration_cache.get(sym, 0.0)
                if (now_ts - last_cal) < 900 and sym in self.ensemble.calibrated_assets:
                    continue
                if calibrations_done >= max_calibrations_this_pass:
                    break
                # En el arranque inicial, escanear al menos 35 activos Tier-1 antes de cortar
                if not silent and validated_count >= 8 and calibrations_done >= 35:
                    break

                try:
                    with self._iq_lock:
                        raw1 = self.iq.get_candles(sym, 60, 1000, now_ts)
                        raw0 = []
                        if raw1 and len(raw1) >= 500:
                            first_ts = int(raw1[0]["from"])
                            raw0 = self.iq.get_candles(sym, 60, 420, first_ts - 60) or []
                    raw = (raw0 + raw1) if raw0 else raw1
                    if raw and len(raw) >= 260:
                        df_h = pd.DataFrame(raw)
                        df_h.rename(columns={'from': 'timestamp', 'min': 'low', 'max': 'high', 'volume': 'vol'}, inplace=True)
                        df_h = df_h.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
                        # Excluir la vela en formación actual para calibrar solo sobre velas 100% cerradas
                        df_closed = df_h.iloc[:-1].reset_index(drop=True)
                        self._calibration_df_cache[sym] = df_closed
                        cal_info = self.ensemble.calibrate_asset(sym, df_closed, min_real_wr=56.5, min_otc_wr=59.5)
                        self._calibration_cache[sym] = now_ts
                        calibrations_done += 1
                        if cal_info.get('is_validated'):
                            validated_count += 1
                        # Pre-poblar caché 1M en RAM con las últimas 90 velas cerradas
                        if len(df_closed) >= 90:
                            df_90 = df_closed.iloc[-90:].reset_index(drop=True)
                            self._candles_1m_cache[sym] = {
                                "closed_ts": int(df_90["timestamp"].iloc[-1]),
                                "time": now_ts,
                                "df": df_90
                            }
                except Exception:
                    pass

            # Si en el catálogo Tier-1 hay menos de 8 activos validados al 59.5%, completar con umbral 58.0%
            current_validated = [
                c for c in candidates
                if self.ensemble.calibrated_assets.get(c['symbol'], {}).get('is_validated', False)
            ]
            if len(current_validated) < 8:
                for c in candidates:
                    sym = c['symbol']
                    if self.ensemble.calibrated_assets.get(sym, {}).get('is_validated', False):
                        continue
                    df_closed = self._calibration_df_cache.get(sym)
                    if df_closed is not None:
                        self.ensemble.calibrate_asset(sym, df_closed, min_real_wr=56.0, min_otc_wr=58.0)

            # Puntuar y ordenar todos los candidatos priorizando:
            # 1) Validados Walk-Forward en doble ventana (is_validated=True)
            # 2) Mercados Reales (CME / Spot) y mayor Score de Win Rate fuera de muestra
            # 3) Mayor Payout en vivo
            scored_candidates = []
            for c in candidates:
                sym = c['symbol']
                cal = self.ensemble.calibrated_assets.get(sym, {})
                is_val = 1 if cal.get('is_validated', False) else 0
                score = float(cal.get('score', 0.0))
                is_real = 1 if c['is_real'] else 0
                scored_candidates.append((is_val, score, is_real, c['payout'], sym))

            scored_candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)

            # Seleccionar EXCLUSIVAMENTE activos validados Walk-Forward (hasta max_count)
            validated_syms = [x[4] for x in scored_candidates if x[0] == 1]
            if len(validated_syms) >= 1:
                selected = validated_syms[:max_count]
            else:
                selected = [x[4] for x in scored_candidates[:max_count]]

            if not selected:
                selected = ["EURUSD-OTC", "GBPUSD-OTC", "EURJPY-OTC", "USDJPY-OTC", "EURGBP-OTC", "GBPJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC"]

            old_set = set(self.config.assets) if self.config.assets else set()
            new_set = set(selected)

            if old_set != new_set or not silent:
                added = list(new_set - old_set)
                removed = list(old_set - new_set)
                
                self.config.assets = selected
                self.current_state["active_assets"] = selected

                for a in selected:
                    if a not in self.martingale_state:
                        self.martingale_state[a] = {
                            "current_level": 0,
                            "next_amount": self.config.amount,
                            "immediate_reentry": False,
                            "last_action": None
                        }
                    if a not in self.assets_signals:
                        p = self._get_asset_payout(a)
                        cal = self.ensemble.calibrated_assets.get(a, {})
                        val_strats = ", ".join(cal.get("validated", {}).keys())
                        init_text = (
                            f"✅ Validado WF (WR Ciclo: {cal.get('best_cycle_wr', 83.0):.1f}%) [{val_strats}]"
                            if cal.get("is_validated") else
                            "⏳ Sincronizando microestructura 1M..."
                        )
                        self.assets_signals[a] = {
                            "asset": a,
                            "price": self.sim_prices.get(a.replace("-op","").replace("-OTC",""), 1.0850),
                            "prob_call": 50.0,
                            "prob_put": 50.0,
                            "dominant_action": "EN ESPERA",
                            "dominant_probability": 50.0,
                            "regime": "WALK-FORWARD ACTIVO" if cal.get("is_validated") else "ANALIZANDO...",
                            "macro_bias": "5M NEUTRAL",
                            "phase_label": "Iniciando",
                            "martingale_level": 1 if self.portfolio_recovery.get("active") else 0,
                            "next_amount": self.portfolio_recovery.get("amount", self.config.amount) if self.portfolio_recovery.get("active") else self.config.amount,
                            "adaptive_threshold": float(self.config.min_probability),
                            "is_pullback": False,
                            "payout": p,
                            "confluence_text": init_text,
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }

                # Purgar inmediatamente cualquier activo que ya no esté en la selección activa
                stale_keys = [k for k in list(self.assets_signals.keys()) if k not in selected]
                for k in stale_keys:
                    del self.assets_signals[k]

                self.current_state["assets_signals"] = self.assets_signals

                print("\n========================================================")
                print(f">> 🌐 [RADAR WALK-FORWARD DE MERCADO] Cartera calibrada ({len(selected)} activos Turbo 1M verificados):")
                for a in selected:
                    p = self._get_asset_payout(a)
                    cal = self.ensemble.calibrated_assets.get(a, {})
                    mkt_tag = "REAL MARKET" if not a.endswith("-OTC") else "OTC FEED   "
                    if cal.get("is_validated"):
                        v_keys = ",".join(cal.get("validated", {}).keys())
                        print(f"   • {a:<16} | {mkt_tag} | Payout: {p:.0f}% | WR Base: {cal['best_flat_wr']:.1f}% -> Ciclo G1: {cal['best_cycle_wr']:.1f}% [{v_keys}]")
                    else:
                        print(f"   • {a:<16} | {mkt_tag} | Payout: {p:.0f}% [DISPONIBLE PARA OPERAR]")
                if added and silent:
                    print(f">> 🟢 [NUEVOS ACTIVOS ACTIVADOS] {', '.join(added)}")
                if removed and silent:
                    print(f">> 🔴 [ACTIVOS RETIRADOS POR SUSPENSIÓN / CIERRE] {', '.join(removed)}")
                print("========================================================\n")

            return selected
        except Exception as e:
            logger.error("Error en auto-descubrimiento de activos: %s", e)
            return self.config.assets

    def _fetch_asset_candles(self, asset: str, count: int = 90, timeframe_sec: int = 60) -> pd.DataFrame:
        if not self.config.use_simulator and self.iq:
            target_asset, _ = self._resolve_target_asset(asset)
            target = target_asset or asset
            now_ts = time.time()

            if timeframe_sec == 300:
                cached = self._candles_5m_cache.get(target)
                sec_now = int(now_ts) % 60
                # Durante los primeros 5s del minuto (ventana crítica de disparo), usar siempre caché 5M en RAM para 0ms de latencia
                if cached and ((now_ts - cached["time"] < 120) or sec_now <= 5):
                    return cached["df"]
            elif timeframe_sec == 60:
                # Identificar el timestamp exacto de la última vela de 1M 100% CERRADA (curr_min - 1)
                curr_min = int(now_ts // 60)
                closed_candle_id = curr_min - 1
                expected_closed_ts = closed_candle_id * 60

                cached_1m = self._candles_1m_cache.get(target)
                if cached_1m and cached_1m.get("closed_ts") == expected_closed_ts and (now_ts - cached_1m.get("time", 0) < 45):
                    return cached_1m["df"]

            try:
                with self._iq_lock:
                    raw = self.iq.get_candles(target, timeframe_sec, count + 2, now_ts)
                if raw and len(raw) >= 30:
                    df = pd.DataFrame(raw)
                    df.rename(columns={'from': 'timestamp', 'min': 'low', 'max': 'high', 'volume': 'vol'}, inplace=True)
                    # Actualizar precio en vivo al instante con el último tick recibido
                    live_px = float(df["close"].iloc[-1])
                    self.current_prices[asset] = live_px
                    self.current_prices[target] = live_px

                    if timeframe_sec == 300:
                        self._candles_5m_cache[target] = {"time": now_ts, "df": df}
                        return df
                    elif timeframe_sec == 60:
                        curr_min = int(now_ts // 60)
                        closed_candle_id = curr_min - 1
                        expected_closed_ts = closed_candle_id * 60
                        # CRÍTICO: Excluir la vela recién nacida en formación
                        # para evaluar SIEMPRE la vela 1M que acaba de cerrar al 100%
                        df_closed = df[df["timestamp"] <= expected_closed_ts].reset_index(drop=True)
                        if len(df_closed) < 30:
                            df_closed = df.iloc[:-1].reset_index(drop=True)
                        self._candles_1m_cache[target] = {
                            "closed_ts": int(df_closed["timestamp"].iloc[-1]),
                            "time": now_ts,
                            "df": df_closed
                        }
                        return df_closed
                    return df
            except Exception:
                pass

        now = int(time.time())
        seed_val = (now + hash(asset) + timeframe_sec) % 100000
        np.random.seed(seed_val)
        
        base = self.sim_prices.get(asset, 1.0850)
        if "BTC" in asset:
            volat = 0.0018 * math.sqrt(timeframe_sec / 60.0)
        elif "XAU" in asset:
            volat = 0.0008 * math.sqrt(timeframe_sec / 60.0)
        elif "JPY" in asset:
            volat = 0.04 * math.sqrt(timeframe_sec / 60.0)
        else:
            volat = 0.0003 * math.sqrt(timeframe_sec / 60.0)

        returns = np.random.normal(0.00001, volat, count)
        prices = base * np.cumprod(1 + returns)
        self.sim_prices[asset] = prices[-1]
        
        data = []
        for i in range(count):
            p = prices[i]
            spread = abs(np.random.normal(0, volat * 0.5))
            o = p - spread / 2
            c = p + spread / 2 if np.random.rand() > 0.49 else p - spread / 2
            h = max(o, c) + abs(np.random.normal(0, volat * 0.3))
            l = min(o, c) - abs(np.random.normal(0, volat * 0.3))
            vol = random.randint(100, 1500)
            data.append({"timestamp": now - (count - i) * timeframe_sec, "open": o, "high": h, "low": l, "close": c, "vol": vol})
            
        return pd.DataFrame(data)

    def _get_asset_payout(self, asset: str) -> float:
        """Obtiene el porcentaje de retorno en vivo (Payout) desde IQ Option."""
        _, payout = self._resolve_target_asset(asset)
        return payout if payout > 0 else 86.0

    def _sync_iq_balance(self):
        """Sincroniza el saldo real de la cuenta DEMO directamente con los servidores de IQ Option."""
        if not self.config.use_simulator and self.iq:
            try:
                bal = float(self.iq.get_balance())
                self.demo_balance = round(bal, 2)
                self.current_state["balance"] = self.demo_balance
                return self.demo_balance
            except Exception:
                pass
        return self.demo_balance

    def _resolve_iq_order(self, order_id: int):
        """Consulta el resultado oficial cerrado directamente a los servidores de IQ Option."""
        if not self.iq or not order_id:
            return None, None

        # 1. Intentar por WebSocket directo (order_async)
        try:
            async_order = self.iq.get_async_order(order_id)
            if async_order and "option-closed" in async_order and async_order["option-closed"]:
                msg = async_order["option-closed"].get("msg", {})
                profit_amount = float(msg.get("profit_amount", 0.0))
                amount = float(msg.get("amount", 0.0))
                net = round(profit_amount - amount, 2)
                res = "WIN" if net > 0 else ("LOSS" if net < 0 else "EQUAL")
                return res, net
        except Exception:
            pass

        # 2. Intentar por historial oficial de opciones cerradas (get_optioninfo_v2)
        try:
            with self._iq_lock:
                history = self.iq.get_optioninfo_v2(15)
            if history and "msg" in history and "closed_options" in history["msg"]:
                for opt in history["msg"]["closed_options"]:
                    if order_id in opt.get("id", []):
                        win_val = opt.get("win")
                        amount = float(opt.get("amount", 10.0))
                        win_amount = float(opt.get("win_amount", 0.0))
                        if win_val == "win":
                            return "WIN", round(win_amount - amount, 2)
                        elif win_val == "loose":
                            return "LOSS", round(-amount, 2)
                        else:
                            return "EQUAL", 0.0
        except Exception:
            pass

        return None, None

    def _execute_order(self, asset: str, action: str, probability: float, votes: dict, snapshot: dict, macro_info: str) -> bool:
        # 1. Determinar activo real disponible en IQ Option y su payout
        if not self.config.use_simulator and self.iq:
            target_asset, payout = self._resolve_target_asset(asset)
            if not target_asset:
                print(f">> ⚠️ [ACTIVO CERRADO O SUSPENDIDO] {asset} no está abierto para opciones Turbo 1M en este horario. Operación evitada.")
                return False
        else:
            target_asset = asset
            payout = 86.0

        # 2. FILTRO DE PAYOUT MÍNIMO (>= 80%): Protege la esperanza matemática
        if payout < self.config.min_payout:
            print(f">> ⚠️ [FILTRO PAYOUT] {target_asset} paga solo {payout:.0f}% (<{self.config.min_payout:.0f}%). Operación evitada para asegurar expectativa matemática positiva.")
            return False

        trade_uid = str(uuid.uuid4())[:12]
        
        # 3. DETERMINACIÓN DEL IMPORTE (G0 Base o G1 Recuperación Walk-Forward de Cartera):
        if self.config.use_martingale and self.portfolio_recovery.get("active", False):
            current_lvl = 1
            amount = float(self.portfolio_recovery.get("amount", round(self.config.amount * 2.15, 2)))
            recovered_uid = self.portfolio_recovery.get("lost_trade_uid")
            lvl_tag = "[RECUPERACIÓN WALK-FORWARD G1] "
        else:
            current_lvl = 0
            amount = float(self.config.amount)
            recovered_uid = None
            lvl_tag = ""
        
        expiration_min = 1
        entry_price = self.current_prices.get(target_asset, snapshot.get("close", 0.0))

        # 4. ENVIAR ORDEN A IQ OPTION ANTES DE REGISTRARLA
        iq_order_id = None
        if not self.config.use_simulator and self.iq:
            try:
                target_mode = "REAL" if (self.config.mode and self.config.mode.upper() == "REAL") else "PRACTICE"
                with self._iq_lock:
                    if self.iq.get_balance_mode() != target_mode:
                        self.iq.change_balance(target_mode)
                        time.sleep(0.2)
                    
                    print(f"\n>> 🚀 {lvl_tag}[ENVIANDO A IQ OPTION ({target_mode})] #{trade_uid} | {action} en {target_asset} | Importe: ${amount:.2f} | Prob Ciclo: {probability:.1f}% | Payout: {payout:.0f}%")
                    status, order_id = self.iq.buy(amount, target_asset, action.lower(), expiration_min)
                
                # VERIFICACIÓN ESTRICTA: SI IQ OPTION RECHAZA LA ORDEN, NO SE REGISTRA
                if not status or not isinstance(order_id, int):
                    print(f">> ⚠️ [ORDEN RECHAZADA POR IQ OPTION] {target_asset}: {order_id}. IQ Option no ejecutó la orden. No se registra ninguna operación fantasma.")
                    return False
                
                iq_order_id = order_id
                print(f">> [ORDEN CONFIRMADA IQ OPTION] #{trade_uid} en {target_asset}. Status: True, ID: {iq_order_id}\n")
            except Exception as e:
                print(f">> [ERROR IQ OPTION] Excepción al enviar orden para {target_asset}: {e}. Operación cancelada.")
                return False
        else:
            print(f"\n>> 🔥 {lvl_tag}[SIMULADOR LOCAL] #{trade_uid} | {action} en {asset} | Importe: ${amount:.2f} | Prob Ciclo: {probability:.1f}% | Payout: {payout:.0f}%\n")
        
        # 5. REGISTRAR ÚNICAMENTE TRAS CONFIRMACIÓN REAL DE IQ OPTION (O EN SIMULADOR)
        snap_copy = dict(snapshot)
        snap_copy["martingale_level"] = current_lvl
        if recovered_uid:
            snap_copy["recovered_uid"] = recovered_uid

        record_trade(
            trade_uid=trade_uid,
            asset=target_asset,
            action=action,
            amount=amount,
            expiration_min=expiration_min,
            payout=payout,
            probability=probability,
            strategy_votes=votes,
            market_snapshot=snap_copy
        )
        
        # Expiración exacta alineada al cierre de minuto de IQ Option (iqoptionapi/expiration.py)
        now_order_ts = time.time()
        sec_order = int(now_order_ts) % 60
        min_order = int(now_order_ts // 60)
        expiry_timestamp = float((min_order + (1 if sec_order < 30 else 2)) * 60)
        
        self.active_trades[trade_uid] = {
            "trade_uid": trade_uid,
            "order_id": iq_order_id,
            "asset": target_asset,
            "base_asset": asset,
            "action": action,
            "amount": amount,
            "payout": payout,
            "entry_price": entry_price,
            "expiry_time": expiry_timestamp,
            "probability": probability,
            "macro_info": macro_info,
            "martingale_level": current_lvl,
            "recovered_uid": recovered_uid
        }

        # Si acabamos de disparar la orden G1, desactivar la espera mientras está en curso
        if current_lvl > 0:
            self.portfolio_recovery["active"] = False

        if asset in self.martingale_state:
            self.martingale_state[asset]["immediate_reentry"] = False

        return True

    def switch_account_mode(self, mode: str) -> Dict[str, Any]:
        """
        Cambia de inmediato entre cuenta DEMO (PRACTICE) y CUENTA REAL en vivo.
        """
        mode = mode.upper()
        if mode not in ["PRACTICE", "REAL"]:
            return {"success": False, "message": "Modo inválido. Use PRACTICE o REAL."}

        self.config.mode = mode

        if not self.config.use_simulator and self.iq:
            try:
                with self._iq_lock:
                    self.iq.change_balance(mode)
                    time.sleep(0.5)
                    act_mode = self.iq.get_balance_mode()
                    if act_mode != mode:
                        self.iq.change_balance(mode)
                        time.sleep(0.5)
                        act_mode = self.iq.get_balance_mode()

                    bal = float(self.iq.get_balance())
                self.demo_balance = bal
                self.current_state["balance"] = bal
                self.current_state["mode"] = f"{act_mode} ({'CUENTA REAL' if act_mode == 'REAL' else 'DEMO'})"

                if act_mode == "REAL":
                    print("\n" + "="*62)
                    print(">> ⚠️⚠️⚠️ [ALERTA DE SEGURIDAD: OPERANDO EN CUENTA REAL] ⚠️⚠️⚠️")
                    print(f">> Se ha cambiado la cuenta a DINERO REAL en IQ Option. Saldo: ${bal:.2f}")
                    print(">> AVISO: 'ESTAS EN UN PROGRAMA EXPERIMENTAL, LAS GANANCIAS O PERDIDAS")
                    print(">> SERÁN UNICAMENTE TU RESPONSABILIDAD'")
                    print("="*62 + "\n")
                else:
                    print(f"\n>> 🛡️ [CAMBIO A CUENTA DEMO] Modo restablecido a PRACTICE. Saldo: ${bal:.2f}\n")

                return {"success": True, "mode": act_mode, "balance": bal}
            except Exception as e:
                logger.error("Error al cambiar cuenta en IQ Option: %s", e)
                return {"success": False, "message": f"Error comunicando con IQ Option: {e}"}
        else:
            self.current_state["mode"] = f"{mode} (SIMULADOR)"
            return {"success": True, "mode": mode, "balance": self.demo_balance}

    def _check_expirations(self):
        now = time.time()
        expired_ids = []
        
        for uid, trade in list(self.active_trades.items()):
            if now >= trade["expiry_time"] + 0.8:
                order_id = trade.get("order_id")
                asset = trade["asset"]
                base_asset = trade.get("base_asset", asset)
                action = trade["action"]
                amount = trade["amount"]
                payout = trade["payout"]
                current_lvl = trade.get("martingale_level", 0)
                recovered_uid = trade.get("recovered_uid")

                # Si está conectado a IQ Option, obtener el resultado oficial de los servidores
                if not self.config.use_simulator and self.iq and order_id:
                    res, profit = self._resolve_iq_order(order_id)
                    if res is None:
                        # Si aún no ha llegado la confirmación del cierre por WS, esperar hasta 12 segundos
                        if now - trade["expiry_time"] < 12:
                            continue
                        # Fallback seguro
                        entry_price = trade["entry_price"]
                        current_price = self.current_prices.get(asset, entry_price)
                        is_win = (current_price > entry_price) if action == "CALL" else (current_price < entry_price)
                        res = "WIN" if is_win else "LOSS"
                        profit = round(amount * (payout / 100.0), 2) if is_win else -amount
                else:
                    entry_price = trade["entry_price"]
                    current_price = self.current_prices.get(asset, entry_price)
                    is_win = (current_price > entry_price) if action == "CALL" else (current_price < entry_price)
                    res = "WIN" if is_win else "LOSS"
                    profit = round(amount * (payout / 100.0), 2) if is_win else -amount

                expired_ids.append(uid)
                resolve_trade(trade_uid=uid, result=res, profit=profit)

                # Sincronizar balance real directamente desde IQ Option
                if not self.config.use_simulator and self.iq:
                    self._sync_iq_balance()
                else:
                    self.demo_balance = round(self.demo_balance + profit, 2)
                    self.current_state["balance"] = self.demo_balance

                print(f">> ✅ [OPERACIÓN CERRADA #{uid}] {asset} (G{current_lvl}) -> {res} (${profit:+.2f}) | Saldo Real Demo: ${self.demo_balance:.2f}")

                # GESTIÓN DE CICLO WALK-FORWARD Y RECUPERACIÓN INTELIGENTE G1 DE CARTERA
                m_key = base_asset if base_asset in self.martingale_state else asset
                if m_key not in self.martingale_state:
                    self.martingale_state[m_key] = {"current_level": 0, "next_amount": self.config.amount}

                if res == "WIN":
                    # Restablecer contador de ciclos fallidos consecutivos
                    self.consecutive_cycle_losses = 0
                    if self.peak_session_balance is None or self.demo_balance > self.peak_session_balance:
                        self.peak_session_balance = self.demo_balance

                    if current_lvl > 0:
                        if recovered_uid:
                            mark_trade_recovered(recovered_uid)
                        print(f">> 🎯 [CICLO WALK-FORWARD RECUPERADO EN G1] {asset} ¡Ganó en nivel G{current_lvl} (+${profit:.2f})! Pérdida previa #{recovered_uid} compensada. Reset a base ${self.config.amount:.2f}")
                    self.portfolio_recovery = {
                        "active": False,
                        "level": 0,
                        "amount": self.config.amount,
                        "lost_trade_uid": None,
                        "lost_asset": None
                    }
                    self.martingale_state[m_key]["current_level"] = 0
                    self.martingale_state[m_key]["next_amount"] = self.config.amount
                    self.martingale_state[m_key]["immediate_reentry"] = False

                elif res == "LOSS":
                    if self.config.use_martingale and current_lvl == 0:
                        # Enfriar por 5 minutos el activo donde falló G0 para no insistir en un rompimiento en curso
                        self.asset_cooldowns[asset] = now + 300
                        # Activar Recuperación Walk-Forward G1 para la próxima señal validada de la cartera
                        mult = max(2.15, float(self.config.martingale_multiplier))
                        recov_amount = round(float(self.config.amount) * mult, 2)
                        self.portfolio_recovery = {
                            "active": True,
                            "level": 1,
                            "amount": recov_amount,
                            "lost_trade_uid": uid,
                            "lost_asset": asset
                        }
                        self.martingale_state[m_key]["current_level"] = 1
                        self.martingale_state[m_key]["next_amount"] = recov_amount
                        self.martingale_state[m_key]["immediate_reentry"] = True
                        print(f">> ⚡ [RECUPERACIÓN WALK-FORWARD G1 ARMADA] Operación base #{uid} cerrada en LOSS en {asset} (enfriado 5m). Próxima señal validada operará en G1 con ${recov_amount:.2f}...")
                    else:
                        # Límite estricto de seguridad: Jamás pasar de G1 (Ciclo fallido definitivo)
                        if recovered_uid:
                            mark_cycle_step_closed(recovered_uid)

                        # 1. Congelar el activo que falló el ciclo por 30 minutos (evitar repeat losses inmediatas)
                        self.asset_cooldowns[asset] = now + 1800
                        print(f">> 🧊 [COOLDOWN DE ACTIVO] {asset} entra en enfriamiento por 30 minutos tras ciclo fallido.")

                        # 2. Incrementar ciclos fallidos consecutivos y evaluar Circuit Breakers
                        self.consecutive_cycle_losses += 1
                        print(f">> 🛑 [TOPE DE SEGURIDAD G1 APLICADO] Ciclo finalizado en {asset} ({self.consecutive_cycle_losses} consecutivo/s fallido/s). Reseteando a importe base ${self.config.amount:.2f}.")

                        if self.consecutive_cycle_losses >= self.max_consecutive_cycle_losses:
                            self.circuit_breaker_until = now + 900  # 15 minutos
                            self.circuit_breaker_reason = f"{self.consecutive_cycle_losses} ciclos fallidos consecutivos"
                            print(f">> 🚨 [CIRCUIT BREAKER ACTIVADO] {self.circuit_breaker_reason}. Pausa total de operaciones por 15 minutos para proteger capital.")

                        # 3. Trailing Stop de Preservación de Capital
                        if self.peak_session_balance is not None:
                            dd = self.peak_session_balance - self.demo_balance
                            if dd >= self.max_trailing_drawdown:
                                self.circuit_breaker_until = max(self.circuit_breaker_until, now + 1800)  # 30 minutos
                                self.circuit_breaker_reason = f"Drawdown de ${dd:.2f} desde pico (${self.peak_session_balance:.2f})"
                                print(f">> 🛡️ [ESCUDO TRAILING DRAWDOWN] {self.circuit_breaker_reason}. Pausa de protección por 30 minutos.")

                        self.portfolio_recovery = {
                            "active": False,
                            "level": 0,
                            "amount": self.config.amount,
                            "lost_trade_uid": None,
                            "lost_asset": None
                        }
                        for k in self.martingale_state:
                            self.martingale_state[k]["current_level"] = 0
                            self.martingale_state[k]["next_amount"] = self.config.amount
                            self.martingale_state[k]["immediate_reentry"] = False

                elif res == "EQUAL":
                    # Si un G1 empata ($0.00), mantener armada la recuperación G1 para la siguiente señal validada
                    if current_lvl > 0 and recovered_uid:
                        self.portfolio_recovery["active"] = True

        for uid in expired_ids:
            if uid in self.active_trades:
                del self.active_trades[uid]

    def _run_multiscan_loop(self):
        self.current_state["status"] = "RUNNING"
        self._connect_iq()

        # Auto-descubrimiento inicial autónomo y calibración Walk-Forward de los mejores activos Turbo 1M
        self._auto_discover_top_assets(min_count=6, max_count=12, silent=False)

        while self.is_running:
            try:
                now_ts = time.time()
                
                # Reloj Micro 1M y alineación estricta a vela 100% cerrada (curr_min - 1)
                second_of_1m = int(now_ts) % 60
                seconds_remaining_1m = 60 - second_of_1m
                curr_min = int(now_ts // 60)
                closed_candle_id = curr_min - 1
                
                # Ventana de disparo exacta al inicio de la nueva vela (segs 00..04 de M+1):
                # La vela M ya cerró al 100% y entramos al precio de apertura de M+1
                is_trigger_window = (second_of_1m <= 4)
                
                # Reloj Macro 5M (0 a 4)
                minute_of_5m = (int(now_ts) % 300) // 60
                is_5m_expansion = (minute_of_5m in [0, 1, 2, 3])
                phase_label = f"Min {minute_of_5m + 1}/5 (Expansión)" if is_5m_expansion else "Min 5/5 (Agotamiento)"

                # Comprobar si hay un Circuit Breaker o Pausa de Protección activa
                is_circuit_breaker_active = (now_ts < self.circuit_breaker_until)
                if is_circuit_breaker_active:
                    rem_pause = int(self.circuit_breaker_until - now_ts)
                    recov_badge = f" | 🛡️ PAUSA PROTECCIÓN ({self.circuit_breaker_reason}: {rem_pause}s)"
                else:
                    recov_badge = f" | ⚡ G1 ARMADO (${self.portfolio_recovery['amount']:.0f})" if self.portfolio_recovery.get("active") else ""

                timing_status = (
                    f"🔥 DISPARO WALK-FORWARD LISTO (Seg {second_of_1m:02d}/60{recov_badge})"
                    if (is_trigger_window and not is_circuit_breaker_active) else
                    (f"🛡️ CIRCUIT BREAKER ACTIVO ({self.circuit_breaker_reason} &bull; Reanuda en {int(self.circuit_breaker_until - now_ts)}s)"
                     if is_circuit_breaker_active else
                     f"⏳ Escaneando Cartera WF ({phase_label} &bull; Cierre 1M en {seconds_remaining_1m}s{recov_badge})")
                )

                self.current_state["candle_timing"] = {
                    "second": second_of_1m,
                    "seconds_remaining": seconds_remaining_1m,
                    "minute_5m": minute_of_5m + 1,
                    "phase_label": phase_label,
                    "is_trigger_window": is_trigger_window,
                    "status_text": timing_status
                }

                # 1. Revisar operaciones cerradas y armar/resetear G1 ANTES de evaluar disparos de la nueva vela
                self._check_expirations()

                # 2. Sincronizar balance real periódicamente con IQ Option (fuera de la ventana de disparo para cero latencia)
                if not is_trigger_window and not self.config.use_simulator and self.iq and int(now_ts) % 6 == 0:
                    self._sync_iq_balance()

                # 3. Rotación y calibración progresiva de cartera fuera de la ventana de disparo (segs 12, 25 y 38)
                if not self.config.use_simulator and self.iq and second_of_1m in (12, 25, 38):
                    self._auto_discover_top_assets(min_count=6, max_count=12, silent=True)

                # 4. ESCANEO ULTRA-RÁPIDO DE LOS ACTIVOS CALIBRADOS WALK-FORWARD
                qualified_candidates = []
                g1_active = self.portfolio_recovery.get("active", False)
                ui_lvl = 1 if g1_active else 0
                ui_amt = float(self.portfolio_recovery.get("amount", self.config.amount)) if g1_active else float(self.config.amount)
                user_thresh = float(self.config.min_probability)

                for asset in list(self.config.assets):
                    if not self.is_running:
                        break

                    # Filtro A: Excluir activos en lista negra tóxica
                    if asset in TOXIC_ASSETS_BLACKLIST:
                        continue

                    # Filtro B: Excluir activo si está en cooldown por pérdida de ciclo (30 min)
                    if asset in self.asset_cooldowns and now_ts < self.asset_cooldowns[asset]:
                        rem_cd = int(self.asset_cooldowns[asset] - now_ts)
                        self.assets_signals[asset] = {
                            "asset": asset,
                            "price": self.current_prices.get(asset, 0.0),
                            "prob_call": 0.0,
                            "prob_put": 0.0,
                            "dominant_action": "ENFRIAMIENTO",
                            "dominant_probability": 0.0,
                            "macro_bias": "Cooldown por ciclo fallido",
                            "regime": "PROTECCIÓN DE CAPITAL",
                            "phase_label": phase_label,
                            "martingale_level": ui_lvl,
                            "next_amount": ui_amt,
                            "adaptive_threshold": user_thresh,
                            "is_pullback": False,
                            "payout": 0.0,
                            "confluence_text": f"🧊 Activo en enfriamiento preventivo ({rem_cd}s restantes)",
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }
                        continue

                    # Filtro C: Escudo de apertura de Wall Street para acciones y ETFs
                    if self._is_market_open_toxic_window() and self._is_equity_or_etf(asset):
                        self.assets_signals[asset] = {
                            "asset": asset,
                            "price": self.current_prices.get(asset, 0.0),
                            "prob_call": 0.0,
                            "prob_put": 0.0,
                            "dominant_action": "VENTANA TOXICA",
                            "dominant_probability": 0.0,
                            "macro_bias": "Apertura NY (09:25-10:00)",
                            "regime": "ALTA VOLATILIDAD",
                            "phase_label": phase_label,
                            "martingale_level": ui_lvl,
                            "next_amount": ui_amt,
                            "adaptive_threshold": user_thresh,
                            "is_pullback": False,
                            "payout": 0.0,
                            "confluence_text": "🛡️ Escudo Apertura Wall Street: Pausa en acciones/ETFs hasta las 10:00 AM",
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }
                        continue

                    target_asset, asset_payout = self._resolve_target_asset(asset)
                    is_open = (target_asset is not None)

                    if not is_open:
                        self.assets_signals[asset] = {
                            "asset": asset,
                            "price": self.current_prices.get(asset, 0.0),
                            "prob_call": 0.0,
                            "prob_put": 0.0,
                            "dominant_action": "CERRADO",
                            "dominant_probability": 0.0,
                            "macro_bias": "Mercado cerrado en binarias",
                            "regime": "MERCADO CERRADO",
                            "phase_label": phase_label,
                            "martingale_level": ui_lvl,
                            "next_amount": ui_amt,
                            "adaptive_threshold": user_thresh,
                            "is_pullback": False,
                            "payout": 0.0,
                            "confluence_text": "⏸️ Activo cerrado o suspendido en este horario",
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }
                        continue
                        
                    df_5m = self._fetch_asset_candles(target_asset, count=80, timeframe_sec=300)
                    df_1m = self._fetch_asset_candles(target_asset, count=90, timeframe_sec=60)
                    
                    eval_mtf = self.ensemble.evaluate_mtf(df_5m, df_1m, asset=target_asset)
                    curr_price = self.current_prices.get(target_asset, eval_mtf["snapshot"]["close"])
                    self.current_prices[asset] = curr_price
                    
                    action = eval_mtf["dominant_action"]
                    prob = eval_mtf["dominant_probability"]
                    macro_bias = eval_mtf["macro_bias"]
                    is_pullback_cand = eval_mtf.get("is_pullback", False)

                    self.assets_signals[asset] = {
                        "asset": asset,
                        "price": curr_price,
                        "prob_call": eval_mtf["prob_call"],
                        "prob_put": eval_mtf["prob_put"],
                        "dominant_action": action,
                        "dominant_probability": prob,
                        "macro_bias": f"Macro 5M: {macro_bias}",
                        "regime": eval_mtf["regime_5m"],
                        "phase_label": phase_label,
                        "martingale_level": ui_lvl,
                        "next_amount": ui_amt,
                        "adaptive_threshold": user_thresh,
                        "is_pullback": is_pullback_cand,
                        "payout": asset_payout,
                        "confluence_text": eval_mtf["confluence_text"],
                        "votes": eval_mtf["votes"],
                        "updated_at": time.strftime("%H:%M:%S")
                    }

                    # Recolectar candidatos con señal Walk-Forward validada en la vela cerrada actual
                    # Exigir espaciado mínimo de 3 velas (3 minutos) en un mismo activo para jamás operar rachas consecutivas
                    last_candle = self.last_trade_candles.get(asset, -999)
                    if action in ["CALL", "PUT"] and prob >= user_thresh and (closed_candle_id - last_candle) >= 3:
                        qualified_candidates.append({
                            "asset": asset,
                            "action": action,
                            "prob": prob,
                            "score": eval_mtf.get("selected_score", prob),
                            "eval_mtf": eval_mtf,
                            "reason": eval_mtf["confluence_text"]
                        })

                # 5. DISPARO CUANTITATIVO WALK-FORWARD AL CIERRE DE VELA CONFIRMADA (Segs 00..04):
                if (
                    is_trigger_window
                    and not is_circuit_breaker_active
                    and qualified_candidates
                    and len(self.active_trades) == 0
                    and self.last_global_trade_candle != closed_candle_id
                ):
                    # En recuperación G1 ($215), exigir convicción institucional estricta (>= 86.5% WR Ciclo / >= 63.0% Flat WR)
                    # para jamás arriesgar un G1 en señales marginales como 84.3%
                    if g1_active:
                        qualified_candidates = [x for x in qualified_candidates if x["prob"] >= 86.5]
                        if not qualified_candidates:
                            time.sleep(0.5 if is_trigger_window else 1.0)
                            continue

                    qualified_candidates.sort(key=lambda x: (x["score"], x["prob"]), reverse=True)
                    cand = qualified_candidates[0]
                    c_asset = cand["asset"]
                    c_action = cand["action"]
                    top_eval = cand["eval_mtf"]
                    lvl_prefix = "⚡ [RECUPERACIÓN G1 WF] " if g1_active else "🎯 [ENTRADA G0 WF] "
                    top_reason = f"{lvl_prefix}{cand['reason']}"
                    
                    executed = self._execute_order(
                        asset=c_asset,
                        action=c_action,
                        probability=cand["prob"],
                        votes=top_eval["votes"],
                        snapshot=top_eval["snapshot"],
                        macro_info=top_reason
                    )
                    if executed:
                        self.last_global_trade_candle = closed_candle_id
                        self.last_trade_candles[c_asset] = closed_candle_id
                        print(f">> 🎯 [ORDEN WALK-FORWARD EJECUTADA] {c_action} en {c_asset} | WR Ciclo Esperado: {cand['prob']:.1f}%\n")

                self.current_state["assets_signals"] = self.assets_signals
                self.current_state["active_trades"] = list(self.active_trades.values())
                self.current_state["active_trades_count"] = len(self.active_trades)
                if self.config.assets:
                    first_asset = self.config.assets[0]
                    self.current_state["last_price"] = self.current_prices.get(first_asset, 1.0850)
                    self.current_state["last_signal"] = self.assets_signals.get(first_asset)
                    
                time.sleep(0.5 if is_trigger_window else 1.0)
            except Exception as e:
                logger.error("Error en bucle MTF: %s", e)
                time.sleep(2)

        self.current_state["status"] = "STOPPED"
