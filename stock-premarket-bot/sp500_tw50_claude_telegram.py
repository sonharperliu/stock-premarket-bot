"""S&P 500 / 台灣 50 盤前 AI 投研：守門員 + 三層分析 + Claude 買賣點 + Telegram 推播（主程式）。

子指令：
  gate  守門員（只用標準庫，安裝套件前執行）：印出台北/紐約時間、EDT/EST、判斷是否執行；
        若視窗已過且今日未推播 → 發 Telegram 漏發告警（每市場每日一次）
  run   執行分析與推播（休市日 → 發一次休市通知並結束）

用法：
  python sp500_tw50_claude_telegram.py gate --market us --state-dir /tmp/bot-state
  python sp500_tw50_claude_telegram.py run  --market us --state-dir /tmp/bot-state
  python sp500_tw50_claude_telegram.py run  --market tw --force              # 立即執行（略過視窗與休市檢查）
  python sp500_tw50_claude_telegram.py run  --market us --force --dry-run --no-llm   # 本機測試
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from src.config import MARKETS, PREP_MINUTES
from src.scheduler import check_window, clock_info, now_in, push_time  # noqa: F401

import warnings
warnings.filterwarnings("ignore")                       # yfinance / pandas 的 FutureWarning 會淹沒 log
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # 個別代號下載失敗已由程式處理
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
ROOT = Path(__file__).resolve().parent


# 量化模組（pandas / yfinance / anthropic…）延遲匯入，讓守門員在安裝套件前就能執行
analyzer = chips_tw = chips_us = fundamentals = macro = news = scanner = telegram = technicals = universe = None
is_trading_day = previous_trading_day = None
ANTHROPIC_API_KEY = DEEP_CANDIDATES = FINAL_PICKS = MIN_DOLLAR_VOLUME = None


def load_quant_modules() -> None:
    global analyzer, chips_tw, chips_us, fundamentals, macro, news, scanner, telegram, technicals, universe
    global is_trading_day, previous_trading_day, ANTHROPIC_API_KEY, DEEP_CANDIDATES, FINAL_PICKS, MIN_DOLLAR_VOLUME
    if analyzer is not None:
        return
    from src import analyzer as _a, chips_tw as _ct, chips_us as _cu, fundamentals as _f, macro as _m
    from src import news as _n, scanner as _s, telegram as _t, technicals as _te, universe as _u
    from src import config as _c, scheduler as _sch
    analyzer, chips_tw, chips_us, fundamentals, macro, news = _a, _ct, _cu, _f, _m, _n
    scanner, telegram, technicals, universe = _s, _t, _te, _u
    is_trading_day, previous_trading_day = _sch.is_trading_day, _sch.previous_trading_day
    ANTHROPIC_API_KEY, DEEP_CANDIDATES = _c.ANTHROPIC_API_KEY, _c.DEEP_CANDIDATES
    FINAL_PICKS, MIN_DOLLAR_VOLUME = _c.FINAL_PICKS, _c.MIN_DOLLAR_VOLUME


# ------------------------------------------------------------------ 推播狀態 / 告警（標準庫）

def read_state(state_dir: str | None, kind: str, market: str) -> str | None:
    """bot-state：last_sent_<market>.txt（最近推播日）、last_alert_<market>.txt（最近告警日）。"""
    if not state_dir:
        return None
    f = Path(state_dir) / f"last_{kind}_{market}.txt"
    return f.read_text().strip() if f.exists() else None


def mark(kind: str, market: str, day: str) -> None:
    """寫 reports/<kind>_<market>.txt，由 workflow 寫回 bot-state 分支。"""
    ROOT.joinpath("reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{kind}_{market}.txt").write_text(day)


def tg_alert(text: str, dry_run: bool = False) -> bool:
    """純文字 Telegram 告警（只用標準庫，守門員也能用）。"""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if dry_run or not (token and chat):
        print("[Telegram 告警（未送出：dry-run 或未設定金鑰）]\n" + text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            log.warning("Telegram 告警送出失敗（第 %d 次）：%s", attempt + 1, e)
            time.sleep(3)
    return False


def print_debug(market: str, extra: dict | None = None) -> None:
    ci = clock_info()
    lines = [f"台北時間：{ci['taipei']}", f"美東時間：{ci['new_york']}（{ci['ny_tz']}，{'夏令' if ci['ny_dst'] else '冬令'}）",
             f"UTC：{ci['utc']}", f"市場：{MARKETS[market]['name']}（{market}）"]
    for k, v in (extra or {}).items():
        lines.append(f"{k}：{v}")
    print("=" * 20 + " 除錯資訊 " + "=" * 20 + "\n" + "\n".join(lines) + "\n" + "=" * 50, flush=True)


def _sleep(seconds: float) -> None:
    """排程等待專用（測試時可單獨替換，不影響其他套件的 time.sleep）。"""
    time.sleep(seconds)


def safe(fn, default):
    """單一資料源失敗不影響整體流程。"""
    try:
        return fn()
    except Exception as e:
        log.warning("資料源失敗（略過）：%s: %s", type(e).__name__, e)
        return default


def r2(x, nd=2):
    try:
        return round(float(x), nd)
    except Exception:
        return None


def layer1(market: str):
    if market == "us":
        stocks, etfs, sector_names = universe.us_universe()
        bench_t = "SPY"
    else:
        stocks, etfs, sector_names = universe.tw_universe()
        bench_t = "0050.TW"
    extra = list(etfs) + list(sector_names) + [bench_t, "SPY"]
    prices = scanner.download(list(stocks["ticker"]) + sorted(set(extra) - set(stocks["ticker"])))
    bench = prices.get(bench_t, {}).get("Close") if bench_t in prices else None
    spy = prices["SPY"]["Close"] if "SPY" in prices else None

    stock_prices = {t: prices[t] for t in stocks["ticker"] if t in prices}
    if len(stock_prices) < 20:
        raise RuntimeError(f"只取得 {len(stock_prices)} 檔股價，Yahoo Finance 可能暫時限流，請稍後重跑")
    tab = scanner.score_table(stock_prices, bench)
    cands = scanner.pick_candidates(tab, DEEP_CANDIDATES[market], MIN_DOLLAR_VOLUME[market])
    sectors = scanner.sector_rotation(prices, sector_names, spy)
    etf_board = scanner.sector_rotation(prices, etfs, bench)
    meta = stocks.set_index("ticker")
    breadth = {"universe_size": len(tab), "pct_above_ma50": r2(tab["above_ma50"].mean() * 100, 1),
               "pct_above_ma200": r2(tab["above_ma200"].mean() * 100, 1),
               "advancers_pct": r2((tab["ret1d"] > 0).mean() * 100, 1),
               "unusual_volume_count": int(tab["unusual"].sum())}
    log.info("第一層完成：掃描 %d 檔，候選 %d 檔", len(tab), len(cands))
    return cands, prices, meta, sectors, etf_board, breadth


def layer2(market: str, cands, prices, meta, today) -> dict[str, dict]:
    us = market == "us"
    last_day = previous_trading_day(market, today)
    out: dict[str, dict] = {}

    def one(t):
        name = str(meta.loc[t, "name"]) if t in meta.index else t
        d = {"name": name, "sector": str(meta.loc[t, "sector"]) if t in meta.index else "",
             "scan": {k: r2(v, 3) for k, v in cands.loc[t].items() if k != "unusual"} | {"unusual": bool(cands.loc[t, "unusual"])},
             "technical": safe(lambda: technicals.snapshot(prices[t]), {})}
        try:
            d["fundamental"] = fundamentals.collect(t, us)
        except Exception as e:
            d["fundamental"] = {"error": str(e)}
        if us:
            d["chips"] = safe(lambda: chips_us.collect(t, last_day), {})
        d["news"] = news.collect(t, name, market)
        return t, d

    with ThreadPoolExecutor(max_workers=6 if us else 4) as ex:
        for t, d in ex.map(one, list(cands.index)):
            out[t] = d
    if not us:
        codes = {t.split(".")[0] for t in out}
        tw = safe(lambda: chips_tw.collect(codes, today), {})
        for t in out:
            out[t]["chips"] = tw.get(t.split(".")[0], {})
    log.info("第二層完成：%d 檔六維度資料", len(out))
    return out


def macro_line(mk: dict) -> str:
    keys = ["ES=F", "NQ=F", "^VIX", "^TNX", "DX-Y.NYB", "^SOX", "TWD=X"]
    parts = []
    for k in keys:
        if k in mk:
            flag = "⚠多日" if mk[k].get("data_warning") else ""
            parts.append(f"{mk[k]['name']} {mk[k]['last']:g}({mk[k]['chg_1d_pct']:+.1f}%{flag})")
    return " | ".join(parts)


def build_markdown(result: dict, market: str, stamp: str, cands: dict) -> str:
    m = MARKETS[market]
    md = [f"# {m['name']}盤前作戰報告 — {stamp}\n"]
    if result.get("report_markdown"):
        import re
        md.append(re.sub(r"\A\s*#\s[^\n]*\n", "", result["report_markdown"]))  # 去掉重複的大標題
    md.append("\n\n## 附錄：買賣點總表（程式驗算）\n")
    md.append("| # | 代號 | 名稱 | 進場區 | 停損 | 停利1 | 停利2 | R/R | ATR | 驗算 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for p in result.get("picks", []):
        md.append(f"| {p.get('rank')} | {p.get('ticker')} | {p.get('name', '')} | {p.get('entry_low')}–{p.get('entry_high')} | "
                  f"{p.get('stop_loss')} | {p.get('take_profit_1')} | {p.get('take_profit_2')} | {p.get('risk_reward')} | "
                  f"{p.get('atr14')} | {p.get('validation', '')} |")
    md.append("\n\n> 本報告由程式自動產生，僅供研究參考，不構成投資建議。投資有風險，請自行判斷並控制部位。")
    return "\n".join(md)


def gate(a) -> str:
    """守門員：回傳狀態並寫入 GITHUB_OUTPUT（run=true/false）。"""
    m = a.market
    sent, alerted = read_state(a.state_dir, "sent", m), read_state(a.state_dir, "alert", m)
    status, mins, why = check_window(m, sent_date=sent)
    if a.force:
        status, why = "run", f"強制執行（{why}）"
    today = now_in(m).date().isoformat()
    print_debug(m, {"觸發來源": os.getenv("TRIGGER_EVENT", "local"), "排程": os.getenv("TRIGGER_SCHEDULE") or "（非排程）",
                    "最近推播日": sent or "無", "最近告警日": alerted or "無", "判斷": status.upper(), "原因": why,
                    "休市檢查": "於 run 階段以交易所行事曆檢查（需安裝套件）" if status == "run" else "未執行（不需要）"})
    if status == "missed" and alerted != today:
        ci = clock_info()
        tg_alert(f"⚠️ {MARKETS[m]['name']}盤前檢查：今日盤前報告未能送出，已暫停推播\n"
                 f"原因：GitHub 排程觸發延遲，抵達時已超過啟動視窗\n{why}\n"
                 f"台北 {ci['taipei'][5:20]}｜紐約 {ci['new_york'][5:20]} {ci['ny_tz']}\n"
                 f"建議：設定外部精準觸發（README「外部精準觸發」）", a.dry_run)
        mark("alert", m, today)
    elif status == "missed":
        print("今日已發過漏發告警，不重複發送")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"run={'true' if status == 'run' else 'false'}\nstatus={status}\n")
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", choices=["gate", "run"], default="run")
    ap.add_argument("--market", choices=["us", "tw"], required=True)
    ap.add_argument("--force", action="store_true", help="略過時間視窗、休市與今日已推播檢查，立即執行")
    ap.add_argument("--dry-run", action="store_true", help="不發 Telegram，只印出")
    ap.add_argument("--no-llm", action="store_true", help="不呼叫 Claude（測試用）")
    ap.add_argument("--state-dir", default=os.getenv("STATE_DIR"), help="bot-state 內容所在資料夾")
    a = ap.parse_args()
    if a.command == "gate":
        gate(a)
        return 0
    try:
        run(a)
    except Exception as e:
        log.exception("執行失敗")
        if not a.dry_run:
            msg = (f"⚠️ {MARKETS[a.market]['name']}盤前報告執行失敗\n{type(e).__name__}: {str(e)[:500]}\n"
                   f"請到 GitHub → Actions 查看完整 log。")
            tg_alert(msg)
        return 1


def run(a):
    load_quant_modules()
    market = a.market
    today = now_in(market).date()
    # 時間視窗由 gate 判斷（安裝套件約需 1 分鐘，在這裡重判會讓視窗尾端通過 gate 的執行被誤擋）
    if not a.force and read_state(a.state_dir, "sent", market) == today.isoformat():
        log.info("今日已推播，不重複發送")
        return
    if not a.force:
        trading, why = is_trading_day(market, today)
        print_debug(market, {"是否為休市日": "否" if trading else f"是（{why}）"})
        if not trading:
            log.info("今天 %s 不推播：%s（不呼叫 Claude，節省費用）", today, why)
            if read_state(a.state_dir, "alert", market) != today.isoformat():
                tg_alert(f"ℹ️ {MARKETS[market]['name']}盤前檢查：今日（{today}）{why}，休市不推播。", a.dry_run)
                mark("alert", market, today.isoformat())
            return
        # 提早抵達的觸發：休眠到推播前 PREP_MINUTES 才抓資料，確保盤前數據最新
        wait = (push_time(market) - now_in(market)).total_seconds() - PREP_MINUTES * 60
        if wait > 0:
            log.info("休眠 %.0f 分鐘，於 %s 開始分析", wait / 60,
                     (push_time(market) - timedelta(minutes=PREP_MINUTES)).strftime("%H:%M %Z"))
            _sleep(wait)
    local_now = now_in(market)

    t0 = time.time()
    cands, prices, meta, sectors, etf_board, breadth = layer1(market)
    deep = layer2(market, cands, prices, meta, today)
    mac = safe(lambda: macro.collect(MARKETS[market]["tz"]), {"markets": {}, "fred": {}})
    headlines = safe(lambda: news.market_headlines(market), [])

    pack = {"as_of_local": local_now.isoformat(timespec="minutes"), "market": market,
            "macro": mac, "market_breadth": breadth, "sector_rotation_11": sectors,
            "etf_board": etf_board, "market_headlines": headlines[:12], "candidates": deep}
    if market == "tw":
        try:  # 台積電 ADR 溢價（夜盤風向球）
            tsm = prices["TSM"]["Close"].iloc[-1] if "TSM" in prices else None
            if tsm is None:
                import yfinance as yf
                tsm = float(yf.Ticker("TSM").fast_info["last_price"])
            fx = mac["markets"]["TWD=X"]["last"]
            tw2330 = float(prices["2330.TW"]["Close"].iloc[-1])
            pack["tsm_adr_premium_pct"] = round((tsm * fx / 5 / tw2330 - 1) * 100, 2)
        except Exception:
            pass

    ROOT.joinpath("reports").mkdir(exist_ok=True)
    stamp = local_now.strftime("%Y-%m-%d %H:%M %Z")
    (ROOT / "reports" / f"{market}_{today}_datapack.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    n = FINAL_PICKS[market]
    if a.no_llm or not ANTHROPIC_API_KEY:
        result = analyzer.fallback_result(deep, n, "未啟用 Claude" if a.no_llm else "未設定 ANTHROPIC_API_KEY")
    else:
        try:
            result = analyzer.call_claude(pack, market, n)
        except analyzer.ClaudeParseError as e:
            log.error("Claude 回應 JSON 解析失敗：%s", e)
            result = analyzer.fallback_result(deep, n, "回應格式解析失敗，完整 AI 報告見附檔")
            result["report_markdown"] = e.report
        except Exception as e:
            log.exception("Claude 失敗")
            result = analyzer.fallback_result(deep, n, f"{type(e).__name__}: {str(e)[:120]}")
    result = analyzer.validate_picks(result, deep)

    md_path = ROOT / "reports" / f"{market}_{today}_report.md"
    md_path.write_text(build_markdown(result, market, stamp, deep), encoding="utf-8")
    msgs = telegram.render_messages(result, market, stamp, macro_line(mac["markets"]))
    log.info("分析完成，耗時 %.0f 秒", time.time() - t0)

    if not a.force:  # 等到開盤前 10 分鐘準時送出
        wait = (push_time(market) - now_in(market)).total_seconds()
        if wait > 0:
            log.info("等待 %.0f 秒後推播（%s）", wait, push_time(market).strftime("%H:%M %Z"))
            _sleep(wait)
    telegram.send(msgs, str(md_path), dry_run=a.dry_run)
    log.info("推播完成")
    if not a.dry_run and not a.force:   # 給 workflow 寫入 bot-state，避免同日其他觸發重複推播
        mark("sent", market, today.isoformat())


if __name__ == "__main__":
    sys.exit(main())
