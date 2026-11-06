"""時區 / 冬夏令 / 時間視窗 / 休市日判斷（守門員）。

不比對 cron 字串、不依賴 datetime.now().dst()：一律以 zoneinfo 取得市場「當地時間」再判斷，
所以不論是 GitHub 排程（可能延遲數小時）、外部 cron 的 repository_dispatch、或手動觸發，判斷方式都一樣：
  run    ：在當地啟動視窗內 → 休眠到推播前 25 分鐘再分析，於開盤前 10 分鐘送出
  early  ：視窗開始前 → 結束（交給較晚的觸發），不告警
  sent   ：今日已推播 → 結束，不告警
  weekend：週末 → 結束，不告警
  missed ：視窗已過且今日尚未推播 → 不送過時報告，發 Telegram 漏發告警（每日一次）
"""
from __future__ import annotations

import logging
from functools import lru_cache
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import EARLY_GRACE_MINUTES, LEAD_MINUTES, MARKETS

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def now_in(market: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(MARKETS[market]["tz"])
    return (now or datetime.now(ZoneInfo("UTC"))).astimezone(tz)


def push_time(market: str, now: datetime | None = None) -> datetime:
    """今天（當地）應推播的時間 = 開盤 - LEAD_MINUTES。"""
    local = now_in(market, now)
    h, m = MARKETS[market]["open"]
    open_dt = datetime.combine(local.date(), time(h, m), tzinfo=local.tzinfo)
    return open_dt - timedelta(minutes=LEAD_MINUTES)


def window(market: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    local = now_in(market, now)
    (h1, m1), (h2, m2) = MARKETS[market]["window"]
    return (datetime.combine(local.date(), time(h1, m1), tzinfo=local.tzinfo),
            datetime.combine(local.date(), time(h2, m2), tzinfo=local.tzinfo))


def clock_info(now: datetime | None = None) -> dict:
    """除錯資訊：台北 / 紐約當地時間、時區縮寫（EDT/EST）、是否夏令。"""
    now = now or datetime.now(ZoneInfo("UTC"))
    tpe = now.astimezone(ZoneInfo("Asia/Taipei"))
    ny = now.astimezone(ZoneInfo("America/New_York"))
    return {"utc": now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
            "taipei": tpe.strftime("%Y-%m-%d %a %H:%M:%S"),
            "new_york": ny.strftime("%Y-%m-%d %a %H:%M:%S"),
            "ny_tz": ny.tzname(), "ny_dst": bool(ny.dst())}


def infer_market(now: datetime | None = None) -> tuple[str, str]:
    """外部觸發未指定市場時，依「現在落在哪個市場的啟動視窗」推斷（冬夏令自動處理）。
    都不在視窗內時，取今天視窗結束時間最接近現在的市場（讓守門員據實回報太早或漏發）。"""
    best = None
    for m in MARKETS:
        st, mins, _ = check_window(m, now)
        if st == "run":
            return m, f"現在落在{MARKETS[m]['name']}啟動視窗內"
        w_start, w_end = window(m, now)
        local = now_in(m, now)
        dist = min(abs((local - w_start).total_seconds()), abs((local - w_end).total_seconds()))
        if best is None or dist < best[1]:
            best = (m, dist)
    return best[0], f"不在任何市場的啟動視窗內，取時間最接近的{MARKETS[best[0]]['name']}"


def check_window(market: str, now: datetime | None = None, sent_date: str | None = None) -> tuple[str, float, str]:
    """回傳 (狀態, 距推播時間的分鐘數, 說明)。狀態：run / early / sent / weekend / missed。
    只用標準庫，可在安裝套件前執行。sent_date：bot-state 記錄的最近一次推播的當地日期。"""
    local = now_in(market, now)
    target = push_time(market, now)
    w_start, w_end = window(market, now)
    mins = (target - local).total_seconds() / 60
    where = (f"{MARKETS[market]['name']}當地 {local:%Y-%m-%d %a %H:%M %Z}，"
             f"啟動視窗 {w_start:%H:%M}–{w_end:%H:%M}，推播 {target:%H:%M}")
    if local.weekday() >= 5:
        return "weekend", mins, f"{where}：週末不推播"
    if sent_date and sent_date.strip() == local.date().isoformat():
        return "sent", mins, f"{where}：今日已推播，不重複發送"
    if local < w_start - timedelta(minutes=EARLY_GRACE_MINUTES):
        return "early", mins, f"{where}：尚未到啟動視窗（早 {(w_start - local).total_seconds() / 60:.0f} 分鐘），交給較晚的觸發"
    if local <= w_end:
        note = "（視窗開始前抵達，休眠等待）" if local < w_start else ""
        return "run", mins, f"{where}：在視窗內{note}，距推播 {mins:.0f} 分鐘"
    return "missed", mins, (f"{where}：觸發抵達時已超過視窗 {(local - w_end).total_seconds() / 60:.0f} 分鐘，"
                            f"今日尚未推播 → 漏發")


# ------------------------------------------------------------------ 休市日

def _extra_closures(market: str) -> set[date]:
    """data/{market}_extra_closures.txt：手動補充（颱風假、臨時休市），一行一個 YYYY-MM-DD。"""
    f = ROOT / "data" / f"{market}_extra_closures.txt"
    out = set()
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                try:
                    out.add(date.fromisoformat(line))
                except ValueError:
                    pass
    return out


@lru_cache(maxsize=4)
def _twse_holidays(year: int) -> dict[date, str]:
    """證交所官方休市日（含春節前後『無交易僅交割』日）。失敗回傳空 dict 並快取，避免重複等待逾時。"""
    try:
        return _fetch_twse_holidays(year)
    except Exception as e:
        log.warning("證交所休市日 API 失敗（改用交易所行事曆）：%s", e)
        return {}


@lru_cache(maxsize=2)
def _calendar(name: str):
    import pandas_market_calendars as mcal
    return mcal.get_calendar(name)


def _fetch_twse_holidays(year: int) -> dict[date, str]:
    import requests
    from .config import HTTP_HEADERS
    url = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
    r = requests.get(url, params={"date": f"{year}0101", "response": "json"}, headers=HTTP_HEADERS, timeout=15)
    r.raise_for_status()
    out = {}
    for row in r.json().get("data", []):
        d_raw, name = str(row[0]).strip(), str(row[1])
        if "開始交易" in name or "最後交易" in name:   # 這兩類是「有交易」的日子
            continue
        try:
            if "-" in d_raw:
                d = date.fromisoformat(d_raw[:10])
            else:  # 民國年 115/01/01
                y, m, dd = d_raw.replace("/", "-").split("-")[:3]
                d = date(int(y) + 1911, int(m), int(dd))
            out[d] = name
        except Exception:
            continue
    return out


def is_trading_day(market: str, d: date) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, "週末"
    if d in _extra_closures(market):
        return False, "手動設定休市（data/*_extra_closures.txt）"
    try:
        cal = _calendar(MARKETS[market]["calendar"])
        days = cal.valid_days(start_date=d, end_date=d)
        if len(days) == 0:
            return False, f"{MARKETS[market]['calendar']} 交易所行事曆：休市日"
    except Exception as e:  # 套件出錯就退回只看週末，不要因此漏推
        log.warning("pandas_market_calendars 檢查失敗：%s", e)
    if market == "tw":
        hol = _twse_holidays(d.year)
        if d in hol:
            return False, f"證交所公告休市：{hol[d]}"
    return True, "交易日"


def previous_trading_day(market: str, d: date) -> date:
    x = d - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(market, x)[0]:
            return x
        x -= timedelta(days=1)
    return d - timedelta(days=1)
