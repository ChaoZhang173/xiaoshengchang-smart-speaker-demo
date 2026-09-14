"""Positive acceptance cases for everyday weather and news requests.

These cases exercise the user's words through intent selection, governance,
and the complete voice pipeline. All upstream clients are local fakes; the
final answer is built from the actual tool payload, not a canned success.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402
from agent_gateway import GovernedToolRouter, ToolInvocationContext  # noqa: E402
from execution_ledger import ExecutionLedger, SUCCEEDED  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from tool_router import ToolRouter  # noqa: E402


WEATHER_REQUESTS = (
    ("查询高密现在的天气。", "高密"),
    ("高密今天的天气怎么样？", "高密"),
    ("现在高密天气怎么样？", "高密"),
    ("帮我查一下邹平市天气", "邹平市"),
    ("请问安吉县现在的天气如何？", "安吉县"),
    ("共和县现在天气怎么样？", "共和县"),
    ("What is the weather in Reykjavik now?", "reykjavik"),
    ("Please check Ulaanbaatar weather today.", "ulaanbaatar"),
    ("weather in Paris, France", "paris, france"),
)

NEWS_REQUESTS = (
    ("今天发生了什么新闻？", "今天发生了什么新闻"),
    ("今天有什么新闻？", "今天有什么新闻"),
    ("今天都有什么大事？", "今天都有什么大事"),
    ("给我播报一下今天的新闻", "今天的新闻"),
    ("说说今天的科技新闻", "今天的科技新闻"),
    ("最近有什么值得关注的消息？", "最近有什么值得关注的消息"),
    ("What happened in the news today?", "What happened in the news today"),
    ("Tell me today's headlines.", "today's headlines"),
)

REQUESTS = tuple(
    (text, "get_weather", {"city": city}) for text, city in WEATHER_REQUESTS
) + tuple(
    (text, "web_search", {"query": query}) for text, query in NEWS_REQUESTS
)


class RecordingWeather:
    def __init__(self):
        self.cities = []

    def current(self, city):
        self.cities.append(city)
        return {
            "source": "local weather fixture",
            "location": city,
            "condition": "多云",
            "temperature_c": 23.7,
            "apparent_temperature_c": 24.1,
            "relative_humidity_percent": 57,
            "wind_speed_kmh": 6.4,
        }


class RecordingSearch:
    def __init__(self):
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return {
            "source": "local news fixture",
            "query": query,
            "results": [
                {
                    "title": "地方图书馆新增夜间开放服务",
                    "snippet": "测试报道：图书馆从今日起延长开放时间至晚间九点。",
                    "domain": "news.example.test",
                }
            ],
        }


class ResultReadingSiliconFlowClient(app.SiliconFlowHTTPClient):
    """Fake the model protocol, but require a real tool result before replying."""

    def __init__(self, config, tool_name, transcript):
        super().__init__(config)
        self.tool_name = tool_name
        self.transcript = transcript
        self.payloads = []
        self.transcriptions = 0

    def _post(self, path, body, content_type, expect_json, **options):
        del content_type, expect_json, options
        if path == "/audio/transcriptions":
            self.transcriptions += 1
            return {"text": self.transcript}
        if path != "/chat/completions":
            raise AssertionError("Unexpected upstream path: " + path)
        payload = json.loads(body.decode("utf-8"))
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            # A model's argument is not the source of the user's city/query.
            proposed = (
                {"city": "上海"}
                if self.tool_name == "get_weather"
                else {"query": "unrelated model-generated search"}
            )
            return {
                "choices": [{"message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "natural_request_call",
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps(proposed, ensure_ascii=False),
                        },
                    }],
                }}]
            }
        if len(self.payloads) != 2:
            raise AssertionError("A request exceeded the one-tool answer cycle.")
        tool_messages = [
            item for item in payload["messages"] if item["role"] == "tool"
        ]
        if len(tool_messages) != 1:
            raise AssertionError("The final answer must receive one tool result.")
        result = json.loads(tool_messages[0]["content"])
        if self.tool_name == "get_weather":
            reply = "%s当前%s，%.1f摄氏度。" % (
                result["location"], result["condition"], result["temperature_c"]
            )
        else:
            item = result["results"][0]
            reply = "关于%s：%s 来源：%s。" % (
                result["query"], item["title"], item["domain"]
            )
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


class NaturalRequestAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.weather = RecordingWeather()
        self.search = RecordingSearch()
        self.router = ToolRouter(
            weather_client=self.weather, search_client=self.search
        )

    def test_public_research_topics_are_not_private_memory_statements(self):
        for text in (
            "今天癌症治疗有什么进展",
            "今天高血压研究有什么新闻",
            "今天SpaceX有什么新闻",
            "最新量子计算新闻",
        ):
            with self.subTest(text=text):
                execution = self.router.deterministic_match(text)
                self.assertEqual(execution.name, "web_search")
                self.assertEqual(self.search.queries[-1], text)

    def test_everyday_requests_select_the_expected_tool_and_parameters(self):
        for text, tool_name, arguments in REQUESTS:
            with self.subTest(text=text):
                self.assertEqual(
                    self.router.authorized_intent(text),
                    {"tool_name": tool_name, "arguments": arguments},
                )
        self.assertEqual(self.weather.cities, [])
        self.assertEqual(self.search.queries, [])

    def test_governance_executes_the_user_requested_destination(self):
        for text, tool_name, arguments in REQUESTS:
            with self.subTest(text=text):
                self.weather.cities.clear()
                self.search.queries.clear()
                ledger = ExecutionLedger()
                governed = GovernedToolRouter(
                    self.router,
                    PolicyEngine(),
                    ledger,
                    ToolInvocationContext(
                        source="natural-request-test",
                        user_text=text,
                        allow_network=True,
                        audit_key=b"local-test-audit-key",
                    ),
                )
                proposed = (
                    {"city": "上海"}
                    if tool_name == "get_weather"
                    else {"query": "unrelated model-generated search"}
                )
                execution = governed.execute(tool_name, proposed)
                self.assertEqual(execution.name, tool_name)
                self.assertEqual(ledger.recent(1)[0].status, SUCCEEDED)
                self._assert_one_correct_destination(tool_name, arguments)

    def test_complete_siliconflow_turn_answers_from_actual_tool_payload(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            for index, (text, tool_name, arguments) in enumerate(REQUESTS):
                with self.subTest(text=text):
                    self.weather.cities.clear()
                    self.search.queries.clear()
                    config = app.AppConfig(provider_id="siliconflow", api_key="test-key")
                    pipeline = app.VoicePipeline(
                        config,
                        memory_store=MemoryStore(Path(temporary_dir) / (str(index) + ".sqlite3")),
                        tool_router=self.router,
                    )
                    client = ResultReadingSiliconFlowClient(config, tool_name, text)
                    pipeline.client = client

                    result = pipeline.process_text(text, "naturalrequestsession")

                    self.assertEqual(result["tools_used"], [tool_name])
                    self.assertEqual(len(client.payloads), 2)
                    self.assertEqual(result["transcript"], text)
                    self.assertEqual(result["client_actions"], [])
                    self._assert_one_correct_destination(tool_name, arguments)
                    self._assert_grounded_reply(result["reply"], tool_name, arguments)

    def test_spoken_screenshot_requests_use_the_same_complete_tool_cycle(self):
        spoken_cases = (
            ("查询高密现在的天气。", "get_weather", {"city": "高密"}),
            ("今天发生了什么新闻？", "web_search", {"query": "今天发生了什么新闻"}),
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            for index, (text, tool_name, arguments) in enumerate(spoken_cases):
                with self.subTest(text=text):
                    self.weather.cities.clear()
                    self.search.queries.clear()
                    config = app.AppConfig(provider_id="siliconflow", api_key="test-key")
                    pipeline = app.VoicePipeline(
                        config,
                        memory_store=MemoryStore(Path(temporary_dir) / (str(index) + ".sqlite3")),
                        tool_router=self.router,
                    )
                    client = ResultReadingSiliconFlowClient(config, tool_name, text)
                    pipeline.client = client

                    result = pipeline.process_audio(
                        b"local-test-recording", "audio/webm", "naturalvoicesession", 1500
                    )

                    self.assertEqual(client.transcriptions, 1)
                    self.assertEqual(len(client.payloads), 2)
                    self.assertEqual(result["tools_used"], [tool_name])
                    self.assertEqual(result["transcript"], text)
                    self._assert_one_correct_destination(tool_name, arguments)
                    self._assert_grounded_reply(result["reply"], tool_name, arguments)

    def _assert_one_correct_destination(self, tool_name, arguments):
        if tool_name == "get_weather":
            self.assertEqual(self.weather.cities, [arguments["city"]])
            self.assertEqual(self.search.queries, [])
        else:
            self.assertEqual(self.search.queries, [arguments["query"]])
            self.assertEqual(self.weather.cities, [])

    def _assert_grounded_reply(self, reply, tool_name, arguments):
        if tool_name == "get_weather":
            self.assertIn(arguments["city"], reply)
            self.assertIn("23.7", reply)
            self.assertIn("多云", reply)
        else:
            self.assertIn(arguments["query"], reply)
            self.assertIn("地方图书馆新增夜间开放服务", reply)
            self.assertIn("news.example.test", reply)
        self.assertNotIn("没有明确要求", reply)
        self.assertNotIn("没有明确授权", reply)


if __name__ == "__main__":
    unittest.main()
