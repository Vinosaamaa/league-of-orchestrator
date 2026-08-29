"""Small, deterministic orchestration-routing policy.

The policy decides ownership only.  Launch, delivery, and durable assignment
receipts remain in their existing lifecycle modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .storage_types import StorageRefusal


LOCAL_DIRECT = "local_direct"
LOCAL_CHAMPION = "local_champion"
SQUAD_ROUTE = "squad_route"
ROUTES = frozenset({LOCAL_DIRECT, LOCAL_CHAMPION, SQUAD_ROUTE})
REASON_CODES = frozenset(
    {
        "explicit_squad",
        "explicit_champion",
        "continuation_squad",
        "unique_strong_squad",
        "direct_tiny",
        "hidden_scientist",
        "worker_required",
    }
)


@dataclass(frozen=True)
class OrchestrationSignals:
    pre_bounded: bool
    read_only: bool
    answer_or_routing_only: bool
    expected_minutes: int
    expected_task_action_calls: int
    creates_artifact: bool = False
    mutates_state: bool = False
    reproduces_issue: bool = False
    runs_tests: bool = False
    runs_benchmark: bool = False
    uses_browser_or_computer: bool = False
    project_implementation: bool = False
    hidden_advisory: bool = False
    project_suggested_shotcaller: Optional[str] = None

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "OrchestrationSignals":
        fields = set(cls.__dataclass_fields__)
        boolean_fields = fields - {
            "expected_minutes",
            "expected_task_action_calls",
            "project_suggested_shotcaller",
        }
        invalid = (
            bool(set(value) - fields)
            or any(name in value and not isinstance(value[name], bool) for name in boolean_fields)
            or any(
                name in value and type(value[name]) is not int
                for name in ("expected_minutes", "expected_task_action_calls")
            )
            or (
                "project_suggested_shotcaller" in value
                and value["project_suggested_shotcaller"] is not None
                and (
                    not isinstance(value["project_suggested_shotcaller"], str)
                    or not value["project_suggested_shotcaller"]
                )
            )
        )
        if invalid:
            raise StorageRefusal(
                "orchestration_signals_invalid", "orchestration signals have invalid types"
            )
        try:
            return cls(**value)
        except TypeError as exc:
            raise StorageRefusal(
                "orchestration_signals_invalid", "orchestration signals are incomplete"
            ) from exc

    def as_record(self) -> dict[str, object]:
        return {
            "pre_bounded": self.pre_bounded,
            "read_only": self.read_only,
            "answer_or_routing_only": self.answer_or_routing_only,
            "expected_minutes": self.expected_minutes,
            "expected_task_action_calls": self.expected_task_action_calls,
            "creates_artifact": self.creates_artifact,
            "mutates_state": self.mutates_state,
            "reproduces_issue": self.reproduces_issue,
            "runs_tests": self.runs_tests,
            "runs_benchmark": self.runs_benchmark,
            "uses_browser_or_computer": self.uses_browser_or_computer,
            "project_implementation": self.project_implementation,
            "hidden_advisory": self.hidden_advisory,
            "project_suggested_shotcaller": self.project_suggested_shotcaller,
        }

    def _validate_estimates(self) -> None:
        if (
            isinstance(self.expected_minutes, bool)
            or not isinstance(self.expected_minutes, int)
            or isinstance(self.expected_task_action_calls, bool)
            or not isinstance(self.expected_task_action_calls, int)
            or self.expected_minutes < 0
            or self.expected_task_action_calls < 0
        ):
            raise StorageRefusal(
                "orchestration_signals_invalid",
                "time and task-action estimates must be non-negative integers",
            )

    def _bounded_read_only(self) -> bool:
        self._validate_estimates()
        return all(
            (
                self.pre_bounded,
                self.read_only,
                self.expected_minutes <= 5,
                self.expected_task_action_calls <= 2,
                not self.creates_artifact,
                not self.mutates_state,
                not self.reproduces_issue,
                not self.runs_tests,
                not self.runs_benchmark,
                not self.uses_browser_or_computer,
                not self.project_implementation,
            )
        )

    def direct_tiny(self) -> bool:
        return self.answer_or_routing_only and self._bounded_read_only()

    def hidden_scientist(self) -> bool:
        """Whether a hidden scientist may perform the bounded support subtask.

        Hidden scientists are not direct execution: they may compute or advise,
        but they share the same hard safety perimeter and must stop when it is
        crossed.  ``answer_or_routing_only`` is intentionally not required
        because the bounded work is returned to the owning Shotcaller.
        """

        return self._bounded_read_only()


@dataclass(frozen=True)
class OrchestrationDecision:
    route: str
    reason_code: str
    target: Optional[str]
    requires_visible_assignment_receipt: bool

    def as_record(self) -> dict[str, object]:
        return {
            "route": self.route,
            "reason_code": self.reason_code,
            "target": self.target,
            "requires_visible_assignment_receipt": self.requires_visible_assignment_receipt,
        }


@dataclass(frozen=True)
class SquadCandidate:
    squad_id: str
    strong_match: bool
    accepting: bool
    owner_live: bool
    capabilities: frozenset[str] = frozenset()


def decide_orchestration_route(
    signals: OrchestrationSignals,
    *,
    explicit_squad_id: Optional[str] = None,
    continuation_squad_id: Optional[str] = None,
    candidates: tuple[SquadCandidate, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    cross_project: bool = False,
) -> OrchestrationDecision:
    """Choose local direct/Champion execution or a stable Squad ownership route.

    A project suggestion and hidden advisory are deliberately non-authoritative.
    Direct work still has to satisfy every direct-tiny bound even when requested.
    """

    required = frozenset(required_capabilities)
    if len(required) != len(required_capabilities) or any(not item for item in required):
        raise StorageRefusal(
            "orchestration_capabilities_invalid", "required capabilities are empty or duplicated"
        )
    requested_target = explicit_squad_id or continuation_squad_id
    if requested_target:
        target = next((item for item in candidates if item.squad_id == requested_target), None)
        if target is None or not target.accepting or not target.owner_live:
            raise StorageRefusal(
                "owner_unavailable", "explicit or continuation Squad has no accepting live owner"
            )
        if not required <= target.capabilities:
            raise StorageRefusal(
                "owner_capability_mismatch", "explicit or continuation Squad lacks a capability"
            )
        return OrchestrationDecision(
            SQUAD_ROUTE,
            "explicit_squad" if explicit_squad_id else "continuation_squad",
            requested_target,
            False,
        )
    if not cross_project:
        eligible = {
            item.squad_id
            for item in candidates
            if item.squad_id
            and item.strong_match
            and item.accepting
            and item.owner_live
            and required <= item.capabilities
        }
        if len(eligible) == 1:
            return OrchestrationDecision(
                SQUAD_ROUTE, "unique_strong_squad", next(iter(eligible)), False
            )
    if signals.direct_tiny():
        return OrchestrationDecision(LOCAL_DIRECT, "direct_tiny", None, False)
    return OrchestrationDecision(LOCAL_CHAMPION, "worker_required", None, True)
