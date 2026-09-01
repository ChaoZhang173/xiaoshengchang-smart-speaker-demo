"""Allow-listed local and network tools for the smart-speaker demo.

The language model may choose a registered tool and provide structured
arguments, but it never receives a Python callable, shell command, local file
path, or arbitrary URL.  This module intentionally uses only the Python
standard library so the Mac demo remains dependency-free.
"""

from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_TOOL_ARGUMENT_CHARS = 2_000
MAX_TOOL_RESPONSE_BYTES = 256 * 1024
WEATHER_TIMEOUT_SECONDS = 9
WEATHER_CACHE_SECONDS = 10 * 60


RADIO_STATIONS = (
    {
        "id": "soma_groove_salad",
        "name": "SomaFM · Groove Salad",
        "description": "氛围音乐与轻电子，适合工作和放松。",
        "stream_url": "https://ice5.somafm.com/groovesalad-128-mp3",
        "source_url": "https://somafm.com/groovesalad/directstreamlinks.html",
    },
)


class ToolInputError(ValueError):
    """A safe validation error for a model-requested tool call."""


class ToolNetworkError(RuntimeError):
    """A safe network failure from an allow-listed online tool."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass
class ToolExecution:
    name: str
    payload: Dict[str, Any]
    spoken_fallback: str
    client_actions: List[Dict[str, Any]] = field(default_factory=list)

    def model_content(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


class WeatherClient:
    """Small fixed-host Open-Meteo client with a short in-memory cache."""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _clean_city(value: Any) -> str:
        city = " ".join(str(value or "").strip().split())
        if len(city) < 2 or len(city) > 80:
            raise ToolInputError("城市名称需要在 2 到 80 个字符之间。")
        if any(ord(char) < 32 for char in city):
            raise ToolInputError("城市名称包含无效字符。")
        return city

    def current(self, city_value: Any) -> Dict[str, Any]:
        city = self._clean_city(city_value)
        cache_key = city.casefold()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - float(cached[0]) < WEATHER_CACHE_SECONDS:
                return dict(cached[1])

        geocoding = self._get_json(
            self.GEOCODING_URL,
            {
                "name": city,
                "count": "1",
                "language": "zh",
                "format": "json",
            },
        )
        results = geocoding.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ToolInputError("没有找到这个城市，请换一个更完整的名称。")
        location = results[0]
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolNetworkError("天气位置服务返回了无法解析的数据。") from exc
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            raise ToolNetworkError("天气位置服务返回了无效坐标。")

        forecast = self._get_json(
            self.FORECAST_URL,
            {
                "latitude": "%.5f" % latitude,
                "longitude": "%.5f" % longitude,
                "current": (
                    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "timezone": "auto",
                "forecast_days": "1",
            },
        )
        current = forecast.get("current")
        if not isinstance(current, dict):
            raise ToolNetworkError("天气服务返回了无法解析的数据。")
        try:
            weather_code = int(current.get("weather_code", -1))
            temperature = round(float(current["temperature_2m"]), 1)
            apparent = round(float(current["apparent_temperature"]), 1)
            humidity = int(round(float(current["relative_humidity_2m"])))
            wind = round(float(current["wind_speed_10m"]), 1)
            precipitation = round(float(current.get("precipitation", 0)), 1)
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolNetworkError("天气服务返回的数据缺少必要字段。") from exc
        numeric_values = (temperature, apparent, float(humidity), precipitation, wind)
        if not all(math.isfinite(value) for value in numeric_values):
            raise ToolNetworkError("天气服务返回了无效数值。")
        if not 0 <= humidity <= 100 or precipitation < 0 or wind < 0:
            raise ToolNetworkError("天气服务返回了超出范围的数值。")

        display_parts = [str(location.get("name", city)).strip() or city]
        admin = str(location.get("admin1", "")).strip()
        country = str(location.get("country", "")).strip()
        if admin and admin not in display_parts:
            display_parts.append(admin)
        if country and country not in display_parts:
            display_parts.append(country)
        result = {
            "source": "Open-Meteo",
            "location": "，".join(display_parts),
            "observed_at": str(current.get("time", "")),
            "timezone": str(forecast.get("timezone", location.get("timezone", ""))),
            "condition": self._condition(weather_code),
            "temperature_c": temperature,
            "apparent_temperature_c": apparent,
            "relative_humidity_percent": humidity,
            "precipitation_mm": precipitation,
            "wind_speed_kmh": wind,
        }
        with self._lock:
            self._cache[cache_key] = (time.monotonic(), dict(result))
            if len(self._cache) > 64:
                oldest_key = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest_key, None)
        return result

    def _get_json(self, base_url: str, query: Dict[str, str]) -> Dict[str, Any]:
        url = base_url + "?" + urlencode(query)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "xiaoshengchang-smart-speaker/0.2",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=WEATHER_TIMEOUT_SECONDS) as response:
                length_header = str(response.headers.get("Content-Length", "")).strip()
                if length_header:
                    try:
                        if int(length_header) > MAX_TOOL_RESPONSE_BYTES:
                            raise ToolNetworkError("天气服务返回内容过大。")
                    except ValueError:
                        pass
                raw = response.read(MAX_TOOL_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ToolNetworkError("天气服务暂时不可用，请稍后重试。") from exc
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise ToolNetworkError("无法连接天气服务，请检查网络后重试。") from exc
        if len(raw) > MAX_TOOL_RESPONSE_BYTES:
            raise ToolNetworkError("天气服务返回内容过大。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolNetworkError("天气服务返回了无法解析的数据。") from exc
        if not isinstance(payload, dict):
            raise ToolNetworkError("天气服务返回的数据格式不正确。")
        return payload

    @staticmethod
    def _condition(code: int) -> str:
        if code == 0:
            return "晴"
        if code in {1, 2}:
            return "少云到多云"
        if code == 3:
            return "阴"
        if code in {45, 48}:
            return "有雾"
        if code in {51, 53, 55, 56, 57}:
            return "毛毛雨"
        if code in {61, 63, 65, 66, 67, 80, 81, 82}:
            return "有雨"
        if code in {71, 73, 75, 77, 85, 86}:
            return "有雪"
        if code in {95, 96, 99}:
            return "雷雨"
        return "天气状况未知"


class ToolRouter:
    """Validate and execute a small, fixed registry of assistant tools."""

    def __init__(self, weather_client: Optional[WeatherClient] = None) -> None:
        self.weather = weather_client or WeatherClient()
        self.station_by_id = {station["id"]: dict(station) for station in RADIO_STATIONS}

    def definitions(self) -> List[Dict[str, Any]]:
        station_ids = sorted(self.station_by_id)
        return [
            self._definition("get_local_time", "读取这台设备当前的本地日期、星期和时间。", {}),
            self._definition(
                "get_weather",
                "查询指定城市当前天气。涉及实时天气时必须调用此工具，不要凭记忆回答。",
                {
                    "city": {
                        "type": "string",
                        "description": "完整城市名，例如上海、杭州市或 Paris, France。",
                        "minLength": 2,
                        "maxLength": 80,
                    }
                },
                required=["city"],
            ),
            self._definition(
                "play_radio",
                "播放白名单中的网络电台。用户泛指播放音乐时选择可用的默认电台。",
                {
                    "station_id": {
                        "type": "string",
                        "enum": station_ids,
                        "description": "只能使用列出的白名单电台编号。",
                    }
                },
                required=["station_id"],
            ),
            self._definition("pause_music", "暂停当前音乐。", {}),
            self._definition("resume_music", "继续播放已经暂停的音乐。", {}),
            self._definition("stop_music", "停止音乐并清除当前播放。", {}),
            self._definition(
                "set_music_volume",
                "把音乐音量设置为 0 到 100 的整数。",
                {
                    "volume": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    }
                },
                required=["volume"],
            ),
        ]

    @staticmethod
    def _definition(
        name: str,
        description: str,
        properties: Dict[str, Any],
        required: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        parameters: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    def execute(self, name_value: Any, arguments: Any) -> ToolExecution:
        name = str(name_value or "").strip()
        if not re.fullmatch(r"[a-z_]{1,64}", name):
            raise ToolInputError("工具名称无效。")
        args = self._arguments(arguments)
        if name == "get_local_time":
            self._require_keys(args, set())
            now = datetime.now().astimezone()
            weekday = "一二三四五六日"[now.weekday()]
            payload = {
                "local_time": now.isoformat(timespec="seconds"),
                "timezone": str(now.tzinfo or ""),
                "weekday_zh": "星期" + weekday,
            }
            spoken = "现在是%s，%s。" % (
                now.strftime("%Y年%m月%d日 %H点%M分"),
                payload["weekday_zh"],
            )
            return ToolExecution(name, payload, spoken)
        if name == "get_weather":
            self._require_keys(args, {"city"}, required={"city"})
            payload = self.weather.current(args["city"])
            spoken = (
                "%s现在%s，%.1f摄氏度，体感%.1f摄氏度，湿度%d%%，风速%.1f公里每小时。"
                % (
                    payload["location"],
                    payload["condition"],
                    payload["temperature_c"],
                    payload["apparent_temperature_c"],
                    payload["relative_humidity_percent"],
                    payload["wind_speed_kmh"],
                )
            )
            return ToolExecution(name, payload, spoken)
        if name == "play_radio":
            self._require_keys(args, {"station_id"}, required={"station_id"})
            station_id = str(args["station_id"] or "")
            station = self.station_by_id.get(station_id)
            if station is None:
                raise ToolInputError("请求的电台不在本地白名单中。")
            return ToolExecution(
                name,
                {"ok": True, "station_id": station_id, "station_name": station["name"]},
                "好的，准备播放%s。" % station["name"],
                [{"type": "music.play", "station_id": station_id}],
            )
        if name in {"pause_music", "resume_music", "stop_music"}:
            self._require_keys(args, set())
            details = {
                "pause_music": ("music.pause", "好的，音乐已暂停。"),
                "resume_music": ("music.resume", "好的，继续播放音乐。"),
                "stop_music": ("music.stop", "好的，音乐已停止。"),
            }
            action, spoken = details[name]
            return ToolExecution(name, {"ok": True}, spoken, [{"type": action}])
        if name == "set_music_volume":
            self._require_keys(args, {"volume"}, required={"volume"})
            volume = args["volume"]
            if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
                raise ToolInputError("音乐音量必须是 0 到 100 的整数。")
            return ToolExecution(
                name,
                {"ok": True, "volume": volume},
                "好的，音乐音量已设为%d%%。" % volume,
                [{"type": "music.set_volume", "volume": volume}],
            )
        raise ToolInputError("这个工具没有在本地允许列表中。")

    def deterministic_match(
        self, text_value: Any, *, allow_network: bool = True
    ) -> Optional[ToolExecution]:
        """Reliable fallback if a model ignores or does not support tools."""

        text = " ".join(str(text_value or "").strip().split())
        lowered = text.casefold()
        # A deterministic fallback must never reverse an explicit refusal.
        # When intent is negated, leave the sentence to ordinary conversation
        # instead of executing a device action or making a weather request.
        if self.has_negated_action(lowered):
            return None
        if re.search(r"(现在)?(几点|什么时间|当前时间|日期|星期几)", text):
            return self.execute("get_local_time", {})
        weather_match = re.search(
            r"(?:查(?:一下)?|看看|告诉我)?\s*([\w\u3400-\u9fff· -]{2,30}?)(?:今天|现在|当前)?(?:的)?天气",
            text,
        )
        if weather_match:
            city = weather_match.group(1).strip()
            city = re.sub(r"^(请问|一下|查询|查|看看)", "", city).strip()
            if city in {"今天", "现在", "当前", "当地", "这里", "那边"}:
                return ToolExecution(
                    "get_weather",
                    {"ok": False, "missing_city": True},
                    "你想查询哪个城市的天气？",
                )
            if city:
                if not allow_network:
                    return ToolExecution(
                        "get_weather",
                        {"ok": False, "offline_demo": True},
                        "天气查询会把城市名发送给 Open-Meteo；请先配置真实联网模式。",
                    )
                return self.execute("get_weather", {"city": city})
        if "天气" in text:
            return ToolExecution(
                "get_weather",
                {"ok": False, "missing_city": True},
                "你想查询哪个城市的天气？",
            )
        if any(phrase in lowered for phrase in ("停止音乐", "关闭音乐", "stop music")):
            return self.execute("stop_music", {})
        if any(phrase in lowered for phrase in ("暂停音乐", "暂停播放", "pause music")):
            return self.execute("pause_music", {})
        if any(phrase in lowered for phrase in ("继续音乐", "继续播放", "resume music")):
            return self.execute("resume_music", {})
        volume_match = re.search(r"(?:音乐)?音量(?:调到|设置为|设为)?\s*(\d{1,3})", text)
        if volume_match:
            return self.execute("set_music_volume", {"volume": int(volume_match.group(1))})
        if any(phrase in lowered for phrase in ("播放音乐", "放点音乐", "播放电台", "play music")):
            return self.execute("play_radio", {"station_id": next(iter(self.station_by_id))})
        return None

    @staticmethod
    def has_negated_action(text_value: Any) -> bool:
        text = " ".join(str(text_value or "").strip().split()).casefold()
        return bool(
            re.search(
                r"(?:不要|别|不想|无需|不用|不需要|禁止|不是(?:要)?|"
                r"don't|do\s+not|never)\s*(?:再\s*)?"
                r"(?:(?:请|你|给我|帮我|替我|去|进行|please|for\s+me)\s*){0,3}"
                r"(?:播放|放|暂停|继续|停止|打开|关闭|查询|查|看看|告诉我|"
                r"调整|调|设置|set|play|pause|resume|stop|check)",
                text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _arguments(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            args = value
        elif isinstance(value, str):
            if len(value) > MAX_TOOL_ARGUMENT_CHARS:
                raise ToolInputError("工具参数过长。")
            try:
                args = json.loads(value or "{}")
            except json.JSONDecodeError as exc:
                raise ToolInputError("工具参数不是有效的 JSON。") from exc
        else:
            raise ToolInputError("工具参数格式无效。")
        if not isinstance(args, dict):
            raise ToolInputError("工具参数必须是对象。")
        if len(json.dumps(args, ensure_ascii=False)) > MAX_TOOL_ARGUMENT_CHARS:
            raise ToolInputError("工具参数过长。")
        return args

    @staticmethod
    def _require_keys(
        args: Dict[str, Any], allowed: set, *, required: Optional[set] = None
    ) -> None:
        if set(args) - allowed:
            raise ToolInputError("工具参数包含未允许的字段。")
        missing = (required or set()) - set(args)
        if missing:
            raise ToolInputError("工具参数缺少必要字段。")


def public_radio_stations() -> List[Dict[str, str]]:
    """Return a defensive copy safe to expose to the localhost page."""

    return [dict(station) for station in RADIO_STATIONS]
