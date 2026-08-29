#!/usr/bin/env python3
"""Bounded startup context and configured bidirectional rollover coverage."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.sqlite_callsign_ops import digest  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_test_support import invoke_cli, migrated_state  # noqa: E402
from test_shotcaller_rollover import (  # noqa: E402
    AT1,
    AT2,
    AT3,
    NEW_ID,
    OLD_ID,
    SQUAD_ID,
    plan,
    runtime_receipt,
    seed_rollover,
)


AT_RUN = "2026-01-01T00:03:00Z"
ADAPTER = ROOT / "tests" / "fakes" / "rollover_provider.py"


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _seed_champion_assignment(store: SQLiteStorage, champion_id: str) -> None:
    task_id = "task:champion:0"
    request_id = "request:champion:0"
    runtime_id = "runtime:champion:0"
    callsign = store.connection.execute(
        "SELECT callsign FROM agent_instances WHERE agent_id=?", (champion_id,)
    ).fetchone()[0]
    store.connection.execute(
        """
        INSERT INTO requests
          (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
           execution_mode,state,version,created_at,updated_at,route_reason_code,
           route_policy_version,route_confidence)
        VALUES(?, 'Private synthetic request summary', ?, ?, ?, 'champion',
               'in_progress',1,?,?, 'explicit_champion','policy:v1','explicit')
        """,
        (request_id, OLD_ID, champion_id, OLD_ID, AT1, AT1),
    )
    store.connection.execute(
        """
        UPDATE tasks SET request_id=?,coordinator_agent_id=?,champion_agent_id=?,project_id=NULL
         WHERE task_id=?
        """,
        (request_id, OLD_ID, champion_id, task_id),
    )
    store.connection.execute(
        """
        INSERT INTO task_assignments
          (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,assignment_role,state,cleanup_required,version,
           created_at,updated_at)
        VALUES('task-assignment:champion:0',?,?,?,?,?,?,'champion','active',0,1,?,?)
        """,
        (task_id, request_id, OLD_ID, champion_id, runtime_id, callsign, AT1, AT1),
    )
    store.connection.execute(
        """
        INSERT INTO obligations
          (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
           created_at,updated_at)
        VALUES('obligation:handoff',?,'coordination',?,'handoff:old-owner','open',?,?)
        """,
        (OLD_ID, SQUAD_ID, AT2, AT2),
    )


def _adapter_config(
    *,
    fail_once_marker: Path | None = None,
    providers: tuple[str, ...] = ("codex", "cursor"),
) -> dict:
    return {
        "schema": "league.rollover-provider-adapters.v1",
        "adapters": [
            {
                "harness_kind": provider,
                "command": [
                    "/usr/bin/env",
                    "python3",
                    str(ADAPTER),
                    *(
                        [f"--fail-once={fail_once_marker}"]
                        if fail_once_marker is not None and provider == "cursor"
                        else []
                    ),
                    provider,
                ],
            }
            for provider in providers
        ],
    }


def _files(
    root: Path,
    context: dict,
    *,
    fail_once_marker: Path | None = None,
) -> tuple[Path, Path]:
    adapters = _adapter_config(fail_once_marker=fail_once_marker)
    handoff_plan = plan()
    handoff_plan["provider_adapter_digest"] = digest(adapters)
    manifest = {
        "schema": "league.shotcaller-rollover-run.v1",
        "operation_id": "rollover:synthetic",
        "squad_id": SQUAD_ID,
        "predecessor_agent_id": OLD_ID,
        "successor_agent_id": NEW_ID,
        "predecessor_runtime_instance_id": "runtime:old-shotcaller",
        "successor_runtime_instance_id": "runtime:new-shotcaller",
        "callsign_assignment_id": context["successor"]["assignment_id"],
        "expected_owner_version": 1,
        "expected_owner_fence": 1,
        "authority_kind": "explicit",
        "authority_digest": "authority-receipt-digest",
        "required_capabilities": ["rollover.accept"],
        "plan": handoff_plan,
        "owner_event_id": "event:owner-changed",
        "owner_outbox_id": "outbox:owner-changed",
    }
    manifest_path = root / "rollover-run.json"
    adapter_path = root / "rollover-adapters.json"
    _write_json(manifest_path, manifest)
    _write_json(adapter_path, adapters)
    return manifest_path, adapter_path


def _direction(root: Path, predecessor: str, successor: str) -> None:
    state, _ = migrated_state(root, f"{predecessor}-to-{successor}")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1, old_harness_kind=predecessor)
        _seed_champion_assignment(store, context["champion_ids"][0])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"],
                "new-shotcaller",
                ["rollover.accept"],
                harness_kind=successor,
            ),
            AT3,
        )
        champion_id = context["champion_ids"][0]
        before = tuple(
            store.connection.execute(
                """
                SELECT task_id,thread_id,repository,issue,branch,worktree,callsign
                  FROM agent_instances WHERE agent_id=?
                """,
                (champion_id,),
            ).fetchone()
        )
        champion_startup = store.startup_context(champion_id, "runtime:champion:0", AT_RUN)
        rendered = json.dumps(champion_startup, sort_keys=True)
        assert champion_startup["identity"]["role"] == "champion"
        assert champion_startup["owning_shotcaller"]["agent_id"] == OLD_ID
        assert champion_startup["requesting_shotcaller"]["agent_id"] == OLD_ID
        assert champion_startup["request"]["request_id"] == "request:champion:0"
        for private in ("/synthetic/worktrees", "example.invalid", "synthetic-endpoint", "session_identity"):
            assert private not in rendered
    startup_envelope = invoke_cli(
        state,
        "agent",
        "startup-context",
        "--agent-id",
        champion_id,
        "--runtime-instance-id",
        "runtime:champion:0",
        "--at",
        AT_RUN,
    )
    assert startup_envelope["result"] == champion_startup
    manifest_path, adapter_path = _files(root, context)
    envelope = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(adapter_path),
        "--at",
        AT_RUN,
    )
    result = envelope["result"]
    assert envelope["ok"] is True
    assert result["operation"]["state"] == "completed"
    assert [item["stage"] for item in result["stages"]] == [
        "status",
        "prepare",
        "bindings",
        "acknowledge",
        "commit",
        "drain",
    ]
    public = json.dumps(envelope, sort_keys=True)
    for private in (str(ADAPTER), "session_identity", "endpoint_identity", "runtime_generation"):
        assert private not in public
    with SQLiteStorage(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id='event:owner-changed'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT owner_agent_id FROM obligations WHERE obligation_id='obligation:handoff'"
        ).fetchone()[0] == NEW_ID
        after = tuple(
            store.connection.execute(
                """
                SELECT task_id,thread_id,repository,issue,branch,worktree,callsign
                  FROM agent_instances WHERE agent_id=?
                """,
                (champion_id,),
            ).fetchone()
        )
        assert after == before
        post_rollover_startup = store.startup_context(
            champion_id, "runtime:champion:0", AT_RUN
        )
        assert post_rollover_startup["owning_shotcaller"]["agent_id"] == NEW_ID
        assert post_rollover_startup["requesting_shotcaller"]["agent_id"] == OLD_ID
        retry = invoke_cli(
            state,
            "rollover",
            "run",
            "--manifest",
            str(manifest_path),
            "--adapter-config",
            str(adapter_path),
            "--at",
            AT_RUN,
        )["result"]
        assert retry["operation"]["state"] == "completed"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 1


def test_stale_and_ambiguous_startup_refusal(root: Path) -> None:
    state, _ = migrated_state(root, "startup-refusals")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion = context["champion_ids"][0]
        _seed_champion_assignment(store, champion)
        store.connection.execute(
            "UPDATE agent_instances SET address='changed-endpoint' WHERE agent_id=?", (champion,)
        )
        try:
            store.startup_context(champion, "runtime:champion:0", AT_RUN)
        except StorageRefusal as exc:
            assert exc.code == "startup_identity_stale"
        else:
            raise AssertionError("stale startup identity was accepted")
        store.connection.execute(
            "UPDATE agent_instances SET address='synthetic-endpoint:champion:0' WHERE agent_id=?",
            (champion,),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
               endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
            VALUES('runtime:ambiguous',?,'synthetic','herdr','synthetic:ambiguous',
                   'synthetic-endpoint:ambiguous','generation:ambiguous','idle',1,?,'["task.execute"]')
            """,
            (champion, AT_RUN),
        )
        try:
            store.startup_context(champion, "runtime:champion:0", AT_RUN)
        except StorageRefusal as exc:
            assert exc.code == "startup_identity_ambiguous"
        else:
            raise AssertionError("ambiguous startup identity was accepted")


def test_pre_switch_adapter_retry_and_abort(root: Path) -> None:
    state, _ = migrated_state(root, "adapter-retry")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0, old_harness_kind="codex")
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"],
                "new-shotcaller",
                ["rollover.accept"],
                harness_kind="cursor",
            ),
            AT3,
        )
    marker = root / "adapter-retry-idempotency-key"
    manifest_path, adapter_path = _files(root, context, fail_once_marker=marker)
    missing_adapter_path = root / "rollover-adapters-missing-predecessor.json"
    _write_json(
        missing_adapter_path,
        _adapter_config(fail_once_marker=marker, providers=("cursor",)),
    )
    unconfigured = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(missing_adapter_path),
        "--at",
        AT_RUN,
        expected=2,
    )
    assert unconfigured["error"]["code"] == "rollover_adapter_config_mismatch"
    with SQLiteStorage(state) as store:
        assert store.rollover_status("rollover:synthetic") is None
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 0
    refused = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(adapter_path),
        "--at",
        AT_RUN,
        expected=3,
    )
    assert refused["error"]["code"] == "rollover_adapter_outcome_unknown"
    with SQLiteStorage(state) as store:
        assert store.rollover_status("rollover:synthetic")["state"] == "prepared"
        assert store.connection.execute(
            "SELECT shotcaller_agent_id FROM squads WHERE squad_id=?", (SQUAD_ID,)
        ).fetchone()[0] == OLD_ID
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 0
    completed = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(adapter_path),
        "--at",
        AT_RUN,
    )["result"]
    assert completed["operation"]["state"] == "completed"

    abort_state, _ = migrated_state(root, "adapter-abort")
    with SQLiteStorage(abort_state) as store:
        abort_context = seed_rollover(store, champion_count=0, old_harness_kind="cursor")
        store.activate_callsign(
            abort_context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                abort_context["successor"],
                "new-shotcaller",
                ["rollover.accept"],
                harness_kind="codex",
            ),
            AT3,
        )
    abort_manifest, abort_adapters = _files(root, abort_context)
    aborted = invoke_cli(
        abort_state,
        "rollover",
        "run",
        "--manifest",
        str(abort_manifest),
        "--adapter-config",
        str(abort_adapters),
        "--at",
        AT_RUN,
        "--abort",
    )["result"]
    assert aborted["operation"]["state"] == "aborted"
    with SQLiteStorage(abort_state) as store:
        assert store.connection.execute(
            "SELECT shotcaller_agent_id FROM squads WHERE squad_id=?", (SQUAD_ID,)
        ).fetchone()[0] == OLD_ID
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 0

    reserved_state, _ = migrated_state(root, "adapter-abort-historical-runtime")
    with SQLiteStorage(reserved_state) as store:
        reserved_context = seed_rollover(
            store, champion_count=0, old_harness_kind="cursor"
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
               endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
            VALUES('runtime:historical-successor',?,'codex','herdr','codex:new-shotcaller',
                   'synthetic-endpoint:new-shotcaller','generation:historical','closed',1,?,
                   '["rollover.accept"]')
            """,
            (NEW_ID, AT2),
        )
    reserved_manifest, reserved_adapters = _files(root, reserved_context)
    reserved_abort = invoke_cli(
        reserved_state,
        "rollover",
        "run",
        "--manifest",
        str(reserved_manifest),
        "--adapter-config",
        str(reserved_adapters),
        "--at",
        AT_RUN,
        "--abort",
    )["result"]
    assert reserved_abort["operation"]["state"] == "aborted"


def test_drain_waits_for_zero_obligations(root: Path) -> None:
    state, _ = migrated_state(root, "drain-gate")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0, old_harness_kind="codex")
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"],
                "new-shotcaller",
                ["rollover.accept"],
                harness_kind="cursor",
            ),
            AT3,
        )
        store.connection.execute(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
               state,version,created_at,updated_at)
            VALUES('request:old-owner-obligation','Synthetic direct obligation',?,?,?,
                   'in_progress',1,?,?)
            """,
            (OLD_ID, OLD_ID, OLD_ID, AT2, AT2),
        )
    manifest_path, adapter_path = _files(root, context)
    waiting = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(adapter_path),
        "--at",
        AT_RUN,
    )["result"]
    assert waiting["operation"]["state"] == "switched"
    assert waiting["pending_obligations"] == {
        "deliveries": 0,
        "durable": 0,
        "requests": 1,
    }
    assert waiting["stages"][-1] == {
        "stage": "drain",
        "outcome": "obligations_pending",
    }
    with SQLiteStorage(state) as store:
        assert store.connection.execute(
            "SELECT status FROM runtime_instances WHERE runtime_instance_id='runtime:old-shotcaller'"
        ).fetchone()[0] == "active"
        store.connection.execute(
            "UPDATE requests SET state='answered',version=2,updated_at=? WHERE request_id='request:old-owner-obligation'",
            (AT_RUN,),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
               endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
            VALUES('runtime:not-operation-bound',?,'codex','herdr','codex:historical-old',
                   'synthetic-endpoint:historical-old','generation:historical-old','closed',1,?,
                   '["rollover.accept"]')
            """,
            (OLD_ID, AT_RUN),
        )
        try:
            store.record_rollover_runtime_closed(
                "rollover:synthetic",
                "predecessor",
                "runtime:not-operation-bound",
                "codex:historical-old",
                "synthetic-endpoint:historical-old",
                "generation:historical-old",
                AT_RUN,
            )
        except StorageRefusal as exc:
            assert exc.code == "rollover_runtime_mismatch"
        else:
            raise AssertionError("unbound predecessor runtime was accepted")
        store.record_rollover_runtime_closed(
            "rollover:synthetic",
            "predecessor",
            "runtime:old-shotcaller",
            "codex:old-shotcaller",
            "synthetic-endpoint:old-shotcaller",
            "generation:old-shotcaller",
            AT_RUN,
        )
    completed = invoke_cli(
        state,
        "rollover",
        "run",
        "--manifest",
        str(manifest_path),
        "--adapter-config",
        str(adapter_path),
        "--at",
        AT_RUN,
    )["result"]
    assert completed["operation"]["state"] == "completed"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-rollover-successor-") as temporary:
        root = Path(temporary)
        _direction(root, "codex", "cursor")
        _direction(root, "cursor", "codex")
        test_stale_and_ambiguous_startup_refusal(root)
        test_pre_switch_adapter_retry_and_abort(root)
        test_drain_waits_for_zero_obligations(root)
    print("PASS: bounded startup context and configured Codex/Cursor rollover in both directions")


if __name__ == "__main__":
    main()
