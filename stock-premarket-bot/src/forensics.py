"""財報體檢：怪老子財報分析六步驟 + 法務會計（Beneish M-Score、自由現金流品質、六大操縱維度紅旗）。

全部由程式從 yfinance 年報（最近 4 個會計年度）計算，結果是「可驗算的數字 + 紅旗清單 + 一行摘要」，
再交給 Claude 交叉驗證。資料缺漏時明確標示「以中性值替代 / 不足」，不會捏造。

注意：Beneish M-Score、流動比率、存貨周轉等指標不適用於銀行、保險、證券等金融業，金融業僅計算 ROE/ROA 與權益乘數。
"""
from __future__ import annotations

import logging
import math

import pandas as pd

log = logging.getLogger(__name__)

# yfinance 科目名稱（美股 / 台股欄位略有差異，依序嘗試）
F = {
    "rev": ["Total Revenue", "Operating Revenue"],
    "cogs": ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
    "gp": ["Gross Profit"],
    "op": ["Operating Income", "Total Operating Income As Reported"],
    "pretax": ["Pretax Income"],
    "ni": ["Net Income Continuous Operations", "Net Income", "Net Income Common Stockholders"],
    "tax": ["Tax Provision"],
    "int": ["Interest Expense", "Interest Expense Non Operating"],
    "sga": ["Selling General And Administration"],
    "dep": ["Reconciled Depreciation", "Depreciation And Amortization", "Depreciation Amortization Depletion"],
    "ta": ["Total Assets"],
    "ca": ["Current Assets"],
    "cl": ["Current Liabilities"],
    "tl": ["Total Liabilities Net Minority Interest"],
    "ncl": ["Total Non Current Liabilities Net Minority Interest"],
    "eq": ["Stockholders Equity", "Common Stock Equity"],
    "teq": ["Total Equity Gross Minority Interest"],
    "nci": ["Minority Interest"],
    "inv": ["Inventory"],
    "ar": ["Accounts Receivable", "Receivables"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "ppe": ["Net PPE"],
    "gw": ["Goodwill"],
    "intang": ["Other Intangible Assets"],
    "gwi": ["Goodwill And Other Intangible Assets"],
    "std": ["Current Debt", "Current Debt And Capital Lease Obligation"],
    "ltd": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "ap": ["Accounts Payable", "Payables"],
    "prepaid": ["Prepaid Assets"],
    "sec": ["Investments And Advances", "Long Term Equity Investment", "Investmentin Financial Assets",
            "Available For Sale Securities", "Other Short Term Investments"],
    "ocf": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure", "Purchase Of PPE"],
    "icf": ["Investing Cash Flow", "Cash Flow From Continuing Investing Activities"],
    "div": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    "d_ap": ["Change In Payable", "Change In Payables And Accrued Expense", "Change In Account Payable"],
}


def _v(df: pd.DataFrame | None, key: str, i: int = 0) -> float | None:
    """取第 i 個會計年度（0 = 最新）的科目值；找不到或為 NaN 回傳 None。"""
    if df is None or df.empty or i >= df.shape[1]:
        return None
    for name in F[key]:
        if name in df.index:
            x = df.loc[name].iloc[i]
            try:
                x = float(x)
            except Exception:
                continue
            if not math.isnan(x):
                return x
    return None


def _div(a, b):
    try:
        return None if a is None or b in (None, 0) else a / b
    except Exception:
        return None


def _avg(df, key, i=0):
    a, b = _v(df, key, i), _v(df, key, i + 1)
    return (a + b) / 2 if a is not None and b is not None else a


def _pct(x, nd=1):
    return None if x is None else round(x * 100, nd)


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def is_financial(sector: str | None, industry: str | None, ticker: str) -> bool:
    s = f"{sector or ''} {industry or ''}".lower()
    if any(k in s for k in ("financial services", "bank", "insurance", "capital markets", "credit services")):
        return True
    code = ticker.split(".")[0]
    return ticker.endswith((".TW", ".TWO")) and code.isdigit() and (code.startswith("28") or code in ("5880", "2801"))


def dupont(inc, bal, i=0) -> dict:
    ni, rev = _v(inc, "ni", i), _v(inc, "rev", i)
    avg_ta, avg_eq = _avg(bal, "ta", i), _avg(bal, "eq", i)
    npm, at, em = _div(ni, rev), _div(rev, avg_ta), _div(avg_ta, avg_eq)
    return {"roe": _div(ni, avg_eq), "roa": _div(ni, avg_ta), "net_margin": npm, "asset_turnover": at,
            "equity_multiplier": em}


def beneish(inc, bal, cf) -> dict:
    """Beneish M-Score 八指標（t = 最新年度、t-1 = 前一年度）。缺資料的指標以中性值替代並註明。"""
    subs, idx = [], {}

    def ratio(name, val, neutral):
        if val is None:
            subs.append(name)
            idx[name] = neutral
        else:
            idx[name] = round(val, 3)

    rev0, rev1 = _v(inc, "rev", 0), _v(inc, "rev", 1)
    ar0, ar1 = _v(bal, "ar", 0), _v(bal, "ar", 1)
    ratio("DSRI", _div(_div(ar0, rev0), _div(ar1, rev1)), 1.0)
    gm0 = _div(_v(inc, "gp", 0) or ((rev0 - _v(inc, "cogs", 0)) if rev0 and _v(inc, "cogs", 0) else None), rev0)
    gm1 = _div(_v(inc, "gp", 1) or ((rev1 - _v(inc, "cogs", 1)) if rev1 and _v(inc, "cogs", 1) else None), rev1)
    ratio("GMI", _div(gm1, gm0) if gm0 and gm0 > 0 and gm1 and gm1 > 0 else None, 1.0)

    def aq(i):
        ta, ca, ppe = _v(bal, "ta", i), _v(bal, "ca", i), _v(bal, "ppe", i)
        if ta is None or ca is None or ppe is None:
            return None
        return 1 - (ca + ppe + (_v(bal, "sec", i) or 0)) / ta
    ratio("AQI", _div(aq(0), aq(1)) if aq(1) not in (None, 0) else None, 1.0)
    ratio("SGI", _div(rev0, rev1), 1.0)

    def deprate(i):
        d, p = _v(inc, "dep", i) or _v(cf, "dep", i), _v(bal, "ppe", i)
        return _div(d, (d + p)) if d is not None and p is not None else None
    ratio("DEPI", _div(deprate(1), deprate(0)), 1.0)
    ratio("SGAI", _div(_div(_v(inc, "sga", 0), rev0), _div(_v(inc, "sga", 1), rev1)), 1.0)

    def lev(i):
        ta, cl = _v(bal, "ta", i), _v(bal, "cl", i)
        return _div((cl or 0) + (_v(bal, "ltd", i) or 0), ta) if ta and cl is not None else None
    ratio("LVGI", _div(lev(0), lev(1)), 1.0)
    ni0, ocf0, ta0 = _v(inc, "ni", 0), _v(cf, "ocf", 0), _v(bal, "ta", 0)
    ratio("TATA", _div(ni0 - ocf0, ta0) if ni0 is not None and ocf0 is not None else None, 0.0)

    m = (-4.84 + 0.920 * idx["DSRI"] + 0.528 * idx["GMI"] + 0.404 * idx["AQI"] + 0.892 * idx["SGI"]
         + 0.115 * idx["DEPI"] - 0.172 * idx["SGAI"] + 4.679 * idx["TATA"] - 0.327 * idx["LVGI"])
    zone = "🟢 低" if m < -2.22 else ("🟡 灰色地帶" if m <= -1.78 else "🔴 高度操縱嫌疑")
    warn = {"DSRI": 1.031, "GMI": 1.014, "AQI": 1.039, "SGI": 1.134, "DEPI": 1.001, "LVGI": 1.037, "TATA": 0.018}
    alerts = [k for k, th in warn.items() if k not in subs and idx[k] > th]
    return {"indices": idx, "m_score": round(m, 2), "zone": zone, "over_threshold": alerts,
            "neutral_substituted": subs, "reliable": len(subs) <= 2}


def analyze(tk, sector: str | None = None, industry: str | None = None, ticker: str = "") -> dict:
    """回傳財報體檢結果。tk 為 yfinance.Ticker（沿用已下載的年報）。"""
    try:
        inc, bal, cf = tk.income_stmt, tk.balance_sheet, tk.cashflow
    except Exception as e:
        return {"available": False, "note": f"年報下載失敗：{e}"}
    if inc is None or bal is None or inc.empty or bal.empty or _v(inc, "rev") is None or _v(bal, "ta") is None:
        return {"available": False, "note": "無年度財報資料"}
    fin = is_financial(sector, industry, ticker)
    years = [str(c)[:10] for c in inc.columns[:3]]
    flags: list[tuple[str, str]] = []   # (燈號, 說明)

    # ---------- 步驟三：杜邦分析 ----------
    d0, d1 = dupont(inc, bal, 0), dupont(inc, bal, 1)
    roe, npm, at, em = d0["roe"], d0["net_margin"], d0["asset_turnover"], d0["equity_multiplier"]
    driver = None
    if roe is not None and npm is not None and at is not None and em is not None:
        driver = ("高權益乘數型（財務槓桿）" if em >= 3 else
                  "高淨利率型（產品高附加價值）" if npm >= 0.15 else
                  "高資產周轉型（經營效率）" if at >= 1.2 else "均衡型")
    roe_cause = None
    if d0["roe"] is not None and d1["roe"] is not None and d0["roe"] < d1["roe"] - 0.02:
        ch = {"淨利率惡化": _div(d0["net_margin"], d1["net_margin"]), "資產周轉率下降": _div(d0["asset_turnover"], d1["asset_turnover"]),
              "權益乘數下降（降槓桿）": _div(d0["equity_multiplier"], d1["equity_multiplier"])}
        ch = {k: v for k, v in ch.items() if v is not None}
        if ch:
            roe_cause = min(ch, key=ch.get)
    dup = {"roe_pct": _pct(roe), "roe_prev_pct": _pct(d1["roe"]), "roa_pct": _pct(d0["roa"]), "net_margin_pct": _pct(npm),
           "asset_turnover": _r(at), "equity_multiplier": _r(em), "roe_driver": driver, "roe_decline_cause": roe_cause}
    if roe is not None and roe < 0.08:
        flags.append(("🟡", f"ROE {roe * 100:.1f}% 偏低（< 8%）"))

    out = {"available": True, "fiscal_years": years, "financial_sector": fin, "dupont": dup}
    if fin:
        out["note"] = "金融業：Beneish M-Score、流動比率、存貨/應收周轉不適用，僅看 ROE/ROA 與權益乘數"
        out["red_flags"] = [f"{a} {b}" for a, b in flags]
        out["verdict"] = "🟡" if flags else "🟢"
        out["summary_line"] = "｜".join(x for x in [
            f"ROE {dup['roe_pct']}%" if dup["roe_pct"] is not None else None,
            f"ROA {dup['roa_pct']}%" if dup["roa_pct"] is not None else None,
            f"權益乘數 {dup['equity_multiplier']}x" if dup["equity_multiplier"] is not None else None,
            "金融業(M-Score不適用)"] if x)
        return out

    # ---------- 步驟一：資產負債結構與地雷科目 ----------
    ta = _v(bal, "ta")
    tl = _v(bal, "tl") or ((ta - _v(bal, "teq")) if _v(bal, "teq") else None)
    ca, ppe, cash = _v(bal, "ca"), _v(bal, "ppe"), _v(bal, "cash")
    intang = _v(bal, "gwi") or ((_v(bal, "gw") or 0) + (_v(bal, "intang") or 0)) or None
    teq, nci = _v(bal, "teq"), _v(bal, "nci")
    debt_ratio = _div(tl, ta)
    ca_pct, ppe_pct = _div(ca, ta), _div(ppe, ta)
    btype = ("靠產能利用率創造營收型（看 PP&E 周轉率）" if (ppe_pct or 0) >= 0.4 else
             "靠流動資產周轉創造營收型（看存貨/應收周轉）" if (ca_pct or 0) >= 0.5 else "混合型")
    struct = {"debt_ratio_pct": _pct(debt_ratio), "current_assets_pct": _pct(ca_pct), "ppe_pct": _pct(ppe_pct),
              "cash_pct": _pct(_div(cash, ta)), "intangible_goodwill_pct": _pct(_div(intang, ta)),
              "non_controlling_interest_pct": _pct(_div(nci, teq)), "business_type": btype}
    if (_div(intang, ta) or 0) > 0.30:
        flags.append(("🔴", f"無形資產+商譽占總資產 {_div(intang, ta) * 100:.0f}%（> 30%，併購溢價/減損風險）"))
    elif (_div(intang, ta) or 0) > 0.05:
        flags.append(("🟡", f"無形資產+商譽占總資產 {_div(intang, ta) * 100:.0f}%（> 5%）"))
    if (_div(nci, teq) or 0) > 0.10:
        flags.append(("🟡", f"非控制權益占總權益 {_div(nci, teq) * 100:.0f}%（> 10%，合併數字高估母公司獲利）"))
    if (_div(cash, ta) or 0) > 0.5:
        flags.append(("🟡", f"現金占總資產 {_div(cash, ta) * 100:.0f}%（安全但拖累周轉率與 ROE）"))

    # ---------- 步驟二：損益結構與業外 ----------
    def margins(i):
        rev = _v(inc, "rev", i)
        gp = _v(inc, "gp", i)
        return {"gm": _div(gp, rev), "opm": _div(_v(inc, "op", i), rev), "ptm": _div(_v(inc, "pretax", i), rev),
                "npm": _div(_v(inc, "ni", i), rev)}
    m0, m1 = margins(0), margins(1)
    op0, pt0 = _v(inc, "op"), _v(inc, "pretax")
    nonop_share = _div((pt0 - op0), abs(pt0)) if op0 is not None and pt0 not in (None, 0) else None
    profit = {"gross_margin_pct": _pct(m0["gm"]), "gross_margin_prev_pct": _pct(m1["gm"]),
              "operating_margin_pct": _pct(m0["opm"]), "operating_margin_prev_pct": _pct(m1["opm"]),
              "pretax_margin_pct": _pct(m0["ptm"]), "net_margin_pct": _pct(m0["npm"]),
              "non_operating_share_of_pretax_pct": _pct(nonop_share)}
    if m0["gm"] is not None and m1["gm"] is not None and m0["opm"] is not None and m1["opm"] is not None \
            and m0["gm"] < m1["gm"] - 0.01 and m0["opm"] < m1["opm"] - 0.01:
        flags.append(("🟡", f"毛利率與營益率同步下滑（{m1['gm'] * 100:.1f}→{m0['gm'] * 100:.1f}%、"
                            f"{m1['opm'] * 100:.1f}→{m0['opm'] * 100:.1f}%），產品競爭力或成本控管轉弱"))
    if op0 is not None and op0 <= 0 and (pt0 or 0) > 0:
        flags.append(("🔴", "本業虧損但靠業外撐出稅前獲利"))
    elif (nonop_share or 0) > 0.30:
        flags.append(("🟡", f"業外占稅前淨利 {nonop_share * 100:.0f}%（需確認是否一次性，不可納入常態 EPS）"))
    if (pt0 or 0) < 0 and (_v(inc, "ni") or 0) > 0:
        flags.append(("🔴", "稅前虧損但靠所得稅利益使稅後轉正"))

    # ---------- 步驟三：周轉 ----------
    cogs, rev = _v(inc, "cogs"), _v(inc, "rev")
    inv_days = _div(365, _div(cogs, _avg(bal, "inv"))) if cogs and _avg(bal, "inv") else None
    ar_days = _div(365, _div(rev, _avg(bal, "ar"))) if _avg(bal, "ar") else None
    inv_days1 = _div(365, _div(_v(inc, "cogs", 1), _avg(bal, "inv", 1))) if _v(inc, "cogs", 1) and _avg(bal, "inv", 1) else None
    ar_days1 = _div(365, _div(_v(inc, "rev", 1), _avg(bal, "ar", 1))) if _avg(bal, "ar", 1) else None
    turn = {"inventory_days": _r(inv_days, 0), "inventory_days_prev": _r(inv_days1, 0),
            "receivable_days": _r(ar_days, 0), "receivable_days_prev": _r(ar_days1, 0),
            "ppe_turnover": _r(_div(rev, _avg(bal, "ppe")))}
    rev_g = _div(rev, _v(inc, "rev", 1))
    ar_g = _div(_v(bal, "ar"), _v(bal, "ar", 1))
    inv_g = _div(_v(bal, "inv"), _v(bal, "inv", 1))
    if rev_g and ar_g and ar_g - rev_g > 0.15:
        flags.append(("🟡", f"應收帳款成長 {(ar_g - 1) * 100:.0f}% 遠高於營收 {(rev_g - 1) * 100:.0f}%（塞貨/提前認列風險）"))
    if rev_g and inv_g and inv_g - rev_g > 0.20:
        flags.append(("🟡", f"存貨成長 {(inv_g - 1) * 100:.0f}% 遠高於營收 {(rev_g - 1) * 100:.0f}%（庫存積壓/跌價風險）"))

    # ---------- 步驟四：償債能力 ----------
    cl, inv, prepaid = _v(bal, "cl"), _v(bal, "inv") or 0, _v(bal, "prepaid") or 0
    std = _v(bal, "std") or 0
    ncl = _v(bal, "ncl") or ((tl - cl) if tl and cl else None)
    eq_total = teq or _v(bal, "eq")
    intexp = abs(_v(inc, "int") or 0)
    ebit = (pt0 + intexp) if pt0 is not None else None
    solv = {"current_ratio_pct": _pct(_div(ca, cl), 0), "quick_ratio_pct": _pct(_div((ca or 0) - inv - prepaid, cl), 0),
            "working_capital_positive": (ca - cl > 0) if ca is not None and cl is not None else None,
            "short_debt_to_liabilities_pct": _pct(_div(std, tl)),
            "long_capital_to_ppe": _r(_div((eq_total or 0) + (ncl or 0), ppe)) if ppe else None,
            "interest_coverage": _r(_div(ebit, intexp), 1) if intexp else None}
    cr, qr = _div(ca, cl), _div((ca or 0) - inv - prepaid, cl)
    if cr is not None and cr < 1:
        flags.append(("🔴", f"流動比率 {cr * 100:.0f}%（< 100%，營運資金為負）"))
    elif cr is not None and cr < 1.5:
        flags.append(("🟡", f"流動比率 {cr * 100:.0f}%（< 150%）"))
    if qr is not None and qr < 1 and (cr or 0) >= 1:
        flags.append(("🟡", f"速動比率 {qr * 100:.0f}%（< 100%）"))
    if (_div(std, tl) or 0) > 0.10:
        flags.append(("🟡", f"短期借款占負債 {_div(std, tl) * 100:.0f}%（> 10%，抽銀根風險）"))
    if solv["long_capital_to_ppe"] is not None and solv["long_capital_to_ppe"] < 1:
        flags.append(("🔴", f"長期資金占 PP&E {solv['long_capital_to_ppe']:.2f}（< 1，以短支長）"))
    if solv["interest_coverage"] is not None and solv["interest_coverage"] < 2:
        flags.append(("🔴", f"利息保障倍數 {solv['interest_coverage']}（< 2 倍）"))
    if (debt_ratio or 0) > 0.6:
        flags.append(("🟡", f"負債比 {debt_ratio * 100:.0f}%（權益乘數 {_div(1, 1 - debt_ratio):.1f} 倍）"))

    # ---------- 步驟五：現金流含金量 / FCF 品質 ----------
    ni, ocf = _v(inc, "ni"), _v(cf, "ocf")
    capex = abs(_v(cf, "capex") or 0)
    icf, div = _v(cf, "icf"), abs(_v(cf, "div") or 0)
    fcf = (ocf - capex) if ocf is not None else None
    ocf1, ni1 = _v(cf, "ocf", 1), _v(inc, "ni", 1)
    d_ap = _v(cf, "d_ap")
    cashq = {"ocf": _r(ocf / 1e6, 0) if ocf is not None else None, "capex": _r(capex / 1e6, 0),
             "fcf": _r(fcf / 1e6, 0) if fcf is not None else None, "unit": "百萬（財報幣別）",
             "ocf_to_net_income": _r(_div(ocf, ni)), "ocf_to_net_income_prev": _r(_div(ocf1, ni1)),
             "fcf_to_net_income": _r(_div(fcf, ni)),
             "fcf_after_all_investing": _r((ocf + icf) / 1e6, 0) if ocf is not None and icf is not None else None,
             "dividend_covered_by_fcf": (fcf >= div) if fcf is not None and div else None,
             "payables_share_of_ocf_pct": _pct(_div(d_ap, ocf)) if d_ap and ocf and ocf > 0 else None}
    if ni and ni > 0 and ocf is not None and ocf < 0:
        flags.append(("🔴", "帳面獲利但營業現金流為負（黑字倒閉風險）"))
    elif ni and ni > 0 and ocf is not None and _div(ocf, ni) < 0.8:
        sev = "🔴" if (ocf1 is not None and ni1 and _div(ocf1, ni1) < 0.8) else "🟡"
        flags.append((sev, f"淨利含金量 {_div(ocf, ni):.2f}（< 0.8{'，連續兩年' if sev == '🔴' else ''}）"))
    if d_ap and ocf and ocf > 0 and _div(d_ap, ocf) > 0.3:
        flags.append(("🟡", f"營業現金流有 {_div(d_ap, ocf) * 100:.0f}% 來自應付帳款增加（拖欠供應商美化 OCF）"))
    if fcf is not None and div and fcf < div:
        flags.append(("🟡", "自由現金流不足以支應現金股利"))

    # ---------- 法務會計：Beneish M-Score ----------
    bm = beneish(inc, bal, cf)
    if bm["reliable"] and bm["m_score"] > -1.78:
        flags.append(("🔴", f"Beneish M-Score {bm['m_score']}（> -1.78，超標：{','.join(bm['over_threshold']) or '綜合'}）"))
    elif bm["reliable"] and bm["m_score"] >= -2.22:
        flags.append(("🟡", f"Beneish M-Score {bm['m_score']}（灰色地帶）"))

    reds = sum(1 for s, _ in flags if s == "🔴")
    yellows = sum(1 for s, _ in flags if s == "🟡")
    verdict = "🔴" if reds else ("🟡" if yellows >= 2 else "🟢")
    out.update({
        "structure": struct, "profit_quality": profit, "turnover": turn, "solvency": solv, "cash_quality": cashq,
        "beneish": bm, "red_flags": [f"{s} {t}" for s, t in flags], "verdict": verdict,
        "summary_line": "｜".join(x for x in [
            f"ROE {dup['roe_pct']}%({(driver or '').split('（')[0]})" if dup["roe_pct"] is not None else None,
            f"OCF/淨利 {cashq['ocf_to_net_income']}" if cashq["ocf_to_net_income"] is not None else None,
            f"負債比 {struct['debt_ratio_pct']}%" if struct["debt_ratio_pct"] is not None else None,
            f"M {bm['m_score']}{bm['zone'][:1]}" if bm["reliable"] else "M-Score 資料不足",
        ] if x),
    })
    return out
