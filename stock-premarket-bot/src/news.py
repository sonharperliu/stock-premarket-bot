"""消息面：Yahoo Finance 個股新聞 + Google News RSS（中英文），只取近 72 小時標題。"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
import yfinance as yf

from .config import HTTP_HEADERS

log = logging.getLogger(__name__)


def yahoo_news(ticker: str, n: int = 6) -> list[dict]:
    out = []
    try:
        for it in (yf.Ticker(ticker).news or [])[:n]:
            c = it.get("content", it)
            title = c.get("title")
            if not title:
                continue
            prov = c.get("provider", {})
            out.append({"title": title, "source": prov.get("displayName") if isinstance(prov, dict) else c.get("publisher"),
                        "time": c.get("pubDate") or c.get("providerPublishTime")})
    except Exception as e:
        log.debug("yahoo news %s：%s", ticker, e)
    return out


def google_news(query: str, lang: str = "en", n: int = 6, hours: int = 72) -> list[dict]:
    params = {"en": "hl=en-US&gl=US&ceid=US:en", "zh": "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"}[lang]
    url = f"https://news.google.com/rss/search?q={quote(query)}+when:3d&{params}"
    out = []
    try:
        root = ET.fromstring(requests.get(url, headers=HTTP_HEADERS, timeout=15).content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for item in root.iter("item"):
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate"))
            except Exception:
                dt = None
            if dt and dt < cutoff:
                continue
            out.append({"title": item.findtext("title"), "source": item.findtext("source"),
                        "time": dt.isoformat() if dt else None})
            if len(out) >= n:
                break
    except Exception as e:
        log.debug("google news %s：%s", query, e)
    return out


def collect(ticker: str, name: str, market: str) -> list[dict]:
    if market == "us":
        items = yahoo_news(ticker) or google_news(f"{ticker} stock")
    else:
        items = google_news(f"{name} 股票", "zh") + yahoo_news(ticker, 3)
    seen, uniq = set(), []
    for it in items:
        k = (it.get("title") or "")[:40]
        if k and k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq[:8]


def market_headlines(market: str) -> list[dict]:
    if market == "us":
        return google_news("stock market today Fed", "en", 8, 24) + google_news("premarket futures", "en", 5, 18)
    return google_news("台股 盤前", "zh", 8, 24) + google_news("外資 台股", "zh", 5, 24)
