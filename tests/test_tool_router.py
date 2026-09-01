import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tool_router import (  # noqa: E402
    MAX_TOOL_RESPONSE_BYTES,
    ToolInputError,
    ToolNetworkError,
    ToolRouter,
    WeatherClient,
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


class ToolRouterTests(unittest.TestCase):
    def setUp(self):
        self.weather = FakeWeather()
        self.router = ToolRouter(weather_client=self.weather)

    def test_definitions_only_expose_registered_functions(self):
        definitions = self.router.definitions()
        names = {item["function"]["name"] for item in definitions}
        self.assertEqual(
            names,
            {
                "get_local_time",
                "get_weather",
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

    def test_unknown_tool_bad_json_and_extra_fields_are_rejected(self):
        with self.assertRaises(ToolInputError):
            self.router.execute("shell", {})
        with self.assertRaises(ToolInputError):
            self.router.execute("get_weather", "not-json")
        with self.assertRaises(ToolInputError):
            self.router.execute("get_weather", {"city": "上海", "url": "http://127.0.0.1"})

    def test_weather_uses_structured_result(self):
        result = self.router.execute("get_weather", '{"city":"上海"}')
        self.assertEqual(self.weather.cities, ["上海"])
        self.assertEqual(result.payload["source"], "Open-Meteo")
        self.assertIn("27.5", result.spoken_fallback)
        self.assertEqual(result.client_actions, [])

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

    def test_negated_commands_never_execute_or_query_network(self):
        for text in (
            "不要播放音乐",
            "不要停止音乐",
            "我不想查询上海天气",
            "不要给我播放音乐",
            "请不要再帮我播放音乐",
            "我不需要你查询上海天气",
            "do not play music",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.router.deterministic_match(text))
        self.assertEqual(self.weather.cities, [])

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

    def test_weather_response_size_limit_is_bounded(self):
        response = FakeHTTPResponse({"results": []})
        response.headers = {"Content-Length": str(MAX_TOOL_RESPONSE_BYTES + 1)}
        client = WeatherClient()
        client._opener = RecordingWeatherOpener([response])
        with self.assertRaisesRegex(ToolNetworkError, "内容过大"):
            client.current("上海")


if __name__ == "__main__":
    unittest.main()
