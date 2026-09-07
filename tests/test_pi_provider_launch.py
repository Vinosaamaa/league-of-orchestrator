#!/usr/bin/env python3
"""Focused Pi runtime/provider launch, lineage, metadata, and restart tests."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.pi_launch import (  # noqa: E402
    HerdrPiLaunchAdapter,
    deterministic_pi_session_id,
    pi_metadata_source,
    pi_launch_environment,
    pi_start_arguments,
    resume_pi_after_restart,
)
from league.pi_session_migration import (  # noqa: E402
    _inventory_identity,
    migrate_pi_session,
)
from league.real_cleanup import HerdrHarnessAdapter  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.agent_adapters import builtin_agent_adapter_registry  # noqa: E402
from league.multiplexer_adapters import builtin_multiplexer_adapter_registry  # noqa: E402
from league.request_services import AssignmentSpec  # noqa: E402
from request_lifecycle_fixture import LUX_ID, create_context  # noqa: E402
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402
from test_visible_champion_launch import (  # noqa: E402
    FakeIssueVerifier,
    _context,
    _options,
    _spec as champion_spec,
)
from league.visible_launch import VisibleChampionLaunchService  # noqa: E402
from dataclasses import replace


PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


class FakePiHerdr:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.running = False
        self.endpoint = {
            "workspace_id": "w1",
            "tab_id": "w1:t84",
            "pane_id": "w1:p84",
            "terminal_id": "term_pi_84",
        }
        self.env: dict[str, str] = {}
        self.pi_arguments: list[str] = []
        self.session_id = ""
        self.session_path = ""
        self.parent_path: str | None = None
        self.start_count = 0
        self.start_failure = None
        self.start_environment_override = {}
        self.calls: list[tuple[str, ...]] = []
        self.native_title_reads_remaining = 0
        self.agent_get_count = 0
        self.report_process_argv = True
        self.launch_metadata_available = True
        self.native_session_available = True
        self.presentation_title: str | None = None
        self.presentation_source: str | None = None
        self.presentation_tokens: dict[str, str] = {}
        self.state_change_seq = 84
        self.context_title: str | None = None
        self.context_source = "herdr:pi"
        self.after_report_title: str | None = None
        self.after_report_reads = 0

    @staticmethod
    def _completed(arguments, result, returncode=0):
        payload = {"result": result} if returncode == 0 else {"error": {"code": "not_found"}}
        return subprocess.CompletedProcess(arguments, returncode, json.dumps(payload) + "\n", "")

    @staticmethod
    def _pairs(arguments: list[str], flag: str) -> list[str]:
        return [arguments[index + 1] for index, item in enumerate(arguments[:-1]) if item == flag]

    def stop_for_restart(self) -> None:
        self.running = False
        self.pi_arguments = []
        self.launch_metadata_available = False
        self.native_session_available = False

    def _agent(self) -> dict:
        if self.after_report_reads:
            self.after_report_reads -= 1
            if self.after_report_reads == 0:
                self.presentation_title = self.after_report_title
                self.presentation_source = "user-selected"
                self.presentation_tokens["sidebar_name"] = "User sidebar"
                self.state_change_seq += 1
        callsign = self.env["LEAGUE_CALLSIGN"]
        provider = self.env["LEAGUE_PROVIDER_KIND"]
        role = self.env["LEAGUE_LAUNCH_ROLE"]
        title = (
            callsign
            if role == "shotcaller"
            else f"{callsign} · {self.env['LEAGUE_PROJECT_CODE']}|{self.env['LEAGUE_TASK_LABEL']}"
        )
        tokens = {
            "runtime_kind": "pi",
            "provider_kind": provider,
            "role": role,
            "placement": self.env["LEAGUE_LAUNCH_PLACEMENT"],
            "sidebar_name": callsign,
            "project_code": self.env["LEAGUE_PROJECT_CODE"],
            "task_label": self.env["LEAGUE_TASK_LABEL"],
            "routing_alias": self.env["LEAGUE_ROUTING_ALIAS"],
            "session_id": self.session_id,
            "session_path": self.session_path,
            "thread_title": title,
            "activation_phase": "session_started",
        }
        if self.launch_metadata_available:
            tokens.update({
                "launch_runtime_kind": "pi",
                "launch_provider_kind": provider,
                "launch_role": role,
                "launch_placement": self.env["LEAGUE_LAUNCH_PLACEMENT"],
                "launch_callsign": callsign,
                "launch_project_code": self.env["LEAGUE_PROJECT_CODE"],
                "launch_task_label": self.env["LEAGUE_TASK_LABEL"],
                "launch_routing_alias": self.env["LEAGUE_ROUTING_ALIAS"],
                "launch_session_id": self.session_id,
                "launch_session_path_digest": __import__("hashlib").sha256(self.session_path.encode()).hexdigest(),
                "launch_descriptor_sha256": self.env["LEAGUE_LAUNCH_DESCRIPTOR_DIGEST"],
                "launch_descriptor_id": self.env["LEAGUE_LAUNCH_DESCRIPTOR_ID"],
                "launch_state_root": self.env["LEAGUE_STATE_ROOT"],
                "launch_metadata_source": self.env["LEAGUE_LAUNCH_METADATA_SOURCE"],
                "launch_activation_phase": "session_started",
            })
        if self.parent_path:
            tokens["parent_session_path"] = self.parent_path
            if self.launch_metadata_available:
                tokens["launch_parent_digest"] = __import__("hashlib").sha256(self.parent_path.encode()).hexdigest()
        value = {
            "agent": "pi",
            "agent_status": "idle",
            "interactive_ready": True,
            "name": self.env["LEAGUE_ROUTING_ALIAS"],
            "display_agent": provider,
            "metadata_source": self.env["LEAGUE_LAUNCH_METADATA_SOURCE"],
            "cwd": self.env["LEAGUE_WORKTREE"],
            "foreground_cwd": self.env["LEAGUE_WORKTREE"],
            "terminal_title": title,
            "title": f"◇ Cursor | {title}",
            "tokens": tokens,
            "state_change_seq": 84,
            **self.endpoint,
        }
        if self.native_session_available:
            value["agent_session"] = {
                "agent": "pi", "kind": "path", "source": "herdr:pi", "value": self.session_path,
            }
        if self.native_title_reads_remaining:
            self.native_title_reads_remaining -= 1
            value["terminal_title"] = f"π - {self.session_id} - worktree"
        if self.presentation_title is not None:
            value["terminal_title"] = self.presentation_title
        if self.presentation_source is not None:
            value["metadata_source"] = self.presentation_source
        value["tokens"].update(self.presentation_tokens)
        value["state_change_seq"] = self.state_change_seq
        return value

    def run(self, arguments, *, timeout_seconds=30):
        arguments = list(arguments)
        self.calls.append(tuple(arguments))
        if arguments[1:3] == ["agent", "list"]:
            return self._completed(arguments, {"agents": [self._agent()] if self.running else []})
        if arguments[1:3] == ["tab", "create"]:
            for pair in self._pairs(arguments, "--env"):
                key, value = pair.split("=", 1)
                self.env[key] = value
            return self._completed(
                arguments,
                {
                    "tab": {"tab_id": self.endpoint["tab_id"]},
                    "root_pane": {
                        "pane_id": self.endpoint["pane_id"],
                        "terminal_id": self.endpoint["terminal_id"],
                    },
                },
            )
        if arguments[1:3] == ["pane", "split"]:
            for pair in self._pairs(arguments, "--env"):
                key, value = pair.split("=", 1)
                self.env[key] = value
            return self._completed(
                arguments,
                {
                    "tab_id": self.endpoint["tab_id"],
                    "pane": {
                        "pane_id": self.endpoint["pane_id"],
                        "terminal_id": self.endpoint["terminal_id"],
                    },
                },
            )
        if arguments[1:3] == ["agent", "start"]:
            if self.start_failure is not None:
                return subprocess.CompletedProcess(arguments, 1, "", self.start_failure)
            self.env.update(self.start_environment_override)
            self.start_count += 1
            self.pi_arguments = arguments[arguments.index("--") + 1 :]
            explicit = {
                "league-state-root": "LEAGUE_STATE_ROOT",
                "league-pane-id": "HERDR_PANE_ID",
                "league-watcher-command": "LEAGUE_WATCHER_COMMAND",
                "league-worktree": "LEAGUE_WORKTREE",
                "league-sandbox-profile": "LEAGUE_PI_SANDBOX_PROFILE",
                "league-runtime-kind": "LEAGUE_RUNTIME_KIND",
                "league-provider-kind": "LEAGUE_PROVIDER_KIND",
                "league-role": "LEAGUE_LAUNCH_ROLE",
                "league-placement": "LEAGUE_LAUNCH_PLACEMENT",
                "league-callsign": "LEAGUE_CALLSIGN",
                "league-project-code": "LEAGUE_PROJECT_CODE",
                "league-task-label": "LEAGUE_TASK_LABEL",
                "league-routing-alias": "LEAGUE_ROUTING_ALIAS",
                "league-descriptor-digest": "LEAGUE_LAUNCH_DESCRIPTOR_DIGEST",
                "league-descriptor-id": "LEAGUE_LAUNCH_DESCRIPTOR_ID",
                "league-metadata-source": "LEAGUE_LAUNCH_METADATA_SOURCE",
            }
            for flag, key in explicit.items():
                option = f"--{flag}"
                if option in self.pi_arguments:
                    self.env[key] = self.pi_arguments[self.pi_arguments.index(option) + 1]
            if "--fork" in self.pi_arguments:
                self.session_id = CHILD_ID
                self.session_path = str((self.root / "child-session.jsonl").resolve())
                self.parent_path = self.pi_arguments[self.pi_arguments.index("--fork") + 1]
            elif "--session-id" in self.pi_arguments:
                self.session_id = self.pi_arguments[self.pi_arguments.index("--session-id") + 1]
                self.session_path = str((self.root / f"{self.session_id}.jsonl").resolve())
                self.parent_path = None
            else:
                self.session_path = self.pi_arguments[self.pi_arguments.index("--session") + 1]
            self.running = True
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] == ["agent", "get"]:
            self.agent_get_count += 1
            return self._completed(
                arguments,
                {"agent": self._agent()} if self.running else {},
                0 if self.running else 1,
            )
        if arguments[1:3] == ["pane", "process-info"]:
            process = {
                "name": "node", "argv0": "pi", "pid": 8401,
                "cwd": self.env["LEAGUE_WORKTREE"],
            }
            if self.report_process_argv:
                process["argv"] = ["pi", *self.pi_arguments]
            processes = (
                [process]
                if self.running
                else [{
                    "name": "zsh", "argv0": "zsh", "argv": ["-zsh"],
                    "pid": 8399, "cwd": self.env["LEAGUE_WORKTREE"],
                }]
            )
            return self._completed(
                arguments,
                {
                    "process_info": {
                        "pane_id": self.endpoint["pane_id"],
                        "shell_pid": 8399,
                        "foreground_process_group_id": 8401 if self.running else 8399,
                        "foreground_processes": processes,
                    }
                },
            )
        if arguments[1:3] == ["pane", "get"]:
            role = self.env["LEAGUE_LAUNCH_ROLE"]
            label = (
                self.env["LEAGUE_CALLSIGN"]
                if role == "shotcaller"
                else f"{self.env['LEAGUE_CALLSIGN']} · {self.env['LEAGUE_PROJECT_CODE']}|{self.env['LEAGUE_TASK_LABEL']}"
            )
            return self._completed(arguments, {"pane": {**self._agent(), "label": label}})
        if arguments[1:3] == ["pane", "rename"]:
            return self._completed(arguments, {"renamed": True})
        if arguments[1:3] in (["pane", "report-agent-session"], ["pane", "report-metadata"]):
            if arguments[1:3] == ["pane", "report-metadata"]:
                self.launch_metadata_available = True
                self.state_change_seq += 1
                if "--title" in arguments:
                    self.presentation_title = arguments[arguments.index("--title") + 1]
                    self.presentation_source = arguments[arguments.index("--source") + 1]
                for token in self._pairs(arguments, "--token"):
                    key, value = token.split("=", 1)
                    self.presentation_tokens[key] = value
                if self.after_report_title is not None and "--title" in arguments:
                    self.after_report_reads = 3
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] == ["agent", "prompt"]:
            if self.context_title is not None:
                self.presentation_title = self.context_title
                self.presentation_source = self.context_source
                self.presentation_tokens.update(
                    sidebar_name=self.context_title, thread_title=self.context_title
                )
                self.state_change_seq += 1
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] in (["tab", "close"], ["pane", "close"]):
            self.running = False
            return self._completed(arguments, {"closed": True})
        raise AssertionError(f"unexpected Herdr command: {arguments}")


class PiCleanupInspectionRunner:
    def __init__(self, pi: FakePiHerdr) -> None:
        self.pi = pi

    def run(self, arguments, *, allow_failure=False):
        exact = tuple(arguments)
        if exact[1:3] != ("agent", "list"):
            raise AssertionError(exact)
        return subprocess.CompletedProcess(
            list(exact),
            0,
            json.dumps({"result": {"agents": [self.pi._agent()]}}),
            "",
        )


def _spec(worktree: Path) -> AssignmentSpec:
    return AssignmentSpec(
        assignment_id="assignment:pi-provider",
        request_id="request:pi-provider",
        claim_token="claim:pi-provider",
        task_id="task:pi-provider",
        task_summary="Preserve Pi session lifecycle",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository=REPOSITORY,
        issue=84,
        branch="agent/synthetic/pi-provider",
        worktree=str(worktree),
        issue_receipt=None,
        callsign="Lux",
    )


def _descriptor(root: Path, worktree: Path, provider: str, mode: str) -> dict:
    descriptor_id = f"pi-launch:assignment:{provider}:{mode}"
    parent_path = str((root / "parent-session.jsonl").resolve())
    return {
        "schema": "league.pi-launch-descriptor.v1",
        "descriptor_id": descriptor_id,
        "assignment_id": "assignment:pi-provider",
        "runtime_kind": "pi",
        "provider_kind": provider,
        "model": "grok-4.6" if provider == "cursor" else "gpt-5.6-codex",
        "effort": "xhigh",
        "cwd": str(worktree.resolve()),
        "role": "champion",
        "placement": "new_tab",
        "callsign": "pending",
        "project_code": "LEAGUE",
        "task_label": "Session Restore",
        "routing_name": "pending",
        "workspace_id": "w1",
        "creator_pane_id": None,
        "state_root": str((root / "state").resolve()),
        "release_root": str(ROOT),
        "launch_mode": mode,
        "requested_session_id": (
            deterministic_pi_session_id(descriptor_id) if mode == "create" else None
        ),
        "requested_session_path": None,
        "parent_session_id": PARENT_ID if mode == "fork" else None,
        "parent_session_path": parent_path if mode == "fork" else None,
    }


def test_fork_metadata_restart_and_duplicate_suppression(root: Path) -> None:
    state, store, _clock = create_context(root, "pi-fork")
    worktree = root / "pi-fork" / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    descriptor = _descriptor(root / "pi-fork", worktree, "cursor", "fork")
    fake = FakePiHerdr(root / "pi-fork")
    fake.native_title_reads_remaining = 1
    fake.report_process_argv = False
    adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:00:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    receipt = adapter.launch(_spec(worktree))
    assert receipt["harness_kind"] == "pi-thread"
    assert receipt["display_agent"] == "cursor"
    assert receipt["thread_id"] == fake.session_path
    assert "session_id" not in receipt and "session_path" not in receipt
    cleanup_action = {
        "expected_identity": {
            "agent_name": receipt["routing_name"],
            "pane_id": receipt["endpoint"],
            "session_id": receipt["thread_id"],
        },
        "intended_state": {"completed": True, "action": "session_exit"},
    }
    assert HerdrHarnessAdapter(
        {
            "agent_name": receipt["routing_name"],
            "provider_kind": "pi",
            "exit_prompt": "/quit",
        },
        PiCleanupInspectionRunner(fake),
    ).inspect(cleanup_action) == cleanup_action["expected_identity"]
    assert fake.agent_get_count >= 3
    assert any(call[1:3] == ("tab", "create") for call in fake.calls)
    start = next(call for call in fake.calls if call[1:3] == ("agent", "start"))
    assert "pi-cursor" not in start
    assert "--approve" in start
    assert ("--provider", "cursor") == tuple(
        start[start.index("--provider") : start.index("--provider") + 2]
    )
    assert "--fork" in start and descriptor["parent_session_path"] in start
    stored = store.provider_launch_descriptor(descriptor["descriptor_id"])
    assert stored["session_id"] == CHILD_ID
    assert stored["launch_receipt"]["parent_session_path"] == descriptor["parent_session_path"]
    assert stored["launch_receipt"]["cwd"] == str(worktree.resolve())
    assert stored["launch_receipt"]["task_label"] == "Session Restore"

    retried_adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:00:30Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    retried = retried_adapter.launch(_spec(worktree))
    assert retried["thread_id"] == fake.session_path and fake.start_count == 1
    delivered = retried_adapter.deliver_context(
        retried, "Retry the exact bounded Pi assignment context."
    )
    assert delivered["display_receipt"]["orchestrator_role"] == "champion"
    assert delivered["display_receipt"]["thread_title"] == "Lux · LEAGUE|Session Restore"
    assert delivered["display_receipt"]["state_change_seq"] == fake.state_change_seq

    fake.stop_for_restart()
    resumed = resume_pi_after_restart(
        store,
        descriptor_id=descriptor["descriptor_id"],
        restart_id="restart:one",
        pane_id=fake.endpoint["pane_id"],
        at="2026-01-01T00:01:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    assert resumed["state"] == "effect_applied"
    assert fake.start_count == 2
    restart_start = [call for call in fake.calls if call[1:3] == ("agent", "start")][-1]
    assert "--session" in restart_start and stored["session_path"] in restart_start
    assert "--fork" not in restart_start
    assert "--league-metadata-source" in restart_start
    reports = [call for call in fake.calls if call[1:3] == ("pane", "report-metadata")]
    assert all(call.count("--token") <= 16 for call in reports)
    assert any("--clear-token" in call and "launch_descriptor_digest" in call for call in reports)
    assert any("--applies-to-source" in call and "launch_metadata_source=" in " ".join(call) for call in reports)
    duplicate = resume_pi_after_restart(
        store,
        descriptor_id=descriptor["descriptor_id"],
        restart_id="restart:one",
        pane_id=fake.endpoint["pane_id"],
        at="2026-01-01T00:01:01Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    assert duplicate["idempotent"] is True and fake.start_count == 2

    fake.stop_for_restart()
    (worktree / ".git").rename(worktree / ".git.owner")
    (worktree / ".git").mkdir()
    try:
        resume_pi_after_restart(
            store,
            descriptor_id=descriptor["descriptor_id"],
            restart_id="restart:worktree-replaced",
            pane_id=fake.endpoint["pane_id"],
            at="2026-01-01T00:01:02Z",
            runner=fake,
            environment={"HERDR_ENV": "1"},
        )
    except StorageRefusal as exc:
        assert exc.code == "provider_restart_worktree_mismatch"
    else:
        raise AssertionError("restart trusted a cwd that was no longer the bound worktree")
    assert fake.start_count == 2
    store.close()


def test_unified_inventory_migration_preserves_bytes_and_lineage(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-migrate")
    base = root / "pi-migrate"
    worktree = base / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    legacy = base / "legacy-sessions"
    unified = base / "unified-sessions"
    unified.mkdir()
    relative = Path("--synthetic-project--") / "2026-01-01_child.jsonl"
    parent = legacy / "--parent-project--" / "2026-01-01_parent.jsonl"
    parent.parent.mkdir(parents=True)
    parent.write_text(
        json.dumps({"type": "session", "version": 3, "id": PARENT_ID, "cwd": str(base)}) + "\n",
        encoding="utf-8",
    )
    source = legacy / relative
    source.parent.mkdir(parents=True)
    payload = (
        json.dumps(
            {
                "type": "session", "version": 3, "id": CHILD_ID,
                "cwd": str(worktree.resolve()), "parentSession": str(parent.resolve()),
            },
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"type": "message", "id": "opaque-history", "text": "preserved"})
        + "\n"
    ).encode()
    source.write_bytes(payload)
    destination = unified / relative
    descriptor = _descriptor(base, worktree, "cursor", "fork")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:migrated:cursor",
            "launch_mode": "resume",
            "requested_session_id": CHILD_ID,
            "requested_session_path": str(destination.resolve()),
            "parent_session_id": PARENT_ID,
            "parent_session_path": str(parent.resolve()),
            "callsign": "Lux",
            "routing_name": "lux",
        }
    )
    fake = FakePiHerdr(base)
    fake.env["LEAGUE_WORKTREE"] = str(worktree.resolve())
    manifest = {
        "schema": "league.pi-session-migration.v1",
        "migration_id": "pi-migration:cursor-child",
        "source_inventory_root": str(legacy.resolve()),
        "unified_inventory_root": str(unified.resolve()),
        "relative_session_path": str(relative),
        "expected_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "descriptor": descriptor,
        "endpoint": fake.endpoint,
    }
    migrated = migrate_pi_session(
        store, manifest, at="2026-01-01T00:03:00Z", runner=fake
    )
    assert migrated["state"] == "bound"
    assert destination.read_bytes() == payload
    assert store.provider_launch_descriptor(descriptor["descriptor_id"])["session_path"] == str(destination.resolve())
    retried = migrate_pi_session(
        store, manifest, at="2026-01-01T00:03:01Z", runner=fake
    )
    assert retried["state"] == "bound" and retried["idempotent"] is True
    assert len(list(unified.rglob("*.jsonl"))) == 1

    fake.running = True
    refused_manifest = dict(manifest)
    refused_manifest["migration_id"] = "pi-migration:active-refusal"
    refused_descriptor = dict(descriptor)
    refused_descriptor["descriptor_id"] = "pi-launch:active-refusal"
    refused_manifest["descriptor"] = refused_descriptor
    try:
        migrate_pi_session(store, refused_manifest, at="2026-01-01T00:03:02Z", runner=fake)
    except StorageRefusal as exc:
        assert exc.code == "pi_session_migration_runtime_active"
    else:
        raise AssertionError("active Pi process migration was not refused")
    store.close()


def test_unified_inventory_duplicate_scan_refuses(root: Path) -> None:
    inventory = root / "pi-duplicate-inventory"
    for name in ("expected", "unexpected"):
        path = inventory / name / "same-session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": CHILD_ID,
                    "cwd": str(root.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    try:
        _inventory_identity(inventory, CHILD_ID)
    except StorageRefusal as exc:
        assert exc.code == "pi_session_identity_duplicate"
    else:
        raise AssertionError("bounded inventory scan stopped before finding a duplicate")


def test_already_unified_shotcaller_session_is_adopted_without_copy(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-adopt")
    base = root / "pi-adopt"
    project_folder = base / "project-folder"
    project_folder.mkdir(parents=True)
    unified = base / "unified-sessions"
    relative = Path("--synthetic-project--") / "2026-01-01_shotcaller.jsonl"
    session = unified / relative
    session.parent.mkdir(parents=True)
    payload = (
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": CHILD_ID,
                "cwd": str(project_folder.resolve()),
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    session.write_bytes(payload)
    descriptor = _descriptor(base, project_folder, "cursor", "resume")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:adopted:shotcaller",
            "assignment_id": "shotcaller:job-journey",
            "role": "shotcaller",
            "placement": "sibling_pane",
            "creator_pane_id": "w1:p1",
            "requested_session_id": CHILD_ID,
            "requested_session_path": str(session.resolve()),
            "parent_session_id": None,
            "parent_session_path": None,
            "callsign": "Ambessa",
            "project_code": "JJ",
            "task_label": "Squad Control",
            "routing_name": "ambessa",
        }
    )
    fake = FakePiHerdr(base)
    fake.env["LEAGUE_WORKTREE"] = str(project_folder.resolve())
    manifest = {
        "schema": "league.pi-session-migration.v1",
        "migration_id": "pi-adoption:shotcaller",
        "source_inventory_root": str(unified.resolve()),
        "unified_inventory_root": str(unified.resolve()),
        "relative_session_path": str(relative),
        "expected_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "descriptor": descriptor,
        "endpoint": fake.endpoint,
    }
    adopted = migrate_pi_session(
        store, manifest, at="2026-01-01T00:04:00Z", runner=fake
    )
    assert adopted["state"] == "bound"
    assert session.read_bytes() == payload
    stored = store.provider_launch_descriptor(descriptor["descriptor_id"])
    assert stored["session_path"] == str(session.resolve())
    assert stored["role"] == "shotcaller"
    assert migrate_pi_session(
        store, manifest, at="2026-01-01T00:04:01Z", runner=fake
    )["idempotent"] is True
    assert len(list(unified.rglob("*.jsonl"))) == 1
    store.close()


def test_already_unified_child_uses_bound_parent_evidence_without_legacy_profile(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-adopt-child")
    base = root / "pi-adopt-child"
    worktree = base / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    unified = base / "unified-sessions"
    child_relative = Path("--synthetic-child--") / "2026-01-01_child.jsonl"
    parent_relative = Path("--synthetic-parent--") / "2025-12-31_parent.jsonl"
    child = unified / child_relative
    parent_evidence = unified / parent_relative
    child.parent.mkdir(parents=True)
    parent_evidence.parent.mkdir(parents=True)
    retired_parent = base / "retired-profile" / parent_evidence.name
    parent_payload = (
        json.dumps(
            {"type": "session", "version": 3, "id": PARENT_ID, "cwd": str(base.resolve())},
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    child_payload = (
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": CHILD_ID,
                "cwd": str(worktree.resolve()),
                "parentSession": str(retired_parent.resolve()),
            },
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"type": "message", "id": "opaque-history", "text": "preserved"})
        + "\n"
    ).encode()
    parent_evidence.write_bytes(parent_payload)
    child.write_bytes(child_payload)
    descriptor = _descriptor(base, worktree, "cursor", "resume")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:adopted:child",
            "requested_session_id": CHILD_ID,
            "requested_session_path": str(child.resolve()),
            "parent_session_id": PARENT_ID,
            "parent_session_path": str(retired_parent.resolve()),
            "callsign": "Lux",
            "routing_name": "lux",
        }
    )
    fake = FakePiHerdr(base)
    fake.env["LEAGUE_WORKTREE"] = str(worktree.resolve())
    manifest = {
        "schema": "league.pi-session-migration.v2",
        "migration_id": "pi-adoption:child-parent-evidence",
        "source_inventory_root": str(unified.resolve()),
        "unified_inventory_root": str(unified.resolve()),
        "relative_session_path": str(child_relative),
        "expected_sha256": __import__("hashlib").sha256(child_payload).hexdigest(),
        "parent_evidence_path": str(parent_evidence.resolve()),
        "expected_parent_sha256": __import__("hashlib").sha256(parent_payload).hexdigest(),
        "descriptor": descriptor,
        "endpoint": fake.endpoint,
    }
    adopted = migrate_pi_session(
        store, manifest, at="2026-01-01T00:05:00Z", runner=fake
    )
    assert adopted["state"] == "bound"
    assert child.read_bytes() == child_payload
    assert parent_evidence.read_bytes() == parent_payload
    assert not retired_parent.exists()
    assert len(list(unified.rglob("*.jsonl"))) == 2
    receipt = adopted["receipt"]
    assert receipt["parent_session_path"] == str(retired_parent.resolve())
    assert receipt["parent_evidence_path"] == str(parent_evidence.resolve())

    refused = dict(manifest)
    refused["migration_id"] = "pi-adoption:child-tampered-parent"
    refused["expected_parent_sha256"] = "0" * 64
    refused_descriptor = dict(descriptor)
    refused_descriptor["descriptor_id"] = "pi-launch:adopted:child-tampered"
    refused["descriptor"] = refused_descriptor
    try:
        migrate_pi_session(store, refused, at="2026-01-01T00:05:01Z", runner=fake)
    except StorageRefusal as exc:
        assert exc.code == "pi_session_migration_digest_mismatch"
    else:
        raise AssertionError("tampered parent evidence was accepted")
    assert store.provider_launch_descriptor(refused_descriptor["descriptor_id"]) is None
    assert len(list(unified.rglob("*.jsonl"))) == 2
    store.close()


def test_provider_mapping_and_role_placement(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-placement")
    worktree = root / "pi-placement" / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    for provider, cli_provider in (("cursor", "cursor"), ("codex", "openai-codex")):
        descriptor = _descriptor(root / "pi-placement", worktree, provider, "create")
        descriptor["descriptor_digest"] = "a" * 64
        descriptor["pane_id"] = "w1:p84"
        arguments = pi_start_arguments(descriptor)
        assert arguments[arguments.index("--provider") + 1] == cli_provider

    project_folder = root / "pi-placement" / "project-folder"
    project_folder.mkdir()
    descriptor = _descriptor(root / "pi-placement", project_folder, "codex", "create")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:shotcaller:placement",
            "role": "shotcaller",
            "placement": "sibling_pane",
            "creator_pane_id": "w1:p1",
            "requested_session_id": deterministic_pi_session_id(
                "pi-launch:shotcaller:placement"
            ),
        }
    )
    fake = FakePiHerdr(root / "pi-placement")
    adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:02:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    receipt = adapter.launch(_spec(project_folder))
    assert receipt["display_agent"] == "codex"
    assert any(call[1:3] == ("pane", "split") for call in fake.calls)
    assert not any(call[1:3] == ("tab", "create") for call in fake.calls)
    agent = fake._agent()
    assert agent["terminal_title"] == "Lux"
    assert agent["tokens"]["task_label"] == "Session Restore"
    assert agent["tokens"]["sidebar_name"] == "Lux"
    store.close()


def test_native_start_failure_preserves_safe_code(root: Path) -> None:
    store, clock, worktree = _context(root, "native-start-failure")
    runner = FakePiHerdr(root / "native-start-failure")
    runner.start_failure = json.dumps({
        "id": "cli:agent:start", "error": {
            "code": "timeout", "message": "SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE",
            "data": {"private": "SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE"},
        },
    })
    adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
    spec = champion_spec(worktree, "native-start-failure")
    try:
        result = VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
        ).launch(spec)
        assert result["failure_class"] == "launch_native_timeout", result
        assert result["state"] == "blocked" and result["cleanup_proven"] is True
        assert "SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE" not in json.dumps(result)
        assert runner.start_count == 0 and not adapter.created_endpoint
    finally:
        store.close()


def test_native_start_failure_rejects_unsafe_diagnostics(root: Path) -> None:
    failures = (
        "SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE",
        '{"error":{"code":"SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE"}}',
        '{"error":{"code":"timeout"},"result":{}}',
        '{"error":{"code":"other","code":"timeout"}}',
        '{"error":{"code":"timeout","data":NaN}}',
        '{"error":{"code":"timeout","message":"' + "x" * 16_384 + '"}}',
        '{"error":{"code":["timeout"]}}',
    )
    for index, failure in enumerate(failures):
        suffix = f"native-unsafe-{index}"
        store, clock, worktree = _context(root, suffix)
        runner = FakePiHerdr(root / suffix)
        runner.start_failure = failure
        adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
        try:
            result = VisibleChampionLaunchService(
                store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
            ).launch(champion_spec(worktree, suffix))
            assert result["failure_class"] == "launch_adapter_failed", result
            assert result["state"] == "blocked" and result["cleanup_proven"] is True
            assert "SENSITIVE_DIAGNOSTIC_MUST_NOT_ESCAPE" not in json.dumps(result)
            assert runner.start_count == 0 and not adapter.created_endpoint
        finally:
            store.close()


def test_initial_placement_environment_remains_identity_guarded(root: Path) -> None:
    store, clock, worktree = _context(root, "initial-env-mismatch")
    runner = FakePiHerdr(root / "initial-env-mismatch")
    runner.start_environment_override = {"LEAGUE_CALLSIGN": "Foreign"}
    adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
    try:
        result = VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
        ).launch(champion_spec(worktree, "initial-env-mismatch"))
        assert result["state"] == "blocked" and result["cleanup_proven"] is True, result
        assert result["failure_class"] == "launch_identity_unverified"
        assert not any(call[1:3] == ("agent", "prompt") for call in runner.calls)
        descriptor = adapter.descriptor
        try:
            pi_start_arguments(descriptor, placement_environment=())
        except StorageRefusal as exc:
            assert exc.code == "launch_scope_invalid"
        else:
            raise AssertionError("unproven placement environment was accepted")
        try:
            pi_start_arguments(descriptor, restart=True, placement_environment=pi_launch_environment(
                descriptor, descriptor["descriptor_digest"]
            ))
        except StorageRefusal as exc:
            assert exc.code == "launch_scope_invalid"
        else:
            raise AssertionError("restart accepted initial-only environment transport")
    finally:
        store.close()


def factory_adapter(store, clock, worktree, root, runner, *, project_code="LEAGUE"):
    options = replace(_options(root), project_code=project_code)
    routing = {
        "decision_id": None, "provider": "codex", "model": options.model,
        "effort": options.effort, "tier": "EXPLICIT",
        "reason": "Explicit launch override.", "reason_code": "explicit_override",
        "policy_version": None, "provider_config_version": None,
        "explicit": {"runtime": True, "provider": True, "model": True, "effort": True},
    }
    multiplexer = builtin_multiplexer_adapter_registry(herdr_runner=runner).adapter("herdr")
    adapter = builtin_agent_adapter_registry().adapter("pi").visible_launch(
        store=store, options=options, multiplexer=multiplexer, startup_timeout_ms=1000,
        launch={
            "assignment_id": "assignment:factory", "project_code": project_code,
            "worktree": str(worktree), "provider_kind": "codex",
            "model": options.model, "effort": options.effort, "routing": routing,
            "resolved_release_root": str(ROOT), "workspace_id": "w1",
            "state_root": str(root / "state"), "session_mode": "create", "at": clock.now(),
        },
    )
    return adapter, options, routing


def test_registered_factory_preserves_explicit_routing(root: Path) -> None:
    store, clock, worktree = _context(root, "factory")
    runner = FakePiHerdr(root / "factory")
    adapter, options, routing = factory_adapter(store, clock, worktree, root, runner)
    spec = champion_spec(worktree, "factory")
    try:
        result = VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
        ).launch(spec)
        assert result["state"] == "active", (result, store.assignment_launch_context(spec.assignment_id))
        durable = store.assignment_launch_context(spec.assignment_id)
        assert durable["acceptance_receipt"]["routing"] == routing
        descriptor = store.provider_launch_descriptor("pi-launch:assignment:factory")["descriptor"]
        assert descriptor["routing"] == routing
        assert descriptor["project_code"] == "LEAGUE"
        assert descriptor["task_label"] == "Tiny Gate"
        assert runner.start_count == 1
        placement = next(call for call in runner.calls if call[1:3] == ("tab", "create"))
        expected_env = pi_launch_environment(descriptor, adapter.descriptor["descriptor_digest"])
        actual_env = tuple(item for index, item in enumerate(placement)
                           if item == "--env" or index > 0 and placement[index - 1] == "--env")
        assert actual_env == expected_env
        # Placement already supplies these bytes; only pane ID remains explicit.
        league_flags = [item for item in runner.pi_arguments if item.startswith("--league-")]
        assert league_flags == ["--league-pane-id"], league_flags
        full_argv = pi_start_arguments(adapter.descriptor)
        assert len(shlex.join(["pi", *runner.pi_arguments]).encode()) < len(shlex.join(["pi", *full_argv]).encode())
        assert runner.pi_arguments[runner.pi_arguments.index("--session-id") + 1] == descriptor["requested_session_id"]
    finally:
        store.close()


def test_factory_preallocation_refusal_releases_only_own_reservation(root: Path) -> None:
    for index, corrupt in enumerate((
        lambda value: value.update(unexpected=True),
        lambda value: value["routing"].update(provider="cursor"),
        lambda value: value["routing"].update(model="foreign-model"),
        lambda value: value["routing"]["explicit"].update(model="true"),
        lambda value: value["routing"].update(unexpected=True),
        lambda value: value["routing"].update(reason={"foreign": True}),
    )):
        suffix = f"factory-invalid-{index}"
        store, clock, worktree = _context(root, suffix)
        runner = FakePiHerdr(root / suffix)
        adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
        corrupt(adapter.descriptor)
        spec = champion_spec(worktree, suffix)
        before = [tuple(row) for row in store.connection.execute("SELECT * FROM callsign_assignments")]
        owner_before = store.agent_status(SHOTCALLER_ID)
        try:
            result = VisibleChampionLaunchService(
                store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
            ).launch(spec)
            assert result["failure_class"] == "provider_launch_descriptor_invalid", result
            assert result["state"] == "blocked" and result["cleanup_required"] is False, result
            reservation = store.callsign_assignment_status(f"callsign-assignment:{spec.assignment_id}")
            assert reservation["state"] == "rolled_back", reservation
            after = [tuple(row) for row in store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id != ?",
                (f"callsign-assignment:{spec.assignment_id}",),
            )]
            assert after == before
            assert store.agent_status(SHOTCALLER_ID) == owner_before
            assert runner.start_count == 0 and not adapter.created_endpoint
            assert all(call[1:3] == ("agent", "list") for call in runner.calls)
            assert store.provider_launch_descriptor("pi-launch:assignment:factory") is None
        finally:
            store.close()


def test_prior_descriptor_keeps_ambiguous_cleanup_fence(root: Path) -> None:
    from league.worktree import exact_launch_cwd_binding

    store, clock, worktree = _context(root, "factory-prior")
    runner = FakePiHerdr(root / "factory-prior")
    adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
    spec = champion_spec(worktree, "factory-prior")
    adapter.descriptor.update(
        assignment_id=spec.assignment_id,
        worktree_binding=exact_launch_cwd_binding(worktree, "champion"),
    )
    store.prepare_provider_launch(adapter.descriptor, clock.now())
    adapter.descriptor["unexpected"] = True
    try:
        result = VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
        ).launch(spec)
        assert result["failure_class"] == "provider_launch_descriptor_invalid", result
        assert result["state"] == "cleanup_pending" and result["cleanup_required"] is True
        assert store.callsign_assignment_status(
            f"callsign-assignment:{spec.assignment_id}"
        )["state"] == "reserved"
        assert runner.start_count == 0
    finally:
        store.close()


def test_factory_invalid_project_refuses_before_reservation(root: Path) -> None:
    for index, code in enumerate((None, "", "league", "LEAGUE EXTRA", "A" * 17, "LEAGUE\n", 123)):
        suffix = f"factory-project-{index}"
        store, clock, worktree = _context(root, suffix)
        runner = FakePiHerdr(root / suffix)
        before = store.connection.total_changes
        try:
            try:
                factory_adapter(store, clock, worktree, root, runner, project_code=code)
            except StorageRefusal as exc:
                assert exc.code == "launch_scope_invalid", exc.code
            else:
                raise AssertionError("invalid project code reached launch reservation boundary")
            assert store.connection.total_changes == before
            assert runner.calls == []
        finally:
            store.close()


def test_factory_persisted_routing_ownership(root: Path) -> None:
    from types import SimpleNamespace
    from league.cli import _champion_launch_route
    from league.routing import ModelRouter, load_routing_config

    for scenario in ("foreign", "valid", "invalid-json", "null", "mixed", "object"):
        foreign = scenario == "foreign"
        malformed = {
            "invalid-json": "[", "null": "null", "mixed": '[1,"write"]', "object": "{}",
        }.get(scenario)
        suffix = f"factory-decision-{scenario}"
        store, clock, worktree = _context(root, suffix)
        runner = FakePiHerdr(root / suffix)
        adapter, options, _ = factory_adapter(store, clock, worktree, root, runner)
        subject = "R2" if foreign else "R3"
        decision = ModelRouter(
            load_routing_config(ROOT / "config/league-model-routing.example.json"), store,
        ).choose(
            decision_id=f"route:{suffix}", subject_kind="request", subject_id=subject,
            role="champion", chosen_at=clock.now(), signals={"bounded_checkable": True},
        )
        spec = champion_spec(worktree, suffix)
        routing = _champion_launch_route(store, SimpleNamespace(
            runtime_kind="pi", provider_kind="codex", model=None, effort=None,
            routing_decision_id=decision["decision_id"], request_id=subject,
            task_id=spec.task_id, requires=[],
        ), assignment_id=spec.assignment_id)
        if malformed is not None:
            # The canonical TEXT column and storage API accept malformed JSON.
            # Persist it through the real API, not a mocked validator/read result.
            bad_id = f"route:{suffix}:malformed"
            store.record_routing_decision({
                **store.routing_decision(decision["decision_id"]),
                "decision_id": bad_id, "required_capabilities_json": malformed,
            })
            routing["decision_id"] = bad_id
        adapter.descriptor.update(routing=routing, model=routing["model"], effort=routing["effort"])
        try:
            result = VisibleChampionLaunchService(
                store, adapter, options, clock, issue_verifier=FakeIssueVerifier(store=store),
            ).launch(spec)
            if foreign or malformed is not None:
                assert result["state"] == "blocked", result
                assert result["failure_class"] == "provider_launch_routing_mismatch", result
                assert result["cleanup_required"] is False
                assert runner.start_count == 0 and not adapter.created_endpoint
                assert all(call[1:3] == ("agent", "list") for call in runner.calls)
                assert store.provider_launch_descriptor("pi-launch:assignment:factory") is None
                assert store.callsign_assignment_status(
                    f"callsign-assignment:{spec.assignment_id}"
                )["state"] == "rolled_back"
            else:
                assert result["state"] == "active", result
                assert store.assignment_launch_context(spec.assignment_id)["acceptance_receipt"]["routing"] == routing
                assert runner.start_count == 1
        finally:
            store.close()


def test_pi_context_display_receipt_matches_shared_contract(root: Path) -> None:
    root = root.resolve()
    for provider in ("cursor", "codex"):
        suffix = f"pi-context-{provider}"
        store, clock, worktree = _context(root, suffix)
        descriptor = _descriptor(root / suffix, worktree, provider, "create")
        descriptor["task_label"] = "Tiny Gate"
        runner = FakePiHerdr(root / suffix)
        runner.context_title = "Prompt generated title"
        adapter = HerdrPiLaunchAdapter(
            store, descriptor, at=clock.now(), runner=runner,
            environment={"HERDR_ENV": "1"},
        )
        service = VisibleChampionLaunchService(
            store, adapter, replace(_options(root), project_code="LEAGUE"),
            clock, issue_verifier=FakeIssueVerifier(store=store),
        )
        spec = champion_spec(worktree, suffix)
        try:
            first = service.launch(spec)
            assert first["state"] == "active", first
            expected = {
                "source": runner.env["LEAGUE_LAUNCH_METADATA_SOURCE"],
                "applies_to_source": "herdr:pi",
                "state_change_seq": runner.state_change_seq,
                "sidebar_name": "Lux",
                "thread_title": "Lux · LEAGUE|Tiny Gate",
                "terminal_title": "Lux · LEAGUE|Tiny Gate",
                "task_label": "Tiny Gate",
                "project_code": "LEAGUE",
                "orchestrator_role": "champion",
            }
            assert first["context_delivery"]["display_receipt"] == expected
            durable = store.assignment_launch_context(spec.assignment_id)
            assert durable["context_delivery"]["display_receipt"] == expected
            prompts = [call for call in runner.calls if call[1:3] == ("agent", "prompt")]
            assert len(prompts) == 1 and "--wait" in prompts[0]
            # Retry from durable receipts, with no in-memory launch adapter.
            service = VisibleChampionLaunchService(
                store,
                HerdrPiLaunchAdapter(store, descriptor, at=clock.now(), runner=runner,
                                     environment={"HERDR_ENV": "1"}),
                replace(_options(root), project_code="LEAGUE"), clock,
                issue_verifier=FakeIssueVerifier(store=store),
            )
            before = len(runner.calls)
            retry = service.launch(spec)
            assert retry["idempotent"] is True
            assert retry["context_delivery"]["display_receipt"] == expected
            assert not any(
                call[1:3] in {("agent", "prompt"), ("agent", "start"),
                             ("pane", "report-metadata"), ("tab", "create")}
                for call in runner.calls[before:]
            )
            runner.presentation_tokens["status_icon"] = "idle"
            runner.state_change_seq += 1
            before = len(runner.calls)
            assert service.launch(spec)["state"] == "active"
            assert not any(call[1:3] == ("pane", "report-metadata")
                           for call in runner.calls[before:])

            for malformed in ([], {}, "shotcaller", None):
                runner.presentation_tokens["orchestrator_role"] = malformed
                if malformed is None:
                    runner.presentation_tokens.pop("orchestrator_role")
                runner.state_change_seq += 1
                before = len(runner.calls)
                try:
                    adapter.verify_active_title(durable["acceptance_receipt"])
                except StorageRefusal as exc:
                    assert exc.code == "launch_title_restore_refused"
                else:
                    raise AssertionError("malformed modern Pi role was accepted")
                assert not any(call[1:3] == ("pane", "report-metadata")
                               for call in runner.calls[before:])
            runner.presentation_tokens["orchestrator_role"] = "champion"

            for changes in (
                {"runtime_instance_id": "runtime:foreign"},
                {"thread_id": "/synthetic/foreign.jsonl"},
                {"assignment_id": "assignment:foreign"},
            ):
                before = len(runner.calls)
                try:
                    adapter.verify_active_title({**durable["acceptance_receipt"], **changes})
                except StorageRefusal as exc:
                    assert exc.code == "launch_title_restore_refused"
                else:
                    raise AssertionError("foreign Pi display identity was accepted")
                assert not any(call[1:3] == ("pane", "report-metadata")
                               for call in runner.calls[before:])

            # A token-only user write must not be mistaken for a provider title.
            runner.presentation_tokens["sidebar_name"] = "User sidebar"
            runner.state_change_seq += 1
            before = len(runner.calls)
            try:
                adapter.verify_active_title(durable["acceptance_receipt"])
            except StorageRefusal as exc:
                assert exc.code == "launch_title_restore_refused"
            else:
                raise AssertionError("user token-only write was overwritten")
            assert not any(call[1:3] == ("pane", "report-metadata")
                           for call in runner.calls[before:])

            # The provider has refreshed its title; a later user write lands
            # after League's one restoration, inside the final settling window.
            runner.presentation_title = "Provider refresh"
            runner.presentation_source = "herdr:pi"
            runner.presentation_tokens["sidebar_name"] = "Provider refresh"
            runner.state_change_seq += 1
            runner.after_report_title = "Newer user title"
            before = len(runner.calls)
            try:
                adapter.verify_active_title(durable["acceptance_receipt"])
            except StorageRefusal as exc:
                assert exc.code == "launch_title_restore_refused"
            else:
                raise AssertionError("transient early Pi restoration was accepted")
            assert runner.presentation_title == "Newer user title"
            assert runner.presentation_tokens["sidebar_name"] == "User sidebar"
            assert sum(call[1:3] == ("pane", "report-metadata")
                       for call in runner.calls[before:]) == 1
            runner.after_report_title = None

            runner.presentation_title = "User selected title"
            runner.presentation_source = "user-selected"
            runner.state_change_seq += 1
            before = len(runner.calls)
            refused = service.launch(spec)
            assert refused["state"] == "cleanup_pending"
            assert runner.presentation_title == "User selected title"
            assert runner.presentation_source == "user-selected"
            assert runner.running and runner.start_count == 1
            assert not any(call[1:3] == ("pane", "report-metadata")
                           for call in runner.calls[before:])
        finally:
            store.close()


def test_pi_metadata_source_reuses_owned_legacy_source(root: Path) -> None:
    worktree = root / "pi-metadata-source" / "worktree"
    worktree.mkdir(parents=True)
    descriptor = _descriptor(root / "pi-metadata-source", worktree, "cursor", "resume")
    descriptor.update(
        {
            "descriptor_digest": "b" * 64,
            "routing_name": "lux",
        }
    )
    legacy = {
        "launch_runtime_kind": "pi",
        "launch_routing_alias": "lux",
        "launch_descriptor_sha256": "a" * 64,
    }
    assert pi_metadata_source(descriptor, legacy) == f"league:pi-launch:{'a' * 16}"
    explicit = {**legacy, "launch_metadata_source": "league:pi-launch:stable"}
    assert pi_metadata_source(descriptor, explicit) == "league:pi-launch:stable"
    assert pi_metadata_source(
        {**descriptor, "metadata_source": "league:pi-launch:carried"}
    ) == "league:pi-launch:carried"
    foreign = {**legacy, "launch_routing_alias": "other"}
    assert pi_metadata_source(descriptor, foreign) == f"league:pi-launch:{'b' * 16}"


def test_cli_exposes_explicit_pi_inputs() -> None:
    completed = subprocess.run(
        (str(ROOT / "bin" / "league"), "assign", "run", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    for option in (
        "--runtime-kind",
        "--provider-kind",
        "--project-code",
        "--session-mode",
        "--session-id",
        "--session-path",
        "--parent-session-id",
        "--parent-session-path",
    ):
        assert option in completed.stdout
    restart = subprocess.run(
        (str(ROOT / "bin" / "league"), "runtime", "resume-launch", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert restart.returncode == 0 and "--restart-id" in restart.stdout


def test_pi_extension_enforces_herdr_token_limit() -> None:
    source = (ROOT / "integrations/pi/league-runtime.ts").read_text(encoding="utf-8")
    assert "const MAX_TOKENS_PER_REPORT = 16;" in source
    assert "tokens.length > MAX_TOKENS_PER_REPORT" in source
    for redundant in (
        "`sidebar_name=${callsign}`",
        "`project_code=${projectCode}`",
        "`task_label=${taskLabel}`",
        "`routing_alias=${routingAlias}`",
        "`thread_title=${threadTitle}`",
        "`parent_session_path=${session.parentFile}`",
    ):
        assert redundant not in source


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-pi-provider-") as directory:
        root = Path(directory).resolve()
        test_native_start_failure_preserves_safe_code(root)
        test_native_start_failure_rejects_unsafe_diagnostics(root)
        test_initial_placement_environment_remains_identity_guarded(root)
        test_registered_factory_preserves_explicit_routing(root)
        test_factory_preallocation_refusal_releases_only_own_reservation(root)
        test_prior_descriptor_keeps_ambiguous_cleanup_fence(root)
        test_factory_invalid_project_refuses_before_reservation(root)
        test_factory_persisted_routing_ownership(root)
        test_pi_context_display_receipt_matches_shared_contract(root)
        test_fork_metadata_restart_and_duplicate_suppression(root)
        test_provider_mapping_and_role_placement(root)
        test_pi_metadata_source_reuses_owned_legacy_source(root)
        test_unified_inventory_migration_preserves_bytes_and_lineage(root)
        test_already_unified_shotcaller_session_is_adopted_without_copy(root)
        test_already_unified_child_uses_bound_parent_evidence_without_legacy_profile(root)
        test_unified_inventory_duplicate_scan_refuses(root)
    test_cli_exposes_explicit_pi_inputs()
    test_pi_extension_enforces_herdr_token_limit()
    print("PASS: Pi provider launch, unified adoption, metadata, placement, and exact restart resume")


if __name__ == "__main__":
    if sys.argv[1:] == ["--issue85-context-regression"]:
        with tempfile.TemporaryDirectory(prefix="league-pi-title-regression-") as directory:
            test_pi_context_display_receipt_matches_shared_contract(Path(directory))
    else:
        main()
