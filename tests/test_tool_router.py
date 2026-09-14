import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tool_router import (  # noqa: E402
    BingRssSearchClient,
    PublicSearchClient,
    MAX_TOOL_RESPONSE_BYTES,
    ToolInputError,
    ToolNetworkError,
    ToolRouter,
    WeatherClient,
    _BingRedirectHandler,
    public_radio_stations,
)


class FakeHTTPResponse:
    def __init__(self, payload, headers=None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def read(self, maximum):
        return self.payload[:maximum]


class FakeXmlResponse(FakeHTTPResponse):
    def __init__(self, payload, headers=None):
        self.payload = payload.encode("utf-8")
        self.headers = {"Content-Type": "text/xml; charset=utf-8"}
        if headers:
            self.headers.update(headers)


class RecordingWeatherOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.responses.pop(0)


class FakeWeather:
    def __init__(self):
        self.cities = []

    def current(self, city):
        self.cities.append(city)
        return {
            "source": "Open-Meteo",
            "location": "上海，中国",
            "observed_at": "2026-09-01T16:00",
            "timezone": "Asia/Shanghai",
            "condition": "多云",
            "temperature_c": 27.5,
            "apparent_temperature_c": 29.0,
            "relative_humidity_percent": 70,
            "precipitation_mm": 0.0,
            "wind_speed_kmh": 8.4,
        }


class FakeSearch:
    def __init__(self, error=None):
        self.queries = []
        self.error = error

    def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return {
            "source": "Bing RSS（实验性）",
            "query": query,
            "results": [
                {
                    "title": "示例搜索结果",
                    "snippet": "这是一个网页摘要。",
                    "domain": "example.com",
                }
            ],
        }


class ToolRouterTests(unittest.TestCase):
    def setUp(self):
        self.weather = FakeWeather()
        self.search = FakeSearch()
        self.router = ToolRouter(
            weather_client=self.weather,
            search_client=self.search,
        )

    def test_definitions_only_expose_registered_functions(self):
        definitions = self.router.definitions()
        names = {item["function"]["name"] for item in definitions}
        self.assertEqual(
            names,
            {
                "get_local_time",
                "get_weather",
                "web_search",
                "play_radio",
                "pause_music",
                "resume_music",
                "stop_music",
                "set_music_volume",
            },
        )
        serialized = json.dumps(definitions, ensure_ascii=False)
        self.assertNotIn("stream_url", serialized)
        self.assertNotIn("eval", serialized)
        search = next(
            item["function"]
            for item in definitions
            if item["function"]["name"] == "web_search"
        )
        self.assertEqual(search["parameters"]["required"], ["query"])
        self.assertEqual(search["parameters"]["properties"]["query"]["maxLength"], 200)

    def test_unknown_tool_bad_json_and_extra_fields_are_rejected(self):
        with self.assertRaises(ToolInputError):
            self.router.execute("shell", {})
        with self.assertRaises(ToolInputError):
            self.router.execute("get_weather", "not-json")
        with self.assertRaises(ToolInputError):
            self.router.execute("get_weather", {"city": "上海", "url": "http://127.0.0.1"})
        with self.assertRaises(ToolInputError):
            self.router.execute(
                "web_search",
                {"query": "测试", "url": "http://127.0.0.1"},
            )

    def test_weather_uses_structured_result(self):
        result = self.router.execute("get_weather", '{"city":"上海"}')
        self.assertEqual(self.weather.cities, ["上海"])
        self.assertEqual(result.payload["source"], "Open-Meteo")
        self.assertIn("27.5", result.spoken_fallback)
        self.assertEqual(result.client_actions, [])

    def test_unindexed_chinese_place_retries_pinyin_and_keeps_chinese_label(self):
        client = WeatherClient()
        opener = RecordingWeatherOpener([
            FakeHTTPResponse({"results": []}),
            FakeHTTPResponse({"results": [
                {"name": "Gaomin", "country_code": "CN", "latitude": 26.0,
                 "longitude": 99.0, "population": 999999},
                {"name": "Gaomi", "country_code": "US", "latitude": 36.0,
                 "longitude": -80.0, "population": 999999},
                {"name": "Gaomi", "country_code": "CN", "latitude": 36.38,
                 "longitude": 119.75, "admin1": "山东", "country": "中国"},
            ]}),
            FakeHTTPResponse({"current": {
                "temperature_2m": 24.6, "apparent_temperature": 25.0,
                "relative_humidity_2m": 62, "wind_speed_10m": 6.0,
                "weather_code": 1, "precipitation": 0,
            }}),
        ])
        client._opener = opener
        with patch.object(WeatherClient, "_romanized_place_name", return_value="gaomi"):
            payload = client.current("高密")
        self.assertEqual(payload["location"], "高密，山东，中国")
        self.assertEqual(payload["temperature_c"], 24.6)
        self.assertIn("name=gaomi", opener.calls[1][0].full_url)
        self.assertIn("latitude=36.38000", opener.calls[2][0].full_url)
        self.assertIn("longitude=119.75000", opener.calls[2][0].full_url)

    def test_pinyin_conversion_is_local_bounded_and_not_executable_input(self):
        with patch("tool_router.sys.platform", "darwin"), patch("tool_router.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "gao mi\n"
            self.assertEqual(WeatherClient._romanized_place_name("高密"), "gaomi")
            self.assertEqual(run.call_args.args[0][-1], "高密")
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(WeatherClient._romanized_place_name("高密;open bad"), "")
            self.assertEqual(run.call_count, 1)

    def test_search_uses_structured_result(self):
        result = self.router.execute("web_search", '{"query":"OpenAI 最新消息"}')
        self.assertEqual(self.search.queries, ["OpenAI 最新消息"])
        self.assertEqual(result.payload["source"], "Bing RSS（实验性）")
        self.assertIn("example.com", result.spoken_fallback)
        self.assertEqual(result.client_actions, [])

    def test_dated_news_does_not_use_ordinary_undated_web_results(self):
        web = FakeSearch()
        with patch("tool_router.NewsFeedClient") as news_type:
            news = news_type.return_value
            news.search.return_value = {"source": "中新网", "results": []}
            client = PublicSearchClient(web=web, news=news)
            client.search("今天发生了什么新闻")
            news.search.assert_called_once_with(
                "今天发生了什么新闻", topic="", period="today"
            )
            self.assertEqual(web.queries, [])
            from news_feed import NewsFeedError
            news.search.side_effect = NewsFeedError("当前来源没有符合时间范围的新闻。")
            with self.assertRaises(ToolNetworkError):
                client.search("今天的新闻")
            self.assertEqual(web.queries, [])
            client.search("Python 官方文档")
            self.assertEqual(web.queries, ["Python 官方文档"])

    def test_radio_action_contains_id_not_model_visible_url(self):
        station = public_radio_stations()[0]
        result = self.router.execute("play_radio", {"station_id": station["id"]})
        self.assertEqual(result.client_actions[0]["type"], "music.play")
        self.assertEqual(result.client_actions[0]["station_id"], station["id"])
        self.assertNotIn("stream_url", result.model_content())
        with self.assertRaises(ToolInputError):
            self.router.execute("play_radio", {"station_id": "https://attacker.invalid"})

    def test_volume_is_bounded_and_boolean_is_not_an_integer(self):
        self.assertEqual(
            self.router.execute("set_music_volume", {"volume": 35}).client_actions,
            [{"type": "music.set_volume", "volume": 35}],
        )
        for value in (-1, 101, True, "35"):
            with self.subTest(value=value):
                with self.assertRaises(ToolInputError):
                    self.router.execute("set_music_volume", {"volume": value})

    def test_mock_weather_fallback_does_not_make_network_call(self):
        result = self.router.deterministic_match("上海天气怎么样", allow_network=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "get_weather")
        self.assertEqual(self.weather.cities, [])

    def test_natural_weather_phrases_extract_only_the_city(self):
        cases = {
            "查询上海天气": "上海",
            "请查询上海现在的天气": "上海",
            "帮我查一下上海天气": "上海",
            "给我查下上海天气": "上海",
            "今天上海天气怎么样": "上海",
            "请告诉我上海当前天气": "上海",
            "能帮我查一下上海实时天气": "上海",
            "和田天气": "和田",
            "共和县天气": "共和县",
            "我想知道 Paris, France 的天气": "Paris, France",
            "what is the weather in Shanghai?": "shanghai",
        }
        for text, city in cases.items():
            with self.subTest(text=text):
                self.router.deterministic_match(text)
                self.assertEqual(self.weather.cities[-1], city)

    def test_music_intent_accepts_only_complete_positive_commands(self):
        positive = {
            "请播放音乐": ("play_radio", {"station_id": "soma_groove_salad"}),
            "暂停播放": ("pause_music", {}),
            "继续播放": ("resume_music", {}),
            "停止音乐": ("stop_music", {}),
            "音乐音量设置为 35": ("set_music_volume", {"volume": 35}),
            "please play music!": (
                "play_radio",
                {"station_id": "soma_groove_salad"},
            ),
        }
        for text, (tool_name, arguments) in positive.items():
            with self.subTest(text=text):
                intent = self.router.music_intent(text)
                self.assertEqual(intent["tool_name"], tool_name)
                self.assertEqual(intent["arguments"], arguments)

        non_commands = (
            "如果我说播放音乐，你会做什么？",
            "我刚才听见别人说播放音乐",
            "千万别突然播放音乐",
            "你会播放音乐吗？",
            "怎么播放音乐？",
            "解释一下“播放音乐”这个命令",
            "为什么你总是播放音乐？",
            "为什么没有播放音乐？",
            "把“音量设置为100”翻译成英文",
            "不要把音乐音量调到 100",
            "请不要将音乐音量设置为 100",
            "别把音乐音量设置为 100",
            "不要自动播放音乐",
            "不要现在播放音乐",
            "我不想听你播放音乐",
        )
        for text in non_commands:
            with self.subTest(text=text):
                self.assertIsNone(self.router.music_intent(text))
                self.assertIsNone(self.router.deterministic_match(text))

    def test_weather_future_multiple_and_missing_city_do_not_query_network(self):
        for text, expected_key in (
            ("上海明天天气", "unsupported_period"),
            ("昨天天气怎么样", "unsupported_period"),
            ("上海和北京天气", "multiple_cities"),
            ("上海，北京天气", "multiple_cities"),
            ("今天天气怎么样", "missing_city"),
        ):
            with self.subTest(text=text):
                result = self.router.deterministic_match(text)
                self.assertTrue(result.payload[expected_key])
        self.assertEqual(self.weather.cities, [])

    def test_weather_rejects_questions_about_commands_and_private_context(self):
        non_commands = (
            "你会查询上海天气吗？",
            "怎么查询上海天气？",
            "把“上海天气”翻译成英文",
            "解释一下天气查询功能",
            "不要自动查询上海天气",
            "我不想让你查询上海天气",
            "别替我查询北京现在的天气，我只是在举例",
            "请解释“北京天气怎么样”这句话",
            "会议记录里写着“查询北京天气”，请原样复述",
            "我的住址是北京朝阳区建国路88号请解释天气这个词",
            "我患有抑郁症请查询上海天气",
            "张三住在我家请查询上海天气",
            "我怀孕了上海天气怎么样",
            "我得了癌症上海天气怎么样",
            "请查询我叫张三上海天气",
            "请查询张三是我男朋友北京天气",
            "张三家在海淀区天气怎么样",
            "王老师住浦东新区天气怎么样",
            "张三的性取向是同性恋海淀区天气怎么样",
            "查询张三得了帕金森病县的天气",
            "查询张三患阿尔茨海默病市天气",
            "what is the weather in London I have cancer",
            "请莫查上海天气",
            "please do not get London weather",
        )
        for text in non_commands:
            with self.subTest(text=text):
                self.assertIsNone(self.router.weather_intent(text))
                self.assertIsNone(self.router.deterministic_match(text))
        self.assertEqual(self.weather.cities, [])

    def test_search_and_weather_have_one_host_selected_destination(self):
        for text, query in (
            ("联网搜索 北京天气", "北京天气"),
            ("请搜索上海天气", "上海天气"),
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.router.weather_intent(text))
                result = self.router.deterministic_match(text)
                self.assertEqual(result.name, "web_search")
                self.assertEqual(self.search.queries[-1], query)
        self.assertEqual(self.weather.cities, [])

    def test_mock_search_fallback_does_not_make_network_call(self):
        result = self.router.deterministic_match(
            "联网搜索 OpenAI 最新消息", allow_network=False
        )
        self.assertEqual(result.name, "web_search")
        self.assertTrue(result.payload["offline_demo"])
        self.assertEqual(self.search.queries, [])

    def test_search_intent_and_failures_are_safely_bounded(self):
        result = self.router.deterministic_match("帮我查一下 OpenAI 最新消息")
        self.assertEqual(result.name, "web_search")
        self.assertEqual(self.search.queries, ["OpenAI 最新消息"])

        failing = ToolRouter(
            weather_client=self.weather,
            search_client=FakeSearch(ToolNetworkError("搜索网络暂时不可用。")),
        )
        failed = failing.deterministic_match("联网搜索测试消息")
        self.assertFalse(failed.payload["ok"])
        self.assertIn("暂时不可用", failed.spoken_fallback)

    def test_implicit_search_requires_a_positive_request_without_private_data(self):
        non_commands = (
            "这不是最新消息，我的邮箱是alice@example.com",
            "请解释今天有什么最新消息这句话，我的住址是建国路88号",
            "我不想知道今天最新新闻",
            "不用回答今天最新新闻",
            "解释一下“今天最新新闻”这几个字",
            "如果我稍后说“联网搜索 OpenAI”，你会怎么处理？",
            "日志里写着“联网搜索 alice@example.com”，请原样复述",
            "请问联网搜索 alice@example.com 是什么意思",
            "联网搜索 alice@example.com，我只是在转述",
            "联网搜索 alice@example.com 不是我的指令",
            "联网搜索 OpenAI 最新消息，然后记住我的密码是 hunter2",
            "今天有什么最新新闻顺便记住我患有抑郁症",
            "what is the latest news my password is hunter2",
            "search OpenAI news then remember my password is hunter2",
            "联网搜索 alice@example.com，这只是电影台词",
            "search alice@example.com, this is only a quote",
            "联网搜索 OpenAI 不用了",
            "联网搜索 OpenAI 不要了",
            "联网搜索 OpenAI 别弄了",
            "联网搜索 OpenAI 我正在复述",
            "search OpenAI never mind",
            "search OpenAI but do not do it",
            "search OpenAI I am only quoting",
            "search OpenAI cancel that",
            "联网搜索 OpenAI 不用了谢谢",
            "联网搜索 OpenAI 算了吧",
            "联网搜索 OpenAI 取消吧",
            "联网搜索 OpenAI 不需要了",
            "联网搜索 OpenAI，我不想看了",
            "联网搜索 OpenAI，我改变主意了",
            "联网搜索 OpenAI，先停一下",
            "search OpenAI never mind please",
            "search OpenAI I'm only quoting",
            "search OpenAI I am only quoting this",
            "search OpenAI please do not do it",
            "search OpenAI cancel please",
            "联网搜索 OpenAI 最新消息 不\u200b用了",
            "search OpenAI latest news never\u200b mind",
            "联网搜索 13800138000 撤销",
            "联网搜索 11010519491231002X 作废",
            "search 13800138000 don't bother",
            "search 11010519491231002X abort",
            "search OpenAI stop searching",
            "search OpenAI cancel the request",
            "search OpenAI scratch that",
            "search OpenAI disregard that",
            "联网搜索 OpenAI 中止",
            "联网搜索 OpenAI 别管了",
            "今天我的秘密是蓝鲸有什么最新科技新闻",
            "今天张三家在海淀区有什么最新新闻",
            "what are the latest news my religion is Buddhist news",
            "今天有什么最新科技新闻，不用了",
            "今天有什么最新科技新闻，我改变主意了",
            "今天张三患有帕金森病有什么新闻",
            "今天张三的性取向是同性恋有什么新闻",
        )
        for text in non_commands:
            with self.subTest(text=text):
                self.assertEqual(self.router.search_query_for_text(text), "")
                self.assertIsNone(self.router.authorized_intent(text))
                self.assertIsNone(self.router.deterministic_match(text))
        self.assertEqual(self.search.queries, [])

        allowed = self.router.deterministic_match("联网搜索 OpenAI 取消政策")
        self.assertEqual(allowed.name, "web_search")
        self.assertEqual(self.search.queries, ["OpenAI 取消政策"])
        for text, expected in (
            ("联网搜索 OpenAI 撤销政策", "OpenAI 撤销政策"),
            ("search HTTP request abort behavior", "HTTP request abort behavior"),
            ("search BIP39 seed phrase security", "BIP39 seed phrase security"),
        ):
            with self.subTest(allowed=text):
                intent = self.router.authorized_intent(text)
                self.assertEqual(intent["tool_name"], "web_search")
                self.assertEqual(intent["arguments"]["query"], expected)

    def test_search_secret_detection_normalizes_unicode_and_format_controls(self):
        non_commands = (
            "联网搜索 我的PIN是7391",
            "联网搜索 我的私钥是ABCDEF123456",
            "联网搜索 private key is ABCDEF123456",
            "联网搜索 我的ｐａｓｓｗｏｒｄ是hunter2",
            "search my pass\u200bword is hunter2",
            "联网搜索 我的密\u200b码是hunter2",
            "联网搜索 pass\ufe0fword是hunter2",
            "search pass\u034fword is hunter2",
            "联网搜索 密 码是hunter2",
            "search pass word is hunter2",
            "search pass-word is hunter2",
            "search pass\u2065word is hunter2",
            "search pass\x7fword is hunter2",
            "search ｍｙ HIV status",
            "search m\ufe0fy medical record",
            "search ｍｅ cancer diagnosis",
            "search ｍｙ phone 13800138000",
            "联网搜索 我喜欢的助记词是apple book candle door eagle forest grape hotel island jungle king lemon",
            "联网搜索 恢复码是12345678",
            "search recovery code is 12345678",
        )
        for text in non_commands:
            with self.subTest(text=text):
                self.assertIsNone(self.router.authorized_intent(text))
                self.assertIsNone(self.router.deterministic_match(text))
        self.assertEqual(self.search.queries, [])

    def test_implicit_search_exports_only_the_standalone_request_clause(self):
        cases = {
            "我被诊断为抑郁症，今天有什么最新新闻？": "今天有什么最新新闻",
            "我工资三万元，今天有哪些最新财经消息？": "今天有哪些最新财经消息",
        }
        for text, expected_query in cases.items():
            with self.subTest(text=text):
                result = self.router.deterministic_match(text)
                self.assertEqual(result.name, "web_search")
                self.assertEqual(self.search.queries[-1], expected_query)
                self.assertNotIn("抑郁症", self.search.queries[-1])
                self.assertNotIn("工资", self.search.queries[-1])

    def test_negated_commands_never_execute_or_query_network(self):
        for text in (
            "不要播放音乐",
            "不要停止音乐",
            "我不想查询上海天气",
            "不要给我播放音乐",
            "请不要再帮我播放音乐",
            "我不需要你查询上海天气",
            "不查上海天气",
            "不要联网搜索 OpenAI",
            "别搜最新新闻",
            "do not play music",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.router.deterministic_match(text))
        self.assertEqual(self.weather.cities, [])
        self.assertEqual(self.search.queries, [])

    def test_weather_condition_mapping(self):
        self.assertEqual(WeatherClient._condition(0), "晴")
        self.assertEqual(WeatherClient._condition(63), "有雨")
        self.assertEqual(WeatherClient._condition(95), "雷雨")

    def test_weather_http_client_uses_fixed_hosts_without_authorization(self):
        client = WeatherClient()
        opener = RecordingWeatherOpener(
            [
                FakeHTTPResponse(
                    {
                        "results": [
                            {
                                "name": "上海",
                                "country": "中国",
                                "latitude": 31.23,
                                "longitude": 121.47,
                            }
                        ]
                    }
                ),
                FakeHTTPResponse(
                    {
                        "timezone": "Asia/Shanghai",
                        "current": {
                            "time": "2026-09-01T18:00",
                            "temperature_2m": 28.0,
                            "apparent_temperature": 30.0,
                            "relative_humidity_2m": 70,
                            "precipitation": 0,
                            "weather_code": 2,
                            "wind_speed_10m": 6.0,
                        },
                    }
                ),
            ]
        )
        client._opener = opener
        result = client.current("上海")
        self.assertEqual(result["source"], "Open-Meteo")
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(
            opener.calls[0][0].full_url.startswith(
                "https://geocoding-api.open-meteo.com/v1/search?"
            )
        )
        self.assertTrue(
            opener.calls[1][0].full_url.startswith(
                "https://api.open-meteo.com/v1/forecast?"
            )
        )
        for request, timeout in opener.calls:
            headers = {key.casefold(): value for key, value in request.header_items()}
            self.assertNotIn("authorization", headers)
            self.assertEqual(timeout, 9)

    def test_weather_normalizes_city_suffix_and_ranks_multiple_results(self):
        client = WeatherClient()
        opener = RecordingWeatherOpener(
            [
                FakeHTTPResponse(
                    {
                        "results": [
                            {
                                "name": "上海市",
                                "country": "美国",
                                "admin1": "伊利诺伊州",
                                "feature_code": "PPL",
                                "population": 50,
                                "latitude": 41.05,
                                "longitude": -90.49,
                            },
                            {
                                "name": "上海",
                                "country": "中国",
                                "admin1": "上海市",
                                "feature_code": "PPLA",
                                "population": 24_000_000,
                                "latitude": 31.23,
                                "longitude": 121.47,
                            },
                        ]
                    }
                ),
                FakeHTTPResponse(
                    {
                        "timezone": "Asia/Shanghai",
                        "current": {
                            "time": "2026-09-02T12:00",
                            "temperature_2m": 28,
                            "apparent_temperature": 31,
                            "relative_humidity_2m": 70,
                            "precipitation": 0,
                            "weather_code": 2,
                            "wind_speed_10m": 6,
                        },
                    }
                ),
            ]
        )
        client._opener = opener
        result = client.current("上海市")
        self.assertEqual(result["location"], "上海，上海市，中国")
        self.assertIn("name=%E4%B8%8A%E6%B5%B7", opener.calls[0][0].full_url)
        self.assertNotIn("%E5%B8%82", opener.calls[0][0].full_url)

    def test_weather_qualifier_disambiguates_same_named_cities(self):
        results = [
            {
                "name": "Paris",
                "country": "France",
                "country_code": "FR",
                "admin1": "Ile-de-France",
                "feature_code": "PPLC",
                "population": 2_100_000,
                "latitude": 48.85,
                "longitude": 2.35,
            },
            {
                "name": "Paris",
                "country": "United States",
                "country_code": "US",
                "admin1": "Texas",
                "feature_code": "PPLA2",
                "population": 25_000,
                "latitude": 33.66,
                "longitude": -95.55,
            },
        ]
        texas = WeatherClient._select_location(results, "Paris, Texas")
        france = WeatherClient._select_location(results, "Paris, France")
        self.assertEqual(texas["admin1"], "Texas")
        self.assertEqual(france["country_code"], "FR")

    def test_bing_redirects_only_between_fixed_search_hosts(self):
        handler = _BingRedirectHandler()
        original = Request(
            "https://www.bing.com/search?q=test&format=rss",
            headers={"Authorization": "Bearer must-not-forward", "Cookie": "secret"},
        )
        allowed = handler.redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "https://cn.bing.com/search?q=test&format=rss&mkt=zh-CN",
        )
        self.assertIsNotNone(allowed)
        allowed_headers = {
            key.casefold(): value for key, value in allowed.header_items()
        }
        self.assertNotIn("authorization", allowed_headers)
        self.assertNotIn("cookie", allowed_headers)
        self.assertIsNone(
            handler.redirect_request(
                original,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/search?q=test&format=rss",
            )
        )
        self.assertIsNone(
            handler.redirect_request(
                original,
                None,
                302,
                "Found",
                {},
                "https://cn.bing.com/other?q=test&format=rss",
            )
        )

    def test_bing_rss_search_is_fixed_host_sanitized_and_bounded(self):
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item><title>第一条 &amp; 结果</title>
            <link>https://example.com/a#fragment</link>
            <description>&lt;b&gt;摘要&lt;/b&gt; 中的内容</description></item>
          <item><title>第二条结果</title>
            <link>https://news.example.org/b</link>
            <description>另一个摘要</description></item>
        </channel></rss>"""
        client = BingRssSearchClient()
        opener = RecordingWeatherOpener([FakeXmlResponse(rss)])
        client._opener = opener
        result = client.search("OpenAI 最新消息")
        self.assertEqual(result["results"][0]["title"], "第一条 & 结果")
        self.assertEqual(result["results"][0]["snippet"], "摘要 中的内容")
        self.assertEqual(result["results"][0]["domain"], "example.com")
        self.assertNotIn("url", result["results"][0])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 1800)
        request, timeout = opener.calls[0]
        self.assertTrue(request.full_url.startswith("https://www.bing.com/search?"))
        self.assertIn("format=rss", request.full_url)
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertEqual(timeout, 10)

    def test_bing_rss_rejects_html_and_unsafe_xml(self):
        for response in (
            FakeXmlResponse("<html></html>", {"Content-Type": "text/html"}),
            FakeXmlResponse('<!DOCTYPE rss [<!ENTITY x "bad">]><rss></rss>'),
        ):
            with self.subTest(payload=response.payload[:30]):
                client = BingRssSearchClient()
                client._opener = RecordingWeatherOpener([response])
                with self.assertRaises(ToolNetworkError):
                    client.search("安全测试")

    def test_weather_response_size_limit_is_bounded(self):
        response = FakeHTTPResponse({"results": []})
        response.headers = {"Content-Length": str(MAX_TOOL_RESPONSE_BYTES + 1)}
        client = WeatherClient()
        client._opener = RecordingWeatherOpener([response])
        with self.assertRaisesRegex(ToolNetworkError, "内容过大"):
            client.current("上海")


if __name__ == "__main__":
    unittest.main()
