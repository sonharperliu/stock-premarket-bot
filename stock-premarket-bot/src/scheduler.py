"""時區 / 冬夏令 / 休市日判斷。

GitHub Actions 的 cron 只吃 UTC，無法表達「紐約時間 09:20」。
做法：workflow 同時排兩個 UTC 時間（分別對應夏令 EDT 與冬令 EST），
程式用 zoneinfo 以 America/New_York 計算「今天真正的推播時間」，
不在時間窗內的那一次就直接結束 —— 一年四季都不用改設定。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import LEAD_MINUTES, MARKETS, MAX_EARLY_MINUTES, MAX_LATE_MINUTES

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


def check_window(market: str, now: datetime | None = None) -> tuple[bool, float, str]:
    """回傳 (是否該執行, 距推播時間的分鐘數, 說明)。只用標準庫，可在安裝套件前先跑。"""
    local = now_in(market, now)
    target = push_time(market, now)
    mins = (target - local).total_seconds() / 60
    if local.weekday() >= 5:
        return False, mins, f"{local:%Y-%m-%d %a} 為週末"
    if mins > MAX_EARLY_MINUTES:
        return False, mins, f"太早：當地 {local:%H:%M}，推播時間 {target:%H:%M}（此排程對應另一個冬/夏令時段）"
    if mins < -min(MAX_LATE_MINUTES, LEAD_MINUTES - 1):   # 無論如何都要在開盤前送出
        return False, mins, f"太晚：當地 {local:%H:%M}，推播時間 {target:%H:%M} 已過"
    return True, mins, f"當地 {local:%Y-%m-%d %H:%M %Z}，距推播 {mins:.0f} 分鐘"


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
