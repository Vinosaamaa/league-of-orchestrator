#!/usr/bin/env python3
"""Focused production-boundary apply, refusal, and rollback checks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import league.livecutover as livecutover  # noqa: E402
from league import __version__  # noqa: E402
from league.livecutover import run_live_cutover, verify_legacy_archive  # noqa: E402
from league.precutover import run_pre_cutover  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from test_pre_cutover import fixture_plan, write_json  # noqa: E402


def refused(operation: Callable[[], Any], code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def node_fingerprint(path: Path) -> tuple[Any, ...]:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode):
        payload: Any = os.readlink(path)
        kind = "symlink"
    elif stat.S_ISREG(details.st_mode):
        payload = hashlib.sha256(path.read_bytes()).hexdigest()
        kind = "file"
    elif stat.S_ISDIR(details.st_mode):
        payload = None
        kind = "directory"
    else:
        payload = None
        kind = "other"
    return (
        kind,
        stat.S_IMODE(details.st_mode),
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        payload,
    )


def tree_fingerprint(root: Path) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    return tuple(
        (
            "." if path == root else path.relative_to(root).as_posix(),
            node_fingerprint(path),
        )
        for path in paths
    )


def prepare_authority(
    root: Path,
    namespace: str,
    *,
    include_league_supplement: bool = False,
    include_cursor: bool = False,
) -> dict[str, Any]:
    fixture = fixture_plan(root / "fixture")
    write_json(fixture["hook"], {"hooks": {}})
    universal = fixture["live"] / ".agents/AGENTS.md"
    league_supplement = fixture["live"] / ".agents/league/AGENTS.md"
    universal.parent.mkdir(parents=True, exist_ok=True)
    universal.write_bytes(b"synthetic toolkit-owned universal guide\n")
    league_supplement.parent.mkdir(parents=True, exist_ok=True)
    league_supplement.write_bytes(b"synthetic prior League supplement\n")
    if include_league_supplement:
        fixture["plan"]["current_targets"].append(
            {
                "target_id": "league-supplement",
                "kind": "configuration",
                "path": str(league_supplement),
                "required": True,
            }
        )
    cursor_hooks = None
    if include_cursor:
        cursor_hooks = fixture["live"] / "config/cursor-hooks.json"
        write_json(
            cursor_hooks,
            {"version": 1, "hooks": {"sessionStart": [{"command": "keep-me"}]}},
        )
        fixture["plan"]["current_targets"].append(
            {
                "target_id": "cursor-hooks",
                "kind": "hook_config",
                "path": str(cursor_hooks),
                "required": True,
            }
        )
        fixture["plan"]["proposed"]["hooks"].append(
            {"harness": "cursor", "target": str(cursor_hooks)}
        )
    write_json(fixture["plan_path"], fixture["plan"])
    acceptance = root / "acceptance"
    acceptance.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    config = root / "config.json"
    write_json(config, {"schema": "league.caller-config-sentinel.v1"})
    result = run_pre_cutover(
        acceptance,
        namespace,
        plan_path=fixture["plan_path"],
        sentinel_paths=(sentinel,),
        config_sentinel=config,
        process_sentinel=fixture["processes"],
    )
    return {
        **fixture,
        "acceptance": acceptance,
        "authority_receipt": Path(result["home"]) / "precutover-receipt.json",
        "authority_digest": result["mutation_manifest"]["manifest_sha256"],
        "universal": universal,
        "league_supplement": league_supplement,
        "cursor_hooks": cursor_hooks,
        "sentinel": sentinel,
        "config": config,
    }


def apply(fixture: dict[str, Any], namespace: str) -> dict[str, Any]:
    return run_live_cutover(
        fixture["acceptance"],
        namespace,
        plan_path=fixture["plan_path"],
        authority_receipt=fixture["authority_receipt"],
        authority_digest=fixture["authority_digest"],
        source_root=ROOT,
    )


def test_universal_target_refuses_without_any_filesystem_change(root: Path) -> None:
    for target_kind in ("direct", "symlink-alias"):
        case = root / target_kind
        namespace = f"universal-{target_kind}"
        fixture = prepare_authority(case, namespace)
        target = fixture["universal"]
        if target_kind == "symlink-alias":
            target = fixture["live"] / "config/universal-guide-alias"
            target.symlink_to(fixture["universal"])
        fixture["plan"]["current_targets"].append(
            {
                "target_id": "generic-current-config",
                "kind": "configuration",
                "path": str(target),
                "required": True,
            }
        )
        write_json(fixture["plan_path"], fixture["plan"])
        before = tree_fingerprint(case)
        refused(
            lambda fixture=fixture: run_pre_cutover(
                fixture["acceptance"],
                "universal-preflight",
                plan_path=fixture["plan_path"],
                sentinel_paths=(fixture["sentinel"],),
                config_sentinel=fixture["config"],
                process_sentinel=fixture["processes"],
            ),
            "universal_guidance_forbidden",
        )
        assert tree_fingerprint(case) == before
        refused(lambda fixture=fixture: apply(fixture, namespace), "universal_guidance_forbidden")
        assert tree_fingerprint(case) == before


def test_release_identity_collisions_refuse_before_any_filesystem_change(
    root: Path,
) -> None:
    assert __version__ == "0.2.29"
    for collision_kind in ("release", "bundle"):
        case = root / collision_kind
        namespace = f"{collision_kind}-collision"
        fixture = prepare_authority(case, namespace)
        if collision_kind == "release":
            collision = (
                Path(fixture["plan"]["proposed"]["release_prefix"])
                / "releases"
                / __version__
            )
        else:
            collision = (
                fixture["acceptance"]
                / f"league-{namespace}-cutover"
                / "release-bundle"
                / __version__
            )
        collision.mkdir(parents=True)
        (collision / "retained-byte").write_bytes(b"pre-existing identity\n")
        before = tree_fingerprint(case)
        refused(
            lambda fixture=fixture, namespace=namespace: apply(fixture, namespace),
            "cutover_release_identity_exists",
        )
        assert tree_fingerprint(case) == before


def test_rollback_preserves_unchanged_guide_nodes(root: Path) -> None:
    namespace = "guide-rollback"
    fixture = prepare_authority(root, namespace, include_league_supplement=True)
    universal_before = node_fingerprint(fixture["universal"])
    supplement_before = node_fingerprint(fixture["league_supplement"])
    original_smoke = livecutover._live_watcher_smoke

    def fail_after_target_mutations(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise StorageRefusal("synthetic_cutover_failure", "synthetic late failure")

    livecutover._live_watcher_smoke = fail_after_target_mutations
    try:
        refused(lambda: apply(fixture, namespace), "synthetic_cutover_failure")
    finally:
        livecutover._live_watcher_smoke = original_smoke
    assert node_fingerprint(fixture["universal"]) == universal_before
    assert node_fingerprint(fixture["league_supplement"]) == supplement_before
    assert (
        Path(fixture["plan"]["proposed"]["backup_root"])
        / "rollback-receipt.json"
    ).is_file()
    retry_before = tree_fingerprint(root)
    refused(lambda: apply(fixture, namespace), "cutover_release_identity_exists")
    assert tree_fingerprint(root) == retry_before


def test_authority_bound_live_apply(root: Path) -> None:
    namespace = "focused-live"
    fixture = prepare_authority(
        root,
        namespace,
        include_league_supplement=True,
        include_cursor=True,
    )
    universal_before = node_fingerprint(fixture["universal"])
    supplement_before = node_fingerprint(fixture["league_supplement"])
    applied = apply(fixture, namespace)
    assert applied["state"] == "completed"
    assert node_fingerprint(fixture["universal"]) == universal_before
    assert node_fingerprint(fixture["league_supplement"]) == supplement_before
    assert Path(fixture["plan"]["proposed"]["state_root"]).is_dir()
    assert json.loads(
        Path(fixture["plan"]["proposed"]["writer_pointer"]).read_text()
    )["writer"] == "sqlite"
    watcher = subprocess.run(
        [
            str(Path(fixture["plan"]["proposed"]["watcher_launcher"])),
            "--shotcaller",
            "Garen",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LEAGUE_WRITER_POINTER": fixture["plan"]["proposed"]["writer_pointer"],
            "LEAGUE_STATE_ROOT": fixture["plan"]["proposed"]["state_root"],
        },
    )
    assert watcher.returncode == 0, watcher.stderr
    assert json.loads(watcher.stdout)["writer"] == "sqlite"
    hook_receipts = {item["harness"]: item for item in applied["hooks"]}
    assert hook_receipts["codex"]["added"] == ["UserPromptSubmit", "Stop"]
    assert hook_receipts["cursor"]["added"] == ["beforeSubmitPrompt", "stop"]
    assert json.loads(fixture["cursor_hooks"].read_text())["hooks"][
        "sessionStart"
    ] == [{"command": "keep-me"}]
    assert applied["watcher_smoke"]["status"] == "passed"
    archive = (
        Path(fixture["plan"]["proposed"]["archive_root"])
        / applied["writer_generation"]
    )
    assert verify_legacy_archive(archive)["verified"] is True
    assert (archive / "RESTORE.md").is_file()
    restore = (archive / "RESTORE.md").read_text(encoding="utf-8")
    assert "acceptance archive-verify" in restore
    assert "never copy by hand" in restore
    manifest = json.loads((archive / "archive-manifest.json").read_text())
    archived = {item["target_id"] for item in manifest["entries"]}
    assert {
        "hooks",
        "cursor-hooks",
        "installed",
        "legacy",
        "watcher-launcher",
    } <= archived
    archived_installed = archive / "legacy-system/installed/bin/agent-watcher"
    archived_installed.write_bytes(archived_installed.read_bytes() + b"tampered")
    refused(lambda: verify_legacy_archive(archive), "legacy_archive_mismatch")
    refused(
        lambda: run_live_cutover(
            fixture["acceptance"],
            "wrong-authority",
            plan_path=fixture["plan_path"],
            authority_receipt=fixture["authority_receipt"],
            authority_digest="0" * 64,
            source_root=ROOT,
        ),
        "cutover_authority_invalid",
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-live-cutover-") as temporary:
        root = Path(temporary)
        test_universal_target_refuses_without_any_filesystem_change(root / "universal")
        test_release_identity_collisions_refuse_before_any_filesystem_change(
            root / "collisions"
        )
        test_rollback_preserves_unchanged_guide_nodes(root / "rollback")
        test_authority_bound_live_apply(root / "apply")
    print("PASS: production live cutover apply, refusal, and node-safe rollback")


if __name__ == "__main__":
    main()
