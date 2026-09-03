#!/usr/bin/env python3
"""Focused ownership, staging, parity, and rollback tests for League guidance."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import league.guidance as guidance_module  # noqa: E402
from league.guidance import (  # noqa: E402
    LEAGUE_TARGET,
    MAX_GUIDANCE_BYTES,
    SUPPORTED_HARNESSES,
    rollback_guidance,
    stage_guidance,
    validate_guidance_manifest,
)
from league.storage import StorageRefusal  # noqa: E402


UNIVERSAL = b"synthetic toolkit-owned universal guide\n"
PRIOR_LEAGUE = b"prior synthetic League supplement\n"


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", path.readlink().as_posix())
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        else:
            result[relative] = ("directory", "")
    return result


def agent_root(root: Path, name: str, *, prior_league: bool = False) -> Path:
    destination = root / name
    destination.mkdir()
    (destination / "AGENTS.md").write_bytes(UNIVERSAL)
    if prior_league:
        (destination / "league").mkdir()
        (destination / LEAGUE_TARGET).write_bytes(PRIOR_LEAGUE)
    return destination


def test_backup_failure_restores_exact_state(root: Path, source: Path) -> None:
    destination = agent_root(root, "backup-failure", prior_league=True)
    before = snapshot(destination)
    original_fsync = guidance_module.os.fsync

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("synthetic backup fsync failure")

    guidance_module.os.fsync = fail_fsync
    try:
        try:
            stage_guidance(source.resolve(), "codex", destination.resolve())
        except OSError as exc:
            assert str(exc) == "synthetic backup fsync failure"
        else:
            raise AssertionError("backup fsync failure did not refuse staging")
    finally:
        guidance_module.os.fsync = original_fsync
    assert snapshot(destination) == before


def test_post_install_universal_failure_restores_supplement(
    root: Path, source: Path
) -> None:
    destination = agent_root(root, "universal-recheck", prior_league=True)
    before = snapshot(destination)
    original_hash = guidance_module._universal_hash
    calls = 0

    def fail_second_hash(agent_root_path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageRefusal(
                "universal_guidance_unproven",
                "synthetic post-install universal validation failure",
            )
        return original_hash(agent_root_path)

    guidance_module._universal_hash = fail_second_hash
    try:
        refused(
            lambda: stage_guidance(source.resolve(), "codex", destination.resolve()),
            "universal_guidance_unproven",
        )
    finally:
        guidance_module._universal_hash = original_hash
    assert snapshot(destination) == before


def test_rollback_size_bounds(root: Path, source: Path) -> None:
    destination = agent_root(root, "oversized-rollback-target", prior_league=True)
    receipt = stage_guidance(source.resolve(), "codex", destination.resolve())
    (destination / LEAGUE_TARGET).write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
    refused(
        lambda: rollback_guidance(destination.resolve(), "codex", receipt),
        "guidance_rollback_conflict",
    )
    assert (destination / LEAGUE_TARGET).stat().st_size == MAX_GUIDANCE_BYTES + 1
    assert (destination / "AGENTS.md").read_bytes() == UNIVERSAL

    backup_root = agent_root(root, "oversized-rollback-backup", prior_league=True)
    receipt = stage_guidance(source.resolve(), "codex", backup_root.resolve())
    prior_sha256 = receipt["prior_sha256"]
    backup = backup_root / "league" / f".AGENTS.md.league-backup-{prior_sha256[:12]}"
    backup.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
    supplement = (backup_root / LEAGUE_TARGET).read_bytes()
    refused(
        lambda: rollback_guidance(backup_root.resolve(), "codex", receipt),
        "guidance_backup_missing",
    )
    assert (backup_root / LEAGUE_TARGET).read_bytes() == supplement
    assert (backup_root / "AGENTS.md").read_bytes() == UNIVERSAL


def source_contract(source: Path) -> bytes:
    original = source.read_bytes()
    triage = " ".join(
        original.decode("utf-8")
        .split("## Durable prompt and request triage\n", 1)[1]
        .split("\n## ", 1)[0]
        .split()
    )
    for required_clause in (
        "Only `UserPromptSubmit` and `beforeSubmitPrompt` from an exactly bound "
        "canonical League runtime capture its exact local prompt bytes once and wake "
        "its verified Shotcaller.",
        "An unbound, non-League, or otherwise unverifiable runtime is left untouched "
        "and unrecorded.",
        "Prompt intake activates only after exact canonical binding; it never "
        "backfills pre-binding prompts or mines transcripts.",
    ):
        assert required_clause in triage, required_clause
    for contradictory_clause in (
        b"UserPromptSubmit and beforeSubmitPrompt capture exact local prompt bytes once",
        b"Missing runtime identity quarantines and deduplicates the exact prompt",
        b"It binds later only to one verified runtime",
        b"Every prompt item is classified",
    ):
        assert contradictory_clause not in original, contradictory_clause
    delivery = " ".join(
        original.decode("utf-8")
        .split("## Delivery and supervision\n", 1)[1]
        .split("\n## ", 1)[0]
        .split()
    )
    for required_clause in (
        "Hooks first verify an exact canonical runtime binding and role.",
        "If a Codex, Pi, or Cursor CLI runtime is unbound or non-League, "
        "`UserPrompt`, pre-mutation, and `Stop` allow/no-op immediately with zero "
        "canonical mutation.",
        "An attached Shotcaller with any owner or delegated obligation blocks every "
        "`Stop` attempt unless the Summoner explicitly requested a final stop and the "
        "Shotcaller armed the exact one-shot allowance after pausing work.",
        "When the Summoner requests all work paused, the Shotcaller reaches a safe "
        "boundary for its own work, sends a pause-and-preserve instruction to every "
        "owned active Champion, then runs `$HOME/.local/bin/agent-watcher "
        "--shotcaller <callsign> allow-stop --once` immediately before `Stop`.",
        "An attached Shotcaller waits for material League work with one "
        "`$HOME/.local/bin/agent-watcher --shotcaller <callsign> wait` invocation.",
        "`attach-shotcaller` requires the exact live supervisor binding and makes the "
        "Shotcaller terminal-attached.",
        "`detach-shotcaller` requests token-saving terminal detachment without "
        "pausing supervision.",
        "Detachment may let the Shotcaller end only when no owner-actionable work "
        "remains and the persistent watcher lease, runtime generation, locator, fence, and "
        "wake/delivery path exactly match its durable detachment receipt.",
        "The watcher remains live and later wakes and delivers exactly once.",
        "`service-pause` and `service-resume` are deprecated aliases for "
        "`detach-shotcaller` and `attach-shotcaller`, respectively.",
        "For a bound Shotcaller, a missing, stale, or ambiguous watcher, fence, "
        "binding, or wake path refuses detachment and keeps `Stop` blocked.",
        "Codex, Pi regardless of model provider, and Cursor CLI share this "
        "provider-neutral contract.",
    ):
        assert required_clause in delivery, required_clause
    assert b"Read the universal `~/.agents/AGENTS.md` first" in original
    assert b"request turn" in original
    assert b"exact repository issue" in original
    assert b"one or two words" in original
    assert b"autonomous_delivery" in original
    assert b"exact-thread reopen" in original
    assert b"terminal-environment-toolkit issue #45" in original
    for forbidden_overlap in (
        b"Repository writer ->",
        b"Fast lane ->",
        b"Public safety ->",
        b"Local install ->",
    ):
        assert forbidden_overlap not in original
    assert not (ROOT / "global-agent-instructions" / "shared-AGENTS.md").exists()
    assert validate_guidance_manifest((LEAGUE_TARGET,)) == (LEAGUE_TARGET,)
    return original


def test_successful_stage_and_rollback(
    root: Path, source: Path, original: bytes
) -> None:
    for harness in sorted(SUPPORTED_HARNESSES):
        destination = agent_root(root, harness)
        universal_before = hashlib.sha256(UNIVERSAL).hexdigest()
        receipt = stage_guidance(source.resolve(), harness, destination.resolve())
        assert (destination / LEAGUE_TARGET).read_bytes() == original
        assert (destination / "AGENTS.md").read_bytes() == UNIVERSAL
        assert receipt["target"] == LEAGUE_TARGET
        assert receipt["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert receipt["installed_sha256"] == receipt["source_sha256"]
        assert receipt["universal_before_sha256"] == universal_before
        assert receipt["universal_after_sha256"] == universal_before
        assert receipt["universal_unchanged"] is True
        rollback = rollback_guidance(destination.resolve(), harness, receipt)
        assert rollback["completed"] and rollback["universal_unchanged"]
        assert not (destination / LEAGUE_TARGET).exists()
        assert (destination / "AGENTS.md").read_bytes() == UNIVERSAL

    replacement = agent_root(root, "replacement", prior_league=True)
    receipt = stage_guidance(source.resolve(), "codex", replacement.resolve())
    assert receipt["rollback_available"] is True
    assert receipt["prior_sha256"] == hashlib.sha256(PRIOR_LEAGUE).hexdigest()
    rollback = rollback_guidance(replacement.resolve(), "codex", receipt)
    assert rollback["restored_sha256"] == receipt["prior_sha256"]
    assert (replacement / LEAGUE_TARGET).read_bytes() == PRIOR_LEAGUE
    assert (replacement / "AGENTS.md").read_bytes() == UNIVERSAL


def test_target_and_input_refusals(root: Path, source: Path) -> None:
    forbidden = agent_root(root, "forbidden", prior_league=True)
    pointer = forbidden / "current"
    pointer.symlink_to("releases/prior")
    before = snapshot(forbidden)
    for target in (
        "AGENTS.md",
        "~/.agents/AGENTS.md",
        "/synthetic/.agents/AGENTS.md",
    ):
        refused(
            lambda target=target: stage_guidance(
                source.resolve(), "codex", forbidden.resolve(), target=target
            ),
            "universal_guidance_forbidden",
        )
        assert snapshot(forbidden) == before
    refused(
        lambda: validate_guidance_manifest((LEAGUE_TARGET, "AGENTS.md")),
        "universal_guidance_forbidden",
    )
    assert snapshot(forbidden) == before

    missing_universal = root / "missing-universal"
    missing_universal.mkdir()
    refused(
        lambda: stage_guidance(source.resolve(), "codex", missing_universal.resolve()),
        "universal_guidance_unproven",
    )
    assert snapshot(missing_universal) == {}

    unsupported = agent_root(root, "unsupported")
    refused(
        lambda: stage_guidance(source.resolve(), "unsupported", unsupported.resolve()),
        "unsupported_harness",
    )


def test_collision_and_size_refusals(root: Path, source: Path) -> None:
    oversized = agent_root(root, "oversized")
    (oversized / "league").mkdir()
    oversized_target = oversized / LEAGUE_TARGET
    oversized_target.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
    refused(
        lambda: stage_guidance(source.resolve(), "codex", oversized.resolve()),
        "guidance_target_unsafe",
    )
    assert oversized_target.stat().st_size == MAX_GUIDANCE_BYTES + 1
    assert (oversized / "AGENTS.md").read_bytes() == UNIVERSAL

    collision = agent_root(root, "collision", prior_league=True)
    staging_file = collision / "league" / ".AGENTS.md.league-stage"
    staging_file.write_text("unrelated interrupted stage\n", encoding="utf-8")
    before = snapshot(collision)
    refused(
        lambda: stage_guidance(source.resolve(), "codex", collision.resolve()),
        "guidance_stage_collision",
    )
    assert snapshot(collision) == before

    oversized_source = root / "oversized-source.md"
    oversized_source.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
    destination = agent_root(root, "oversized-source-target")
    refused(
        lambda: stage_guidance(oversized_source.resolve(), "codex", destination.resolve()),
        "invalid_guidance_source",
    )


def test_tamper_and_receipt_refusals(root: Path, source: Path) -> None:
    tampered = agent_root(root, "tampered", prior_league=True)
    receipt = stage_guidance(source.resolve(), "codex", tampered.resolve())
    (tampered / "AGENTS.md").write_bytes(b"changed universal guide\n")
    supplement_before = (tampered / LEAGUE_TARGET).read_bytes()
    refused(
        lambda: rollback_guidance(tampered.resolve(), "codex", receipt),
        "universal_guidance_changed",
    )
    assert (tampered / LEAGUE_TARGET).read_bytes() == supplement_before

    invalid_receipt = agent_root(root, "invalid-receipt", prior_league=True)
    receipt = stage_guidance(source.resolve(), "codex", invalid_receipt.resolve())
    forged = dict(receipt)
    forged["installed_sha256"] = "0" * 64
    before = snapshot(invalid_receipt)
    refused(
        lambda: rollback_guidance(invalid_receipt.resolve(), "codex", forged),
        "guidance_receipt_invalid",
    )
    assert snapshot(invalid_receipt) == before


def main() -> None:
    source = ROOT / "global-agent-instructions" / "league" / "AGENTS.md"
    original = source_contract(source)
    with tempfile.TemporaryDirectory(prefix="league-guidance-stage-") as temporary:
        root = Path(temporary)
        test_successful_stage_and_rollback(root, source, original)
        test_target_and_input_refusals(root, source)
        test_collision_and_size_refusals(root, source)
        test_tamper_and_receipt_refusals(root, source)
        test_backup_failure_restores_exact_state(root, source)
        test_post_install_universal_failure_restores_supplement(root, source)
        test_rollback_size_bounds(root, source)

    assert source.read_bytes() == original
    print(
        "PASS: League-only staging and rollback preserve the universal guide and "
        "forbid its target before mutation"
    )


if __name__ == "__main__":
    main()
