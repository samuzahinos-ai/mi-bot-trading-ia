"""Simulador usado para validar la estrategia v2 (ver INFORME_BACKTEST.md).

Necesita un archivo pickle con un diccionario {(intervalo, símbolo): DataFrame} de velas
(columnas open, high, low, close, volume; índice de fechas UTC). Los datos del informe son velas spot de
Binance (BTC, ETH, SOL, XRP, DOGE contra USDT) del repositorio público Speirsy11/crypto-dataset.
Ruta por entorno: BT_DATA (por defecto data.pkl). Requiere numpy y pandas.
"""
import os
import pickle
import numpy as np
import pandas as pd
DATA = pickle.load(open(os.environ.get("BT_DATA", "data.pkl"), "rb"))
SYMS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT"]
BARS_PER_YEAR = {"1d":365, "4h":365*6, "1h":365*24}

def sma(s, n): return s.rolling(n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()
def rsi(s, n=2):
    d = s.diff(); up = d.clip(lower=0); dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1/n, adjust=False).mean(); ad = dn.ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1+au/ad.replace(0,np.nan))

# ---------------- generadores de señal (usan solo datos hasta el cierre de la vela t) -------------
def sig_donchian(df, N, M, trend=None):
    hh = df.high.shift(1).rolling(N).max(); ll = df.low.shift(1).rolling(M).min()
    ent = df.close > hh
    if trend: ent &= df.close > sma(df.close, trend)
    return ent.fillna(False).values, (df.close < ll).fillna(False).values
def sig_ema(df, f, s):
    ef, es = ema(df.close, f), ema(df.close, s)
    return (ef > es).fillna(False).values, (ef < es).fillna(False).values
def sig_tsmom(df, N):
    r = df.close / df.close.shift(N) - 1
    return (r > 0).fillna(False).values, (r < 0).fillna(False).values
def sig_rsi2(df, trend=200, lo=10, ex=5):
    r = rsi(df.close, 2); ent = (r < lo) & (df.close > sma(df.close, trend))
    return ent.fillna(False).values, (df.close > sma(df.close, ex)).fillna(False).values

# ---------------- simulador de una "hucha" (1 activo, 100 % invertido cuando hay señal) ------------
def run(df, ent, ext, fee, slip, trail_atr=None, init_atr=None, size_risk=None):
    """size_risk: fracción de la hucha que se arriesga hasta el stop inicial (requiere init_atr)."""
    o,h,l,c = (df[k].values for k in ("open","high","low","close"))
    a = atr(df).values
    n = len(df); eq = 1.0; curve = np.ones(n); pos = False; trades = []; stop = None; cash = 0.0; units = 0.0
    def close_at(px_raw, t):
        nonlocal eq, pos, cash, units
        px = px_raw*(1-slip); eq = cash + units*px*(1-fee); trades.append((entry_t, t, eq/entry_eq-1)); pos = False; curve[t] = eq
    for t in range(1, n):
        if pos:
            if stop is not None and l[t] <= stop: close_at(min(o[t], stop), t); continue
            if ext[t-1]: close_at(o[t], t); continue
            if trail_atr and not np.isnan(a[t]): stop = max(stop if stop is not None else -1, h[t]-trail_atr*a[t])
            curve[t] = cash + units*c[t]
        else:
            if ent[t-1]:
                px = o[t]*(1+slip); f = 1.0; stop = None
                if init_atr and not np.isnan(a[t-1]):
                    stop = px - init_atr*a[t-1]
                    if size_risk: f = min(1.0, size_risk/max((init_atr*a[t-1])/px, 1e-9))
                units = eq*f*(1-fee)/px; cash = eq*(1-f); entry_eq = eq; entry_t = t; pos = True
                if stop is not None and l[t] <= stop: close_at(min(o[t], stop), t)
                else: curve[t] = cash + units*c[t]
            else: curve[t] = eq
    return pd.Series(curve, index=df.index), trades

def metrics(curve, bpy, trades=None):
    curve = curve.dropna()
    if len(curve) < 30: return {}
    r = curve.pct_change().dropna()
    years = len(curve)/bpy
    tot = curve.iloc[-1]/curve.iloc[0]
    dd = (curve/curve.cummax()-1).min()
    out = {"ret%": 100*(tot-1), "cagr%": 100*(tot**(1/years)-1) if tot>0 else -100, "maxDD%": 100*dd,
           "sharpe": (r.mean()/r.std()*np.sqrt(bpy)) if r.std()>0 else 0.0}
    if trades is not None:
        rets = np.array([x[2] for x in trades]) if trades else np.array([])
        out["trades"] = len(rets)
        out["win%"] = 100*(rets>0).mean() if len(rets) else 0
        out["avg_trade%"] = 100*rets.mean() if len(rets) else 0
        g = rets[rets>0].sum(); ls = -rets[rets<0].sum()
        out["PF"] = g/ls if ls>0 else np.inf if g>0 else 0
    return out

def portfolio(itv, sigfn, fee, slip, start, end, syms=SYMS, **kw):
    sigfns = sigfn if isinstance(sigfn, (list, tuple)) else [sigfn]
    """Cartera equiponderada de huchas (una por activo). Devuelve curva, métricas y operaciones."""
    curves = []; alltr = []; expo = []
    for s in syms:
        df = DATA[(itv, s)]
        cvs = []; trs = []
        for fn in sigfns:
            ent, ext = fn(df); cv1, tr1 = run(df, ent, ext, fee, slip, **kw); cvs.append(cv1); trs += tr1
        cv = sum(cvs)/len(cvs); tr = trs
        ts = df.index
        sl = (ts >= start) & (ts <= end)
        if sl.sum() < 50: continue
        cv_s = cv[sl]; cv_s = cv_s/cv_s.iloc[0]
        curves.append(cv_s)
        alltr += [x for x in tr if start <= ts[x[0]] <= end]
    if not curves: return None, {}, []
    idx = curves[0].index
    for cvv in curves[1:]: idx = idx.union(cvv.index)
    mat = pd.concat([cvv.reindex(idx).ffill().fillna(1.0) for cvv in curves], axis=1)
    pf = mat.mean(axis=1)
    return pf, metrics(pf, BARS_PER_YEAR[itv], alltr), alltr

def buy_hold(itv, start, end, syms=SYMS):
    curves = []
    for s in syms:
        df = DATA[(itv, s)]; sl = (df.index >= start) & (df.index <= end)
        if sl.sum() < 50: continue
        c = df.close[sl]; curves.append(c/c.iloc[0])
    idx = curves[0].index
    for cvv in curves[1:]: idx = idx.union(cvv.index)
    pf = pd.concat([x.reindex(idx).ffill().fillna(1.0) for x in curves], axis=1).mean(axis=1)
    return metrics(pf, BARS_PER_YEAR[itv])
