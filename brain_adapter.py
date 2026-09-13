"""Provider-neutral brain interface for the local assistant gateway.

The current demo still uses the existing HTTP provider clients in ``app.py``.
This module gives the orchestration layer a stable boundary so a Codex SDK,
another cloud model, or a local model can be added without teaching the voice
pipeline about each provider's protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


@dataclass
class AssistantTurn:
    """Provider-neutral result of one assistant reasoning turn."""

    text: str
    client_actions: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)


@dataclass(frozen=True)
class BrainRequest:
    """Bounded context supplied to a brain implementation for one turn."""

    text: str
    session_id: str
    history: list
    memory_context: str = ""


class BrainAdapter(Protocol):
    """Interface implemented by every provider used as an assistant brain."""

    @property
    def name(self) -> str:
        ...

    def respond(self, request: BrainRequest, tools: Any) -> AssistantTurn:
        ...

    def transcribe(self, audio: bytes, mime: str, suffix: str) -> str:
        ...

    def synthesize(self, text: str) -> bytes:
        ...


class CallbackBrainAdapter:
    """Adapt the existing provider clients without moving their protocols yet."""

    def __init__(
        self,
        name: str,
        *,
        responder: Callable[[BrainRequest, Any], AssistantTurn],
        transcriber: Optional[Callable[[bytes, str, str], str]] = None,
        synthesizer: Optional[Callable[[str], bytes]] = None,
    ) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("A brain adapter must have a name.")
        self._name = normalized_name
        self._responder = responder
        self._transcriber = transcriber
        self._synthesizer = synthesizer

    @property
    def name(self) -> str:
        return self._name

    def respond(self, request: BrainRequest, tools: Any) -> AssistantTurn:
        turn = self._responder(request, tools)
        if not isinstance(turn, AssistantTurn):
            raise TypeError("Brain adapters must return AssistantTurn.")
        return turn

    def transcribe(self, audio: bytes, mime: str, suffix: str) -> str:
        if self._transcriber is None:
            raise RuntimeError("This brain adapter does not support transcription.")
        return self._transcriber(audio, mime, suffix)

    def synthesize(self, text: str) -> bytes:
        if self._synthesizer is None:
            raise RuntimeError("This brain adapter does not support speech synthesis.")
        return self._synthesizer(text)
