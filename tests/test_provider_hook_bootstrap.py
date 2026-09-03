#!/usr/bin/env python3
"""Provider hook bootstrap declarations, installation, and Pi activation."""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from league.acceptance import _release_files  # noqa: E402
from league.agent_adapters import (  # noqa: E402
    AgentAdapterRegistry,
    builtin_agent_adapter_kinds,
    builtin_agent_adapter_registry,
)
from league.provider_hooks import (  # noqa: E402
    install_provider_hook_bootstrap,
    rollback_provider_hooks,
    upgrade_provider_hooks,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import AT2, CHAMPION_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def installed_pi_payload(watcher: Path) -> bytes:
    source = (ROOT / "integrations/pi/league-hooks.mjs").read_bytes()
    placeholder = b'"__LEAGUE_STABLE_WATCHER__"'
    assert source.count(placeholder) == 1
    return source.replace(placeholder, json.dumps(str(watcher)).encode("utf-8"))


def test_registry_declares_provider_hook_bootstrap_parity() -> None:
    registry = builtin_agent_adapter_registry()
    profiles = {
        adapter.contract.kind: adapter.hook_bootstrap_profile
        for adapter in registry.adapters()
    }
    assert set(profiles) == set(builtin_agent_adapter_kinds())
    for kind, profile in profiles.items():
        assert profile["schema"] == "league.provider-hook-bootstrap.v1"
        assert profile["profile_loaded"] is True
        assert callable(registry.adapter(kind).hook_bootstrap_installer)
        assert set(registry.adapter(kind).hook_profile) == {
            "prompt_intake",
            "pre_tool_authorization",
            "stop_supervision",
        }
    assert profiles["codex"]["target_relative"] == ".codex/hooks.json"
    assert profiles["cursor"]["target_relative"] == ".cursor/hooks.json"
    assert profiles["codex"]["activation"] == "native_hook_payload"
    assert profiles["cursor"]["activation"] == "native_hook_payload"
    assert profiles["pi"]["target_relative"] == ".pi/agent/extensions/league-hooks.ts"
    assert profiles["pi"]["activation"] == "exact_canonical_binding"
    assert profiles["pi"]["launch_enforcement"] == "separate"
    for adapter in registry.adapters():
        assert callable(adapter.hook_input_translator)
        assert callable(adapter.hook_output_translator)


def test_native_pre_tool_translation_has_provider_refusal_schemas() -> None:
    registry = builtin_agent_adapter_registry()
    codex = registry.adapter("codex")
    codex_input = codex.translate_hook_input(
        "pre_tool_authorization",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-native-codex",
            "turn_id": "turn-native-codex",
            "tool_name": "Write",
            "tool_use_id": "tool-native-codex",
            "tool_input": {"path": "synthetic.txt"},
        },
    )
    assert "authorized" not in codex_input
    assert codex.translate_hook_output(
        "pre_tool_authorization",
        {"decision": "refuse", "reason_code": "shotcaller_delegation_unverified"},
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "shotcaller_delegation_unverified",
        }
    }

    cursor = registry.adapter("cursor")
    for tool_name in ("Write", "Delete", "Task", "mcp__synthetic__call"):
        translated = cursor.translate_hook_input(
            "pre_tool_authorization",
            {
                "hook_event_name": "preToolUse",
                "conversation_id": "session-native-cursor",
                "generation_id": "generation-native-cursor",
                "tool_name": tool_name,
                "tool_use_id": f"tool-native-{tool_name}",
                "tool_input": {},
                "cwd": "/synthetic/worktree",
            },
        )
        assert translated["tool_name"] == tool_name and "authorized" not in translated
    assert cursor.translate_hook_output(
        "pre_tool_authorization",
        {"decision": "refuse", "reason_code": "runtime_replacement_fenced"},
    ) == {
        "permission": "deny",
        "user_message": "runtime_replacement_fenced",
    }
    stop_after_five = cursor.translate_hook_input(
        "stop_supervision",
        {
            "hook_event_name": "stop",
            "conversation_id": "session-native-cursor",
            "generation_id": "generation-native-cursor",
            "status": "completed",
            "loop_count": 6,
        },
    )
    assert stop_after_five["loop_count"] == 6
    assert cursor.translate_hook_output(
        "stop_supervision", {"followup_message": "continue exact supervision"}
    ) == {"followup_message": "continue exact supervision"}


def test_installs_are_idempotent_and_preserve_unrelated_handlers(root: Path) -> None:
    registry = builtin_agent_adapter_registry()
    watcher = root / "bin dir/agent-watcher;$"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic\n", encoding="utf-8")
    targets = {
        "codex": root / ".codex/hooks.json",
        "cursor": root / ".cursor/hooks.json",
        "pi": root / ".pi/agent/extensions/league-hooks.ts",
    }
    targets["codex"].parent.mkdir(parents=True)
    targets["codex"].write_text(
        '{"hooks":{"Stop":[{"hooks":[{"command":"keep-codex","type":"command"}]}]}}\n',
        encoding="utf-8",
    )
    targets["cursor"].parent.mkdir(parents=True)
    cursor_stop = shlex.join((str(watcher), "cursor-stop-hook"))
    targets["cursor"].write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [{"command": "keep-cursor"}],
                    "stop": [{"command": cursor_stop, "loop_limit": 5}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for kind, target in targets.items():
        first = install_provider_hook_bootstrap(
            registry,
            kind,
            source_root=ROOT,
            target=target,
            stable_watcher=watcher,
        )
        first_bytes = target.read_bytes()
        second = install_provider_hook_bootstrap(
            registry,
            kind,
            source_root=ROOT,
            target=target,
            stable_watcher=watcher,
        )
        assert first["added"] and second["added"] == []
        assert target.read_bytes() == first_bytes
    codex = json.loads(targets["codex"].read_text(encoding="utf-8"))
    codex_commands = [
        item["command"]
        for groups in codex["hooks"].values()
        for group in groups
        for item in group["hooks"]
    ]
    assert codex_commands.count("keep-codex") == 1
    assert len([value for value in codex_commands if str(watcher) in value]) == 3
    cursor = json.loads(targets["cursor"].read_text(encoding="utf-8"))
    cursor_commands = [
        item["command"] for handlers in cursor["hooks"].values() for item in handlers
    ]
    assert cursor_commands.count("keep-cursor") == 1
    assert len([value for value in cursor_commands if str(watcher) in value]) == 3
    assert cursor["hooks"]["preToolUse"] == [
        {
            "command": shlex.join((str(watcher), "cursor-pre-tool-hook")),
            "failClosed": True,
        }
    ]
    assert cursor["hooks"]["stop"] == [
        {
            "command": shlex.join((str(watcher), "cursor-stop-hook")),
            "loop_limit": None,
        }
    ]
    for kind, commands in (("codex", codex_commands), ("cursor", cursor_commands)):
        expected = {
            shlex.join((str(watcher), str(profile["command"])))
            for profile in registry.adapter(kind).hook_profile.values()
        }
        assert expected.issubset(commands)
    assert targets["pi"].read_bytes() == installed_pi_payload(watcher)
    stale_watcher = root / "stale-release/bin/agent-watcher"
    probe = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            (
                f'import {{ installedPaths }} from {json.dumps(targets["pi"].as_uri())}; '
                "process.stdout.write(JSON.stringify(installedPaths()));"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LEAGUE_WATCHER_COMMAND": str(stale_watcher),
            "LEAGUE_STATE_ROOT": str(root / "state"),
        },
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout)["watcher"] == str(watcher)


def test_installers_refuse_malformed_groups_and_bound_existing_reads(root: Path) -> None:
    registry = builtin_agent_adapter_registry()
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic\n", encoding="utf-8")
    for malformed in (None, "not-a-list"):
        target = root / f"codex-{str(malformed)}.json"
        target.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": malformed}]}}) + "\n",
            encoding="utf-8",
        )
        before = target.read_bytes()
        refused(
            lambda target=target: install_provider_hook_bootstrap(
                registry,
                "codex",
                source_root=ROOT,
                target=target,
                stable_watcher=watcher,
            ),
            "hook_bootstrap_invalid",
        )
        assert target.read_bytes() == before

    pi_target = root / ".pi/agent/extensions/league-hooks.ts"
    pi_target.parent.mkdir(parents=True)
    pi_target.write_bytes(b"x" * (2 * 1024 * 1024))
    install_provider_hook_bootstrap(
        registry,
        "pi",
        source_root=ROOT,
        target=pi_target,
        stable_watcher=watcher,
    )
    assert pi_target.read_bytes() == installed_pi_payload(watcher)

    codex_target = root / ".codex/hooks.json"
    codex_target.parent.mkdir(parents=True)
    codex_target.write_text('{"hooks":{}}\n', encoding="utf-8")
    stale_temporary = codex_target.with_name(f".{codex_target.name}.league-hook.tmp")
    stale_temporary.write_text("stale\n", encoding="utf-8")
    receipt = install_provider_hook_bootstrap(
        registry,
        "codex",
        source_root=ROOT,
        target=codex_target,
        stable_watcher=watcher,
    )
    assert receipt["added"] and stale_temporary.read_text(encoding="utf-8") == "stale\n"


def test_unsupported_adapter_refuses_without_target_mutation(root: Path) -> None:
    target = root / "future/hooks"
    before = target.exists()
    refused(
        lambda: install_provider_hook_bootstrap(
            AgentAdapterRegistry(),
            "future",
            source_root=ROOT,
            target=target,
            stable_watcher=root / "bin/agent-watcher",
        ),
        "adapter_unknown",
    )
    assert target.exists() is before
    invalid = AgentAdapterRegistry()
    refused(
        lambda: invalid.register(
            replace(
                builtin_agent_adapter_registry().adapter("pi"),
                hook_bootstrap_installer=None,
            )
        ),
        "adapter_contract_invalid",
    )


def _hook_targets(root: Path) -> dict[str, Path]:
    return {
        "codex": root / ".codex/hooks.json",
        "cursor": root / ".cursor/hooks.json",
        "pi": root / ".pi/agent/extensions/league-hooks.ts",
    }


def test_registry_upgrade_is_idempotent_and_rollback_capable(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    targets = _hook_targets(profile)
    targets["codex"].parent.mkdir(parents=True)
    (targets["codex"].parent / "foreign.txt").write_text("keep\n", encoding="utf-8")
    legacy = b'{"hooks":{"Stop":[{"hooks":[{"command":"legacy-stop","type":"command"}]}]}}\n'
    targets["codex"].write_bytes(legacy)
    targets["codex"].chmod(0o640)
    manifest = root / "provider-hook-upgrade.json"

    first = upgrade_provider_hooks(
        builtin_agent_adapter_registry(),
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert first["state"] == "active" and first["adapter_count"] == 3
    installed = {kind: target.read_bytes() for kind, target in targets.items()}
    second = upgrade_provider_hooks(
        builtin_agent_adapter_registry(),
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert second["state"] == "active" and second["idempotent"] is True
    assert {kind: target.read_bytes() for kind, target in targets.items()} == installed

    rolled_back = rollback_provider_hooks(
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert rolled_back["state"] == "rolled_back"
    assert targets["codex"].read_bytes() == legacy
    assert targets["codex"].stat().st_mode & 0o777 == 0o640
    assert (targets["codex"].parent / "foreign.txt").read_text(encoding="utf-8") == "keep\n"
    assert not targets["cursor"].exists() and not targets["cursor"].parent.exists()
    assert not targets["pi"].exists() and not (profile / ".pi").exists()
    repeated = rollback_provider_hooks(
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert repeated["idempotent"] is True


def test_upgrade_refuses_symlinked_profile_parents_without_escape(root: Path) -> None:
    profile = root / "profile"
    outside = root / "outside"
    profile.mkdir(parents=True)
    outside.mkdir(parents=True)
    (profile / ".cursor").symlink_to(outside, target_is_directory=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    manifest = root / "provider-hook-upgrade.json"

    refused(
        lambda: upgrade_provider_hooks(
            builtin_agent_adapter_registry(),
            source_root=ROOT,
            profile_root=profile,
            stable_watcher=watcher,
            manifest_path=manifest,
        ),
        "hook_upgrade_invalid",
    )
    assert list(outside.iterdir()) == []
    assert not manifest.exists()
    assert not (profile / ".league-provider-hook-backups").exists()

    (profile / ".cursor").unlink()
    backup_parent = profile / ".league-provider-hook-backups"
    backup_parent.symlink_to(outside, target_is_directory=True)
    refused(
        lambda: upgrade_provider_hooks(
            builtin_agent_adapter_registry(),
            source_root=ROOT,
            profile_root=profile,
            stable_watcher=watcher,
            manifest_path=manifest,
        ),
        "hook_upgrade_invalid",
    )
    assert list(outside.iterdir()) == [] and not manifest.exists()


def test_registry_upgrade_rolls_back_partial_failure(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    targets = _hook_targets(profile)
    targets["cursor"].parent.mkdir(parents=True)
    legacy = b'{"version":1,"hooks":{"sessionStart":[{"command":"keep"}]}}\n'
    targets["cursor"].write_bytes(legacy)
    manifest = root / "provider-hook-upgrade.json"

    def fail(label: str) -> None:
        if label == "provider_hook_upgraded:cursor":
            raise RuntimeError("synthetic provider hook upgrade crash")

    try:
        upgrade_provider_hooks(
            builtin_agent_adapter_registry(),
            source_root=ROOT,
            profile_root=profile,
            stable_watcher=watcher,
            manifest_path=manifest,
            fault=fail,
        )
    except RuntimeError as exc:
        assert "synthetic provider hook upgrade crash" in str(exc)
    else:
        raise AssertionError("fault-injected provider hook upgrade unexpectedly succeeded")
    assert not targets["codex"].exists() and not targets["pi"].exists()
    assert targets["cursor"].read_bytes() == legacy
    receipt = json.loads(manifest.read_text(encoding="utf-8"))
    assert receipt["state"] == "rolled_back"


def test_registry_upgrade_replaces_cursor_stop_exhaustion_and_restores(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    target = _hook_targets(profile)["cursor"]
    target.parent.mkdir(parents=True)
    command = shlex.join((str(watcher.resolve()), "cursor-stop-hook"))
    legacy = (
        json.dumps(
            {
                "version": 1,
                "hooks": {"stop": [{"command": command, "loop_limit": 5}]},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    target.write_bytes(legacy)
    target.chmod(0o644)
    manifest = root / "provider-hook-upgrade.json"

    upgrade_provider_hooks(
        builtin_agent_adapter_registry(),
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    installed = json.loads(target.read_text(encoding="utf-8"))
    assert installed["hooks"]["stop"] == [
        {"command": command, "loop_limit": None}
    ], installed
    rollback_provider_hooks(
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert target.read_bytes() == legacy
    assert target.stat().st_mode & 0o777 == 0o644
def run_pi_scenario(scenario: str, extension: Path | None = None) -> dict[str, object]:
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "tests/fixtures/pi_hook_bootstrap_runner.mjs"),
            str(extension or ROOT / "integrations/pi/league-hooks.mjs"),
            scenario,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_unbound_pi_is_inert_and_promotes_without_relaunch() -> None:
    unbound = run_pi_scenario("unbound")
    assert unbound["firstInput"] == {"action": "continue"}
    assert [item["command"] for item in unbound["calls"]] == [
        "pi-input-hook",
        "pi-input-hook",
        "pi-input-hook",
    ]
    assert unbound["notifications"] == [] and unbound["messages"] == []
    assert unbound["handlers"] == {"input": 1, "tool_call": 1, "agent_settled": 1}

    promoted = run_pi_scenario("promoted")
    assert promoted["firstInput"] == {"action": "continue"}
    assert promoted["secondInput"] is None
    assert [item["command"] for item in promoted["calls"]] == [
        "pi-input-hook",
        "pi-input-hook",
        "pi-pre-tool-hook",
        "pi-stop-hook",
        "pi-stop-hook",
    ]
    assert promoted["tool"] is None
    assert len(promoted["messages"]) == 1
    assert promoted["notifications"] == []


def test_league_launched_and_restored_pi_provider_parity() -> None:
    for provider in ("codex", "cursor"):
        for lifecycle in ("launched", "restored"):
            result = run_pi_scenario(f"{lifecycle}-{provider}")
            assert result["firstInput"] == {"action": "continue"}
            assert [item["command"] for item in result["calls"]] == [
                "pi-input-hook",
                "pi-pre-tool-hook",
                "pi-stop-hook",
            ]
            assert result["notifications"] == [] and result["messages"] == []


def test_pi_outage_is_inert_when_unbound_and_closed_when_managed() -> None:
    ordinary = run_pi_scenario("outage-ordinary")
    assert ordinary["firstInput"] == {"action": "continue"}
    assert ordinary["tool"] is None and ordinary["settled"] is None
    assert ordinary["notifications"] == [] and ordinary["messages"] == []

    managed = run_pi_scenario("outage-managed")
    assert managed["firstInput"] == {"action": "handled"}
    assert managed["tool"] == {
        "block": True,
        "reason": "League prompt binding is unavailable",
        "terminate": True,
    }
    assert len(managed["notifications"]) == 1
    assert managed["messages"] == []

    activation_failure = run_pi_scenario("activation-write-failure")
    assert activation_failure["firstInput"] == {"action": "handled"}
    assert activation_failure["tool"] is None
    assert len(activation_failure["notifications"]) == 1
    assert activation_failure["messages"] == []

    stop_outage = run_pi_scenario("outage-stop")
    assert stop_outage["firstInput"] == {"action": "continue"}
    assert stop_outage["tool"] is None
    assert stop_outage["messages"] == []
    assert stop_outage["notifications"] == [
        {
            "message": (
                "League Stop guard is unavailable; session is paused pending "
                "watcher recovery."
            ),
            "level": "error",
        }
    ]


def test_disposable_installed_profiles_accept_exact_native_payloads(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = ROOT / "bin/agent-watcher"
    for kind, target in _hook_targets(profile).items():
        install_provider_hook_bootstrap(
            builtin_agent_adapter_registry(),
            kind,
            source_root=ROOT,
            target=target,
            stable_watcher=watcher,
        )
    installed_pi = run_pi_scenario("launched-codex", _hook_targets(profile)["pi"])
    assert [item["command"] for item in installed_pi["calls"]] == [
        "pi-input-hook", "pi-pre-tool-hook", "pi-stop-hook"
    ]

    env = {
        **dict(__import__("os").environ),
        "LEAGUE_STATE_ROOT": str(root / "absent-state"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    native = {
        "codex": {
            "command": "codex-pre-tool-hook",
            "payload": {
                "hook_event_name": "PreToolUse", "session_id": "unbound-codex",
                "turn_id": "turn-unbound", "tool_name": "Write",
                "tool_use_id": "tool-unbound", "tool_input": {"path": "synthetic.txt"},
            },
            "expected": {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}},
        },
        "cursor": {
            "command": "cursor-pre-tool-hook",
            "payload": {
                "hook_event_name": "preToolUse", "conversation_id": "unbound-cursor",
                "generation_id": "generation-unbound", "tool_name": "Write",
                "tool_use_id": "tool-unbound", "tool_input": {"path": "synthetic.txt"},
                "cwd": str(root.resolve()),
            },
            "expected": {"permission": "allow"},
        },
    }
    for case in native.values():
        completed = subprocess.run(
            [str(watcher), str(case["command"])],
            input=json.dumps(case["payload"]),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == case["expected"]
    assert not (root / "absent-state").exists()


class _PiRPC:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self._records: queue.Queue[dict[str, object] | None] = queue.Queue()
        self.seen: list[dict[str, object]] = []

        def read_records() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self._records.put(json.loads(line))
            self._records.put(None)

        self._reader = threading.Thread(target=read_records, daemon=True)
        self._reader.start()

    def send(self, payload: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def receive_until(self, predicate, *, timeout: float = 20) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = self.close()
                raise AssertionError(
                    f"real Pi RPC response timed out; records={self.seen[-8:]!r}; stderr={error}"
                )
            try:
                record = self._records.get(timeout=remaining)
            except queue.Empty as exc:
                error = self.close()
                raise AssertionError(
                    f"real Pi RPC response timed out; records={self.seen[-8:]!r}; stderr={error}"
                ) from exc
            if record is None:
                error = "" if self.process.stderr is None else self.process.stderr.read()
                raise AssertionError(
                    f"real Pi RPC exited before acceptance completed: {error}"
                )
            self.seen.append(record)
            if predicate(record):
                return record

    def request(self, request_id: str, kind: str, **fields: object) -> dict[str, object]:
        self.send({"id": request_id, "type": kind, **fields})
        return self.receive_until(
            lambda item: item.get("type") == "response" and item.get("id") == request_id
        )

    def close(self) -> str:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self._reader.join(timeout=1)
        return "" if self.process.stderr is None else self.process.stderr.read()


def _pi_command(
    executable: str,
    *,
    extension: Path,
    sessions: Path,
    session_id: str | None = None,
    session_file: Path | None = None,
) -> list[str]:
    command = [
        executable,
        "--mode", "rpc",
        "--provider", "league-offline",
        "--model", "fixture-model",
        "--api-key", "fixture-key",
        "--thinking", "off",
        "--no-approve",
        "--offline",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--extension", str(extension),
    ]
    if session_file is not None:
        command.extend(("--session", str(session_file)))
    else:
        assert session_id is not None
        command.extend(("--session-dir", str(sessions), "--session-id", session_id))
    return command


def _pi_json_command(
    executable: str,
    *,
    extension: Path,
    sessions: Path,
    prompt: str,
    session_id: str | None = None,
    session_file: Path | None = None,
) -> list[str]:
    command = _pi_command(
        executable,
        extension=extension,
        sessions=sessions,
        session_id=session_id,
        session_file=session_file,
    )
    command[command.index("rpc")] = "json"
    command.append(prompt)
    return command


def test_installed_pi_profile_restart_preserves_activation_and_exactly_once(root: Path) -> None:
    executable = shutil.which("pi")
    assert executable is not None, "ordinary installed pi executable is unavailable"
    home = root / "home"
    profile = home / ".pi/agent"
    project = root / "project"
    sessions = (root / "sessions").resolve()
    profile.mkdir(parents=True)
    project.mkdir(parents=True)
    sessions.mkdir(parents=True)
    target = _hook_targets(home)["pi"]
    install_provider_hook_bootstrap(
        builtin_agent_adapter_registry(),
        "pi",
        source_root=ROOT,
        target=target,
        stable_watcher=ROOT / "bin/agent-watcher",
    )

    guarded_file = project / "guarded.txt"
    guarded_file_b = project / "guarded-b.txt"
    requests: list[dict[str, object]] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            index = len(requests)
            if index in {2, 5}:
                tool_target = guarded_file if index == 2 else guarded_file_b
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call-guarded-write-{index}",
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps(
                                    {"path": str(tool_target), "content": f"guarded-{index}\n"}
                                ),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta = {
                    "role": "assistant",
                    "content": (
                        "exact Stop continuation complete"
                        if index in {4, 7}
                        else (
                            "disposable session initialized"
                            if index == 1
                            else "guarded mutation complete"
                        )
                    ),
                }
                finish = "stop"
            chunks = [
                {
                    "id": f"chatcmpl-{index}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": f"chatcmpl-{index}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                },
            ]
            body = "".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                for chunk in chunks
            ) + "data: [DONE]\n\n"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    session_id = "99999999-9999-4999-8999-999999999999"
    _, state, _ = seeded_state(root, "canonical")
    writer_pointer = root / "league-writer-pointer.json"
    writer_pointer.write_text(
        json.dumps(
            {"writer": "sqlite", "generation": "pi-process-acceptance"},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(profile),
        "PI_OFFLINE": "1",
        "LEAGUE_STATE_ROOT": str(state),
        "LEAGUE_WRITER_POINTER": str(writer_pointer),
        "LEAGUE_WATCHER_COMMAND": str(ROOT / "bin/agent-watcher"),
        "LEAGUE_TEST_PROVIDER_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "LEAGUE_TEST_PROVIDER_KEY": "fixture-key",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    extension = ROOT / "tests/fixtures/pi_offline_provider.mjs"
    initialized = subprocess.run(
        _pi_json_command(
            executable,
            extension=extension,
            sessions=sessions,
            prompt="initialize the disposable unbound session",
            session_id=session_id,
        ),
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert len(requests) == 1
    session_files = list(sessions.rglob(f"*_{session_id}.jsonl"))
    assert len(session_files) == 1
    session_file = session_files[0].resolve()
    assert session_file.is_relative_to(sessions.resolve())
    assert not list((profile / "league-bindings").glob("*.json"))
    with SQLiteStorage(state) as observer:
        assert observer.connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert observer.connection.execute(
            "SELECT COUNT(*) FROM prompt_quarantine"
        ).fetchone()[0] == 0
    with SQLiteStorage(state) as store:
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id="runtime:pi:real-process",
                actor_agent_id=CHAMPION_ID,
                harness_kind="pi",
                backend_kind="herdr",
                session_ref=str(session_file),
                endpoint="synthetic:pi:real-process",
                runtime_generation="generation:pi:real-process",
                status="active",
                verified=True,
                at=AT2,
            )
        )
    completed = subprocess.run(
        _pi_json_command(
            executable,
            extension=extension,
            sessions=sessions,
            prompt="perform the guarded mutation",
            session_file=session_file,
        ),
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert guarded_file.read_text(encoding="utf-8") == "guarded-2\n"
    assert len(requests) == 4
    continuation_requests = [
        item for item in requests if "League requires" in json.dumps(item)
    ]
    assert len(continuation_requests) == 1
    activation = list((profile / "league-bindings").glob("*.json"))
    assert len(activation) == 1
    with SQLiteStorage(state) as store:
        captured = store.connection.execute(
            """
            SELECT p.intake_actor_id,p.runtime_instance_id,p.session_ref,pp.body
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE pp.body=?
            """,
            ("perform the guarded mutation",),
        ).fetchall()
        assert [tuple(row) for row in captured] == [
            (
                CHAMPION_ID,
                "runtime:pi:real-process",
                str(session_file),
                "perform the guarded mutation",
            )
        ]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prompt_quarantine"
        ).fetchone()[0] == 0

    second = subprocess.run(
        _pi_json_command(
            executable,
            extension=extension,
            sessions=sessions,
            prompt="perform the second guarded mutation",
            session_file=session_file,
        ),
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert guarded_file_b.read_text(encoding="utf-8") == "guarded-5\n"
    assert len(requests) == 7
    continuation_requests = [
        item for item in requests if "League requires" in json.dumps(item)
    ]
    assert len(continuation_requests) == 2
    with SQLiteStorage(state) as store:
        captured = store.connection.execute(
            """
            SELECT pp.body
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.runtime_instance_id=? ORDER BY pp.body
            """,
            ("runtime:pi:real-process",),
        ).fetchall()
        assert [str(row["body"]) for row in captured] == [
            "perform the guarded mutation",
            "perform the second guarded mutation",
        ]
    session_records = [
        json.loads(line)
        for line in session_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    tool_calls = [
        content
        for record in session_records
        for content in record.get("message", {}).get("content", [])
        if isinstance(content, dict) and content.get("type") == "toolCall"
    ]
    tool_results = [
        record
        for record in session_records
        if record.get("message", {}).get("role") == "toolResult"
    ]
    assert len(tool_calls) == 2 and len(tool_results) == 2
    assert {item["id"] for item in tool_calls} == {
        "call-guarded-write-2",
        "call-guarded-write-5",
    }

    before_resume_requests = len(requests)
    resumed = _PiRPC(
        _pi_command(
            executable,
            extension=extension,
            sessions=sessions,
            session_file=session_file,
        ),
        cwd=project,
        env=env,
    )
    try:
        resumed_state = resumed.request("state-resumed", "get_state")
        resumed_data = resumed_state["data"]
        assert isinstance(resumed_data, dict)
        assert resumed_data["sessionId"] == session_id
        assert Path(str(resumed_data["sessionFile"])) == session_file
    finally:
        resumed_error = resumed.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert resumed_error == "", resumed_error
    assert len(requests) == before_resume_requests


def test_release_manifest_and_launch_extension_separation() -> None:
    manifest = {path.relative_to(ROOT) for path in _release_files(ROOT)}
    assert Path("integrations/pi/league-hooks.mjs") in manifest
    runtime = (ROOT / "integrations/pi/league-runtime.ts").read_text(encoding="utf-8")
    bootstrap = (ROOT / "integrations/pi/league-hooks.mjs").read_text(encoding="utf-8")
    for command in ("pi-input-hook", "pi-pre-tool-hook", "pi-stop-hook"):
        assert command not in runtime
        assert command in bootstrap
    assert 'pi.on("tool_call"' in runtime
    assert "reportLeagueMetadata" in runtime


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-provider-hooks-") as temporary:
        root = Path(temporary)
        test_installs_are_idempotent_and_preserve_unrelated_handlers(root / "install")
        test_installers_refuse_malformed_groups_and_bound_existing_reads(root / "fail-closed")
        test_unsupported_adapter_refuses_without_target_mutation(root / "unsupported")
        test_registry_upgrade_is_idempotent_and_rollback_capable(root / "upgrade")
        test_upgrade_refuses_symlinked_profile_parents_without_escape(root / "symlink")
        test_registry_upgrade_rolls_back_partial_failure(root / "upgrade-failure")
        test_registry_upgrade_replaces_cursor_stop_exhaustion_and_restores(
            root / "cursor-stop-upgrade"
        )
        test_disposable_installed_profiles_accept_exact_native_payloads(root / "native")
        test_installed_pi_profile_restart_preserves_activation_and_exactly_once(
            root / "installed-pi"
        )
    test_registry_declares_provider_hook_bootstrap_parity()
    test_native_pre_tool_translation_has_provider_refusal_schemas()
    test_unbound_pi_is_inert_and_promotes_without_relaunch()
    test_league_launched_and_restored_pi_provider_parity()
    test_pi_outage_is_inert_when_unbound_and_closed_when_managed()
    test_release_manifest_and_launch_extension_separation()
    print("PASS: provider hook bootstrap declaration, install, activation, and parity")


if __name__ == "__main__":
    main()
