"""
Test Walk-Forward Meta-Strategy Engine across all 79 Turbo assets on IQ Option.
Downloads 1500 1M candles per Turbo asset, defines 12 quantitative micro-strategies
(Mean-Reversion, Streak Exhaustion, Connors RSI-2/RSI-3, Keltner+Bollinger Squeeze,
Z-Score Pinbar, OU-Kalman Reversion, Volatility Climax Fade, Intraday Momentum),
calibrates on rolling past window (first 60% = 900 bars) and evaluates strictly
OUT-OF-SAMPLE on the final 40% (600 bars = 10 hours).
"""
import os, sys, time, math
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code
from app.config import load_saved_credentials
from app.strategies import calc_rsi, calc_ema

CACHE_DIR = os.path.join(ROOT, "data", "history_turbo")
os.makedirs(CACHE_DIR, exist_ok=True)

def download_all():
    creds = load_saved_credentials()
    iq = IQ_Option(creds["email"], creds["password"])
    iq.connect()
    init = iq.get_all_init_v2()
    profits = iq.get_all_profit()
    turbo = init.get("turbo", {}).get("actives", {})

    assets = {}
    for aid, d in turbo.items():
        if d.get("enabled") and not d.get("is_suspended"):
            name = d.get("name", "").replace("front.", "")
            if name.endswith("-op"): continue
            p = profits.get(name, {}).get("turbo", 0)
            p = round(p*100 if p<=1 else p, 1)
            if p >= 80.0:
                OP_code.ACTIVES[name] = int(aid)
                assets[name] = p

    print(f"Found {len(assets)} Turbo assets >=80% payout.")
    meta = {}
    for sym, pay in sorted(assets.items(), key=lambda x: -x[1]):
        fpath = os.path.join(CACHE_DIR, f"{sym.replace('/', '_').replace(':', '_')}.csv")
        if os.path.exists(fpath) and (time.time() - os.path.getmtime(fpath) < 3600):
            meta[sym] = {"file": fpath, "payout": pay}
            continue
        try:
            rows = {}
            end = time.time()
            for _ in range(2):
                c = iq.get_candles(sym, 60, 1000, end)
                if not c: break
                for r in c: rows[int(r["from"])] = r
                end = min(int(r["from"]) for r in c) - 1
            if len(rows) < 600: continue
            df = pd.DataFrame(sorted(rows.values(), key=lambda x: x["from"]))
            df = df.rename(columns={"from": "ts", "min": "low", "max": "high", "volume": "vol"}).iloc[:-1]
            df[["ts", "open", "high", "low", "close", "vol"]].to_csv(fpath, index=False)
            meta[sym] = {"file": fpath, "payout": pay}
        except Exception:
            pass
    return meta

def compute_signals(df: pd.DataFrame) -> dict:
    """Returns dict of {strategy_name: Series of {-1, 0, +1}} without lookahead."""
    o, h, l, c, v = df.open, df.high, df.low, df.close, df.vol
    rng = (h - l).replace(0, np.nan)
    atr = (h - l).rolling(14).mean() + 1e-12
    dir_ = np.sign(c - o)
    body = (c - o).abs() / (rng + 1e-12)
    uw = (h - np.maximum(o, c)) / (rng + 1e-12)
    lw = (np.minimum(o, c) - l) / (rng + 1e-12)

    # Streak of same-direction candles
    s = dir_.values
    st = np.zeros(len(df))
    for i in range(1, len(df)):
        st[i] = st[i-1] + 1 if (s[i] == s[i-1] and s[i] != 0) else (1 if s[i] != 0 else 0)
    streak = pd.Series(st, index=df.index)

    rsi2 = calc_rsi(c, 2)
    rsi3 = calc_rsi(c, 3)
    rsi7 = calc_rsi(c, 7)
    rsi14 = calc_rsi(c, 14)

    sma10 = c.rolling(10).mean(); std10 = c.rolling(10).std() + 1e-12
    z10 = (c - sma10) / std10
    sma20 = c.rolling(20).mean(); std20 = c.rolling(20).std() + 1e-12
    z20 = (c - sma20) / std20

    ema9 = calc_ema(c, 9)
    ema21 = calc_ema(c, 21)
    ema50 = calc_ema(c, 50)

    # Keltner channel (EMA20 +/- 1.8 ATR)
    ema20 = calc_ema(c, 20)
    kc_up = ema20 + 1.8 * atr
    kc_dn = ema20 - 1.8 * atr

    # Stochastic (5, 3)
    ll5 = l.rolling(5).min(); hh5 = h.rolling(5).max()
    stoch5 = 100 * (c - ll5) / (hh5 - ll5 + 1e-12)

    sigs = {}
    # S1: Connors RSI(2) + BB(10) Extreme Reversion
    sigs["S1_Connors_RSI2_BB10"] = pd.Series(
        np.where((rsi2 >= 92) & (z10 >= 1.5), -1,
        np.where((rsi2 <= 8) & (z10 <= -1.5), 1, 0)), index=df.index)

    # S2: RSI(3) + BB(20) + Wick Rejection
    sigs["S2_RSI3_BB20_Wick"] = pd.Series(
        np.where((rsi3 >= 85) & (z20 >= 1.6) & (uw >= 0.25), -1,
        np.where((rsi3 <= 15) & (z20 <= -1.6) & (lw >= 0.25), 1, 0)), index=df.index)

    # S3: Streak >= 3 Exhaustion + RSI(7) Extreme
    sigs["S3_Streak3_RSI7"] = pd.Series(
        np.where((streak >= 3) & (dir_ > 0) & (rsi7 >= 68), -1,
        np.where((streak >= 3) & (dir_ < 0) & (rsi7 <= 32), 1, 0)), index=df.index)

    # S4: Streak >= 4 Pure Exhaustion Reversal
    sigs["S4_Streak4_Rev"] = pd.Series(
        np.where(streak >= 4, -dir_, 0), index=df.index)

    # S5: Double Outside Band (Bollinger 20 + Keltner 1.8 ATR) Climax Reversion
    sigs["S5_BB_Keltner_Climax"] = pd.Series(
        np.where((z20 >= 1.9) & (c >= kc_up) & (rsi7 >= 70), -1,
        np.where((z20 <= -1.9) & (c <= kc_dn) & (rsi7 <= 30), 1, 0)), index=df.index)

    # S6: Trend Pullback RSI(2) Dip in Macro Trend (EMA21 > EMA50)
    sigs["S6_Trend_RSI2_Pullback"] = pd.Series(
        np.where((ema21 > ema50) & (c > ema50) & (rsi2 <= 12) & (dir_ < 0), 1,
        np.where((ema21 < ema50) & (c < ema50) & (rsi2 >= 88) & (dir_ > 0), -1, 0)), index=df.index)

    # S7: Pinbar Liquidity Sweep (Wick >= 50% of range at 10-bar High/Low)
    hi10 = h.rolling(10).max(); lo10 = l.rolling(10).min()
    sigs["S7_Liquidity_Sweep_Pinbar"] = pd.Series(
        np.where((h >= hi10) & (uw >= 0.50) & (body <= 0.35), -1,
        np.where((l <= lo10) & (lw >= 0.50) & (body <= 0.35), 1, 0)), index=df.index)

    # S8: Volatility Spike Climax Fade (Range >= 1.8x ATR + Z10 >= 1.6)
    sigs["S8_Vol_Spike_Fade"] = pd.Series(
        np.where(((h - l) >= 1.8 * atr) & (dir_ > 0) & (z10 >= 1.5), -1,
        np.where(((h - l) >= 1.8 * atr) & (dir_ < 0) & (z10 <= -1.5), 1, 0)), index=df.index)

    # S9: Stoch(5) + RSI(7) + Streak>=2 Reversion
    sigs["S9_Stoch5_RSI7_Streak2"] = pd.Series(
        np.where((stoch5 >= 90) & (rsi7 >= 70) & (streak >= 2) & (dir_ > 0), -1,
        np.where((stoch5 <= 10) & (rsi7 <= 30) & (streak >= 2) & (dir_ < 0), 1, 0)), index=df.index)

    # S10: Momentum Breakout Continuation (for trending assets)
    sigs["S10_Momentum_Breakout"] = pd.Series(
        np.where((ema9 > ema21) & (ema21 > ema50) & (body >= 0.60) & (dir_ > 0) & (55 <= rsi14) & (rsi14 <= 68), 1,
        np.where((ema9 < ema21) & (ema21 < ema50) & (body >= 0.60) & (dir_ < 0) & (32 <= rsi14) & (rsi14 <= 45), -1, 0)), index=df.index)

    return sigs

def run_study():
    meta = download_all()
    records = []
    for sym, info in meta.items():
        df = pd.read_csv(info["file"])
        if len(df) < 600: continue
        # Outcome: next candle close vs next candle open (entering at second 00) AND vs current close (entering at second 59)
        df["out_co"] = np.sign(df.close.shift(-1) - df.open.shift(-1))
        df["out_cc"] = np.sign(df.close.shift(-1) - df.close)
        sigs = compute_signals(df)
        cut = df.ts.quantile(0.60)
        is_m = (df.ts <= cut) & (df.out_co != 0) & df.out_co.notna()
        oos_m = (df.ts > cut) & (df.out_co != 0) & df.out_co.notna()
        hrs_oos = (df.loc[oos_m, "ts"].max() - df.loc[oos_m, "ts"].min()) / 3600.0 if oos_m.sum() else 1.0

        for sname, sseries in sigs.items():
            m1 = is_m & (sseries != 0)
            m2 = oos_m & (sseries != 0)
            n_is = int(m1.sum()); n_oos = int(m2.sum())
            if n_is < 15 or n_oos < 8:
                continue
            w_is = int((df.loc[m1, "out_co"] == sseries[m1]).sum())
            w_oos = int((df.loc[m2, "out_co"] == sseries[m2]).sum())
            w_oos_cc = int((df.loc[m2, "out_cc"] == sseries[m2]).sum())
            records.append({
                "sym": sym, "pay": info["payout"], "strat": sname,
                "n_IS": n_is, "WR_IS": 100*w_is/n_is,
                "n_OOS": n_oos, "WR_OOS": 100*w_oos/n_oos,
                "WR_OOS_cc": 100*w_oos_cc/n_oos,
                "w_OOS": w_oos, "hrs_OOS": hrs_oos
            })

    res = pd.DataFrame(records)
    print(f"\nTotal (asset, strategy) pairs evaluated: {len(res)}")
    for min_is in (55, 58, 60, 62, 65):
        for min_n in (15, 20, 25):
            sel = res[(res.WR_IS >= min_is) & (res.n_IS >= min_n)]
            if len(sel) == 0: continue
            tot_w = sel.w_OOS.sum(); tot_n = sel.n_OOS.sum()
            wr_oos = 100 * tot_w / max(1, tot_n)
            hrs = sel.hrs_OOS.median()
            print(f"Filter IS>={min_is}% (n_IS>={min_n}) -> pairs={len(sel):>3} | OOS trades={tot_n:>4} ({tot_n/hrs:5.1f}/h) | OOS Flat WR={wr_oos:5.2f}% | Cycle WR(G1)={100*(1-(1-wr_oos/100)**2):5.2f}%")

if __name__ == "__main__":
    run_study()
