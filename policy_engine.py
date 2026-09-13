"""Deterministic authorization policy for assistant tool calls.

The language model may propose a tool call, but it must not decide whether the
call is permitted.  This module keeps that decision in a small, immutable
allow-list that can later grow to cover calendar, email, and shopping tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, Optional


class PolicyDecision(str, Enum):
    """Outcome of evaluating a proposed tool call."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class RiskLevel(str, Enum):
    """Human-readable risk category stored with every registered tool."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfirmationRequirement(str, Enum):
    """Confirmation strength a tool may require before execution."""

    NONE = "none"
    USER = "user"
    DEVICE_AUTH = "device_auth"


@dataclass(frozen=True)
class ToolPolicy:
    """Static policy metadata for one exact tool name."""

    name: str
    risk_level: RiskLevel = RiskLevel.LOW
    confirmation: ConfirmationRequirement = ConfirmationRequirement.NONE
    requires_network: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("Tool policy names must be non-empty and normalized.")
        if not isinstance(self.risk_level, RiskLevel):
            raise TypeError("risk_level must be a RiskLevel value.")
        if not isinstance(self.confirmation, ConfirmationRequirement):
            raise TypeError("confirmation must be a ConfirmationRequirement value.")
        if (
            self.enabled
            and self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            and self.confirmation is ConfirmationRequirement.NONE
        ):
            raise ValueError("Enabled high-risk tools must require confirmation.")


@dataclass(frozen=True)
class PolicyEvaluation:
    """Deterministic result returned before a tool can be executed."""

    tool_name: str
    decision: PolicyDecision
    risk_level: Optional[RiskLevel]
    confirmation: ConfirmationRequirement
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


DEFAULT_TOOL_POLICIES = (
    ToolPolicy("get_local_time"),
    ToolPolicy("get_weather", requires_network=True),
    ToolPolicy("web_search", requires_network=True),
    # The backend only emits a fixed station id.  The browser's separately
    # allow-listed player owns the eventual stream connection.
    ToolPolicy("play_radio"),
    ToolPolicy("pause_music"),
    ToolPolicy("resume_music"),
    ToolPolicy("stop_music"),
    ToolPolicy("set_music_volume"),
)


class PolicyEngine:
    """Evaluate tool calls against an immutable local allow-list."""

    def __init__(self, policies: Iterable[ToolPolicy] = DEFAULT_TOOL_POLICIES) -> None:
        policy_by_name = {}
        for policy in policies:
            if not isinstance(policy, ToolPolicy):
                raise TypeError("policies must contain ToolPolicy values.")
            if policy.name in policy_by_name:
                raise ValueError("Duplicate tool policy: %s" % policy.name)
            policy_by_name[policy.name] = policy
        self._policies: Mapping[str, ToolPolicy] = MappingProxyType(policy_by_name)

    @property
    def policies(self) -> Mapping[str, ToolPolicy]:
        """Read-only policy metadata, keyed by exact tool name."""

        return self._policies

    def policy_for(self, tool_name: object) -> Optional[ToolPolicy]:
        """Return metadata for a known tool without granting permission."""

        if not isinstance(tool_name, str):
            return None
        return self._policies.get(tool_name.strip())

    def evaluate(
        self,
        tool_name: object,
        *,
        allow_network: bool = False,
        confirmed: bool = False,
    ) -> PolicyEvaluation:
        """Return allow, confirm, or deny for one proposed tool call."""

        normalized_name = tool_name.strip() if isinstance(tool_name, str) else ""
        policy = self._policies.get(normalized_name)
        if policy is None:
            return PolicyEvaluation(
                normalized_name,
                PolicyDecision.DENY,
                None,
                ConfirmationRequirement.NONE,
                "unknown_tool",
            )
        if not policy.enabled:
            return PolicyEvaluation(
                policy.name,
                PolicyDecision.DENY,
                policy.risk_level,
                policy.confirmation,
                "tool_disabled",
            )
        if policy.requires_network and allow_network is not True:
            return PolicyEvaluation(
                policy.name,
                PolicyDecision.DENY,
                policy.risk_level,
                policy.confirmation,
                "network_disabled",
            )
        if (
            policy.confirmation is not ConfirmationRequirement.NONE
            and confirmed is not True
        ):
            return PolicyEvaluation(
                policy.name,
                PolicyDecision.CONFIRM,
                policy.risk_level,
                policy.confirmation,
                "confirmation_required",
            )
        return PolicyEvaluation(
            policy.name,
            PolicyDecision.ALLOW,
            policy.risk_level,
            policy.confirmation,
            "policy_allowed",
        )
