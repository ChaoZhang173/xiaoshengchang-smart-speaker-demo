import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from brain_adapter import (  # noqa: E402
    AssistantTurn,
    BrainRequest,
    CallbackBrainAdapter,
)


class BrainAdapterTests(unittest.TestCase):
    def test_callback_adapter_keeps_provider_protocol_outside_gateway(self):
        seen = []

        def respond(request, tools):
            seen.append((request, tools))
            return AssistantTurn("回答", [{"type": "demo"}], ["demo_tool"])

        adapter = CallbackBrainAdapter(
            "test-brain",
            responder=respond,
            transcriber=lambda audio, mime, suffix: "转录",
            synthesizer=lambda text: b"speech",
        )
        request = BrainRequest("问题", "session123", [], "memory")
        marker = object()

        turn = adapter.respond(request, marker)

        self.assertEqual(adapter.name, "test-brain")
        self.assertEqual(turn.text, "回答")
        self.assertEqual(seen, [(request, marker)])
        self.assertEqual(adapter.transcribe(b"audio", "audio/wav", ".wav"), "转录")
        self.assertEqual(adapter.synthesize("回答"), b"speech")

    def test_optional_modalities_fail_closed(self):
        adapter = CallbackBrainAdapter(
            "text-only",
            responder=lambda request, tools: AssistantTurn("回答"),
        )
        with self.assertRaises(RuntimeError):
            adapter.transcribe(b"audio", "audio/wav", ".wav")
        with self.assertRaises(RuntimeError):
            adapter.synthesize("回答")

    def test_adapter_rejects_invalid_results(self):
        adapter = CallbackBrainAdapter(
            "bad",
            responder=lambda request, tools: "not-a-turn",
        )
        with self.assertRaises(TypeError):
            adapter.respond(BrainRequest("问题", "session123", []), object())


if __name__ == "__main__":
    unittest.main()
