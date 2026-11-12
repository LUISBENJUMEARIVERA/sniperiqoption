"""Forensic reverse-engineering of IQ Option 1M candles in data/history/*.csv."""
import os, math
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = os.path.join(ROOT, "data", "history")

frames = []
for f in sorted(os.listdir(H)):
    if not f.endswith("-OTC.csv"):
        continue
    d = pd.read_csv(os.path.join(H, f))
    d["asset"] = f[:-4]
    o, h, l, c, v = d.open, d.high, d.low, d.close, d.vol
    rng = (h - l).replace(0, np.nan)
    atr = rng.rolling(14).mean()

    # Gap between previous close and current open
    d["gap"] = o - c.shift(1)
    d["ret"] = c - o
    d["ret_cc"] = c - c.shift(1)
    d["nxt_ret"] = c.shift(-1) - o.shift(-1)      # next candle C - O
    d["nxt_cc"] = c.shift(-1) - c                  # next candle C - current C
    d["dir"] = np.sign(d["ret"])
    d["nxt_dir_co"] = np.sign(d["nxt_ret"])
    d["nxt_dir_cc"] = np.sign(d["nxt_cc"])

    # Rolling autocorrelation of candle directions over last K bars
    for k in (5, 10, 20, 30):
        prod = d["dir"] * d["dir"].shift(1)
        d[f"ac_{k}"] = prod.rolling(k).mean()

    # Variance ratio VR(q) = Var(c - c.shift(q)) / (q * Var(c - c.shift(1)))
    for q in (4, 8):
        r1 = d["ret_cc"]
        rq = c - c.shift(q)
        d[f"vr_{q}"] = rq.rolling(24).var() / (q * r1.rolling(24).var() + 1e-18)

    # Z-score of price vs SMA / EMA
    for p in (5, 10, 20, 50):
        m = c.rolling(p).mean()
        s = c.rolling(p).std()
        d[f"z_{p}"] = (c - m) / (s + 1e-18)

    # Close position within candle range [0 = at low, 1 = at high]
    d["clv"] = (c - l) / rng
    d["rng_atr"] = rng / atr
    d["vol_z"] = (v - v.rolling(20).mean()) / (v.rolling(20).std() + 1e-9)

    # Upper & lower wicks in ATR units
    d["atr"] = atr
    d["uw_atr"] = (h - np.maximum(o, c)) / atr
    d["lw_atr"] = (np.minimum(o, c) - l) / atr
    d["up_exc"] = (h - o) / atr
    d["dn_exc"] = (o - l) / atr
    d["body_atr"] = (c - o) / atr

    frames.append(d)

df = pd.concat(frames, ignore_index=True).dropna(subset=["nxt_dir_cc", "ac_20", "z_20"])
cut = df.timestamp.quantile(0.6)

print("=== 1. GAP STRUCTURE (open[t+1] vs close[t]) ===")
gap_nonzero = (df["gap"].abs() > 1e-10).mean()
print(f"Fraction of candles with open[t] != close[t-1]: {100*gap_nonzero:.2f}%")
print(f"Sign(close[t+1]-open[t+1]) == Sign(close[t+1]-close[t]): {100*(df.nxt_dir_co == df.nxt_dir_cc).mean():.2f}%")

# If you enter at second 00, your strike is open[t+1] (or close[t])?
# Wait: at second 00.2, price is already open[t+1]!
# What if open[t+1] != close[t]?
g_up = df[df["gap"] > 0]
g_dn = df[df["gap"] < 0]
print(f"When gap > 0 (open > prev_close): P(close > open) = {100*(g_up.ret > 0).mean():.2f}%, P(close > prev_close) = {100*(g_up.ret_cc > 0).mean():.2f}% (n={len(g_up)})")
print(f"When gap < 0 (open < prev_close): P(close < open) = {100*(g_dn.ret < 0).mean():.2f}%, P(close < prev_close) = {100*(g_dn.ret_cc < 0).mean():.2f}% (n={len(g_dn)})")

print("\n=== 2. AUTOCORRELATION REGIME CONDITIONING ===")
for k in (5, 10, 20, 30):
    for thresh in (0.2, 0.4, 0.6):
        # Positive AC regime -> test continuation; Negative AC regime -> test reversal
        pos = df[df[f"ac_{k}"] >= thresh]
        neg = df[df[f"ac_{k}"] <= -thresh]
        p_cont = (pos.nxt_dir_cc == pos.dir).mean() * 100 if len(pos) else 0
        p_rev = (neg.nxt_dir_cc == -neg.dir).mean() * 100 if len(neg) else 0
        print(f"ac_{k} >= +{thresh}: cont WR={p_cont:5.2f}% (n={len(pos):>5}) | ac_{k} <= -{thresh}: rev WR={p_rev:5.2f}% (n={len(neg):>5})")

print("\n=== 3. Z-SCORE / BOLLINGER / CLV / WICK EXTREMES ===")
for zt in (1.5, 2.0, 2.5, 3.0):
    for p in (5, 10, 20):
        hi = df[df[f"z_{p}"] >= zt]
        lo = df[df[f"z_{p}"] <= -zt]
        wr_put = (hi.nxt_dir_cc == -1).mean() * 100 if len(hi) else 0
        wr_call = (lo.nxt_dir_cc == 1).mean() * 100 if len(lo) else 0
        print(f"z_{p} >= {zt}: PUT WR={wr_put:5.2f}% (n={len(hi):>5}) | z_{p} <= -{zt}: CALL WR={wr_call:5.2f}% (n={len(lo):>5})")

print("\n=== 4. INTRA-CANDLE EXCURSION / WICK GEOMETRY (REAL 1M CANDLES) ===")
# In every 1M candle (open O, high H, low L, close C):
# High H >= max(O, C) and Low L <= min(O, C).
# Suppose during the first 25 seconds of the candle, price dips below O by delta = k * ATR (so L <= O - k*ATR).
# If we buy CALL at strike S = O - k*ATR (expiring at close C of the SAME candle):
# Win condition is C > S = O - k*ATR!
print("\n=== 4. INTRA-CANDLE EXCURSION / WICK GEOMETRY (SAME-CANDLE EXPIRY) ===")
# Note: in a 1M candle, we don't know from OHLC alone whether H or L occurred before second 28.
# However, if price moves +k*ATR or -k*ATR from Open, what fraction of candles that reach O - k*ATR close above O - k*ATR vs below O - k*ATR?
for k in (0.20, 0.30, 0.40, 0.50, 0.60, 0.75):
    dipped = df[df["dn_exc"] >= k]
    spiked = df[df["up_exc"] >= k]
    # Reversion from dip: C > O - k*ATR (i.e. body_atr > -k)
    rev_dip = (dipped["body_atr"] > -k).mean() * 100
    # Continuation of dip: C < O - k*ATR (i.e. body_atr < -k)
    cont_dip = (dipped["body_atr"] < -k).mean() * 100
    # Reversion from spike: C < O + k*ATR (i.e. body_atr < k)
    rev_spk = (spiked["body_atr"] < k).mean() * 100
    print(f"Excursion k={k:.2f} ATR | Dip n={len(dipped):>5}: Close > Strike (CALL rev) = {rev_dip:5.1f}% | Spike n={len(spiked):>5}: Close < Strike (PUT rev) = {rev_spk:5.1f}%")

print("\n=== 5. CROSS-CURRENCY TRIANGULAR / BASKET DISLOCATION ===")
# Pivot normalized returns (ret / atr) by timestamp x asset
piv = df.pivot_table(index="timestamp", columns="asset", values="body_atr")
nxt_piv = df.pivot_table(index="timestamp", columns="asset", values="nxt_dir_cc")

# Decompose each pair into Base Currency (first 3 chars) and Quote Currency (chars 3..6)
pairs = list(piv.columns)
currencies = sorted(list({p[:3] for p in pairs} | {p[3:6] for p in pairs}))
# Least-squares currency strength at each timestamp
B = np.zeros((len(pairs), len(currencies)))
for i, p in enumerate(pairs):
    B[i, currencies.index(p[:3])] = 1.0
    B[i, currencies.index(p[3:6])] = -1.0
pinvB = np.linalg.pinv(B)  # (8, 20)
mat = piv.dropna().values  # (T, 20)
ts_idx = piv.dropna().index
strengths = mat @ pinvB.T  # (T, 8)
implied = strengths @ B.T  # (T, 20)
residual = mat - implied   # (T, 20): how much pair i overshot its triangular basket implied move!
nxt_mat = nxt_piv.loc[ts_idx, pairs].values

for thr in (0.5, 0.8, 1.0, 1.2, 1.5):
    over = residual >= thr
    under = residual <= -thr
    put_wins = (nxt_mat[over] == -1).mean() * 100 if over.sum() else 0
    call_wins = (nxt_mat[under] == 1).mean() * 100 if under.sum() else 0
    # Also test basket momentum (following implied move when implied is strong)
    imp_up = implied >= thr
    imp_dn = implied <= -thr
    imp_call = (nxt_mat[imp_up] == 1).mean() * 100 if imp_up.sum() else 0
    imp_put = (nxt_mat[imp_dn] == -1).mean() * 100 if imp_dn.sum() else 0
    print(f"Thr={thr:.1f} | Residual Fade PUT={put_wins:5.1f}% (n={over.sum():>5}) CALL={call_wins:5.1f}% (n={under.sum():>5}) | Basket Follow CALL={imp_call:5.1f}% PUT={imp_put:5.1f}%")

