"""技術指標：ATR、RSI、MACD、均線、CMF 資金流、樞紐點、支撐壓力，以及 ATR 基準的買賣點。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def macd(c: pd.Series):
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    s = m.ewm(span=9, adjust=False).mean()
    return m, s, m - s


def cmf(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Chaikin Money Flow：> 0 代表資金淨流入。"""
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    mfm = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / rng
    mfv = mfm.fillna(0) * df["Volume"]
    return mfv.rolling(n).sum() / df["Volume"].rolling(n).sum().replace(0, np.nan)


def _r(x, nd=2):
    try:
        return None if x is None or pd.isna(x) else round(float(x), nd)
    except Exception:
        return None


def snapshot(df: pd.DataFrame) -> dict:
    """單一標的完整技術面快照（給第二層 / Claude 使用）。"""
    df = df.dropna(subset=["Close"])
    c = df["Close"]
    a = atr(df)
    m, s, hist = macd(c)
    last = c.iloc[-1]
    ma20, ma50, ma200 = c.rolling(20).mean().iloc[-1], c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1]
    std20 = c.rolling(20).std().iloc[-1]
    prev = df.iloc[-1]
    pivot = (prev["High"] + prev["Low"] + prev["Close"]) / 3
    out = {
        "close": _r(last), "atr14": _r(a.iloc[-1]), "atr_pct": _r(a.iloc[-1] / last * 100),
        "rsi14": _r(rsi(c).iloc[-1], 1), "macd": _r(m.iloc[-1], 3), "macd_signal": _r(s.iloc[-1], 3),
        "macd_hist_rising": bool(hist.iloc[-1] > hist.iloc[-2]) if len(hist) > 2 else None,
        "ma20": _r(ma20), "ma50": _r(ma50), "ma200": _r(ma200),
        "bb_upper": _r(ma20 + 2 * std20), "bb_lower": _r(ma20 - 2 * std20),
        "support_20d": _r(df["Low"].tail(20).min()), "resistance_20d": _r(df["High"].tail(20).max()),
        "high_52w": _r(df["High"].tail(252).max()), "low_52w": _r(df["Low"].tail(252).min()),
        "pivot": _r(pivot), "r1": _r(2 * pivot - prev["Low"]), "s1": _r(2 * pivot - prev["High"]),
        "cmf20": _r(cmf(df).iloc[-1], 3),
        "vol_ratio_20d": _r(df["Volume"].iloc[-1] / df["Volume"].tail(21).iloc[:-1].mean()),
        "ret_1d_pct": _r(c.pct_change().iloc[-1] * 100),
        "ret_5d_pct": _r((last / c.iloc[-6] - 1) * 100) if len(c) > 6 else None,
        "ret_20d_pct": _r((last / c.iloc[-21] - 1) * 100) if len(c) > 21 else None,
        "trend": ("多頭排列" if last > ma20 > ma50 > ma200 else
                  "空頭排列" if last < ma20 < ma50 < ma200 else "盤整/轉折"),
    }
    out["baseline_plan"] = atr_plan(out)
    return out


def atr_plan(t: dict, stop_mult: float = 1.5, rr: float = 2.0) -> dict | None:
    """ATR 基準做多計畫（給 Claude 交叉驗證 & 當作 LLM 失敗時的備援）。
    進場區：昨收 - 0.5ATR ~ 昨收（不追高）；停損：進場中位 - 1.5ATR 與 20 日低點取較高者（但至少 1ATR）；
    停利1 = 2R，停利2 = 3R 或 52 週高點。"""
    c, a = t.get("close"), t.get("atr14")
    if not c or not a:
        return None
    entry_hi, entry_lo = c, c - 0.5 * a
    mid = (entry_hi + entry_lo) / 2
    stop = mid - stop_mult * a
    sup = t.get("support_20d")
    if sup and mid - a > sup * 0.995 > stop:   # 20 日支撐在 1~1.5ATR 之間 → 放在支撐下方
        stop = sup * 0.995
    risk = mid - stop
    tp1 = mid + rr * risk
    tp2 = mid + 3 * risk
    return {"entry_low": round(entry_lo, 2), "entry_high": round(entry_hi, 2), "stop_loss": round(stop, 2),
            "take_profit_1": round(tp1, 2), "take_profit_2": round(tp2, 2),
            "risk_reward": round((tp1 - mid) / risk, 2) if risk > 0 else None}
