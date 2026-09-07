"""Identity recovery, restart, refusal and registration regressions; synthetic only."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import tempfile
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from storage_fixture import SHOTCALLER_ID, CHAMPION_ID, AT2, write_complete_fixture
from storage_test_support import seeded_state, invoke_cli
from league.sqlite_store import SQLiteStorage
from league.storage import RuntimeRegistrationCommand, StorageRefusal
from league.runtime_identity import repair_shotcaller_identity, validate_registration
from league.restored_agent import restored_runtime_generation
from league.sqlite_watcher_ops import obligation_counts, _stop_obligation_summaries
from league.display_replay import _shotcaller_publication
from league.agent_adapters.base import native_presentation
from league.importer import build_import_plan


class Native:
    kind = "herdr"
    thread = SHOTCALLER_ID
    reads = 0
    change_process = False

    def calling_context(self):
        return {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}

    def discover(self):
        return [{"pane_id": "w1:p1"}]

    def endpoint(self, *_):
        return SimpleNamespace(pane_id="w1:p1", workspace_id="w1", tab_id="w1:t1", terminal_id="terminal:new")

    def inspect_restored(self, *_):
        self.reads += 1
        return {"session_ref": self.thread, "session_source": "herdr:codex",
                "agent": {"agent": "codex", "agent_status": "working"},
                "process_fingerprint": str(self.reads) if self.change_process else "process:1"}


class Watcher:
    fail = False

    def preflight(self, *_):
        return {"fence": 1}

    def bind(self, *_):
        if self.fail:
            raise StorageRefusal("synthetic_watcher_failure", "synthetic")

    def verify(self, *_):
        pass


def seed(parent):
    _, state, _ = seeded_state(parent, "fixture")
    store = SQLiteStorage(state)
    with store._transaction():
        store.connection.execute("UPDATE agent_instances SET thread_id='terminal-session' WHERE agent_id=?", (SHOTCALLER_ID,))
    command = RuntimeRegistrationCommand(runtime_instance_id="runtime:legacy", actor_agent_id=SHOTCALLER_ID,
        harness_kind="codex", backend_kind="herdr", session_ref="terminal-session", endpoint="w1:p1",
        runtime_generation="terminal:old", status="active", verified=True, at=AT2)
    store.register_runtime(command)  # Synthetic legacy state, not the validated native CLI.
    request = dict(agent_id=SHOTCALLER_ID, runtime_instance_id=command.runtime_instance_id,
        expected_version=store.agent_status(SHOTCALLER_ID)["version"], expected_session_ref=command.session_ref,
        expected_generation=command.runtime_generation, endpoint=command.endpoint, thread_id=SHOTCALLER_ID)
    return store, command, request


def repair(store, request, native=None, watcher=None, authorized=True):
    # Scope selection is not under test; use one synthetic exact binding.
    def binding(_):
        return {"actor_agent_id": SHOTCALLER_ID, "runtime_instance_id": "runtime:legacy",
                "session_ref": store.agent_status(SHOTCALLER_ID)["thread_id"]}
    with patch.object(store, "supervisor_binding", side_effect=binding):
        return repair_shotcaller_identity(store, request, multiplexer=native or Native(),
            watcher=watcher or Watcher(), cwd="/synthetic/project", at=AT2, owner_authorized=authorized)


def refused(call, code):
    try:
        call()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError("expected refusal: " + code)


def preserved(store):
    return {table: [tuple(row) for row in store.connection.execute("SELECT * FROM " + table)]
            for table in ("tasks", "task_assignments", "requests", "prompts", "prompt_quarantine",
                          "callsign_assignments", "squads", "delivery_outbox", "cleanup_obligations")}


def test_repair_retry_restart():
    with tempfile.TemporaryDirectory() as tmp:
        store, command, request = seed(Path(tmp))
        with store:
            baseline, champion = preserved(store), store.agent_status(CHAMPION_ID)
            result = repair(store, request)
            assert result["native_identity_verified"] and result["watcher_verified"]
            assert not result["idempotent"]
            assert store.agent_status(SHOTCALLER_ID)["thread_id"] == SHOTCALLER_ID
            assert repair(store, request)["idempotent"]
            assert preserved(store) == baseline
            assert store.agent_status(CHAMPION_ID) == champion
            # Both restore readers must honor the repair, without rewriting the old receipt.
            publication = {"assignment_id": "assignment:legacy", "agent_id": SHOTCALLER_ID,
                "session_identity": "terminal-session", "callsign": "Garen", "routing_name": "garen",
                "worktree": "/synthetic/project", "presentation_source": "herdr:codex"}
            row = {**store.agent_status(SHOTCALLER_ID), "session_ref": SHOTCALLER_ID,
                "runtime_instance_id": "runtime:legacy", "routing_name": "garen", "worktree": "/synthetic/project"}
            with patch.object(store, "shotcaller_bootstrap_publication", return_value=publication):
                assert _shotcaller_publication(store, row, "assignment:legacy")["session_identity"] == SHOTCALLER_ID
                native_presentation(store, row, "assignment:legacy", "Synthetic")
            assert publication["session_identity"] == "terminal-session"
            validate_registration(store, replace(command, session_ref=SHOTCALLER_ID, runtime_generation=result["runtime_generation"]))
            state = store.state_root
        # A new connection and a subsequent terminal generation must retain the provider UUID.
        with SQLiteStorage(state) as store:
            restored = store.reconcile_restored_runtime(command.runtime_instance_id, SHOTCALLER_ID,
                SHOTCALLER_ID, SHOTCALLER_ID, "herdr", "w1:p1", result["runtime_generation"],
                "w1:p2", "terminal:next", AT2)
            assert not restored["idempotent"]
            assert store.agent_status(SHOTCALLER_ID)["thread_id"] == SHOTCALLER_ID
            assert store.agent_status(SHOTCALLER_ID)["address"] == "w1:p2"
            assert preserved(store) == baseline


def test_refusals_and_atomicity():
    with tempfile.TemporaryDirectory() as tmp:
        store, _, request = seed(Path(tmp))
        with store:
            original = store.agent_status(SHOTCALLER_ID)
            refused(lambda: repair(store, request, authorized=False), "owner_authorization_required")
            refused(lambda: repair(store, {**request, "expected_version": request["expected_version"] + 1}), "runtime_identity_repair_conflict")
            native = Native()
            native.thread = CHAMPION_ID
            refused(lambda: repair(store, request, native), "runtime_identity_repair_refused")
            native = Native()
            native.change_process = True
            refused(lambda: repair(store, request, native), "runtime_identity_repair_refused")
            refused(lambda: repair(store, {**request, "expected_session_ref": CHAMPION_ID}), "runtime_identity_repair_refused")
            with patch("league.sqlite_runtime_ops.record_restored_runtime_recovery", side_effect=StorageRefusal("synthetic_fault", "synthetic")):
                refused(lambda: repair(store, request), "synthetic_fault")
            assert store.agent_status(SHOTCALLER_ID) == original
            assert store.connection.execute("SELECT session_ref FROM runtime_instances WHERE runtime_instance_id='runtime:legacy'").fetchone()[0] == "terminal-session"
            store.record_restored_runtime_recovery("runtime:legacy", SHOTCALLER_ID, "synthetic_route_failure", AT2)
            refused(lambda: repair(store, request), "runtime_identity_repair_conflict")
            assert store.agent_status(SHOTCALLER_ID) == original
            assert "synthetic_route_failure" in store.connection.execute("SELECT details_json FROM obligations WHERE kind='runtime_restore'").fetchone()[0]


def test_watcher_failure_retains_retry():
    with tempfile.TemporaryDirectory() as tmp:
        store, _, request = seed(Path(tmp))
        with store:
            watcher = Watcher()
            watcher.fail = True
            refused(lambda: repair(store, request, watcher=watcher), "runtime_identity_repair_pending")
            row = store.connection.execute("SELECT state,details_json FROM obligations WHERE kind='runtime_restore'").fetchone()
            assert row["state"] == "open" and "repair-shotcaller-identity" in row["details_json"]
            counts = obligation_counts(store, SHOTCALLER_ID)
            assert counts["runtime_recovery"] == 1
            assert "1 runtime recovery pending" in _stop_obligation_summaries(store, SHOTCALLER_ID, counts)
            assert repair(store, request)["idempotent"]
            assert store.connection.execute("SELECT state FROM obligations WHERE kind='runtime_restore'").fetchone()[0] == "satisfied"


def test_native_registration_guard():
    with tempfile.TemporaryDirectory() as tmp:
        store, command, _ = seed(Path(tmp))
        with store:
            refused(lambda: validate_registration(store, command), "runtime_session_invalid")
            refused(lambda: validate_registration(store, replace(command, session_ref=CHAMPION_ID)), "runtime_identity_mismatch")
            validate_registration(store, replace(command, verified=False))
            result = invoke_cli(store.state_root, "hook", "register-runtime",
                "--runtime-instance-id", "runtime:rejected", "--actor-agent-id", SHOTCALLER_ID,
                "--harness-kind", "codex", "--backend-kind", "herdr", "--session-ref", "terminal-session",
                "--endpoint", "w1:p1", "--runtime-generation", "terminal:old", "--status", "active",
                "--verified", "--at", AT2, expected=2)
            assert result["error"]["code"] == "runtime_session_invalid"
            assert store.connection.execute("SELECT 1 FROM runtime_instances WHERE runtime_instance_id='runtime:rejected'").fetchone() is None


def test_import_rejects_terminal_name_as_codex_thread():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = write_complete_fixture(root)
        status_path = root / "rosters/Garen/status.json"
        status = json.loads(status_path.read_text())
        status["thread_id"] = "terminal-session"
        status_path.write_text(json.dumps(status) + "\n")
        refused(lambda: build_import_plan(root, fixture["manifest"]), "identity_collision")


if __name__ == "__main__":
    for test in (test_repair_retry_restart, test_refusals_and_atomicity, test_watcher_failure_retains_retry,
                 test_native_registration_guard, test_import_rejects_terminal_name_as_codex_thread):
        test()
    print("PASS: exact identity repair, restart retention, atomic refusals, watcher recovery, native registration guard")
