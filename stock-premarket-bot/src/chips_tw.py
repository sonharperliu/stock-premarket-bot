"""台股籌碼面 + 基本面（官方公開資料，免費）：
- 證交所 T86：三大法人買賣超（近 5 個交易日）
- 證交所 MI_MARGN：融資融券餘額增減
- 證交所 BWIBBU_ALL：本益比 / 殖利率 / 股價淨值比
- 證交所 t187ap05_L：上市公司月營收（MOPS 每月 10 日前公布的自結營收）
- 集保 TDCC 1-5：股權分散表（千張大戶 vs 散戶持股比）
上櫃（.TWO）股票的法人/融資資料，證交所端點不涵蓋，會標示為缺。
"""
from __future__ import annotations

import io
import logging
import time
from datetime import date

import pandas as pd
import requests

from .config import HTTP_HEADERS
from .scheduler import previous_trading_day

log = logging.getLogger(__name__)
S = requests.Session()
S.headers.update(HTTP_HEADERS)


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def _field(fields: list[str], *preds) -> int:
    """依序用多個條件找欄位位置；證交所偶爾改欄位名稱，找不到就丟出可讀的錯誤。"""
    for pred in preds:
        for i, f in enumerate(fields):
            if pred(f):
                return i
    raise KeyError(f"T86 欄位格式改變：{fields}")


def _twse(path: str, d: date, **params) -> list[tuple[list, list]]:
    r = S.get(f"https://www.twse.com.tw/rwd/zh/{path}", params={"date": f"{d:%Y%m%d}", "response": "json", **params},
              timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") not in ("OK", None) and "tables" not in j:
        return []
    if "tables" in j:
        return [(t.get("fields", []), t.get("data", [])) for t in j["tables"] if t.get("data")]
    return [(j.get("fields", []), j.get("data", []))]


def institutional(codes: set[str], last_day: date, days: int = 5) -> dict[str, dict]:
    out = {c: {"foreign": [], "trust": [], "dealer": []} for c in codes}
    d, got, fails = last_day, 0, 0
    for _ in range(days + 4):          # 最多嘗試 days+4 次，避免證交所擋 IP 時無限迴圈
        if got >= days or fails >= 3:
            break
        try:
            tables = _twse("fund/T86", d, selectType="ALLBUT0999")
            if tables:
                fields, data = tables[0]
                fi = _field(fields, lambda f: "外陸資買賣超股數" in f and "不含" in f, lambda f: "外資" in f and "買賣超" in f)
                ti = _field(fields, lambda f: f.startswith("投信買賣超"))
                di = _field(fields, lambda f: f.startswith("自營商買賣超股數") and "(" not in f,
                            lambda f: f.startswith("自營商買賣超"))
                for row in data:
                    c = str(row[0]).strip()
                    if c in out:
                        out[c]["foreign"].append(_num(row[fi]) or 0)
                        out[c]["trust"].append(_num(row[ti]) or 0)
                        out[c]["dealer"].append(_num(row[di]) or 0)
                got += 1
        except Exception as e:
            fails += 1
            log.warning("T86 %s 失敗：%s", d, e)
        d = previous_trading_day("tw", d)
        time.sleep(2.5)   # 證交所有頻率限制
    res = {}
    for c, v in out.items():
        if v["foreign"]:
            res[c] = {"foreign_net_lots_last": round(v["foreign"][0] / 1000), "foreign_net_lots_5d": round(sum(v["foreign"]) / 1000),
                      "trust_net_lots_last": round(v["trust"][0] / 1000), "trust_net_lots_5d": round(sum(v["trust"]) / 1000),
                      "dealer_net_lots_5d": round(sum(v["dealer"]) / 1000),
                      "foreign_buy_days_of_5": sum(1 for x in v["foreign"] if x > 0)}
    return res


def margin(codes: set[str], last_day: date) -> dict[str, dict]:
    try:
        tables = _twse("marginTrading/MI_MARGN", last_day, selectType="ALL")
    except Exception as e:
        log.warning("融資融券失敗：%s", e)
        return {}
    res = {}
    for fields, data in tables:
        if not data or len(data[0]) < 13:
            continue
        for row in data:
            c = str(row[0]).strip()
            if c in codes:
                mp, mt, sp, st = _num(row[5]), _num(row[6]), _num(row[11]), _num(row[12])
                res[c] = {"margin_balance_lots": mt, "margin_change_lots": (mt - mp) if mt is not None and mp is not None else None,
                          "short_balance_lots": st, "short_change_lots": (st - sp) if st is not None and sp is not None else None,
                          "short_to_margin_ratio_pct": round(st / mt * 100, 2) if mt and st is not None else None}
    return res


def valuation_and_revenue(codes: set[str]) -> dict[str, dict]:
    res: dict[str, dict] = {c: {} for c in codes}
    try:
        for r in S.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=20).json():
            if r.get("Code") in res:
                res[r["Code"]].update({"pe": _num(r.get("PEratio")), "pb": _num(r.get("PBratio")),
                                       "dividend_yield_pct": _num(r.get("DividendYield"))})
    except Exception as e:
        log.warning("BWIBBU 失敗：%s", e)
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap05_O"):
        try:
            for r in S.get(url, timeout=20).json():
                c = r.get("公司代號")
                if c in res:
                    res[c].update({
                        "revenue_month": r.get("資料年月"),
                        "revenue_mom_pct": _num(r.get("營業收入-上月比較增減(%)")),
                        "revenue_yoy_pct": _num(r.get("營業收入-去年同月增減(%)")),
                        "revenue_ytd_yoy_pct": _num(r.get("累計營業收入-前期比較增減(%)")),
                    })
        except Exception as e:
            log.warning("月營收 %s 失敗：%s", url, e)
    return res


def shareholder_distribution(codes: set[str]) -> dict[str, dict]:
    """集保股權分散：分級 15 = 1,000 張以上大戶；分級 1~8 = 50 張以下散戶。"""
    try:
        r = S.get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", timeout=40)
        df = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig")))
        df.columns = ["date", "code", "level", "holders", "shares", "pct"]
        df["code"] = df["code"].astype(str).str.strip()
        df = df[df["code"].isin(codes)]
        res = {}
        for c, g in df.groupby("code"):
            g = g.set_index("level")
            res[c] = {"data_date": str(g["date"].iloc[0]),
                      "whale_1000lots_pct": round(float(g.loc[15, "pct"]), 2) if 15 in g.index else None,
                      "retail_under50lots_pct": round(float(g.loc[[i for i in range(1, 9) if i in g.index], "pct"].sum()), 2),
                      "total_holders": int(g.loc[17, "holders"]) if 17 in g.index else None}
        return res
    except Exception as e:
        log.warning("集保股權分散失敗：%s", e)
        return {}


def _safe(fn, *a) -> dict:
    try:
        return fn(*a)
    except Exception as e:
        log.warning("%s 失敗：%s", fn.__name__, e)
        return {}


def collect(codes: set[str], today: date) -> dict[str, dict]:
    last = previous_trading_day("tw", today)
    inst = _safe(institutional, codes, last)
    mar = _safe(margin, codes, last)
    val = _safe(valuation_and_revenue, codes)
    dist = _safe(shareholder_distribution, codes)
    return {c: {"institutional": inst.get(c, "N/A（上櫃或無資料）"), "margin": mar.get(c, "N/A"),
                "valuation_revenue": val.get(c, {}), "shareholder_distribution": dist.get(c, {}),
                "data_as_of": last.isoformat()} for c in codes}
