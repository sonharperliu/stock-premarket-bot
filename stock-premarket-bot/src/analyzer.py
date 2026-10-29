"""第三層：Claude 深度推理 + 買賣點定價，並由程式驗算風報比與價格合理性。"""
from __future__ import annotations

import json
import logging
import re

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位擁有 30 年資歷的華爾街資深投資組合經理兼量化策略師，曾任職頂級避險基金，專長是由上而下（Top-down）總經判斷結合由下而上（Bottom-up）個股研究，並以嚴格的風險控管定價每一筆交易。

你的任務：根據使用者提供的「盤前數據包」，產出一份機構級盤前作戰報告。

## 分析框架（必須逐層執行）
1. 總經與流動性（Macro Regime）：利率路徑（Fed 期貨隱含機率）、殖利率曲線、通膨/就業趨勢、美元淨流動性、信用利差、VIX、期貨盤前走勢、今明兩日重磅數據。判定 risk-on / neutral / risk-off。
2. 板塊輪動：用產業 ETF 的相對強弱(RS)、量比、CMF 資金流，判斷資金正流入/流出哪些板塊，與總經體制是否一致。
3. 個股六維度評分（各 1–10 分）：總經順風度、基本面（成長/獲利/估值）、護城河（毛利穩定度、ROIC、研發強度、FCF）、技術面（趨勢、RSI、MACD、ATR、支撐壓力）、籌碼面（法人/融資券/大戶 或 選擇權異動/空單比/機構持股）、消息面（催化劑與風險）。
4. 交叉驗證（最重要）：找出維度之間的矛盾並據此降評，例如「技術突破但法人連賣」、「基本面強但估值過高且 RSI>75」、「財報日在 3 天內」、「板塊資金流出」。只有多數維度共振的標的才能給高信心。
5. 買賣點定價：每檔都附有程式計算的 ATR 基準計畫（baseline_plan）。你可依支撐壓力、樞紐點、缺口與消息調整，但必須遵守：
   - 做多：停損 < 進場區下緣 < 進場區上緣 < 停利1 < 停利2
   - 停損距進場中位數介於 1.0～3.0 倍 ATR
   - 風報比 (停利1 − 進場中位) / (進場中位 − 停損) ≥ 1.5；達不到就不要推薦，改列入 avoid
   - 不追高：進場區上緣原則上不高於昨收 + 0.3 ATR
6. 數據缺漏請明說，不可捏造任何數字或新聞。

## 輸出格式（嚴格遵守，分兩段）
第一段：<json> 與 </json> 之間放一個合法 JSON 物件（字串內不可有未跳脫的雙引號與換行），結構如下：
{
  "market_regime": {"stance": "risk-on|neutral|risk-off", "summary": "3-4 句總經與資金面結論", "key_points": ["…", "…", "…"]},
  "sector_view": [{"sector": "名稱", "stance": "超配|中性|低配", "reason": "一句話"}],
  "picks": [{
     "ticker": "代號(與輸入 candidates 的 key 完全相同)", "name": "名稱", "rank": 1, "conviction": 1,
     "setup": "突破 / 回測支撐 / 趨勢延續 / 反轉 等",
     "thesis": "2-3 句核心論點，需引用具體數據",
     "scores": {"macro": 0, "fundamental": 0, "moat": 0, "technical": 0, "chips": 0, "news": 0},
     "conflicts": "交叉驗證發現的矛盾（沒有則寫 無）",
     "entry_low": 0, "entry_high": 0, "stop_loss": 0, "take_profit_1": 0, "take_profit_2": 0,
     "risk_reward": 0, "holding_period": "例如 3-10 個交易日", "position_size_pct": 0,
     "catalysts": ["…"], "key_risks": ["…"]
  }],
  "avoid": [{"ticker": "…", "reason": "一句話"}],
  "events_today": ["時間 事件 — 對盤面可能影響"]
}
conviction 為 1-5 的整數；價格欄位為數字。picks 依信心排序，數量等於使用者指定數量（合格標的不足可以少於指定數，並在報告說明）。position_size_pct 為建議占總資金百分比，單檔不超過 10%，risk-off 時整體應降低。

第二段：<report> 與 </report> 之間放完整繁體中文 Markdown 報告：一、總經與流動性 二、板塊輪動 三、精選標的逐檔分析（含六維度評分表與買賣點）四、今日事件與風險 五、操作策略與部位建議。

兩段以外不要輸出任何文字。全部使用繁體中文。"""


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def call_claude(data_pack: dict, market: str, n_picks: int) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=3, timeout=600)
    user = (f"市場：{'美股' if market == 'us' else '台股'}。請從 candidates 中選出最終 {n_picks} 檔做多標的。\n"
            f"以下是盤前數據包（JSON）：\n{_compact(data_pack)}")
    # 用串流呼叫：長輸出不會被 HTTP 逾時中斷
    with client.messages.stream(model=ANTHROPIC_MODEL, max_tokens=16000, system=SYSTEM_PROMPT,
                                messages=[{"role": "user", "content": user}]) as stream:
        resp = stream.get_final_message()
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    log.info("Claude 用量：input=%s output=%s stop=%s", resp.usage.input_tokens, resp.usage.output_tokens,
             resp.stop_reason)
    return parse_response(text)


def parse_response(text: str) -> dict:
    """解析 <json>…</json> + <report>…</report>。JSON 壞掉時仍保留報告文字，由呼叫端決定備援。"""
    report = ""
    m = re.search(r"<report>(.*?)(?:</report>|$)", text, re.S)
    if m:
        report = m.group(1).strip()
    m = re.search(r"<json>(.*?)</json>", text, re.S)
    body = m.group(1) if m else text.split("<report>")[0]
    try:
        result = parse_json(body)
    except Exception as e:
        raise ClaudeParseError(str(e), report) from e
    if report:
        result["report_markdown"] = report
    return result


class ClaudeParseError(ValueError):
    def __init__(self, msg: str, report: str):
        super().__init__(msg)
        self.report = report


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 常見瑕疵修補：字串內的實際換行、結尾多餘逗號
        fixed = re.sub(r",\s*([}\]])", r"\1", raw)
        fixed = re.sub(r'"(?:[^"\\]|\\.)*"', lambda mm: mm.group(0).replace("\n", "\\n"), fixed)
        return json.loads(fixed)


def validate_picks(result: dict, candidates: dict[str, dict]) -> dict:
    """程式驗算：價格順序、停損距離（ATR 倍數）、風報比；不合格就套用 ATR 基準計畫並標註。"""
    fixed, seen = [], set()
    alias = {}
    for k in candidates:  # 允許 2330 / 2330.TW / brk.b 等寫法
        for a in {k, k.upper(), k.split(".")[0], k.replace("-", ".")}:
            alias.setdefault(a.upper(), k)
    for p in result.get("picks", []) or []:
        if not isinstance(p, dict):
            continue
        t = alias.get(str(p.get("ticker", "")).strip().upper())
        if t and t in seen:
            continue
        p["ticker"] = t or p.get("ticker")
        cand = candidates.get(t) if t else None
        if not cand:
            log.warning("Claude 回傳了不在候選名單的代號 %s，已剔除", t)
            continue
        seen.add(t)
        p.setdefault("name", cand.get("name", t))
        tech = cand.get("technical", {})
        atr_v = tech.get("atr14") or 0
        notes = []
        try:
            el, eh, sl = float(p["entry_low"]), float(p["entry_high"]), float(p["stop_loss"])
            tp1, tp2 = float(p["take_profit_1"]), float(p.get("take_profit_2") or p["take_profit_1"])
            mid = (el + eh) / 2
            ok = sl < el <= eh < tp1 <= tp2
            if atr_v and ok:
                dist = (mid - sl) / atr_v
                ok = 0.9 <= dist <= 3.2
                if not ok:
                    notes.append(f"停損距離 {dist:.1f} ATR 超出範圍")
            rr = (tp1 - mid) / (mid - sl) if mid > sl else 0
            if ok and rr < 1.5:
                notes.append(f"風報比 {rr:.2f} < 1.5")
                ok = False
        except Exception:
            ok, rr = False, 0
            notes.append("價格欄位缺漏")
        if not ok:
            base = tech.get("baseline_plan") or {}
            if not base:
                continue
            p.update({k: base[k] for k in ("entry_low", "entry_high", "stop_loss", "take_profit_1", "take_profit_2")})
            rr = base["risk_reward"]
            notes.append("已改用 ATR 基準計畫")
        p["risk_reward"] = round(rr, 2)
        p["validation"] = "；".join(notes) if notes else "通過"
        p["atr14"] = atr_v
        fixed.append(p)
    for i, p in enumerate(fixed, 1):
        p["rank"] = i
    result["picks"] = fixed
    return result


def fallback_result(candidates: dict[str, dict], n: int, reason: str) -> dict:
    """Claude 無法使用時：直接用量化分數 + ATR 計畫產出精簡版報告，確保仍有推播。"""
    picks = []
    for i, (t, c) in enumerate(list(candidates.items())[:n], 1):
        base = c["technical"].get("baseline_plan") or {}
        if not base:
            continue
        picks.append({"ticker": t, "name": c.get("name", t), "rank": i, "conviction": 2, "setup": "量化強勢",
                      "thesis": f"第一層綜合強勢分數 {c['scan'].get('score')}，RS20 {c['scan'].get('rs20')}，"
                                f"量比 {c['scan'].get('vol_ratio')}，趨勢 {c['technical'].get('trend')}",
                      **base, "holding_period": "3-10 個交易日", "position_size_pct": 5,
                      "validation": "量化備援（未經 Claude 分析）", "atr14": c["technical"].get("atr14"),
                      "catalysts": [], "key_risks": ["未經 AI 六維度交叉驗證"]})
    return {"market_regime": {"stance": "neutral", "summary": f"⚠️ Claude 分析未完成（{reason}），以下為純量化備援結果。",
                              "key_points": []},
            "sector_view": [], "picks": picks, "avoid": [], "events_today": [], "report_markdown": ""}
