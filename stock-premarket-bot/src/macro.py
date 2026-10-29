"""總體經濟與全球流動性：FRED、盤前期貨/跨資產、Fed 利率期貨隱含機率、本週重磅經濟日曆。"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from .config import FRED_API_KEY, FRED_SERIES, HTTP_HEADERS, MARKET_TICKERS

log = logging.getLogger(__name__)


def _fred_series(sid: str, timeout: int = 10) -> pd.Series:
    if FRED_API_KEY:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": sid, "api_key": FRED_API_KEY, "file_type": "json",
                                 "sort_order": "desc", "limit": 30}, timeout=timeout)
        r.raise_for_status()
        obs = r.json()["observations"]
        s = pd.Series({o["date"]: o["value"] for o in obs})
    else:  # 免金鑰公開 CSV
        start = (datetime.utcnow() - timedelta(days=900)).strftime("%Y-%m-%d")
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": sid, "cosd": start}, headers=HTTP_HEADERS, timeout=timeout)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        s = pd.Series(df.iloc[:, 1].values, index=df.iloc[:, 0].values)
    s = pd.to_numeric(s, errors="coerce").dropna().sort_index()
    return s


def fred_dashboard() -> dict:
    """先用一個序列探測連線（熔斷），可連線才平行抓全部；連不到就整組跳過，不浪費時間。"""
    from concurrent.futures import ThreadPoolExecutor
    try:
        _fred_series("DFF", timeout=8)
    except Exception as e:
        log.warning("FRED 無法連線，略過總經序列%s：%s",
                    "" if FRED_API_KEY else "（建議設定免費的 FRED_API_KEY）", e)
        if not FRED_API_KEY:
            try:  # 診斷：官方 API 端點是否可連（無金鑰會回 400，代表網路可達）
                r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                                 params={"series_id": "DFF", "file_type": "json"}, timeout=8)
                log.info("FRED 官方 API 可連線（HTTP %s）：設定 FRED_API_KEY 即可取得總經數據", r.status_code)
            except Exception as e2:
                log.info("FRED 官方 API 也無法連線：%s", e2)
        return {}

    def fetch(sid):
        try:
            return sid, _fred_series(sid)
        except Exception as e:
            log.warning("FRED %s 失敗：%s", sid, e)
            return sid, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        series = dict(ex.map(fetch, FRED_SERIES))
    out = {}
    for sid, (label, mode) in FRED_SERIES.items():
        s = series.get(sid)
        if s is None or len(s) < 2:
            continue
        try:
            last, prev = s.iloc[-1], s.iloc[-2]
            item = {"name": label, "date": s.index[-1], "value": round(float(last), 3), "prev": round(float(prev), 3)}
            if mode == "diff":
                item["change"] = round(float(last - prev), 2)
            elif mode == "mom":
                item["mom_pct"] = round(float((last / prev - 1) * 100), 2)
            elif mode == "yoy_m" and len(s) > 12:
                item["yoy_pct"] = round(float((last / s.iloc[-13] - 1) * 100), 2)
                item["prev_yoy_pct"] = round(float((prev / s.iloc[-14] - 1) * 100), 2)
            elif mode == "yoy_q" and len(s) > 4:
                item["yoy_pct"] = round(float((last / s.iloc[-5] - 1) * 100), 2)
            out[sid] = item
        except Exception as e:
            log.warning("FRED %s 計算失敗：%s", sid, e)
    # 淨流動性 = Fed 資產 - TGA - ON RRP（單位換算成十億）
    try:
        net = out["WALCL"]["value"] / 1000 - out["WTREGEN"]["value"] / 1000 - out["RRPONTSYD"]["value"]
        out["NET_LIQUIDITY"] = {"name": "美元淨流動性(十億) = Fed資產-TGA-RRP", "value": round(net, 1)}
    except Exception:
        pass
    return out


def market_snapshot() -> dict:
    out = {}
    try:
        raw = yf.download(list(MARKET_TICKERS), period="1mo", interval="1d", group_by="ticker",
                          auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        log.warning("市場指標下載失敗：%s", e)
        return out
    for t, name in MARKET_TICKERS.items():
        try:
            c = raw[t]["Close"].dropna()
            out[t] = {"name": name, "last": round(float(c.iloc[-1]), 3),
                      "chg_1d_pct": round(float((c.iloc[-1] / c.iloc[-2] - 1) * 100), 2),
                      "chg_5d_pct": round(float((c.iloc[-1] / c.iloc[-6] - 1) * 100), 2) if len(c) > 6 else None}
        except Exception:
            pass
    return out


def fed_expectation(fred: dict, mkt: dict) -> dict | None:
    """簡化版 FedWatch：用 30 天 Fed Funds 期貨（ZQ=F，100-價格=市場隱含平均利率）對比目前有效利率。
    CME FedWatch 無免費 API，此為粗估，僅供方向判斷。"""
    try:
        implied = 100 - mkt["ZQ=F"]["last"]
        effr = fred["DFF"]["value"] if "DFF" in fred else mkt["^IRX"]["last"]
        diff_bp = (implied - effr) * 100
        p_cut = max(0.0, min(1.0, -diff_bp / 25))
        p_hike = max(0.0, min(1.0, diff_bp / 25))
        return {"implied_rate_front_month": round(implied, 3), "effective_rate": effr,
                "implied_change_bp": round(diff_bp, 1),
                "approx_prob_cut_25bp": round(p_cut, 2), "approx_prob_hike_25bp": round(p_hike, 2),
                "note": "以近月 ZQ 期貨粗估（月內平均效應會稀釋機率），精確值請以 CME FedWatch 為準"}
    except Exception:
        return None


def econ_calendar(tz: str) -> list[dict]:
    """本週高/中影響經濟數據（ForexFactory 公開週曆 JSON），回傳今天與明天的美國事件。"""
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=HTTP_HEADERS, timeout=15)
        r.raise_for_status()
        zone = ZoneInfo(tz)
        today = datetime.now(zone).date()
        out = []
        for e in r.json():
            if e.get("country") not in ("USD", "CNY", "JPY", "EUR"):
                continue
            if e.get("impact") not in ("High", "Medium"):
                continue
            dt = datetime.fromisoformat(e["date"]).astimezone(zone)
            if 0 <= (dt.date() - today).days <= 1:
                out.append({"time_local": dt.strftime("%m-%d %H:%M"), "country": e["country"], "event": e["title"],
                            "impact": e["impact"], "forecast": e.get("forecast"), "previous": e.get("previous")})
        return out
    except Exception as e:
        log.warning("經濟日曆失敗：%s", e)
        return []


def collect(tz: str) -> dict:
    fred = fred_dashboard()
    mkt = market_snapshot()
    return {"fred": fred, "markets": mkt, "fed_expectation": fed_expectation(fred, mkt),
            "econ_calendar_today_tomorrow": econ_calendar(tz)}
