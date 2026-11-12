"""Minería de reglas sobre las salidas reales del motor (votos de las 10 estrategias),
selección en el 60% inicial y validación ciega en el 40% final."""
import os, math, itertools
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
df = pd.read_csv(os.path.join(ROOT, "data", "history", "replay.csv"))
df = df[df.ret_next != 0].copy()
df["up"] = (df.ret_next > 0).astype(int)
cut = df.ts.quantile(0.6)
IS, OOS = df[df.ts <= cut], df[df.ts > cut]
hours = (df.ts.max() - df.ts.min()) / 3600

vote_cols = [c for c in df.columns if c.startswith("vote_")]
conds = {}
for c in vote_cols:
    for v in sorted(df[c].dropna().unique()):
        if v != 0:
            conds[f"{c}=={v:g}"] = df[c] == v
for k in range(5, 11):
    conds[f"v_call>={k}"] = df.v_call >= k
    conds[f"v_put>={k}"] = df.v_put >= k
for mb in ("CALL", "PUT", "RANGO"):
    conds[f"macro={mb}"] = df.macro_bias == mb
conds["green"] = df.green == 1
conds["red"] = df.green == 0
for h in range(24):
    conds[f"hour=={h}"] = df.hour == h

def score(mask, part):
    s = part[mask.loc[part.index]]
    n = len(s)
    if n == 0: return 0, 0.5
    return n, s.up.mean()

rows = []
names = list(conds)
pairs = list(itertools.combinations(names, 2))
cands = [(n,) for n in names] + pairs
for combo in cands:
    m = conds[combo[0]]
    for c in combo[1:]:
        m = m & conds[c]
    n_is, p_is = score(m, IS)
    if n_is < 150:
        continue
    direction = "CALL" if p_is >= 0.5 else "PUT"
    wr_is = p_is if direction == "CALL" else 1 - p_is
    n_oos, p_oos = score(m, OOS)
    wr_oos = p_oos if direction == "CALL" else 1 - p_oos
    rows.append((" & ".join(combo), direction, n_is, 100 * wr_is, n_oos, 100 * wr_oos))

res = pd.DataFrame(rows, columns=["regla", "dir", "n_IS", "WR_IS", "n_OOS", "WR_OOS"])
print(f"Reglas probadas: {len(res)}  (horas={hours:.1f})")
top = res.sort_values("WR_IS", ascending=False).head(25)
print("\nTOP 25 reglas elegidas por el 60% inicial -> su resultado en el 40% ciego:")
print(top.to_string(index=False, float_format=lambda x: f"{x:.1f}"))
print(f"\nMedia WR_OOS del top 25: {top.WR_OOS.mean():.1f}%")
print(f"Reglas con WR_OOS >= 80% y n_OOS >= 50: {len(res[(res.WR_OOS >= 80) & (res.n_OOS >= 50)])}")
print(f"Reglas con WR_OOS >= 58% y n_OOS >= 100: {len(res[(res.WR_OOS >= 58) & (res.n_OOS >= 100)])}")
print(res[(res.WR_OOS >= 58) & (res.n_OOS >= 100)].sort_values("WR_OOS", ascending=False).head(10).to_string(index=False, float_format=lambda x: f"{x:.1f}"))
