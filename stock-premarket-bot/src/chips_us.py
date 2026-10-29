"""美股籌碼面（免費替代方案）：
- 選擇權異動：yfinance 選擇權鏈，找出 成交量 > 3×未平倉 且 量 ≥ 1000 口 的大單合約、Put/Call 比
- 場外 / 暗池代理：FINRA Reg SHO 每日空單成交量（CNMS 檔，含 TRF 場外成交）
- Unusual Whales / Cheddar Flow 為付費 API，可自行在 unusual_whales() 接上
"""
from __future__ import annotations

import io
import logging
import os
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

from .config import HTTP_HEADERS

log = logging.getLogger(__name__)


def options_flow(ticker: str, expiries: int = 2) -> dict:
    try:
        tk = yf.Ticker(ticker)
        exps = tk.options[:expiries]
        if not exps:
            return {}
        cv = pv = coi = poi = 0
        unusual = []
        for e in exps:
            ch = tk.option_chain(e)
            for side, df in (("CALL", ch.calls), ("PUT", ch.puts)):
                df = df.fillna(0)
                vol, oi = df["volume"].sum(), df["openInterest"].sum()
                if side == "CALL":
                    cv, coi = cv + vol, coi + oi
                else:
                    pv, poi = pv + vol, poi + oi
                hot = df[(df["volume"] >= 1000) & (df["volume"] > 3 * df["openInterest"].clip(lower=1))]
                for _, r in hot.nlargest(3, "volume").iterrows():
                    unusual.append({"side": side, "exp": e, "strike": float(r["strike"]), "volume": int(r["volume"]),
                                    "oi": int(r["openInterest"]), "iv": round(float(r["impliedVolatility"]), 3),
                                    "premium_usd_est": int(r["volume"] * r["lastPrice"] * 100)})
        return {"call_volume": int(cv), "put_volume": int(pv), "put_call_vol_ratio": round(pv / cv, 2) if cv else None,
                "put_call_oi_ratio": round(poi / coi, 2) if coi else None,
                "unusual_contracts": sorted(unusual, key=lambda x: -x["premium_usd_est"])[:5]}
    except Exception as e:
        log.debug("options %s 失敗：%s", ticker, e)
        return {}


_finra_cache: pd.DataFrame | None = None


def finra_short_volume(ticker: str, last_day: date) -> dict:
    global _finra_cache
    if _finra_cache is None:
        _finra_cache = pd.DataFrame()
        d = last_day
        for _ in range(6):
            url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
            try:
                r = requests.get(url, headers=HTTP_HEADERS, timeout=20)
                if r.status_code == 200 and len(r.text) > 1000:
                    _finra_cache = pd.read_csv(io.StringIO(r.text), sep="|").set_index("Symbol")
                    _finra_cache.attrs["date"] = d.isoformat()
                    break
            except Exception:
                pass
            d -= timedelta(days=1)
    try:
        row = _finra_cache.loc[ticker.replace("-", ".")]
        return {"date": _finra_cache.attrs.get("date"),
                "short_volume_ratio": round(float(row["ShortVolume"]) / float(row["TotalVolume"]), 3),
                "off_exchange_total_volume": int(row["TotalVolume"])}
    except Exception:
        return {}


def unusual_whales(ticker: str) -> dict:
    """付費擴充點：設定 UW_API_KEY 後，依 Unusual Whales 官方文件填入端點即可。"""
    if not os.getenv("UW_API_KEY"):
        return {}
    return {}


def collect(ticker: str, last_day: date) -> dict:
    return {"options": options_flow(ticker), "finra_short_volume": finra_short_volume(ticker, last_day),
            **({"unusual_whales": uw} if (uw := unusual_whales(ticker)) else {})}
