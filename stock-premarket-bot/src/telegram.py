"""Telegram 推播：摘要訊息（HTML 格式，自動切段 ≤4000 字）+ 完整報告 .md 附檔。"""
from __future__ import annotations

import html
import logging

import requests

from .config import MARKETS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{}/{}"
STANCE = {"risk-on": "🟢 Risk-On", "neutral": "🟡 中性", "risk-off": "🔴 Risk-Off"}


def e(x) -> str:
    return html.escape(str(x if x is not None else "—"))


def fmt_price(x) -> str:
    try:
        x = float(x)
        return f"{x:,.2f}" if x < 1000 else f"{x:,.1f}"
    except Exception:
        return "—"


def render_messages(result: dict, market: str, stamp: str, macro_line: str) -> list[str]:
    m = MARKETS[market]
    reg = result.get("market_regime", {})
    head = [f"{m['flag']} <b>{m['name']}盤前作戰報告</b>｜{e(stamp)}",
            f"市場體制：<b>{STANCE.get(reg.get('stance'), e(reg.get('stance')))}</b>",
            e(reg.get("summary", "")), ""]
    if macro_line:
        head.append(f"<code>{e(macro_line)}</code>")
    for k in reg.get("key_points", [])[:4]:
        head.append(f"• {e(k)}")
    sv = result.get("sector_view", [])
    if sv:
        head.append("\n<b>📊 板塊觀點</b>")
        icon = {"超配": "⬆️", "低配": "⬇️"}
        head += [f"{icon.get(s.get('stance'), '➖')} {e(s.get('sector'))}：{e(s.get('reason'))}" for s in sv[:6]]
    ev = result.get("events_today", [])
    if ev:
        head.append("\n<b>📅 今日事件</b>")
        head += [f"• {e(x)}" for x in ev[:5]]
    blocks = ["\n".join(head)]

    for p in result.get("picks", []):
        sc = p.get("scores", {})
        score_line = " ".join(f"{lbl}{sc.get(k, '-')}" for k, lbl in
                              [("macro", "總經"), ("fundamental", "基本"), ("moat", "護城"), ("technical", "技術"),
                               ("chips", "籌碼"), ("news", "消息")]) if sc else ""
        b = [f"<b>{p.get('rank', '')}. {e(p.get('ticker'))} {e(p.get('name', ''))}</b>  {'⭐' * int(p.get('conviction') or 0)}",
             f"型態：{e(p.get('setup', ''))}｜持有：{e(p.get('holding_period', ''))}｜部位：{e(p.get('position_size_pct', '—'))}%",
             f"🎯 進場 <b>{fmt_price(p.get('entry_low'))} – {fmt_price(p.get('entry_high'))}</b>",
             f"🛑 停損 <b>{fmt_price(p.get('stop_loss'))}</b>　✅ 停利 {fmt_price(p.get('take_profit_1'))} / {fmt_price(p.get('take_profit_2'))}",
             f"⚖️ 風報比 <b>1 : {e(p.get('risk_reward'))}</b>　ATR {fmt_price(p.get('atr14'))}"]
        if score_line:
            b.append(f"<code>{e(score_line)}</code>")
        b.append(f"📝 {e(p.get('thesis', ''))}")
        if p.get("conflicts") and p.get("conflicts") != "無":
            b.append(f"⚠️ {e(p['conflicts'])}")
        if p.get("key_risks"):
            b.append(f"風險：{e('；'.join(p['key_risks'][:2]))}")
        if p.get("validation") and p["validation"] != "通過":
            b.append(f"<i>🔧 {e(p['validation'])}</i>")
        blocks.append("\n".join(b))

    if result.get("avoid"):
        blocks.append("<b>🚫 避開名單</b>\n" + "\n".join(f"• {e(a.get('ticker'))}：{e(a.get('reason'))}"
                                                     for a in result["avoid"][:6]))
    blocks.append("<i>本報告由程式自動產生，僅供研究參考，不構成投資建議。</i>")

    msgs, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


def send(messages: list[str], doc_path: str | None = None, dry_run: bool = False) -> None:
    if dry_run or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        if not dry_run:
            log.warning("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，改印在 log")
        for msg in messages:
            print(msg, "\n" + "-" * 60)
        return
    for msg in messages:
        r = requests.post(API.format(TELEGRAM_BOT_TOKEN, "sendMessage"),
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if not r.ok:  # HTML 解析失敗時改純文字重送，確保一定送達
            log.warning("Telegram HTML 失敗：%s，改用純文字", r.text[:200])
            import re
            plain = html.unescape(re.sub(r"<[^>]+>", "", msg))
            requests.post(API.format(TELEGRAM_BOT_TOKEN, "sendMessage"),
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": plain}, timeout=30).raise_for_status()
    if doc_path:
        with open(doc_path, "rb") as f:
            requests.post(API.format(TELEGRAM_BOT_TOKEN, "sendDocument"),
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": "📄 完整分析報告"},
                          files={"document": f}, timeout=60)
