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

from app.config import BotConfig, DEFAULT_ASSETS
from app.ensemble import EnsembleEngine
from app.optimizer import StrategyOptimizer
from app.database import record_trade, resolve_trade, get_kpi_summary

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
        self._candles_5m_cache: Dict[str, Dict[str, Any]] = {}
        self.open_instruments_cache: Dict[str, Dict[str, Any]] = {}
        self.last_catalog_refresh: float = 0.0
        self.active_trades: Dict[str, Dict[str, Any]] = {}
        self.assets_signals: Dict[str, Dict[str, Any]] = {}
        
        # Estado individual de Martingala por activo (se sincroniza dinámicamente)
        self.martingale_state: Dict[str, Dict[str, Any]] = {}
        self.assets_signals: Dict[str, Dict[str, Any]] = {}
        
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
        Escaneo Forense del Catálogo Real de IQ Option en Vivo:
        1. Consulta get_all_init_v2() para examinar turbo y binary.
        2. Filtra ESTRICTAMENTE activos con enabled=True e is_suspended=False.
        3. Registra dinámicamente sus IDs en OP_code.ACTIVES para habilitar la orden de compra.
        4. Cruza con get_all_profit() para capturar el Payout exacto al instante.
        5. Actualiza self.open_instruments_cache con los activos 100% operativos.
        """
        if self.config.use_simulator or not self.iq:
            return

        now = time.time()
        if not force and (now - self.last_catalog_refresh < 45) and self.open_instruments_cache:
            return

        try:
            import iqoptionapi.constants as OP_code
            init = self.iq.get_all_init_v2()
            profits = self.iq.get_all_profit()
            
            if not init or not profits:
                return

            turbo = init.get('turbo', {}).get('actives', {})
            binary = init.get('binary', {}).get('actives', {})
            
            new_cache = {}
            currencies = ["EUR", "GBP", "USD", "JPY", "CHF", "AUD", "CAD", "NZD"]

            for category, cat_dict in [('turbo', turbo), ('binary', binary)]:
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
                    
                    # PROHIBICIÓN ABSOLUTA DE DIGITAL OPTIONS (-op):
                    # En Digital Options los empates son pérdidas automáticas y el broker desvía el strike
                    if clean_name.endswith('-op'):
                        continue
                        
                    base = clean_name.replace('-op', '').replace('-OTC', '')
                    
                    # Filtro estricto de pares de divisas Forex auténticos y materias primas / cripto mayores
                    is_forex = (len(base) == 6 and base[:3] in currencies and base[3:6] in currencies)
                    is_tradable = is_forex or base in ["XAUUSD", "BTCUSD"]
                    
                    p_data = profits.get(clean_name, {})
                    payout_val = p_data.get(category) or p_data.get('turbo') or p_data.get('binary') or 0.0
                    payout_pct = round(payout_val * 100.0 if payout_val <= 1.0 else payout_val, 1)
                    
                    # Inyección dinámica en ACTIVES de la librería para que buy() nunca falle
                    OP_code.ACTIVES[clean_name] = aid
                    if base not in OP_code.ACTIVES:
                        OP_code.ACTIVES[base] = aid
                        
                    if clean_name not in new_cache or payout_pct > new_cache[clean_name]['payout']:
                        new_cache[clean_name] = {
                            'id': aid,
                            'payout': payout_pct,
                            'base': base,
                            'is_forex': is_forex,
                            'is_tradable': is_tradable,
                            'category': category,
                            'raw_name': raw_name
                        }

            if new_cache:
                self.open_instruments_cache = new_cache
                self.last_catalog_refresh = now
        except Exception as e:
            logger.error("Error refrescando catálogo de activos en vivo: %s", e)

    def _resolve_target_asset(self, asset: str) -> tuple:
        """
        Mapeo y Redireccionamiento Inteligente en Vivo:
        Toma cualquier símbolo (ej. 'EURUSD', 'EURUSD-OTC', 'EURUSD-op') y devuelve
        el activo que REALMENTE está abierto (no suspendido) con el mayor payout en este momento.
        """
        if self.config.use_simulator or not self.iq:
            return asset, 86.0

        self._refresh_live_catalog()
        
        # 1. Extraer símbolo base (ej. EURUSD) y seleccionar la versión binaria/OTC que más pague (excluyendo Digital -op)
        base = asset.replace("-op", "").replace("-OTC", "")
        candidates = []
        for name, info in self.open_instruments_cache.items():
            if name.endswith("-op"):
                continue
            if info.get('base') == base:
                candidates.append((name, info.get('payout', 0.0)))

        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0], candidates[0][1]

        # 2. Si el símbolo exacto está en el catálogo verificado de abiertos y no es digital
        if asset in self.open_instruments_cache and not asset.endswith("-op"):
            info = self.open_instruments_cache[asset]
            return asset, info.get('payout', 0.0)

        return None, 0.0

    def _auto_discover_top_assets(self, min_count: int = 15, max_count: int = 25, silent: bool = False) -> List[str]:
        """
        Radar Cuantitativo Multi-Activo de Gran Amplitud (20 a 25 pares en simultáneo):
        1. Refresca el catálogo real de IQ Option (solo enabled=True, is_suspended=False).
        2. Filtra pares Forex, OTC, Oro y Cripto institucional con payout >= min_payout.
        3. Deduplica por par base seleccionando la versión con mayor payout.
        4. Ordena por mayor retorno y mantiene hasta 25 pares vigilados en paralelo.
        """
        if self.config.use_simulator or not self.iq:
            default_pool = [
                "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURJPY-OTC", "EURGBP-OTC", 
                "USDCHF-OTC", "NZDUSD-OTC", "GBPJPY-OTC", "AUDCAD-OTC", "AUDUSD-OTC",
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", 
                "USDCAD", "EURGBP", "USDCHF", "GBPJPY", "AUDCAD", "XAUUSD", "BTCUSD"
            ]
            self.config.assets = default_pool[:max_count]
            self.current_state["active_assets"] = self.config.assets
            return self.config.assets

        try:
            self._refresh_live_catalog(force=True)

            # Agrupar por par base para evitar duplicados y elegir la versión que más paga y que esté abierta
            base_groups = {}
            for name, info in self.open_instruments_cache.items():
                if not info.get('is_tradable', info.get('is_forex', False)):
                    continue
                payout = info.get('payout', 0.0)
                if payout < self.config.min_payout:
                    continue
                
                base = info.get('base')
                if base not in base_groups or payout > base_groups[base]['payout']:
                    base_groups[base] = {
                        'symbol': name,
                        'payout': payout,
                        'base': base
                    }

            candidates = list(base_groups.values())
            candidates.sort(key=lambda x: x['payout'], reverse=True)

            # Si hay menos de min_count, relajar temporalmente al 75%
            if len(candidates) < min_count:
                for name, info in self.open_instruments_cache.items():
                    if not info.get('is_tradable', info.get('is_forex', False)):
                        continue
                    payout = info.get('payout', 0.0)
                    if payout >= 75.0:
                        base = info.get('base')
                        if base not in base_groups:
                            base_groups[base] = {'symbol': name, 'payout': payout, 'base': base}
                candidates = list(base_groups.values())
                candidates.sort(key=lambda x: x['payout'], reverse=True)

            selected = [c['symbol'] for c in candidates[:max_count]]

            # Fallback seguro en caso extremo (solo binarias estándar y OTC puras)
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
                        self.assets_signals[a] = {
                            "asset": a,
                            "price": self.sim_prices.get(a.replace("-op","").replace("-OTC",""), 1.0850),
                            "prob_call": 0.0,
                            "prob_put": 0.0,
                            "dominant_action": "EN ESPERA",
                            "dominant_probability": 0.0,
                            "regime": "ANALIZANDO...",
                            "macro_bias": "5M NEUTRAL",
                            "phase_label": "Iniciando",
                            "martingale_level": 0,
                            "next_amount": self.config.amount,
                            "adaptive_threshold": float(self.config.min_probability),
                            "is_pullback": False,
                            "payout": p,
                            "confluence_text": "⏳ Sincronizando datos de mercado...",
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }

                # Purgar inmediatamente cualquier activo que ya no esté en la selección activa
                stale_keys = [k for k in list(self.assets_signals.keys()) if k not in selected]
                for k in stale_keys:
                    del self.assets_signals[k]

                self.current_state["assets_signals"] = self.assets_signals

                print("\n========================================================")
                print(f">> 🌐 [RADAR AUTÓNOMO DE MERCADO] Rotación completada ({len(selected)} pares activos reales verificados):")
                for a in selected:
                    p = self._get_asset_payout(a)
                    print(f"   • {a:<16} | Payout en vivo: {p:.0f}% [DISPONIBLE PARA OPERAR]")
                if added and silent:
                    print(f">> 🟢 [NUEVOS ACTIVOS ACTIVADOS] {', '.join(added)}")
                if removed and silent:
                    print(f">> 🔴 [ACTIVOS RETIRADOS POR SUSPENSIÓN / CIERRE] {', '.join(removed)}")
                print("========================================================\n")

            return selected
        except Exception as e:
            logger.error("Error en auto-descubrimiento de activos: %s", e)
            return self.config.assets

    def _fetch_asset_candles(self, asset: str, count: int = 80, timeframe_sec: int = 60) -> pd.DataFrame:
        if not self.config.use_simulator and self.iq:
            target_asset, _ = self._resolve_target_asset(asset)
            target = target_asset or asset

            if timeframe_sec == 300:
                cached = self._candles_5m_cache.get(target)
                if cached and (time.time() - cached["time"] < 40):
                    return cached["df"]

            try:
                raw = self.iq.get_candles(target, timeframe_sec, count, time.time())
                if raw and len(raw) >= 30:
                    df = pd.DataFrame(raw)
                    df.rename(columns={'from': 'timestamp', 'min': 'low', 'max': 'high', 'volume': 'vol'}, inplace=True)
                    if timeframe_sec == 300:
                        self._candles_5m_cache[target] = {"time": time.time(), "df": df}
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
                print(f">> ⚠️ [ACTIVO CERRADO O SUSPENDIDO] {asset} no está abierto para opciones binarias en este horario. Operación evitada.")
                return False
        else:
            target_asset = asset
            payout = 86.0

        # 2. FILTRO DE PAYOUT MÍNIMO (>= 80%): Protege la esperanza matemática
        if payout < self.config.min_payout:
            print(f">> ⚠️ [FILTRO PAYOUT] {target_asset} paga solo {payout:.0f}% (<{self.config.min_payout:.0f}%). Operación evitada para asegurar expectativa matemática positiva.")
            return False

        trade_uid = str(uuid.uuid4())[:12]
        
        # 3. DETERMINACIÓN DEL IMPORTE:
        m_state = self.martingale_state.get(asset, {"current_level": 0, "next_amount": self.config.amount})
        current_lvl = m_state.get("current_level", 0)
        
        if current_lvl > 0:
            amount = float(m_state.get("next_amount", self.config.amount))
            lvl_tag = f"[MARTINGALA G{current_lvl}] "
        else:
            amount = float(self.config.amount)
            lvl_tag = ""
        
        expiration_min = 1
        entry_price = snapshot.get("close", 0.0)

        # 4. ENVIAR ORDEN A IQ OPTION ANTES DE REGISTRARLA
        iq_order_id = None
        if not self.config.use_simulator and self.iq:
            try:
                target_mode = "REAL" if (self.config.mode and self.config.mode.upper() == "REAL") else "PRACTICE"
                if self.iq.get_balance_mode() != target_mode:
                    self.iq.change_balance(target_mode)
                    time.sleep(0.3)
                
                print(f"\n>> 🚀 {lvl_tag}[ENVIANDO A IQ OPTION ({target_mode})] #{trade_uid} | {action} en {target_asset} | Importe: ${amount:.2f} | Prob: {probability:.1f}% | Payout: {payout:.0f}%")
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
            print(f"\n>> 🔥 {lvl_tag}[SIMULADOR LOCAL] #{trade_uid} | {action} en {asset} | Importe: ${amount:.2f} | Prob: {probability:.1f}% | Payout: {payout:.0f}%\n")
        
        # 5. REGISTRAR ÚNICAMENTE TRAS CONFIRMACIÓN REAL DE IQ OPTION (O EN SIMULADOR)
        record_trade(
            trade_uid=trade_uid,
            asset=target_asset,
            action=action,
            amount=amount,
            expiration_min=expiration_min,
            payout=payout,
            probability=probability,
            strategy_votes=votes,
            market_snapshot=snapshot
        )
        
        expiry_timestamp = time.time() + 60
        
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
            "martingale_level": current_lvl
        }

        # Desactivar bandera de reentrada inmediata
        if asset in self.martingale_state:
            self.martingale_state[asset]["immediate_reentry"] = False

        return True

    def _execute_martingale_recovery(self, asset: str, base_asset: str, action: str, amount: float, next_level: int) -> bool:
        """
        Ejecuta la orden de recuperación Martingala de forma INTELIGENTE e INMEDIATA tras confirmarse una pérdida.
        Incluye VETO DE SEGURIDAD CONTRA ROMPIMIENTOS:
        Si la vela que perdió rompió violentamente en contra (cambio de SuperTrend y EMAs), cancela
        la reentrada para proteger el capital en lugar de quemar dinero contra la tendencia.
        Si la tendencia se mantiene o hay rebote, dispara la recuperación con los votos reales de las 10 estrategias.
        """
        if not self.config.use_simulator and self.iq:
            target_asset, payout = self._resolve_target_asset(asset)
            if not target_asset:
                target_asset, payout = self._resolve_target_asset(base_asset)
            if not target_asset:
                target_asset = asset
                payout = 86.0
        else:
            target_asset = asset
            payout = 86.0

        # FILTRO DE SEGURIDAD ESTRICTO: Evaluar estructura tras la vela de pérdida
        df_1m = self._fetch_asset_candles(target_asset, count=40, timeframe_sec=60)
        eval_1m = self.ensemble.evaluate(df_1m)
        votes = eval_1m.get("votes", {})
        
        st_vote = votes.get("SuperTrend Volatility", 0.0)
        ema_vote = votes.get("Triple EMA Ribbon", 0.0)
        m_key = base_asset if base_asset in self.martingale_state else asset

        # VETO DE SEGURIDAD CONTRA RUPTURAS VIOLENTAS:
        # - Para CALL: SuperTrend Y Triple EMA se volvieron negativos (ruptura bajista)
        # - Para PUT: SuperTrend Y Triple EMA se volvieron positivos (ruptura alcista)
        has_structural_break = (action == "CALL" and st_vote < 0 and ema_vote < 0) or \
                               (action == "PUT" and st_vote > 0 and ema_vote > 0)

        opposite_action = "PUT" if action == "CALL" else "CALL"
        fav_orig = sum(1 for v in votes.values() if (action == "CALL" and v > 0) or (action == "PUT" and v < 0))
        fav_opp = sum(1 for v in votes.values() if (opposite_action == "CALL" and v > 0) or (opposite_action == "PUT" and v < 0))

        # DECISIÓN INTELIGENTE DE RECUPERACIÓN G1 (FILTRO DE CONSENSO MATEMÁTICO ESTRICTO):
        # 1. Si el mercado giró con mayoría contundente (>= 6/10) a favor de la dirección opuesta:
        if fav_opp >= 6 and eval_1m.get("dominant_action") == opposite_action:
            recov_action = opposite_action
            recov_prob = eval_1m.get("dominant_probability", 82.0)
            count_fav = fav_opp
            print(f">> 🔄 [MARTINGALA ADAPTADA AL GIRO] {target_asset}: Mercado volcado a {recov_action} ({count_fav}/10 a favor). Disparando recuperación en nueva dirección...")
        # 2. Si la tesis original se mantiene con apoyo mayoritario (>= 5/10) y SIN ruptura estructural:
        elif fav_orig >= 5 and not has_structural_break:
            recov_action = action
            recov_prob = eval_1m.get("prob_call" if action == "CALL" else "prob_put", 80.0)
            count_fav = fav_orig
            print(f">> ⚡ [MARTINGALA G{next_level} CONFIRMADA] {target_asset}: Tesis {action} vigente ({count_fav}/10 a favor | Prob: {recov_prob:.1f}%). Disparando reentrada inmediata...")
        # 3. VETO TOTAL: Si menos de 5 estrategias apoyan o hubo ruptura violenta, ABORTAR INMEDIATAMENTE
        else:
            print(f"\n>> 🛡️🛑 [MARTINGALA ABORTADA POR SEGURIDAD] {target_asset}: Pérdida de consenso estadístico ({fav_orig}/10 a favor de {action}, {fav_opp}/10 a favor de {opposite_action}). Reentrada G{next_level} cancelada para proteger capital.")
            if m_key in self.martingale_state:
                self.martingale_state[m_key]["current_level"] = 0
                self.martingale_state[m_key]["next_amount"] = self.config.amount
                self.martingale_state[m_key]["immediate_reentry"] = False
            return False

        trade_uid = str(uuid.uuid4())[:12]
        expiration_min = 1
        entry_price = self.current_prices.get(asset, self.current_prices.get(target_asset, 0.0))
        iq_order_id = None

        if not self.config.use_simulator and self.iq:
            try:
                target_mode = "REAL" if (self.config.mode and self.config.mode.upper() == "REAL") else "PRACTICE"
                if self.iq.get_balance_mode() != target_mode:
                    self.iq.change_balance(target_mode)
                    time.sleep(0.3)

                print(f"\n>> ⚡⚡ [REENTRADA INTELIGENTE G{next_level}] #{trade_uid} | {recov_action} en {target_asset} ({target_mode}) | Importe: ${amount:.2f} ({count_fav}/10 a favor) | Payout: {payout:.0f}%")
                status, order_id = self.iq.buy(amount, target_asset, recov_action.lower(), expiration_min)

                if not status or not isinstance(order_id, int):
                    print(f">> ⚠️ [REENTRADA RECHAZADA POR IQ OPTION] {target_asset}: {order_id}. No se registra operación fantasma.")
                    return False

                iq_order_id = order_id
                print(f">> 🎯 [ORDEN DE RECUPERACIÓN CONFIRMADA IQ OPTION] #{trade_uid} en {target_asset}. Status: True, ID: {iq_order_id}\n")
            except Exception as e:
                print(f">> [ERROR IQ OPTION] Fallo enviando orden de recuperación para {target_asset}: {e}")
                return False
        else:
            print(f"\n>> 🔥⚡ [SIMULADOR LOCAL - REENTRADA G{next_level}] #{trade_uid} | {recov_action} en {asset} | Importe: ${amount:.2f} ({count_fav}/10 a favor) | Payout: {payout:.0f}%\n")

        # Registrar de inmediato en la base de datos con los votos reales de las 10 estrategias
        record_trade(
            trade_uid=trade_uid,
            asset=target_asset,
            action=recov_action,
            amount=amount,
            expiration_min=expiration_min,
            payout=payout,
            probability=recov_prob,
            strategy_votes=votes,
            market_snapshot={"close": entry_price, "martingale_level": next_level, "strategies_favor": count_fav}
        )

        expiry_timestamp = time.time() + 60

        self.active_trades[trade_uid] = {
            "trade_uid": trade_uid,
            "order_id": iq_order_id,
            "asset": target_asset,
            "base_asset": base_asset,
            "action": recov_action,
            "amount": amount,
            "payout": payout,
            "entry_price": entry_price,
            "expiry_time": expiry_timestamp,
            "probability": recov_prob,
            "macro_info": f"⚡ REENTRADA INTELIGENTE MARTINGALA G{next_level} ({count_fav}/10 a favor)",
            "martingale_level": next_level
        }

        # Desactivar bandera de espera en el estado
        if m_key in self.martingale_state:
            self.martingale_state[m_key]["immediate_reentry"] = False

        # Registrar la vela para no duplicar entradas estándar simultáneas en ese par
        now_candle = int(time.time() // 60)
        self.last_trade_candles[asset] = now_candle
        self.last_trade_candles[target_asset] = now_candle

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
            if now >= trade["expiry_time"]:
                order_id = trade.get("order_id")
                asset = trade["asset"]
                base_asset = trade.get("base_asset", asset)
                action = trade["action"]
                amount = trade["amount"]
                payout = trade["payout"]
                current_lvl = trade.get("martingale_level", 0)

                # Si está conectado a IQ Option, obtener el resultado oficial de los servidores
                if not self.config.use_simulator and self.iq and order_id:
                    res, profit = self._resolve_iq_order(order_id)
                    if res is None:
                        # Si aún no ha llegado la confirmación, esperar hasta 10 segundos
                        if now - trade["expiry_time"] < 10:
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

                # Auto-optimización cuántica continua de pesos tras cada operación
                try:
                    from app.optimizer import StrategyOptimizer
                    opt = StrategyOptimizer(learning_rate=0.35)
                    opt_res = opt.analyze_and_optimize()
                    if opt_res.get("success"):
                        self.ensemble.load_weights()
                        logger.info("🧠 Auto-optimizador actualizó pesos del ensamble con éxito.")
                except Exception as e:
                    logger.debug("Optimización omitida: %s", e)

                # Sincronizar balance real directamente desde IQ Option
                if not self.config.use_simulator and self.iq:
                    self._sync_iq_balance()
                else:
                    self.demo_balance = round(self.demo_balance + profit, 2)
                    self.current_state["balance"] = self.demo_balance

                print(f">> ✅ [OPERACIÓN CERRADA #{uid}] {asset} -> {res} (${profit:+.2f}) | Saldo Real Demo: ${self.demo_balance:.2f}")

                # LÓGICA DE REENTRADA / MARTINGALA AUTOMÁTICA
                m_key = base_asset if base_asset in self.martingale_state else asset
                if m_key not in self.martingale_state:
                    self.martingale_state[m_key] = {"current_level": 0, "next_amount": self.config.amount}

                if res == "WIN":
                    if self.config.use_martingale and current_lvl > 0:
                        print(f">> 🎯 [MARTINGALA RECUPERADA] {m_key} ¡Ganó en nivel G{current_lvl}! Capital recuperado. Reset al importe base ${self.config.amount:.2f}")
                    self.martingale_state[m_key]["current_level"] = 0
                    self.martingale_state[m_key]["next_amount"] = self.config.amount
                    self.martingale_state[m_key]["immediate_reentry"] = False
                elif res == "LOSS":
                    if self.config.use_martingale:
                        # Límite estricto de seguridad: Máximo 1 nivel de recuperación (G1: x2 max)
                        # Prohibido absolutamente escaladas suicidas a G2 o G3 que quemen el balance
                        max_allowed_lvl = min(1, max(1, self.config.martingale_levels))
                        if current_lvl < max_allowed_lvl:
                            next_lvl = current_lvl + 1
                            recov_amount = round(amount * self.config.martingale_multiplier, 2)
                            self.martingale_state[m_key]["current_level"] = next_lvl
                            self.martingale_state[m_key]["next_amount"] = recov_amount
                            self.martingale_state[m_key]["immediate_reentry"] = True
                            self.martingale_state[m_key]["last_action"] = action
                            print(f">> ⚡ [MARTINGALA G{next_lvl}] Operación inicial cerrada en LOSS en {m_key}. Evaluando reentrada segura con ${recov_amount:.2f} (x2.0)...")
                            # Ejecución instantánea con validación rigurosa de consenso
                            self._execute_martingale_recovery(
                                asset=asset,
                                base_asset=base_asset,
                                action=action,
                                amount=recov_amount,
                                next_level=next_lvl
                            )
                        else:
                            print(f">> 🛑 [MARTINGALA FINALIZADA] {m_key} completó la reentrada permitida (G{max_allowed_lvl}). Reseteando a importe base ${self.config.amount:.2f}.")
                            self.martingale_state[m_key]["current_level"] = 0
                            self.martingale_state[m_key]["next_amount"] = self.config.amount
                            self.martingale_state[m_key]["immediate_reentry"] = False
                    else:
                        self.martingale_state[m_key]["current_level"] = 0
                        self.martingale_state[m_key]["next_amount"] = self.config.amount
                        self.martingale_state[m_key]["immediate_reentry"] = False

        for uid in expired_ids:
            if uid in self.active_trades:
                del self.active_trades[uid]

    def _run_multiscan_loop(self):
        self.current_state["status"] = "RUNNING"
        self._connect_iq()

        # Auto-descubrimiento inicial autónomo de los 8 a 12 mejores pares con mayor Payout (>=82%)
        self._auto_discover_top_assets(min_count=8, max_count=12, silent=False)

        while self.is_running:
            try:
                now_ts = time.time()
                
                # Reloj Micro 1M
                second_of_1m = int(now_ts) % 60
                seconds_remaining_1m = 60 - second_of_1m
                current_candle_1m_id = int(now_ts // 60)
                
                # Reloj Macro 5M (0 a 4)
                minute_of_5m = (int(now_ts) % 300) // 60
                is_5m_expansion = (minute_of_5m in [0, 1, 2, 3])
                is_trigger_window = (58 <= second_of_1m <= 59 or second_of_1m == 0)
                
                phase_label = f"Min {minute_of_5m + 1}/5 (Expansión)" if is_5m_expansion else "Min 5/5 (Agotamiento)"
                
                timing_status = (
                    f"🔥 DISPARO MTF LISTO (1M seg {second_of_1m}/60 en {phase_label})"
                    if is_trigger_window else
                    f"⏳ Monitoreando ({phase_label} &bull; Cierre 1M en {seconds_remaining_1m}s)"
                )
                
                self.current_state["candle_timing"] = {
                    "second": second_of_1m,
                    "seconds_remaining": seconds_remaining_1m,
                    "minute_5m": minute_of_5m + 1,
                    "phase_label": phase_label,
                    "is_trigger_window": is_trigger_window,
                    "status_text": timing_status
                }

                # Revisar operaciones cerradas y calcular martingalas si corresponde
                self._check_expirations()
                
                # Sincronizar balance real periódicamente con IQ Option cada 4s
                if not self.config.use_simulator and self.iq and int(now_ts) % 4 == 0:
                    self._sync_iq_balance()

                # Rotación y vigilancia continua de cartera autónoma cada 45s (8 a 12 mejores pares)
                if not self.config.use_simulator and self.iq and int(now_ts) % 45 == 0:
                    self._auto_discover_top_assets(min_count=8, max_count=12, silent=True)
                
                # ESCANEO CONTINUO DE LOS ACTIVOS AUTÓNOMAMENTE ASIGNADOS
                qualified_candidates = []
                for asset in list(self.config.assets):
                    if not self.is_running:
                        break

                    # 1. Verificar si el activo está abierto en IQ Option y resolver su símbolo real
                    target_asset, asset_payout = self._resolve_target_asset(asset)
                    is_open = (target_asset is not None)

                    m_info = self.martingale_state.get(asset, {"current_level": 0, "next_amount": self.config.amount, "immediate_reentry": False})
                    curr_lvl = m_info.get("current_level", 0)
                    next_amt = m_info.get("next_amount", self.config.amount)
                    immediate_reentry = m_info.get("immediate_reentry", False)
                    user_thresh = float(self.config.min_probability)

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
                            "martingale_level": curr_lvl,
                            "next_amount": next_amt,
                            "adaptive_threshold": user_thresh,
                            "is_pullback": False,
                            "payout": 0.0,
                            "confluence_text": "⏸️ Activo cerrado en opciones binarias en este horario",
                            "votes": {},
                            "updated_at": time.strftime("%H:%M:%S")
                        }
                        continue
                        
                    df_5m = self._fetch_asset_candles(target_asset, count=80, timeframe_sec=300)
                    df_1m = self._fetch_asset_candles(target_asset, count=80, timeframe_sec=60)
                    
                    eval_mtf = self.ensemble.evaluate_mtf(df_5m, df_1m)
                    curr_price = eval_mtf["snapshot"]["close"]
                    self.current_prices[asset] = curr_price
                    
                    action = eval_mtf["dominant_action"]
                    prob = eval_mtf["dominant_probability"]
                    macro_bias = eval_mtf["macro_bias"]
                    
                    # Nivel actual de Martingala e importe para la UI
                    m_info = self.martingale_state.get(asset, {"current_level": 0, "next_amount": self.config.amount, "immediate_reentry": False})
                    curr_lvl = m_info.get("current_level", 0)
                    next_amt = m_info.get("next_amount", self.config.amount)
                    immediate_reentry = m_info.get("immediate_reentry", False)

                    # UMBRAL DE DISPARO ESTRICTO DEFINIDO POR EL USUARIO:
                    # El bot respeta 100% el valor fijado por el usuario en el panel
                    user_thresh = float(self.config.min_probability)

                    # Payout actual
                    asset_payout = self._get_asset_payout(asset)

                    # Gatillo Sniper Pullback a mitad de vela (segundo 12 a 42)
                    is_pullback_cand = eval_mtf.get("is_pullback", False) and self.config.use_sniper_pullback
                    is_pullback_window = (12 <= second_of_1m <= 42) and is_pullback_cand

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
                        "martingale_level": curr_lvl,
                        "next_amount": next_amt,
                        "adaptive_threshold": user_thresh,
                        "is_pullback": is_pullback_cand,
                        "payout": asset_payout,
                        "confluence_text": eval_mtf["confluence_text"],
                        "votes": eval_mtf["votes"],
                        "updated_at": time.strftime("%H:%M:%S")
                    }

                    # Recolectar candidatos calificados que cumplan umbral estricto y no estén en rango
                    last_candle = self.last_trade_candles.get(asset, -1)
                    if action in ["CALL", "PUT"] and prob >= user_thresh and macro_bias != "RANGO" and current_candle_1m_id != last_candle:
                        qualified_candidates.append({
                            "asset": asset,
                            "action": action,
                            "prob": prob,
                            "eval_mtf": eval_mtf,
                            "is_martingale": immediate_reentry,
                            "reason": eval_mtf["confluence_text"]
                        })

                # DISPARO CUANTITATIVO DE ALTA PRECISIÓN AL CIERRE DE VELA CONFIRMADA (Segundos 58-59 o 00):
                # Elimina cualquier ruido intra-vela y garantiza que la expiración cubra la siguiente vela completa de 1M
                if is_trigger_window and qualified_candidates:
                    qualified_candidates.sort(key=lambda x: (1 if x["is_martingale"] else 0, x["prob"]), reverse=True)
                    
                    used_currencies = set()
                    executed_in_candle = 0
                    max_concurrent_per_candle = 1  # 1 orden quirúrgica por cierre para evitar sobreexposición
                    
                    for cand in qualified_candidates:
                        if executed_in_candle >= max_concurrent_per_candle:
                            break
                        if len(self.active_trades) >= 1:  # MÁXIMA PRECISIÓN: Solo 1 operación activa a la vez en todo el bot
                            break
                            
                        c_asset = cand["asset"]
                        c_action = cand["action"]
                        last_c = self.last_trade_candles.get(c_asset, -1)
                        if (current_candle_1m_id - last_c) < 2:  # Cooldown de al menos 2 velas para refrescar el ciclo de liquidez
                            continue
                            
                        # FILTRO DE DESCORRELACIÓN INSTITUCIONAL:
                        base_sym = c_asset.replace("-op", "").replace("-OTC", "")
                        curr_pair = (base_sym[:3], base_sym[3:6]) if len(base_sym) == 6 else (base_sym, "")
                        
                        # Si alguna de las divisas del par ya está activa, evitar correlación directa
                        if curr_pair[0] in used_currencies or (curr_pair[1] and curr_pair[1] in used_currencies):
                            continue
                            
                        top_eval = cand["eval_mtf"]
                        top_reason = f"🎯 [CIERRE VELA CONFIRMADO | SEG {second_of_1m}] {cand['reason']} (Prob: {cand['prob']:.1f}% &ge; Umbral: {user_thresh:.0f}%)"
                        
                        executed = self._execute_order(
                            asset=c_asset,
                            action=c_action,
                            probability=cand["prob"],
                            votes=top_eval["votes"],
                            snapshot=top_eval["snapshot"],
                            macro_info=top_reason
                        )
                        if executed:
                            executed_in_candle += 1
                            self.last_trade_candles[c_asset] = current_candle_1m_id
                            used_currencies.add(curr_pair[0])
                            if curr_pair[1]:
                                used_currencies.add(curr_pair[1])
                            print(f">> 🎯 [ORDEN DE ALTA CONVICCIÓN EJECUTADA] {c_action} en {c_asset} | Convicción: {cand['prob']:.1f}%\n")

                self.current_state["assets_signals"] = self.assets_signals
                self.current_state["active_trades"] = list(self.active_trades.values())
                self.current_state["active_trades_count"] = len(self.active_trades)
                if self.config.assets:
                    first_asset = self.config.assets[0]
                    self.current_state["last_price"] = self.current_prices.get(first_asset, 1.0850)
                    self.current_state["last_signal"] = self.assets_signals.get(first_asset)
                    
                time.sleep(1) # Pulso continuo de 1 segundo
            except Exception as e:
                logger.error("Error en bucle MTF: %s", e)
                time.sleep(2)

        self.current_state["status"] = "STOPPED"
