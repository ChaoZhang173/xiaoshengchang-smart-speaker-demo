import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from policy_engine import (  # noqa: E402
    ConfirmationRequirement,
    PolicyDecision,
    PolicyEngine,
    RiskLevel,
    ToolPolicy,
)


CURRENT_TOOLS = {
    "get_local_time",
    "get_weather",
    "web_search",
    "play_radio",
    "pause_music",
    "resume_music",
    "stop_music",
    "set_music_volume",
}


class PolicyEngineTests(unittest.TestCase):
    def test_default_registry_covers_current_tools_without_confirmation(self):
        engine = PolicyEngine()

        self.assertEqual(set(engine.policies), CURRENT_TOOLS)
        for name in CURRENT_TOOLS:
            with self.subTest(name=name):
                policy = engine.policy_for(name)
                self.assertIsNotNone(policy)
                self.assertEqual(policy.confirmation, ConfirmationRequirement.NONE)
                evaluation = engine.evaluate(name, allow_network=True)
                self.assertEqual(evaluation.decision, PolicyDecision.ALLOW)
                self.assertTrue(evaluation.allowed)

    def test_network_policy_respects_allow_network(self):
        engine = PolicyEngine()

        network_tools = {"get_weather", "web_search"}
        for name in CURRENT_TOOLS:
            with self.subTest(name=name):
                evaluation = engine.evaluate(name, allow_network=False)
                expected = (
                    PolicyDecision.DENY
                    if name in network_tools
                    else PolicyDecision.ALLOW
                )
                self.assertEqual(evaluation.decision, expected)
                if name in network_tools:
                    self.assertEqual(evaluation.reason, "network_disabled")

    def test_unknown_or_invalid_tool_is_denied(self):
        engine = PolicyEngine()

        for name in ("shell", "calendar_create", "", None, 42):
            with self.subTest(name=name):
                evaluation = engine.evaluate(name)
                self.assertEqual(evaluation.decision, PolicyDecision.DENY)
                self.assertFalse(evaluation.allowed)
                self.assertIsNone(evaluation.risk_level)
                self.assertEqual(evaluation.reason, "unknown_tool")

    def test_future_tool_can_require_confirmation(self):
        engine = PolicyEngine(
            [
                ToolPolicy(
                    "shopping_checkout",
                    risk_level=RiskLevel.CRITICAL,
                    confirmation=ConfirmationRequirement.DEVICE_AUTH,
                    requires_network=True,
                )
            ]
        )

        pending = engine.evaluate("shopping_checkout", allow_network=True)
        self.assertEqual(pending.decision, PolicyDecision.CONFIRM)
        self.assertEqual(pending.risk_level, RiskLevel.CRITICAL)
        self.assertEqual(
            pending.confirmation,
            ConfirmationRequirement.DEVICE_AUTH,
        )
        self.assertEqual(pending.reason, "confirmation_required")

        approved = engine.evaluate(
            "shopping_checkout",
            allow_network=True,
            confirmed=True,
        )
        self.assertEqual(approved.decision, PolicyDecision.ALLOW)

        offline = engine.evaluate(
            "shopping_checkout",
            allow_network=False,
            confirmed=True,
        )
        self.assertEqual(offline.decision, PolicyDecision.DENY)
        self.assertEqual(offline.reason, "network_disabled")

        fake_confirmation = engine.evaluate(
            "shopping_checkout",
            allow_network=True,
            confirmed="yes",
        )
        self.assertEqual(fake_confirmation.decision, PolicyDecision.CONFIRM)

        fake_network_permission = engine.evaluate(
            "shopping_checkout",
            allow_network="yes",
            confirmed=True,
        )
        self.assertEqual(fake_network_permission.decision, PolicyDecision.DENY)

    def test_disabled_registered_tool_is_denied(self):
        engine = PolicyEngine(
            [ToolPolicy("email_delete", risk_level=RiskLevel.HIGH, enabled=False)]
        )

        evaluation = engine.evaluate("email_delete", confirmed=True)
        self.assertEqual(evaluation.decision, PolicyDecision.DENY)
        self.assertEqual(evaluation.reason, "tool_disabled")
        self.assertEqual(evaluation.risk_level, RiskLevel.HIGH)

    def test_enabled_high_risk_tool_cannot_omit_confirmation(self):
        for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            with self.subTest(risk=risk):
                with self.assertRaises(ValueError):
                    ToolPolicy("dangerous_action", risk_level=risk)

    def test_registry_rejects_duplicates_and_is_read_only(self):
        with self.assertRaises(ValueError):
            PolicyEngine([ToolPolicy("same"), ToolPolicy("same")])

        engine = PolicyEngine()
        with self.assertRaises(TypeError):
            engine.policies["shell"] = ToolPolicy("shell")


if __name__ == "__main__":
    unittest.main()
