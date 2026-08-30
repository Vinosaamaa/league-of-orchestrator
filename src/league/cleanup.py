"""Deterministic proof-first cleanup planning and crash-resumable execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from .storage_types import StorageRefusal


TASK_CLASSES = frozenset({"analysis", "local_git", "pr_ci", "deployed_service"})
DISPOSITIONS = frozenset({"completed", "rejected", "cancelled", "failed"})
CLEANUP_DISPOSITIONS_BY_TASK_STATE: dict[str, frozenset[str]] = {
    "completed": frozenset({"completed"}),
    "complete": frozenset({"completed"}),
    "ready_to_land": frozenset({"completed", "rejected", "cancelled"}),
    "rejected": frozenset({"rejected"}),
    "blocked": frozenset({"rejected", "failed"}),
    "cancelled": frozenset({"rejected", "cancelled"}),
    "canceled": frozenset({"rejected", "cancelled"}),
    "failed": frozenset({"failed"}),
}
RESOURCE_LIFETIMES = frozenset({"task_owned", "shared_lease", "persistent_retain"})
CLEANUP_ADAPTER_KINDS = frozenset(
    {"archive", "harness", "backend", "git", "callsign", "process", "lease", "retain", "issue"}
)
FINAL_ACTION_ADAPTERS = {
    "session_exit": "harness",
    "endpoint_close": "backend",
    "worktree_remove": "git",
    "branch_delete": "git",
    "callsign_release": "callsign",
    "issue_close": "issue",
}
RECOVERABLE_EXECUTION_REFUSALS = frozenset(
    {"cleanup_adapter_failed", "cleanup_verification_failed"}
)


@dataclass(frozen=True)
class CleanupFaultEvent:
    phase: str
    action_kind: str

POLICY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "analysis": ("identity.exact", "endpoint.terminal_or_idle"),
    "local_git": (
        "identity.exact",
        "endpoint.terminal_or_idle",
        "git.exact_registration",
        "git.clean",
        "git.no_unpublished",
    ),
    "pr_ci": (
        "identity.exact",
        "endpoint.terminal_or_idle",
        "git.exact_registration",
        "git.clean",
        "git.no_unpublished",
        "publication.exact_head",
        "publication.ci_green",
        "publication.integrated",
    ),
    "deployed_service": (
        "identity.exact",
        "endpoint.terminal_or_idle",
        "git.exact_registration",
        "git.clean",
        "git.no_unpublished",
        "publication.exact_head",
        "publication.ci_green",
        "publication.integrated",
        "deployment.exact_revision",
        "deployment.smoke_passed",
    ),
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def cleanup_action_digest(action: Mapping[str, Any]) -> str:
    """Hash only the immutable action plan, never mutable state or receipts."""

    immutable = {
        key: action.get(key)
        for key in (
            "action_id",
            "operation_id",
            "ordinal",
            "action_kind",
            "adapter_kind",
            "resource_id",
            "expected_identity",
            "intended_state",
        )
    }
    return hashlib.sha256(_stable_json(immutable).encode("utf-8")).hexdigest()


def _proof(proof: Mapping[str, Any], dotted: str) -> Any:
    value: Any = proof
    for component in dotted.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def select_cleanup_policy(task_class: str, disposition: str) -> dict[str, Any]:
    if task_class not in TASK_CLASSES:
        raise StorageRefusal("cleanup_class_unsupported", "task class has no cleanup policy")
    if disposition not in DISPOSITIONS:
        raise StorageRefusal("cleanup_disposition_unsupported", "task disposition has no cleanup policy")
    requirements = list(POLICY_REQUIREMENTS[task_class])
    if disposition in {"rejected", "cancelled"}:
        requirements = [item for item in requirements if not item.startswith(("publication.", "deployment."))]
        requirements.append("decision.explicit")
    elif disposition == "failed":
        requirements = [item for item in requirements if not item.startswith(("publication.", "deployment."))]
        requirements.append("failure.preserved")
    return {
        "policy": f"{task_class}:{disposition}:v1",
        "task_class": task_class,
        "disposition": disposition,
        "requirements": requirements,
    }


def require_cleanup_task_disposition(task_state: str, disposition: str) -> None:
    """Fail closed unless one canonical task state permits the requested disposition."""

    if disposition not in CLEANUP_DISPOSITIONS_BY_TASK_STATE.get(task_state, frozenset()):
        raise StorageRefusal(
            "cleanup_owner_refused",
            "cleanup owner is not the exact terminal task owner",
        )


@dataclass(frozen=True)
class ResourceRegistration:
    resource_id: str
    task_id: str
    owner_id: str
    owner_role: str
    resource_type: str
    lifetime: str
    expected_identity: Mapping[str, Any]
    cleanup_action: str
    adapter_kind: str
    applicable: bool
    applicability_reason: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResourceRegistration":
        if not isinstance(value, Mapping):
            raise StorageRefusal("resource_invalid", "task resource registration must be an object")
        required = (
            "resource_id",
            "task_id",
            "owner_id",
            "owner_role",
            "resource_type",
            "lifetime",
            "expected_identity",
            "cleanup_action",
            "adapter_kind",
            "applicable",
            "applicability_reason",
        )
        if any(key not in value for key in required):
            raise StorageRefusal("resource_invalid", "task resource registration is incomplete")
        if not isinstance(value["lifetime"], str) or value["lifetime"] not in RESOURCE_LIFETIMES:
            raise StorageRefusal("resource_invalid", "task resource lifetime is unsupported")
        string_fields = (
            "resource_id",
            "task_id",
            "owner_id",
            "owner_role",
            "resource_type",
            "cleanup_action",
            "adapter_kind",
            "applicability_reason",
        )
        if any(
            not isinstance(value[field], str)
            or not value[field]
            or value[field].strip() != value[field]
            for field in string_fields
        ):
            raise StorageRefusal("resource_invalid", "task resource string fields are not exact")
        if value["owner_role"] == "shotcaller":
            raise StorageRefusal("resource_owner_refused", "Shotcaller-owned resources are ineligible for task cleanup")
        if value["owner_role"] not in {"champion", "hidden-worker", "task"}:
            raise StorageRefusal("resource_invalid", "task resource owner role is unsupported")
        if not isinstance(value["applicable"], bool):
            raise StorageRefusal("resource_invalid", "task resource applicability must be Boolean")
        if not isinstance(value["expected_identity"], Mapping) or not value["expected_identity"]:
            raise StorageRefusal("resource_invalid", "task resource expected identity is missing")
        if value["lifetime"] == "shared_lease" and value["cleanup_action"] != "release_lease":
            raise StorageRefusal("shared_resource_refused", "shared resources permit lease release only")
        if value["lifetime"] == "shared_lease" and value["adapter_kind"] != "lease":
            raise StorageRefusal("shared_resource_refused", "shared leases require the exact lease adapter")
        if value["lifetime"] == "persistent_retain" and value["cleanup_action"] != "retain":
            raise StorageRefusal("persistent_resource_refused", "persistent resources must be retained")
        if value["lifetime"] == "persistent_retain" and value["adapter_kind"] != "retain":
            raise StorageRefusal("persistent_resource_refused", "persistent resources require retain policy")
        return cls(**{key: value[key] for key in required})

    def as_record(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "owner_role": self.owner_role,
            "resource_type": self.resource_type,
            "lifetime": self.lifetime,
            "expected_identity": dict(self.expected_identity),
            "cleanup_action": self.cleanup_action,
            "adapter_kind": self.adapter_kind,
            "applicable": self.applicable,
            "applicability_reason": self.applicability_reason,
        }


class CleanupStorage(Protocol):
    def unresolved_repository_publications(self, task_id: str) -> list[dict[str, Any]]: ...
    def register_task_resource(self, resource: Mapping[str, Any], at: str) -> dict[str, Any]: ...
    def task_resources(self, task_id: str) -> list[dict[str, Any]]: ...
    def plan_cleanup(self, plan: Mapping[str, Any]) -> dict[str, Any]: ...
    def cleanup_operation(self, operation_id: str) -> Optional[dict[str, Any]]: ...
    def claim_cleanup_operation(
        self, operation_id: str, expected_fence: int, executor_id: str, leased_until: str, at: str
    ) -> dict[str, Any]: ...
    def record_cleanup_action_receipt(
        self,
        action_id: str,
        operation_id: str,
        fence: int,
        outcome: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        adapter_receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...
    def finalize_cleanup(self, operation_id: str, fence: int, at: str) -> dict[str, Any]: ...
    def block_cleanup_operation(
        self,
        operation_id: str,
        fence: int,
        action_id: Optional[str],
        refusal_code: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...
    def rollover_cleanup_target(self, operation_id: str) -> Optional[dict[str, Any]]: ...


class CleanupActionAdapter(Protocol):
    kind: str

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def intended(self, action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool: ...


class CleanupAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, CleanupActionAdapter] = {}

    def register(self, adapter: CleanupActionAdapter) -> None:
        if adapter.kind in self._adapters:
            raise StorageRefusal("cleanup_adapter_conflict", "cleanup adapter is already registered")
        self._adapters[adapter.kind] = adapter

    def get(self, kind: str) -> CleanupActionAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise StorageRefusal("cleanup_adapter_unknown", f"cleanup adapter is not registered: {kind}") from exc


class CleanupPlanner:
    def __init__(
        self,
        storage: CleanupStorage,
        adapter_kinds: frozenset[str] = CLEANUP_ADAPTER_KINDS,
    ) -> None:
        self.storage = storage
        self.adapter_kinds = frozenset(adapter_kinds)
        if "archive" not in self.adapter_kinds or any(
            not isinstance(kind, str) or not kind for kind in self.adapter_kinds
        ):
            raise StorageRefusal("cleanup_adapter_unknown", "cleanup adapter declarations are invalid")

    def _validate_resource_adapter(self, resource: ResourceRegistration) -> None:
        if resource.adapter_kind not in self.adapter_kinds:
            raise StorageRefusal(
                "cleanup_adapter_unknown",
                f"cleanup adapter is not declared: {resource.adapter_kind}",
            )

    def register_resource(self, value: Mapping[str, Any], at: str) -> dict[str, Any]:
        resource = ResourceRegistration.from_mapping(value)
        self._validate_resource_adapter(resource)
        return self.storage.register_task_resource(resource.as_record(), at)

    def plan(self, manifest: Mapping[str, Any], *, operation_id: str, at: str) -> dict[str, Any]:
        task_id = manifest.get("task_id")
        owner = manifest.get("owner")
        identity = manifest.get("identity")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(owner, Mapping)
            or not isinstance(owner.get("id"), str)
            or not owner.get("id")
            or not isinstance(identity, Mapping)
            or not identity
        ):
            raise StorageRefusal("cleanup_manifest_invalid", "cleanup task identity is incomplete")
        rollover: Optional[dict[str, Any]] = None
        if owner.get("role") == "shotcaller":
            requested = manifest.get("rollover")
            if not isinstance(requested, Mapping) or set(requested) != {
                "operation_id",
                "expected_version",
            }:
                raise StorageRefusal(
                    "cleanup_owner_refused",
                    "Shotcaller cleanup is permitted only for one exact switched rollover predecessor",
                )
            observed = self.storage.rollover_cleanup_target(str(requested["operation_id"]))
            if (
                observed is None
                or observed.get("state") != "switched"
                or observed.get("predecessor_agent_id") != owner.get("id")
                or observed.get("version") != requested.get("expected_version")
            ):
                raise StorageRefusal(
                    "cleanup_owner_refused",
                    "Shotcaller cleanup does not match an exact switched rollover predecessor",
                )
            rollover = dict(observed)
        elif owner.get("role") not in {"champion", "hidden-worker", "task"} or owner.get("persistent") is not False:
            raise StorageRefusal("cleanup_owner_refused", "cleanup owner identity is not an eligible task owner")
        if manifest.get("pending_decisions_clear") is not True:
            raise StorageRefusal("pending_decision", "cleanup has a pending owner decision")
        unresolved = self.storage.unresolved_repository_publications(task_id)
        if unresolved:
            raise StorageRefusal(
                "repository_publication_unresolved",
                "required repository artifact publication is not merged",
            )
        policy = select_cleanup_policy(str(manifest.get("task_class")), str(manifest.get("disposition")))
        proof = manifest.get("proof")
        if not isinstance(proof, Mapping):
            raise StorageRefusal("cleanup_proof_missing", "cleanup proof object is missing")
        missing = [requirement for requirement in policy["requirements"] if _proof(proof, requirement) is not True]
        if missing:
            raise StorageRefusal("cleanup_proof_missing", "cleanup proof is incomplete: " + ", ".join(missing))
        legacy = manifest.get("legacy_identity")
        if legacy is not None and legacy != manifest.get("identity"):
            raise StorageRefusal("legacy_identity_mismatch", "legacy path pointer conflicts with exact task identity")

        resources_value = manifest.get("resources", [])
        if not isinstance(resources_value, Sequence) or isinstance(resources_value, (str, bytes)):
            raise StorageRefusal("resource_invalid", "cleanup resources must be a list")
        if any(not isinstance(item, Mapping) for item in resources_value):
            raise StorageRefusal("resource_invalid", "cleanup resource entries must be objects")
        resources = [ResourceRegistration.from_mapping(item) for item in resources_value]
        resource_ids = [resource.resource_id for resource in resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise StorageRefusal("resource_invalid", "cleanup resource ids must be unique")
        for resource in resources:
            self._validate_resource_adapter(resource)
        registered = [
            ResourceRegistration.from_mapping(item)
            for item in self.storage.task_resources(task_id)
        ]
        for resource in registered:
            self._validate_resource_adapter(resource)
        manifest_resources = {resource.resource_id: resource for resource in resources}
        for resource in registered:
            declared = manifest_resources.get(resource.resource_id)
            if declared is None:
                raise StorageRefusal(
                    "resource_proof_missing",
                    "active registered task resource is missing from cleanup proof",
                )
            if declared != resource:
                raise StorageRefusal(
                    "resource_identity_mismatch",
                    "cleanup resource conflicts with its canonical registration",
                )
        for resource in resources:
            if resource.task_id != task_id or resource.owner_id != owner.get("id"):
                raise StorageRefusal("resource_identity_mismatch", "task resource owner or task identity conflicts")
            if not resource.applicable:
                raise StorageRefusal("resource_not_applicable", "task resource is not applicable to this cleanup")

        actions: list[dict[str, Any]] = [
            {
                "action_id": f"{operation_id}:000",
                "ordinal": 0,
                "action_kind": "archive_identity_evidence",
                "adapter_kind": "archive",
                "resource_id": None,
                "expected_identity": manifest.get("identity", {}),
                "intended_state": {
                    "archived": True,
                    "task_id": task_id,
                    "owner": dict(owner),
                    "identity": manifest.get("identity", {}),
                    "policy": policy,
                    "proof": proof,
                    "resources": [resource.as_record() for resource in resources],
                    "pending_decisions_clear": True,
                    "rollover": rollover,
                },
            }
        ]
        ordinal = 1
        for resource in sorted(resources, key=lambda item: item.resource_id):
            if resource.lifetime == "persistent_retain":
                continue
            actions.append(
                {
                    "action_id": f"{operation_id}:{ordinal:03d}",
                    "ordinal": ordinal,
                    "action_kind": resource.cleanup_action,
                    "adapter_kind": resource.adapter_kind,
                    "resource_id": resource.resource_id,
                    "expected_identity": dict(resource.expected_identity),
                    "intended_state": {"completed": True},
                }
            )
            ordinal += 1
        continuation_archive = manifest.get("continuation_archive")
        if continuation_archive is not None and (
            owner.get("role") != "champion"
            or policy["disposition"] != "completed"
            or policy["task_class"] not in {"pr_ci", "deployed_service"}
        ):
            raise StorageRefusal(
                "issue_close_ineligible",
                "issue-coupled close requires one completed published Champion task",
            )
        if continuation_archive is not None and (
            not isinstance(continuation_archive, Mapping)
            or not isinstance(continuation_archive.get("acceptance"), Mapping)
            or continuation_archive["acceptance"].get("required_gates_complete") is not True
            or continuation_archive.get("cleanup_evidence") != proof
        ):
            raise StorageRefusal(
                "thread_archive_invalid",
                "issue-coupled close requires exact completed acceptance and cleanup evidence",
            )
        final_actions = manifest.get("final_actions", [])
        if not isinstance(final_actions, list):
            raise StorageRefusal("cleanup_manifest_invalid", "cleanup final actions must be a list")
        allowed_final = set(FINAL_ACTION_ADAPTERS)
        required_final = ["session_exit", "endpoint_close"]
        if policy["task_class"] != "analysis":
            required_final.extend(("worktree_remove", "branch_delete"))
        required_final.append("callsign_release")
        if continuation_archive is not None:
            required_final.append("issue_close")
        observed_final = [item.get("action_kind") for item in final_actions if isinstance(item, Mapping)]
        if observed_final != required_final:
            raise StorageRefusal(
                "cleanup_action_mismatch",
                "cleanup final actions do not match the selected policy",
            )
        for item in final_actions:
            if not isinstance(item, Mapping) or item.get("action_kind") not in allowed_final:
                raise StorageRefusal("cleanup_action_unsupported", "cleanup final action is unsupported")
            adapter_kind = item.get("adapter_kind")
            expected_identity = item.get("expected_identity")
            intended_state = item.get("intended_state")
            if not isinstance(adapter_kind, str) or adapter_kind not in self.adapter_kinds:
                raise StorageRefusal("cleanup_adapter_unknown", "cleanup final action adapter is not declared")
            if adapter_kind != FINAL_ACTION_ADAPTERS[item["action_kind"]]:
                raise StorageRefusal(
                    "cleanup_adapter_mismatch",
                    "cleanup final action uses the wrong adapter category",
                )
            if not isinstance(expected_identity, Mapping) or not expected_identity:
                raise StorageRefusal("cleanup_manifest_invalid", "cleanup final expected identity is missing")
            if not isinstance(intended_state, Mapping) or not intended_state:
                raise StorageRefusal("cleanup_manifest_invalid", "cleanup final intended state is missing")
            actions.append(
                {
                    "action_id": f"{operation_id}:{ordinal:03d}",
                    "ordinal": ordinal,
                    "action_kind": item["action_kind"],
                    "adapter_kind": adapter_kind,
                    "resource_id": item.get("resource_id"),
                    "expected_identity": dict(expected_identity),
                    "intended_state": dict(intended_state),
                }
            )
            ordinal += 1
        ordered = [action["action_kind"] for action in actions]
        if "archive_identity_evidence" not in ordered or ordered[0] != "archive_identity_evidence":
            raise StorageRefusal("cleanup_order_invalid", "identity archive must be the first cleanup action")
        release_positions = [ordered.index(name) for name in ("endpoint_close", "worktree_remove", "branch_delete", "callsign_release") if name in ordered]
        if release_positions != sorted(release_positions):
            raise StorageRefusal("cleanup_order_invalid", "cleanup release actions are out of order")
        digest = hashlib.sha256(_stable_json({"policy": policy, "actions": actions}).encode("utf-8")).hexdigest()
        expected_cleanup_version = manifest.get("expected_cleanup_version", 0)
        if (
            isinstance(expected_cleanup_version, bool)
            or not isinstance(expected_cleanup_version, int)
            or expected_cleanup_version < 0
        ):
            raise StorageRefusal("cleanup_manifest_invalid", "cleanup expected version is invalid")
        return self.storage.plan_cleanup(
            {
                "operation_id": operation_id,
                "task_id": task_id,
                "owner_id": owner.get("id"),
                "task_class": policy["task_class"],
                "disposition": policy["disposition"],
                "required_policy": policy["policy"],
                "expected_cleanup_version": expected_cleanup_version,
                "plan_digest": digest,
                "resources": [resource.as_record() for resource in resources],
                "actions": actions,
                "continuation_archive": continuation_archive,
                "proof": proof,
                "at": at,
            }
        )


class CleanupExecutor:
    """Perform one action at a time and persist an immutable receipt after each."""

    def __init__(self, storage: CleanupStorage, adapters: CleanupAdapterRegistry) -> None:
        self.storage = storage
        self.adapters = adapters

    def execute(
        self,
        operation_id: str,
        *,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
        fault: Optional[Any] = None,
    ) -> dict[str, Any]:
        claimed = self.storage.claim_cleanup_operation(
            operation_id, expected_fence, executor_id, leased_until, at
        )
        fence = int(claimed["fence"])
        current_action_id: Optional[str] = None
        try:
            operation = self.storage.cleanup_operation(operation_id)
            if operation is None:
                raise StorageRefusal("cleanup_operation_unknown", "cleanup operation disappeared")
            actions = operation.get("actions")
            if not isinstance(actions, list):
                raise StorageRefusal("cleanup_operation_invalid", "cleanup action list is invalid")
            prepared: list[tuple[Mapping[str, Any], CleanupActionAdapter]] = []
            for action in actions:
                current_action_id = str(action["action_id"])
                adapter = self.adapters.get(str(action["adapter_kind"]))
                observation = dict(adapter.inspect(action))
                if action["state"] == "completed":
                    if not adapter.intended(action, observation):
                        raise StorageRefusal(
                            "cleanup_completed_action_changed",
                            "a completed cleanup action no longer has its verified intended state",
                        )
                    continue
                if not adapter.intended(action, observation) and observation != action["expected_identity"]:
                    raise StorageRefusal(
                        "cleanup_identity_mismatch",
                        "cleanup preflight found stale or ambiguous action identity",
                    )
                prepared.append((action, adapter))
            for action, adapter in prepared:
                current_action_id = str(action["action_id"])
                # Preflight proves the whole plan is initially safe; this second
                # observation is intentional because earlier effects may change
                # external reality before a later action runs.
                before = dict(adapter.inspect(action))
                if adapter.intended(action, before):
                    outcome = "already_applied"
                    after = before
                    receipt = {"reconciled": True}
                else:
                    if before != action["expected_identity"]:
                        raise StorageRefusal("cleanup_identity_mismatch", "cleanup action identity is stale or ambiguous")
                    receipt = dict(adapter.apply(action))
                    if fault is not None:
                        fault(
                            CleanupFaultEvent(
                                phase="after_external_action",
                                action_kind=str(action["action_kind"]),
                            )
                        )
                    after = dict(adapter.inspect(action))
                    if not adapter.intended(action, after):
                        raise StorageRefusal("cleanup_verification_failed", "cleanup action effect was not verified")
                    outcome = "applied"
                self.storage.record_cleanup_action_receipt(
                    str(action["action_id"]),
                    operation_id,
                    fence,
                    outcome,
                    before,
                    after,
                    receipt,
                    at,
                )
            current_action_id = None
            return self.storage.finalize_cleanup(operation_id, fence, at)
        except StorageRefusal as exc:
            if exc.retryable:
                raise
            if exc.code in RECOVERABLE_EXECUTION_REFUSALS:
                raise StorageRefusal(
                    exc.code,
                    str(exc),
                    retryable=True,
                ) from exc
            else:
                self.storage.block_cleanup_operation(
                    operation_id,
                    fence,
                    current_action_id,
                    exc.code,
                    {
                        "executor_id": executor_id,
                        "refusal_code": exc.code,
                        "retryable": False,
                    },
                    at,
                )
                raise
