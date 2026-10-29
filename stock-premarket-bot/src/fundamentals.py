"""個股基本面 + 護城河量化指標（yfinance 財報）+ Finviz 快照（美股）。"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from .config import HTTP_HEADERS

log = logging.getLogger(__name__)

INFO_KEYS = ["longName", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "priceToBook",
             "enterpriseToEbitda", "returnOnEquity", "returnOnAssets", "grossMargins", "operatingMargins",
             "profitMargins", "revenueGrowth", "earningsGrowth", "freeCashflow", "totalRevenue", "debtToEquity",
             "currentRatio", "dividendYield", "beta", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
             "recommendationKey", "recommendationMean", "numberOfAnalystOpinions", "heldPercentInstitutions",
             "heldPercentInsiders", "shortPercentOfFloat", "shortRatio", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"]


def _row(df: pd.DataFrame, *names):
    for n in names:
        if df is not None and n in df.index:
            return df.loc[n]
    return None


def moat_metrics(tk: yf.Ticker) -> dict:
    """護城河代理指標：毛利率水準與穩定度、ROIC、研發強度、FCF 利潤率、營收 CAGR。"""
    out = {}
    try:
        inc, bal, cf = tk.income_stmt, tk.balance_sheet, tk.cashflow
        rev = _row(inc, "Total Revenue")
        gp = _row(inc, "Gross Profit")
        ebit = _row(inc, "EBIT", "Operating Income")
        rnd = _row(inc, "Research And Development")
        tax_rate = _row(inc, "Tax Rate For Calcs")
        debt = _row(bal, "Total Debt")
        eq = _row(bal, "Stockholders Equity", "Common Stock Equity")
        cash = _row(bal, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")
        fcf = _row(cf, "Free Cash Flow")
        if rev is not None and gp is not None:
            gm = (gp / rev).dropna()
            out["gross_margin_by_year"] = [round(float(x) * 100, 1) for x in gm.values[:4]]
            out["gross_margin_std_pp"] = round(float(gm.std() * 100), 2) if len(gm) > 1 else None
        if rev is not None and len(rev.dropna()) >= 3:
            r = rev.dropna().values
            yrs = len(r) - 1
            out["revenue_cagr_pct"] = round(float((r[0] / r[-1]) ** (1 / yrs) - 1) * 100, 1) if r[-1] > 0 else None
        if ebit is not None and eq is not None:
            t = float(tax_rate.iloc[0]) if tax_rate is not None and not pd.isna(tax_rate.iloc[0]) else 0.21
            ic = (debt.fillna(0) if debt is not None else 0) + eq - (cash.fillna(0) if cash is not None else 0)
            roic = (ebit * (1 - t) / ic).replace([np.inf, -np.inf], np.nan).dropna()
            out["roic_by_year_pct"] = [round(float(x) * 100, 1) for x in roic.values[:4]]
        if rnd is not None and rev is not None:
            out["rnd_intensity_pct"] = round(float(rnd.iloc[0] / rev.iloc[0] * 100), 1)
        if fcf is not None and rev is not None:
            out["fcf_margin_pct"] = round(float(fcf.iloc[0] / rev.iloc[0] * 100), 1)
    except Exception as e:
        log.debug("moat 失敗 %s", e)
    return out


def finviz_snapshot(ticker: str) -> dict:
    """Finviz 個股快照（機構持股、空單比、內部人交易、分析師目標價、財報日）。網頁抓取，失敗不影響主流程。"""
    try:
        from bs4 import BeautifulSoup
        r = requests.get("https://finviz.com/quote.ashx", params={"t": ticker, "p": "d"},
                         headers=HTTP_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        soup = BeautifulSoup(r.text, "lxml")
        tbl = soup.select_one("table.snapshot-table2")
        if not tbl:
            return {}
        cells = [td.get_text(strip=True) for td in tbl.find_all("td")]
        kv = dict(zip(cells[0::2], cells[1::2]))
        keep = ["Inst Own", "Inst Trans", "Insider Own", "Insider Trans", "Short Float", "Short Ratio",
                "Target Price", "Recom", "Earnings", "Perf Week", "Perf Month", "Perf YTD", "EPS next Y",
                "EPS next 5Y", "Sales Q/Q", "EPS Q/Q", "PEG", "ROIC", "Rel Volume"]
        time.sleep(0.8)
        return {k: kv[k] for k in keep if k in kv}
    except Exception:
        return {}


def earnings_date(tk: yf.Ticker):
    try:
        cal = tk.calendar
        d = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if d:
            return str(d[0] if isinstance(d, list) else d)
    except Exception:
        pass
    return None


def collect(ticker: str, us: bool) -> dict:
    tk = yf.Ticker(ticker)
    out: dict = {}
    try:
        info = tk.info or {}
        out["info"] = {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None}
    except Exception as e:
        log.warning("info 失敗 %s：%s", ticker, e)
    out["moat"] = moat_metrics(tk)
    out["next_earnings"] = earnings_date(tk)
    if us:
        out["finviz"] = finviz_snapshot(ticker)
    return out
