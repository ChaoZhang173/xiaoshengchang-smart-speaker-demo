"""Provider-neutral orchestration and governed tool execution.

The gateway is the stable boundary between the voice/UI pipeline and whichever
"brain" is selected. A model may propose an allow-listed tool, but local
policy and current-user intent remain authoritative. Only this module can
forward browser actions produced by an approved tool execution.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Type

from brain_adapter import AssistantTurn, BrainAdapter, BrainRequest
from execution_ledger import (
    AWAITING_CONFIRMATION,
    RUNNING,
    ExecutionLedger,
    ExecutionLedgerError,
)
from policy_engine import (
    ConfirmationRequirement,
    PolicyDecision,
    PolicyEngine,
    PolicyEvaluation,
)
from tool_router import (
    ToolExecution,
    ToolInputError,
    ToolNetworkError,
    ToolRouter,
)


MUSIC_TOOLS = frozenset(
    {
        "play_radio",
        "pause_music",
        "resume_music",
        "stop_music",
        "set_music_volume",
    }
)
USER_BOUND_TOOLS = MUSIC_TOOLS | {"get_weather", "web_search"}


@dataclass(frozen=True)
class ToolInvocationContext:
    """Host-controlled permissions attached to one assistant turn."""

    source: str
    user_text: str
    session_fingerprint: str = "not_recorded"
    audit_key: bytes = b""
    allow_network: bool = False


@dataclass(frozen=True)
class GatewayResult:
    """Provider-neutral outcome consumed by the existing HTTP/UI pipeline."""

    assistant_turn: AssistantTurn
    speech: Optional[bytes] = None
    tts_warning: Optional[str] = None


class ToolPolicyError(ToolInputError):
    """Safe, local error raised when policy prevents tool execution."""

    def __init__(
        self,
        message: str,
        *,
        evaluation: PolicyEvaluation,
        execution_id: str,
    ) -> None:
        super().__init__(message)
        self.evaluation = evaluation
        self.execution_id = execution_id


class ToolConfirmationRequired(ToolPolicyError):
    """Raised when a registered tool needs confirmation before it may run."""


class GovernedToolRouter(ToolRouter):
    """Apply host policy, intent binding, and audit around a ToolRouter."""

    def __init__(
        self,
        router: ToolRouter,
        policy: PolicyEngine,
        ledger: ExecutionLedger,
        context: ToolInvocationContext,
    ) -> None:
        # ``ToolRouter.deterministic_match`` deliberately calls ``self.execute``.
        # Reusing it on this wrapper means local fallbacks receive the same
        # policy, intent, and ledger checks as model-selected tool calls.
        self._router = router
        self.policy = policy
        self.ledger = ledger
        self.context = context
        self.weather = router.weather
        self.search = router.search
        self.station_by_id = router.station_by_id
        self._approved_client_actions = []
        self._attempt_count = 0

    @property
    def approved_client_actions(self) -> list:
        """Return detached actions proven to come from approved executions."""

        return copy.deepcopy(self._approved_client_actions)

    def definitions(self) -> list:
        """Expose only locally registered tools that policy has not denied."""

        visible = []
        for definition in self._router.definitions():
            try:
                name = definition["function"]["name"]
            except (KeyError, TypeError):
                continue
            evaluation = self.policy.evaluate(
                name,
                allow_network=self.context.allow_network,
                confirmed=False,
            )
            if evaluation.decision is not PolicyDecision.DENY:
                visible.append(definition)
        return visible

    def execute(self, name_value: Any, arguments: Any) -> ToolExecution:
        name = str(name_value or "").strip()
        self._attempt_count += 1
        evaluation = self.policy.evaluate(
            name,
            allow_network=self.context.allow_network,
            confirmed=False,
        )
        bound_arguments = arguments
        intent_message = None
        if self._attempt_count > 1:
            evaluation = PolicyEvaluation(
                tool_name=name,
                decision=PolicyDecision.DENY,
                risk_level=evaluation.risk_level,
                confirmation=evaluation.confirmation,
                reason="tool_call_limit",
            )
            bound_arguments = {}
            intent_message = "一轮指令最多执行一个工具，后续操作没有执行。"
        elif evaluation.decision is not PolicyDecision.DENY:
            bound_arguments, intent_message = self._bind_user_arguments(
                name,
                arguments,
            )
            if intent_message is not None:
                evaluation = PolicyEvaluation(
                    tool_name=name,
                    decision=PolicyDecision.DENY,
                    risk_level=evaluation.risk_level,
                    confirmation=evaluation.confirmation,
                    reason="user_intent_not_authorized",
                )

        record = self._start_record(
            name,
            bound_arguments,
            evaluation,
            omit_arguments=evaluation.decision is PolicyDecision.DENY,
        )
        if evaluation.decision is PolicyDecision.DENY:
            self.ledger.block(record.execution_id, evaluation.reason)
            raise ToolPolicyError(
                intent_message or self._denied_message(evaluation),
                evaluation=evaluation,
                execution_id=record.execution_id,
            )
        if evaluation.decision is PolicyDecision.CONFIRM:
            self.ledger.await_confirmation(
                record.execution_id,
                {
                    "requirement": evaluation.confirmation.value,
                    "message": "需要用户确认后才能执行。",
                },
            )
            raise ToolConfirmationRequired(
                self._confirmation_message(evaluation.confirmation),
                evaluation=evaluation,
                execution_id=record.execution_id,
            )

        try:
            execution = self._router.execute(name, bound_arguments)
            approved_actions = self._validate_execution(
                name,
                bound_arguments,
                execution,
            )
            result_snapshot = {
                "ok": execution.payload.get("ok") is not False,
                "client_action_count": len(approved_actions),
                "external_effect_status": (
                    "client_ack_unavailable"
                    if approved_actions
                    else "completed_locally"
                ),
            }
            if execution.payload.get("ok") is False:
                self.ledger.fail(
                    record.execution_id,
                    {
                        "type": "ToolReportedFailure",
                        "code": "tool_reported_failure",
                    },
                )
            elif approved_actions:
                self.ledger.dispatch(record.execution_id, result_snapshot)
                self._approved_client_actions.extend(approved_actions)
            else:
                self.ledger.complete(record.execution_id, result_snapshot)
            return execution
        except Exception as exc:
            self._fail_if_active(record.execution_id, type(exc).__name__)
            raise

    def _network_tool(
        self,
        name: str,
        arguments: dict,
        *,
        allow_network: bool,
        offline_message: str,
    ) -> ToolExecution:
        """Record an offline guard instead of silently bypassing governance."""

        if not allow_network:
            self._attempt_count += 1
            evaluation = self.policy.evaluate(
                name,
                allow_network=False,
                confirmed=False,
            )
            if self._attempt_count > 1:
                evaluation = PolicyEvaluation(
                    tool_name=name,
                    decision=PolicyDecision.DENY,
                    risk_level=evaluation.risk_level,
                    confirmation=evaluation.confirmation,
                    reason="tool_call_limit",
                )
            elif evaluation.decision is not PolicyDecision.DENY:
                evaluation = PolicyEvaluation(
                    tool_name=name,
                    decision=PolicyDecision.DENY,
                    risk_level=evaluation.risk_level,
                    confirmation=evaluation.confirmation,
                    reason="network_disabled",
                )
            record = self._start_record(
                name,
                arguments,
                evaluation,
                omit_arguments=True,
            )
            self.ledger.block(record.execution_id, evaluation.reason)
            spoken = (
                "一轮指令最多执行一个工具，后续操作没有执行。"
                if evaluation.reason == "tool_call_limit"
                else offline_message
            )
            return ToolExecution(
                name,
                {"ok": False, "offline_demo": True},
                spoken,
            )
        try:
            return self.execute(name, arguments)
        except (ToolInputError, ToolNetworkError) as exc:
            return ToolExecution(
                name,
                {"ok": False, "error": str(exc)},
                str(exc),
            )

    def _bind_user_arguments(
        self,
        name: str,
        arguments: Any,
    ) -> Tuple[Any, Optional[str]]:
        if name not in USER_BOUND_TOOLS:
            return arguments, None
        text = " ".join(str(self.context.user_text or "").strip().split())
        if not text or self._router.has_negated_action(text):
            return {}, "当前指令没有明确授权这个操作，操作没有执行。"

        authorized = self._router.authorized_intent(text)
        if authorized is None or authorized.get("tool_name") != name:
            return {}, self._router.intent_recovery_message(name, text)

        if name == "get_weather":
            guard = self._router.weather_guard(text)
            if guard is not None:
                return {}, guard.spoken_fallback
            city = str(authorized["arguments"].get("city", "")).strip()
            if not city:
                return {}, "你想查询哪个城市的天气？"
            return {"city": city}, None

        if name == "web_search":
            query = str(authorized["arguments"].get("query", "")).strip()
            if not query:
                return {}, self._router.intent_recovery_message(name, text)
            return {"query": query}, None

        return dict(authorized["arguments"]), None

    def _start_record(
        self,
        name: str,
        arguments: Any,
        evaluation: PolicyEvaluation,
        *,
        omit_arguments: bool,
    ) -> Any:
        operation = (
            name
            if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name)
            else "invalid_tool"
        )
        audit_arguments = (
            {"arguments_omitted": True}
            if omit_arguments
            else self._audit_arguments(name, arguments)
        )
        return self.ledger.start(
            operation,
            audit_arguments,
            metadata={
                "session_fingerprint": self.context.session_fingerprint,
                "source": self.context.source,
                "policy_decision": evaluation.decision.value,
                "policy_reason": evaluation.reason,
                "risk_level": (
                    evaluation.risk_level.value if evaluation.risk_level else None
                ),
                "confirmation": evaluation.confirmation.value,
            },
        )

    def _audit_arguments(self, name: str, arguments: Any) -> dict:
        if not isinstance(arguments, dict):
            return {"argument_shape": type(arguments).__name__}
        if name == "get_weather":
            city = str(arguments.get("city", ""))
            return {
                "city_fingerprint": self._fingerprint("city", city),
                "city_length": len(city),
            }
        if name == "web_search":
            query = str(arguments.get("query", ""))
            return {
                "query_fingerprint": self._fingerprint("query", query),
                "query_length": len(query),
            }
        if name == "play_radio":
            return {"station_id": str(arguments.get("station_id", ""))}
        if name == "set_music_volume":
            return {"volume": arguments.get("volume")}
        return {}

    def _fingerprint(self, label: str, value: str) -> str:
        if not self.context.audit_key:
            return "not_recorded"
        return hmac.new(
            self.context.audit_key,
            (label + "\0" + value).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:20]

    def _validate_execution(
        self,
        name: str,
        bound_arguments: Any,
        execution: Any,
    ) -> list:
        if not isinstance(execution, ToolExecution):
            raise ToolInputError("本地工具返回了无效结果，操作已停止。")
        if execution.name != name or not isinstance(execution.payload, dict):
            raise ToolInputError("本地工具返回了无效结果，操作已停止。")
        if not isinstance(execution.spoken_fallback, str):
            raise ToolInputError("本地工具返回了无效结果，操作已停止。")
        actions = execution.client_actions
        if not isinstance(actions, list) or any(
            not isinstance(action, dict) for action in actions
        ):
            raise ToolInputError("本地工具返回了无效动作，操作已停止。")
        if execution.payload.get("ok") is False and actions:
            raise ToolInputError("失败的工具不能下发设备动作。")
        self._validate_action_shape(name, bound_arguments, actions)
        return copy.deepcopy(actions)

    def _validate_action_shape(
        self,
        name: str,
        bound_arguments: Any,
        actions: list,
    ) -> None:
        expected_types = {
            "play_radio": "music.play",
            "pause_music": "music.pause",
            "resume_music": "music.resume",
            "stop_music": "music.stop",
            "set_music_volume": "music.set_volume",
        }
        expected_type = expected_types.get(name)
        if expected_type is None:
            if actions:
                raise ToolInputError("这个工具不能下发浏览器动作。")
            return
        if len(actions) != 1 or actions[0].get("type") != expected_type:
            raise ToolInputError("本地工具返回了不匹配的浏览器动作。")
        action = actions[0]
        if name == "play_radio":
            if set(action) != {"type", "station_id"}:
                raise ToolInputError("播放动作包含未允许的字段。")
            if action.get("station_id") not in self.station_by_id:
                raise ToolInputError("播放动作中的电台不在本地白名单中。")
            if (
                not isinstance(bound_arguments, dict)
                or action.get("station_id") != bound_arguments.get("station_id")
            ):
                raise ToolInputError("播放动作与用户授权的电台不一致。")
        elif name == "set_music_volume":
            volume = action.get("volume")
            if (
                set(action) != {"type", "volume"}
                or isinstance(volume, bool)
                or not isinstance(volume, int)
                or not 0 <= volume <= 100
            ):
                raise ToolInputError("音量动作包含无效参数。")
            if (
                not isinstance(bound_arguments, dict)
                or volume != bound_arguments.get("volume")
            ):
                raise ToolInputError("音量动作与用户授权的数值不一致。")
        elif set(action) != {"type"}:
            raise ToolInputError("音乐控制动作包含未允许的字段。")

    def _fail_if_active(self, execution_id: str, error_type: str) -> None:
        try:
            record = self.ledger.get(execution_id)
            if record.status in {RUNNING, AWAITING_CONFIRMATION}:
                self.ledger.fail(
                    execution_id,
                    {"type": error_type, "code": "tool_execution_failed"},
                )
        except ExecutionLedgerError:
            # Preserve the original tool error. The ledger must never cause a
            # caller to retry a tool merely because bookkeeping also failed.
            return

    @staticmethod
    def _denied_message(evaluation: PolicyEvaluation) -> str:
        if evaluation.reason == "network_disabled":
            return "当前不允许这个工具访问网络，操作没有执行。"
        if evaluation.reason == "tool_disabled":
            return "这个工具当前已停用，操作没有执行。"
        return "这个工具没有在本地允许列表中，操作没有执行。"

    @staticmethod
    def _confirmation_message(
        requirement: ConfirmationRequirement,
    ) -> str:
        if requirement is ConfirmationRequirement.DEVICE_AUTH:
            return "这个操作需要先在可信设备上验证，尚未执行。"
        return "这个操作需要你先确认，尚未执行。"


class AgentGateway:
    """Coordinate local routing, bounded memory, a brain, tools, and speech."""

    def __init__(
        self,
        *,
        brain: BrainAdapter,
        sessions: Any,
        memories: Any,
        tools: ToolRouter,
        policy: Optional[PolicyEngine] = None,
        ledger: Optional[ExecutionLedger] = None,
        local_responder: Optional[
            Callable[[str, GovernedToolRouter], Optional[AssistantTurn]]
        ] = None,
        uses_cloud_tts: bool = False,
        memory_error_types: Tuple[Type[BaseException], ...] = (),
        speech_error_types: Tuple[Type[BaseException], ...] = (),
    ) -> None:
        self.brain = brain
        self.sessions = sessions
        self.memories = memories
        self.tools = tools
        self.policy = policy if policy is not None else PolicyEngine()
        self.ledger = ledger if ledger is not None else ExecutionLedger()
        self.local_responder = local_responder
        self.uses_cloud_tts = uses_cloud_tts
        self.memory_error_types = self._exception_types(
            memory_error_types,
            "memory_error_types",
        )
        self.speech_error_types = self._exception_types(
            speech_error_types,
            "speech_error_types",
        )
        self._audit_key = secrets.token_bytes(32)

    def transcribe(self, audio: bytes, mime: str, suffix: str) -> str:
        return self.brain.transcribe(audio, mime, suffix)

    def run(
        self,
        text: str,
        session_id: str,
        *,
        allow_network: bool = False,
    ) -> GatewayResult:
        context = ToolInvocationContext(
            source=self.brain.name,
            user_text=text,
            session_fingerprint=self._session_fingerprint(session_id),
            audit_key=self._audit_key,
            allow_network=allow_network,
        )
        governed_tools = GovernedToolRouter(
            self.tools,
            self.policy,
            self.ledger,
            context,
        )

        if self.local_responder is not None:
            local_turn = self.local_responder(text, governed_tools)
            if local_turn is not None:
                if not isinstance(local_turn, AssistantTurn):
                    raise TypeError("Local responders must return AssistantTurn or None.")
                return GatewayResult(
                    self._authoritative_turn(local_turn, governed_tools)
                )

        history = self.sessions.get_history(session_id)
        memory_context = self._memory_context(text)
        request = BrainRequest(
            text=text,
            session_id=session_id,
            history=history,
            memory_context=memory_context,
        )
        proposed_turn = self.brain.respond(request, governed_tools)
        turn = self._authoritative_turn(proposed_turn, governed_tools)

        speech = None
        tts_warning = None
        if self.uses_cloud_tts:
            if len(turn.text) <= 4_000:
                try:
                    speech = self.brain.synthesize(turn.text)
                except self.speech_error_types:
                    tts_warning = "云端语音合成失败，已改用 Mac 的系统语音。"
            else:
                tts_warning = "回答较长，已改用 Mac 的系统语音。"

        self.sessions.add_turn(session_id, text, turn.text)
        return GatewayResult(turn, speech, tts_warning)

    def _memory_context(self, text: str) -> str:
        try:
            selected = self.memories.select_context(text, limit=5, max_chars=1_200)
            return self.memories.context_for_prompt(selected) if selected else ""
        except self.memory_error_types:
            return ""

    def _session_fingerprint(self, session_id: str) -> str:
        return hmac.new(
            self._audit_key,
            ("session\0" + str(session_id)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:20]

    @staticmethod
    def _authoritative_turn(
        proposed: AssistantTurn,
        tools: GovernedToolRouter,
    ) -> AssistantTurn:
        return AssistantTurn(
            proposed.text,
            tools.approved_client_actions,
            list(proposed.tools_used),
        )

    @staticmethod
    def _exception_types(
        values: Tuple[Type[BaseException], ...],
        field_name: str,
    ) -> Tuple[Type[BaseException], ...]:
        if not isinstance(values, tuple) or any(
            not isinstance(value, type) or not issubclass(value, BaseException)
            for value in values
        ):
            raise TypeError("%s must be a tuple of exception types." % field_name)
        return values


__all__ = [
    "AgentGateway",
    "GatewayResult",
    "GovernedToolRouter",
    "ToolConfirmationRequired",
    "ToolInvocationContext",
    "ToolPolicyError",
]
