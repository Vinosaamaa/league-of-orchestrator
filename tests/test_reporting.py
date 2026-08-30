#!/usr/bin/env python3
"""Focused deterministic report, reproduction, scope, gap, and renderer tests."""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.reporting import render_report  # noqa: E402
import league.cli as league_cli  # noqa: E402
import league.sqlite_report_ops as report_ops  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage_types import StorageRefusal  # noqa: E402
from storage_test_support import invoke_cli, migrated_state  # noqa: E402


AT0 = "2026-08-28T07:00:00Z"
AT1 = "2026-08-28T07:01:00Z"
AT2 = "2026-08-28T07:02:00Z"
AT3 = "2026-08-28T07:03:00Z"
AT4 = "2026-08-28T07:04:00Z"
AT5 = "2026-08-28T07:05:00Z"
AT6 = "2026-08-28T07:06:00Z"
AT20 = "2026-08-28T07:20:00Z"
HASH = "a" * 64


def evidence(identifier: str, kind: str, action: str, at: str, **overrides):
    value = {
        "evidence_id": identifier,
        "evidence_kind": kind,
        "action": action,
        "owner_agent_id": "actor:orianna",
        "squad_id": "squad:garen",
        "project_id": "project:league",
        "request_id": "request:champion",
        "task_id": "task:reporting",
        "state": "succeeded",
        "verification": "verified",
        "summary": f"Synthetic {kind} evidence",
        "public_url": None,
        "object_hash": HASH,
        "local_evidence_ref": None,
        "local_evidence": None,
        "local_evidence_hash": None,
        "stable_repair_id": None,
        "repair_phase": None,
        "root_cause_tag": None,
        "owning_issue_url": None,
        "required_for_completion": False,
        "occurred_at": at,
    }
    value.update(overrides)
    return value


def seed(store: SQLiteStorage) -> None:
    store.put_project(
        "project:league",
        expected_version=0,
        summary="League coordination",
        repository="https://github.com/Vinosaamaa/league-of-orchestrator",
        root="/synthetic/local/project",
        code="LEAGUE",
        aliases=("orchestrator",),
        state="active",
        repository_visibility="public",
        export_policy="public_repository",
        at=AT0,
    )
    with store._transaction():
        store.connection.executemany(
            "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES(?,?,1,?,NULL)",
            (
                ("Garen", "shotcaller", 1),
                ("Janna", "shotcaller", 2),
                ("Orianna", "champion", 3),
                ("Lux", "champion", 4),
            ),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,kind,status,version,updated_at,update_text,next_action,metadata_json)
            VALUES('actor:garen','Garen','shotcaller','synthetic','working',1,?,'Coordinating','Continue','{}')
            """,
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,kind,status,version,updated_at,update_text,next_action,metadata_json)
            VALUES('actor:janna','Janna','shotcaller','synthetic','working',1,?,'Receiving handoff','Acknowledge','{}')
            """,
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,shotcaller_agent_id,kind,status,version,updated_at,update_text,next_action,metadata_json)
            VALUES('actor:lux','Lux','champion','actor:garen','synthetic','working',1,?,'Implementing','Verify','{}')
            """,
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,shotcaller_agent_id,kind,status,version,updated_at,update_text,next_action,metadata_json)
            VALUES('actor:orianna','Orianna','champion','actor:garen','synthetic','working',1,?,'Implementing','Verify','{}')
            """,
            (AT0,),
        )
        store.connection.execute(
            "INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at) VALUES('squad:garen','actor:garen','active',1,?)",
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
            VALUES('actor:garen','squad:garen','accepting',1,1,?)
            """,
            (AT0,),
        )
        store.connection.executemany(
            "INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at) VALUES('squad:garen',?,?)",
            (("actor:orianna", AT0), ("actor:lux", AT0)),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
               runtime_generation,status,verified,last_seen_at)
            VALUES('operation:runtime','actor:orianna','codex','herdr','session:synthetic',
                   'endpoint:synthetic','generation:synthetic','active',1,?)
            """,
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
               runtime_generation,status,verified,last_seen_at)
            VALUES('operation:runtime-lux','actor:lux','pi','tmux','session:synthetic-lux',
                   'endpoint:synthetic-lux','generation:synthetic-lux','active',1,?)
            """,
            (AT0,),
        )
        store.connection.execute(
            """
            INSERT INTO prompts
              (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,source_event_key,
               triage_state,triage_digest,created_at,current_owner_agent_id,
               current_owner_runtime_instance_id)
            VALUES('evidence:prompt','actor:garen','operation:runtime','codex','session:synthetic',
                   'source:synthetic','complete',?,?,'actor:garen','operation:runtime')
            """,
            (HASH, AT1),
        )
        store.connection.executemany(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,execution_mode,state,version,created_at,updated_at)
            VALUES(?,?,'actor:garen',?,?, 'open',1,?,?)
            """,
            (
                ("request:direct", "Direct Shotcaller work", "actor:garen", "direct", AT1, AT2),
                ("request:champion", "Champion report implementation", "actor:orianna", "champion", AT1, AT2),
                ("request:privacy", "Champion privacy implementation", "actor:lux", "champion", AT1, AT2),
            ),
        )
        store.connection.executemany(
            "INSERT INTO prompt_items(prompt_item_id,prompt_id,ordinal,summary,disposition) VALUES(?,'evidence:prompt',?,?,?)",
            (
                ("evidence:item-direct", 1, "Answer direct concern", "new_request"),
                ("evidence:item-champion", 2, "Implement report", "new_request"),
                ("evidence:item-privacy", 3, "Implement privacy", "new_request"),
            ),
        )
        store.connection.executemany(
            "INSERT INTO request_sources(request_id,prompt_item_id,source_role) VALUES(?,?,'origin')",
            (
                ("request:direct", "evidence:item-direct"),
                ("request:champion", "evidence:item-champion"),
                ("request:privacy", "evidence:item-privacy"),
            ),
        )
        store.connection.executemany(
            """
            INSERT INTO request_dispatches
              (dispatch_id,request_id,request_version,work_kind,execution_mode,reason,requested_mode,
               requested_model,requested_effort,explicit_route,input_json,decided_at)
            VALUES(?,?,1,'implementation',?,'Synthetic route',?,?,?,'recorded','{}',?)
            """,
            (
                ("evidence:dispatch-direct", "request:direct", "direct", "direct", "gpt-synthetic", "high", AT2),
                ("evidence:dispatch-champion", "request:champion", "champion", "champion", "gpt-synthetic", "high", AT2),
                ("evidence:dispatch-privacy", "request:privacy", "champion", "champion", "gpt-synthetic-fast", "medium", AT2),
            ),
        )
        store.connection.execute(
            """
            INSERT INTO tasks
              (task_id,project_id,summary,state,version,updated_at,request_id,coordinator_agent_id,champion_agent_id)
            VALUES('task:reporting','project:league','Reporting and privacy','complete',1,?,
                   'request:champion','actor:garen','actor:orianna')
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO task_assignments
              (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
               runtime_instance_id,callsign,assignment_role,state,version,created_at,updated_at)
            VALUES('assignment:orianna','task:reporting','request:champion','actor:garen','actor:orianna',
                   'operation:runtime','Orianna','champion','active',1,?,?)
            """,
            (AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO tasks
              (task_id,project_id,summary,state,version,updated_at,request_id,coordinator_agent_id,champion_agent_id)
            VALUES('task:privacy','project:league','Outbound privacy','complete',1,?,
                   'request:privacy','actor:garen','actor:lux')
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO task_assignments
              (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
               runtime_instance_id,callsign,assignment_role,state,version,created_at,updated_at)
            VALUES('assignment:lux','task:privacy','request:privacy','actor:garen','actor:lux',
                   'operation:runtime-lux','Lux','champion','active',1,?,?)
            """,
            (AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO model_routing_decisions
              (decision_id,subject_kind,subject_id,role,tier,model,effort,reason,explicit_model,
               explicit_effort,state,escalation_count,chosen_at)
            VALUES('evidence:routing','task','task:reporting','champion','WORKER_STRONG',
                   'gpt-synthetic','high','Synthetic route',1,1,'selected',0,?)
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO model_routing_decisions
              (decision_id,subject_kind,subject_id,role,tier,model,effort,reason,explicit_model,
               explicit_effort,state,escalation_count,chosen_at)
            VALUES('evidence:routing-lux','task','task:privacy','champion','WORKER_FAST',
                   'gpt-synthetic-fast','medium','Synthetic route',1,1,'selected',0,?)
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_bindings
              (binding_id,task_id,harness_kind,backend_kind,session_identity,endpoint_identity,
               endpoint_generation,capabilities_json,state,version,created_at,updated_at)
            VALUES('operation:binding','task:reporting','codex','herdr','session:binding',
                   'endpoint:binding','generation:binding','{}','active',1,?,?)
            """,
            (AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO runtime_bindings
              (binding_id,task_id,harness_kind,backend_kind,session_identity,endpoint_identity,
               endpoint_generation,capabilities_json,state,version,created_at,updated_at)
            VALUES('operation:binding-lux','task:privacy','pi','tmux','session:binding-lux',
                   'endpoint:binding-lux','generation:binding-lux','{}','active',1,?,?)
            """,
            (AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO task_resources
              (resource_id,task_id,owner_id,owner_role,resource_type,lifetime,expected_identity_json,
               cleanup_action,adapter_kind,applicable,applicability_reason,state,version,registered_at,updated_at)
            VALUES('resource:worktree','task:reporting','actor:orianna','champion','worktree','task_owned',
                   '{}','release','fake',1,'Synthetic resource','active',1,?,?)
            """,
            (AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_obligations
              (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at,
               owner_id,task_class,disposition)
            VALUES('cleanup:reporting','task:reporting','cleanup_pending','terminal_task','Verify cleanup',
                   1,?,'actor:orianna','pr_ci','completed')
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_operations
              (operation_id,cleanup_obligation_id,cleanup_revision,plan_digest,state,fence,
               executor_id,leased_until,created_at,updated_at)
            VALUES('operation:cleanup-reporting','cleanup:reporting',1,?,'planned',0,NULL,NULL,?,?)
            """,
            (HASH, AT3, AT3),
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_actions
              (action_id,operation_id,ordinal,action_kind,adapter_kind,resource_id,state,
               expected_identity_json,intended_state_json)
            VALUES('cleanup:release-worktree','operation:cleanup-reporting',0,'release','fake',
                   'resource:worktree','planned','{}','{}')
            """
        )
        store.connection.execute(
            """
            INSERT INTO callsign_assignments
              (callsign_assignment_id,callsign,subject_id,agent_id,role,scope_kind,scope_id,
               state,reservation_position,queue_version,requirements_json,version,reserved_at)
            VALUES('assignment:janna-shotcaller','Janna','actor:janna','actor:janna','shotcaller',
                   'squad','squad:garen','reserved',0,1,'[]',1,?)
            """,
            (AT3,),
        )
        store.connection.execute(
            """
            INSERT INTO rollover_operations
              (operation_id,squad_id,predecessor_agent_id,successor_agent_id,callsign_assignment_id,
               state,authority_kind,authority_digest,required_capabilities_json,plan_json,plan_digest,
               handoff_digest,expected_owner_version,expected_owner_fence,snapshot_id,version,
               created_at,updated_at)
            VALUES('operation:garen-janna','squad:garen','actor:garen','actor:janna',
                   'assignment:janna-shotcaller','prepared','explicit',?,'[]','{}',?,?,1,1,
                   'evidence:snapshot-garen-janna',1,?,?)
            """,
            (HASH, HASH, HASH, AT3, AT3),
        )
    public_issue = "https://github.com/Vinosaamaa/league-of-orchestrator/issues/22"
    local_evidence = {"path": "/synthetic/local/evidence", "receipt": "full local proof"}
    local_hash = hashlib.sha256(
        json.dumps(local_evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    actions = {
        "issue": "created", "commit": "published_head", "pull_request": "opened",
        "check": "passed", "merge": "merged", "install": "installed", "smoke": "passed",
        "rollback": "verified", "teardown": "completed", "authority": "used",
        "handoff": "recorded", "continuation": "recorded",
    }
    public_urls = {
        "issue": public_issue,
        "commit": "https://github.com/Vinosaamaa/league-of-orchestrator/commit/" + HASH,
        "pull_request": "https://github.com/Vinosaamaa/league-of-orchestrator/pull/99",
        "check": "https://github.com/Vinosaamaa/league-of-orchestrator/actions/runs/99",
        "merge": "https://github.com/Vinosaamaa/league-of-orchestrator/commit/" + HASH,
    }
    for kind, action in actions.items():
        store.record_activity_evidence(
            evidence(
                f"evidence:{kind}", kind, action, AT4,
                public_url=public_urls.get(kind),
                owning_issue_url=public_issue if kind == "issue" else None,
                local_evidence_ref="evidence:local-proof" if kind == "issue" else None,
                local_evidence=local_evidence if kind == "issue" else None,
                local_evidence_hash=local_hash if kind == "issue" else None,
            )
        )
    store.record_activity_evidence(
        evidence(
            "evidence:deploy-pending", "deployment", "publish", AT4,
            state="pending", verification="unknown", required_for_completion=True,
        )
    )
    store.record_activity_evidence(
        evidence(
            "evidence:squad-authority-pending", "authority", "squad_release", AT4,
            owner_agent_id=None, project_id=None, request_id=None, task_id=None,
            state="pending", verification="verified", required_for_completion=True,
        )
    )
    store.record_activity_evidence(
        evidence(
            "evidence:squad-authority-optional", "authority", "squad_release", AT5,
            owner_agent_id=None, project_id=None, request_id=None, task_id=None,
            state="succeeded", verification="verified", required_for_completion=False,
        )
    )
    for phase, identifier, state in (
        ("failure", "evidence:repair-failure", "failed"),
        ("fix", "evidence:repair-fix", "succeeded"),
        ("final", "evidence:repair-final", "succeeded"),
    ):
        store.record_activity_evidence(
            evidence(
                identifier, "repair", "repair", AT5, state=state,
                stable_repair_id="repair:ci-retry", repair_phase=phase,
                root_cause_tag="repair:transient-check",
            )
        )


def finish(store: SQLiteStorage) -> None:
    with store._transaction():
        store.connection.execute("UPDATE requests SET state='cancelled',updated_at=?", (AT6,))
        store.connection.execute(
            """
            INSERT INTO task_transitions
              (transition_id,transition_key,task_id,from_state,to_state,update_text,next_action,created_at,event_id)
            VALUES('evidence:transition-complete','transition:complete','task:reporting','working','complete',
                   'Synthetic completion','None',?,'event:transition-complete')
            """,
            (AT6,),
        )
        store.connection.execute(
            """
            INSERT INTO task_transitions
              (transition_id,transition_key,task_id,from_state,to_state,update_text,next_action,created_at,event_id)
            VALUES('evidence:transition-privacy-complete','transition:privacy-complete','task:privacy','working','complete',
                   'Synthetic privacy completion','None',?,'event:transition-privacy-complete')
            """,
            (AT6,),
        )
        store.connection.execute(
            "UPDATE task_resources SET state='released',updated_at=? WHERE resource_id='resource:worktree'",
            (AT6,),
        )
        store.connection.execute(
            "UPDATE cleanup_obligations SET cleanup_state='completed',updated_at=? WHERE task_id='task:reporting'",
            (AT6,),
        )
        store.connection.execute(
            "UPDATE cleanup_operations SET state='completed',fence=1,updated_at=? WHERE operation_id='operation:cleanup-reporting'",
            (AT6,),
        )
        store.connection.execute(
            "UPDATE cleanup_actions SET state='completed' WHERE action_id='cleanup:release-worktree'"
        )
        store.connection.execute(
            """
            UPDATE callsign_assignments
               SET state='active',acceptance_digest=?,version=2,activated_at=?
             WHERE callsign_assignment_id='assignment:janna-shotcaller'
            """,
            (HASH, AT6),
        )
        store.connection.execute(
            """
            UPDATE squads SET shotcaller_agent_id='actor:janna',owner_fence=2,version=2,updated_at=?
             WHERE squad_id='squad:garen'
            """,
            (AT6,),
        )
        store.connection.execute(
            "UPDATE shotcaller_intake SET state='closed',version=2,updated_at=? WHERE agent_id='actor:garen'",
            (AT6,),
        )
        store.connection.execute(
            """
            INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
            VALUES('actor:janna','squad:garen','accepting',2,1,?)
            """,
            (AT6,),
        )
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,squad_id,entity_version,event_type,status,update_text,occurred_at,detail_json,
               aggregate_kind,aggregate_id,source_event_id)
            VALUES('event:owner-garen-janna','squad:garen',2,'owner_changed','complete',
                   'Synthetic owner handoff',?,'{}','rollover','operation:garen-janna',
                   'event:synthetic-authority')
            """,
            (AT6,),
        )
        store.connection.execute(
            """
            UPDATE rollover_operations
               SET state='completed',acknowledgement_digest=?,owner_event_id='event:owner-garen-janna',
                   switch_receipt_digest=?,cleanup_receipt_digest=?,version=4,updated_at=?
             WHERE operation_id='operation:garen-janna'
            """,
            (HASH, HASH, HASH, AT6),
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_action_receipts
              (action_id,operation_id,fence,outcome,before_json,after_json,adapter_receipt_json,
               receipt_hash,recorded_at)
            VALUES('cleanup:release-worktree','operation:cleanup-reporting',1,'applied','{}','{}','{}',?,?)
            """,
            ("b" * 64, AT6),
        )
        store.connection.execute(
            """
            INSERT INTO teardown_receipts
              (receipt_id,operation_id,task_id,policy_version,receipt_hash,completed_at)
            VALUES('evidence:teardown-canonical','operation:cleanup-reporting','task:reporting',?,?,?)
            """,
            (HASH, "c" * 64, AT6),
        )
    store.record_activity_evidence(
        evidence(
            "evidence:deploy-success", "deployment", "publish", AT6,
            state="succeeded", verification="verified", required_for_completion=True,
        )
    )
    store.record_activity_evidence(
        evidence(
            "evidence:squad-authority-success", "authority", "squad_release", AT6,
            owner_agent_id=None, project_id=None, request_id=None, task_id=None,
            state="succeeded", verification="verified", required_for_completion=True,
        )
    )


def generate(store: SQLiteStorage, **overrides):
    values = {
        "from_at": AT0,
        "to_at": AT20,
        "timezone_name": "America/Los_Angeles",
        "from_inclusive": True,
        "scope_kind": "all",
        "scope_id": None,
        "limit": 1000,
        "cursor": None,
        "local_diagnostic": False,
    }
    values.update(overrides)
    return store.generate_report(**values)


def test_report_contract(root: Path) -> None:
    report_schema = json.loads(
        (ROOT / "schema" / "league-report.schema.json").read_text(encoding="utf-8")
    )
    definitions = report_schema["$defs"]
    assert definitions["fact"]["properties"]["details"] == {
        "$ref": "#/$defs/details"
    }
    assert definitions["details"]["maxProperties"] == 32
    assert "facts" not in definitions["owner_group"]["properties"]
    assert definitions["owner_group"]["properties"]["fact_ids"]["maxItems"] == 1000
    assert definitions["repair_group"]["properties"]["underlying_evidence"]["maxItems"] == 100
    state, _ = migrated_state(root, "report-contract")
    with SQLiteStorage(state) as store:
        seed(store)
        malformed_url = evidence("evidence:malformed-url", "issue", "observe", AT1)
        malformed_url["owning_issue_url"] = 7
        try:
            store.record_activity_evidence(malformed_url)
        except StorageRefusal as exc:
            assert exc.code == "invalid_evidence"
        else:
            raise AssertionError("malformed owning issue URL escaped typed refusal")
        oversized_local = "x" * 4097
        oversized_payload = evidence("evidence:oversized-local", "issue", "observe", AT1)
        oversized_payload.update(
            local_evidence_ref="evidence:oversized-local-value",
            local_evidence={"value": oversized_local},
            local_evidence_hash=hashlib.sha256(
                json.dumps(
                    {"value": oversized_local}, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        )
        try:
            store.record_activity_evidence(oversized_payload)
        except StorageRefusal as exc:
            assert exc.code == "invalid_evidence"
        else:
            raise AssertionError("unbounded local evidence entered report details")
        unfinished = generate(store)
        assert unfinished["completion"]["everything_finished"] is False
        gates = {item["kind"]: item["count"] for item in unfinished["completion"]["gates"]}
        assert gates["unresolved_requests"] == 3
        assert gates["pending_resources"] == 1 and gates["pending_cleanup"] == 1
        assert gates["pending_handoff"] == 1
        assert gates["pending_deployment"] == 1 and gates["evidence_gaps"] >= 2
        squad_unfinished = generate(store, scope_kind="squad", scope_id="squad:garen")
        squad_gates = {
            item["kind"]: item["count"] for item in squad_unfinished["completion"]["gates"]
        }
        assert squad_gates["pending_authority_or_release"] == 1
        assert any(
            fact["action"] == "squad_release" for fact in squad_unfinished["chronological"]
        )
        owner_unfinished = generate(store, scope_kind="owner", scope_id="Orianna")
        assert not any(
            fact["action"] == "squad_release" for fact in owner_unfinished["chronological"]
        )
        assert unfinished["chronological"] == sorted(
            unfinished["chronological"], key=lambda item: (item["occurred_at"], item["fact_id"])
        )
        categories = set(unfinished["totals"]["by_category"])
        assert {
            "prompt", "direct_work", "champion_assignment", "callsign_assignment",
            "model_routing", "runtime", "handoff", "teardown", "authority",
        } <= categories
        assert any(
            fact["fact_id"] == "evidence:rollover:operation:garen-janna"
            and fact["action"] == "rollover_prepared"
            for fact in unfinished["chronological"]
        )
        assignments = [
            fact for fact in unfinished["chronological"]
            if fact["category"] == "champion_assignment"
        ]
        assert {fact["details"]["callsign"] for fact in assignments} == {"Orianna", "Lux"}
        assert {(fact["details"]["model"], fact["details"]["effort"], fact["details"]["harness"], fact["details"]["backend"]) for fact in assignments} == {
            ("gpt-synthetic", "high", "codex", "herdr"),
            ("gpt-synthetic-fast", "medium", "pi", "tmux"),
        }
        linked_categories = {
            fact["category"] for fact in unfinished["chronological"]
            if any(link["kind"] == "public_url" for link in fact["evidence"])
        }
        assert {"issue", "commit", "pull_request", "check", "merge"} <= linked_categories
        repairs = unfinished["recurring_repairs"]["groups"]
        assert len(repairs) == 1 and repairs[0]["stable_id"] == "repair:ci-retry"
        assert repairs[0]["phases"] == {"failure": 1, "final": 1, "fix": 1}
        original_repair_bound = report_ops.MAX_REPAIR_EVIDENCE
        try:
            report_ops.MAX_REPAIR_EVIDENCE = 2
            sampled = generate(store, persist=False)["recurring_repairs"]["groups"][0]
        finally:
            report_ops.MAX_REPAIR_EVIDENCE = original_repair_bound
        assert len(sampled["underlying_evidence"]) == 2
        assert sampled["evidence_truncated"] is True and sampled["repetitions"] == 3
        assert all(group["fact_ids"] for group in unfinished["owner_grouped"])
        assert {
            fact_id
            for group in unfinished["owner_grouped"]
            for fact_id in group["fact_ids"]
        } == {fact["fact_id"] for fact in unfinished["chronological"]}
        assert "/synthetic/" not in render_report(unfinished, "json").decode()
        local = generate(store, local_diagnostic=True)
        assert "/synthetic/local/evidence" in json.dumps(local)
        assert local["report"]["content_hash"] == unfinished["report"]["content_hash"]
        inspection = json.loads(
            store.export_bytes(format_name="json", purpose="inspection", max_records=1000)
        )
        issue_row = next(
            row
            for row in inspection["tables"]["activity_evidence"]
            if row["evidence_id"] == "evidence:issue"
        )
        expected_local_hash = hashlib.sha256(
            json.dumps(
                {"path": "/synthetic/local/evidence", "receipt": "full local proof"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert issue_row["local_evidence_json"] == "[redacted]"
        assert issue_row["local_evidence_ref"] == "[redacted]"
        assert issue_row["local_evidence_hash"] == expected_local_hash
        rollback = store.export_bytes(
            format_name="json", purpose="rollback", max_records=1000
        )
        assert b"/synthetic/local/evidence" in rollback
        assert expected_local_hash.encode() in rollback

        finish(store)
        complete = generate(store)
        assert complete["completion"]["everything_finished"] is True
        assert complete["completion"]["status"] == "finished"
        complete_gates = {
            item["kind"]: item["count"] for item in complete["completion"]["gates"]
        }
        assert complete_gates["pending_handoff"] == 0
        assert any(
            fact["fact_id"] == "evidence:owner-change:event:owner-garen-janna"
            and fact["action"] == "owner_changed"
            and fact["owner"]["actor_id"] == "actor:janna"
            for fact in complete["chronological"]
        )
        assert any(
            fact["fact_id"] == "evidence:rollover:operation:garen-janna"
            and fact["action"] == "rollover_completed"
            and fact["details"]["callsign_assignment_id"] == "assignment:janna-shotcaller"
            for fact in complete["chronological"]
        )
        squad_complete = generate(store, scope_kind="squad", scope_id="squad:garen")
        assert squad_complete["completion"]["everything_finished"] is True
        assert any(
            fact["category"] == "resource"
            and fact["action"] == "release"
            and fact["state"] == "applied"
            and fact["occurred_at"] == AT6
            and fact["details"]["receipt_hash"] == "b" * 64
            for fact in complete["chronological"]
        )
        assert any(
            fact["category"] == "teardown"
            and fact["details"].get("receipt_hash") == "c" * 64
            for fact in complete["chronological"]
        )
        json_payload = render_report(complete, "json")
        markdown = render_report(complete, "markdown").decode()
        html = render_report(complete, "html").decode()
        assert json.loads(json_payload)["report"]["content_hash"] == complete["report"]["content_hash"]
        assert complete["report"]["content_hash"] in markdown and complete["report"]["content_hash"] in html
        assert "EVERYTHING FINISHED" in html and "http://" not in html and "<script" not in html
        marker_report = json.loads(json.dumps(complete))
        marker_report["chronological"][0]["summary"] = (
            "literal {{OWNERS}} and {{FOO_BAR}} evidence"
        )
        marker_html = render_report(marker_report, "html").decode()
        assert "literal {{OWNERS}} and {{FOO_BAR}} evidence" in marker_html

        for scope_kind, scope_id in (
            ("owner", "Orianna"), ("squad", "squad:garen"), ("project", "project:league"), ("all", None)
        ):
            scoped = generate(store, scope_kind=scope_kind, scope_id=scope_id)
            assert scoped["report"]["scope"]["kind"] == scope_kind
            assert scoped["totals"]["facts"] > 0

        first = generate(store, limit=5)
        assert first["pagination"]["next_cursor"]
        source_calls = 0
        original_sources = report_ops._source_facts

        def counted_sources(*args, **kwargs):
            nonlocal source_calls
            assert store.connection.in_transaction
            source_calls += 1
            return original_sources(*args, **kwargs)

        report_ops._source_facts = counted_sources
        try:
            second = generate(store, limit=5, cursor=first["pagination"]["next_cursor"])
        finally:
            report_ops._source_facts = original_sources
        assert source_calls == 1
        assert not ({item["fact_id"] for item in first["chronological"]} & {item["fact_id"] for item in second["chronological"]})

        stored = store.report_spec(complete["report"]["report_id"])
        assert stored and stored["content_hash"] == complete["report"]["content_hash"]
        reproduced = store.generate_report(
            from_at=stored["from_at"], to_at=stored["to_at"], timezone_name=stored["timezone"],
            from_inclusive=bool(stored["from_inclusive"]), scope_kind=stored["scope_kind"],
            scope_id=stored["scope_id"], limit=1000, cursor=None, local_diagnostic=False,
            report_id=stored["report_id"], event_watermark=stored["event_watermark"], persist=False,
            expected_content_hash=stored["content_hash"],
        )
        assert reproduced["report"]["reproduction"]["matches_stored_hash"] is True
        since = store.generate_report(
            from_at=stored["to_at"], to_at="2026-08-28T07:30:00Z", timezone_name=stored["timezone"],
            from_inclusive=False, scope_kind="all", scope_id=None, limit=1000, cursor=None,
            local_diagnostic=False,
        )
        assert since["report"]["from_inclusive"] is False and since["totals"]["facts"] == 0
        # Simulate a historical source repair that preserves the coarse table
        # watermark; a new immutable report still needs a distinct identity.
        with store._transaction():
            store.connection.execute(
                "UPDATE activity_evidence SET summary=? WHERE evidence_id=?",
                ("Synthetic corrected deployment evidence", "evidence:deploy-success"),
            )
        fresh = store.generate_report(
            from_at=stored["from_at"], to_at=stored["to_at"], timezone_name=stored["timezone"],
            from_inclusive=bool(stored["from_inclusive"]), scope_kind=stored["scope_kind"],
            scope_id=stored["scope_id"], limit=1000, cursor=None, local_diagnostic=False,
        )
        assert fresh["report"]["report_id"] != stored["report_id"]
        assert fresh["report"]["source_watermark"] == stored["source_watermark"]
        mismatch = store.generate_report(
            from_at=stored["from_at"], to_at=stored["to_at"], timezone_name=stored["timezone"],
            from_inclusive=bool(stored["from_inclusive"]), scope_kind=stored["scope_kind"],
            scope_id=stored["scope_id"], limit=1000, cursor=None, local_diagnostic=False,
            report_id=stored["report_id"], event_watermark=stored["event_watermark"], persist=False,
            expected_content_hash=stored["content_hash"],
        )
        assert mismatch["report"]["reproduction"]["matches_stored_hash"] is False
        assert mismatch["completion"]["status"] == "unknown"
        mismatch_gates = {
            item["kind"]: item for item in mismatch["completion"]["gates"]
        }
        assert mismatch_gates["evidence_gaps"]["count"] == 1
        assert mismatch_gates["evidence_gaps"]["status"] == "unknown"

    raw = invoke_cli(
        state, "report", "show", complete["report"]["report_id"], "--format", "json", raw=True
    )
    assert json.loads(raw)["report"]["report_id"] == complete["report"]["report_id"]
    since_cli = invoke_cli(
        state, "report", "--since-report", complete["report"]["report_id"],
        "--to", "2026-08-28T07:30:00Z", "--format", "json", raw=True,
    )
    assert json.loads(since_cli)["report"]["from_inclusive"] is False

    class FixedDateTime:
        @classmethod
        def now(cls, zone):
            return datetime(2026, 8, 28, 12, 0, 0, tzinfo=zone)

    original_datetime = league_cli.datetime
    try:
        league_cli.datetime = FixedDateTime
        today_cli = invoke_cli(
            state, "report", "--today", "--timezone", "America/Los_Angeles",
            "--all", "--format", "json", raw=True,
        )
    finally:
        league_cli.datetime = original_datetime
    today = json.loads(today_cli)["report"]
    assert today["from"] == "2026-08-28T07:00:00Z"
    assert today["to"] == "2026-08-28T19:00:00Z"
    assert today["timezone"] == "America/Los_Angeles"
    for arguments in (
        ("report", "--today", "--timezone", "Invalid/Timezone", "--all"),
        (
            "report", "--since-report", complete["report"]["report_id"],
            "--timezone", "Invalid/Timezone",
        ),
    ):
        refused_timezone = invoke_cli(state, *arguments, expected=2)
        assert refused_timezone["error"]["code"] == "invalid_report_timezone"


def test_completion_scan_is_bounded(root: Path) -> None:
    state, _ = migrated_state(root, "report-completion-bound")
    with SQLiteStorage(state) as store:
        with store._transaction():
            store.connection.execute(
                """
                INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at)
                VALUES('Bounded','shotcaller',1,1,NULL)
                """
            )
            store.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,kind,status,version,updated_at,update_text,next_action,metadata_json)
                VALUES('actor:bounded','Bounded','shotcaller','synthetic','working',1,?,
                       'Synthetic bound owner','Continue','{}')
                """,
                (AT0,),
            )
            store.connection.executemany(
                """
                INSERT INTO requests
                  (request_id,summary,requester_agent_id,owner_agent_id,execution_mode,
                   state,version,created_at,updated_at)
                VALUES(?,?,'actor:bounded','actor:bounded','direct','open',1,?,?)
                """,
                tuple(
                    (f"request:bounded-{index}", "Synthetic pending request", AT0, AT0)
                    for index in range(4)
                ),
            )
        original_bound = report_ops.MAX_REPORT_FACTS
        try:
            report_ops.MAX_REPORT_FACTS = 3
            try:
                generate(store, from_at=AT20, to_at="2026-08-28T07:30:00Z")
            except StorageRefusal as exc:
                assert exc.code == "report_completion_too_large"
            else:
                raise AssertionError("completion obligations escaped the report row bound")
        finally:
            report_ops.MAX_REPORT_FACTS = original_bound


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-reporting-") as temporary:
        test_report_contract(Path(temporary))
        test_completion_scan_is_bounded(Path(temporary))
    print("PASS: deterministic scopes, completion gates, pagination, since/show reproduction, and JSON-derived renderers")


if __name__ == "__main__":
    main()
