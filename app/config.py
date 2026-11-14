import os
import json
from typing import List, Dict, Any
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "trades.db")
WEIGHTS_PATH = os.path.join(DATA_DIR, "strategy_weights.json")
CREDS_PATH = os.path.join(DATA_DIR, "credentials.json")

FOREX_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD"]
OTC_ASSETS = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURJPY-OTC", 
    "EURGBP-OTC", "USDCHF-OTC", "NZDUSD-OTC", "GBPJPY-OTC", "AUDCAD-OTC"
]
CRYPTO_COMMODITY_ASSETS = ["XAUUSD", "BTCUSD"]
DEFAULT_ASSETS = OTC_ASSETS + FOREX_ASSETS + CRYPTO_COMMODITY_ASSETS

# Whitelist Estricta Institucional para Mercado OTC:
# Permite ÚNICAMENTE los 28 pares Forex G8 (bancos centrales mayores), Índices Bursátiles Globales,
# Oro y pares con ventaja estructural demostrada. Bloquea el 100% de altcoins/memecoins cripto OTC,
# acciones individuales OTC, metales ilíquidos (XPD/XPT) y divisas exóticas (USDSGD, USDZAR, etc.).
INSTITUTIONAL_OTC_WHITELIST = {
    # 28 Pares Forex G8 (EUR, GBP, USD, JPY, CHF, AUD, CAD, NZD)
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "USDCHF-OTC", "AUDUSD-OTC", "USDCAD-OTC", "NZDUSD-OTC",
    "EURJPY-OTC", "GBPJPY-OTC", "EURGBP-OTC", "AUDCHF-OTC", "CADCHF-OTC", "CADJPY-OTC", "CHFJPY-OTC",
    "EURCHF-OTC", "EURCAD-OTC", "EURAUD-OTC", "EURNZD-OTC", "GBPCHF-OTC", "GBPCAD-OTC", "GBPAUD-OTC",
    "GBPNZD-OTC", "AUDCAD-OTC", "AUDJPY-OTC", "AUDNZD-OTC", "NZDCAD-OTC", "NZDCHF-OTC", "NZDJPY-OTC",
    # Índices Bursátiles Globales Mayores OTC
    "US30-OTC", "USNDAQ100-OTC", "SP500-OTC", "US2000-OTC", "EU50-OTC", "GER30-OTC", "UK100-OTC",
    "FR40-OTC", "JP225-OTC", "AUS200-OTC", "HK33-OTC",
    "US100/JP225-OTC", "US500/JP225-OTC", "US30/JP225-OTC", "GER30/UK100-OTC",
    # Metales Mayores y Pares Verificados Walk-Forward
    "XAUUSD-OTC", "XAU/XAG-OTC", "USDBOB-OTC", "COTTON-OTC", "SUGAR-OTC"
}

# Lista Negra Estricta de Activos Tóxicos:
# Excluye divisas de paridad fija/intervenidas, acciones ilíquidas OTC y memecoins con ruido errático
TOXIC_ASSETS_BLACKLIST = {
    # Divisas de paridad fija, control cambiario o micro-gaps no modelables
    "USDAED-OTC", "USDBDT-OTC", "USDVND-OTC", "USDDOP-OTC", "USDARS-OTC",
    "USDBRL-OTC", "USDMYR-OTC", "USDPHP-OTC", "USDTRY-OTC", "USDCLP-OTC",
    "USDCOP-OTC", "USDNGN-OTC", "CHFNOK-OTC", "USDSGD-OTC", "USDZAR-OTC",
    # Memecoins, altcoins cripto OTC, acciones sintéticas OTC y metales ilíquidos
    "PEPEUSD-OTC", "URANIUM-OTC", "TRUMPUSD-OTC", "WIFUSD-OTC", "BONKUSD-OTC",
    "ORDIUSD-OTC", "STXUSD-OTC", "SEIUSD-OTC", "ETHUSD-OTC", "SHIBUSD-OTC",
    "JUPUSD-OTC", "WLDUSD-OTC", "TAOUSD-OTC", "SUIUSD-OTC", "RAYDIUMUSD-OTC",
    "RENDERUSD-OTC", "PENGUUSD-OTC", "ONDOUSD-OTC", "HBARUSD-OTC", "FETUSD-OTC",
    "DYDXUSD-OTC", "XPDUSD-OTC", "XPTUSD-OTC", "XNGUSD-OTC",
    "FWONA-OTC", "AIG-OTC", "HYPE-OTC", "OpenAI-OTC", "COFFEE-OTC", "XLK", "ASMLa"
}

def load_saved_credentials() -> Dict[str, str]:
    if os.path.exists(CREDS_PATH):
        try:
            with open(CREDS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"email": "", "password": ""}

def save_credentials_to_file(email: str, password: str):
    if email or password:
        try:
            with open(CREDS_PATH, "w", encoding="utf-8") as f:
                json.dump({"email": email, "password": password}, f, indent=2)
        except Exception:
            pass

class BotConfig(BaseModel):
    email: str = ""
    password: str = ""
    assets: List[str] = []  # Vacío por defecto = Modo Radar Autónomo (descubre los 6-10 mejores pares abiertos)
    asset: str = "EURUSD"
    timeframe_min: int = 1
    amount: float = 10.0
    min_probability: float = 80.0
    mode: str = "PRACTICE"  # "PRACTICE" (DEMO) o "REAL"
    use_simulator: bool = False
    use_martingale: bool = True  # Activado por defecto siempre al encender el bot
    martingale_levels: int = 1  # 1 reentrada inteligente (G1: x2) recomendada para evitar sobreexposición
    martingale_multiplier: float = 2.0
    use_adaptive_threshold: bool = True  # Umbral dinámico adaptativo (74% en tendencia / 82% en rango)
    use_confidence_scaling: bool = True  # Escalado asimétrico por convicción estadística (Kelly)
    use_sniper_pullback: bool = False    # Disparos estrictos al cierre de vela confirmada (segundos 58-59 o 00)
    min_payout: float = 80.0             # Filtro de payout mínimo en vivo (>= 80%)
