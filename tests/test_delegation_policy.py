#!/usr/bin/env python3
"""Issue #81 native provider parity without live providers or repositories."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.agent_adapters import LifecycleEvent, SharedLifecyclePolicy  # noqa: E402
from league.canonical_watcher import _pre_tool_output, handle_brokered_hook  # noqa: E402
from league.delegation_policy import repository_implementation  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402
from test_provider_hook_bootstrap import run_pi_scenario  # noqa: E402
from test_canonical_watcher import (  # noqa: E402
    _environment, _register_garen_runtime, _watcher,
    test_read_only_pre_tool_fast_path_needs_no_state_or_supervisor,
)


def payload(provider: str, cwd: Path, name: str, inputs: dict) -> dict:
    common = {"cwd": str(cwd), "tool_name": name, "tool_input": inputs}
    if provider == "codex":
        return {**common, "session_id": "33333333-3333-4333-8333-333333333333",
                "turn_id": "turn:delegation", "tool_use_id": "tool:delegation",
                "hook_event_name": "PreToolUse"}
    return {**common, "session_id": "session:delegation",
            "session_path": str(cwd / "session.jsonl"),
            "input_id": "input:delegation", "hook_event_name": "PiToolCall"}


def test_pr134_native_hook_refuses_before_file_or_publication_effect(root: Path) -> None:
    for provider in ("codex", "pi"):
        label = f"delegation-{provider}"
        _, state, _ = seeded_state(root, label)
        repo = root / label / "repository"
        repo.mkdir()
        (repo / ".git").mkdir()  # Synthetic boundary, not a repository/worktree.
        target = repo / "AGENTS.md"
        target.write_text("unchanged\n", encoding="utf-8")
        pretool = payload(provider, repo, "write", {"path": str(target), "content": "changed"})
        session = pretool["session_id" if provider == "codex" else "session_path"]
        _register_garen_runtime(state, label, session_ref=session, harness_kind=f"{provider}-thread")
        command = f"{provider}-pre-tool-hook"
        env = _environment(root / label, state)
        with patch("league.canonical_watcher.SharedLifecyclePolicy.decide", autospec=True,
                   side_effect=SharedLifecyclePolicy.decide) as shared:
            with SQLiteStorage(state) as store:
                decision = handle_brokered_hook(store, {"command": command, "payload": pretool})["hook_output"]
            assert shared.call_count == 1
        assert _watcher(env, command, payload=pretool) == decision
        if provider == "codex":
            denied = decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            denied = decision["decision"] == "refuse"
        assert "delegation_required" in str(decision), decision
        effects = []
        if not denied:
            target.write_text("changed", encoding="utf-8")
            effects.append("publish PR")
        assert effects == [] and target.read_text(encoding="utf-8") == "unchanged\n"


def test_shared_policy_is_effect_scoped_and_does_not_gate_recovery(root: Path) -> None:
    repo = root / "effects-repository"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = root / "recovery-state.json"
    writes = (
        ("apply_patch", {"input": "*** Begin Patch\n*** Update File: AGENTS.md\n*** End Patch"}),
        ("edit", {"path": "AGENTS.md"}),
        ("exec_command", {"cmd": "printf changed > AGENTS.md && git add AGENTS.md && gh pr create"}),
        ("bash", {"command": "sed -i '' 's/old/new/' AGENTS.md"}),
        ("bash", {"command": "git apply change.patch"}),
        ("bash", {"command": f"cd {repo} && printf changed > AGENTS.md"}),
    )
    reads_and_recovery = (
        ("Read", {"path": "AGENTS.md"}),
        ("bash", {"command": "git status --short && git diff -- AGENTS.md"}),
        ("bash", {"command": "grep needle AGENTS.md > /dev/null"}),
        ("bash", {"command": "printf '>' AGENTS.md"}),
        ("bash", {"command": f"cp AGENTS.md {outside}"}),
        ("write", {"path": str(outside), "content": "synthetic recovery"}),
        ("bash", {"command": "league rollover refresh-bindings --mode-action live_reconcile"}),
        ("bash", {"command": "league supervisor detach --expected-version 1"}),
        ("bash", {"command": "league assign run --task-id synthetic"}),
        ("bash", {"command": "git merge --ff-only approved-head"}),
    )
    for provider in ("codex", "pi"):
        for name, inputs in writes + reads_and_recovery:
            native = payload(provider, repo, name, inputs)
            expected = (name, inputs) in writes
            assert repository_implementation(native) == expected, native
            decision = _pre_tool_output(f"{provider}-pre-tool-hook", native, exact_binding=True,
                                       actor_role="shotcaller", delegated_by_shotcaller=False)
            assert decision["reason_code"] == ("delegation_required" if expected else "policy_accepted"), native
            champion = _pre_tool_output(f"{provider}-pre-tool-hook", native, exact_binding=True,
                                       actor_role="champion", delegated_by_shotcaller=True)
            assert champion["decision"] == "accept"
    for operation in ("prompt_intake", "stop_supervision", "steer", "retirement"):
        event = LifecycleEvent(operation, "pi", "synthetic", "session:test", "event:test", {})
        assert SharedLifecyclePolicy().decide(event, actor_role="shotcaller").outcome == "accept"
    assert repository_implementation({"tool_name": "write", "tool_input": {"path": "AGENTS.md"}})
    assert not repository_implementation({"tool_name": "write", "tool_input": {"path": str(outside)}})


def test_unresolved_shell_mutation_targets_refuse_without_expansion(root: Path) -> None:
    outside = root / "shell-outside"
    outside.mkdir()
    repo = root / "shell-repository"
    repo.mkdir()
    (repo / ".git").mkdir()
    commands = (
        'printf x > "$TARGET"',
        'printf x >& "$TARGET"',
        'printf x > "${TARGET}/file"',
        'printf x > /"$TARGET"',
        'printf x > "/""$TARGET"',
        "printf x > $(touch must-not-execute)",
        'printf x > "`touch must-not-execute`"',
        'touch "$TARGET"',
        'cp source "$TARGET"',
        'cp -t "$TARGET" source',
        'cp --target-directory="$TARGET" source',
        'mv "$TARGET" destination',
        'sed -i s/x/y/ "$TARGET"',
        'cd "$DIRECTORY" && touch relative.txt',
        'cd -- "$DIRECTORY" && touch relative.txt',
        'cd && touch relative.txt',
        'git -C "$DIRECTORY" apply change.patch',
        'git -C / -C "$DIRECTORY" apply change.patch',
        'MODE=synthetic touch "$TARGET"',
    )
    for provider in ("codex", "pi"):
        for command in commands:
            native = payload(provider, outside, "bash", {"command": command})
            # Classify source text only; even command substitution must never run.
            with patch("subprocess.run", side_effect=AssertionError("shell evaluation forbidden")):
                decision = _pre_tool_output(f"{provider}-pre-tool-hook", native, exact_binding=True,
                                           actor_role="shotcaller", delegated_by_shotcaller=False)
            assert decision == {"decision": "refuse", "reason_code": "delegation_required"}, command
    assert not (outside / "must-not-execute").exists()


def test_literal_off_repository_writes_and_diagnostics_stay_available(root: Path) -> None:
    outside = root / "literal-outside"
    outside.mkdir()
    commands = (
        "printf x > '$TARGET'",
        r'printf x > "\$TARGET"',
        r'printf x > \$TARGET',
        "printf x > 'prefix/'\"literal$\"",
        f'printf "$MESSAGE" > "{outside}/note.txt"',
        'printf "$MESSAGE" > /dev/null',
        'cp "$SOURCE" literal-destination',
        'cp -t literal-destination "$SOURCE"',
        'cat "$SOURCE"',
        'git -C "$DIRECTORY" status --short',
        f'cd "$DIRECTORY" && printf x > "{outside}/note.txt"',
        'git -C "$DIRECTORY" status; touch literal-destination',
        'printf okay # > "$TARGET"',
        'printf okay\nprintf x > literal-destination',
        "printf x > '~literal-name'",
    )
    for command in commands:
        assert not repository_implementation(payload("pi", outside, "bash", {"command": command})), command
    (outside / ".git").mkdir()
    for command in ('git status 2>&1', 'printf diagnostics >&2', 'printf diagnostics >&-'):
        assert not repository_implementation(payload("pi", outside, "bash", {"command": command})), command
    # Return to off-repository assertions with a different synthetic directory.
    outside = root / "absolute-outside"
    outside.mkdir()
    assert not repository_implementation({"tool_name": "bash", "tool_input": {"command": "cat $SOURCE"}})
    assert repository_implementation({"tool_name": "bash", "tool_input": {"command": "touch relative"}})
    assert not repository_implementation({"tool_name": "bash", "tool_input": {
        "command": f'printf x > "{outside}/absolute-without-cwd"',
    }})


def test_target_identity_is_rechecked_without_cross_request_caching(root: Path) -> None:
    outside = root / "fresh-outside"
    outside.mkdir()
    repo = root / "fresh-repository"
    repo.mkdir()
    link = root / "changing-target"
    link.symlink_to(outside, target_is_directory=True)
    native = payload("codex", outside, "bash", {"command": f'printf x > "{link}/file"'})
    assert not repository_implementation(native)
    (repo / ".git").mkdir()
    link.unlink()
    link.symlink_to(repo, target_is_directory=True)
    assert repository_implementation(native)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-delegation-policy-") as temporary:
        root = Path(temporary)
        test_pr134_native_hook_refuses_before_file_or_publication_effect(root)
        test_shared_policy_is_effect_scoped_and_does_not_gate_recovery(root)
        test_unresolved_shell_mutation_targets_refuse_without_expansion(root)
        test_literal_off_repository_writes_and_diagnostics_stay_available(root)
        test_target_identity_is_rechecked_without_cross_request_caching(root)
        test_read_only_pre_tool_fast_path_needs_no_state_or_supervisor(root)
    native = run_pi_scenario("delegation-required")
    assert native["tool"] == {"block": True, "reason": "delegation_required", "terminate": True}
    pretool = next(item for item in native["calls"] if item["command"] == "pi-pre-tool-hook")
    assert pretool["payload"]["cwd"] == "/synthetic/repository"
    assert any(item["command"] == "pi-stop-hook" for item in native["calls"])
    assert native["secondInput"] == {"action": "continue"} and native["rearmed"] is None
    assert native["notifications"] == []
    print("PASS: shared Codex/Pi delegation, zero-write PR134, diagnostics and recovery")


if __name__ == "__main__":
    main()
