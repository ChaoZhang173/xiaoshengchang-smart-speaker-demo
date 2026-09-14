import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from xml.sax.saxutils import escape


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_feed import NewsFeedClient, NewsFeedError, _NoNewsRedirectHandler  # noqa: E402


LOCAL_ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 14, 10, 30, tzinfo=LOCAL_ZONE)


def item(title="新闻标题", published=None, *, link=None, description="报道摘要"):
    date = format_datetime(published or NOW - timedelta(minutes=5))
    return xml_item(title, date, link=link, description=description)


def xml_item(title, date, *, link=None, description="报道摘要"):
    link = link or "https://www.chinanews.com.cn/test/" + str(abs(hash(title))) + ".shtml"
    return "<item><title>%s</title><link>%s</link><description>%s</description><pubDate>%s</pubDate></item>" % tuple(
        escape(value) for value in (title, link, description, date)
    )


def feed(*items):
    return ("<?xml version='1.0' encoding='UTF-8'?><rss><channel>" + "".join(items) + "</channel></rss>").encode("utf-8")


class FakeResponse:
    def __init__(self, raw, *, headers=None, final_url=None, status=200):
        self.raw = raw
        self.headers = {"Content-Type": "text/xml", **(headers or {})}
        self.final_url = final_url
        self.status = status
        self.read_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, maximum):
        self.read_count += 1
        return self.raw[:maximum]


class RecordingOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        if self.response.final_url is None:
            self.response.final_url = request.full_url
        return self.response


class NewsFeedTests(unittest.TestCase):
    def make_client(self, raw, *, headers=None, final_url=None):
        self.now = NOW
        self.ticks = 100.0
        response = FakeResponse(raw, headers=headers, final_url=final_url)
        opener = RecordingOpener(response)
        client = NewsFeedClient(
            opener=opener, clock=lambda: self.now, monotonic=lambda: self.ticks
        )
        return client, opener

    def test_today_returns_newest_three_dated_stories_from_fixed_endpoint(self):
        client, opener = self.make_client(feed(
            item("较早新闻", NOW - timedelta(hours=2)),
            item("最新新闻", NOW - timedelta(minutes=1)),
            item("第三条", NOW - timedelta(minutes=3)),
            item("第二条", NOW - timedelta(minutes=2)),
        ))
        result = client.search("今天发生了什么新闻")
        self.assertEqual([entry["title"] for entry in result["results"]], ["最新新闻", "第二条", "第三条"])
        self.assertIn("单一新闻源", result["coverage_note"])
        self.assertIn("并非全网", result["coverage_note"])
        self.assertIn("不等同于事件发生时间", result["coverage_note"])
        self.assertEqual(result["retrieved_at"], NOW.isoformat(timespec="seconds"))
        self.assertTrue(all(entry["published_at"].endswith("+08:00") for entry in result["results"]))
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://www.chinanews.com.cn/rss/scroll-news.xml")
        self.assertEqual(timeout, 10)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertNotIn("query", request.full_url)

    def test_today_filters_old_future_missing_and_timezone_free_publication_dates(self):
        client, _ = self.make_client(feed(
            item("有效新闻"),
            item("昨天新闻", NOW.replace(hour=0, minute=0) - timedelta(seconds=1)),
            item("未来新闻", NOW + timedelta(seconds=1)),
            xml_item("缺少时间", ""),
            xml_item("无时区", "Mon, 14 Sep 2026 09:00:00"),
            xml_item("坏时间", "not a date"),
        ))
        result = client.search("今天新闻")
        self.assertEqual([entry["title"] for entry in result["results"]], ["有效新闻"])

    def test_today_uses_local_day_instead_of_utc_day(self):
        client, _ = self.make_client(feed(
            item("当地今天", datetime(2026, 9, 13, 17, tzinfo=timezone.utc)),
            item("当地昨天", datetime(2026, 9, 13, 15, tzinfo=timezone.utc)),
        ))
        self.assertEqual(client.search("今天新闻")["results"][0]["title"], "当地今天")
        self.assertEqual(len(client.search("今天新闻")["results"]), 1)

    def test_recent_includes_at_most_seven_days_and_filters_future(self):
        client, _ = self.make_client(feed(
            item("六天前", NOW - timedelta(days=6)),
            item("七天边界", NOW - timedelta(days=7)),
            item("更早", NOW - timedelta(days=7, seconds=1)),
            item("未来", NOW + timedelta(days=1)),
        ))
        self.assertEqual(
            [entry["title"] for entry in client.search("最近新闻", period="recent")["results"]],
            ["六天前", "七天边界"],
        )

    def test_cold_topic_never_receives_unrelated_stories(self):
        client, opener = self.make_client(feed(item("公园开放")))
        with self.assertRaisesRegex(NewsFeedError, "没有找到.*符合这个主题"):
            client.search("今天夸克芯片公司新闻", topic="夸克芯片公司")
        self.assertEqual(opener.requests[0][0].full_url, NewsFeedClient.BASE_URL + "scroll-news.xml")

    def test_topic_filters_locally_and_english_matching_respects_word_boundaries(self):
        client, _ = self.make_client(feed(
            item("OpenAI 发布新消息"), item("OpenAIR 展会开幕"), item("不相关内容")
        ))
        result = client.search("最新 OpenAI 新闻", topic="OpenAI")
        self.assertEqual([entry["title"] for entry in result["results"]], ["OpenAI 发布新消息"])

    def test_categories_use_only_the_corresponding_official_feed(self):
        for topic, filename in (("国际", "world.xml"), ("finance", "finance.xml"), ("社会", "society.xml"), ("体育", "sports.xml")):
            with self.subTest(topic=topic):
                client, opener = self.make_client(feed(item("栏目新闻")))
                self.assertEqual(client.search("今日相关新闻", topic=topic)["results"][0]["title"], "栏目新闻")
                self.assertEqual(opener.requests[0][0].full_url, NewsFeedClient.BASE_URL + filename)

    def test_no_current_entries_fails_without_returning_undated_or_old_content(self):
        for raw in (feed(), feed(item("旧新闻", NOW - timedelta(days=10)))):
            client, _ = self.make_client(raw)
            with self.assertRaisesRegex(NewsFeedError, "发布时间可核实"):
                client.search("今天新闻")

    def test_cache_is_bounded_short_lived_and_refilters_after_midnight(self):
        client, opener = self.make_client(feed(item("午夜前新闻", NOW.replace(hour=23, minute=59))))
        self.now = NOW.replace(hour=23, minute=59, second=30)
        first = client.search("今天新闻")
        first["results"][0]["title"] = "修改返回副本"
        self.assertEqual(client.search("再查今天新闻")["results"][0]["title"], "午夜前新闻")
        self.assertEqual(len(opener.requests), 1)
        self.now += timedelta(seconds=40)
        self.ticks += 40
        with self.assertRaisesRegex(NewsFeedError, "没有找到"):
            client.search("今天新闻")
        self.assertEqual(len(opener.requests), 1)
        self.ticks += 21
        with self.assertRaises(NewsFeedError):
            client.search("今天新闻")
        self.assertEqual(len(opener.requests), 2)

    def test_cache_entry_limit_is_enforced(self):
        client, opener = self.make_client(feed(item("栏目新闻")))
        client.MAX_CACHE_ENTRIES = 2
        for topic in ("国际", "财经", "体育"):
            self.ticks += 1
            opener.response.final_url = None
            client.search("今日新闻", topic=topic)
        self.assertEqual(len(client._cache), 2)
        self.assertNotIn("world.xml", client._cache)

    def test_html_wrong_root_malformed_and_unsafe_xml_are_rejected(self):
        cases = (
            (b"<html>blocked</html>", {"Content-Type": "text/html"}),
            (b"<html>blocked</html>", {}),
            (b"<rss>", {}),
            (b"<!DOCTYPE rss [<!ENTITY x 'bad'>]><rss/>", {}),
            ("<!DOCTYPE rss><rss/>".encode("utf-16"), {}),
            (b"", {}),
            (feed(item()), {"Content-Encoding": "gzip"}),
        )
        for raw, headers in cases:
            with self.subTest(raw=raw[:40], headers=headers):
                client, _ = self.make_client(raw, headers=headers)
                with self.assertRaises(NewsFeedError):
                    client.search("今日新闻")

    def test_oversized_content_is_rejected_with_and_without_length_header(self):
        client, opener = self.make_client(b"x" * (NewsFeedClient.MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(NewsFeedError, "内容过大"):
            client.search("今日新闻")
        self.assertEqual(opener.response.read_count, 1)
        client, opener = self.make_client(feed(item()), headers={"Content-Length": str(NewsFeedClient.MAX_RESPONSE_BYTES + 1)})
        with self.assertRaisesRegex(NewsFeedError, "内容过大"):
            client.search("今日新闻")
        self.assertEqual(opener.response.read_count, 0)

    def test_all_redirects_and_changed_final_urls_are_rejected(self):
        handler = _NoNewsRedirectHandler()
        url = NewsFeedClient.BASE_URL + "scroll-news.xml"
        for destination in (url, NewsFeedClient.BASE_URL + "world.xml", "https://attacker.invalid/news"):
            self.assertIsNone(handler.redirect_request(Request(url), None, 302, "Found", {}, destination))
        client, _ = self.make_client(feed(item()), final_url="https://attacker.invalid/news")
        with self.assertRaisesRegex(NewsFeedError, "重定向"):
            client.search("今日新闻")
        error = HTTPError(url, 302, "Found", {}, None)
        client = NewsFeedClient(opener=RecordingOpener(error), clock=lambda: NOW)
        with self.assertRaisesRegex(NewsFeedError, "重定向"):
            client.search("今日新闻")

    def test_network_errors_are_safe_and_no_story_links_are_opened(self):
        client = NewsFeedClient(opener=RecordingOpener(URLError("internal detail")), clock=lambda: NOW)
        with self.assertRaisesRegex(NewsFeedError, "无法连接") as raised:
            client.search("今日新闻")
        self.assertNotIn("internal detail", str(raised.exception))
        client, opener = self.make_client(feed(item("正常新闻"), item("假域名", link="https://chinanews.com.cn.attacker.invalid/test")))
        self.assertEqual(len(client.search("今日新闻")["results"]), 1)
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_inputs_and_clock_fail_before_network(self):
        for kwargs in ({"period": "tomorrow"}, {"topic": "x" * 81}, {"topic": "bad\nvalue"}):
            client, opener = self.make_client(feed(item()))
            with self.assertRaises(NewsFeedError):
                client.search("今日新闻", **kwargs)
            self.assertEqual(opener.requests, [])
        client, opener = self.make_client(feed(item()))
        self.now = NOW.replace(tzinfo=None)
        with self.assertRaisesRegex(NewsFeedError, "时区"):
            client.search("今日新闻")
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
