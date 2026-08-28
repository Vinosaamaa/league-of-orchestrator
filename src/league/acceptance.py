"""Explicit-root isolated acceptance and reversible cutover foundation.

This module operates only beneath caller-supplied temporary roots.  Its
adapters are deliberately synthetic and its receipts never claim real harness
or terminal-backend support.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .importer import build_import_plan
from .sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage
from .storage import StorageRefusal


RECEIPT_SCHEMA = "league.acceptance-receipt.v1"
HOOK_FIXTURE_SCHEMA = "league.synthetic-hook.v1"
PROCESS_SENTINEL_SCHEMA = "league.synthetic-process-sentinel.v1"
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
POINTER_STAGES = (
    "lock_acquired",
    "backup_recorded",
    "old_writer_quiesced",
    "pointer_prepared",
    "pointer_switched",
    "new_writer_activated",
    "generation_verified",
)
PENDING_SLICES = (
    ("request", 17),
    ("assignment", 4),
    ("watcher", 3),
    ("stop", 5),
    ("teardown", 11),
)
UNVERIFIED_RUNTIMES = ("codex", "cursor", "pi", "herdr", "tmux")


def _stable_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    _atomic_write(path, _stable_bytes(value), mode=mode)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_object(path: Path, *, schema: Optional[str] = None) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StorageRefusal("invalid_sentinel", "sentinel must be an explicit regular file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageRefusal("invalid_sentinel", "sentinel JSON is malformed") from exc
    if not isinstance(value, dict) or (schema is not None and value.get("schema") != schema):
        raise StorageRefusal("invalid_sentinel", "sentinel JSON schema is unsupported")
    return value


def _tree_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.exists():
        raise StorageRefusal(
            "invalid_sentinel", "sentinel paths must be explicit existing non-symlinks"
        )
    records: list[dict[str, Any]] = []
    candidates = [path] if path.is_file() else [path, *sorted(path.rglob("*"))]
    for candidate in candidates:
        if candidate.is_symlink():
            raise StorageRefusal("invalid_sentinel", "sentinel trees cannot contain symlinks")
        relative = "." if candidate == path else candidate.relative_to(path).as_posix()
        stat = candidate.stat()
        if candidate.is_dir():
            records.append({"path": relative, "kind": "directory", "mode": stat.st_mode & 0o777})
        elif candidate.is_file():
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.st_mode & 0o777,
                    "bytes": stat.st_size,
                    "sha256": _sha256(candidate.read_bytes()),
                }
            )
        else:
            raise StorageRefusal("invalid_sentinel", "sentinel trees require regular files")
    return {"path": str(path), "digest": _sha256(_stable_bytes(records)), "records": records}


@dataclass
class SentinelSet:
    byte_paths: tuple[Path, ...]
    config_path: Path
    process_path: Path
    before: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        config = _read_object(self.config_path)
        processes = _read_object(self.process_path, schema=PROCESS_SENTINEL_SCHEMA)
        if not isinstance(processes.get("processes"), list):
            raise StorageRefusal("invalid_sentinel", "process sentinel list is required")
        self.before = {
            "bytes": [_tree_snapshot(path) for path in self.byte_paths],
            "config": {
                "snapshot": _tree_snapshot(self.config_path),
                "canonical_sha256": _sha256(_stable_bytes(config)),
            },
            "process": {
                "snapshot": _tree_snapshot(self.process_path),
                "canonical_sha256": _sha256(_stable_bytes(processes)),
                "count": len(processes["processes"]),
            },
        }

    def verify(self) -> dict[str, Any]:
        after = SentinelSet(self.byte_paths, self.config_path, self.process_path).before
        if after != self.before:
            raise StorageRefusal("sentinel_changed", "a caller-specified live sentinel changed")
        return {
            "unchanged": True,
            "byte_paths": len(self.before["bytes"]),
            "config_sha256": self.before["config"]["canonical_sha256"],
            "process_sha256": self.before["process"]["canonical_sha256"],
            "process_count": self.before["process"]["count"],
        }


@dataclass
class DeterministicContext:
    at: str = "2026-01-01T00:00:00Z"
    sequence: int = 0

    def identifier(self, kind: str) -> str:
        self.sequence += 1
        return f"synthetic-{kind}-{self.sequence:04d}"


class FakeAdapter:
    def __init__(self, name: str, context: DeterministicContext) -> None:
        self.name = name
        self.context = context
        self.calls: list[dict[str, Any]] = []

    def call(self, operation: str, **fields: Any) -> dict[str, Any]:
        receipt = {
            "adapter": f"fake-{self.name}",
            "operation": operation,
            "receipt_id": self.context.identifier(f"{self.name}-receipt"),
            "at": self.context.at,
            "fields": fields,
        }
        self.calls.append(receipt)
        return receipt


class FakeProcessResourceAdapter(FakeAdapter):
    def __init__(self, context: DeterministicContext) -> None:
        super().__init__("process-resource", context)
        self.processes: dict[int, dict[str, Any]] = {}

    def start(
        self, *, pid: int, process_start: str, endpoint: str, generation: str
    ) -> dict[str, Any]:
        if pid in self.processes:
            raise StorageRefusal("resource_conflict", "synthetic process identity collided")
        self.processes[pid] = {
            "pid": pid,
            "process_start": process_start,
            "endpoint": endpoint,
            "generation": generation,
            "running": True,
        }
        return self.call("start", **self.processes[pid])

    def terminate(self, identity: dict[str, Any]) -> dict[str, Any]:
        observed = self.processes.get(int(identity["pid"]))
        if observed is None or any(
            observed.get(key) != identity.get(key)
            for key in ("pid", "process_start", "endpoint", "generation")
        ):
            raise StorageRefusal("resource_identity_mismatch", "exact canary resource changed")
        observed["running"] = False
        return self.call("terminate", **observed)


def _fake_adapters(context: DeterministicContext) -> dict[str, FakeAdapter]:
    return {
        "harness": FakeAdapter("harness", context),
        "terminal_backend": FakeAdapter("terminal-backend", context),
        "git": FakeAdapter("git", context),
        "github": FakeAdapter("github", context),
        "process_resource": FakeProcessResourceAdapter(context),
        "notification": FakeAdapter("notification", context),
        "deployment": FakeAdapter("deployment", context),
        "hook": FakeAdapter("hook", context),
    }


def validate_hook_fixture(harness: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate only the synthetic hook envelope, never provider capability."""
    if harness not in {"codex", "cursor", "pi"}:
        raise StorageRefusal("invalid_hook_fixture", "synthetic harness fixture is unsupported")
    if payload != {
        "schema": HOOK_FIXTURE_SCHEMA,
        "harness": harness,
        "event": "stop",
        "session_ref": f"synthetic-{harness}-session",
    }:
        raise StorageRefusal("invalid_hook_fixture", "synthetic hook fixture is malformed")
    return {
        "harness": harness,
        "fixture_consumed": True,
        "configuration_mutated": False,
        "runtime_support_proven": False,
    }


def _load_fixture_module(source_root: Path) -> Any:
    fixture_path = source_root / "tests/storage_fixture.py"
    if not fixture_path.is_file():
        raise StorageRefusal("fixture_missing", "source-managed legacy fixture is missing")
    spec = importlib.util.spec_from_file_location("league_acceptance_fixture", fixture_path)
    if spec is None or spec.loader is None:
        raise StorageRefusal("fixture_missing", "source-managed legacy fixture cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_shadow(home: Path, source_root: Path) -> dict[str, Any]:
    fixture_module = _load_fixture_module(source_root)
    legacy_root = home / "legacy-fixture"
    state_root = home / "state"
    legacy_root.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    fixture = fixture_module.write_complete_fixture(legacy_root)
    legacy_before = _tree_snapshot(legacy_root)["digest"]
    with SQLiteStorage.for_migration(state_root, request_wal=False) as store:
        migration = store.migrate()
    with SQLiteStorage(state_root, request_wal=False) as store:
        plan = build_import_plan(
            legacy_root, fixture["manifest"], target_counts=store.import_target_counts()
        )
        dry_run = plan["report"]
        if not dry_run["dry_run"] or not dry_run["eligible"]:
            raise StorageRefusal("shadow_parity_failed", "fixture dry-run was not eligible")
        applied = store.apply_import(plan, plan["report_digest"])
        exported = json.loads(
            store.export_bytes(format_name="json", purpose="rollback", max_records=10_000)
        )
        expected_rows = plan["rows"]
        for table, rows in expected_rows.items():
            expected = sorted(rows, key=lambda row: _stable_bytes(row))
            observed = sorted(exported["tables"][table], key=lambda row: _stable_bytes(row))
            if expected != observed:
                raise StorageRefusal("shadow_parity_failed", f"fixture parity failed for {table}")
        parity_payload = {
            table: sorted(rows, key=lambda row: _stable_bytes(row))
            for table, rows in expected_rows.items()
        }
        parity_digest = _sha256(_stable_bytes(parity_payload))
        if not store.integrity()["ok"]:
            raise StorageRefusal("shadow_parity_failed", "fixture target integrity failed")
    return {
        "migration": migration,
        "dry_run": {
            "eligible": True,
            "report_digest": dry_run["report_digest"],
            "source_digest": dry_run["source_digest"],
            "artifact_counts": dry_run["artifact_counts"],
        },
        "apply": {"applied": applied["applied"], "row_counts": dry_run["row_counts"]},
        "exact_parity": True,
        "parity_sha256": parity_digest,
        "legacy_unchanged": legacy_before == _tree_snapshot(legacy_root)["digest"],
    }


def _release_files(source_root: Path) -> list[Path]:
    files = [
        source_root / "VERSION",
        source_root / "bin/league",
        source_root / "tests/storage_fixture.py",
    ]
    files.extend(sorted((source_root / "src/league").glob("*.py")))
    files.extend(sorted((source_root / "schema").glob("*.json")))
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise StorageRefusal(
            "release_incomplete", "release manifest contains a missing source file"
        )
    return files


def _switch_symlink(link: Path, target: str) -> None:
    temporary = link.with_name(f".{link.name}.next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _run_checked(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise StorageRefusal("staged_check_failed", "a staged executable check failed")
    return result.stdout


def _staged_install(home: Path, source_root: Path) -> dict[str, Any]:
    if (source_root / "VERSION").read_text(encoding="utf-8").strip() != __version__:
        raise StorageRefusal("staged_version_failed", "source version declarations disagree")
    prefix = home / "stage-prefix"
    release_bundle = home / "release-bundle" / __version__
    releases = prefix / "releases"
    release = releases / __version__
    legacy = releases / "0.0.0-legacy"
    for directory in (
        release_bundle,
        prefix,
        releases,
        release,
        legacy,
        prefix / "bin",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (legacy / "bin").mkdir(mode=0o700)
    legacy_launcher = legacy / "bin/league"
    _atomic_write(
        legacy_launcher,
        b"#!/usr/bin/env python3\nprint('league 0.0.0-legacy')\n",
        mode=0o755,
    )
    source_hashes: dict[str, str] = {}
    release_hashes: dict[str, str] = {}
    staged_hashes: dict[str, str] = {}
    for source in _release_files(source_root):
        relative = source.relative_to(source_root)
        bundle_file = release_bundle / relative
        bundle_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, bundle_file)
        os.chmod(bundle_file, 0o755 if relative == Path("bin/league") else 0o644)
        destination = release / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(bundle_file, destination)
        os.chmod(destination, 0o755 if relative == Path("bin/league") else 0o644)
        source_hashes[relative.as_posix()] = _sha256(source.read_bytes())
        release_hashes[relative.as_posix()] = _sha256(bundle_file.read_bytes())
        staged_hashes[relative.as_posix()] = _sha256(destination.read_bytes())
    if not source_hashes == release_hashes == staged_hashes:
        raise StorageRefusal(
            "staged_parity_failed", "source, release bundle, and staged bytes differ"
        )
    forbidden = (str(source_root).encode(), str(prefix).encode())
    for tree in (release_bundle, release):
        for path in tree.rglob("*"):
            if path.is_file() and any(value in path.read_bytes() for value in forbidden):
                raise StorageRefusal(
                    "staged_path_leak", "release or staged bytes contain a local path leak"
                )
    current = prefix / "current"
    current.symlink_to("releases/0.0.0-legacy")
    stable = prefix / "bin/league"
    stable.symlink_to("../current/bin/league")
    previous_target = os.readlink(current)
    _switch_symlink(current, f"releases/{__version__}")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    version_output = _run_checked(
        [str(stable), "--version"], cwd=home, env=environment
    ).strip()
    help_output = _run_checked([str(stable), "--help"], cwd=home, env=environment)
    if version_output != f"league {__version__}" or "acceptance" not in help_output:
        raise StorageRefusal("staged_version_failed", "staged launcher version/help mismatch")
    staged_state = home / "staged-schema-state"
    staged_state.mkdir(mode=0o700)
    staged_migration = json.loads(
        _run_checked(
            [
                str(stable),
                "--state-root",
                str(staged_state),
                "--no-wal",
                "storage",
                "migrate",
            ],
            cwd=home,
            env=environment,
        )
    )
    staged_integrity = json.loads(
        _run_checked(
            [
                str(stable),
                "--state-root",
                str(staged_state),
                "--no-wal",
                "storage",
                "integrity",
            ],
            cwd=home,
            env=environment,
        )
    )
    migration_result = staged_migration.get("result", {})
    integrity_result = staged_integrity.get("result", {})
    if (
        not staged_migration.get("ok")
        or migration_result.get("to_version") != CURRENT_SCHEMA_VERSION
        or not staged_integrity.get("ok")
        or not integrity_result.get("ok")
    ):
        raise StorageRefusal(
            "staged_schema_failed", "staged schema migration or integrity check failed"
        )
    schema_files = sorted((release / "schema").glob("*.json"))
    for schema_file in schema_files:
        value = json.loads(schema_file.read_text(encoding="utf-8"))
        if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise StorageRefusal("staged_schema_failed", "staged schema is malformed")
    hook_checks = []
    hook_script = (
        "import json,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from league.acceptance import validate_hook_fixture;"
        "h=sys.argv[2];"
        "p={'schema':'league.synthetic-hook.v1','harness':h,'event':'stop',"
        "'session_ref':'synthetic-'+h+'-session'};"
        "print(json.dumps(validate_hook_fixture(h,p),sort_keys=True,separators=(',',':')))"
    )
    for harness in ("codex", "cursor", "pi"):
        hook_checks.append(
            json.loads(
                _run_checked(
                    [sys.executable, "-c", hook_script, str(release / "src"), harness],
                    cwd=home,
                    env=environment,
                )
            )
        )
    expected_modes = {
        relative: (0o755 if relative == "bin/league" else 0o644)
        for relative in staged_hashes
    }
    observed_modes = {
        relative: (release / relative).stat().st_mode & 0o777 for relative in staged_hashes
    }
    release_modes = {
        relative: (release_bundle / relative).stat().st_mode & 0o777
        for relative in release_hashes
    }
    if observed_modes != expected_modes or release_modes != expected_modes or any(
        path.stat().st_mode & 0o022
        for path in (release_bundle, prefix, releases, release, legacy)
    ):
        raise StorageRefusal(
            "staged_permissions_failed", "staged release permissions are not owner-controlled"
        )
    _switch_symlink(current, previous_target)
    rollback_version = _run_checked([str(stable), "--version"], cwd=home, env=environment).strip()
    if rollback_version != "league 0.0.0-legacy":
        raise StorageRefusal("staged_rollback_failed", "staged stable pointer did not roll back")
    source_manifest_digest = _sha256(_stable_bytes(source_hashes))
    release_manifest_digest = _sha256(_stable_bytes(release_hashes))
    staged_manifest_digest = _sha256(_stable_bytes(staged_hashes))
    return {
        "prefix": str(prefix),
        "version": __version__,
        "source_release_staged_parity": True,
        "source_manifest_sha256": source_manifest_digest,
        "release_manifest_sha256": release_manifest_digest,
        "staged_manifest_sha256": staged_manifest_digest,
        "file_count": len(source_hashes),
        "launcher_resolution": True,
        "help_checked": True,
        "schemas_checked": len(schema_files),
        "schema_migration": {
            "to_version": migration_result["to_version"],
            "journal_mode": migration_result["policy"]["journal_mode"],
            "integrity": integrity_result["ok"],
        },
        "hook_fixtures": hook_checks,
        "permissions_checked": True,
        "path_leaks": False,
        "rollback": {
            "completed": True,
            "restored_target": previous_target,
            "observed_version": rollback_version.removeprefix("league "),
        },
    }


def _operation_write(
    path: Path,
    receipt: dict[str, Any],
    state: str,
    context: DeterministicContext,
    **extra: Any,
) -> None:
    receipt["state"] = state
    receipt["history"].append({"state": state, "at": context.at, **extra})
    _write_json(path, receipt)


def _cutover_case(
    root: Path,
    context: DeterministicContext,
    fault_stage: Optional[str],
    *,
    resume: bool,
) -> dict[str, Any]:
    root.mkdir(parents=True, mode=0o700)
    lock_path = root / "cutover.lock"
    pointer_path = root / "writer-pointer.json"
    writers_path = root / "writers.json"
    operation_path = root / "operation.json"
    old = {
        "schema": "league.writer-pointer.v1",
        "generation": "generation-old",
        "writer": "legacy",
        "version": "0.0.0-legacy",
    }
    new = {
        "schema": "league.writer-pointer.v1",
        "generation": "generation-new",
        "writer": "sqlite",
        "version": __version__,
    }
    _write_json(pointer_path, old)
    history: list[list[str]] = []

    def set_writers(values: list[dict[str, str]]) -> None:
        if len(values) > 1:
            raise StorageRefusal("dual_writer", "two canonical writers are forbidden")
        _write_json(writers_path, {"active": values})
        history.append([item["generation"] for item in values])

    def coherent() -> dict[str, Any]:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        writers = json.loads(writers_path.read_text(encoding="utf-8"))["active"]
        if len(writers) != 1 or writers[0]["generation"] != pointer["generation"]:
            raise StorageRefusal("generation_mismatch", "writer and pointer generations disagree")
        return pointer

    set_writers([old])
    receipt = {
        "schema": "league.cutover-operation.v1",
        "operation_id": context.identifier("cutover-operation"),
        "fault_stage": fault_stage,
        "history": [],
    }
    _operation_write(operation_path, receipt, "planned", context)
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = lock_path.open("a+b")
        try:
            try:
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_exclusive = True
            else:
                lock_exclusive = False
                fcntl.flock(second, fcntl.LOCK_UN)
        finally:
            second.close()
        if not lock_exclusive:
            raise StorageRefusal("cutover_lock_failed", "exclusive cutover lock was not exclusive")
        _operation_write(operation_path, receipt, "executing", context, stage="lock_acquired")
        try:
            if fault_stage == "lock_acquired":
                raise RuntimeError(fault_stage)
            _write_json(root / "pointer-backup.json", old)
            if fault_stage == "backup_recorded":
                raise RuntimeError(fault_stage)
            set_writers([])
            if fault_stage == "old_writer_quiesced":
                raise RuntimeError(fault_stage)
            _write_json(root / "pointer.next.json", new)
            if fault_stage == "pointer_prepared":
                raise RuntimeError(fault_stage)
            os.replace(root / "pointer.next.json", pointer_path)
            if fault_stage == "pointer_switched":
                raise RuntimeError(fault_stage)
            set_writers([new])
            if fault_stage == "new_writer_activated":
                raise RuntimeError(fault_stage)
            coherent()
            if fault_stage == "generation_verified":
                raise RuntimeError(fault_stage)
            _operation_write(operation_path, receipt, "completed", context, outcome="new")
        except RuntimeError:
            selected = json.loads(pointer_path.read_text(encoding="utf-8"))
            set_writers([new if selected["generation"] == new["generation"] else old])
            coherent()
            _operation_write(
                operation_path,
                receipt,
                "blocked",
                context,
                stage=fault_stage,
                resumable=True,
            )
            if resume:
                _operation_write(operation_path, receipt, "executing", context, stage="resume")
                outcome = "new" if selected["generation"] == new["generation"] else "old"
                _operation_write(operation_path, receipt, "completed", context, outcome=outcome)
    final = coherent()
    return {
        "fault_stage": fault_stage,
        "terminal_state": receipt["state"],
        "final_generation": final["generation"],
        "history": receipt["history"],
        "max_active_writers": max(len(item) for item in history),
        "lock_exclusive": True,
        "coherent": True,
    }


def _cutover_matrix(home: Path, context: DeterministicContext) -> dict[str, Any]:
    cases = [_cutover_case(home / "normal", context, None, resume=True)]
    for index, stage in enumerate(POINTER_STAGES):
        cases.append(
            _cutover_case(
                home / f"fault-{index:02d}-{stage}",
                context,
                stage,
                resume=index != len(POINTER_STAGES) - 1,
            )
        )
    if any(not case["coherent"] or case["max_active_writers"] > 1 for case in cases):
        raise StorageRefusal("cutover_matrix_failed", "cutover matrix left an incoherent writer")
    return {
        "pointer_schema": "league.writer-pointer.v1",
        "generation_bound": True,
        "exclusive_lock": True,
        "fault_stages": list(POINTER_STAGES),
        "cases": cases,
        "never_two_writers": True,
    }


def _canary(
    home: Path,
    context: DeterministicContext,
    adapters: dict[str, FakeAdapter],
) -> dict[str, Any]:
    process = adapters["process_resource"]
    if not isinstance(process, FakeProcessResourceAdapter):
        raise StorageRefusal("adapter_mismatch", "synthetic process adapter is unavailable")
    generation = "synthetic-canary-generation-0001"
    identity = {
        "pid": 4242,
        "process_start": "synthetic-start-0001",
        "endpoint": "synthetic://canary/endpoint-0001",
        "generation": generation,
    }
    process.start(**identity)
    resource = {
        "resource_id": context.identifier("canary-resource"),
        "task_id": "synthetic-acceptance-task",
        "owner": "SyntheticChampion",
        "generation": generation,
        **identity,
    }
    registry = home / "canary-resources.json"
    _write_json(registry, {"schema": "league.canary-resources.v1", "resources": [resource]})
    observed = json.loads(registry.read_text(encoding="utf-8"))["resources"]
    if observed != [resource]:
        raise StorageRefusal("resource_identity_mismatch", "canary registration changed")
    wrong_identity = dict(identity, generation="synthetic-wrong-generation")
    try:
        process.terminate(wrong_identity)
    except StorageRefusal as exc:
        if exc.code != "resource_identity_mismatch":
            raise
        mismatch_refused = True
    else:
        raise StorageRefusal("resource_cleanup_failed", "wrong-generation cleanup was accepted")
    cleanup = process.terminate(identity)
    _write_json(registry, {"schema": "league.canary-resources.v1", "resources": []})
    if json.loads(registry.read_text(encoding="utf-8"))["resources"]:
        raise StorageRefusal("resource_cleanup_failed", "canary registry was not released")
    return {
        "registered_exactly": True,
        "resource_id": resource["resource_id"],
        "generation": resource["generation"],
        "cleanup_exact": True,
        "wrong_generation_refused": mismatch_refused,
        "adapter_receipt": cleanup["receipt_id"],
        "real_runtime_proven": False,
    }


def run_acceptance(
    temporary_root: Path,
    namespace: str,
    *,
    sentinel_paths: tuple[Path, ...],
    config_sentinel: Path,
    process_sentinel: Path,
    source_root: Optional[Path] = None,
) -> dict[str, Any]:
    root = Path(temporary_root)
    if (
        not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
        or root.resolve() == Path("/")
    ):
        raise StorageRefusal(
            "invalid_temporary_root", "temporary root must be an explicit directory"
        )
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise StorageRefusal("invalid_namespace", "acceptance namespace is invalid")
    if not sentinel_paths:
        raise StorageRefusal("sentinel_required", "at least one byte sentinel is required")
    source = (source_root or Path(__file__).resolve().parents[2]).resolve()
    sentinels = SentinelSet(
        tuple(Path(path) for path in sentinel_paths),
        Path(config_sentinel),
        Path(process_sentinel),
    )
    home = root / f"league-{namespace}"
    try:
        home.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StorageRefusal("namespace_collision", "acceptance namespace already exists") from exc
    context = DeterministicContext()
    adapters = _fake_adapters(context)
    adapters["harness"].call("create", namespace=namespace)
    adapters["terminal_backend"].call("create-namespace", namespace=namespace)
    adapters["git"].call("inspect-fixture", repository="synthetic://repository")
    adapters["github"].call("inspect-fixture", repository="synthetic://repository")
    adapters["notification"].call("record-only", delivery="disabled")
    adapters["deployment"].call("record-only", deployment="disabled")
    for harness in ("codex", "cursor", "pi"):
        adapters["hook"].call(
            "consume-fixture",
            **validate_hook_fixture(
                harness,
                {
                    "schema": HOOK_FIXTURE_SCHEMA,
                    "harness": harness,
                    "event": "stop",
                    "session_ref": f"synthetic-{harness}-session",
                },
            ),
        )
    migration = _migration_shadow(home, source)
    staged = _staged_install(home, source)
    cutover = _cutover_matrix(home / "cutover", context)
    canary = _canary(home, context, adapters)
    sentinel_receipt = sentinels.verify()
    adapter_receipt = {
        name: {"kind": f"fake-{adapter.name}", "calls": len(adapter.calls), "real": False}
        for name, adapter in adapters.items()
    }
    result = {
        "schema": RECEIPT_SCHEMA,
        "version": __version__,
        "namespace": namespace,
        "home": str(home),
        "determinism": {"clock": context.at, "ids_allocated": context.sequence},
        "sentinels": sentinel_receipt,
        "migration_shadow": migration,
        "staged_install": staged,
        "cutover": cutover,
        "canary": canary,
        "adapters": adapter_receipt,
        "pending_assertions": [
            {
                "slice": name,
                "issue": issue,
                "status": "pending",
                "passed": False,
                "reason": "owning lifecycle slice is not merged",
            }
            for name, issue in PENDING_SLICES
        ],
        "runtime_claims": [
            {"runtime": name, "status": "unverified", "mock_proof": False}
            for name in UNVERIFIED_RUNTIMES
        ],
    }
    _write_json(home / "acceptance-receipt.json", result)
    return result
