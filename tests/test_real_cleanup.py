#!/usr/bin/env python3
"""Focused exact-root adapter and SQLite cleanup execution tests."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.real_cleanup import (  # noqa: E402
    ArchiveAdapter,
    CallsignAdapter,
    GitAdapter,
    HerdrBackendAdapter,
    HerdrHarnessAdapter,
    SubprocessRunner,
    validate_canary_config,
)
from league.real_canary import (  # noqa: E402
    LIFECYCLE_TASK_ID,
    SHOTCALLER_ID,
    _cleanup_files,
    _cleanup_failed_canary,
    _codex_session_id,
    _create_git_canary,
    _settle_transition_and_request,
    _setup_sqlite,
)
from league import real_canary  # noqa: E402
from league.sqlite_handoff_schema import CHAMPION_SEED, SHUFFLE_VERSION  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand, StorageRefusal  # noqa: E402
from lifecycle_fakes import issue_bound_spec  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


AT1 = "2026-01-01T00:00:00Z"
AT2 = "2026-01-01T00:01:00Z"
AT3 = "2026-01-01T00:02:00Z"


def refused(operation: Any, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def run(arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        list(arguments), text=True, capture_output=True, check=False, timeout=20
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def herdr_identity(agent_name: str = "cleanupcanary") -> dict[str, str]:
    return {
        "agent_name": agent_name,
        "workspace_id": "w-test",
        "pane_id": "w-test:p-canary",
        "terminal_id": "terminal-canary",
        "session_id": "canary-session",
        "runtime_instance_id": "runtime:canary",
        "runtime_generation": "generation:canary",
    }


def git_fixture(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    repository = root / "repository"
    worktree = root / "worktree"
    run(("git", "init", "-b", "main", str(repository)))
    run(("git", "-C", str(repository), "config", "user.name", "League Canary"))
    run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "12794431+Vinosaamaa@users.noreply.github.com",
        )
    )
    (repository / "README.md").write_text("disposable canary\n", encoding="utf-8")
    run(("git", "-C", str(repository), "add", "README.md"))
    run(("git", "-C", str(repository), "commit", "-m", "Seed disposable canary"))
    head = run(("git", "-C", str(repository), "rev-parse", "HEAD"))
    run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "canary-cleanup",
            str(worktree),
            head,
        )
    )
    return {
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": "canary-cleanup",
        "head": head,
        "base_ref": "main",
        "merge_commit": head,
    }


def squash_git_fixture(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    repository = root / "repository"
    worktree = root / "worktree"
    run(("git", "init", "-b", "main", str(repository)))
    run(("git", "-C", str(repository), "config", "user.name", "League Canary"))
    run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "12794431+Vinosaamaa@users.noreply.github.com",
        )
    )
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    run(("git", "-C", str(repository), "add", "README.md"))
    run(("git", "-C", str(repository), "commit", "-m", "Seed base"))
    base = run(("git", "-C", str(repository), "rev-parse", "HEAD"))
    run(("git", "-C", str(repository), "switch", "-c", "canary-squash"))
    (repository / "REPORT.md").write_text("repository artifact\n", encoding="utf-8")
    run(("git", "-C", str(repository), "add", "REPORT.md"))
    run(("git", "-C", str(repository), "commit", "-m", "Produce report"))
    head = run(("git", "-C", str(repository), "rev-parse", "HEAD"))
    tree = run(("git", "-C", str(repository), "rev-parse", "HEAD^{tree}"))
    merge_commit = run(
        (
            "git",
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            "Squash report",
        )
    )
    run(("git", "-C", str(repository), "update-ref", "refs/heads/main", merge_commit))
    run(("git", "-C", str(repository), "switch", "main"))
    run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            str(worktree),
            "canary-squash",
        )
    )
    return {
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": "canary-squash",
        "head": head,
        "base_ref": merge_commit,
        "merge_commit": merge_commit,
        "tested_tree": tree,
        "merge_tree": tree,
    }


def active_callsign(store: SQLiteStorage) -> dict[str, Any]:
    store.reconcile_callsign_pool(
        "champion",
        1,
        CHAMPION_SEED,
        SHUFFLE_VERSION,
        [{"callsign": "Lux", "enabled": True, "capabilities": ["backend.herdr"]}],
        AT1,
    )
    assignment = store.allocate_callsign(
        "callsign-assignment:canary",
        "agent:canary",
        "champion",
        "task",
        "task:canary",
        ["backend.herdr"],
        AT2,
    )
    receipt = {
        "schema": "league.runtime-acceptance.v1",
        "verified": True,
        "assignment_id": assignment["assignment_id"],
        "agent_id": assignment["agent_id"],
        "callsign": assignment["callsign"],
        "runtime_instance_id": "runtime:canary",
        "harness_kind": "codex",
        "backend_kind": "herdr",
        "session_identity": "codex:canary-session",
        "endpoint_identity": "w-test:p-canary",
        "endpoint_generation": "generation:canary",
        "routing_name": assignment["callsign"].lower(),
        "display_agent": "codex",
        "capabilities": ["backend.herdr"],
    }
    store.activate_callsign(assignment["assignment_id"], 1, receipt, AT2)
    return assignment


class FakeHerdrRunner:
    def __init__(self, agent_kind: str = "codex") -> None:
        self.agent_kind = agent_kind
        self.agent = True
        self.pane = True
        self.fail_close_once = False
        self.fail_exit_once = False
        self.calls: list[tuple[str, ...]] = []

    def run(
        self, arguments: Sequence[str], *, allow_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        args = tuple(arguments)
        self.calls.append(args)
        if args[1:3] == ("agent", "list"):
            agents = (
                [
                    {
                        "agent": self.agent_kind,
                        "name": "cleanupcanary",
                        "pane_id": "w-test:p-canary",
                        "agent_status": str(self.agent),
                        "agent_session": {"value": "canary-session"},
                    }
                ]
                if self.agent
                else []
            )
            output = json.dumps({"result": {"agents": agents}})
        elif args[1:3] == ("agent", "prompt"):
            if self.fail_exit_once:
                self.fail_exit_once = False
                return subprocess.CompletedProcess(list(args), 1, "", "synthetic refusal")
            self.agent = "done"
            output = json.dumps({"result": {"submitted": True}})
        elif args[1:3] == ("pane", "list"):
            panes = (
                [{"pane_id": "w-test:p-canary", "terminal_id": "terminal-canary"}]
                if self.pane
                else []
            )
            output = json.dumps({"result": {"panes": panes}})
        elif args[1:3] == ("pane", "close"):
            if self.fail_close_once:
                self.fail_close_once = False
                raise StorageRefusal(
                    "cleanup_adapter_failed", "synthetic pane close failed"
                )
            self.pane = False
            output = json.dumps({"result": {"closed": True}})
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(list(args), 0, output, "")


def test_archive_git_and_scope(root: Path) -> None:
    git = git_fixture(root)
    config = {
        "schema": "league.cleanup-canary-adapters.v1",
        "scope": "disposable-canary",
        "temporary_root": str(root),
        "archive_path": str(root / "archive/identity.json"),
        "herdr": herdr_identity(),
        "git": git,
        "callsign": {
            "assignment_id": "callsign-assignment:canary",
            "callsign": "Lux",
            "expected_version": 2,
        },
    }
    validated = validate_canary_config(config)
    assert validated["scope"] == "disposable-canary"
    invalid = dict(config)
    invalid["archive_path"] = "/tmp/outside-canary.json"
    refused(lambda: validate_canary_config(invalid), "cleanup_adapter_scope_refused")
    linked_root = root.parent / f"{root.name}-link"
    linked_root.symlink_to(root, target_is_directory=True)
    linked = dict(config)
    linked["temporary_root"] = str(linked_root)
    refused(lambda: validate_canary_config(linked), "cleanup_adapter_scope_refused")

    archive_action = {
        "expected_identity": {"task_id": "task:canary"},
        "intended_state": {"archived": True, "identity": {"task_id": "task:canary"}},
    }
    archive = ArchiveAdapter(Path(config["archive_path"]))
    assert archive.inspect(archive_action) == archive_action["expected_identity"]
    archive.apply(archive_action)
    assert archive.inspect(archive_action) == archive_action["intended_state"]

    git = validated["git"]
    run(("git", "-C", git["repository"], "switch", "--orphan", "unrelated-primary"))
    run(
        (
            "git",
            "-C",
            git["repository"],
            "commit",
            "--allow-empty",
            "-m",
            "Synthetic unrelated primary checkout",
        )
    )
    adapter = GitAdapter(git, SubprocessRunner())
    worktree_action = {
        "action_kind": "worktree_remove",
        "expected_identity": {
            "repository": git["repository"],
            "worktree": git["worktree"],
            "branch": git["branch"],
            "head": git["head"],
        },
        "intended_state": {"completed": True, "action": "worktree_remove"},
    }
    assert adapter.inspect(worktree_action) == worktree_action["expected_identity"]
    adapter.apply(worktree_action)
    assert adapter.inspect(worktree_action) == worktree_action["intended_state"]
    branch_action = {
        "action_kind": "branch_delete",
        "expected_identity": {
            "repository": git["repository"],
            "branch": git["branch"],
            "head": git["head"],
            "base_ref": git["base_ref"],
            "merge_commit": git["merge_commit"],
        },
        "intended_state": {"completed": True, "action": "branch_delete"},
    }
    assert adapter.inspect(branch_action) == branch_action["expected_identity"]
    adapter.apply(branch_action)
    assert adapter.inspect(branch_action) == branch_action["intended_state"]
    assert Path(git["repository"]).is_dir()


def test_herdr_and_callsign_exact_cleanup(root: Path) -> None:
    root.mkdir(parents=True)
    state, _ = migrated_state(root, "sqlite")
    runner = FakeHerdrRunner()
    identity = herdr_identity()
    with SQLiteStorage(state) as store:
        assignment = active_callsign(store)
        harness_action = {
            "expected_identity": {
                "agent_name": "cleanupcanary",
                "pane_id": "w-test:p-canary",
                "session_id": "canary-session",
            },
            "intended_state": {"completed": True, "action": "session_exit"},
        }
        harness = HerdrHarnessAdapter(identity, runner)
        runner.fail_exit_once = True
        refused(lambda: harness.apply(harness_action), "cleanup_adapter_failed")
        assert runner.agent is True
        harness.apply(harness_action)
        assert harness.inspect(harness_action) == harness_action["intended_state"]

        backend_action = {
            "expected_identity": {
                "pane_id": "w-test:p-canary",
                "terminal_id": "terminal-canary",
                "runtime_instance_id": "runtime:canary",
                "runtime_generation": "generation:canary",
            },
            "intended_state": {"completed": True, "action": "endpoint_close"},
        }
        backend = HerdrBackendAdapter(store, identity, runner, AT3)
        assert backend.inspect(backend_action) == backend_action["expected_identity"]
        backend.apply(backend_action)
        assert backend.inspect(backend_action) == backend_action["intended_state"]
        store.connection.execute(
            "UPDATE runtime_instances SET endpoint='w-test:p-reused' WHERE runtime_instance_id='runtime:canary'"
        )
        refused(lambda: backend.inspect(backend_action), "cleanup_identity_mismatch")
        store.connection.execute(
            "UPDATE runtime_instances SET endpoint='w-test:p-canary' WHERE runtime_instance_id='runtime:canary'"
        )

        callsign_identity = {
            "assignment_id": assignment["assignment_id"],
            "callsign": assignment["callsign"],
            "expected_version": 2,
        }
        callsign_action = {
            "expected_identity": callsign_identity,
            "intended_state": {"completed": True, "action": "callsign_release"},
        }
        callsign = CallsignAdapter(store, callsign_identity, AT3)
        assert callsign.inspect(callsign_action) == callsign_identity
        callsign.apply(callsign_action)
        assert callsign.inspect(callsign_action) == callsign_action["intended_state"]
        store.connection.execute(
            "UPDATE callsign_assignments SET version=99 WHERE callsign_assignment_id=?",
            (assignment["assignment_id"],),
        )
        refused(lambda: callsign.inspect(callsign_action), "cleanup_identity_mismatch")


def test_repository_artifact_squash_tree_is_cleanup_eligible(root: Path) -> None:
    git = squash_git_fixture(root)
    assert git["head"] != git["merge_commit"]
    assert git["tested_tree"] == git["merge_tree"]
    adapter = GitAdapter(git, SubprocessRunner())
    worktree_action = {
        "action_kind": "worktree_remove",
        "expected_identity": {
            key: git[key] for key in ("repository", "worktree", "branch", "head")
        },
        "intended_state": {"completed": True, "action": "worktree_remove"},
    }
    adapter.apply(worktree_action)
    assert adapter.inspect(worktree_action) == worktree_action["intended_state"]
    branch_action = {
        "action_kind": "branch_delete",
        "expected_identity": {
            key: git[key]
            for key in ("repository", "branch", "head", "base_ref", "merge_commit")
        },
        "intended_state": {"completed": True, "action": "branch_delete"},
    }
    assert adapter.inspect(branch_action) == branch_action["expected_identity"]
    receipt = adapter.apply(branch_action)
    assert receipt["deletion_proof"] == "squash-tree-equivalent"
    assert adapter.inspect(branch_action) == branch_action["intended_state"]
    assert Path(git["repository"]).is_dir()


def test_squash_on_advanced_base_requires_exact_reconstruction(root: Path) -> None:
    identity = squash_git_fixture(root)
    repository = identity["repository"]
    base = run(("git", "-C", repository, "rev-parse", identity["merge_commit"] + "^"))
    run(("git", "-C", repository, "switch", "--detach", base))
    (Path(repository) / "UNRELATED.md").write_text("another accepted change\n", encoding="utf-8")
    run(("git", "-C", repository, "add", "UNRELATED.md"))
    run(("git", "-C", repository, "commit", "-m", "Advance base"))
    advanced = run(("git", "-C", repository, "rev-parse", "HEAD"))
    run(("git", "-C", repository, "merge", "--squash", identity["head"]))
    run(("git", "-C", repository, "commit", "-m", "Accept exact report on advanced base"))
    identity["merge_commit"] = run(("git", "-C", repository, "rev-parse", "HEAD"))
    identity["base_ref"] = identity["merge_commit"]
    adapter = GitAdapter(identity, SubprocessRunner())
    assert adapter._branch_deletion_mode() == "squash-reconstructed-tree-equivalent"
    assert run(("git", "-C", identity["worktree"], "status", "--porcelain")) == ""
    assert run(("git", "-C", repository, "rev-parse", "HEAD")) == identity["merge_commit"]
    # A matching merge that is not published cannot authorize deletion.
    unpublished = GitAdapter({**identity, "base_ref": base}, SubprocessRunner())
    refused(unpublished._branch_deletion_mode, "cleanup_identity_mismatch")
    # A new branch commit not present in the accepted squash must survive.
    (Path(identity["worktree"]) / "UNSHIPPED.md").write_text("preserve me\n", encoding="utf-8")
    run(("git", "-C", identity["worktree"], "add", "UNSHIPPED.md"))
    run(("git", "-C", identity["worktree"], "commit", "-m", "Unshipped follow-up"))
    unshipped = run(("git", "-C", identity["worktree"], "rev-parse", "HEAD"))
    refused(GitAdapter({**identity, "head": unshipped}, SubprocessRunner())._branch_deletion_mode,
            "cleanup_identity_mismatch")
    # A conflicting base cannot be accepted by ignoring merge-tree's exit code.
    run(("git", "-C", repository, "switch", "--detach", advanced))
    (Path(repository) / "REPORT.md").write_text("conflicting report\n", encoding="utf-8")
    run(("git", "-C", repository, "add", "REPORT.md"))
    run(("git", "-C", repository, "commit", "-m", "Conflict in base"))
    run(("git", "-C", repository, "commit", "--allow-empty", "-m", "Unproven resolution"))
    conflicting = run(("git", "-C", repository, "rev-parse", "HEAD"))
    refused(GitAdapter({**identity, "merge_commit": conflicting, "base_ref": conflicting},
                       SubprocessRunner())._branch_deletion_mode, "cleanup_identity_mismatch")
    assert Path(identity["worktree"]).is_dir()
    assert run(("git", "-C", identity["worktree"], "rev-parse", "HEAD")) == unshipped


def test_backend_close_resumes_after_external_failure(root: Path) -> None:
    root.mkdir(parents=True)
    state, _ = migrated_state(root, "sqlite")
    runner = FakeHerdrRunner()
    runner.fail_close_once = True
    identity = herdr_identity()
    action = {
        "expected_identity": {
            key: identity[key]
            for key in (
                "pane_id",
                "terminal_id",
                "runtime_instance_id",
                "runtime_generation",
            )
        },
        "intended_state": {"completed": True, "action": "endpoint_close"},
    }
    with SQLiteStorage(state) as store:
        active_callsign(store)
        backend = HerdrBackendAdapter(store, identity, runner, AT3)
        refused(lambda: backend.apply(action), "cleanup_adapter_failed")
        assert runner.pane is True
        assert store.connection.execute(
            "SELECT status FROM runtime_instances WHERE runtime_instance_id='runtime:canary'"
        ).fetchone()[0] == "closed"
        refused(
            lambda: store.register_runtime(
                RuntimeRegistrationCommand(
                    runtime_instance_id=identity["runtime_instance_id"],
                    actor_agent_id="agent:canary",
                    harness_kind="codex",
                    backend_kind="herdr",
                    session_ref="codex:canary-session",
                    endpoint=identity["pane_id"],
                    runtime_generation=identity["runtime_generation"],
                    status="active",
                    verified=True,
                    at="2026-01-01T00:03:00Z",
                    capabilities=("backend.herdr",),
                )
            ),
            "runtime_closed",
        )
        receipt = backend.apply(action)
        assert receipt["runtime"]["idempotent"] is True
        assert backend.inspect(action) == action["intended_state"]


def test_real_canary_sqlite_setup_uses_explicit_root(root: Path) -> None:
    root.mkdir(parents=True)
    git = _create_git_canary(root)
    herdr = herdr_identity()
    setup = _setup_sqlite(
        root, ROOT, git, herdr, issue_spec_resolver=issue_bound_spec
    )
    extended_git = {
        **git,
        "tested_tree": git["head"],
        "merge_tree": git["head"],
        "artifact_sha256": "a" * 64,
    }
    _, adapter_path = _cleanup_files(
        root,
        extended_git,
        herdr,
        f"callsign-assignment:{setup['assignment']['assignment_id']}",
        "Lux",
    )
    adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
    assert set(adapter["git"]) == {
        "repository",
        "worktree",
        "branch",
        "head",
        "base_ref",
        "merge_commit",
    }
    assert validate_canary_config(adapter)["git"]["head"] == git["head"]
    assert setup["assignment"]["state"] == "active"
    state = root / "league/state"
    with SQLiteStorage(state, request_wal=False) as store:
        runtime = store.connection.execute(
            "SELECT harness_kind,backend_kind,endpoint,status FROM runtime_instances WHERE runtime_instance_id=?",
            (herdr["runtime_instance_id"],),
        ).fetchone()
        assert tuple(runtime) == ("codex", "herdr", herdr["pane_id"], "active")
        assert store.connection.execute(
            "SELECT state FROM delivery_outbox WHERE outbox_id=?",
            (setup["assignment"]["outbox_id"],),
        ).fetchone()[0] == "delivered"


def test_failure_scope_removes_only_exact_git_resources(root: Path) -> None:
    namespace = "testfailure"
    home = root / f"league-real-canary-{namespace}"
    home.mkdir(parents=True)
    git = _create_git_canary(home)
    (home / "failure-scope.json").write_text(
        json.dumps(
            {
                "schema": "league.real-canary-failure-scope.v1",
                "namespace": namespace,
                "git": git,
            }
        ),
        encoding="utf-8",
    )
    _cleanup_failed_canary(home)
    assert not Path(git["worktree"]).exists()
    branch = run(
        (
            "git", "-C", git["repository"], "for-each-ref", "--format=%(objectname)",
            f"refs/heads/{git['branch']}",
        )
    )
    assert branch == ""
    assert Path(git["repository"]).is_dir()


def write_fake_herdr(root: Path, state_path: Path, agent_name: str = "cleanupcanary") -> Path:
    binary = root / "fake-bin/herdr"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

path = pathlib.Path(os.environ["LEAGUE_FAKE_HERDR_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args[:2] == ["agent", "list"]:
    agents = []
    if state["agent"]:
        agents = [{
            "agent": "codex",
            "name": state["agent_name"],
            "pane_id": "w-test:p-canary",
            "agent_status": "done" if state["done"] else "idle",
            "agent_session": {"value": "canary-session"},
        }]
    result = {"agents": agents}
elif args[:2] == ["agent", "prompt"]:
    if "--until" in args:
        raise SystemExit(2)
    state["done"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    result = {"submitted": True}
elif args[:2] == ["pane", "list"]:
    panes = []
    if state["pane"]:
        panes = [{"pane_id": "w-test:p-canary", "terminal_id": "terminal-canary"}]
    result = {"panes": panes}
elif args[:2] == ["pane", "close"]:
    state["pane"] = False
    state["agent"] = False
    path.write_text(json.dumps(state), encoding="utf-8")
    result = {"closed": True}
else:
    raise SystemExit(2)
print(json.dumps({"result": result}, sort_keys=True))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    state_path.write_text(
        json.dumps(
            {"agent": True, "pane": True, "done": False, "agent_name": agent_name}
        ),
        encoding="utf-8",
    )
    return binary.parent


def test_failure_scope_closes_exact_fake_herdr(root: Path) -> None:
    namespace = "testfailureherdr"
    home = root / f"league-real-canary-{namespace}"
    home.mkdir(parents=True)
    git = _create_git_canary(home)
    agent_name = "l23" + hashlib.sha256(namespace.encode()).hexdigest()[:12]
    fake_state = home / "fake-herdr.json"
    fake_bin = write_fake_herdr(home, fake_state, agent_name)
    (home / "failure-scope.json").write_text(
        json.dumps(
            {
                "schema": "league.real-canary-failure-scope.v1",
                "namespace": namespace,
                "git": git,
                "herdr": herdr_identity(agent_name),
            }
        ),
        encoding="utf-8",
    )
    previous_path = os.environ.get("PATH")
    previous_state = os.environ.get("LEAGUE_FAKE_HERDR_STATE")
    os.environ["PATH"] = str(fake_bin) + os.pathsep + (previous_path or "/usr/bin:/bin")
    os.environ["LEAGUE_FAKE_HERDR_STATE"] = str(fake_state)
    try:
        _cleanup_failed_canary(home)
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
        if previous_state is None:
            os.environ.pop("LEAGUE_FAKE_HERDR_STATE", None)
        else:
            os.environ["LEAGUE_FAKE_HERDR_STATE"] = previous_state
    observed = json.loads(fake_state.read_text(encoding="utf-8"))
    assert observed == {
        "agent": False,
        "pane": False,
        "done": True,
        "agent_name": agent_name,
    }
    assert not Path(git["worktree"]).exists()


def cleanup_reconcile_command(
    root: Path,
    state: Path,
    manifest_path: Path,
    adapter_path: Path,
    fake_bin: Path,
    fake_state: Path,
    *,
    resume: bool,
) -> subprocess.CompletedProcess[str]:
    pycache = root / "pycache"
    pycache.mkdir(exist_ok=True)
    arguments = [
        str(ROOT / "bin/league"),
        "--state-root",
        str(state),
        "--no-wal",
        "cleanup",
        "reconcile",
        "--manifest",
        str(manifest_path),
        "--operation-id",
        "operation:true-restart",
        "--adapter-config",
        str(adapter_path),
        "--executor-id",
        "executor:resume" if resume else "executor:first",
        "--leased-until",
        "2026-01-01T01:14:00Z" if resume else "2026-01-01T01:03:00Z",
        "--at",
        "2026-01-01T01:04:00Z" if resume else "2026-01-01T01:02:00Z",
    ]
    if not resume:
        arguments.append("--simulate-interruption-after-archive")
    environment = {
        **os.environ,
        "HOME": str(root / "process-home"),
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPYCACHEPREFIX": str(pycache),
        "LEAGUE_FAKE_HERDR_STATE": str(fake_state),
        "LC_ALL": "C",
    }
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )


def test_cleanup_reconcile_resumes_in_a_new_process(root: Path) -> None:
    root.mkdir(parents=True)
    git = _create_git_canary(root)
    herdr = herdr_identity()
    setup = _setup_sqlite(
        root, ROOT, git, herdr, issue_spec_resolver=issue_bound_spec
    )
    state = root / "league/state"
    with SQLiteStorage(state, request_wal=False) as store:
        transition = store.transition_task(
            LIFECYCLE_TASK_ID,
            herdr["runtime_instance_id"],
            3,
            "completed",
            "Disposable Champion completed",
            "Automatically execute exact cleanup",
            None,
            "transition:true-restart",
            "transition-key:true-restart",
            "event:true-restart",
            "outbox:true-restart",
            SHOTCALLER_ID,
            "2026-01-01T01:01:00Z",
        )
    before = _settle_transition_and_request(state, transition, setup["dispatch"])
    assert before["hook_decision"] == "block"
    manifest_path, adapter_path = _cleanup_files(
        root,
        git,
        herdr,
        f"callsign-assignment:{setup['assignment']['assignment_id']}",
        "Lux",
    )
    fake_state = root / "fake-herdr-state.json"
    fake_bin = write_fake_herdr(root, fake_state)

    first = cleanup_reconcile_command(
        root, state, manifest_path, adapter_path, fake_bin, fake_state, resume=False
    )
    first_value = json.loads(first.stdout if first.stdout.strip() else first.stderr)
    assert first.returncode == 3
    assert first_value["error"]["code"] == "cleanup_interrupted"
    with SQLiteStorage(state, request_wal=False) as store:
        interrupted = store.cleanup_operation("operation:true-restart")
        assert interrupted is not None
        assert interrupted["state"] == "executing" and interrupted["fence"] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM cleanup_action_receipts WHERE operation_id=?",
            ("operation:true-restart",),
        ).fetchone()[0] == 0
    assert (root / "archive/identity-evidence.json").is_file()

    resumed = cleanup_reconcile_command(
        root, state, manifest_path, adapter_path, fake_bin, fake_state, resume=True
    )
    resumed_value = json.loads(resumed.stdout if resumed.stdout.strip() else resumed.stderr)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert resumed_value["result"]["automatic_after_proof"] is True
    assert resumed_value["result"]["execution"]["state"] == "cleanup_completed"
    with SQLiteStorage(state, request_wal=False) as store:
        operation = store.cleanup_operation("operation:true-restart")
        assert operation is not None and operation["state"] == "completed"
        assert operation["fence"] == 2
        assert store.connection.execute(
            "SELECT cleanup_state FROM cleanup_obligations WHERE task_id=?",
            (LIFECYCLE_TASK_ID,),
        ).fetchone()[0] == "cleanup_completed"
    assert json.loads(fake_state.read_text(encoding="utf-8"))["pane"] is False
    assert not Path(git["worktree"]).exists()
    assert Path(git["repository"]).is_dir()


def test_session_title_fallback_and_strict_canary_schemas() -> None:
    session_id = "01234567-89ab-cdef-0123-456789abcdef"
    assert _codex_session_id({"terminal_title_stripped": f"{session_id} | codex"}) == session_id
    assert _codex_session_id({"terminal_title_stripped": "unrelated"}) is None
    refused(
        lambda: SubprocessRunner().run(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * (1024 * 1024 + 1))",
            )
        ),
        "cleanup_adapter_output_too_large",
    )
    for name in (
        "league-cleanup-canary-adapters.schema.json",
        "league-real-cleanup-artifact-profile.schema.json",
        "league-real-cleanup-canary-receipt.schema.json",
    ):
        schema = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
    receipt = json.loads(
        (ROOT / "schema/league-real-cleanup-canary-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["properties"]["supervision"]["properties"] == {
        "normal_wake": {"const": "event_driven"},
        "readiness_wait_milliseconds": {"const": 30000},
        "maximum_observations": {"const": 2},
        "periodic_unchanged_messages": {"const": 0},
        "separate_15_second_policy": {"const": False},
    }


def test_cursor_and_pi_use_provider_exit_contract() -> None:
    action = {
        "expected_identity": {
            "agent_name": "cleanupcanary",
            "pane_id": "w-test:p-canary",
            "session_id": "canary-session",
        },
        "intended_state": {"completed": True, "action": "session_exit"},
    }
    for provider, exit_prompt in (("cursor", "/exit"), ("pi", "/quit")):
        runner = FakeHerdrRunner(provider)
        identity = {
            **herdr_identity(),
            "provider_kind": provider,
            "exit_prompt": exit_prompt,
        }
        adapter = HerdrHarnessAdapter(identity, runner)
        assert adapter.inspect(action) == action["expected_identity"]
        adapter.apply(action)
        assert any(
            call[:5]
            == ("herdr", "agent", "prompt", "cleanupcanary", exit_prompt)
            for call in runner.calls
        )


def test_canary_start_retries_only_atomic_shell_preflight() -> None:
    args = ("agent", "start", "synthetic", "--pane", "w-test:p-test")
    def outcome(code: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, int(code is not None),
            json.dumps({"error": {"code": code}} if code else {"ok": True}), "")
    with patch.object(real_canary, "_run", side_effect=[outcome("agent_pane_busy"), outcome(None)]) as run, \
            patch.object(real_canary.time, "sleep") as sleep:
        assert real_canary._herdr(args, Path("/synthetic")) == {"ok": True}
        assert run.call_count == 2 and run.call_args_list[0] == run.call_args_list[1]
        sleep.assert_called_once_with(0.1)
    for code, attempts in (("agent_pane_busy", 21), ("timeout", 1), ("agent_already_running", 1)):
        with patch.object(real_canary, "_run", return_value=outcome(code)) as run, \
                patch.object(real_canary.time, "sleep") as sleep:
            refused(lambda: real_canary._herdr(args, Path("/synthetic")), "real_canary_command_failed")
            assert run.call_count == attempts and sleep.call_count == attempts - 1


def test_canary_readiness_requires_reply_without_trust_override(root: Path) -> None:
    for reply, stalled, trust in ((False, False, False), (True, False, False), (True, True, False), (False, False, True), (False, False, "refused"), (True, False, "yolo"), (True, False, "human")):
        home = root / f"reply-{reply}-stalled-{stalled}-trust-{trust}"
        worktree = home / "git/worktree"
        worktree.mkdir(parents=True)
        (worktree.parent / "repository").mkdir()
        (home / "failure-scope.json").write_text("{}", encoding="utf-8")
        calls: list[tuple[str, ...]] = []
        prompt = ""
        token = ""
        human_trusted = False

        def fake_run(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal prompt, token, human_trusted
            args = tuple(arguments)
            calls.append(args)
            result = ""
            code = 0
            if args[:3] == ("herdr", "agent", "prompt"):
                prompt = args[4]
                token = "LEAGUE23_CANARY_READY_" + prompt.split('"')[5]
                assert token not in prompt
                code = int(stalled)
                result = json.dumps({"error": {"code": "agent_prompt_stalled"}} if stalled else {"ok": True})
            elif args[:3] == ("herdr", "pane", "read"):
                if trust == "human" and "visible" in args:
                    human_trusted = True  # Synthetic human interaction, not agent input.
                result = "gpt-6-astra high\n" + prompt
                if trust and trust != "yolo" and not human_trusted:
                    result = "Do you trust the\n contents of this directory?"
                elif reply:
                    result += "\n" + token
            elif args[0] == "git":
                if "rev-parse" in args:
                    result = real_canary.REPORT_TESTED_HEAD
                elif "branch" in args:
                    result = real_canary.REPORT_BRANCH
            else:
                assert args[:3] == ("herdr", "pane", "wait-output"), args
            return subprocess.CompletedProcess(args, code, result, "")

        def fake_herdr(arguments: Sequence[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(("herdr", *arguments))
            if arguments[:2] == ("pane", "split"):
                assert ("--env" in arguments) == (trust == "yolo")
                if trust == "yolo":
                    assert f"ZDOTDIR={home / 'shell-config'}" in arguments
                    shell_config = home / "shell-config"
                    assert [path.name for path in shell_config.iterdir()] == [".zshrc"]
                    expected_python = real_canary.shlex.quote(str(Path(sys.executable).resolve().parent))
                    assert (shell_config / ".zshrc").read_text() == f'export PATH={expected_python}:"$PATH"\n'
                return {"result": {"pane": {"pane_id": "w-test:p-test"}}}
            assert arguments[:2] == ("agent", "start")
            assert "gpt-6-astra" in arguments
            assert not any("trust_level" in part for part in arguments)
            assert ("--dangerously-bypass-approvals-and-sandbox" in arguments) == (trust == "yolo")
            if trust in ("refused", "human"):
                raise StorageRefusal("real_canary_command_failed", "synthetic startup refusal")
            return {"ok": True}

        identity = {
            "agent": "codex", "workspace_id": "w-test", "pane_id": "w-test:p-test",
            "terminal_id": "terminal-test", "agent_session": {"value": "session-test"},
        }
        with patch.object(real_canary, "_run", side_effect=fake_run), patch.object(
            real_canary, "_herdr", side_effect=fake_herdr
        ), patch.object(real_canary, "_exact_agent", side_effect=[None, identity, identity] if trust == "human" else [None, identity]):
            if trust and trust not in ("yolo", "human"):
                refused(lambda: real_canary._create_herdr_canary(home, worktree, "test"), "real_canary_trust_required")
                scope = json.loads((home / "failure-scope.json").read_text())
                assert scope["herdr"]["startup_blocker"] == "directory_trust"
            elif reply:
                assert real_canary._create_herdr_canary(home, worktree, "test", codex_yolo=trust == "yolo",
                    trust_wait_seconds=1 if trust == "human" else 0)["session_id"] == "session-test"
            else:
                refused(lambda: real_canary._create_herdr_canary(home, worktree, "test"), "real_canary_readiness_unproven")
        assert sum(call[:3] == ("herdr", "agent", "prompt") for call in calls) == (0 if trust and trust not in ("yolo", "human") else 1)
        assert not any(call[:3] == ("herdr", "pane", "run") for call in calls)


def test_trust_gate_cleanup_cancels_only_exact_canary() -> None:
    namespace = "trust-fixture"
    name = "l23" + hashlib.sha256(namespace.encode()).hexdigest()[:12]
    identity = {"agent": "codex", "pane_id": "w-test:p-canary", "agent_status": "idle"}
    scope = {"agent_name": name, "pane_id": identity["pane_id"], "workspace_id": "w-test",
             "startup_blocker": "directory_trust"}
    screen = "Do you trust the\n contents of this directory?"
    for mode in ("exit", "changed", "stuck", "wrong-pane"):
        calls = []
        def fake_herdr(arguments: Sequence[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(tuple(arguments))
            if arguments[:2] == ("pane", "list"):
                return {"result": {"panes": [{"pane_id": identity["pane_id"]}]}}
            assert arguments in (("agent", "send-keys", name, "ctrl+c"), ("pane", "close", identity["pane_id"]))
            return {}
        observations = [identity, None] if mode == "exit" else None
        observed = {**identity, "pane_id": "w-test:p-other"} if mode == "wrong-pane" else identity
        with patch.object(real_canary, "_exact_agent", side_effect=observations, return_value=observed), patch.object(
            real_canary, "_run", return_value=subprocess.CompletedProcess([], 0, "ready" if mode == "changed" else screen, "")
        ), patch.object(real_canary, "_herdr", side_effect=fake_herdr), patch.object(real_canary.time, "sleep"):
            if mode == "exit":
                real_canary._cleanup_failed_herdr(Path("."), namespace, scope)
            else:
                refused(lambda: real_canary._cleanup_failed_herdr(Path("."), namespace, scope), "real_canary_failure_cleanup_refused")
        assert ("pane", "close", identity["pane_id"]) in calls if mode == "exit" else not any(c[:2] == ("pane", "close") for c in calls)
        assert not any(c[:2] == ("agent", "prompt") for c in calls)
        if mode in ("changed", "wrong-pane"):
            assert not calls


def test_canary_failure_diagnostics_do_not_echo_private_data() -> None:
    secret = "private-path-or-prompt"
    arguments = ("herdr", "agent", "start", secret)
    cases = (
        (subprocess.CompletedProcess(arguments, 2, secret, secret), "herdr agent start exited with code 2"),
        (subprocess.TimeoutExpired(arguments, 1, output=secret), "herdr agent start timed out"),
        (OSError(secret), "herdr agent start could not start"),
    )
    for outcome, expected in cases:
        mocked = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
        with patch.object(real_canary.subprocess, "run", **mocked):
            try:
                real_canary._run(arguments, cwd=Path("."))
            except StorageRefusal as exc:
                assert exc.code == "real_canary_command_failed"
                assert str(exc) == expected, str(exc)
                assert secret not in str(exc)
            else:
                raise AssertionError("expected command refusal")
    assert real_canary._command_label((secret, secret)) == "canary subprocess"
    with patch.object(real_canary.subprocess, "run", return_value=cases[0][0]):
        assert real_canary._run(arguments, cwd=Path("."), allowed=frozenset({2})).returncode == 2
    with patch.object(real_canary, "_real_canary_paths", return_value=(Path("."),) * 3), patch.object(
        real_canary, "_prepare_real_canary", side_effect=StorageRefusal("real_canary_command_failed", secret)
    ), patch.object(
        real_canary, "_cleanup_failed_canary", side_effect=StorageRefusal("real_canary_failure_cleanup_refused", secret)
    ):
        try:
            real_canary.run_real_cleanup_canary(Path("."), "synthetic")
        except StorageRefusal as exc:
            assert exc.code == "real_canary_failure_cleanup_failed"
            assert "primary=real_canary_command_failed" in str(exc)
            assert "cleanup=real_canary_failure_cleanup_refused" in str(exc)
            assert secret not in str(exc)
        else:
            raise AssertionError("expected cleanup refusal")


def main() -> None:
    test_canary_failure_diagnostics_do_not_echo_private_data()
    test_trust_gate_cleanup_cancels_only_exact_canary()
    test_canary_start_retries_only_atomic_shell_preflight()
    with tempfile.TemporaryDirectory(prefix="league-real-cleanup-") as directory:
        root = Path(directory)
        test_canary_readiness_requires_reply_without_trust_override(root / "readiness")
        test_archive_git_and_scope(root / "git")
        test_herdr_and_callsign_exact_cleanup(root / "runtime")
        test_cursor_and_pi_use_provider_exit_contract()
        test_repository_artifact_squash_tree_is_cleanup_eligible(root / "artifact")
        test_squash_on_advanced_base_requires_exact_reconstruction(root / "advanced-squash")
        test_backend_close_resumes_after_external_failure(root / "backend-retry")
        test_real_canary_sqlite_setup_uses_explicit_root(root / "canary-setup")
        test_failure_scope_removes_only_exact_git_resources(root / "failure-cleanup")
        test_failure_scope_closes_exact_fake_herdr(root / "failure-herdr")
        test_cleanup_reconcile_resumes_in_a_new_process(root / "true-restart")
    test_session_title_fallback_and_strict_canary_schemas()
    print(
        "PASS: exact-root adapter cleanup, true-process restart recovery, "
        "session fallback, and strict canary schemas"
    )


if __name__ == "__main__":
    main()
