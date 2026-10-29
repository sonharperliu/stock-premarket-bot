"""第一層：廣度掃描。批次下載約 550 檔一年日線，計算量價、相對強弱（RS）與資金流，
排出最強勢與量價異動的候選名單，同時排出產業 ETF 輪動強弱。"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd
import yfinance as yf

from .technicals import cmf

log = logging.getLogger(__name__)


def download(tickers: list[str], period: str = "1y", chunk: int = 120) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        raw = None
        for attempt in range(3):  # Yahoo 限流時退避重試
            try:
                raw = yf.download(part, period=period, interval="1d", group_by="ticker", auto_adjust=True,
                                  threads=True, progress=False)
                if raw is not None and not raw.empty:
                    break
            except Exception as e:
                log.warning("下載失敗 %s...（第 %d 次）：%s", part[:3], attempt + 1, e)
            time.sleep(5 * (attempt + 1))
        if raw is None or raw.empty:
            continue
        for t in part:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
                if len(df) >= 60:
                    out[t] = df
            except Exception:
                pass
    log.info("價格資料：%d / %d 檔", len(out), len(tickers))
    return out


def metrics(df: pd.DataFrame, bench: pd.Series | None) -> dict:
    c, v = df["Close"], df["Volume"]
    last = c.iloc[-1]
    r = lambda n: (last / c.iloc[-n - 1] - 1) * 100 if len(c) > n else np.nan
    m = {
        "close": last, "ret1d": r(1), "ret5d": r(5), "ret20d": r(20), "ret60d": r(60),
        "vol_ratio": v.iloc[-1] / max(v.tail(21).iloc[:-1].mean(), 1),
        "dollar_vol": (c * v).tail(20).mean(),
        "cmf20": cmf(df).iloc[-1],
        "near_high": last / df["High"].tail(252).max(),   # 1.0 = 創 52 週新高
        "above_ma50": float(last > c.rolling(50).mean().iloc[-1]),
        "above_ma200": float(last > c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else 0.5,
        "gap_pct": (df["Open"].iloc[-1] / c.iloc[-2] - 1) * 100,
    }
    if bench is not None and len(bench) > 61:
        b = bench.reindex(c.index).ffill()
        m["rs20"] = m["ret20d"] - (b.iloc[-1] / b.iloc[-21] - 1) * 100
        m["rs60"] = m["ret60d"] - (b.iloc[-1] / b.iloc[-61] - 1) * 100
    else:
        m["rs20"], m["rs60"] = m["ret20d"], m["ret60d"]
    return m


def _z(s: pd.Series) -> pd.Series:
    s = s.astype(float).clip(s.quantile(0.02), s.quantile(0.98))
    return (s - s.mean()) / (s.std() or 1)


def score_table(prices: dict[str, pd.DataFrame], bench: pd.Series | None) -> pd.DataFrame:
    rows = []
    for t, df in prices.items():
        try:
            rows.append({"ticker": t, **metrics(df, bench)})
        except Exception:
            pass
    if not rows:
        return pd.DataFrame()
    tab = pd.DataFrame(rows).set_index("ticker")
    # 綜合強勢分數：相對強弱 + 量能 + 資金流 + 趨勢位置
    tab["score"] = (0.25 * _z(tab["rs20"]) + 0.20 * _z(tab["rs60"]) + 0.15 * _z(np.log(tab["vol_ratio"].clip(0.1)))
                    + 0.15 * _z(tab["cmf20"].fillna(0)) + 0.15 * _z(tab["near_high"])
                    + 0.05 * tab["above_ma50"] + 0.05 * tab["above_ma200"])
    # 異動旗標：爆量 + 大漲跌 / 跳空
    tab["unusual"] = (tab["vol_ratio"] >= 2) & ((tab["ret1d"].abs() >= 3) | (tab["gap_pct"].abs() >= 2))
    return tab.sort_values("score", ascending=False)


def pick_candidates(tab: pd.DataFrame, n: int, min_dollar_vol: float) -> pd.DataFrame:
    """強勢榜為主，保留約 1/4 名額給量價異動股。"""
    liquid = tab[(tab["dollar_vol"] >= min_dollar_vol) & (tab["close"] >= 5)]
    n_unusual = max(1, n // 4)
    strong = liquid.head(n - n_unusual)
    unusual = liquid[liquid["unusual"] & ~liquid.index.isin(strong.index)].sort_values("vol_ratio", ascending=False)
    picks = pd.concat([strong, unusual.head(n_unusual)])
    if len(picks) < n:
        picks = pd.concat([picks, liquid[~liquid.index.isin(picks.index)].head(n - len(picks))])
    return picks.head(n)


def sector_rotation(prices: dict[str, pd.DataFrame], names: dict[str, str], bench: pd.Series | None) -> list[dict]:
    tab = score_table({k: v for k, v in prices.items() if k in names}, bench)
    out = []
    for t, r in tab.iterrows():
        out.append({"etf": t, "sector": names.get(t, t), "ret1d": round(r.ret1d, 2), "ret5d": round(r.ret5d, 2),
                    "ret20d": round(r.ret20d, 2), "rs20_vs_bench": round(r.rs20, 2),
                    "vol_ratio": round(r.vol_ratio, 2), "cmf20_money_flow": round(r.cmf20, 3),
                    "rank_score": round(r.score, 2)})
    return out
