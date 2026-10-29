"""盤前自動分析 + Telegram 推播 主程式。

用法：
  python main.py --market us            # 正式模式：檢查時間窗、休市日，等到開盤前 10 分鐘推播
  python main.py --market tw --force    # 立即執行（略過時間/休市檢查，不等待）
  python main.py --market us --force --dry-run --no-llm   # 本機測試：不呼叫 Claude、不發 Telegram
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src import analyzer, chips_tw, chips_us, fundamentals, macro, news, scanner, telegram, technicals, universe
from src.config import ANTHROPIC_API_KEY, DEEP_CANDIDATES, FINAL_PICKS, MARKETS, MIN_DOLLAR_VOLUME
from src.scheduler import check_window, is_trading_day, now_in, previous_trading_day, push_time

import warnings
warnings.filterwarnings("ignore")                       # yfinance / pandas 的 FutureWarning 會淹沒 log
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # 個別代號下載失敗已由程式處理
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
ROOT = Path(__file__).resolve().parent


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
            parts.append(f"{mk[k]['name']} {mk[k]['last']:g}({mk[k]['chg_1d_pct']:+.1f}%)")
    return " | ".join(parts)


def build_markdown(result: dict, market: str, stamp: str, cands: dict) -> str:
    m = MARKETS[market]
    md = [f"# {m['name']}盤前作戰報告 — {stamp}\n"]
    if result.get("report_markdown"):
        md.append(result["report_markdown"])
    md.append("\n\n## 附錄：買賣點總表（程式驗算）\n")
    md.append("| # | 代號 | 名稱 | 進場區 | 停損 | 停利1 | 停利2 | R/R | ATR | 驗算 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for p in result.get("picks", []):
        md.append(f"| {p.get('rank')} | {p.get('ticker')} | {p.get('name', '')} | {p.get('entry_low')}–{p.get('entry_high')} | "
                  f"{p.get('stop_loss')} | {p.get('take_profit_1')} | {p.get('take_profit_2')} | {p.get('risk_reward')} | "
                  f"{p.get('atr14')} | {p.get('validation', '')} |")
    md.append("\n\n> 本報告由程式自動產生，僅供研究參考，不構成投資建議。投資有風險，請自行判斷並控制部位。")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["us", "tw"], required=True)
    ap.add_argument("--force", action="store_true", help="略過時間窗與休市檢查，立即推播")
    ap.add_argument("--dry-run", action="store_true", help="不發 Telegram，只印出")
    ap.add_argument("--no-llm", action="store_true", help="不呼叫 Claude（測試用）")
    a = ap.parse_args()
    try:
        run(a)
    except Exception as e:
        log.exception("執行失敗")
        if not a.dry_run:
            telegram.send([f"⚠️ <b>{MARKETS[a.market]['name']}盤前報告執行失敗</b>\n"
                           f"<code>{telegram.e(type(e).__name__)}: {telegram.e(str(e)[:500])}</code>\n"
                           f"請到 GitHub → Actions 查看完整 log。"])
        return 1


def run(a):
    market = a.market

    if not a.force:
        ok, mins, why = check_window(market)
        log.info("時間窗檢查：%s", why)
        if not ok:
            return
    local_now = now_in(market)
    today = local_now.date()
    if not a.force:
        trading, why = is_trading_day(market, today)
        if not trading:
            log.info("今天 %s 不推播：%s（不呼叫 Claude，節省費用）", today, why)
            return

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
            time.sleep(wait)
    telegram.send(msgs, str(md_path), dry_run=a.dry_run)
    log.info("推播完成")


if __name__ == "__main__":
    sys.exit(main())
