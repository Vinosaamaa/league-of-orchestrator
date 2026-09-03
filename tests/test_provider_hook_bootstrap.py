#!/usr/bin/env python3
"""Provider hook bootstrap declarations, installation, and Pi activation."""

from __future__ import annotations

from dataclasses import replace
import json
import shlex
import subprocess
import sys
import tempfile
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
from league.storage import StorageRefusal  # noqa: E402


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


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
    assert profiles["pi"]["target_relative"] == ".pi/agent/extensions/league-hooks.mjs"
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


def test_installs_are_idempotent_and_preserve_unrelated_handlers(root: Path) -> None:
    registry = builtin_agent_adapter_registry()
    watcher = root / "bin dir/agent-watcher;$"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic\n", encoding="utf-8")
    targets = {
        "codex": root / ".codex/hooks.json",
        "cursor": root / ".cursor/hooks.json",
        "pi": root / ".pi/agent/extensions/league-hooks.mjs",
    }
    targets["codex"].parent.mkdir(parents=True)
    targets["codex"].write_text(
        '{"hooks":{"Stop":[{"hooks":[{"command":"keep-codex","type":"command"}]}]}}\n',
        encoding="utf-8",
    )
    targets["cursor"].parent.mkdir(parents=True)
    targets["cursor"].write_text(
        '{"version":1,"hooks":{"sessionStart":[{"command":"keep-cursor"}]}}\n',
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
    for kind, commands in (("codex", codex_commands), ("cursor", cursor_commands)):
        expected = {
            shlex.join((str(watcher), str(profile["command"])))
            for profile in registry.adapter(kind).hook_profile.values()
        }
        assert expected.issubset(commands)
    assert targets["pi"].read_bytes() == (ROOT / "integrations/pi/league-hooks.mjs").read_bytes()


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

    pi_target = root / ".pi/agent/extensions/league-hooks.mjs"
    pi_target.parent.mkdir(parents=True)
    pi_target.write_bytes(b"x" * (2 * 1024 * 1024))
    install_provider_hook_bootstrap(
        registry,
        "pi",
        source_root=ROOT,
        target=pi_target,
        stable_watcher=watcher,
    )
    assert pi_target.read_bytes() == (ROOT / "integrations/pi/league-hooks.mjs").read_bytes()

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
        "pi": root / ".pi/agent/extensions/league-hooks.mjs",
    }


def test_registry_upgrade_is_idempotent_and_rollback_capable(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    targets = _hook_targets(profile)
    targets["codex"].parent.mkdir(parents=True)
    legacy = b'{"hooks":{"Stop":[{"hooks":[{"command":"legacy-stop","type":"command"}]}]}}\n'
    targets["codex"].write_bytes(legacy)
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
    assert not targets["cursor"].exists() and not targets["pi"].exists()
    repeated = rollback_provider_hooks(
        source_root=ROOT,
        profile_root=profile,
        stable_watcher=watcher,
        manifest_path=manifest,
    )
    assert repeated["idempotent"] is True


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
    assert len(managed["messages"]) == 1

    activation_failure = run_pi_scenario("activation-write-failure")
    assert activation_failure["firstInput"] == {"action": "handled"}
    assert activation_failure["tool"] == {
        "block": True,
        "reason": "League prompt binding is unavailable",
        "terminate": True,
    }
    assert len(activation_failure["notifications"]) == 1
    assert len(activation_failure["messages"]) == 1


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


def test_installed_pi_profile_restart_preserves_activation_and_exactly_once(root: Path) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    watcher = root / "bin/agent-watcher"
    watcher.parent.mkdir(parents=True)
    watcher.write_text("synthetic watcher\n", encoding="utf-8")
    target = _hook_targets(profile)["pi"]
    install_provider_hook_bootstrap(
        builtin_agent_adapter_registry(),
        "pi",
        source_root=ROOT,
        target=target,
        stable_watcher=watcher,
    )
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "tests/fixtures/pi_installed_profile_e2e.mjs"),
            str(target),
            str(profile),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["launchInput"] == {"action": "continue"}
    assert receipt["launchTool"] is None
    assert receipt["restartInput"] == {"action": "handled"}
    assert len(receipt["restartNotifications"]) == 1
    assert receipt["resumeInput"] == {"action": "continue"}
    assert receipt["resumeTool"] is None
    assert receipt["promptDeliveries"] == 1
    assert receipt["toolAuthorizations"] == 2
    assert receipt["stopCalls"] == 2
    assert len(receipt["stopContinuations"]) == 1
    activation = list((profile / "league-bindings").glob("*.json"))
    assert len(activation) == 1


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
        test_registry_upgrade_rolls_back_partial_failure(root / "upgrade-failure")
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
