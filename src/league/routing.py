"""Evidence-based, provider-neutral semantic model and effort routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from .storage_types import StorageRefusal


COORDINATOR = "COORDINATOR"
WORKER_FAST = "WORKER_FAST"
WORKER_STRONG = "WORKER_STRONG"
TIERS = frozenset({COORDINATOR, WORKER_FAST, WORKER_STRONG})
ESCALATION_FAILURES = frozenset(
    {
        "schema_failure",
        "tool_failure",
        "missing_evidence",
        "ambiguity",
        "conflicting_results",
        "failed_acceptance",
        "high_impact_boundary",
    }
)


class RoutingStorage(Protocol):
    def record_routing_decision(self, decision: Mapping[str, Any]) -> dict[str, Any]: ...
    def routing_decision(self, decision_id: str) -> Optional[dict[str, Any]]: ...
    def record_routing_outcome(self, outcome: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RoutingChoice:
    decision_id: str
    subject_kind: str
    subject_id: str
    role: str
    tier: str
    model: str
    effort: str
    reason: str
    explicit_model: bool
    explicit_effort: bool
    state: str
    escalation_count: int
    prior_decision_id: Optional[str]
    failure_class: Optional[str]
    chosen_at: str

    def as_record(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "role": self.role,
            "tier": self.tier,
            "model": self.model,
            "effort": self.effort,
            "reason": self.reason,
            "explicit_model": self.explicit_model,
            "explicit_effort": self.explicit_effort,
            "state": self.state,
            "escalation_count": self.escalation_count,
            "prior_decision_id": self.prior_decision_id,
            "failure_class": self.failure_class,
            "chosen_at": self.chosen_at,
        }


def load_routing_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("routing_config_invalid", "routing configuration could not be read") from exc
    if not isinstance(value, dict) or value.get("schema") not in {1, 2}:
        raise StorageRefusal("routing_config_invalid", "routing configuration schema is unsupported")
    tiers = value.get("tiers")
    if not isinstance(tiers, dict):
        raise StorageRefusal("routing_config_invalid", "routing tiers are missing")
    for name in TIERS:
        tier = tiers.get(name)
        if not isinstance(tier, dict) or not isinstance(tier.get("model"), str) or not tier["model"]:
            raise StorageRefusal("routing_config_invalid", f"routing tier is incomplete: {name}")
        if not isinstance(tier.get("effort"), str) or not tier["effort"]:
            raise StorageRefusal("routing_config_invalid", f"routing tier is incomplete: {name}")
    return value


class ModelRouter:
    """Stable API consumed by assignment code without owning assignment state."""

    def __init__(self, config: Mapping[str, Any], storage: RoutingStorage) -> None:
        self.config = dict(config)
        self.storage = storage

    def _tier(self, profile: str) -> tuple[str, str]:
        if profile == "coordination":
            return COORDINATOR, "Coordinator duties use the configured coordinator tier."
        if profile in {"ambiguous", "high-impact", "weak-verification"}:
            return WORKER_STRONG, f"{profile} work uses the strong worker quality baseline."
        if profile != "bounded":
            raise StorageRefusal("routing_profile_unknown", f"routing profile is unsupported: {profile}")
        evaluation = self.config.get("evaluations", {}).get(WORKER_FAST, {})
        if isinstance(evaluation, dict) and evaluation.get("approved") is True:
            samples = evaluation.get("representative_tasks")
            if isinstance(samples, int) and samples > 0:
                return WORKER_FAST, "Representative evaluation approved the bounded/checkable worker tier."
        return WORKER_STRONG, "No approved representative downgrade evidence exists; use the strongest worker baseline."

    def choose(
        self,
        *,
        decision_id: str,
        subject_kind: str,
        subject_id: str,
        role: str,
        profile: str,
        chosen_at: str,
        explicit_model: Optional[str] = None,
        explicit_effort: Optional[str] = None,
    ) -> dict[str, Any]:
        tier, reason = self._tier(profile)
        selected = self.config["tiers"][tier]
        model = explicit_model if explicit_model is not None else str(selected["model"])
        effort = explicit_effort if explicit_effort is not None else str(selected["effort"])
        if not all(isinstance(item, str) and item for item in (decision_id, subject_kind, subject_id, role, chosen_at, model, effort)):
            raise StorageRefusal("routing_invalid", "routing decision fields cannot be empty")
        if explicit_model is not None or explicit_effort is not None:
            reason = "Explicit user choices were preserved exactly; only unspecified fields use the selected semantic tier."
        choice = RoutingChoice(
            decision_id,
            subject_kind,
            subject_id,
            role,
            tier,
            model,
            effort,
            reason,
            explicit_model is not None,
            explicit_effort is not None,
            "selected",
            0,
            None,
            None,
            chosen_at,
        )
        return self.storage.record_routing_decision(choice.as_record())

    def escalate(
        self,
        *,
        decision_id: str,
        prior_decision_id: str,
        failure_class: str,
        chosen_at: str,
    ) -> dict[str, Any]:
        if failure_class not in ESCALATION_FAILURES:
            raise StorageRefusal("escalation_not_evidenced", "failure does not justify routing escalation")
        prior = self.storage.routing_decision(prior_decision_id)
        if prior is None:
            raise StorageRefusal("routing_decision_unknown", "prior routing decision does not exist")
        already_escalated = int(prior["escalation_count"]) >= 1
        if already_escalated or prior["tier"] == WORKER_STRONG:
            state = "blocked"
            tier = str(prior["tier"])
            model = str(prior["model"])
            effort = str(prior["effort"])
            reason = "The one safe-boundary escalation is unavailable or exhausted; report blocked."
        else:
            state = "escalated"
            tier = WORKER_STRONG
            selected = self.config["tiers"][tier]
            model = str(prior["model"]) if prior["explicit_model"] else str(selected["model"])
            effort = str(prior["effort"]) if prior["explicit_effort"] else str(selected["effort"])
            reason = f"Concrete {failure_class.replace('_', ' ')} triggered the one safe-boundary stronger retry."
        choice = RoutingChoice(
            decision_id,
            str(prior["subject_kind"]),
            str(prior["subject_id"]),
            str(prior["role"]),
            tier,
            model,
            effort,
            reason,
            bool(prior["explicit_model"]),
            bool(prior["explicit_effort"]),
            state,
            int(prior["escalation_count"]) + (1 if state == "escalated" else 0),
            prior_decision_id,
            failure_class,
            chosen_at,
        )
        return self.storage.record_routing_decision(choice.as_record())

    def record_outcome(
        self,
        *,
        outcome_id: str,
        decision_id: str,
        success: bool,
        corrections: int,
        latency_ms: int,
        cost_microunits: int,
        recorded_at: str,
    ) -> dict[str, Any]:
        if min(corrections, latency_ms, cost_microunits) < 0:
            raise StorageRefusal("routing_outcome_invalid", "routing outcome measures cannot be negative")
        return self.storage.record_routing_outcome(
            {
                "outcome_id": outcome_id,
                "decision_id": decision_id,
                "success": success,
                "corrections": corrections,
                "latency_ms": latency_ms,
                "cost_microunits": cost_microunits,
                "recorded_at": recorded_at,
            }
        )
