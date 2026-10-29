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
                item["qoq_saar_pct"] = round(float(((last / prev) ** 4 - 1) * 100), 2)   # 市場慣用的季增年化率
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


_MONTH_CODE = "FGHJKMNQUVXZ"


def _zq_symbols(n: int = 3) -> list[tuple[str, str]]:
    """未來 n 個月的 30 天 Fed Funds 期貨合約代號，例如 ZQZ26.CBT（2026 年 12 月）。"""
    today = datetime.utcnow().date()
    out = []
    for k in range(1, n + 1):
        y, m = today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1
        out.append((f"{y}-{m:02d}", f"ZQ{_MONTH_CODE[m - 1]}{y % 100:02d}.CBT"))
    return out


def fed_expectation(fred: dict, mkt: dict) -> dict | None:
    """簡化版 FedWatch：用未來 1~3 個月的 30 天 Fed Funds 期貨（隱含利率 = 100 - 價格）對比目前有效利率。
    近月合約在月底幾乎等於現行利率、沒有資訊量，所以改看次月與次次月。CME FedWatch 無免費 API，此為粗估。"""
    if "DFF" in fred:
        effr, src = fred["DFF"]["value"], "FRED 有效聯邦基金利率"
    elif "^IRX" in mkt:
        effr, src = mkt["^IRX"]["last"], "13 週國庫券殖利率（近似）"
    else:
        return None
    curve = []
    try:
        syms = _zq_symbols()
        raw = yf.download([s for _, s in syms], period="5d", interval="1d", group_by="ticker",
                          auto_adjust=True, progress=False, threads=True)
        for month, sym in syms:
            try:
                px = float(raw[sym]["Close"].dropna().iloc[-1])
            except Exception:
                continue
            implied = 100 - px
            diff = (implied - effr) * 100
            curve.append({"month": month, "contract": sym, "implied_rate": round(implied, 3),
                          "vs_current_bp": round(diff, 1), "expected_25bp_moves": round(diff / 25, 2)})
    except Exception as e:
        log.warning("Fed Funds 期貨曲線失敗：%s", e)
    if not curve and "ZQ=F" in mkt:   # 備援：連續近月合約
        implied = 100 - mkt["ZQ=F"]["last"]
        diff = (implied - effr) * 100
        curve.append({"month": "front", "contract": "ZQ=F", "implied_rate": round(implied, 3),
                      "vs_current_bp": round(diff, 1), "expected_25bp_moves": round(diff / 25, 2)})
    if not curve:
        return None
    far = curve[-1]["expected_25bp_moves"]
    return {"current_rate": effr, "current_rate_source": src, "futures_curve": curve,
            "market_pricing": ("偏向降息" if far <= -0.3 else "偏向升息" if far >= 0.3 else "預期按兵不動"),
            "note": "expected_25bp_moves 為負代表市場預期降息次數（例如 -0.6 ≈ 6 成機率降 1 碼）。粗估值，精確值以 CME FedWatch 為準"}


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
