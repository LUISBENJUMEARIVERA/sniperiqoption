import os, numpy as np, pandas as pd
H = "data/history"
out = []
for f in os.listdir(H):
    if not f.endswith("-OTC.csv"): continue
    d = pd.read_csv(os.path.join(H, f)); c = d.close
    for hz in (1, 3, 5, 15):
        fut = np.sign(c.shift(-hz) - c)
        for lb in (1, 5, 15, 60):
            past = np.sign(c - c.shift(lb))
            m = (fut != 0) & (past != 0) & fut.notna() & past.notna()
            out.append((hz, lb, int(m.sum()), int((fut[m] == past[m]).sum())))
r = pd.DataFrame(out, columns=["hz","lb","n","cont"]).groupby(["hz","lb"]).sum()
r["continuacion_%"] = 100 * r.cont / r.n
r["reversion_%"] = 100 - r["continuacion_%"]
print(r[["n","continuacion_%","reversion_%"]].round(2).to_string())
