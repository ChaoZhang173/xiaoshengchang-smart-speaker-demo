import json
import os
import http.client
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["SMART_SPEAKER_MODE"] = "mock"
_TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["SMART_SPEAKER_DATA_DIR"] = _TEST_DATA_DIR.name

import app  # noqa: E402


class RecordingOpenAIClient(app.OpenAIHTTPClient):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []

    def _post(self, path, body, content_type, expect_json, **options):
        self.calls.append(
            {
                "path": path,
                "body": body,
                "content_type": content_type,
                "expect_json": expect_json,
                "options": options,
            }
        )
        if path == "/audio/transcriptions":
            return {"text": "测试转录"}
        if path == "/responses":
            return {
                "id": "resp_test",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "测试回答"}],
                    }
                ],
            }
        if path == "/audio/speech":
            return b"fake-mp3"
        raise AssertionError("unexpected path: " + path)


class RecordingSiliconFlowClient(app.SiliconFlowHTTPClient):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []
        self.synthesize_called = False

    def _post(self, path, body, content_type, expect_json, **options):
        self.calls.append(
            {
                "path": path,
                "body": body,
                "content_type": content_type,
                "expect_json": expect_json,
                "options": options,
            }
        )
        if path == "/audio/transcriptions":
            return {"text": "硅基流动测试转录"}
        if path == "/chat/completions":
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "硅基流动测试回答",
                            "reasoning_content": "不应朗读的思考过程",
                        }
                    }
                ]
            }
        raise AssertionError("unexpected path: " + path)

    def synthesize(self, text):
        self.synthesize_called = True
        raise AssertionError("SiliconFlow 免费模式不应调用云端 TTS")


class FunctionCallingSiliconFlowClient(app.SiliconFlowHTTPClient):
    def __init__(self, config, tool_name="play_radio", arguments=None):
        super().__init__(config)
        self.calls = []
        self.tool_name = tool_name
        self.arguments = arguments or {"station_id": "soma_groove_salad"}

    def _post(self, path, body, content_type, expect_json, **options):
        del content_type, expect_json
        payload = json.loads(body.decode("utf-8"))
        self.calls.append({"path": path, "payload": payload, "options": options})
        if len(self.calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_test_1",
                                    "type": "function",
                                    "function": {
                                        "name": self.tool_name,
                                        "arguments": json.dumps(
                                            self.arguments, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "好的，马上为你播放。"}}
            ]
        }


class ToolsUnsupportedSiliconFlowClient(app.SiliconFlowHTTPClient):
    def __init__(self, config):
        super().__init__(config)
        self.payloads = []

    def _post(self, path, body, content_type, expect_json, **options):
        del path, content_type, expect_json, options
        payload = json.loads(body.decode("utf-8"))
        self.payloads.append(payload)
        if "tools" in payload:
            raise app.UpstreamAPIError(
                "工具暂不受支持。", status=400, stage="chat"
            )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "普通聊天回退回答",
                    }
                }
            ]
        }


class FailingTTSClient:
    def respond(self, text, history):
        return "文字回答仍然可用"

    def synthesize(self, text):
        raise app.UpstreamAPIError("simulated TTS failure")


class RaisingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout):
        del request, timeout
        raise self.error


class FakeSearchClient:
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
                    "title": "联网搜索测试结果",
                    "snippet": "这是作为不可信数据传给模型的网页摘要。",
                    "domain": "example.com",
                }
            ],
        }


class FakeWeatherClient:
    def __init__(self):
        self.cities = []

    def current(self, city):
        self.cities.append(city)
        return {
            "source": "Open-Meteo",
            "location": city,
            "condition": "晴",
            "temperature_c": 25.0,
            "apparent_temperature_c": 26.0,
            "relative_humidity_percent": 60,
            "wind_speed_kmh": 5.0,
        }


class RetryOnceSiliconFlowClient(app.SiliconFlowHTTPClient):
    def __init__(self, config, retryable=True):
        super().__init__(config)
        self.calls = []
        self.retryable = retryable

    def _post(self, path, body, content_type, expect_json, **options):
        del body, content_type, expect_json
        self.calls.append({"path": path, "options": options})
        if len(self.calls) == 1:
            raise app.UpstreamAPIError(
                "simulated transcription timeout",
                status=504,
                stage="transcription",
                retryable=self.retryable,
            )
        return {"text": "重试成功"}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.config = app.AppConfig(provider_id="mock", api_key="")
        self.pipeline = app.VoicePipeline(self.config)
        self.session_id = "testsession123"

    def test_mime_normalization_strips_codec(self):
        self.assertEqual(
            app.normalize_mime("audio/webm;codecs=opus"),
            ("audio/webm", ".webm"),
        )

    def test_session_id_validation(self):
        self.assertEqual(app.validate_session_id(self.session_id), self.session_id)
        with self.assertRaises(app.ClientInputError):
            app.validate_session_id("../bad")

    def test_extract_response_text(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "你好"},
                        {"type": "output_text", "text": "，世界"},
                    ],
                }
            ]
        }
        self.assertEqual(app.extract_response_text(payload), "你好，世界")

    def test_mock_text_turn_never_returns_cloud_audio(self):
        result = self.pipeline.process_text("你好", self.session_id)
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(result["provider_id"], "mock")
        self.assertIsNone(result["audio_base64"])
        self.assertTrue(result["use_browser_tts"])

    def test_mock_local_tool_runs_through_policy_and_execution_ledger(self):
        result = self.pipeline.process_text("现在几点？", self.session_id)

        self.assertEqual(result["tools_used"], ["get_local_time:local_demo"])
        record = self.pipeline.execution_ledger.recent(1)[0]
        self.assertEqual(record.operation, "get_local_time")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.metadata["policy_decision"], "allow")

    def test_mock_audio_turn_marks_transcript_as_placeholder(self):
        result = self.pipeline.process_audio(
            b"not-real-audio-but-nonempty",
            "audio/webm;codecs=opus",
            self.session_id,
            2300,
        )
        self.assertTrue(result["transcript_is_placeholder"])
        self.assertIn("2.3", result["transcript"])

    def test_session_history_is_local_and_bounded(self):
        store = app.SessionStore(max_sessions=2, max_turns=2)
        store.add_turn("session001", "u1", "a1")
        store.add_turn("session001", "u2", "a2")
        store.add_turn("session001", "u3", "a3")
        history = store.get_history("session001")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "u2")
        store.clear("session001")
        self.assertEqual(store.get_history("session001"), [])

    def test_responses_payload_uses_local_history_and_store_false(self):
        client = RecordingOpenAIClient(
            app.AppConfig(
                provider_id="openai",
                api_key="test-key",
                api_base="https://api.openai.com/v1",
                transcribe_model="gpt-transcribe",
                text_model="gpt-5.4-mini",
                tts_model="gpt-4o-mini-tts",
                voice="marin",
            )
        )
        reply = client.respond(
            "第二问",
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )
        self.assertEqual(reply, "测试回答")
        call = client.calls[-1]
        payload = json.loads(call["body"].decode("utf-8"))
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][-1]["content"], "第二问")
        self.assertNotIn("previous_response_id", payload)

    def test_transcription_multipart_and_tts_payload(self):
        client = RecordingOpenAIClient(
            app.AppConfig(
                provider_id="openai",
                api_key="test-key",
                api_base="https://api.openai.com/v1",
                transcribe_model="gpt-transcribe",
                text_model="gpt-5.4-mini",
                tts_model="gpt-4o-mini-tts",
                voice="marin",
            )
        )
        transcript = client.transcribe(b"audio-bytes", "audio/webm", ".webm")
        self.assertEqual(transcript, "测试转录")
        multipart_call = client.calls[-1]
        self.assertIn("multipart/form-data; boundary=", multipart_call["content_type"])
        self.assertIn(b'name="model"', multipart_call["body"])
        self.assertIn(b'filename="recording.webm"', multipart_call["body"])
        self.assertIn(b"audio-bytes", multipart_call["body"])

        speech = client.synthesize("测试回答")
        self.assertEqual(speech, b"fake-mp3")
        speech_payload = json.loads(client.calls[-1]["body"].decode("utf-8"))
        self.assertEqual(speech_payload["model"], "gpt-4o-mini-tts")
        self.assertEqual(speech_payload["response_format"], "mp3")

    def test_server_rejects_non_loopback_bind(self):
        with self.assertRaises(app.ClientInputError):
            app.build_server(self.config, "0.0.0.0", 0)

    def test_tts_failure_degrades_to_browser_speech(self):
        pipeline = app.VoicePipeline(
            app.AppConfig(
                provider_id="openai",
                api_key="test-key",
                api_base="https://api.openai.com/v1",
                transcribe_model="gpt-transcribe",
                text_model="gpt-5.4-mini",
                tts_model="gpt-4o-mini-tts",
                voice="marin",
            )
        )
        pipeline.client = FailingTTSClient()
        result = pipeline.process_text("测试", self.session_id)
        self.assertEqual(result["reply"], "文字回答仍然可用")
        self.assertIsNone(result["audio_base64"])
        self.assertTrue(result["use_browser_tts"])
        self.assertIn("系统语音", result["tts_warning"])
        self.assertEqual(len(pipeline.sessions.get_history(self.session_id)), 2)

    def test_environment_prefers_siliconflow_and_keeps_fixed_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "SMART_SPEAKER_MODE": "auto",
                "SILICONFLOW_API_KEY": "sf-secret-test",
                "OPENAI_API_KEY": "openai-secret-test",
                "SILICONFLOW_API_BASE": "https://attacker.invalid/v1",
            },
            clear=True,
        ):
            config = app.AppConfig.from_environment()
        self.assertEqual(config.provider_id, "siliconflow")
        self.assertEqual(config.api_base, "https://api.siliconflow.cn/v1")
        self.assertEqual(config.transcribe_model, "TeleAI/TeleSpeechASR")
        self.assertEqual(config.text_model, "Qwen/Qwen3.5-4B")
        self.assertNotIn("sf-secret-test", repr(config))

    def test_auto_mode_does_not_fall_back_to_openai(self):
        with patch.dict(
            os.environ,
            {
                "SMART_SPEAKER_MODE": "auto",
                "OPENAI_API_KEY": "openai-secret-test",
            },
            clear=True,
        ):
            config = app.AppConfig.from_environment()
        self.assertEqual(config.provider_id, "mock")
        self.assertEqual(config.api_key, "")

    def test_explicit_provider_requires_its_key(self):
        with patch.dict(
            os.environ,
            {"SMART_SPEAKER_MODE": "siliconflow"},
            clear=True,
        ):
            with self.assertRaises(app.ClientInputError):
                app.AppConfig.from_environment()

    def test_force_mock_overrides_all_credentials(self):
        with patch.dict(
            os.environ,
            {
                "SMART_SPEAKER_MODE": "siliconflow",
                "SILICONFLOW_API_KEY": "sf-secret-test",
            },
            clear=True,
        ):
            config = app.AppConfig.from_environment(force_mock=True)
        self.assertEqual(config.provider_id, "mock")
        self.assertEqual(config.api_key, "")

    def test_invalid_model_name_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "SMART_SPEAKER_MODE": "siliconflow",
                "SILICONFLOW_API_KEY": "test-key",
                "SILICONFLOW_TRANSCRIBE_MODEL": "good\r\nX-Evil: yes",
            },
            clear=True,
        ):
            with self.assertRaises(app.ClientInputError):
                app.AppConfig.from_environment()

    def test_siliconflow_chat_completions_payload_and_response(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            transcribe_model="TeleAI/TeleSpeechASR",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = RecordingSiliconFlowClient(config)
        history = []
        for index in range(1, 7):
            history.extend(
                [
                    {"role": "user", "content": "问题%d" % index},
                    {"role": "assistant", "content": "回答%d" % index},
                ]
            )
        reply = client.respond("现在的问题", history)
        self.assertEqual(reply, "硅基流动测试回答")
        call = client.calls[-1]
        self.assertEqual(call["path"], "/chat/completions")
        payload = json.loads(call["body"].decode("utf-8"))
        self.assertLessEqual(len(payload["messages"]), 10)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["content"], "问题5")
        self.assertEqual(payload["messages"][-1]["content"], "现在的问题")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["enable_thinking"])
        self.assertNotIn("store", payload)
        self.assertNotIn("instructions", payload)
        self.assertNotIn("input", payload)

    def test_siliconflow_function_call_is_allowlisted_and_bounded(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = FunctionCallingSiliconFlowClient(config)
        router = app.ToolRouter()
        turn = client.respond_with_tools(
            "播放音乐",
            [
                {"role": "user", "content": "上一问"},
                {"role": "assistant", "content": "上一答"},
            ],
            router,
        )
        self.assertEqual(turn.text, "好的，马上为你播放。")
        self.assertEqual(
            turn.client_actions,
            [{"type": "music.play", "station_id": "soma_groove_salad"}],
        )
        self.assertEqual(turn.tools_used, ["play_radio"])
        self.assertEqual(len(client.calls), 2)
        first_payload = client.calls[0]["payload"]
        second_payload = client.calls[1]["payload"]
        self.assertIn("tools", first_payload)
        self.assertEqual(first_payload["tool_choice"], "auto")
        self.assertNotIn("tools", second_payload)
        self.assertLessEqual(len(second_payload["messages"]), 10)
        self.assertEqual(
            [item["role"] for item in second_payload["messages"][-2:]],
            ["assistant", "tool"],
        )
        self.assertNotIn(
            "ice5.somafm.com", json.dumps(second_payload, ensure_ascii=False)
        )

    def test_siliconflow_web_search_function_call_uses_local_fixed_client(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        search = FakeSearchClient()
        router = app.ToolRouter(search_client=search)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="web_search",
            arguments={"query": "PRIVATE_MEMORY_SHOULD_NOT_LEAK"},
        )
        turn = client.respond_with_tools(
            "联网搜索 OpenAI 最新消息", [], router
        )
        self.assertEqual(search.queries, ["OpenAI 最新消息"])
        self.assertEqual(turn.tools_used, ["web_search"])
        self.assertEqual(len(client.calls), 2)
        tool_message = client.calls[1]["payload"]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("example.com", tool_message["content"])
        self.assertNotIn("https://", tool_message["content"])
        self.assertNotIn("PRIVATE_MEMORY_SHOULD_NOT_LEAK", tool_message["content"])

    def test_model_cannot_force_search_without_local_search_intent(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        search = FakeSearchClient()
        router = app.ToolRouter(search_client=search)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="web_search",
            arguments={"query": "模型擅自添加的查询"},
        )
        turn = client.respond_with_tools("这是我的私人备忘，不要对外发送", [], router)
        self.assertIn("没有发送", turn.text)
        self.assertEqual(turn.tools_used, ["web_search:guard"])
        self.assertEqual(search.queries, [])
        self.assertEqual(len(client.calls), 1)

    def test_model_cannot_force_weather_without_local_weather_intent(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        weather = FakeWeatherClient()
        router = app.ToolRouter(weather_client=weather)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="get_weather",
            arguments={"city": "PRIVATE_MEMORY_CITY"},
        )

        turn = client.respond_with_tools(
            "这是我的私人备忘，不要对外发送",
            [],
            router,
        )

        self.assertIn("没有发送", turn.text)
        self.assertEqual(turn.tools_used, ["get_weather:guard"])
        self.assertEqual(weather.cities, [])
        self.assertEqual(len(client.calls), 1)

    def test_model_tool_calls_cannot_execute_quoted_or_meta_language(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        cases = (
            (
                "play_radio",
                {"station_id": "soma_groove_salad"},
                "请解释“播放音乐”这个命令",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "我的住址是北京朝阳区建国路88号请解释天气这个词",
            ),
            (
                "get_weather",
                {"city": "模型猜测的城市"},
                "联网搜索 北京天气",
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
                "web_search",
                {"query": "模型拼接的私密内容"},
                "这不是最新消息，我的邮箱是alice@example.com",
            ),
            (
                "web_search",
                {"query": "alice@example.com 是什么意思"},
                "请问联网搜索 alice@example.com 是什么意思",
            ),
            (
                "web_search",
                {"query": "alice@example.com"},
                "联网搜索 alice@example.com，我只是在转述",
            ),
            (
                "web_search",
                {"query": "模型拼接的私密内容"},
                "联网搜索 OpenAI 最新消息，然后记住我的密码是 hunter2",
            ),
        )
        for tool_name, arguments, text in cases:
            with self.subTest(tool_name=tool_name):
                weather = FakeWeatherClient()
                search = FakeSearchClient()
                router = app.ToolRouter(
                    weather_client=weather,
                    search_client=search,
                )
                client = FunctionCallingSiliconFlowClient(
                    config,
                    tool_name=tool_name,
                    arguments=arguments,
                )

                turn = client.respond_with_tools(text, [], router)

                self.assertEqual(turn.client_actions, [])
                self.assertEqual(turn.tools_used, [tool_name + ":guard"])
                self.assertEqual(weather.cities, [])
                self.assertEqual(search.queries, [])
                self.assertEqual(len(client.calls), 1)

    def test_model_search_exports_only_the_standalone_request_clause(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        search = FakeSearchClient()
        router = app.ToolRouter(search_client=search)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="web_search",
            arguments={"query": "模型拼接的整段私密上下文"},
        )

        turn = client.respond_with_tools(
            "我工资三万元，今天有哪些最新财经消息？",
            [],
            router,
        )

        self.assertEqual(search.queries, ["今天有哪些最新财经消息"])
        self.assertEqual(turn.tools_used, ["web_search"])
        self.assertEqual(len(client.calls), 2)

    def test_gateway_requires_positive_music_intent_from_current_user(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = FunctionCallingSiliconFlowClient(config)
        pipeline = app.VoicePipeline(config)
        pipeline.client = client

        result = pipeline.process_text(
            "给我介绍一下爵士乐的历史",
            self.session_id,
        )

        self.assertIn("没有明确授权", result["reply"])
        self.assertEqual(result["client_actions"], [])
        self.assertEqual(result["tools_used"], ["play_radio:guard"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(pipeline.execution_ledger.recent(), [])

    def test_gateway_issues_only_router_authorized_music_action(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = FunctionCallingSiliconFlowClient(
            config,
            arguments={"station_id": "model_attempted_other_station"},
        )
        pipeline = app.VoicePipeline(config)
        pipeline.client = client

        result = pipeline.process_text("播放音乐", self.session_id)

        self.assertEqual(
            result["client_actions"],
            [{"type": "music.play", "station_id": "soma_groove_salad"}],
        )
        self.assertEqual(
            pipeline.execution_ledger.recent(1)[0].status,
            "dispatched",
        )

    def test_siliconflow_weather_call_is_bound_to_the_users_city(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        weather = FakeWeatherClient()
        router = app.ToolRouter(weather_client=weather)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="get_weather",
            arguments={"city": "上海"},
        )
        turn = client.respond_with_tools("北京现在天气", [], router)
        self.assertEqual(weather.cities, ["北京"])
        self.assertEqual(turn.tools_used, ["get_weather"])
        self.assertEqual(len(client.calls), 2)

    def test_siliconflow_weather_call_cannot_bypass_local_boundaries(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        for text, expected in (
            ("上海明天天气", "未来或历史"),
            ("上海和北京天气", "一次只查询一个城市"),
            ("今天天气怎么样", "哪个城市"),
        ):
            with self.subTest(text=text):
                weather = FakeWeatherClient()
                router = app.ToolRouter(weather_client=weather)
                client = FunctionCallingSiliconFlowClient(
                    config,
                    tool_name="get_weather",
                    arguments={"city": "上海"},
                )
                turn = client.respond_with_tools(text, [], router)
                self.assertIn(expected, turn.text)
                self.assertEqual(turn.tools_used, ["get_weather:guard"])
                self.assertEqual(weather.cities, [])
                self.assertEqual(len(client.calls), 1)

    def test_failed_realtime_tool_is_not_rewritten_by_the_model(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        search = FakeSearchClient(app.ToolNetworkError("搜索服务暂时不可用。"))
        router = app.ToolRouter(search_client=search)
        client = FunctionCallingSiliconFlowClient(
            config,
            tool_name="web_search",
            arguments={"query": "测试消息"},
        )
        turn = client.respond_with_tools("联网搜索测试消息", [], router)
        self.assertEqual(turn.text, "搜索服务暂时不可用。")
        self.assertEqual(turn.tools_used, ["web_search:error"])
        self.assertEqual(len(client.calls), 1)

    def test_tools_unsupported_fallback_keeps_selected_memory_context(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = ToolsUnsupportedSiliconFlowClient(config)
        turn = client.respond_with_tools(
            "我喜欢什么音乐？",
            [],
            app.ToolRouter(),
            "以下 JSON 仅包含用户明确保存的事实数据：爵士乐",
        )
        self.assertEqual(turn.text, "普通聊天回退回答")
        self.assertEqual(len(client.payloads), 2)
        self.assertIn(
            "爵士乐", client.payloads[1]["messages"][0]["content"]
        )

    def test_model_cannot_reverse_a_negated_music_command(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = FunctionCallingSiliconFlowClient(config)
        turn = client.respond_with_tools(
            "不要播放音乐", [], app.ToolRouter()
        )
        self.assertEqual(turn.client_actions, [])
        self.assertEqual(turn.tools_used, ["negated_tool_refused"])
        self.assertEqual(len(client.calls), 1)

    def test_explicit_memory_persists_and_does_not_call_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            pipeline = app.VoicePipeline(
                app.AppConfig(provider_id="mock", api_key=""),
                memory_store=store,
            )
            saved = pipeline.process_text("记住我喜欢爵士乐", self.session_id)
            self.assertTrue(saved["memory_changed"])
            self.assertIn("爵士乐", saved["reply"])
            reopened = app.MemoryStore(Path(directory) / "memory.sqlite3")
            self.assertEqual(reopened.list_memories()[0].summary, "我喜欢爵士乐")
            listed = pipeline.process_text("你还记得我什么吗？", self.session_id)
            self.assertIn("爵士乐", listed["reply"])
            forgotten = pipeline.process_text("忘记爵士乐", self.session_id)
            self.assertTrue(forgotten["memory_changed"])
            self.assertEqual(reopened.list_memories(), [])

    def test_fuzzy_or_contradictory_forget_requires_ui_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            pipeline = app.VoicePipeline(
                app.AppConfig(provider_id="mock", api_key=""),
                memory_store=store,
            )
            pipeline.process_text("记住我喜欢香菜", self.session_id)
            result = pipeline.process_text("忘记我不喜欢香菜", self.session_id)
            self.assertFalse(result["memory_changed"])
            self.assertIn("页面", result["reply"])
            self.assertEqual(len(store.list_memories()), 1)

    def test_voice_forget_never_echoes_or_deletes_private_matches(self):
        for query in ("忘记我", "忘记我的", "忘记手机号"):
            with self.subTest(query=query), tempfile.TemporaryDirectory() as directory:
                store = app.MemoryStore(Path(directory) / "memory.sqlite3")
                pipeline = app.VoicePipeline(
                    app.AppConfig(provider_id="mock", api_key=""),
                    memory_store=store,
                )
                pipeline.process_text(
                    "记住我的手机号是13800138000",
                    self.session_id,
                )
                pipeline.process_text("记住我有高血压", self.session_id)

                result = pipeline.process_text(query, self.session_id)

                self.assertIn("页面", result["reply"])
                self.assertNotIn("13800138000", result["reply"])
                self.assertNotIn("高血压", result["reply"])
                self.assertFalse(result["memory_changed"])
                self.assertEqual(len(store.list_memories()), 2)

    def test_place_memories_for_other_people_do_not_overwrite_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            pipeline = app.VoicePipeline(
                app.AppConfig(provider_id="mock", api_key=""),
                memory_store=store,
            )
            pipeline.process_text("记住我住在北京", self.session_id)
            pipeline.process_text("记住我朋友住在上海", self.session_id)
            summaries = {item.summary for item in store.list_memories()}
            self.assertEqual(summaries, {"我住在北京", "我朋友住在上海"})

    def test_sensitive_memory_stays_local_and_is_not_put_in_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            pipeline = app.VoicePipeline(
                app.AppConfig(provider_id="mock", api_key=""),
                memory_store=store,
            )
            saved = pipeline.process_text(
                "记住我的手机号是13800138000", self.session_id
            )
            self.assertIn("仅本机", saved["reply"])
            record = store.list_memories()[0]
            self.assertTrue(record.sensitive)
            self.assertEqual(
                store.select_context("我的手机号是什么", include_sensitive=False),
                [],
            )

    def test_contact_and_address_memories_are_sensitive(self):
        for summary in (
            "我的邮箱是alice@example.com",
            "我住在北京市朝阳区建国路88号",
        ):
            with self.subTest(summary=summary):
                self.assertTrue(app.VoicePipeline._memory_is_sensitive(summary))

    def test_health_memories_stay_local_and_out_of_cloud_context(self):
        for summary in (
            "我患有抑郁症",
            "我得了癌症",
            "我怀孕了",
            "I have cancer",
            "I am pregnant",
            "I was diagnosed with depression",
            "我的秘密是蓝鲸",
            "这是我的隐私信息",
            "我的性取向是同性恋",
            "My religion is Buddhist",
            "我住上海",
            "我的住址是北京",
            "我的地址是上海",
            "我家位于深圳",
            "我年薪三十万",
            "我有存款一百万",
            "我得高血压",
            "我是艾滋病患者",
            "我有糖尿病",
        ):
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as directory:
                store = app.MemoryStore(Path(directory) / "memory.sqlite3")
                pipeline = app.VoicePipeline(
                    app.AppConfig(provider_id="mock", api_key=""),
                    memory_store=store,
                )

                saved = pipeline.process_text("记住" + summary, self.session_id)

                self.assertIn("仅本机", saved["reply"])
                record = store.list_memories()[0]
                self.assertTrue(record.sensitive)
                self.assertEqual(
                    store.select_context(summary, include_sensitive=False),
                    [],
                )

    def test_legacy_sensitive_memory_is_reclassified_before_cloud_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            legacy = store.remember(
                "我患有抑郁症",
                kind="preference",
                sensitive=False,
            )
            pipeline = app.VoicePipeline(
                app.AppConfig(provider_id="mock", api_key=""),
                memory_store=store,
            )

            self.assertFalse(legacy.sensitive)
            self.assertEqual(
                store.select_context("抑郁症", include_sensitive=False),
                [],
            )
            self.assertTrue(pipeline.list_memories()[0]["sensitive"])

    def test_openai_mode_uses_local_router_for_time_without_cloud_call(self):
        config = app.AppConfig(
            provider_id="openai",
            api_key="test-key",
            api_base="https://api.openai.com/v1",
            text_model="gpt-test",
        )
        client = RecordingOpenAIClient(config)
        pipeline = app.VoicePipeline(config)
        pipeline.client = client
        result = pipeline.process_text("现在几点？", self.session_id)
        self.assertIn("get_local_time", result["tools_used"][0])
        self.assertEqual(client.calls, [])

    def test_openai_mode_uses_local_router_for_explicit_search(self):
        config = app.AppConfig(
            provider_id="openai",
            api_key="test-key",
            api_base="https://api.openai.com/v1",
            text_model="gpt-test",
        )
        search = FakeSearchClient()
        router = app.ToolRouter(search_client=search)
        client = RecordingOpenAIClient(config)
        pipeline = app.VoicePipeline(config, tool_router=router)
        pipeline.client = client
        result = pipeline.process_text(
            "联网搜索 OpenAI 最新消息", self.session_id
        )
        self.assertEqual(search.queries, ["OpenAI 最新消息"])
        self.assertIn("web_search", result["tools_used"][0])
        self.assertIn("example.com", result["reply"])
        self.assertEqual(client.calls, [])

        implicit = pipeline.process_text(
            "今天有什么最新科技新闻", self.session_id
        )
        self.assertEqual(search.queries[-1], "今天有什么最新科技新闻")
        self.assertIn("web_search", implicit["tools_used"][0])
        self.assertIn("最新、实时或近期新闻", pipeline.health()["privacy_notice"])

    def test_selected_memory_is_bounded_inside_system_message(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.MemoryStore(Path(directory) / "memory.sqlite3")
            store.remember("我喜欢爵士乐", kind="preference")
            config = app.AppConfig(
                provider_id="siliconflow",
                api_key="test-key",
                api_base="https://api.siliconflow.cn/v1",
                transcribe_model="TeleAI/TeleSpeechASR",
                text_model="Qwen/Qwen3.5-4B",
            )
            client = RecordingSiliconFlowClient(config)
            pipeline = app.VoicePipeline(config, memory_store=store)
            pipeline.client = client
            result = pipeline.process_text("我喜欢什么音乐？", self.session_id)
            self.assertEqual(result["reply"], "硅基流动测试回答")
            chat_payload = json.loads(client.calls[-1]["body"].decode("utf-8"))
            system_content = chat_payload["messages"][0]["content"]
            self.assertIn("用户明确保存的事实数据", system_content)
            self.assertIn("爵士乐", system_content)

    def test_siliconflow_transcription_and_local_speech_pipeline(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            transcribe_model="TeleAI/TeleSpeechASR",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = RecordingSiliconFlowClient(config)
        pipeline = app.VoicePipeline(config)
        pipeline.client = client
        result = pipeline.process_audio(
            b"audio-bytes",
            "audio/webm;codecs=opus",
            self.session_id,
            1500,
        )
        self.assertEqual(client.calls[0]["path"], "/audio/transcriptions")
        self.assertIn(b"TeleAI/TeleSpeechASR", client.calls[0]["body"])
        self.assertEqual(client.calls[1]["path"], "/chat/completions")
        self.assertFalse(client.synthesize_called)
        self.assertEqual(result["provider_id"], "siliconflow")
        self.assertEqual(result["reply"], "硅基流动测试回答")
        self.assertIsNone(result["audio_base64"])
        self.assertTrue(result["use_browser_tts"])
        self.assertIsNone(result["tts_warning"])
        self.assertEqual(len(pipeline.sessions.get_history(self.session_id)), 2)

    def test_socket_timeout_is_sanitized_as_retryable_504(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
        )
        for error in (socket.timeout(), URLError(socket.timeout())):
            with self.subTest(error_type=type(error).__name__):
                client = app.SiliconFlowHTTPClient(config)
                client.opener = RaisingOpener(error)
                with self.assertRaises(app.UpstreamAPIError) as raised:
                    client._post(
                        "/audio/transcriptions",
                        b"test-body",
                        "application/octet-stream",
                        expect_json=True,
                        stage="transcription",
                    )
                self.assertEqual(raised.exception.status, 504)
                self.assertEqual(raised.exception.stage, "transcription")
                self.assertTrue(raised.exception.retryable)
                self.assertIn("语音识别", str(raised.exception))

    def test_transport_error_is_sanitized_as_retryable_503(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
        )
        client = app.SiliconFlowHTTPClient(config)
        client.opener = RaisingOpener(URLError(OSError("private detail")))
        with self.assertRaises(app.UpstreamAPIError) as raised:
            client._post(
                "/audio/transcriptions",
                b"test-body",
                "application/octet-stream",
                expect_json=True,
                stage="transcription",
            )
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.stage, "transcription")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private detail", str(raised.exception))

    def test_direct_http_transport_errors_are_sanitized_as_503(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
        )
        errors = (
            http.client.RemoteDisconnected("private detail"),
            http.client.IncompleteRead(b"private partial body", 100),
            ConnectionResetError("private detail"),
        )
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                client = app.SiliconFlowHTTPClient(config)
                client.opener = RaisingOpener(error)
                with self.assertRaises(app.UpstreamAPIError) as raised:
                    client._post(
                        "/audio/transcriptions",
                        b"test-body",
                        "application/octet-stream",
                        expect_json=True,
                        stage="transcription",
                    )
                self.assertEqual(raised.exception.status, 503)
                self.assertEqual(raised.exception.stage, "transcription")
                self.assertTrue(raised.exception.retryable)
                self.assertNotIn("private", str(raised.exception))

    def test_siliconflow_transcription_retries_one_timeout(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            request_timeout=60,
        )
        client = RetryOnceSiliconFlowClient(config)
        with patch("app.time.sleep") as sleep:
            transcript = client.transcribe(b"audio", "audio/wav", ".wav")
        self.assertEqual(transcript, "重试成功")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            [call["options"]["attempt"] for call in client.calls], [1, 2]
        )
        self.assertEqual(
            [call["options"]["timeout"] for call in client.calls], [35, 35]
        )
        sleep.assert_called_once_with(0.6)

    def test_siliconflow_transcription_does_not_retry_permanent_error(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
        )
        client = RetryOnceSiliconFlowClient(config, retryable=False)
        with patch("app.time.sleep") as sleep:
            with self.assertRaises(app.UpstreamAPIError):
                client.transcribe(b"audio", "audio/wav", ".wav")
        self.assertEqual(len(client.calls), 1)
        sleep.assert_not_called()

    def test_two_audio_turns_send_exactly_one_previous_turn(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            transcribe_model="TeleAI/TeleSpeechASR",
            text_model="Qwen/Qwen3.5-4B",
        )
        client = RecordingSiliconFlowClient(config)
        pipeline = app.VoicePipeline(config)
        pipeline.client = client
        pipeline.process_audio(
            b"first-audio", "audio/webm;codecs=opus", self.session_id, 1000
        )
        pipeline.process_audio(
            b"second-audio", "audio/webm;codecs=opus", self.session_id, 1000
        )
        chat_calls = [
            call for call in client.calls if call["path"] == "/chat/completions"
        ]
        self.assertEqual(len(chat_calls), 2)
        second_payload = json.loads(chat_calls[1]["body"].decode("utf-8"))
        self.assertEqual(
            [message["role"] for message in second_payload["messages"]],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(
            second_payload["messages"][1]["content"], "硅基流动测试转录"
        )
        self.assertEqual(
            second_payload["messages"][2]["content"], "硅基流动测试回答"
        )
        self.assertEqual(len(pipeline.sessions.get_history(self.session_id)), 4)

    def test_siliconflow_chat_timeout_is_capped_below_browser_budget(self):
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key="test-key",
            api_base="https://api.siliconflow.cn/v1",
            request_timeout=300,
        )
        client = RecordingSiliconFlowClient(config)
        client.respond("测试", [])
        self.assertEqual(client.calls[-1]["options"]["timeout"], 60)

    def test_provider_error_does_not_surface_upstream_body_or_key(self):
        secret = "sf-secret-MUST-NOT-LEAK"
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key=secret,
            api_base="https://api.siliconflow.cn/v1",
        )
        client = app.SiliconFlowHTTPClient(config)
        message = client._friendly_http_error(
            400,
            json.dumps({"error": {"message": secret}}).encode("utf-8"),
        )
        self.assertNotIn(secret, message)
        health = app.VoicePipeline(config).health()
        serialized = json.dumps(health, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("api_base", serialized)


class LocalHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = app.AppConfig(provider_id="mock", api_key="")
        cls.memory_directory = tempfile.TemporaryDirectory()
        store = app.MemoryStore(
            Path(cls.memory_directory.name) / "http-test-memory.sqlite3"
        )
        pipeline = app.VoicePipeline(config, memory_store=store)
        cls.server = app.SmartSpeakerServer(("127.0.0.1", 0), pipeline)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base_url = "http://%s:%s" % (host, port)
        with urlopen(cls.base_url + "/api/health", timeout=3) as response:
            cls.request_token = json.loads(response.read().decode("utf-8"))[
                "request_token"
            ]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.memory_directory.cleanup()

    def get_json(self, path, authorized=False):
        headers = {"X-Demo-Token": self.request_token} if authorized else {}
        request = Request(self.base_url + path, headers=headers, method="GET")
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post_json(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Demo-Token": self.request_token,
            },
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_endpoint(self):
        status, payload = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["app"], "smart-speaker-demo")
        self.assertEqual(
            payload["build_id"], "agent-foundation-v1"
        )
        self.assertEqual(payload["mode"], "mock")
        self.assertEqual(payload["provider_id"], "mock")
        self.assertFalse(payload["real_api_enabled"])
        self.assertTrue(payload["capabilities"]["web_search"])
        self.assertTrue(payload["capabilities"]["agent_gateway"])
        self.assertTrue(payload["capabilities"]["brain_adapter"])
        self.assertTrue(payload["capabilities"]["execution_ledger"])
        self.assertTrue(payload["capabilities"]["policy_engine"])
        self.assertFalse(payload["capabilities"]["network_tools_enabled"])
        self.assertTrue(payload["request_token"])

    def test_home_page(self):
        with urlopen(self.base_url + "/", timeout=3) as response:
            body = response.read().decode("utf-8")
        self.assertIn("小声场", body)
        self.assertIn("/app.js", body)

    def test_mock_text_endpoint(self):
        status, payload = self.post_json(
            "/api/turn/text",
            {"session_id": "httpsession123", "text": "你好"},
        )
        self.assertEqual(status, 200)
        self.assertIn("你好", payload["reply"])

    def test_reset_endpoint(self):
        status, payload = self.post_json(
            "/api/session/reset",
            {"session_id": "httpsession123"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_station_endpoint_only_returns_whitelisted_station(self):
        status, payload = self.get_json("/api/music/stations")
        self.assertEqual(status, 200)
        self.assertEqual(payload["stations"][0]["id"], "soma_groove_salad")
        self.assertTrue(payload["stations"][0]["stream_url"].startswith("https://"))

    def test_memory_endpoints_require_token_and_clear_confirmation(self):
        with self.assertRaises(HTTPError) as raised:
            self.get_json("/api/memories")
        self.assertEqual(raised.exception.code, 403)

        self.post_json(
            "/api/turn/text",
            {"session_id": "memoryhttp123", "text": "记住我喜欢爵士乐"},
        )
        status, payload = self.get_json("/api/memories", authorized=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["memories"][0]["summary"], "我喜欢爵士乐")

        request = Request(
            self.base_url + "/api/memories/clear",
            data=json.dumps({"confirm": False}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Demo-Token": self.request_token,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)

        status, payload = self.post_json(
            "/api/memories/clear", {"confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["deleted"], 1)

    def test_post_rejects_missing_startup_token(self):
        request = Request(
            self.base_url + "/api/turn/text",
            data=json.dumps(
                {"session_id": "httpsession123", "text": "不应执行"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 403)

    def test_json_endpoint_rejects_text_plain(self):
        request = Request(
            self.base_url + "/api/turn/text",
            data=b"{}",
            headers={
                "Content-Type": "text/plain",
                "X-Demo-Token": self.request_token,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
