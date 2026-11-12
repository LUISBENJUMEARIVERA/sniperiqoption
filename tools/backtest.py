"""
Backtester honesto de AutoTrader.

1) `download`: descarga velas 1M históricas REALES de IQ Option para todos los
   pares binarios/OTC abiertos y las guarda en data/history/<ACTIVO>.csv
2) `replay`:   reproduce EXACTAMENTE `EnsembleEngine.evaluate_mtf` vela a vela
   (5M reconstruido sin mirar al futuro) y mide el resultado real de la vela
   siguiente (expiración 1M). Guarda todas las filas en data/history/replay.csv
3) `report`:   win rate por patrón con intervalo de confianza de Wilson,
   señales/hora, simulación de Martingala G1 y PnL en unidades.

Uso:
    .venv\\Scripts\\python.exe tools\\backtest.py download --candles 3000
    .venv\\Scripts\\python.exe tools\\backtest.py replay
    .venv\\Scripts\\python.exe tools\\backtest.py report
"""
import os
import sys
import json
import math
import time
import argparse
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

HIST_DIR = os.path.join(ROOT, "data", "history")
os.makedirs(HIST_DIR, exist_ok=True)
REPLAY_PATH = os.path.join(HIST_DIR, "replay.csv")
META_PATH = os.path.join(HIST_DIR, "meta.json")

WIN_1M = 80   # velas 1M que ve el motor en vivo
WIN_5M = 80   # velas 5M que ve el motor en vivo


# --------------------------------------------------------------------------
# 1) DESCARGA
# --------------------------------------------------------------------------
def cmd_download(args):
    from iqoptionapi.stable_api import IQ_Option
    from app.config import load_saved_credentials
    from app.engine import TradingEngine

    creds = load_saved_credentials()
    if not creds.get("email") or not creds.get("password"):
        print("No hay credenciales guardadas en data/credentials.json")
        return
    iq = IQ_Option(creds["email"], creds["password"])
    ok, reason = iq.connect()
    if not ok:
        print(f"No se pudo conectar: {reason}")
        return
    print("Conectado a IQ Option (solo lectura de velas).")

    eng = TradingEngine()
    eng.iq = iq
    eng.config.use_simulator = False
    eng._refresh_live_catalog(force=True)

    best = {}
    for name, info in eng.open_instruments_cache.items():
        if not info.get("is_tradable"):
            continue
        if info.get("payout", 0) < args.min_payout:
            continue
        base = info["base"]
        if base not in best or info["payout"] > best[base]["payout"]:
            best[base] = {"symbol": name, "payout": info["payout"]}
    assets = sorted(best.values(), key=lambda x: -x["payout"])[: args.max_assets]
    print(f"{len(assets)} activos a descargar: {[a['symbol'] for a in assets]}")

    meta = {"downloaded_at": time.time(), "assets": {}}
    for a in assets:
        sym = a["symbol"]
        rows = {}
        end = time.time()
        while len(rows) < args.candles:
            try:
                raw = iq.get_candles(sym, 60, 1000, end)
            except Exception as e:
                print(f"  {sym}: error {e}")
                break
            if not raw:
                break
            before = len(rows)
            for c in raw:
                rows[int(c["from"])] = c
            if len(rows) == before:
                break
            end = min(int(c["from"]) for c in raw) - 1
            time.sleep(0.15)
        if not rows:
            continue
        df = pd.DataFrame(sorted(rows.values(), key=lambda c: c["from"]))
        df = df.rename(columns={"from": "timestamp", "min": "low", "max": "high", "volume": "vol"})
        df = df[["timestamp", "open", "high", "low", "close", "vol"]]
        df = df.iloc[:-1]  # descartar la vela en formación
        df.to_csv(os.path.join(HIST_DIR, f"{sym}.csv"), index=False)
        meta["assets"][sym] = {"payout": a["payout"], "candles": len(df)}
        print(f"  {sym}: {len(df)} velas | payout {a['payout']}%")

    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print("Descarga completada.")


# --------------------------------------------------------------------------
# 2) REPLAY (reproduce el motor real)
# --------------------------------------------------------------------------
def _build_5m_window(df1: pd.DataFrame, closed5: pd.DataFrame, i: int) -> pd.DataFrame:
    """5M visibles en el instante de la vela 1M i: velas 5M cerradas + parcial actual."""
    t = int(df1["timestamp"].iat[i])
    b = t - (t % 300)
    prev = closed5[closed5["timestamp"] < b].tail(WIN_5M - 1)
    part = df1[(df1["timestamp"] >= b) & (df1["timestamp"] <= t)]
    partial = pd.DataFrame([{
        "timestamp": b,
        "open": float(part["open"].iat[0]),
        "high": float(part["high"].max()),
        "low": float(part["low"].min()),
        "close": float(part["close"].iat[-1]),
        "vol": float(part["vol"].sum()),
    }])
    return pd.concat([prev, partial], ignore_index=True)


def _resample_5m(df1: pd.DataFrame) -> pd.DataFrame:
    g = df1.assign(bucket=df1["timestamp"] - (df1["timestamp"] % 300)).groupby("bucket")
    out = pd.DataFrame({
        "timestamp": g["timestamp"].first().index,
        "open": g["open"].first().values,
        "high": g["high"].max().values,
        "low": g["low"].min().values,
        "close": g["close"].last().values,
        "vol": g["vol"].sum().values,
    })
    # solo velas 5M completas (5 velas 1M)
    counts = g.size().values
    return out[counts == 5].reset_index(drop=True)


def _pattern_of(text: str) -> str:
    for tag in ("PATRÓN A", "PATRÓN B", "PATRÓN C"):
        if tag in text:
            return tag.replace("PATRÓN ", "")
    return ""


def _outcome(action: str, entry: float, exit_: float) -> str:
    if exit_ == entry:
        return "TIE"
    if action == "CALL":
        return "WIN" if exit_ > entry else "LOSS"
    return "WIN" if exit_ < entry else "LOSS"


def _replay_asset(sym: str) -> list:
    from app.ensemble import EnsembleEngine
    ens = EnsembleEngine()
    df1 = pd.read_csv(os.path.join(HIST_DIR, f"{sym}.csv"))
    closed5 = _resample_5m(df1)
    ts = df1["timestamp"].values
    close = df1["close"].values
    rows = []
    start = max(WIN_1M, 5 * 60)
    for i in range(start, len(df1) - 2):
        # requiere continuidad temporal para entrada y la G1
        if ts[i + 1] - ts[i] != 60 or ts[i + 2] - ts[i + 1] != 60:
            continue
        w1 = df1.iloc[i - WIN_1M + 1: i + 1].reset_index(drop=True)
        w5 = _build_5m_window(df1, closed5, i)
        if len(w5) < 30:
            continue
        ev = ens.evaluate_mtf(w5, w1)
        votes = ev["votes"]
        v_call = sum(1 for v in votes.values() if v > 0)
        v_put = sum(1 for v in votes.values() if v < 0)
        o, h, l, c = (float(w1[k].iat[-1]) for k in ("open", "high", "low", "close"))
        rng = h - l + 1e-12
        nxt = float(close[i + 1])
        nxt2 = float(close[i + 2])
        row = {
            "asset": sym,
            "ts": int(ts[i]),
            "hour": int(time.gmtime(int(ts[i])).tm_hour),
            "action": ev["dominant_action"],
            "pattern": _pattern_of(ev["confluence_text"]),
            "macro_bias": ev["macro_bias"],
            "regime_5m": ev["regime_5m"],
            "micro_action": ev["micro_action"],
            "micro_prob": ev["micro_prob"],
            "macro_prob": ev["macro_prob"],
            "v_call": v_call,
            "v_put": v_put,
            "body_ratio": abs(c - o) / rng,
            "upper_wick": (h - max(o, c)) / rng,
            "lower_wick": (min(o, c) - l) / rng,
            "green": int(c > o),
            "ret_next": (nxt - c),
            "ret_next2": (nxt2 - nxt),
        }
        for k, v in votes.items():
            row["vote_" + k.split(" ")[0]] = v
        # resultado de la operación principal
        if row["action"] in ("CALL", "PUT"):
            row["result"] = _outcome(row["action"], c, nxt)
            # Martingala G1 replicando la lógica en vivo (_execute_martingale_recovery)
            if row["result"] == "LOSS":
                w1b = df1.iloc[i - WIN_1M + 2: i + 2].reset_index(drop=True)
                e1 = ens.evaluate(w1b)
                vb = e1["votes"]
                act = row["action"]
                opp = "PUT" if act == "CALL" else "CALL"
                fav_o = sum(1 for v in vb.values() if (act == "CALL" and v > 0) or (act == "PUT" and v < 0))
                fav_p = sum(1 for v in vb.values() if (opp == "CALL" and v > 0) or (opp == "PUT" and v < 0))
                st, em = vb.get("SuperTrend Volatility", 0), vb.get("Triple EMA Ribbon", 0)
                brk = (act == "CALL" and st < 0 and em < 0) or (act == "PUT" and st > 0 and em > 0)
                if fav_p >= 6 and e1["dominant_action"] == opp:
                    g1 = opp
                elif fav_o >= 5 and not brk:
                    g1 = act
                else:
                    g1 = ""
                row["g1_action"] = g1
                row["g1_result"] = _outcome(g1, nxt, nxt2) if g1 else "ABORT"
        rows.append(row)
    return rows


def cmd_replay(args):
    files = [f[:-4] for f in os.listdir(HIST_DIR) if f.endswith(".csv") and f != "replay.csv"]
    if not files:
        print("No hay histórico. Ejecuta primero: download")
        return
    t0 = time.time()
    all_rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for sym, rows in zip(files, ex.map(_replay_asset, files)):
            all_rows.extend(rows)
            print(f"  {sym}: {len(rows)} velas evaluadas ({time.time() - t0:.0f}s)")
    pd.DataFrame(all_rows).to_csv(REPLAY_PATH, index=False)
    print(f"Replay guardado en {REPLAY_PATH} ({len(all_rows)} filas, {time.time() - t0:.0f}s)")


# --------------------------------------------------------------------------
# 3) REPORTE
# --------------------------------------------------------------------------
def wilson(w: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (c - m), 100 * (c + m))


def summarize(df: pd.DataFrame, label: str, hours: float, payout: float):
    w = int((df["result"] == "WIN").sum())
    l = int((df["result"] == "LOSS").sum())
    t = int((df["result"] == "TIE").sum())
    n = w + l
    wr = 100 * w / n if n else 0
    lo, hi = wilson(w, n)
    ev = (w * payout - l) / max(1, n + t)
    print(f"{label:<28} n={n:>5} W={w:>5} L={l:>5} T={t:>3} | WR={wr:5.1f}% IC95=[{lo:4.1f}-{hi:4.1f}] "
          f"| señ/h={n / hours:5.2f} | EV/op={ev:+.3f}u")


def cmd_report(args):
    df = pd.read_csv(REPLAY_PATH)
    meta = json.load(open(META_PATH, encoding="utf-8")) if os.path.exists(META_PATH) else {"assets": {}}
    payouts = [v["payout"] for v in meta["assets"].values()] or [85.0]
    payout = float(np.median(payouts)) / 100.0
    hours = (df["ts"].max() - df["ts"].min()) / 3600.0
    n_assets = df["asset"].nunique()
    print(f"\nHistórico: {len(df)} velas | {n_assets} activos | {hours:.1f} h | payout mediano {payout * 100:.0f}% "
          f"| WR de equilibrio = {100 / (1 + payout):.1f}%\n")

    sig = df[df["action"].isin(["CALL", "PUT"])].copy()
    print("=== Señales del motor actual (todas las velas de todos los activos) ===")
    summarize(sig, "TOTAL", hours, payout)
    for p in sorted(sig["pattern"].dropna().unique()):
        for a in ("CALL", "PUT"):
            s = sig[(sig["pattern"] == p) & (sig["action"] == a)]
            if len(s):
                summarize(s, f"Patrón {p} {a}", hours, payout)

    # Out-of-sample: último 30% del tiempo
    cut = df["ts"].quantile(0.7)
    print("\n=== Estabilidad temporal (70% inicial vs 30% final) ===")
    summarize(sig[sig["ts"] <= cut], "In-sample 70%", hours * 0.7, payout)
    summarize(sig[sig["ts"] > cut], "Out-of-sample 30%", hours * 0.3, payout)

    # Martingala G1
    if "g1_result" in sig.columns:
        loss = sig[sig["result"] == "LOSS"]
        g1w = int((loss["g1_result"] == "WIN").sum())
        g1l = int((loss["g1_result"] == "LOSS").sum())
        g1a = int((loss["g1_result"] == "ABORT").sum())
        w = int((sig["result"] == "WIN").sum())
        cycles_win = w + g1w
        cycles = w + len(loss)
        pnl = w * payout - len(loss) + g1w * 2 * payout - g1l * 2
        print("\n=== Martingala G1 (x2) ===")
        print(f"G1 disparadas={g1w + g1l} (W={g1w} L={g1l}) abortadas={g1a}")
        print(f"WR por CICLO (lo que ve el panel con G1) = {100 * cycles_win / max(1, cycles):.1f}%")
        print(f"PnL total = {pnl:+.1f} unidades  (sin G1: {w * payout - len(loss):+.1f}u)")

    # Por activo
    print("\n=== Por activo ===")
    for a, s in sig.groupby("asset"):
        summarize(s, a, hours, payout)
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--candles", type=int, default=3000)
    d.add_argument("--max-assets", type=int, default=20)
    d.add_argument("--min-payout", type=float, default=80.0)
    r = sub.add_parser("replay")
    r.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    sub.add_parser("report")
    a = ap.parse_args()
    {"download": cmd_download, "replay": cmd_replay, "report": cmd_report}[a.cmd](a)
