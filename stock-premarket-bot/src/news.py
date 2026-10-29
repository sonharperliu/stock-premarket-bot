"""消息面：Yahoo Finance 個股新聞 + Google News RSS（中英文），只取近 72 小時標題。"""
from __future__ import annotations

import logging
import re
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


_SUFFIX = re.compile(r"[,.]?\s+(inc|corp|corporation|co|company|ltd|plc|group|holdings|class [a-c]|n\.?v|s\.?a|ag|se)\.?$", re.I)


def short_name(name: str) -> str:
    n = re.sub(r"\(.*?\)", "", (name or "").replace("*", "").split("-KY")[0]).strip()
    for _ in range(3):
        n = _SUFFIX.sub("", n).strip(" ,.")
    return n


def _relevant(title: str, keys: list[str]) -> bool:
    t = (title or "").lower()
    return any(k and k.lower() in t for k in keys)


def collect(ticker: str, name: str, market: str) -> list[dict]:
    """只保留標題確實提到公司名稱或代號的新聞（Yahoo 對個股常回傳大盤新聞）。"""
    sn = short_name(name)
    if market == "us":
        keys = [sn, sn.split()[0] if sn and len(sn.split()[0]) >= 4 else "", f"({ticker})", f" {ticker} "]
        items = google_news(f'"{sn}" stock' if sn and sn != ticker else f"{ticker} stock", "en", 10) + yahoo_news(ticker, 10)
    else:
        code = ticker.split(".")[0]
        keys = [sn, code]
        items = google_news(f"{sn} {code}", "zh", 10) + yahoo_news(ticker, 5)
    seen, uniq = set(), []
    for it in items:
        title = it.get("title") or ""
        k = title[:40]
        if k and k not in seen and _relevant(f" {title} ", keys):
            seen.add(k)
            uniq.append(it)
    return uniq[:8]


def market_headlines(market: str) -> list[dict]:
    if market == "us":
        return google_news("stock market today Fed", "en", 8, 24) + google_news("premarket futures", "en", 5, 18)
    return google_news("台股 盤前", "zh", 8, 24) + google_news("外資 台股", "zh", 5, 24)
