"""Compatibility harness implementation shared by dedicated adapters."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePath
from typing import Any, Mapping

from ..adapter_types import AdapterContract, AdapterInstruction, OpaqueIdentity, RuntimeObservation
from ..storage_types import StorageRefusal
from .core import LifecycleEvent


def deliver_via_multiplexer(
    *, target: Mapping[str, Any], envelope: Mapping[str, Any], multiplexer: Any,
    **_unused: Any,
) -> None:
    routing_target = target.get("routing_name") or target.get("locator")
    if not isinstance(routing_target, str) or not routing_target:
        raise StorageRefusal("delivery_target_unavailable", "receiver routing is unavailable")
    if "delivery" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_delivery_unsupported",
            "selected multiplexer has no delivery transport",
        )
    summary = " ".join(str(envelope.get("summary", "")).split())
    multiplexer.delivery(
        routing_target,
        (
            f"CHAMPION TRANSITION [{envelope['event_id']}] "
            f"{envelope.get('status')}: {summary}"
        ),
    )


def steer_via_multiplexer(
    *, target: Mapping[str, Any], envelope: Mapping[str, Any], multiplexer: Any,
    **_unused: Any,
) -> None:
    """Transport one canonical delegated pause through provider steering."""

    routing_target = target.get("routing_name") or target.get("locator")
    if not isinstance(routing_target, str) or not routing_target:
        raise StorageRefusal("delivery_target_unavailable", "receiver routing is unavailable")
    if (
        envelope.get("event_type") != "owner_stop_control"
        or "delivery" not in multiplexer.capabilities
    ):
        raise StorageRefusal(
            "multiplexer_steering_unsupported",
            "selected multiplexer cannot transport delegated steering",
        )
    multiplexer.delivery(
        routing_target,
        (
            f"LEAGUE OWNER CONTROL [{envelope['event_id']}] Pause delegated work now, "
            "preserve durable progress, and await a new explicit owner instruction."
        ),
    )


def _one(rows: list[Any], code: str, message: str) -> Any:
    if len(rows) != 1:
        raise StorageRefusal(code, message)
    return rows[0]


def native_assignment(store: Any, row: Mapping[str, Any]) -> str:
    """Resolve a native Codex/Cursor assignment without provider branching."""

    if row["role"] == "champion":
        rows = store.connection.execute(
            """
            SELECT task_assignment_id AS assignment_id,NULL AS scope_kind,
                   NULL AS scope_id
              FROM task_assignments
             WHERE runtime_instance_id=? AND champion_agent_id=? AND state='active'
             ORDER BY task_assignment_id LIMIT 2
            """,
            (row["runtime_instance_id"], row["agent_id"]),
        ).fetchall()
    else:
        rows = store.connection.execute(
            """
            SELECT callsign_assignment_id AS assignment_id,scope_kind,scope_id
              FROM callsign_assignments
             WHERE runtime_instance_id=? AND agent_id=? AND role='shotcaller'
               AND state='active'
             ORDER BY callsign_assignment_id LIMIT 2
            """,
            (row["runtime_instance_id"], row["agent_id"]),
        ).fetchall()
    assignment = _one(
        list(rows),
        "display_replay_assignment_unproven",
        "runtime does not bind one active canonical assignment",
    )
    if row["role"] == "shotcaller":
        current_scope = (
            assignment["scope_kind"] == "shotcaller"
            and assignment["scope_id"] == row["agent_id"]
        )
        legacy_scope = assignment["scope_kind"] == "squad"
        if legacy_scope:
            squad_rows = store.connection.execute(
                """
                SELECT squad_id FROM squads
                 WHERE squad_id=? AND shotcaller_agent_id=? AND state='active'
                 ORDER BY squad_id LIMIT 2
                """,
                (assignment["scope_id"], row["agent_id"]),
            ).fetchall()
            legacy_scope = len(squad_rows) == 1
        if not current_scope and not legacy_scope:
            raise StorageRefusal(
                "display_replay_assignment_unproven",
                "Shotcaller assignment scope does not prove current ownership",
            )
    return str(assignment["assignment_id"])


def no_replacement_descriptor_transactions(**_inputs: Any) -> tuple[Any, ...]:
    """Default for adapters without a canonical launch-descriptor resource."""

    return ()


def native_presentation(
    store: Any,
    row: Mapping[str, Any],
    assignment_id: str,
    project_code: str,
) -> dict[str, Any]:
    """Resolve canonical presentation for a native Codex/Cursor session."""

    if row["role"] == "shotcaller":
        publication = store.shotcaller_bootstrap_publication(assignment_id)
        if not isinstance(publication, Mapping):
            raise StorageRefusal(
                "display_replay_descriptor_unproven",
                "Shotcaller runtime has no canonical bootstrap publication",
            )
        if (
            publication.get("session_identity") != row["session_ref"]
            or publication.get("routing_name") != row["routing_name"]
            or publication.get("callsign") != row["callsign"]
            or publication.get("worktree") != row["worktree"]
        ):
            raise StorageRefusal(
                "display_replay_descriptor_mismatch",
                "Shotcaller publication does not bind the canonical runtime",
            )
        task_label = "Squad Lead"
        title = f"{row['callsign']} · {project_code}"
        source = "league-shotcaller-" + hashlib.sha256(
            assignment_id.encode()
        ).hexdigest()[:16]
        applies_to_source = str(publication["presentation_source"])
    else:
        context = store.assignment_launch_context(assignment_id)
        delivery = context.get("context_delivery")
        display = delivery.get("display_receipt") if isinstance(delivery, Mapping) else None
        if not isinstance(display, Mapping):
            raise StorageRefusal(
                "display_replay_descriptor_unproven",
                "Champion runtime has no canonical display receipt",
            )
        task_label = str(display.get("task_label", ""))
        title = str(display.get("terminal_title", ""))
        source = str(display.get("source", ""))
        applies_to_source = str(display.get("applies_to_source", ""))
    if len(task_label.split()) != 2 or not title or not source or not applies_to_source:
        raise StorageRefusal(
            "display_replay_descriptor_unproven",
            "canonical presentation fields are incomplete",
        )
    return {
        "provider_kind": str(row["display_agent"]),
        "task_label": task_label,
        "thread": str(row["session_ref"]),
        "metadata_source": source,
        "applies_to_source": applies_to_source,
        "title": title,
        "tokens": {
            "launch_title_owner": hashlib.sha256(assignment_id.encode()).hexdigest()[:16],
            "launch_title_source": source,
            "launch_title_applies_to": applies_to_source,
        },
    }


@dataclass(frozen=True)
class DeclaredAgentAdapter:
    contract: AdapterContract
    native_events: Mapping[str, str]
    session_fields: tuple[str, ...]
    source_fields: tuple[str, ...]
    lifecycle_operations: frozenset[str]
    process_names: frozenset[str]
    provider_kinds: frozenset[str]
    provider_aliases: Mapping[str, str]
    launch_profile: Any
    hook_profile: Mapping[str, Mapping[str, Any]]
    hook_bootstrap_profile: Mapping[str, Any]
    hook_bootstrap_installer: Any
    multiplexer_requirements: Mapping[str, frozenset[str]]
    visible_launch_factory: Any
    delivery_handler: Any
    steering_handler: Any
    presentation_factory: Any
    assignment_factory: Any
    replacement_descriptor_factory: Any

    def install_hook_bootstrap(
        self,
        *,
        source_root: Path,
        target: Path,
        stable_watcher: Path,
    ) -> Mapping[str, Any]:
        if not callable(self.hook_bootstrap_installer):
            raise StorageRefusal(
                "hook_bootstrap_unsupported",
                "agent adapter does not provide a hook bootstrap installer",
            )
        return self.hook_bootstrap_installer(
            adapter_kind=self.contract.kind,
            hook_profile=self.hook_profile,
            bootstrap_profile=self.hook_bootstrap_profile,
            source_root=source_root,
            target=target,
            stable_watcher=stable_watcher,
        )

    def accepts_provider(self, provider_kind: str) -> bool:
        return self.normalize_provider(provider_kind) in self.provider_kinds

    def normalize_provider(self, provider_kind: str) -> str:
        if not isinstance(provider_kind, str) or not provider_kind:
            raise StorageRefusal(
                "launch_provider_invalid", "provider identity is missing"
            )
        return self.provider_aliases.get(provider_kind, provider_kind)

    def canonical_presentation(self, **inputs: Any) -> Mapping[str, Any]:
        if not callable(self.presentation_factory):
            raise StorageRefusal(
                "display_replay_adapter_unknown",
                "agent adapter has no canonical presentation resolver",
            )
        return self.presentation_factory(**inputs)

    def canonical_assignment(
        self, *, store: Any, row: Mapping[str, Any]
    ) -> str:
        if not callable(self.assignment_factory):
            raise StorageRefusal(
                "display_replay_adapter_unknown",
                "agent adapter has no canonical assignment resolver",
            )
        return str(self.assignment_factory(store, row))

    def replacement_descriptor_transactions(
        self,
        *,
        phase: str,
        participant: str,
        operation_id: str,
        assignment_id: str,
        target: Mapping[str, Any],
        activated: bool,
    ) -> tuple[Any, ...]:
        if not callable(self.replacement_descriptor_factory):
            raise StorageRefusal(
                "runtime_replacement_descriptor_invalid",
                "agent adapter descriptor transition factory is unavailable",
            )
        actions = self.replacement_descriptor_factory(
            phase=phase,
            participant=participant,
            operation_id=operation_id,
            assignment_id=assignment_id,
            target=target,
            activated=activated,
        )
        if not isinstance(actions, tuple) or any(
            getattr(transaction, "source_adapter", None) != self.contract.kind
            or getattr(transaction, "assignment_id", None) != assignment_id
            or not callable(getattr(transaction, "apply", None))
            for transaction in actions
        ):
            raise StorageRefusal(
                "runtime_replacement_descriptor_invalid",
                "agent adapter descriptor transition plan is malformed",
            )
        return actions

    def verify_replacement(
        self,
        *,
        target: Mapping[str, Any],
        multiplexer: Any,
    ) -> Mapping[str, Any]:
        provider_kind = self._validated_lifecycle_pair(
            operation="replacement",
            provider_kind=target.get("provider_kind"),
            multiplexer=multiplexer,
            multiplexer_capability="runtime_replacement",
            adapter_code="runtime_replacement_adapter_unsupported",
            adapter_message="agent adapter does not support runtime replacement",
            provider_code="runtime_replacement_provider_mismatch",
            multiplexer_code="runtime_replacement_multiplexer_unsupported",
            multiplexer_message="multiplexer does not support exact runtime replacement",
        )
        return multiplexer.replacement_verify(
            adapter_kind=self.contract.kind,
            provider_kind=provider_kind,
            process_names=self.process_names,
            target=target,
        )

    def _validated_lifecycle_pair(
        self,
        *,
        operation: str,
        provider_kind: Any,
        multiplexer: Any,
        multiplexer_capability: str,
        adapter_code: str,
        adapter_message: str,
        provider_code: str,
        multiplexer_code: str,
        multiplexer_message: str,
    ) -> str:
        if operation not in self.lifecycle_operations:
            raise StorageRefusal(
                adapter_code,
                adapter_message,
            )
        if not isinstance(provider_kind, str):
            raise StorageRefusal(
                provider_code,
                "provider does not belong to the selected agent adapter",
            )
        normalized = self.normalize_provider(provider_kind)
        if not self.accepts_provider(normalized):
            raise StorageRefusal(
                provider_code,
                "provider does not belong to the selected agent adapter",
            )
        if multiplexer_capability not in multiplexer.capabilities:
            raise StorageRefusal(
                multiplexer_code,
                multiplexer_message,
            )
        return normalized

    def recover_replacement(
        self,
        *,
        target: Mapping[str, Any],
        multiplexer: Any,
    ) -> Mapping[str, Any] | None:
        provider_kind = self._validated_lifecycle_pair(
            operation="replacement",
            provider_kind=target.get("provider_kind"),
            multiplexer=multiplexer,
            multiplexer_capability="runtime_replacement",
            adapter_code="runtime_replacement_adapter_unsupported",
            adapter_message="agent adapter does not support runtime replacement",
            provider_code="runtime_replacement_provider_mismatch",
            multiplexer_code="runtime_replacement_multiplexer_unsupported",
            multiplexer_message="multiplexer does not support exact runtime replacement",
        )
        return multiplexer.replacement_recover(
            adapter_kind=self.contract.kind,
            provider_kind=provider_kind,
            process_names=self.process_names,
            target=target,
        )

    def retire_replacement(
        self,
        *,
        operation_id: str,
        target: Mapping[str, Any],
        multiplexer: Any,
    ) -> Mapping[str, Any]:
        return multiplexer.replacement_retire(
            operation_id=operation_id,
            adapter_kind=self.contract.kind,
            provider_kind=str(target["provider_kind"]),
            process_names=self.process_names,
            exit_prompt=str(self.launch_profile.exit_prompt),
            target=target,
        )

    def verify_stopped_retirement(
        self,
        *,
        target: Mapping[str, Any],
        provider_kind: str,
        multiplexer: Any,
    ) -> Mapping[str, Any]:
        normalized = self._validated_lifecycle_pair(
            operation="retirement",
            provider_kind=provider_kind,
            multiplexer=multiplexer,
            multiplexer_capability="stopped_retirement",
            adapter_code="stopped_retirement_adapter_unsupported",
            adapter_message="agent adapter does not support retirement",
            provider_code="stopped_retirement_provider_mismatch",
            multiplexer_code="stopped_retirement_multiplexer_unsupported",
            multiplexer_message="multiplexer cannot prove an already-stopped endpoint",
        )
        return multiplexer.verify_stopped_agent(
            adapter_kind=self.contract.kind,
            provider_kind=normalized,
            process_names=self.process_names,
            target=target,
        )

    def translate_event(self, native_event: str, payload: Mapping[str, Any]) -> LifecycleEvent:
        operation = self.native_events.get(native_event)
        if operation is None:
            raise StorageRefusal("adapter_event_unsupported", "provider event is not registered")
        session = next(
            (payload.get(field) for field in self.session_fields if isinstance(payload.get(field), str) and payload.get(field)),
            None,
        )
        source = next(
            (payload.get(field) for field in self.source_fields if isinstance(payload.get(field), str) and payload.get(field)),
            None,
        )
        if not isinstance(session, str) or not isinstance(source, str):
            raise StorageRefusal("adapter_event_invalid", "provider event lacks exact session or source identity")
        return LifecycleEvent(operation, self.contract.kind, native_event, session, source, dict(payload))

    def visible_launch(self, **inputs: Any) -> Any:
        if "launch" not in self.lifecycle_operations or not callable(
            self.visible_launch_factory
        ):
            raise StorageRefusal(
                "launch_harness_unsupported",
                "agent adapter does not provide a visible launch driver",
            )
        return self.visible_launch_factory(**inputs)

    def deliver(self, **inputs: Any) -> Any:
        if "delivery" not in self.lifecycle_operations or not callable(
            self.delivery_handler
        ):
            raise StorageRefusal(
                "delivery_harness_unsupported",
                "agent adapter does not provide a delivery driver",
            )
        return self.delivery_handler(**inputs)

    def control_delegated(self, **inputs: Any) -> Any:
        """Apply a canonical owner control through the adapter's steer surface."""

        if "steer" not in self.lifecycle_operations or not callable(
            self.steering_handler
        ):
            raise StorageRefusal(
                "owner_stop_adapter_unsupported",
                "agent adapter cannot steer delegated work",
            )
        return self.steering_handler(**inputs)

    def restored_presentation(
        self, descriptor: Mapping[str, Any], observation: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        agent = observation.get("agent")
        process = observation.get("process")
        if (
            descriptor.get("agent_adapter_kind") != self.contract.kind
            or descriptor.get("runtime_kind") != self.contract.kind
            or not isinstance(agent, Mapping)
            or agent.get("agent") != self.contract.kind
            or observation.get("session_ref") != descriptor.get("session_ref")
            or observation.get("session_source") != descriptor.get("applies_to_source")
            or not isinstance(process, Mapping)
            or PurePath(str(process.get("argv0", ""))).name not in self.process_names
        ):
            raise StorageRefusal(
                "display_replay_binding_mismatch",
                "restored process does not bind the exact agent adapter and session",
            )
        return {
            key: descriptor[key]
            for key in (
                "agent_adapter_kind",
                "provider_kind",
                "session_ref",
                "cwd",
                "routing_name",
                "metadata_source",
                "applies_to_source",
                "title",
                "tokens",
            )
        }

    def _instruction(self, capability: str, session: OpaqueIdentity | None = None, **payload: Any) -> AdapterInstruction:
        self.contract.require(capability)
        if session is not None:
            if session.namespace != self.contract.kind:
                raise StorageRefusal("identity_mismatch", "session namespace does not match harness adapter")
            payload = {"session_identity": session.encoded, **payload}
        return AdapterInstruction(capability, {"harness": self.contract.kind, **payload})

    def create(self, specification: Mapping[str, Any]) -> AdapterInstruction:
        return self._instruction("create", specification=dict(specification))

    def identify(self, observation: RuntimeObservation) -> OpaqueIdentity:
        self.contract.require("identify")
        identity = OpaqueIdentity.decode(str(observation.details.get("session_identity")))
        if identity.namespace != self.contract.kind:
            raise StorageRefusal("identity_mismatch", "observed session belongs to another harness")
        return identity

    def title(self, session: OpaqueIdentity, title: str) -> AdapterInstruction:
        if not title.strip():
            raise StorageRefusal("invalid_title", "runtime title cannot be empty")
        return self._instruction("title", session, title=title)

    def prompt(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction:
        if not prompt.strip():
            raise StorageRefusal("invalid_prompt", "runtime prompt cannot be empty")
        return self._instruction("prompt", session, prompt=prompt)

    def steer(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction:
        """Translate shared steering to the adapter's native prompt intake.

        Cursor's effect driver additionally applies its verified working-state
        interrupt protocol; Codex and Pi accept the same logical operation as a
        provider-native prompt without advertising an interrupt capability.
        """

        return self.prompt(session, prompt)

    def status(self, session: OpaqueIdentity, observation: RuntimeObservation) -> str:
        self.contract.require("status")
        observed = observation.details.get("session_identity")
        if session.namespace != self.contract.kind or (observed is not None and str(observed) != session.encoded):
            raise StorageRefusal("identity_mismatch", "runtime observation belongs to another session")
        return observation.state

    def hook(self, session: OpaqueIdentity, event: str) -> AdapterInstruction:
        if not event.strip():
            raise StorageRefusal("invalid_hook", "hook event cannot be empty")
        return self._instruction("hook", session, event=event)

    def interrupt(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("interrupt", session)

    def resume(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("resume", session)

    def exit(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("exit", session)
