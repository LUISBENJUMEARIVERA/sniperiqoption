"""Exploración honesta de edge en el histórico 1M OTC (walk-forward 60/40)."""
import os, sys, math
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = os.path.join(ROOT, "data", "history")

def wilson_lo(w, n, z=1.96):
    if n == 0: return 0
    p = w/n; d = 1+z*z/n; c = (p+z*z/(2*n))/d
    return 100*(c - z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d)

frames = []
for f in os.listdir(H):
    if not f.endswith("-OTC.csv"): continue
    d = pd.read_csv(os.path.join(H, f)); d["asset"] = f[:-4]
    o, h, l, c = d.open, d.high, d.low, d.close
    rng = (h-l).replace(0, np.nan)
    d["dir"] = np.sign(c-o)
    d["body"] = (c-o).abs()/rng
    d["uw"] = (h-np.maximum(o,c))/rng
    d["lw"] = (np.minimum(o,c)-l)/rng
    d["nxt"] = np.sign(c.shift(-1)-c)          # entrada al cierre, expira cierre siguiente
    # racha de velas del mismo color
    s = d["dir"].values; streak = np.zeros(len(d))
    for i in range(1, len(d)):
        streak[i] = streak[i-1]+1 if s[i] == s[i-1] and s[i] != 0 else (1 if s[i] != 0 else 0)
    d["streak"] = streak
    atr = (h-l).rolling(14).mean(); d["rng_atr"] = (h-l)/atr
    d["ret5"] = np.sign(c - c.shift(5))
    frames.append(d)
df = pd.concat(frames, ignore_index=True).dropna(subset=["nxt","rng_atr"])
df = df[df.nxt != 0]
cut = df.timestamp.quantile(0.6)
hours = (df.timestamp.max()-df.timestamp.min())/3600

def test(name, mask, direction):
    """direction: +1 CALL, -1 PUT, or series (follow / fade)."""
    res = []
    for part, m in (("IS", df.timestamp <= cut), ("OOS", df.timestamp > cut)):
        sel = df[mask & m]
        dirv = direction[mask & m] if isinstance(direction, pd.Series) else direction
        win = (sel.nxt == dirv).sum(); n = len(sel)
        res.append((n, 100*win/max(1,n), wilson_lo(win, n)))
    (n1,w1,l1),(n2,w2,l2) = res
    print(f"{name:<48} IS n={n1:>6} WR={w1:5.1f}% | OOS n={n2:>6} WR={w2:5.1f}% lo95={l2:5.1f}% | señ/h(20 activos)={(n1+n2)/hours:6.1f}")

print(f"Velas: {len(df)}  horas: {hours:.1f}\n")
allm = pd.Series(True, index=df.index)
test("Continuación (seguir color vela)", df.dir != 0, df.dir)
test("Reversión (contra color vela)", df.dir != 0, -df.dir)
for k in (2,3,4,5,6):
    m = df.streak >= k
    test(f"Reversión tras racha >= {k}", m, -df.dir)
    test(f"Continuación tras racha >= {k}", m, df.dir)
for b in (0.6, 0.8):
    m = df.body >= b
    test(f"Reversión cuerpo >= {b}", m, -df.dir)
for a in (1.5, 2.0, 2.5):
    m = df.rng_atr >= a
    test(f"Reversión rango >= {a} ATR", m, -df.dir)
    test(f"Continuación rango >= {a} ATR", m, df.dir)
m = (df.uw >= 0.5); test("PUT tras mecha superior >= 50%", m, -1)
m = (df.lw >= 0.5); test("CALL tras mecha inferior >= 50%", m, 1)
m = df.ret5 != 0
test("Reversión 5 velas", m, -df.ret5)
test("Continuación 5 velas", m, df.ret5)
