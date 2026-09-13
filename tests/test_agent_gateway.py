import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_gateway import (  # noqa: E402
    AgentGateway,
    GovernedToolRouter,
    ToolConfirmationRequired,
    ToolInvocationContext,
    ToolPolicyError,
)
from brain_adapter import AssistantTurn, CallbackBrainAdapter  # noqa: E402
from execution_ledger import (  # noqa: E402
    AWAITING_CONFIRMATION,
    BLOCKED,
    DISPATCHED,
    FAILED,
    SUCCEEDED,
    ExecutionLedger,
)
from policy_engine import (  # noqa: E402
    ConfirmationRequirement,
    PolicyEngine,
    RiskLevel,
    ToolPolicy,
)
from tool_router import ToolExecution, ToolInputError, ToolRouter  # noqa: E402


class FakeSessions:
    def __init__(self):
        self.history = [
            {"role": "user", "content": "上一问"},
            {"role": "assistant", "content": "上一答"},
        ]
        self.added = []

    def get_history(self, session_id):
        self.requested_session = session_id
        return list(self.history)

    def add_turn(self, session_id, text, reply):
        self.added.append((session_id, text, reply))


class FakeMemories:
    def __init__(self):
        self.calls = []

    def select_context(self, text, *, limit, max_chars):
        self.calls.append((text, limit, max_chars))
        return ["selected"]

    def context_for_prompt(self, selected):
        self.selected = selected
        return "安全的本地记忆上下文"


class ExplodingSearch:
    def search(self, query):
        raise AssertionError("network client must not be reached: " + str(query))


class RecordingSearch:
    def __init__(self):
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return {"source": "test", "query": query, "results": []}


class RecordingWeather:
    def __init__(self):
        self.cities = []

    def current(self, city):
        self.cities.append(city)
        return {
            "source": "test",
            "location": city,
            "condition": "晴",
            "temperature_c": 25.0,
            "apparent_temperature_c": 25.0,
            "relative_humidity_percent": 50,
            "wind_speed_kmh": 3.0,
        }


class InvalidActionRouter(ToolRouter):
    def execute(self, name_value, arguments):
        del arguments
        return ToolExecution(
            str(name_value),
            {"ok": True},
            "不应下发",
            [
                {
                    "type": "music.play",
                    "station_id": "soma_groove_salad",
                    "url": "https://attacker.invalid/stream",
                }
            ],
        )


class SpeechFailure(RuntimeError):
    pass


class AgentGatewayTests(unittest.TestCase):
    def governed(
        self,
        *,
        policy=None,
        allow_network=True,
        user_text="把音乐音量调到 35",
        base_router=None,
    ):
        ledger = ExecutionLedger()
        router = GovernedToolRouter(
            base_router or ToolRouter(search_client=ExplodingSearch()),
            policy if policy is not None else PolicyEngine(),
            ledger,
            ToolInvocationContext(
                source="test-brain",
                user_text=user_text,
                session_fingerprint="session-hash",
                audit_key=b"test-audit-key",
                allow_network=allow_network,
            ),
        )
        return router, ledger

    def test_allowed_tool_is_executed_and_recorded(self):
        router, ledger = self.governed()

        execution = router.execute("set_music_volume", {"volume": 35})

        self.assertEqual(execution.payload, {"ok": True, "volume": 35})
        record = ledger.recent(1)[0]
        self.assertEqual(record.operation, "set_music_volume")
        self.assertEqual(record.status, DISPATCHED)
        self.assertEqual(record.arguments, {"volume": 35})
        self.assertEqual(record.metadata["policy_decision"], "allow")
        self.assertEqual(record.result["client_action_count"], 1)
        self.assertEqual(
            record.result["external_effect_status"],
            "client_ack_unavailable",
        )

    def test_unknown_tool_is_blocked_before_router_execution(self):
        router, ledger = self.governed()

        with self.assertRaises(ToolPolicyError) as raised:
            router.execute("shell", {"command": "whoami"})

        self.assertIn("允许列表", str(raised.exception))
        record = ledger.recent(1)[0]
        self.assertEqual(record.operation, "shell")
        self.assertEqual(record.status, BLOCKED)
        self.assertEqual(record.reason, "unknown_tool")

    def test_network_policy_filters_and_blocks_without_network_call(self):
        router, ledger = self.governed(
            allow_network=False,
            user_text="联网搜索 不会发出的搜索",
        )
        names = {
            item["function"]["name"]
            for item in router.definitions()
        }
        self.assertNotIn("web_search", names)
        self.assertIn("get_local_time", names)

        with self.assertRaises(ToolPolicyError) as raised:
            router.execute("web_search", {"query": "不会发出的搜索"})

        self.assertIn("不允许", str(raised.exception))
        self.assertEqual(ledger.recent(1)[0].status, BLOCKED)
        self.assertEqual(ledger.recent(1)[0].reason, "network_disabled")

    def test_confirmation_policy_waits_until_host_marks_tool_confirmed(self):
        policy = PolicyEngine(
            [
                ToolPolicy(
                    "set_music_volume",
                    risk_level=RiskLevel.HIGH,
                    confirmation=ConfirmationRequirement.USER,
                )
            ]
        )
        pending_router, pending_ledger = self.governed(
            policy=policy,
            user_text="把音乐音量调到 20",
        )

        with self.assertRaises(ToolConfirmationRequired):
            pending_router.execute("set_music_volume", {"volume": 20})
        self.assertEqual(
            pending_ledger.recent(1)[0].status,
            AWAITING_CONFIRMATION,
        )

    def test_router_validation_failure_is_recorded_and_preserved(self):
        router, ledger = self.governed(user_text="把音乐音量调到 101")

        with self.assertRaises(ToolInputError):
            router.execute("set_music_volume", {"volume": 101})

        record = ledger.recent(1)[0]
        self.assertEqual(record.status, FAILED)
        self.assertEqual(record.error["type"], "ToolInputError")

    def test_model_music_action_requires_matching_positive_user_intent(self):
        router, ledger = self.governed(user_text="介绍一下爵士乐的历史")

        with self.assertRaises(ToolPolicyError) as raised:
            router.execute(
                "play_radio",
                {"station_id": "soma_groove_salad"},
            )

        self.assertIn("没有明确授权", str(raised.exception))
        self.assertEqual(router.approved_client_actions, [])
        self.assertEqual(ledger.recent(1)[0].status, BLOCKED)
        self.assertEqual(
            ledger.recent(1)[0].reason,
            "user_intent_not_authorized",
        )

    def test_model_cannot_turn_meta_or_private_text_into_a_tool_call(self):
        cases = (
            (
                "play_radio",
                {"station_id": "soma_groove_salad"},
                "请解释“播放音乐”这个口令是什么意思",
            ),
            (
                "set_music_volume",
                {"volume": 100},
                "如果我说“把音乐音量调到 100”，你会直接执行吗？",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "我的住址是北京朝阳区建国路88号请解释“天气”这个词",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "我患有抑郁症请查询上海天气",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "我怀孕了上海天气怎么样",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "联网搜索 北京天气",
            ),
            (
                "web_search",
                {"query": "模型拼接的私密内容"},
                "这不是最新消息，我的邮箱是alice@example.com",
            ),
            (
                "web_search",
                {"query": "模型拼接的私密内容"},
                "联网搜索 OpenAI 最新消息，然后记住我的密码是 hunter2",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "search ｍｙ HIV status",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "联网搜索 13800138000 撤销",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "联网搜索 我的助记词是 apple book candle door eagle forest grape hotel island jungle king lemon",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "search recovery code is 12345678",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "search OpenAI cancel the request",
            ),
            (
                "web_search",
                {"query": "模型猜测的查询"},
                "联网搜索 OpenAI 别管了",
            ),
        )
        for tool_name, arguments, user_text in cases:
            with self.subTest(tool_name=tool_name, user_text=user_text):
                search = RecordingSearch()
                weather = RecordingWeather()
                router, ledger = self.governed(
                    user_text=user_text,
                    base_router=ToolRouter(
                        weather_client=weather,
                        search_client=search,
                    ),
                )

                with self.assertRaises(ToolPolicyError) as raised:
                    router.execute(tool_name, arguments)

                self.assertEqual(
                    raised.exception.evaluation.reason,
                    "user_intent_not_authorized",
                )
                self.assertEqual(router.approved_client_actions, [])
                self.assertEqual(weather.cities, [])
                self.assertEqual(search.queries, [])
                record = ledger.recent(1)[0]
                self.assertEqual(record.status, BLOCKED)
                self.assertEqual(record.reason, "user_intent_not_authorized")
                self.assertNotIn("alice@example.com", repr(record.to_dict()))

    def test_implicit_search_rebinds_only_the_standalone_request_clause(self):
        search = RecordingSearch()
        router, ledger = self.governed(
            user_text="我被诊断为抑郁症，今天有什么最新新闻？",
            base_router=ToolRouter(search_client=search),
        )

        router.execute("web_search", {"query": "整段私密上下文"})

        self.assertEqual(search.queries, ["今天有什么最新新闻"])
        record = ledger.recent(1)[0]
        self.assertEqual(record.status, SUCCEEDED)
        serialized = repr(record.to_dict())
        self.assertNotIn("抑郁症", serialized)
        self.assertNotIn("整段私密上下文", serialized)

    def test_negative_volume_wording_cannot_dispatch_an_action(self):
        for user_text in (
            "不要把音乐音量调到 100",
            "请不要将音乐音量设置为 100",
            "别把音乐音量设置为 100",
            "不要自动播放音乐",
            "不要现在播放音乐",
            "我不想听你播放音乐",
        ):
            with self.subTest(user_text=user_text):
                router, ledger = self.governed(user_text=user_text)
                with self.assertRaises(ToolPolicyError):
                    router.execute("set_music_volume", {"volume": 100})
                self.assertEqual(router.approved_client_actions, [])
                self.assertEqual(ledger.recent(1)[0].status, BLOCKED)

    def test_user_text_overrides_model_supplied_music_arguments(self):
        router, ledger = self.governed(user_text="把音乐音量调到 20")

        execution = router.execute("set_music_volume", {"volume": 100})

        self.assertEqual(execution.payload["volume"], 20)
        self.assertEqual(ledger.recent(1)[0].arguments, {"volume": 20})

    def test_one_turn_cannot_execute_a_second_tool(self):
        router, ledger = self.governed(user_text="现在几点？")

        router.execute("get_local_time", {})
        with self.assertRaises(ToolPolicyError) as raised:
            router.execute("get_local_time", {})

        self.assertIn("最多执行一个工具", str(raised.exception))
        records = ledger.recent(2)
        self.assertEqual(records[0].status, BLOCKED)
        self.assertEqual(records[0].reason, "tool_call_limit")
        self.assertEqual(records[1].status, SUCCEEDED)

    def test_invalid_router_action_is_rejected_and_never_approved(self):
        router, ledger = self.governed(
            user_text="播放音乐",
            base_router=InvalidActionRouter(),
        )

        with self.assertRaises(ToolInputError):
            router.execute(
                "play_radio",
                {"station_id": "soma_groove_salad"},
            )

        self.assertEqual(router.approved_client_actions, [])
        self.assertEqual(ledger.recent(1)[0].status, FAILED)

    def test_offline_deterministic_result_is_audited_as_blocked(self):
        router, ledger = self.governed(
            allow_network=False,
            user_text="联网搜索 本地隐私测试",
        )

        execution = router.deterministic_match(
            "联网搜索 本地隐私测试",
            allow_network=False,
        )

        self.assertTrue(execution.payload["offline_demo"])
        record = ledger.recent(1)[0]
        self.assertEqual(record.status, BLOCKED)
        self.assertEqual(record.reason, "network_disabled")
        self.assertNotIn("本地隐私测试", repr(record.to_dict()))

    def test_network_arguments_are_rebuilt_and_minimized_in_audit(self):
        search = RecordingSearch()
        router, ledger = self.governed(
            allow_network=True,
            user_text="联网搜索 用户明确的主题",
            base_router=ToolRouter(search_client=search),
        )

        router.execute("web_search", {"query": "模型擅自添加的私密内容"})

        self.assertEqual(search.queries, ["用户明确的主题"])
        record = ledger.recent(1)[0]
        self.assertEqual(record.status, SUCCEEDED)
        serialized = repr(record.to_dict())
        self.assertNotIn("用户明确的主题", serialized)
        self.assertNotIn("模型擅自添加的私密内容", serialized)
        self.assertNotIn("session123", serialized)
        self.assertIn("query_fingerprint", record.arguments)

    def test_gateway_passes_history_and_bounded_memory_then_saves_turn(self):
        sessions = FakeSessions()
        memories = FakeMemories()
        seen = []

        def respond(request, tools):
            seen.append((request, tools))
            return AssistantTurn("新回答", [{"type": "demo"}], ["demo_tool"])

        adapter = CallbackBrainAdapter(
            "test-brain",
            responder=respond,
            synthesizer=lambda text: b"speech:" + text.encode("utf-8"),
        )
        gateway = AgentGateway(
            brain=adapter,
            sessions=sessions,
            memories=memories,
            tools=ToolRouter(),
            uses_cloud_tts=True,
        )

        result = gateway.run("新问题", "session123")

        request, tools = seen[0]
        self.assertIsInstance(tools, GovernedToolRouter)
        self.assertEqual(request.history, sessions.history)
        self.assertEqual(request.memory_context, "安全的本地记忆上下文")
        self.assertEqual(memories.calls, [("新问题", 5, 1_200)])
        self.assertEqual(result.assistant_turn.text, "新回答")
        self.assertEqual(result.assistant_turn.client_actions, [])
        self.assertEqual(result.speech, "speech:新回答".encode("utf-8"))
        self.assertEqual(sessions.added, [("session123", "新问题", "新回答")])

    def test_only_governed_router_can_authorize_client_actions(self):
        sessions = FakeSessions()

        def respond(request, tools):
            tools.execute(
                "play_radio",
                {"station_id": "model_attempted_other_station"},
            )
            return AssistantTurn(
                "开始播放",
                [{"type": "music.stop"}],
                ["play_radio"],
            )

        gateway = AgentGateway(
            brain=CallbackBrainAdapter("test-brain", responder=respond),
            sessions=sessions,
            memories=FakeMemories(),
            tools=ToolRouter(),
        )

        result = gateway.run("播放音乐", "session123")

        self.assertEqual(
            result.assistant_turn.client_actions,
            [{"type": "music.play", "station_id": "soma_groove_salad"}],
        )
        self.assertEqual(gateway.ledger.recent(1)[0].status, DISPATCHED)

    def test_local_response_short_circuits_memory_brain_tts_and_history(self):
        sessions = FakeSessions()
        memories = FakeMemories()
        adapter = CallbackBrainAdapter(
            "test-brain",
            responder=lambda request, tools: self.fail("brain should not run"),
            synthesizer=lambda text: self.fail("tts should not run"),
        )
        gateway = AgentGateway(
            brain=adapter,
            sessions=sessions,
            memories=memories,
            tools=ToolRouter(),
            local_responder=lambda text, tools: AssistantTurn(
                "本地回答", [], ["local"]
            ),
            uses_cloud_tts=True,
        )

        result = gateway.run("本地问题", "session123")

        self.assertEqual(result.assistant_turn.text, "本地回答")
        self.assertIsNone(result.speech)
        self.assertEqual(memories.calls, [])
        self.assertEqual(sessions.added, [])

    def test_configured_speech_failure_degrades_after_brain_response(self):
        sessions = FakeSessions()
        adapter = CallbackBrainAdapter(
            "test-brain",
            responder=lambda request, tools: AssistantTurn("文字仍可用"),
            synthesizer=lambda text: (_ for _ in ()).throw(SpeechFailure("failed")),
        )
        gateway = AgentGateway(
            brain=adapter,
            sessions=sessions,
            memories=FakeMemories(),
            tools=ToolRouter(),
            uses_cloud_tts=True,
            speech_error_types=(SpeechFailure,),
        )

        result = gateway.run("问题", "session123")

        self.assertIsNone(result.speech)
        self.assertIn("系统语音", result.tts_warning)
        self.assertEqual(sessions.added, [("session123", "问题", "文字仍可用")])


if __name__ == "__main__":
    unittest.main()
