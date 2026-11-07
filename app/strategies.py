import numpy as np
import pandas as pd
from typing import Dict, Any, List

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

class TechnicalStrategies:
    @staticmethod
    def triple_ema_ribbon(df: pd.DataFrame) -> Dict[str, Any]:
        """1. Triple EMA Ribbon (9, 21, 55): Tendencia macro y micro."""
        ema9 = calc_ema(df['close'], 9).iloc[-1]
        ema21 = calc_ema(df['close'], 21).iloc[-1]
        ema55 = calc_ema(df['close'], 55).iloc[-1]
        close = df['close'].iloc[-1]
        
        if close > ema9 and ema9 > ema21 and ema21 > ema55:
            return {"signal": 1.0, "reason": "Alineación alcista fuerte (Close > EMA9 > EMA21 > EMA55)"}
        elif close < ema9 and ema9 < ema21 and ema21 < ema55:
            return {"signal": -1.0, "reason": "Alineación bajista fuerte (Close < EMA9 < EMA21 < EMA55)"}
        return {"signal": 0.0, "reason": "EMAs en cruce o consolidación"}

    @staticmethod
    def dynamic_rsi_reversal(df: pd.DataFrame) -> Dict[str, Any]:
        """2. RSI Dinámico (14): Reversión desde extremos o confirmación de momentum saludable."""
        rsi = calc_rsi(df['close'], 14)
        curr_rsi = rsi.iloc[-1]
        prev_rsi = rsi.iloc[-2]
        
        # 1. Reversión confirmada desde extremos
        if curr_rsi < 35 and curr_rsi > prev_rsi:
            return {"signal": 1.0, "reason": f"RSI sobrevendido rebotando al alza ({curr_rsi:.1f} > {prev_rsi:.1f})"}
        elif curr_rsi > 65 and curr_rsi < prev_rsi:
            return {"signal": -1.0, "reason": f"RSI sobrecomprado rechazando a la baja ({curr_rsi:.1f} < {prev_rsi:.1f})"}
        
        # 2. Zona de agotamiento extremo (PELIGRO: Prohibido perseguir precio sobre-extendido)
        elif curr_rsi >= 75.0:
            return {"signal": -0.4, "reason": f"RSI en agotamiento extremo alcista ({curr_rsi:.1f} >= 75) - Riesgo de retroceso"}
        elif curr_rsi <= 25.0:
            return {"signal": 0.4, "reason": f"RSI en agotamiento extremo bajista ({curr_rsi:.1f} <= 25) - Riesgo de rebote"}

        # 3. Momentum direccional con espacio de recorrido
        elif 54.0 <= curr_rsi <= 68.0 and curr_rsi > prev_rsi:
            return {"signal": 0.8, "reason": f"RSI en expansión alcista con inercia ({curr_rsi:.1f})"}
        elif 32.0 <= curr_rsi <= 46.0 and curr_rsi < prev_rsi:
            return {"signal": -0.8, "reason": f"RSI en presión bajista con inercia ({curr_rsi:.1f})"}
            
        return {"signal": 0.0, "reason": f"RSI en equilibrio neutro ({curr_rsi:.1f})"}

    @staticmethod
    def macd_momentum_cross(df: pd.DataFrame) -> Dict[str, Any]:
        """3. MACD Momentum Cross (12, 26, 9): Cruces e inercia de histograma."""
        ema12 = calc_ema(df['close'], 12)
        ema26 = calc_ema(df['close'], 26)
        macd_line = ema12 - ema26
        signal_line = calc_ema(macd_line, 9)
        hist = macd_line - signal_line
        
        curr_hist = hist.iloc[-1]
        prev_hist = hist.iloc[-2]
        curr_m = macd_line.iloc[-1]
        curr_s = signal_line.iloc[-1]
        
        if curr_hist > 0 and prev_hist <= 0:
            return {"signal": 1.0, "reason": "Cruce alcista reciente del MACD sobre señal"}
        elif curr_hist < 0 and prev_hist >= 0:
            return {"signal": -1.0, "reason": "Cruce bajista reciente del MACD bajo señal"}
        elif curr_m > curr_s and curr_hist > prev_hist and curr_hist > 0:
            return {"signal": 0.8, "reason": "MACD acelerando momentum alcista"}
        elif curr_m < curr_s and curr_hist < prev_hist and curr_hist < 0:
            return {"signal": -0.8, "reason": "MACD acelerando momentum bajista"}
        return {"signal": 0.0, "reason": "MACD en compresión o desaceleración"}

    @staticmethod
    def bollinger_squeeze_piercing(df: pd.DataFrame) -> Dict[str, Any]:
        """4. Bollinger Bands (20, 2): Rechazo en bandas o expansión sostenida."""
        sma20 = df['close'].rolling(20).mean()
        std20 = df['close'].rolling(20).std()
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        
        curr_close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        curr_low = df['low'].iloc[-1]
        curr_high = df['high'].iloc[-1]
        curr_lower = lower.iloc[-1]
        curr_upper = upper.iloc[-1]
        curr_sma = sma20.iloc[-1]
        curr_std = std20.iloc[-1]
        
        # Rechazo extremo en mecha
        if curr_low <= curr_lower and curr_close > curr_lower:
            return {"signal": 1.0, "reason": "Rechazo de banda inferior Bollinger (Soporte dinámico)"}
        elif curr_high >= curr_upper and curr_close < curr_upper:
            return {"signal": -1.0, "reason": "Rechazo de banda superior Bollinger (Resistencia dinámica)"}
            
        # Expansión direccional con separación clara de la media
        elif curr_close > (curr_sma + 0.6 * curr_std) and curr_close > prev_close:
            return {"signal": 0.7, "reason": "Expansión alcista sobre banda media Bollinger"}
        elif curr_close < (curr_sma - 0.6 * curr_std) and curr_close < prev_close:
            return {"signal": -0.7, "reason": "Expansión bajista bajo banda media Bollinger"}
            
        return {"signal": 0.0, "reason": "Precio en rango medio de Bollinger"}

    @staticmethod
    def stochastic_rsi_timing(df: pd.DataFrame) -> Dict[str, Any]:
        """5. Stochastic RSI (14, 14, 3, 3): Cruces de oscilador en extremos y ondas de continuación."""
        rsi = calc_rsi(df['close'], 14)
        min_rsi = rsi.rolling(14).min()
        max_rsi = rsi.rolling(14).max()
        stoch = (rsi - min_rsi) / (max_rsi - min_rsi + 1e-9) * 100
        k = stoch.rolling(3).mean()
        d = k.rolling(3).mean()
        
        curr_k = k.iloc[-1]
        curr_d = d.iloc[-1]
        prev_k = k.iloc[-2]
        prev_d = d.iloc[-2]
        
        if curr_k < 25 and curr_k > curr_d and prev_k <= prev_d:
            return {"signal": 1.0, "reason": "Cruce alcista Stoch RSI en zona de sobreventa (<25)"}
        elif curr_k > 75 and curr_k < curr_d and prev_k >= prev_d:
            return {"signal": -1.0, "reason": "Cruce bajista Stoch RSI en zona de sobrecompra (>75)"}
            
        # Agotamiento extremo
        elif curr_k >= 88 and curr_d >= 88:
            return {"signal": -0.4, "reason": f"Stoch RSI saturado en sobrecompra extrema ({curr_k:.0f})"}
        elif curr_k <= 12 and curr_d <= 12:
            return {"signal": 0.4, "reason": f"Stoch RSI saturado en sobreventa extrema ({curr_k:.0f})"}

        # Onda con inercia
        elif curr_k > curr_d and curr_k < 75 and prev_k < curr_k:
            return {"signal": 0.7, "reason": f"Onda alcista Stoch RSI en desarrollo (K={curr_k:.1f})"}
        elif curr_k < curr_d and curr_k > 25 and prev_k > curr_k:
            return {"signal": -0.7, "reason": f"Onda bajista Stoch RSI en desarrollo (K={curr_k:.1f})"}
            
        return {"signal": 0.0, "reason": f"Stoch RSI neutro (K={curr_k:.1f}, D={curr_d:.1f})"}

    @staticmethod
    def supertrend_volatility(df: pd.DataFrame, period: int = 10, multiplier: float = 2.5) -> Dict[str, Any]:
        """6. SuperTrend Volatility: Indicador de tendencia persistente con ATR."""
        atr = calc_atr(df, period)
        hl2 = (df['high'] + df['low']) / 2
        basic_upper = hl2 + (multiplier * atr)
        basic_lower = hl2 - (multiplier * atr)
        
        trend = 1
        closes = df['close'].values
        lowers = basic_lower.values
        uppers = basic_upper.values
        
        # Iteración para calcular Supertrend continuo
        final_lower = lowers[0] if not np.isnan(lowers[0]) else closes[0]
        final_upper = uppers[0] if not np.isnan(uppers[0]) else closes[0]
        
        for i in range(1, len(df)):
            if np.isnan(lowers[i]) or np.isnan(uppers[i]):
                continue
            if closes[i-1] > final_lower:
                final_lower = max(lowers[i], final_lower)
            else:
                final_lower = lowers[i]
                
            if closes[i-1] < final_upper:
                final_upper = min(uppers[i], final_upper)
            else:
                final_upper = uppers[i]
                
            if trend == 1 and closes[i] < final_lower:
                trend = -1
            elif trend == -1 and closes[i] > final_upper:
                trend = 1
                
        if trend == 1:
            return {"signal": 1.0, "reason": f"SuperTrend Alcista continuo (Soporte dinámico: {final_lower:.5f})"}
        else:
            return {"signal": -1.0, "reason": f"SuperTrend Bajista continuo (Resistencia dinámica: {final_upper:.5f})"}

    @staticmethod
    def candlestick_price_action(df: pd.DataFrame) -> Dict[str, Any]:
        """7. Candlestick Price Action: Patrones y fuerza de la vela actual."""
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        curr_body = abs(curr['close'] - curr['open'])
        curr_range = curr['high'] - curr['low'] + 1e-9
        
        lower_wick = min(curr['open'], curr['close']) - curr['low']
        upper_wick = curr['high'] - max(curr['open'], curr['close'])
        
        # Envolvente alcista
        if prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['close'] > prev['open']:
            return {"signal": 1.0, "reason": "Patrón Envolvente Alcista (Bullish Engulfing)"}
            
        # Envolvente bajista
        if prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['close'] < prev['open']:
            return {"signal": -1.0, "reason": "Patrón Envolvente Bajista (Bearish Engulfing)"}
            
        # Martillo alcista (sombra inferior >= 2x cuerpo)
        if lower_wick >= 1.8 * curr_body and upper_wick <= 0.25 * curr_range and curr['close'] >= curr['open']:
            return {"signal": 1.0, "reason": "Patrón Martillo Alcista (Hammer Pinbar)"}
            
        # Estrella fugaz bajista (sombra superior >= 2x cuerpo)
        if upper_wick >= 1.8 * curr_body and lower_wick <= 0.25 * curr_range and curr['close'] <= curr['open']:
            return {"signal": -1.0, "reason": "Patrón Estrella Fugaz Bajista (Shooting Star)"}
            
        # Vela direccional con cuerpo sólido
        if curr['close'] > curr['open'] and (curr_body / curr_range) >= 0.55:
            return {"signal": 0.6, "reason": "Vela alcista de cuerpo sólido"}
        elif curr['close'] < curr['open'] and (curr_body / curr_range) >= 0.55:
            return {"signal": -0.6, "reason": "Vela bajista de cuerpo sólido"}
            
        return {"signal": 0.0, "reason": "Vela neutra o indecisión (Doji)"}

    @staticmethod
    def support_resistance_pivots(df: pd.DataFrame) -> Dict[str, Any]:
        """8. Soportes y Resistencias Dinámicos / Pivotes de Fractal."""
        recent_window = df.iloc[-30:-1]
        high_pivot = recent_window['high'].max()
        low_pivot = recent_window['low'].min()
        curr_close = df['close'].iloc[-1]
        
        tolerance = (high_pivot - low_pivot) * 0.03
        
        if abs(curr_close - low_pivot) <= tolerance:
            return {"signal": 1.0, "reason": f"Precio rebotando en soporte clave ({low_pivot:.5f})"}
        elif abs(curr_close - high_pivot) <= tolerance:
            return {"signal": -1.0, "reason": f"Precio rechazando resistencia clave ({high_pivot:.5f})"}
        return {"signal": 0.0, "reason": "Precio en zona media del rango de pivotes"}

    @staticmethod
    def volume_flow_direction(df: pd.DataFrame) -> Dict[str, Any]:
        """9. Volume Flow / OBV Momentum: Validación de inyección de volumen."""
        vol = df.get('vol', pd.Series([1]*len(df)))
        change = df['close'].diff()
        direction = np.where(change > 0, 1, np.where(change < 0, -1, 0))
        obv = (vol * direction).cumsum()
        
        obv_ema = calc_ema(obv, 20)
        curr_obv = obv.iloc[-1]
        curr_obv_ema = obv_ema.iloc[-1]
        
        if curr_obv > curr_obv_ema and change.iloc[-1] > 0:
            return {"signal": 1.0, "reason": "Volumen institucional apoyando movimiento alcista"}
        elif curr_obv < curr_obv_ema and change.iloc[-1] < 0:
            return {"signal": -1.0, "reason": "Volumen institucional apoyando presión bajista"}
        return {"signal": 0.0, "reason": "Flujo de volumen equilibrado"}

    @staticmethod
    def adx_trend_strength(df: pd.DataFrame, period: int = 14) -> Dict[str, Any]:
        """10. ADX Trend Strength: Filtra mercados laterales y confirma dirección."""
        high = df['high']
        low = df['low']
        up_move = high.diff()
        down_move = -low.diff()
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        atr = calc_atr(df, period)
        plus_di = 100 * (pd.Series(plus_dm).rolling(period).mean() / (atr + 1e-9))
        minus_di = 100 * (pd.Series(minus_dm).rolling(period).mean() / (atr + 1e-9))
        
        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
        adx = dx.rolling(period).mean()
        
        curr_adx = adx.iloc[-1] if not np.isnan(adx.iloc[-1]) else 20.0
        curr_pdi = plus_di.iloc[-1] if not np.isnan(plus_di.iloc[-1]) else 0.0
        curr_mdi = minus_di.iloc[-1] if not np.isnan(minus_di.iloc[-1]) else 0.0
        
        if curr_adx > 25:
            if curr_pdi > curr_mdi:
                return {"signal": 1.0, "reason": f"Tendencia alcista firme (ADX={curr_adx:.1f}, +DI > -DI)"}
            else:
                return {"signal": -1.0, "reason": f"Tendencia bajista firme (ADX={curr_adx:.1f}, -DI > +DI)"}
        return {"signal": 0.0, "reason": f"Mercado lateral o sin fuerza (ADX={curr_adx:.1f} < 25)"}

ALL_STRATEGY_FUNCS = [
    ("Triple EMA Ribbon", TechnicalStrategies.triple_ema_ribbon),
    ("Dynamic RSI Reversal", TechnicalStrategies.dynamic_rsi_reversal),
    ("MACD Momentum Cross", TechnicalStrategies.macd_momentum_cross),
    ("Bollinger Squeeze & Piercing", TechnicalStrategies.bollinger_squeeze_piercing),
    ("Stochastic RSI Timing", TechnicalStrategies.stochastic_rsi_timing),
    ("SuperTrend Volatility", TechnicalStrategies.supertrend_volatility),
    ("Candlestick Price Action", TechnicalStrategies.candlestick_price_action),
    ("Support & Resistance Pivots", TechnicalStrategies.support_resistance_pivots),
    ("Volume Flow Direction", TechnicalStrategies.volume_flow_direction),
    ("ADX Trend Strength", TechnicalStrategies.adx_trend_strength),
]
