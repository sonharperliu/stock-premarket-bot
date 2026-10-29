"""建立掃描池：S&P 500 + 全球大型 ADR（合計約 550 檔，涵蓋全球前 500 大市值資產的絕大部分）+ 產業 ETF。"""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from .config import (CROSS_ASSETS, GLOBAL_EXTRA, HTTP_HEADERS, SECTOR_ETFS, THEME_ETFS,
                     TW_ETFS, TW_OTC, TW_UNIVERSE)

log = logging.getLogger(__name__)


def sp500() -> pd.DataFrame:
    """回傳 columns: ticker, name, sector。先抓 Wikipedia，失敗改用 GitHub datasets 鏡像。"""
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                            headers=HTTP_HEADERS, timeout=20).text
        t = pd.read_html(io.StringIO(html))[0]
        df = pd.DataFrame({"ticker": t["Symbol"], "name": t["Security"], "sector": t["GICS Sector"]})
    except Exception as e:
        log.warning("Wikipedia S&P500 失敗，改用備援：%s", e)
        t = pd.read_csv("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")
        df = pd.DataFrame({"ticker": t["Symbol"], "name": t.get("Security", t.get("Name")),
                           "sector": t.get("GICS Sector", t.get("Sector"))})
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    return df


def us_universe() -> tuple[pd.DataFrame, dict, dict]:
    stocks = sp500()
    extra = pd.DataFrame({"ticker": GLOBAL_EXTRA, "name": GLOBAL_EXTRA, "sector": "Global ADR"})
    stocks = pd.concat([stocks, extra]).drop_duplicates("ticker").reset_index(drop=True)
    etfs = {**SECTOR_ETFS, **THEME_ETFS, **CROSS_ASSETS}
    return stocks, etfs, SECTOR_ETFS


def tw_universe() -> tuple[pd.DataFrame, dict, dict]:
    rows = [{"ticker": f"{c}.TW", "name": n, "sector": "TWSE"} for c, n in TW_UNIVERSE.items()]
    rows += [{"ticker": f"{c}.TWO", "name": n, "sector": "TPEx"} for c, n in TW_OTC.items()]
    stocks = pd.DataFrame(rows)
    # 台股的「板塊」以美股 11 大產業 ETF + 費半作為輪動參考（台股是美科技供應鏈）
    sector_ref = {**SECTOR_ETFS, "SMH": "半導體"}
    return stocks, {**TW_ETFS}, sector_ref
