"""Bounded, dated news from fixed official China News Service RSS feeds.

Queries and topics are filtered locally and never added to an upstream URL.
RSS publication time is not represented as the event's occurrence time.
"""

from __future__ import annotations

import copy
import html
import re
import socket
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NewsFeedError(RuntimeError):
    """A user-readable failure; no unrelated or undated fallback is returned."""


class _NoNewsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NewsFeedClient:
    BASE_URL = "https://www.chinanews.com.cn/rss/"
    SOURCE_LABEL = "中新网官方 RSS"
    COVERAGE_NOTE = (
        "来自中新网单一新闻源，仅覆盖该源当前 RSS 条目，并非全网新闻汇总。"
        "发布时间不等同于事件发生时间。"
    )
    MAX_RESPONSE_BYTES = 256 * 1024
    TIMEOUT_SECONDS = 10
    CACHE_SECONDS = 60
    MAX_CACHE_ENTRIES = 8
    MAX_RESULTS = 3

    # Paths are published at https://www.chinanews.com.cn/rss/ .
    CATEGORY_FEEDS = {
        "国际": "world.xml", "世界": "world.xml",
        "world": "world.xml", "international": "world.xml",
        "社会": "society.xml", "society": "society.xml",
        "财经": "finance.xml", "经济": "finance.xml", "金融": "finance.xml",
        "finance": "finance.xml", "financial": "finance.xml",
        "economy": "finance.xml", "business": "finance.xml",
        "体育": "sports.xml", "sports": "sports.xml",
        "文娱": "culture.xml", "文化": "culture.xml", "娱乐": "culture.xml",
        "culture": "culture.xml", "entertainment": "culture.xml",
        "教育": "edu.xml", "education": "edu.xml",
        "时政": "china.xml", "politics": "china.xml",
    }
    TECHNOLOGY_TOPICS = frozenset({"科技", "科学", "technology", "science", "tech"})
    TECHNOLOGY_TERMS = ("科技", "科学", "技术", "科研", "人工智能", "航天", "芯片")

    def __init__(
        self,
        *,
        opener: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._opener = opener if opener is not None else build_opener(_NoNewsRedirectHandler())
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._monotonic = monotonic or time.monotonic
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def search(self, query: str, *, topic: str = "", period: str = "today") -> dict:
        query = self._text_input(query, "新闻查询", maximum=200, allow_empty=False)
        topic = self._text_input(topic, "新闻主题", maximum=80, allow_empty=True)
        if period not in {"today", "recent"}:
            raise NewsFeedError("新闻查询目前只支持今天或最近七天。")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise NewsFeedError("无法确定本机日期和时区，暂时不能核实新闻发布时间。")
        start = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            if period == "today" else now - timedelta(days=7)
        )
        normalized_topic = unicodedata.normalize("NFKC", topic).casefold().strip()
        filename = self.CATEGORY_FEEDS.get(normalized_topic, "scroll-news.xml")
        dated_items = self._feed_items(filename)
        matching = []
        for published, item in dated_items:
            if not start <= published <= now:
                continue
            if normalized_topic and filename == "scroll-news.xml":
                if not self._matches_topic(normalized_topic, item):
                    continue
            matching.append((published, item))
        matching.sort(key=lambda pair: pair[0], reverse=True)
        if not matching:
            window = "今天" if period == "today" else "最近七天"
            raise NewsFeedError(
                "当前中新网新闻源没有找到%s%s且发布时间可核实的报道；"
                "这不代表其他新闻源没有报道。"
                % (window, "符合这个主题" if topic else "")
            )
        return {
            "source": self.SOURCE_LABEL,
            "query": query,
            "retrieved_at": now.isoformat(timespec="seconds"),
            "coverage_note": self.COVERAGE_NOTE,
            "results": [copy.deepcopy(item) for _, item in matching[:self.MAX_RESULTS]],
        }

    @staticmethod
    def _text_input(value: Any, label: str, *, maximum: int, allow_empty: bool) -> str:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise NewsFeedError(label + "需要使用不含控制字符的文字。")
        cleaned = " ".join(value.strip().split())
        if len(cleaned) > maximum or (not cleaned and not allow_empty):
            raise NewsFeedError(label + "长度不合适，请缩短后重试。")
        return cleaned

    @classmethod
    def _matches_topic(cls, topic: str, item: dict) -> bool:
        text = unicodedata.normalize(
            "NFKC", item["title"] + " " + item["snippet"]
        ).casefold()
        if topic in cls.TECHNOLOGY_TOPICS:
            return any(term in text for term in cls.TECHNOLOGY_TERMS) or bool(
                re.search(r"\b(?:ai|technology|science|scientific|chip)\b", text)
            )
        if re.fullmatch(r"[a-z0-9 .'-]+", topic):
            return bool(re.search(r"(?<![a-z0-9])" + re.escape(topic) + r"(?![a-z0-9])", text))
        return topic in text

    def _feed_items(self, filename: str) -> list:
        allowed = {"scroll-news.xml"} | set(self.CATEGORY_FEEDS.values())
        if filename not in allowed:
            raise NewsFeedError("新闻来源不在本机允许列表中。")
        instant = self._monotonic()
        with self._lock:
            cached = self._cache.get(filename)
            if cached and 0 <= instant - cached[0] < self.CACHE_SECONDS:
                return copy.deepcopy(cached[1])
        raw = self._read(self.BASE_URL + filename)
        upper = raw.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"\x00" in raw:
            raise NewsFeedError("新闻来源返回了不安全的 XML 格式。")
        try:
            root = ET.fromstring(raw)
        except (ET.ParseError, ValueError, LookupError) as exc:
            raise NewsFeedError("新闻来源返回内容无法解析。") from exc
        if self._local_name(root.tag) != "rss":
            raise NewsFeedError("新闻来源返回的不是 RSS 新闻。")
        items = []
        seen_links = set()
        scanned = 0
        for element in root.iter():
            if self._local_name(element.tag) != "item":
                continue
            scanned += 1
            if scanned > 200:
                break
            fields = {}
            for child in list(element):
                name = self._local_name(child.tag)
                if name in {"title", "link", "description", "pubdate"} and name not in fields:
                    fields[name] = "".join(child.itertext())
            title = self._plain_text(fields.get("title", ""), 120)
            link = fields.get("link", "").strip()
            if not title or not link or any(ord(char) < 32 for char in link):
                continue
            try:
                parsed = urlparse(link)
                host = (parsed.hostname or "").casefold()
                valid_host = host == "chinanews.com.cn" or host.endswith(".chinanews.com.cn")
                if (
                    parsed.scheme not in {"http", "https"} or not valid_host
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in {None, 80, 443}
                ):
                    continue
                published = parsedate_to_datetime(fields.get("pubdate", ""))
                if published.tzinfo is None or published.utcoffset() is None:
                    continue
            except (TypeError, ValueError, OverflowError, IndexError):
                continue
            canonical_link = link.split("#", 1)[0]
            if canonical_link in seen_links:
                continue
            seen_links.add(canonical_link)
            items.append((published, {
                "title": title,
                "snippet": self._plain_text(fields.get("description", ""), 240),
                "domain": host.removeprefix("www."),
                "published_at": published.isoformat(timespec="seconds"),
            }))
        with self._lock:
            self._cache[filename] = (self._monotonic(), copy.deepcopy(items))
            if len(self._cache) > self.MAX_CACHE_ENTRIES:
                oldest = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest, None)
        return items

    def _read(self, url: str) -> bytes:
        request = Request(url, headers={
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            "Accept-Encoding": "identity",
            "User-Agent": "xiaoshengchang-smart-speaker/0.4",
        }, method="GET")
        try:
            with self._opener.open(request, timeout=self.TIMEOUT_SECONDS) as response:
                if response.geturl() != url or response.status != 200:
                    raise NewsFeedError("新闻来源发生了重定向或返回异常状态，查询未完成。")
                media_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
                if media_type not in {"text/xml", "application/xml", "application/rss+xml"}:
                    raise NewsFeedError("新闻来源返回的不是 RSS 新闻内容。")
                encoding = str(response.headers.get("Content-Encoding", "")).strip().casefold()
                if encoding not in {"", "identity"}:
                    raise NewsFeedError("新闻来源返回了不支持的编码。")
                length = str(response.headers.get("Content-Length", "")).strip()
                if length.isdigit() and int(length) > self.MAX_RESPONSE_BYTES:
                    raise NewsFeedError("新闻来源返回内容过大。")
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise NewsFeedError("新闻来源发生了重定向，查询未完成。") from exc
            raise NewsFeedError("新闻来源暂时不可用，请稍后重试。") from exc
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise NewsFeedError("无法连接新闻来源，请检查网络后重试。") from exc
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise NewsFeedError("新闻来源返回内容过大。")
        if not raw.strip():
            raise NewsFeedError("新闻来源返回了空内容。")
        return raw

    @staticmethod
    def _local_name(tag: Any) -> str:
        return str(tag or "").rsplit("}", 1)[-1].casefold()

    @staticmethod
    def _plain_text(value: str, maximum: int) -> str:
        text = html.unescape(value)
        text = re.sub(r"<[^>]{0,1000}>", " ", text)
        text = " ".join(html.unescape(text).split())
        return text if len(text) <= maximum else text[:maximum - 1].rstrip() + "…"
