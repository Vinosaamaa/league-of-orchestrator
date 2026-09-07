#!/usr/bin/env python3
"""Async Herdr restart replay from canonical League state."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.display_replay import canonical_presentations, replay_restored_display  # noqa: E402
from league.multiplexer_adapters import (  # noqa: E402
    CommandRunner,
    MULTIPLEXER_OPERATIONS,
    MULTIPLEXER_OPERATION_METHODS,
    MultiplexerAdapter,
    RestoredEndpoint,
    builtin_multiplexer_adapter_registry,
)
from league.multiplexer_adapters.herdr.adapter import MAX_TOKENS_PER_REPORT  # noqa: E402
from league.restored_agent import reconcile_restored_agents  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


AT = "2026-01-01T00:00:00Z"
REPOSITORY = "https://example.invalid/league-84.git"
SESSIONS = {
    "Ashe": "11111111-1111-4111-8111-111111111111",
    "Azir": "66666666-6666-4666-8666-666666666666",
    "Qiyana": "77777777-7777-4777-8777-777777777777",
    "Ambessa": "22222222-2222-4222-8222-222222222222",
    "Heimerdinger": "33333333-3333-4333-8333-333333333333",
    "KaiSa": "44444444-4444-4444-8444-444444444444",
}
SHOTCALLER_PRESENTATION = {
    "Ashe": {"project_code": "Projects", "worktree_name": "Projects"},
    "Azir": {"project_code": "IA", "worktree_name": "interview-arc"},
    "Qiyana": {"project_code": "JJ", "worktree_name": "job-journey"},
}
SHOTCALLER_SQUADS = {
    "Ashe": "squad:Garen",
    "Azir": "squad:Azir",
    "Qiyana": "squad:Qiyana",
}


def stable(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RestoredHerdr:
    """Synthetic post-restart Herdr: same sessions/processes, new terminals."""

    def __init__(self, presentations: list[dict], *, source_less: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.source_less = source_less
        self.unavailable_reads = 1
        self.agents: list[dict] = []
        self.processes: dict[str, dict] = {}
        for index, presentation in enumerate(presentations, start=1):
            kind = presentation["agent_adapter_kind"]
            pane_id = f"restart:p{index}"
            terminal_id = f"terminal:restart:{index}"
            agent = {
                    "name": f"native-{index}",
                    "agent": kind,
                    "agent_status": "idle",
                    "agent_session": {
                        "source": presentation["applies_to_source"],
                        "value": presentation["session_ref"],
                        "kind": "path" if kind == "pi" else "id",
                    },
                    "workspace_id": "restart:w1",
                    "tab_id": f"restart:t{index}",
                    "pane_id": pane_id,
                    "terminal_id": terminal_id,
                    "cwd": presentation["cwd"],
                    "foreground_cwd": presentation["cwd"],
                    "state_change_seq": 40 + index,
                    "terminal_title": f"native-{kind}",
                    "terminal_title_stripped": f"native-{kind}",
                    "tokens": {},
                }
            if not source_less:
                agent["display_agent"] = kind
                agent["metadata_source"] = presentation["applies_to_source"]
            self.agents.append(agent)
            self.processes[pane_id] = {
                "pid": 7000 + index,
                "process_start": f"synthetic-start-{index}",
                "argv0": (
                    "pi" if kind == "pi" else "cursor-agent" if kind == "cursor" else kind
                ),
                "argv": [kind],
                "cwd": presentation["cwd"],
            }

    @staticmethod
    def completed(arguments, result=None, returncode=0):
        output = "" if result is None else stable({"ok": True, "result": result})
        return subprocess.CompletedProcess(arguments, returncode, output, "")

    def _agent(self, target: str) -> dict:
        matches = [
            agent
            for agent in self.agents
            if target in {agent["name"], agent["pane_id"]}
        ]
        assert len(matches) == 1, (target, matches)
        return matches[0]

    def run(self, arguments, timeout_seconds=30):
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        if Path(command[0]).name == "herdr" and command[1:3] == ("agent", "list"):
            if self.unavailable_reads:
                self.unavailable_reads -= 1
                return self.completed(command, {"agents": []})
            return self.completed(command, {"agents": self.agents})
        if Path(command[0]).name == "herdr" and command[1:3] == ("agent", "get"):
            return self.completed(command, {"agent": self._agent(command[3])})
        if Path(command[0]).name == "herdr" and command[1:3] == ("pane", "get"):
            agent = self._agent(command[3])
            pane = {
                key: agent[key]
                for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")
            }
            return self.completed(command, {"pane": pane})
        if Path(command[0]).name == "herdr" and command[1:3] == ("pane", "process-info"):
            pane_id = command[command.index("--pane") + 1]
            return self.completed(
                command,
                {"process_info": {"foreground_processes": [self.processes[pane_id]]}},
            )
        if Path(command[0]).name == "herdr" and command[1:3] == ("agent", "rename"):
            self._agent(command[3])["name"] = command[4]
            return self.completed(command)
        if Path(command[0]).name == "herdr" and command[1:3] == ("pane", "report-metadata"):
            agent = self._agent(command[3])
            assert command[command.index("--applies-to-source") + 1] == agent["agent_session"]["source"]
            for index, item in enumerate(command[:-1]):
                if item == "--token":
                    key, value = command[index + 1].split("=", 1)
                    agent["tokens"][key] = value
            if not self.source_less:
                agent["metadata_source"] = command[command.index("--source") + 1]
                agent["display_agent"] = command[command.index("--display-agent") + 1]
            agent["terminal_title"] = command[command.index("--title") + 1]
            agent["terminal_title_stripped"] = agent["terminal_title"]
            agent["state_change_seq"] = int(command[command.index("--seq") + 1])
            return self.completed(command)
        raise AssertionError(command)


class RestoredWatcher:
    """Synthetic exact Shotcaller watcher binding with idempotent retry."""

    def __init__(self) -> None:
        self.bindings: dict[str, tuple[str, str]] = {}

    def preflight(self, presentation):
        return {"agent_id": presentation["agent_id"], "live": True}

    def bind(self, presentation, runtime_receipt, preflight):
        assert preflight == {"agent_id": presentation["agent_id"], "live": True}
        exact = (
            runtime_receipt["endpoint"], runtime_receipt["runtime_generation"]
        )
        previous = self.bindings.get(presentation["agent_id"])
        self.bindings[presentation["agent_id"]] = exact
        return {
            "watcher_id": "watcher:synthetic",
            "wake_locator_verified": True,
            "idempotent": previous == exact,
        }

    def verify(self, presentation, runtime_receipt):
        assert self.bindings[presentation["agent_id"]] == (
            runtime_receipt["endpoint"],
            runtime_receipt["runtime_generation"],
        )
        return {"watcher_live": True, "fence": 1}


class FailingMetadataHerdr(RestoredHerdr):
    def __init__(self, presentations):
        super().__init__(presentations)
        self.fail_metadata_once = True

    def run(self, arguments, timeout_seconds=30):
        command = tuple(arguments)
        if (
            self.fail_metadata_once
            and Path(command[0]).name == "herdr"
            and command[1:3] == ("pane", "report-metadata")
        ):
            self.fail_metadata_once = False
            self.calls.append(command)
            return self.completed(command, returncode=1)
        return super().run(arguments, timeout_seconds=timeout_seconds)


class FailingFinalInventoryHerdr(RestoredHerdr):
    def __init__(self, presentations):
        super().__init__(presentations)
        self.unavailable_reads = 0
        self.fail_final_once = True

    def run(self, arguments, timeout_seconds=30):
        command = tuple(arguments)
        if (
            self.fail_final_once
            and command[1:3] == ("agent", "list")
            and all(agent["tokens"] for agent in self.agents)
        ):
            self.fail_final_once = False
            self.calls.append(command)
            return self.completed(command, {"agents": []})
        return super().run(arguments, timeout_seconds=timeout_seconds)


class FailingWatcher(RestoredWatcher):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def bind(self, presentation, runtime_receipt, preflight):
        if self.fail_once:
            self.fail_once = False
            raise StorageRefusal(
                "synthetic_watcher_failure", "synthetic watcher bind failed"
            )
        return super().bind(presentation, runtime_receipt, preflight)


class FailingCASStore:
    def __init__(self, store: SQLiteStorage) -> None:
        self.store = store
        self.fail_once = True

    def __getattr__(self, name):
        return getattr(self.store, name)

    def reconcile_restored_runtime(self, *args, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise StorageRefusal("synthetic_cas_failure", "synthetic CAS failed")
        return self.store.reconcile_restored_runtime(*args, **kwargs)


def _project_and_agents(
    store: SQLiteStorage,
    root: Path,
    *,
    shotcaller_callsign: str = "Ashe",
    include_champions: bool = True,
) -> dict[str, Path]:
    shotcaller_squad_id = SHOTCALLER_SQUADS[shotcaller_callsign]
    project_root = root / "project"
    project_root.mkdir(parents=True)
    store.put_project(
        "project:league-84",
        expected_version=0,
        summary="League adapter lifecycle",
        repository=REPOSITORY,
        root=str(project_root),
        code="LOO",
        aliases=(),
        state="active",
        repository_visibility="private",
        export_policy="deny",
        at=AT,
    )
    shotcaller_presentation = SHOTCALLER_PRESENTATION[shotcaller_callsign]
    shotcaller_root = root / str(shotcaller_presentation["worktree_name"])
    shotcaller_root.mkdir(parents=True)
    if shotcaller_callsign != "Ashe":
        store.put_project(
            f"project:shotcaller-{shotcaller_callsign.lower()}",
            expected_version=0,
            summary=f"{shotcaller_callsign} synthetic project",
            repository=(
                f"https://example.invalid/{shotcaller_callsign.lower()}.git"
            ),
            root=str(shotcaller_root),
            code=str(shotcaller_presentation["project_code"]),
            aliases=(),
            state="active",
            repository_visibility="private",
            export_policy="deny",
            at=AT,
        )
    worktrees: dict[str, Path] = {}
    champions = ("Ambessa", "Heimerdinger", "KaiSa") if include_champions else ()
    callsigns = (shotcaller_callsign, *champions)
    for callsign in callsigns:
        role = "shotcaller" if callsign == shotcaller_callsign else "champion"
        worktree = (
            shotcaller_root
            if role == "shotcaller"
            else root / "worktrees" / callsign.lower()
        )
        worktree.mkdir(parents=True, exist_ok=True)
        worktrees[callsign] = worktree
        kind = "codex-thread" if role == "shotcaller" else "pi-thread"
        provider = (
            "codex"
            if role == "shotcaller" or callsign == "Heimerdinger"
            else "cursor"
        )
        task_id = None if role == "shotcaller" else f"task:{callsign.lower()}"
        store.connection.execute(
            "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position) VALUES(?,?,1,?)",
            (callsign, role, len(worktrees)),
        )
        if task_id is not None:
            store.connection.execute(
                """
                INSERT INTO tasks(task_id,project_id,summary,state,version,updated_at)
                VALUES(?,?,'Synthetic adapter restart','working',1,?)
                """,
                (task_id, "project:league-84", AT),
            )
        metadata = "{}"
        if role == "shotcaller":
            baseline = {
                "schema": "league.shotcaller-bootstrap-baseline.v2",
                "terminal_id": "terminal:before-restart",
                "endpoint_generation": "herdr:before-restart",
                "state_change_seq": 9,
                "routing_name": None,
                "sidebar_name": "",
                "thread_title": "",
                "title": "League",
                "presentation_source": "herdr:codex",
            }
            slug = callsign.lower()
            assignment_id = f"callsign-assignment:{slug}"
            publication = {
                "schema": "league.shotcaller-bootstrap-publication.v1",
                "assignment_id": assignment_id,
                "agent_id": f"agent:{slug}",
                "callsign": callsign,
                "routing_name": slug,
                "terminal_id": "terminal:before-restart",
                "endpoint_generation": "herdr:before-restart",
                "session_identity": SESSIONS[callsign],
                "worktree": str(worktree),
                "presentation_source": "herdr:codex",
                "title": callsign,
                "sidebar_name": callsign,
                "thread_title": callsign,
                "baseline_digest": hashlib.sha256(stable(baseline).encode()).hexdigest(),
                "observed_state_change_seq": 10,
            }
            metadata = stable(
                {
                    "scope_kind": "shotcaller",
                    "scope_id": f"agent:{slug}",
                    "shotcaller_bootstrap_baseline": baseline,
                    "shotcaller_bootstrap_publication": publication,
                    "shotcaller_bootstrap_runtime_id": f"runtime:{slug}",
                }
            )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,task_id,kind,thread_id,backend,routing_name,
               display_agent,address,repository,worktree,status,version,updated_at,
               update_text,next_action,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'Synthetic restart','Verify replay',?)
            """,
            (
                f"agent:{callsign.lower()}", callsign, role, task_id, kind,
                SESSIONS[callsign], "herdr", callsign.lower(), provider,
                f"before:{callsign.lower()}",
                None if role == "shotcaller" else REPOSITORY,
                None if role == "shotcaller" else str(worktree),
                "working", AT,
                metadata,
            ),
        )
        session_ref = (
            SESSIONS[callsign]
            if role == "shotcaller"
            else str((root / "sessions" / f"{SESSIONS[callsign]}.jsonl").resolve())
        )
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,
               session_ref,endpoint,runtime_generation,status,verified,last_seen_at)
            VALUES(?,?,?,?,?,?,'generation:before-restart','active',1,?)
            """,
            (
                f"runtime:{callsign.lower()}", f"agent:{callsign.lower()}",
                "codex" if role == "shotcaller" else "pi", "herdr",
                session_ref, f"before:{callsign.lower()}", AT,
            ),
        )
    store.connection.execute(
        """
        INSERT INTO callsign_assignments
          (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
           role,scope_kind,scope_id,state,queue_version,requirements_json,
           acceptance_digest,version,reserved_at,activated_at)
        VALUES(?,?,?,?,?,'shotcaller',?,?,'active',1,
               '[]','synthetic-acceptance',2,?,?)
        """,
        (
            f"callsign-assignment:{shotcaller_callsign.lower()}",
            shotcaller_callsign,
            f"agent:{shotcaller_callsign.lower()}",
            f"agent:{shotcaller_callsign.lower()}",
            f"runtime:{shotcaller_callsign.lower()}",
            (
                "squad"
                if shotcaller_callsign in {"Ashe", "Qiyana"}
                else "shotcaller"
            ),
            (
                shotcaller_squad_id
                if shotcaller_callsign in {"Ashe", "Qiyana"}
                else f"agent:{shotcaller_callsign.lower()}"
            ),
            AT,
            AT,
        ),
    )
    store.connection.execute(
        """
        INSERT INTO squads
          (squad_id,shotcaller_agent_id,state,version,updated_at,owner_fence)
        VALUES(?,?,'active',1,?,1)
        """,
        (
            shotcaller_squad_id,
            f"agent:{shotcaller_callsign.lower()}",
            AT,
        ),
    )
    if shotcaller_callsign != "Ashe":
        store.connection.execute(
            """
            INSERT INTO project_squad_suggestions
              (project_id,squad_id,position,created_at,updated_at)
            VALUES(?,?,0,?,?)
            """,
            (
                f"project:shotcaller-{shotcaller_callsign.lower()}",
                shotcaller_squad_id,
                AT,
                AT,
            ),
        )
    return worktrees


def _pi_descriptors(store: SQLiteStorage, root: Path, worktrees: dict[str, Path]) -> None:
    sessions_root = root / "sessions"
    sessions_root.mkdir()
    for callsign in ("Ambessa", "Heimerdinger", "KaiSa"):
        session_id = SESSIONS[callsign]
        session_path = str((sessions_root / f"{session_id}.jsonl").resolve())
        provider = "codex" if callsign == "Heimerdinger" else "cursor"
        descriptor_id = f"pi-launch:assignment:{callsign.lower()}"
        descriptor = {
            "schema": "league.pi-launch-descriptor.v1",
            "descriptor_id": descriptor_id,
            "assignment_id": f"assignment:{callsign.lower()}",
            "runtime_kind": "pi",
            "provider_kind": provider,
            "model": "synthetic-model",
            "effort": "high",
            "cwd": str(worktrees[callsign]),
            "worktree_binding": hashlib.sha256(str(worktrees[callsign]).encode()).hexdigest(),
            "role": "champion",
            "placement": "new_tab",
            "callsign": callsign,
            "project_code": "LOO",
            "task_label": "Adapter Restart",
            "routing_name": callsign.lower(),
            "workspace_id": "before:w1",
            "creator_pane_id": None,
            "state_root": str(root.resolve()),
            "release_root": str(ROOT.resolve()),
            "launch_mode": "create",
            "requested_session_id": session_id,
            "requested_session_path": None,
            "parent_session_id": None,
            "parent_session_path": None,
        }
        prepared = store.prepare_provider_launch(descriptor, AT)
        store.bind_provider_launch(
            descriptor_id,
            prepared["version"],
            {
                "schema": "league.pi-launch-observation.v1",
                "runtime_kind": "pi",
                "provider_kind": provider,
                "session_id": session_id,
                "session_path": session_path,
                "parent_session_path": None,
                "cwd": str(worktrees[callsign]),
                "role": "champion",
                "placement": "new_tab",
                "callsign": callsign,
                "project_code": "LOO",
                "task_label": "Adapter Restart",
                "routing_name": callsign.lower(),
                "workspace_id": "before:w1",
                "tab_id": f"before:t:{callsign.lower()}",
                "pane_id": f"before:p:{callsign.lower()}",
                "terminal_id": f"before:terminal:{callsign.lower()}",
            },
            AT,
        )


def canonical_state(
    root: Path,
    *,
    shotcaller_callsign: str = "Ashe",
    include_champions: bool = True,
) -> Path:
    root.mkdir(parents=True)
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        worktrees = _project_and_agents(
            store,
            root,
            shotcaller_callsign=shotcaller_callsign,
            include_champions=include_champions,
        )
        if include_champions:
            _pi_descriptors(store, root, worktrees)
    return state


def _add_standalone_cursor_champion(store: SQLiteStorage, root: Path) -> None:
    callsign = "Vayne"
    agent_id = "agent:vayne"
    task_id = "task:vayne"
    assignment_id = "assignment:vayne"
    runtime_id = "runtime:vayne"
    session_id = "55555555-5555-4555-8555-555555555555"
    worktree = root / "worktrees" / "vayne"
    worktree.mkdir(parents=True)
    title = "Vayne · LOO|Cursor Restart"
    source = "league-launch-" + hashlib.sha256(assignment_id.encode()).hexdigest()[:16]
    display = {
        "source": source,
        "applies_to_source": "herdr:cursor",
        "state_change_seq": 12,
        "sidebar_name": callsign,
        "task_label": "Cursor Restart",
        "thread_title": title,
        "terminal_title": title,
    }
    store.connection.execute(
        "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position) VALUES(?,'champion',1,99)",
        (callsign,),
    )
    store.connection.execute(
        """
        INSERT INTO tasks(task_id,project_id,summary,state,version,updated_at)
        VALUES(?, 'project:league-84','Standalone Cursor restart','working',3,?)
        """,
        (task_id, AT),
    )
    store.connection.execute(
        """
        INSERT INTO agent_instances
          (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,thread_id,backend,
           routing_name,display_agent,address,repository,worktree,status,version,
           updated_at,update_text,next_action,metadata_json)
        VALUES(?,?,'champion','agent:ashe',?,'cursor-thread',?,'herdr',?,'cursor',
               'before:vayne',?,?, 'working',1,?,'Synthetic restart','Verify replay','{}')
        """,
        (agent_id, callsign, task_id, session_id, callsign.lower(), REPOSITORY, str(worktree), AT),
    )
    store.connection.execute(
        """
        UPDATE tasks
           SET current_owner_agent_id=?,coordinator_agent_id='agent:ashe',champion_agent_id=?
         WHERE task_id=?
        """,
        (agent_id, agent_id, task_id),
    )
    store.connection.execute(
        """
        INSERT INTO runtime_instances
          (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
           endpoint,runtime_generation,status,verified,last_seen_at)
        VALUES(?,?,'cursor-thread','herdr',?,'before:vayne','generation:before-restart',
               'active',1,?)
        """,
        (runtime_id, agent_id, session_id, AT),
    )
    store.connection.execute(
        """
        INSERT INTO task_assignments
          (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,assignment_role,state,acceptance_receipt_json,
           cleanup_required,version,created_at,updated_at)
        VALUES(?,?,NULL,'agent:ashe',?,?,?,'champion','active','{}',0,3,?,?)
        """,
        (assignment_id, task_id, agent_id, runtime_id, callsign, AT, AT),
    )
    detail = {
        "bytes": 1,
        "context_sha256": "a" * 64,
        "effect_sha256": "b" * 64,
        "display_receipt": display,
    }
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,task_id,entity_version,event_type,status,update_text,occurred_at,
           detail_json,aggregate_kind,aggregate_id)
        VALUES('event:vayne:context',?,3,'assignment_context_delivered','active',
               'Synthetic Cursor context',?,?,'assignment',?)
        """,
        (task_id, AT, stable(detail), assignment_id),
    )


def report_calls(herdr: RestoredHerdr) -> list[tuple[str, ...]]:
    return [
        call
        for call in herdr.calls
        if Path(call[0]).name == "herdr" and call[1:3] == ("pane", "report-metadata")
    ]


def test_async_restart_converges_without_duplicate_processes(root: Path) -> None:
    state = canonical_state(root)
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        assert [item["agent_id"] for item in expected] == [
            "agent:ambessa", "agent:ashe", "agent:heimerdinger", "agent:kaisa"
        ]
        herdr = RestoredHerdr(expected)
        before_processes = json.loads(json.dumps(herdr.processes))
        first = replay_restored_display(
            store, herdr_runner=herdr, timeout_ms=1_000, sleeper=lambda _: None
        )
        assert first["candidate_count"] == 4
        assert first["replayed_count"] == 4
        assert first["created_processes"] == first["resumed_sessions"] == 0
        assert herdr.processes == before_processes
        assert all(
            sum(item == "--token" for item in call) <= MAX_TOKENS_PER_REPORT
            for call in report_calls(herdr)
        )
        by_session = {item["session_ref"]: item for item in expected}
        for agent in herdr.agents:
            presentation = by_session[agent["agent_session"]["value"]]
            assert agent["display_agent"] == presentation["provider_kind"]
            assert agent["terminal_title"] == presentation["title"]
            assert agent["tokens"] == presentation["tokens"]
            assert agent["terminal_id"].startswith("terminal:restart:")
            assert agent["tokens"]["orchestrator_role"] in {"shotcaller", "champion"}
            assert agent["tokens"]["project_code"] == (
                "Projects"
                if agent["tokens"]["orchestrator_role"] == "shotcaller"
                else "LOO"
            )
            assert len(agent["tokens"]["task_label"].split()) == 2
            assert agent["tokens"]["status_token"] == "working"

        reports_before_retry = len(report_calls(herdr))
        second = replay_restored_display(store, herdr_runner=herdr, timeout_ms=0)
        assert second["replayed_count"] == 0 and second["idempotent_count"] == 4
        assert len(report_calls(herdr)) == reports_before_retry
        assert not any(
            call[1:3]
            in {
                ("agent", "start"), ("agent", "prompt"), ("tab", "create"),
                ("pane", "split"), ("pane", "send-text"), ("pane", "send-keys"),
            }
            for call in herdr.calls
        )


def test_shotcallers_use_publication_cwd_and_optional_exact_project_without_process_effects(
    root: Path,
) -> None:
    for callsign in ("Ashe", "Azir", "Qiyana"):
        case = root / callsign.lower()
        state = canonical_state(case, shotcaller_callsign=callsign)
        with SQLiteStorage(state) as store:
            store.connection.execute(
                "UPDATE agent_instances SET repository=NULL WHERE role='champion'"
            )
            row = store.connection.execute(
                "SELECT repository,worktree FROM agent_instances WHERE agent_id=?",
                (f"agent:{callsign.lower()}",),
            ).fetchone()
            assert tuple(row) == (None, None)
            assignment_scope = store.connection.execute(
                """
                SELECT scope_kind,scope_id FROM callsign_assignments
                 WHERE callsign_assignment_id=?
                """,
                (f"callsign-assignment:{callsign.lower()}",),
            ).fetchone()
            assert tuple(assignment_scope) == (
                ("squad", SHOTCALLER_SQUADS[callsign])
                if callsign in {"Ashe", "Qiyana"}
                else ("shotcaller", f"agent:{callsign.lower()}")
            )
            expected = canonical_presentations(store)
            shotcaller = next(
                item
                for item in expected
                if item["agent_id"] == f"agent:{callsign.lower()}"
            )
            presentation = SHOTCALLER_PRESENTATION[callsign]
            project_code = str(presentation["project_code"])
            assert shotcaller["tokens"]["project_code"] == project_code
            assert shotcaller["title"] == f"{callsign} · {project_code}"
            assert shotcaller["cwd"] == str(
                case / str(presentation["worktree_name"])
            )
            champions = [item for item in expected if item["role"] == "champion"]
            assert {item["agent_id"] for item in champions} == {
                "agent:ambessa",
                "agent:heimerdinger",
                "agent:kaisa",
            }
            assert all(item["tokens"]["project_code"] == "LOO" for item in champions)
            assert all("|Adapter Restart" in item["title"] for item in champions)
            herdr = RestoredHerdr(expected)
            before_processes = json.loads(json.dumps(herdr.processes))
            receipt = replay_restored_display(
                store,
                herdr_runner=herdr,
                timeout_ms=1_000,
                sleeper=lambda _: None,
            )
            assert receipt["created_processes"] == receipt["resumed_sessions"] == 0
            assert herdr.processes == before_processes
            assert not any(
                call[1:3]
                in {
                    ("agent", "start"),
                    ("agent", "prompt"),
                    ("tab", "create"),
                    ("pane", "split"),
                    ("pane", "send-text"),
                    ("pane", "send-keys"),
                }
                for call in herdr.calls
            )


def test_shotcaller_publication_cwd_accepts_absent_or_matching_and_refuses_conflict(
    root: Path,
) -> None:
    for case in ("absent", "matching", "conflicting"):
        case_root = root / case
        state = canonical_state(case_root, include_champions=False)
        expected_cwd = str(case_root / "Projects")
        with SQLiteStorage(state) as store:
            if case == "matching":
                store.connection.execute(
                    "UPDATE agent_instances SET worktree=? WHERE agent_id='agent:ashe'",
                    (expected_cwd,),
                )
            elif case == "conflicting":
                conflicting_cwd = case_root / "different-project"
                conflicting_cwd.mkdir()
                store.connection.execute(
                    "UPDATE agent_instances SET worktree=? WHERE agent_id='agent:ashe'",
                    (str(conflicting_cwd),),
                )
            if case == "conflicting":
                try:
                    canonical_presentations(store)
                except StorageRefusal as exc:
                    assert exc.code == "display_replay_project_unproven"
                    assert "agent:ashe" in str(exc)
                    assert "runtime:ashe" in str(exc)
                    assert len(str(exc).encode("utf-8")) <= 320
                else:
                    raise AssertionError("conflicting Shotcaller cwd was accepted")
                continue
            expected = canonical_presentations(store)
            assert len(expected) == 1 and expected[0]["cwd"] == expected_cwd
            herdr = RestoredHerdr(expected)
            before_processes = json.loads(json.dumps(herdr.processes))
            receipt = replay_restored_display(
                store,
                herdr_runner=herdr,
                timeout_ms=1_000,
                sleeper=lambda _: None,
            )
            assert receipt["created_processes"] == receipt["resumed_sessions"] == 0
            assert herdr.processes == before_processes


def test_shotcaller_project_binding_refuses_ambiguous_or_malformed_sources(
    root: Path,
) -> None:
    for case in (
        "multiple",
        "uncoded",
        "inactive",
        "malformed-publication",
        "identity-mismatch",
    ):
        case_root = root / case
        callsign = "Azir" if case in {"multiple", "uncoded", "inactive"} else "Ashe"
        state = canonical_state(
            case_root,
            shotcaller_callsign=callsign,
            include_champions=False,
        )
        with SQLiteStorage(state) as store:
            slug = callsign.lower()
            if case == "multiple":
                second_root = case_root / "second-project"
                second_root.mkdir()
                store.put_project(
                    "project:second",
                    expected_version=0,
                    summary="Second synthetic project",
                    repository="https://example.invalid/second.git",
                    root=str(second_root),
                    code="SECOND",
                    aliases=(),
                    state="active",
                    repository_visibility="private",
                    export_policy="deny",
                    at=AT,
                )
                store.connection.execute(
                    """
                    INSERT INTO project_squad_suggestions
                      (project_id,squad_id,position,created_at,updated_at)
                    VALUES('project:second',?,0,?,?)
                    """,
                    (SHOTCALLER_SQUADS[callsign], AT, AT),
                )
            elif case == "uncoded":
                store.connection.execute(
                    "UPDATE projects SET code=NULL WHERE project_id=?",
                    (f"project:shotcaller-{slug}",),
                )
            elif case == "inactive":
                store.connection.execute(
                    "UPDATE projects SET state='retired' WHERE project_id=?",
                    (f"project:shotcaller-{slug}",),
                )
            else:
                row = store.connection.execute(
                    "SELECT metadata_json FROM agent_instances WHERE agent_id=?",
                    (f"agent:{slug}",),
                ).fetchone()
                metadata = json.loads(row["metadata_json"])
                publication = metadata["shotcaller_bootstrap_publication"]
                if case == "malformed-publication":
                    publication["worktree"] = "relative/Projects"
                else:
                    publication["session_identity"] = SESSIONS["Qiyana"]
                store.connection.execute(
                    "UPDATE agent_instances SET metadata_json=? WHERE agent_id=?",
                    (stable(metadata), f"agent:{slug}"),
                )
            try:
                canonical_presentations(store)
            except StorageRefusal as exc:
                assert exc.code == "display_replay_project_unproven"
                assert f"agent:{slug}" in str(exc)
                assert f"runtime:{slug}" in str(exc)
                assert len(str(exc).encode("utf-8")) <= 320
            else:
                raise AssertionError(f"Shotcaller {case} source was accepted")


def test_shotcaller_assignment_refuses_unproven_legacy_squad_ownership(
    root: Path,
) -> None:
    for case in ("missing", "mismatched", "multiple-assignments"):
        case_root = root / case
        state = canonical_state(case_root)
        with SQLiteStorage(state) as store:
            if case == "missing":
                store.connection.execute(
                    "UPDATE squads SET state='retired' WHERE squad_id='squad:Garen'"
                )
            elif case == "mismatched":
                store.connection.execute(
                    """
                    UPDATE squads SET shotcaller_agent_id='agent:ambessa'
                     WHERE squad_id='squad:Garen'
                    """
                )
            else:
                store.connection.execute(
                    """
                    INSERT INTO callsign_assignments
                      (callsign_assignment_id,callsign,subject_id,agent_id,
                       runtime_instance_id,role,scope_kind,scope_id,state,
                       queue_version,requirements_json,acceptance_digest,version,
                       reserved_at,activated_at)
                    VALUES('callsign-assignment:ashe:duplicate','Ashe',
                           'agent:ashe:duplicate','agent:ashe','runtime:ashe',
                           'shotcaller','squad','squad:Garen','active',1,'[]',
                           'synthetic-acceptance',1,?,?)
                    """,
                    (AT, AT),
                )
            try:
                canonical_presentations(store)
            except StorageRefusal as exc:
                assert exc.code == "display_replay_assignment_unproven"
            else:
                raise AssertionError(
                    f"Shotcaller {case} assignment ownership was accepted"
                )


def test_replaced_session_fails_closed_before_metadata(root: Path) -> None:
    state = canonical_state(root)
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        herdr = RestoredHerdr(expected)
        herdr.unavailable_reads = 0
        herdr.agents[0]["agent_session"]["value"] = "replacement-session"
        herdr.agents[0]["name"] = expected[0]["routing_name"]
        try:
            replay_restored_display(store, herdr_runner=herdr, timeout_ms=0)
        except StorageRefusal as exc:
            assert exc.code == "display_replay_session_replaced"
        else:
            raise AssertionError("replaced native session received League metadata")
        assert report_calls(herdr) == []


def test_full_restored_agent_reconciliation_is_once_only(root: Path) -> None:
    state = canonical_state(root)
    watcher = RestoredWatcher()
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        herdr = RestoredHerdr(expected)
        before_processes = json.loads(json.dumps(herdr.processes))
        first = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=1_000,
            herdr_runner=herdr,
            watcher_adapter=watcher,
            sleeper=lambda _: None,
        )
        assert first["candidate_count"] == first["reconciled_count"] == 4
        assert first["idempotent_count"] == 0
        assert first["created_processes"] == first["resumed_sessions"] == 0
        assert first["prompted_sessions"] == first["closed_processes"] == 0
        assert herdr.processes == before_processes
        assert {item["name"] for item in herdr.agents} == {
            "ambessa", "ashe", "heimerdinger", "kaisa"
        }
        assert watcher.bindings.keys() == {"agent:ashe"}
        for receipt in first["receipts"]:
            assert receipt["matched"] is True
            assert receipt["runtime_reconciled"] is True
            assert receipt["route_bound"] is True
            assert receipt["presentation_replayed"] is True
            assert receipt["stable_readbacks"] == 2
            expected_watcher = True if receipt["role"] == "shotcaller" else "not_applicable"
            assert receipt["watcher_live"] == expected_watcher
            runtime = dict(
                store.connection.execute(
                    "SELECT * FROM runtime_instances WHERE runtime_instance_id=?",
                    (receipt["runtime_instance_id"],),
                ).fetchone()
            )
            agent = dict(
                store.connection.execute(
                    "SELECT * FROM agent_instances WHERE agent_id=?",
                    (receipt["agent_id"],),
                ).fetchone()
            )
            assert runtime["endpoint"] == receipt["pane_id"]
            assert runtime["runtime_generation"] == receipt["runtime_generation"]
            assert agent["address"] == receipt["pane_id"]

        reports_before_retry = len(report_calls(herdr))
        renames_before_retry = sum(
            call[1:3] == ("agent", "rename") for call in herdr.calls
        )
        second = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        assert second["reconciled_count"] == 0
        assert second["idempotent_count"] == 4
        assert len(report_calls(herdr)) == reports_before_retry
        assert sum(call[1:3] == ("agent", "rename") for call in herdr.calls) == renames_before_retry
        assert not any(
            call[1:3]
            in {
                ("agent", "start"), ("agent", "prompt"), ("tab", "create"),
                ("pane", "split"), ("pane", "send-text"), ("pane", "send-keys"),
                ("pane", "close"),
            }
            for call in herdr.calls
        )


def test_source_less_herdr_inventory_reconciles_from_exact_owned_tokens(
    root: Path,
) -> None:
    state = canonical_state(root)
    watcher = RestoredWatcher()
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        herdr = RestoredHerdr(expected, source_less=True)
        herdr.unavailable_reads = 0
        first = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        assert first["candidate_count"] == first["reconciled_count"] == 4
        assert all("metadata_source" not in agent for agent in herdr.agents)
        assert all("display_agent" not in agent for agent in herdr.agents)
        assert all(agent["tokens"]["display_provider"] for agent in herdr.agents)
        reports = len(report_calls(herdr))

        second = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        assert second["reconciled_count"] == 0
        assert second["idempotent_count"] == 4
        assert len(report_calls(herdr)) == reports


def test_standalone_cursor_cli_restart_preserves_exact_session_without_process_effects(
    root: Path,
) -> None:
    state = canonical_state(root)
    watcher = RestoredWatcher()
    with SQLiteStorage(state) as store:
        _add_standalone_cursor_champion(store, root)
        expected = canonical_presentations(store)
        cursor = next(item for item in expected if item["agent_id"] == "agent:vayne")
        assert cursor["agent_adapter_kind"] == cursor["runtime_kind"] == "cursor"
        assert cursor["provider_kind"] == "cursor"
        herdr = RestoredHerdr(expected)
        herdr.unavailable_reads = 0
        processes = json.loads(json.dumps(herdr.processes))
        reconciled = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        receipt = next(
            item for item in reconciled["receipts"] if item["agent_id"] == "agent:vayne"
        )
        assert receipt["matched"] and receipt["presentation_replayed"]
        live = next(
            item
            for item in herdr.agents
            if item["agent_session"]["value"] == cursor["session_ref"]
        )
        assert live["agent"] == live["display_agent"] == "cursor"
        assert live["name"] == "vayne" and live["terminal_title"] == cursor["title"]
        assert herdr.processes == processes
        assert not any(
            call[1:3] in {
                ("agent", "start"), ("agent", "prompt"), ("tab", "create"),
                ("pane", "split"), ("pane", "close"),
            }
            for call in herdr.calls
        )


def test_partial_restore_records_recovery_and_exact_retry_settles_it(
    root: Path,
) -> None:
    state = canonical_state(root)
    watcher = RestoredWatcher()
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        herdr = FailingMetadataHerdr(expected)
        processes = json.loads(json.dumps(herdr.processes))
        try:
            reconcile_restored_agents(
                store,
                multiplexer_kind="herdr",
                at=AT,
                timeout_ms=1_000,
                herdr_runner=herdr,
                watcher_adapter=watcher,
                sleeper=lambda _: None,
            )
        except StorageRefusal as exc:
            assert exc.code == "restored_agent_recovery_pending"
            assert exc.retryable is True
        else:
            raise AssertionError("partial metadata replay was accepted")
        obligation = store.connection.execute(
            "SELECT state,details_json FROM obligations WHERE kind='runtime_restore'"
        ).fetchone()
        assert obligation is not None and obligation["state"] == "open"
        assert json.loads(obligation["details_json"])["failure_code"] == (
            "display_replay_adapter_failed"
        )
        recovered = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        assert recovered["candidate_count"] == 4
        assert store.connection.execute(
            "SELECT state FROM obligations WHERE kind='runtime_restore'"
        ).fetchone()["state"] == "satisfied"
        assert herdr.processes == processes


def test_route_then_cas_or_watcher_failure_records_recovery_and_retries_once(
    root: Path,
) -> None:
    for failure in ("cas", "watcher"):
        state = canonical_state(root / failure)
        with SQLiteStorage(state) as store:
            expected = canonical_presentations(store)
            herdr = RestoredHerdr(expected)
            herdr.unavailable_reads = 0
            processes = json.loads(json.dumps(herdr.processes))
            watcher = FailingWatcher() if failure == "watcher" else RestoredWatcher()
            target_store = FailingCASStore(store) if failure == "cas" else store
            try:
                reconcile_restored_agents(
                    target_store,
                    multiplexer_kind="herdr",
                    at=AT,
                    timeout_ms=0,
                    herdr_runner=herdr,
                    watcher_adapter=watcher,
                )
            except StorageRefusal as exc:
                assert exc.code == "restored_agent_recovery_pending"
            else:
                raise AssertionError(f"synthetic {failure} failure was accepted")
            assert store.connection.execute(
                "SELECT COUNT(*) FROM obligations WHERE kind='runtime_restore' AND state='open'"
            ).fetchone()[0] == 1
            mutations_before = {
                "renames": sum(call[1:3] == ("agent", "rename") for call in herdr.calls),
                "reports": len(report_calls(herdr)),
            }
            recovered = reconcile_restored_agents(
                store,
                multiplexer_kind="herdr",
                at=AT,
                timeout_ms=0,
                herdr_runner=herdr,
                watcher_adapter=watcher,
            )
            assert recovered["candidate_count"] == 4
            assert store.connection.execute(
                "SELECT COUNT(*) FROM obligations WHERE kind='runtime_restore' AND state='open'"
            ).fetchone()[0] == 0
            assert herdr.processes == processes
            assert sum(
                call[1:3] == ("agent", "rename") for call in herdr.calls
            ) <= mutations_before["renames"] + 4
            assert len(report_calls(herdr)) <= mutations_before["reports"] + 8


def test_final_inventory_race_records_every_affected_recovery_obligation(
    root: Path,
) -> None:
    state = canonical_state(root)
    watcher = RestoredWatcher()
    with SQLiteStorage(state) as store:
        expected = canonical_presentations(store)
        herdr = FailingFinalInventoryHerdr(expected)
        processes = json.loads(json.dumps(herdr.processes))
        try:
            reconcile_restored_agents(
                store,
                multiplexer_kind="herdr",
                at=AT,
                timeout_ms=0,
                herdr_runner=herdr,
                watcher_adapter=watcher,
            )
        except StorageRefusal as exc:
            assert exc.code == "restored_agent_recovery_pending"
        else:
            raise AssertionError("post-effect final inventory race was accepted")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM obligations WHERE kind='runtime_restore' AND state='open'"
        ).fetchone()[0] == len(expected)
        recovered = reconcile_restored_agents(
            store,
            multiplexer_kind="herdr",
            at=AT,
            timeout_ms=0,
            herdr_runner=herdr,
            watcher_adapter=watcher,
        )
        assert recovered["candidate_count"] == len(expected)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM obligations WHERE kind='runtime_restore' AND state='open'"
        ).fetchone()[0] == 0
        assert herdr.processes == processes


def test_adapter_capabilities_are_truthful() -> None:
    assert "discover" in MultiplexerAdapter.__dict__
    assert "endpoint" in MultiplexerAdapter.__dict__
    assert "inspect_restored" in MultiplexerAdapter.__dict__
    assert "routing" in MultiplexerAdapter.__dict__
    assert "metadata" in MultiplexerAdapter.__dict__
    assert "server_generation" in MultiplexerAdapter.__dict__
    assert "calling_context" in MultiplexerAdapter.__dict__
    assert "focus" in MultiplexerAdapter.__dict__
    assert "verify_stopped_agent" in MultiplexerAdapter.__dict__
    assert "replay_presentation" not in CommandRunner.__dict__
    assert "run" in CommandRunner.__dict__
    registry = builtin_multiplexer_adapter_registry()
    assert registry.adapter("herdr").capabilities == frozenset(
        {
            "calling_context", "discover", "routing", "placement", "metadata",
            "title", "delivery", "steering_delivery", "close",
            "visible_launch", "shotcaller_bootstrap", "rollover_reconciliation",
            "production_cleanup", "provider_session_lifecycle",
            "runtime_replacement", "stopped_retirement",
        }
    )
    assert registry.adapter("tmux").capabilities == frozenset()
    for adapter in registry.adapters():
        assert adapter.capabilities <= MULTIPLEXER_OPERATIONS
        for capability in adapter.capabilities:
            for method in MULTIPLEXER_OPERATION_METHODS[capability]:
                assert callable(getattr(adapter, method, None))
    try:
        registry.adapter("tmux").metadata(
            {}, RestoredEndpoint("synthetic", "w", "t", "p", "term"), 1
        )
    except StorageRefusal as exc:
        assert exc.code == "multiplexer_restore_unsupported"
    else:
        raise AssertionError("tmux restore was fabricated without native support")


def test_plugin_is_supported_async_startup_only() -> None:
    plugin = ROOT / "integrations/herdr/league-restore"
    manifest = (plugin / "herdr-plugin.toml").read_text(encoding="utf-8")
    restore = (plugin / "restore.sh").read_text(encoding="utf-8")
    assert "[[startup]]" in manifest and 'command = ["sh", "restore.sh"]' in manifest
    assert "barrier" not in manifest.lower()
    assert "runtime reconcile-restored-agent" in restore
    assert "--multiplexer-kind herdr" in restore
    assert "runtime replay-restored-display" not in restore
    assert "resume-launch" not in restore and "agent start" not in restore


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-async-restore-") as temporary:
        root = Path(temporary)
        test_async_restart_converges_without_duplicate_processes(root / "success")
        test_shotcallers_use_publication_cwd_and_optional_exact_project_without_process_effects(
            root / "named-shotcallers"
        )
        test_shotcaller_publication_cwd_accepts_absent_or_matching_and_refuses_conflict(
            root / "shotcaller-cwd-evidence"
        )
        test_shotcaller_project_binding_refuses_ambiguous_or_malformed_sources(
            root / "shotcaller-project-refusals"
        )
        test_shotcaller_assignment_refuses_unproven_legacy_squad_ownership(
            root / "shotcaller-assignment-refusals"
        )
        test_replaced_session_fails_closed_before_metadata(root / "replaced")
        test_full_restored_agent_reconciliation_is_once_only(root / "reconcile")
        test_standalone_cursor_cli_restart_preserves_exact_session_without_process_effects(
            root / "cursor"
        )
        test_partial_restore_records_recovery_and_exact_retry_settles_it(
            root / "recovery"
        )
        test_route_then_cas_or_watcher_failure_records_recovery_and_retries_once(
            root / "effect-failures"
        )
        test_final_inventory_race_records_every_affected_recovery_obligation(
            root / "final-inventory"
        )
    test_adapter_capabilities_are_truthful()
    test_plugin_is_supported_async_startup_only()
    print("PASS: async Herdr restart replay converges exact sessions without duplicate processes")


if __name__ == "__main__":
    main()
