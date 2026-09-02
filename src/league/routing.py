"""Evidence-gated, provider-neutral model and effort routing."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .storage_types import StorageRefusal


COORDINATOR = "COORDINATOR"
WORKER_FAST = "WORKER_FAST"
WORKER_STRONG = "WORKER_STRONG"
TIERS = frozenset({COORDINATOR, WORKER_FAST, WORKER_STRONG})
REASON_CODES = frozenset(
    {
        "explicit_override",
        "operator_override",
        "coordination_baseline",
        "reliability_baseline",
        "evidence_downgrade",
        "provider_capability_fallback",
        "failure_escalation",
        "escalation_exhausted",
    }
)
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


def _time(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("routing_config_invalid", f"{label} must be RFC3339") from exc
    if result.tzinfo is None:
        raise StorageRefusal("routing_config_invalid", f"{label} must include an offset")
    return result


@dataclass(frozen=True)
class RoutingSignals:
    coordination: bool = False
    bounded_checkable: bool = False
    ambiguity: bool = False
    high_impact: bool = False
    weak_verification: bool = False

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "RoutingSignals":
        if set(value) - set(cls.__dataclass_fields__) or any(
            not isinstance(item, bool) for item in value.values()
        ):
            raise StorageRefusal("routing_signals_invalid", "semantic routing signals are invalid")
        return cls(**value)

    def as_record(self) -> dict[str, bool]:
        return {
            "coordination": self.coordination,
            "bounded_checkable": self.bounded_checkable,
            "ambiguity": self.ambiguity,
            "high_impact": self.high_impact,
            "weak_verification": self.weak_verification,
        }


@dataclass(frozen=True)
class RoutingChoice:
    decision_id: str
    subject_kind: str
    subject_id: str
    role: str
    tier: str
    provider: str
    provider_config_version: str
    model: str
    effort: str
    reason: str
    reason_code: str
    policy_version: str
    explicit_provider: bool
    explicit_model: bool
    explicit_effort: bool
    operator_override_id: Optional[str]
    fallback_from_provider: Optional[str]
    required_capabilities: tuple[str, ...]
    signals: Mapping[str, bool]
    state: str
    escalation_count: int
    prior_decision_id: Optional[str]
    failure_class: Optional[str]
    chosen_at: str

    def as_record(self) -> dict[str, Any]:
        value = dict(self.__dict__)
        value["required_capabilities_json"] = json.dumps(
            list(value.pop("required_capabilities")), separators=(",", ":")
        )
        value["signals_json"] = json.dumps(
            value.pop("signals"), sort_keys=True, separators=(",", ":")
        )
        return value


def validate_routing_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema") != 3
        or not isinstance(value.get("policy_version"), str)
        or not value["policy_version"]
        or not isinstance(value.get("default_provider"), str)
        or not value["default_provider"]
    ):
        raise StorageRefusal("routing_config_invalid", "versioned routing configuration is incomplete")
    providers = value.get("providers")
    if not isinstance(providers, dict) or value["default_provider"] not in providers:
        raise StorageRefusal("routing_config_invalid", "routing providers are incomplete")
    for provider_name, provider in providers.items():
        if (
            not isinstance(provider_name, str)
            or not provider_name
            or not isinstance(provider, dict)
            or not isinstance(provider.get("config_version"), str)
            or not provider["config_version"]
            or not isinstance(provider.get("capabilities"), list)
            or any(not isinstance(item, str) or not item for item in provider["capabilities"])
            or len(set(provider["capabilities"])) != len(provider["capabilities"])
            or not isinstance(provider.get("tiers"), dict)
        ):
            raise StorageRefusal("routing_config_invalid", "provider configuration is invalid")
        for tier_name in TIERS:
            tier = provider["tiers"].get(tier_name)
            if (
                not isinstance(tier, dict)
                or not isinstance(tier.get("model"), str)
                or not tier["model"]
                or not isinstance(tier.get("effort"), str)
                or not tier["effort"]
            ):
                raise StorageRefusal(
                    "routing_config_invalid", f"routing tier is incomplete: {provider_name}/{tier_name}"
                )
    order = value.get("provider_order", list(providers))
    if (
        not isinstance(order, list)
        or set(order) != set(providers)
        or len(order) != len(set(order))
    ):
        raise StorageRefusal("routing_config_invalid", "provider order must list each provider once")
    policy = value.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("quality_baseline") != WORKER_STRONG
        or policy.get("safe_boundary_escalations") != 1
        or isinstance(policy.get("safe_boundary_escalations"), bool)
    ):
        raise StorageRefusal("routing_config_invalid", "routing policy must preserve the reliability baseline")
    evaluations = value.get("evaluations", {})
    if not isinstance(evaluations, Mapping):
        raise StorageRefusal("routing_config_invalid", "routing evaluations must be an object")
    for key, evidence in evaluations.items():
        if not isinstance(key, str) or not isinstance(evidence, Mapping):
            raise StorageRefusal("routing_config_invalid", "routing evaluation entry is invalid")
        fields = (
            "representative_tasks",
            "task_success_rate",
            "correction_rate",
            "minimum_representative_tasks",
            "minimum_task_success_rate",
            "maximum_correction_rate",
        )
        if any(field not in evidence for field in fields):
            raise StorageRefusal("routing_config_invalid", "routing evaluation thresholds are incomplete")
        if (
            isinstance(evidence["representative_tasks"], bool)
            or not isinstance(evidence["representative_tasks"], int)
            or isinstance(evidence["minimum_representative_tasks"], bool)
            or not isinstance(evidence["minimum_representative_tasks"], int)
            or any(
                isinstance(evidence[field], bool)
                or not isinstance(evidence[field], (int, float))
                or not 0 <= float(evidence[field]) <= 1
                for field in (
                    "task_success_rate",
                    "correction_rate",
                    "minimum_task_success_rate",
                    "maximum_correction_rate",
                )
            )
        ):
            raise StorageRefusal("routing_config_invalid", "routing evaluation values are invalid")
    overrides = value.get("operator_overrides", [])
    if not isinstance(overrides, list) or len(overrides) > 32:
        raise StorageRefusal(
            "routing_config_invalid", "operator overrides must be a bounded list"
        )
    override_ids: set[str] = set()
    for override in overrides:
        if (
            not isinstance(override, Mapping)
            or not all(
                isinstance(override.get(field), str) and override[field]
                for field in ("id", "provider", "model", "effort", "starts_at", "expires_at")
            )
            or override["provider"] not in providers
            or not isinstance(override.get("roles"), list)
            or any(not isinstance(role, str) or not role for role in override["roles"])
            or override["id"] in override_ids
            or _time(override["expires_at"], "operator override expiry")
            <= _time(override["starts_at"], "operator override start")
        ):
            raise StorageRefusal("routing_config_invalid", "operator override is invalid")
        override_ids.add(str(override["id"]))
    return dict(value)


def load_routing_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("routing_config_invalid", "routing configuration could not be read") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("routing_config_invalid", "routing configuration must be an object")
    return validate_routing_config(value)


def migrate_routing_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the retained schema-1/2 tier file into a safe schema-3 policy."""

    if not isinstance(value, Mapping):
        raise StorageRefusal(
            "routing_config_invalid", "legacy routing configuration must be an object"
        )
    if value.get("schema") == 3:
        return validate_routing_config(dict(value))
    legacy_schema = value.get("schema")
    legacy_fields = set(value)
    if (
        isinstance(legacy_schema, bool)
        or legacy_schema not in {1, 2}
        or not {"schema", "tiers"}.issubset(legacy_fields)
        or not legacy_fields.issubset({"schema", "tiers", "evaluations", "policy"})
    ):
        raise StorageRefusal(
            "routing_migration_unsupported",
            "only the retained schema-1/2 routing policy can be migrated",
        )
    tiers = value.get("tiers")
    policy = value.get(
        "policy",
        {
            "quality_baseline": WORKER_STRONG,
            "safe_boundary_escalations": 1,
        },
    )
    if (
        not isinstance(tiers, Mapping)
        or set(tiers) != TIERS
        or not isinstance(policy, Mapping)
        or set(policy) != {"quality_baseline", "safe_boundary_escalations"}
        or policy.get("quality_baseline") != WORKER_STRONG
        or isinstance(policy.get("safe_boundary_escalations"), bool)
        or policy.get("safe_boundary_escalations") != 1
    ):
        raise StorageRefusal(
            "routing_migration_unsafe",
            "legacy routing policy does not preserve the strongest baseline",
        )
    if "evaluations" in value:
        evaluations = value["evaluations"]
        if not isinstance(evaluations, Mapping) or not set(evaluations).issubset(
            {WORKER_FAST}
        ):
            raise StorageRefusal(
                "routing_migration_unsafe", "legacy routing evaluations are malformed"
            )
        if WORKER_FAST in evaluations:
            fast_evaluation = evaluations[WORKER_FAST]
            representative_tasks = (
                fast_evaluation.get("representative_tasks")
                if isinstance(fast_evaluation, Mapping)
                else None
            )
            if (
                not isinstance(fast_evaluation, Mapping)
                or set(fast_evaluation) != {"approved", "representative_tasks"}
                or not isinstance(fast_evaluation.get("approved"), bool)
                or isinstance(representative_tasks, bool)
                or not isinstance(representative_tasks, int)
                or representative_tasks < 0
            ):
                raise StorageRefusal(
                    "routing_migration_unsafe",
                    "legacy routing evaluations are malformed",
                )
    normalized_tiers: dict[str, dict[str, str]] = {}
    for tier in sorted(TIERS):
        entry = tiers.get(tier)
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"model", "effort"}
            or not isinstance(entry.get("model"), str)
            or not entry["model"]
            or not isinstance(entry.get("effort"), str)
            or not entry["effort"]
        ):
            raise StorageRefusal(
                "routing_migration_unsafe", "legacy routing tier is incomplete"
            )
        normalized_tiers[tier] = {
            "model": str(entry["model"]),
            "effort": str(entry["effort"]),
        }
    if "luna" in normalized_tiers[WORKER_STRONG]["model"].casefold():
        raise StorageRefusal(
            "routing_migration_unsafe",
            "legacy strongest routing baseline cannot silently select Luna",
        )
    migrated = {
        "schema": 3,
        "policy_version": "league.model-routing.migrated-v3.1",
        "default_provider": "openai",
        "provider_order": ["openai"],
        "providers": {
            "openai": {
                "config_version": "openai.legacy-migration-v3.1",
                "capabilities": ["reasoning", "tools"],
                "tiers": normalized_tiers,
            }
        },
        # Legacy approval did not carry the rate/correction evidence required by
        # schema 3.  Preserve it only as a fail-closed, not-approved baseline.
        "evaluations": {
            "openai/WORKER_FAST": {
                "representative_tasks": 0,
                "task_success_rate": 0.0,
                "correction_rate": 1.0,
                "minimum_representative_tasks": 20,
                "minimum_task_success_rate": 0.95,
                "maximum_correction_rate": 0.05,
            }
        },
        "policy": {
            "quality_baseline": WORKER_STRONG,
            "safe_boundary_escalations": 1,
        },
        "operator_overrides": [],
    }
    return validate_routing_config(migrated)


def _routing_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_regular(path: Path, *, required: bool = True) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise StorageRefusal(
                "routing_install_invalid", "routing policy file does not exist"
            )
        return None
    except OSError as exc:
        raise StorageRefusal(
            "routing_install_invalid", "routing policy file cannot be inspected"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise StorageRefusal(
            "routing_install_invalid", "routing policy must be a regular file"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StorageRefusal(
            "routing_install_invalid", "routing policy file cannot be read"
        ) from exc
    if len(payload) > 1_048_576:
        raise StorageRefusal(
            "routing_install_invalid", "routing policy exceeds the bounded input size"
        )
    return payload


def _absolute_install_path(path: Path, label: str) -> Path:
    if not path.is_absolute() or path == Path("/"):
        raise StorageRefusal(
            "routing_install_invalid", f"{label} must be an exact absolute file path"
        )
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise StorageRefusal(
            "routing_install_invalid", f"{label} parent must be an existing directory"
        )
    return path


def _atomic_replace(path: Path, payload: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.league-{secrets.token_hex(8)}")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageRefusal(
            "routing_install_failed", "routing policy atomic write failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def install_migrated_routing_config(
    source: Path, destination: Path, backup: Path
) -> dict[str, Any]:
    """Back up and atomically replace one exact installed legacy policy."""

    destination = _absolute_install_path(destination, "routing destination")
    backup = _absolute_install_path(backup, "routing backup")
    if destination == backup:
        raise StorageRefusal(
            "routing_install_invalid", "routing destination and backup must differ"
        )
    source_payload = _read_regular(source)
    destination_payload = _read_regular(destination)
    assert source_payload is not None and destination_payload is not None
    if source.resolve() != destination.resolve() and source_payload != destination_payload:
        raise StorageRefusal(
            "routing_install_conflict", "routing source does not match the installed policy"
        )
    try:
        source_value = json.loads(source_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "routing_config_invalid", "routing configuration could not be read"
        ) from exc
    if not isinstance(source_value, dict):
        raise StorageRefusal(
            "routing_config_invalid", "routing configuration must be an object"
        )
    migrated = migrate_routing_config(source_value)
    installed_payload = _routing_bytes(migrated)
    prior_backup = _read_regular(backup, required=False)
    source_schema = source_value.get("schema")
    if destination_payload == installed_payload:
        if prior_backup is not None and source_schema == 3:
            try:
                prior_value = json.loads(prior_backup.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise StorageRefusal(
                    "routing_install_conflict", "routing backup is malformed"
                ) from exc
            if not isinstance(prior_value, dict) or prior_value.get("schema") not in {1, 2}:
                raise StorageRefusal(
                    "routing_install_conflict", "routing backup is not the retained legacy policy"
                )
        return {
            "schema": "league.routing-config-install.v1",
            "state": "installed" if prior_backup is not None else "already_valid",
            "from_schema": (
                int(json.loads(prior_backup.decode("utf-8"))["schema"])
                if prior_backup is not None
                else 3
            ),
            "to_schema": 3,
            "installed_sha256": hashlib.sha256(installed_payload).hexdigest(),
            "backup_sha256": (
                hashlib.sha256(prior_backup).hexdigest()
                if prior_backup is not None
                else None
            ),
            "rollback_ready": prior_backup is not None,
            "idempotent": True,
        }
    if source_schema not in {1, 2}:
        raise StorageRefusal(
            "routing_install_conflict", "installed schema-3 routing policy differs"
        )
    if prior_backup is None:
        _atomic_replace(backup, destination_payload, stat.S_IMODE(destination.lstat().st_mode))
        prior_backup = destination_payload
    elif prior_backup != destination_payload:
        raise StorageRefusal(
            "routing_install_conflict", "routing backup does not match the installed legacy policy"
        )
    mode = stat.S_IMODE(destination.lstat().st_mode)
    _atomic_replace(destination, installed_payload, mode)
    if _read_regular(destination) != installed_payload or _read_regular(backup) != prior_backup:
        raise StorageRefusal(
            "routing_install_unverified", "routing policy or backup did not verify"
        )
    return {
        "schema": "league.routing-config-install.v1",
        "state": "installed",
        "from_schema": int(source_schema),
        "to_schema": 3,
        "installed_sha256": hashlib.sha256(installed_payload).hexdigest(),
        "backup_sha256": hashlib.sha256(prior_backup).hexdigest(),
        "rollback_ready": True,
        "idempotent": False,
    }


def rollback_routing_config(
    destination: Path,
    backup: Path,
    *,
    expected_installed_sha256: str,
    expected_backup_sha256: str,
) -> dict[str, Any]:
    destination = _absolute_install_path(destination, "routing destination")
    backup = _absolute_install_path(backup, "routing backup")
    destination_payload = _read_regular(destination)
    backup_payload = _read_regular(backup)
    assert destination_payload is not None and backup_payload is not None
    installed_digest = hashlib.sha256(destination_payload).hexdigest()
    backup_digest = hashlib.sha256(backup_payload).hexdigest()
    if backup_digest != expected_backup_sha256:
        raise StorageRefusal(
            "routing_rollback_conflict", "routing backup does not match its exact receipt"
        )
    if installed_digest == backup_digest:
        return {
            "schema": "league.routing-config-rollback.v1",
            "state": "rolled_back",
            "restored_sha256": backup_digest,
            "idempotent": True,
        }
    if installed_digest != expected_installed_sha256:
        raise StorageRefusal(
            "routing_rollback_conflict", "installed routing policy changed after migration"
        )
    mode = stat.S_IMODE(destination.lstat().st_mode)
    _atomic_replace(destination, backup_payload, mode)
    if _read_regular(destination) != backup_payload:
        raise StorageRefusal(
            "routing_rollback_unverified", "routing policy rollback did not verify"
        )
    return {
        "schema": "league.routing-config-rollback.v1",
        "state": "rolled_back",
        "restored_sha256": backup_digest,
        "idempotent": False,
    }


class ModelRouter:
    """Map semantic evidence to versioned provider configuration only."""

    def __init__(self, config: Mapping[str, Any], storage: RoutingStorage) -> None:
        self.config = validate_routing_config(config)
        self.storage = storage

    def _tier(self, signals: RoutingSignals, provider: str) -> tuple[str, str, str]:
        if signals.coordination:
            return COORDINATOR, "coordination_baseline", "Coordination uses the configured reliable coordinator tier."
        unsafe = signals.ambiguity or signals.high_impact or signals.weak_verification
        if not signals.bounded_checkable or unsafe:
            return WORKER_STRONG, "reliability_baseline", "Risk or weak verification keeps the strongest reliability baseline."
        evidence = self.config.get("evaluations", {}).get(f"{provider}/{WORKER_FAST}")
        if isinstance(evidence, Mapping) and (
            evidence["representative_tasks"] >= evidence["minimum_representative_tasks"]
            and evidence["task_success_rate"] >= evidence["minimum_task_success_rate"]
            and evidence["correction_rate"] <= evidence["maximum_correction_rate"]
        ):
            return WORKER_FAST, "evidence_downgrade", "Representative evaluation evidence passed every configured downgrade threshold."
        return WORKER_STRONG, "reliability_baseline", "Downgrade evidence is absent or below threshold; use the strongest reliability baseline."

    def _provider(
        self, requested: Optional[str], required: tuple[str, ...]
    ) -> tuple[str, Optional[str]]:
        providers = self.config["providers"]
        initial = requested or self.config["default_provider"]
        if initial not in providers:
            raise StorageRefusal("routing_provider_unknown", "explicit provider is not configured")
        if set(required) <= set(providers[initial]["capabilities"]):
            return str(initial), None
        if requested is not None:
            raise StorageRefusal(
                "routing_capability_unavailable", "explicit provider lacks a required capability"
            )
        for candidate in self.config.get("provider_order", list(providers)):
            if set(required) <= set(providers[candidate]["capabilities"]):
                return str(candidate), str(initial)
        raise StorageRefusal("routing_capability_unavailable", "no configured provider satisfies the task")

    def _override(self, role: str, at: str) -> Optional[Mapping[str, Any]]:
        now = _time(at, "routing decision time")
        matches = [
            override
            for override in self.config.get("operator_overrides", [])
            if role in override["roles"]
            and _time(override["starts_at"], "operator override start") <= now
            < _time(override["expires_at"], "operator override expiry")
        ]
        if len(matches) > 1:
            raise StorageRefusal("routing_override_conflict", "multiple operator overrides match")
        return matches[0] if matches else None

    def choose(
        self,
        *,
        decision_id: str,
        subject_kind: str,
        subject_id: str,
        role: str,
        chosen_at: str,
        signals: Mapping[str, Any] | RoutingSignals,
        required_capabilities: Sequence[str] = (),
        explicit_provider: Optional[str] = None,
        explicit_model: Optional[str] = None,
        explicit_effort: Optional[str] = None,
    ) -> dict[str, Any]:
        semantic = signals if isinstance(signals, RoutingSignals) else RoutingSignals.from_value(signals)
        required = tuple(required_capabilities)
        if len(set(required)) != len(required) or any(not isinstance(item, str) or not item for item in required):
            raise StorageRefusal("routing_capabilities_invalid", "required capabilities are invalid")
        override = self._override(role, chosen_at)
        requested_provider = explicit_provider or (str(override["provider"]) if override else None)
        provider, fallback = self._provider(requested_provider, required)
        tier, reason_code, reason = self._tier(semantic, provider)
        selected = self.config["providers"][provider]["tiers"][tier]
        model = explicit_model or (str(override["model"]) if override else str(selected["model"]))
        effort = explicit_effort or (str(override["effort"]) if override else str(selected["effort"]))
        if explicit_provider is not None or explicit_model is not None or explicit_effort is not None:
            reason_code = "explicit_override"
            reason = "Explicit provider, model, and effort choices take precedence; only unspecified fields use policy."
        elif override is not None:
            reason_code = "operator_override"
            reason = "An unexpired operator override supplies the configured provider, model, and effort."
        elif fallback is not None:
            reason_code = "provider_capability_fallback"
            reason = "Required capabilities selected the first configured capable provider."
        if reason_code not in REASON_CODES or not all(
            isinstance(item, str) and item
            for item in (decision_id, subject_kind, subject_id, role, chosen_at, provider, model, effort)
        ):
            raise StorageRefusal("routing_invalid", "routing decision fields cannot be empty")
        return self.storage.record_routing_decision(
            RoutingChoice(
                decision_id=decision_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
                tier=tier,
                provider=provider,
                provider_config_version=str(
                    self.config["providers"][provider]["config_version"]
                ),
                model=model,
                effort=effort,
                reason=reason,
                reason_code=reason_code,
                policy_version=str(self.config["policy_version"]),
                explicit_provider=explicit_provider is not None,
                explicit_model=explicit_model is not None,
                explicit_effort=explicit_effort is not None,
                operator_override_id=str(override["id"]) if override is not None else None,
                fallback_from_provider=fallback,
                required_capabilities=required,
                signals=semantic.as_record(),
                state="selected",
                escalation_count=0,
                prior_decision_id=None,
                failure_class=None,
                chosen_at=chosen_at,
            ).as_record()
        )

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
        explicit_target_pin = bool(prior["explicit_model"]) or bool(prior["explicit_effort"])
        exhausted = (
            int(prior["escalation_count"]) >= 1
            or prior["tier"] == WORKER_STRONG
            or explicit_target_pin
        )
        if exhausted:
            state, tier = "blocked", str(prior["tier"])
            provider, model, effort = str(prior["provider"]), str(prior["model"]), str(prior["effort"])
            reason_code = "escalation_exhausted"
            reason = "The one safe-boundary escalation is unavailable or exhausted; report blocked."
        else:
            state, tier = "escalated", WORKER_STRONG
            provider = str(prior["provider"])
            selected = self.config["providers"][provider]["tiers"][WORKER_STRONG]
            model = str(prior["model"]) if prior["explicit_model"] else str(selected["model"])
            effort = str(prior["effort"]) if prior["explicit_effort"] else str(selected["effort"])
            reason_code = "failure_escalation"
            reason = f"Concrete {failure_class.replace('_', ' ')} triggered the one safe-boundary stronger retry."
        return self.storage.record_routing_decision(
            RoutingChoice(
                decision_id=decision_id,
                subject_kind=str(prior["subject_kind"]),
                subject_id=str(prior["subject_id"]),
                role=str(prior["role"]),
                tier=tier,
                provider=provider,
                provider_config_version=str(prior["provider_config_version"]),
                model=model,
                effort=effort,
                reason=reason,
                reason_code=reason_code,
                policy_version=str(prior["policy_version"]),
                explicit_provider=bool(prior["explicit_provider"]),
                explicit_model=bool(prior["explicit_model"]),
                explicit_effort=bool(prior["explicit_effort"]),
                operator_override_id=prior["operator_override_id"],
                fallback_from_provider=prior["fallback_from_provider"],
                required_capabilities=tuple(json.loads(prior["required_capabilities_json"])),
                signals=json.loads(prior["signals_json"]),
                state=state,
                escalation_count=int(prior["escalation_count"]) + (1 if state == "escalated" else 0),
                prior_decision_id=prior_decision_id,
                failure_class=failure_class,
                chosen_at=chosen_at,
            ).as_record()
        )

    def record_outcome(
        self,
        *,
        outcome_id: str,
        decision_id: str,
        success: bool,
        corrections: int,
        latency_ms: int,
        cost_microunits: Optional[int],
        recorded_at: str,
    ) -> dict[str, Any]:
        measures = (corrections, latency_ms)
        if (
            not all(isinstance(item, str) and item for item in (outcome_id, decision_id, recorded_at))
            or not isinstance(success, bool)
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in measures)
            or (
                cost_microunits is not None
                and (isinstance(cost_microunits, bool) or not isinstance(cost_microunits, int) or cost_microunits < 0)
            )
        ):
            raise StorageRefusal("routing_outcome_invalid", "routing outcome fields are invalid")
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
