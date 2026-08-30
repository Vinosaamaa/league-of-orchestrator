"""Exact adapter-backed cleanup for an explicitly disposable acceptance canary."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .acceptance import _atomic_write, _stable_bytes
from .cleanup import CleanupAdapterRegistry, cleanup_action_digest
from .sqlite_runtime_ops import runtime_cleanup_identity
from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal


CONFIG_SCHEMA = "league.cleanup-canary-adapters.v1"
SAFE_AGENT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
CODEX_SESSION_TITLE = re.compile(
    r"^(?P<session>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) \| codex$"
)
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(
        self, arguments: Sequence[str], *, allow_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.run(
                    list(arguments),
                    stdout=stdout,
                    stderr=stderr,
                    timeout=30,
                    check=False,
                )
                outputs: list[str] = []
                for stream in (stdout, stderr):
                    size = stream.tell()
                    if size > MAX_COMMAND_OUTPUT_BYTES:
                        raise StorageRefusal(
                            "cleanup_adapter_output_too_large",
                            "cleanup adapter command output exceeded its bound",
                        )
                    stream.seek(0)
                    outputs.append(stream.read().decode("utf-8"))
                completed = subprocess.CompletedProcess(
                    list(arguments), process.returncode, outputs[0], outputs[1]
                )
        except UnicodeDecodeError as exc:
            raise StorageRefusal(
                "cleanup_adapter_failed", "cleanup adapter command output is not UTF-8"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageRefusal(
                "cleanup_adapter_failed", "cleanup adapter command could not complete"
            ) from exc
        if completed.returncode != 0 and not allow_failure:
            raise StorageRefusal(
                "cleanup_adapter_failed", "cleanup adapter command refused or failed"
            )
        return completed


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise StorageRefusal("cleanup_adapter_config_invalid", f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        raise StorageRefusal("cleanup_adapter_config_invalid", f"{label} must be absolute")
    return path


def _canonical(path: Path) -> Path:
    """Normalize macOS /var aliases while preserving a missing leaf for resume."""

    return path.resolve(strict=False)


def _beneath(path: Path, root: Path, label: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", f"{label} is outside the disposable root"
        ) from exc
    if path == root:
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", f"{label} must be narrower than the disposable root"
        )
    return path


def _validate_scope(value: Mapping[str, Any]) -> tuple[Path, Path]:
    supplied_root = _absolute(value["temporary_root"], "temporary_root")
    if supplied_root.is_symlink():
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", "disposable root must be an existing non-symlink directory"
        )
    root = _canonical(supplied_root)
    if not root.is_dir():
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", "disposable root must be an existing non-symlink directory"
        )
    archive = _beneath(
        _canonical(_absolute(value["archive_path"], "archive_path")),
        root,
        "archive_path",
    )
    return root, archive


def _validate_herdr(value: Any) -> dict[str, Any]:
    herdr = value
    if not isinstance(herdr, Mapping) or set(herdr) != {
        "agent_name",
        "workspace_id",
        "pane_id",
        "terminal_id",
        "session_id",
        "runtime_instance_id",
        "runtime_generation",
    }:
        raise StorageRefusal("cleanup_adapter_config_invalid", "Herdr identity is incomplete")
    if not isinstance(herdr["agent_name"], str) or not SAFE_AGENT.fullmatch(herdr["agent_name"]):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Herdr agent name is invalid")
    for field in (
        "workspace_id",
        "pane_id",
        "terminal_id",
        "session_id",
        "runtime_instance_id",
        "runtime_generation",
    ):
        if not isinstance(herdr[field], str) or not herdr[field] or herdr[field].strip() != herdr[field]:
            raise StorageRefusal("cleanup_adapter_config_invalid", "Herdr identity value is invalid")
    if not str(herdr["pane_id"]).startswith(f"{herdr['workspace_id']}:"):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Herdr pane and workspace disagree")
    return dict(herdr)


def _validate_git(value: Any, root: Path) -> dict[str, Any]:
    git = value
    if not isinstance(git, Mapping) or set(git) != {
        "repository",
        "worktree",
        "branch",
        "head",
        "base_ref",
        "merge_commit",
    }:
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git identity is incomplete")
    supplied_repository = _absolute(git["repository"], "git.repository")
    supplied_worktree = _absolute(git["worktree"], "git.worktree")
    if supplied_repository.is_symlink() or (
        supplied_worktree.exists() and supplied_worktree.is_symlink()
    ):
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", "Git canary paths must not be symlinks"
        )
    repository = _beneath(
        _canonical(supplied_repository),
        root,
        "repository",
    )
    worktree = _beneath(
        _canonical(supplied_worktree),
        root,
        "worktree",
    )
    if repository == worktree or repository in worktree.parents or worktree in repository.parents:
        raise StorageRefusal(
            "cleanup_adapter_scope_refused", "repository and removable worktree must be siblings"
        )
    if not repository.is_dir() or repository.is_symlink():
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git canary repository is not exact")
    if worktree.exists() and (not worktree.is_dir() or worktree.is_symlink()):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git canary worktree is not exact")
    if not isinstance(git["branch"], str) or not SAFE_BRANCH.fullmatch(git["branch"]):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git branch is invalid")
    if not isinstance(git["base_ref"], str) or not SAFE_BRANCH.fullmatch(git["base_ref"]):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git base ref is invalid")
    if not isinstance(git["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", git["head"]):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git head is invalid")
    if not isinstance(git["merge_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", git["merge_commit"]
    ):
        raise StorageRefusal("cleanup_adapter_config_invalid", "Git merge commit is invalid")
    return {**dict(git), "repository": str(repository), "worktree": str(worktree)}


def _validate_callsign(value: Any) -> dict[str, Any]:
    callsign = value
    if not isinstance(callsign, Mapping) or set(callsign) != {
        "assignment_id",
        "callsign",
        "expected_version",
    }:
        raise StorageRefusal("cleanup_adapter_config_invalid", "callsign identity is incomplete")
    if any(
        not isinstance(callsign[field], str)
        or not callsign[field]
        or callsign[field].strip() != callsign[field]
        for field in ("assignment_id", "callsign")
    ) or callsign["expected_version"] != 2:
        raise StorageRefusal("cleanup_adapter_config_invalid", "callsign identity is invalid")
    return dict(callsign)


def validate_canary_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "scope",
        "temporary_root",
        "archive_path",
        "herdr",
        "git",
        "callsign",
    }:
        raise StorageRefusal("cleanup_adapter_config_invalid", "canary adapter config is incomplete")
    if value.get("schema") != CONFIG_SCHEMA or value.get("scope") != "disposable-canary":
        raise StorageRefusal("cleanup_adapter_scope_refused", "only disposable canary cleanup is supported")
    root, archive = _validate_scope(value)
    herdr = _validate_herdr(value.get("herdr"))
    git = _validate_git(value.get("git"), root)
    callsign = _validate_callsign(value.get("callsign"))
    return {
        "schema": CONFIG_SCHEMA,
        "scope": "disposable-canary",
        "temporary_root": str(root),
        "archive_path": str(archive),
        "herdr": herdr,
        "git": git,
        "callsign": callsign,
    }


def _json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("cleanup_adapter_failed", "cleanup adapter returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("cleanup_adapter_failed", "cleanup adapter returned a non-object")
    return value


class _BaseAdapter:
    def intended(self, action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        return dict(observation) == dict(action["intended_state"])


class ArchiveAdapter(_BaseAdapter):
    kind = "archive"

    def __init__(self, path: Path) -> None:
        self.path = path

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.path.exists():
            return dict(action["expected_identity"])
        if not self.path.is_file() or self.path.is_symlink():
            raise StorageRefusal("cleanup_identity_mismatch", "archive target identity changed")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StorageRefusal("cleanup_identity_mismatch", "archive receipt is malformed") from exc
        if value != action["intended_state"]:
            raise StorageRefusal("cleanup_identity_mismatch", "archive receipt changed")
        return dict(value)

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = _stable_bytes(action["intended_state"])
        _atomic_write(self.path, payload, mode=0o600)
        return {"archive_sha256": hashlib.sha256(payload).hexdigest(), "disposable": True}


class HerdrHarnessAdapter(_BaseAdapter):
    kind = "harness"

    def __init__(self, identity: Mapping[str, Any], runner: CommandRunner) -> None:
        self.identity = dict(identity)
        self.runner = runner

    def _agent(self) -> Optional[dict[str, Any]]:
        value = _json_output(self.runner.run(("herdr", "agent", "list")))
        agents = value.get("result", {}).get("agents", [])
        if not isinstance(agents, list) or any(
            not isinstance(item, Mapping) for item in agents
        ):
            raise StorageRefusal(
                "cleanup_adapter_failed", "Herdr agent inventory is malformed"
            )
        matches = [item for item in agents if item.get("name") == self.identity["agent_name"]]
        if not matches:
            return None
        if len(matches) != 1:
            raise StorageRefusal("cleanup_identity_mismatch", "Herdr canary agent is ambiguous")
        return matches[0]

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        agent = self._agent()
        if agent is None:
            return dict(action["intended_state"])
        session = agent.get("agent_session", {})
        session_id = session.get("value") if isinstance(session, Mapping) else None
        if session_id is None:
            title = agent.get("terminal_title_stripped")
            matched = CODEX_SESSION_TITLE.fullmatch(title) if isinstance(title, str) else None
            session_id = matched.group("session") if matched is not None else None
        observed = {
            "agent_name": agent.get("name"),
            "pane_id": agent.get("pane_id"),
            "session_id": session_id,
        }
        if observed != action["expected_identity"]:
            raise StorageRefusal("cleanup_identity_mismatch", "Herdr agent identity changed")
        if agent.get("agent_status") == "done":
            return dict(action["intended_state"])
        return observed

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        completed = self.runner.run(
            (
                "herdr",
                "agent",
                "prompt",
                self.identity["agent_name"],
                "/exit",
                "--wait",
                "--timeout",
                "30000",
            )
        )
        if completed.returncode != 0:
            raise StorageRefusal("cleanup_adapter_failed", "Codex canary exit was rejected")
        remaining = self._agent()
        if remaining is not None and remaining.get("agent_status") != "done":
            raise StorageRefusal("cleanup_adapter_failed", "Codex canary did not exit")
        return {"command_exit": completed.returncode, "exact_agent": True}


class HerdrBackendAdapter(_BaseAdapter):
    kind = "backend"

    def __init__(
        self,
        store: SQLiteStorage,
        identity: Mapping[str, Any],
        runner: CommandRunner,
        at: str,
    ) -> None:
        self.store = store
        self.identity = dict(identity)
        self.runner = runner
        self.at = at

    def _pane(self) -> Optional[dict[str, Any]]:
        value = _json_output(
            self.runner.run(
                ("herdr", "pane", "list", "--workspace", self.identity["workspace_id"])
            )
        )
        panes = value.get("result", {}).get("panes", [])
        if not isinstance(panes, list) or any(
            not isinstance(item, Mapping) for item in panes
        ):
            raise StorageRefusal(
                "cleanup_adapter_failed", "Herdr pane inventory is malformed"
            )
        return next((item for item in panes if item.get("pane_id") == self.identity["pane_id"]), None)

    def _runtime(self) -> Mapping[str, Any]:
        return runtime_cleanup_identity(
            self.store,
            self.identity["runtime_instance_id"],
            self.identity["pane_id"],
            self.identity["runtime_generation"],
        )

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        pane = self._pane()
        runtime = self._runtime()
        if pane is None and runtime["status"] == "closed":
            return dict(action["intended_state"])
        if pane is not None and pane.get("terminal_id") != self.identity["terminal_id"]:
            raise StorageRefusal("cleanup_identity_mismatch", "Herdr terminal identity changed")
        return dict(action["expected_identity"])

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        closed = self.store.close_runtime_for_cleanup(
            self.identity["runtime_instance_id"],
            self.identity["pane_id"],
            self.identity["runtime_generation"],
            self.at,
        )
        if self._pane() is not None:
            self.runner.run(("herdr", "pane", "close", self.identity["pane_id"]))
        return {"exact_pane": True, "runtime": closed}


class GitAdapter(_BaseAdapter):
    kind = "git"

    def __init__(self, identity: Mapping[str, Any], runner: CommandRunner) -> None:
        self.identity = dict(identity)
        self.runner = runner

    def _worktree_registered(self) -> bool:
        completed = self.runner.run(
            ("git", "-C", self.identity["repository"], "worktree", "list", "--porcelain")
        )
        blocks = completed.stdout.strip().split("\n\n") if completed.stdout.strip() else []
        expected = f"worktree {self.identity['worktree']}"
        return any(block.splitlines() and block.splitlines()[0] == expected for block in blocks)

    def _branch_deletion_mode(self) -> str:
        merged = self.runner.run(
            (
                "git",
                "-C",
                self.identity["repository"],
                "merge-base",
                "--is-ancestor",
                self.identity["head"],
                self.identity["base_ref"],
            ),
            allow_failure=True,
        )
        if merged.returncode == 0:
            return "merged-ancestor"
        head_tree = self.runner.run(
            (
                "git",
                "-C",
                self.identity["repository"],
                "rev-parse",
                f"{self.identity['head']}^{{tree}}",
            )
        ).stdout.strip()
        merge_tree = self.runner.run(
            (
                "git",
                "-C",
                self.identity["repository"],
                "rev-parse",
                f"{self.identity['merge_commit']}^{{tree}}",
            )
        ).stdout.strip()
        if not head_tree or head_tree != merge_tree:
            raise StorageRefusal(
                "cleanup_identity_mismatch",
                "Git branch is neither merged nor squash-tree equivalent",
            )
        return "squash-tree-equivalent"

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = action["action_kind"]
        if kind == "worktree_remove":
            expected = {
                key: self.identity[key]
                for key in ("repository", "worktree", "branch", "head")
            }
            if action["expected_identity"] != expected:
                raise StorageRefusal(
                    "cleanup_identity_mismatch", "Git worktree plan and adapter disagree"
                )
            exists = Path(self.identity["worktree"]).exists()
            registered = self._worktree_registered()
            if not exists and not registered:
                return dict(action["intended_state"])
            if not exists or not registered:
                raise StorageRefusal("cleanup_identity_mismatch", "Git worktree state is partial")
            status = self.runner.run(
                ("git", "-C", self.identity["worktree"], "status", "--porcelain")
            ).stdout
            head = self.runner.run(
                ("git", "-C", self.identity["worktree"], "rev-parse", "HEAD")
            ).stdout.strip()
            branch = self.runner.run(
                ("git", "-C", self.identity["worktree"], "branch", "--show-current")
            ).stdout.strip()
            if status or head != self.identity["head"] or branch != self.identity["branch"]:
                raise StorageRefusal("cleanup_identity_mismatch", "Git worktree proof changed")
        elif kind == "branch_delete":
            expected = {
                key: self.identity[key]
                for key in ("repository", "branch", "head", "base_ref", "merge_commit")
            }
            if action["expected_identity"] != expected:
                raise StorageRefusal(
                    "cleanup_identity_mismatch", "Git branch plan and adapter disagree"
                )
            ref = self.runner.run(
                (
                    "git",
                    "-C",
                    self.identity["repository"],
                    "for-each-ref",
                    "--format=%(objectname)",
                    f"refs/heads/{self.identity['branch']}",
                )
            )
            references = [line for line in ref.stdout.splitlines() if line]
            if not references:
                return dict(action["intended_state"])
            if references != [self.identity["head"]]:
                raise StorageRefusal("cleanup_identity_mismatch", "Git branch head changed")
            self._branch_deletion_mode()
        else:
            raise StorageRefusal("cleanup_action_unsupported", "Git cleanup action is unsupported")
        return dict(action["expected_identity"])

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        if action["action_kind"] == "worktree_remove":
            self.runner.run(
                (
                    "git",
                    "-C",
                    self.identity["repository"],
                    "worktree",
                    "remove",
                    self.identity["worktree"],
                )
            )
        elif action["action_kind"] == "branch_delete":
            mode = self._branch_deletion_mode()
            self.runner.run(
                (
                    "git",
                    "-C",
                    self.identity["repository"],
                    "update-ref",
                    "-d",
                    f"refs/heads/{self.identity['branch']}",
                    self.identity["head"],
                )
            )
            return {
                "exact_git_target": True,
                "action": action["action_kind"],
                "deletion_proof": mode,
            }
        return {"exact_git_target": True, "action": action["action_kind"]}


class CallsignAdapter(_BaseAdapter):
    kind = "callsign"

    def __init__(self, store: SQLiteStorage, identity: Mapping[str, Any], at: str) -> None:
        self.store = store
        self.identity = dict(identity)
        self.at = at

    def _assignment(self) -> Mapping[str, Any]:
        row = self.store.connection.execute(
            "SELECT callsign_assignment_id,callsign,runtime_instance_id,state,version,release_receipt_digest FROM callsign_assignments WHERE callsign_assignment_id=?",
            (self.identity["assignment_id"],),
        ).fetchone()
        if row is None:
            raise StorageRefusal("cleanup_identity_mismatch", "callsign assignment disappeared")
        return dict(row)

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        assignment = self._assignment()
        if assignment["callsign"] != self.identity["callsign"]:
            raise StorageRefusal(
                "cleanup_identity_mismatch", "callsign assignment identity changed"
            )
        if assignment["state"] == "released":
            expected_digest = cleanup_action_digest(action)
            if (
                assignment["version"] != self.identity["expected_version"] + 1
                or assignment["release_receipt_digest"] != expected_digest
            ):
                raise StorageRefusal(
                    "cleanup_identity_mismatch", "callsign release history changed"
                )
            return dict(action["intended_state"])
        observed = {
            "assignment_id": assignment["callsign_assignment_id"],
            "callsign": assignment["callsign"],
            "expected_version": assignment["version"],
        }
        if observed != action["expected_identity"] or assignment["state"] != "active":
            raise StorageRefusal("cleanup_identity_mismatch", "callsign assignment identity changed")
        return observed

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        digest = cleanup_action_digest(action)
        result = self.store.release_callsign(
            self.identity["assignment_id"],
            self.identity["expected_version"],
            digest,
            self.at,
        )
        return {"assignment_id": result["assignment_id"], "state": result["state"]}


def canary_cleanup_registry(
    store: SQLiteStorage,
    config_value: Mapping[str, Any],
    *,
    at: str,
    runner: Optional[CommandRunner] = None,
) -> CleanupAdapterRegistry:
    config = validate_canary_config(config_value)
    command_runner = runner or SubprocessRunner()
    registry = CleanupAdapterRegistry()
    registry.register(ArchiveAdapter(Path(config["archive_path"])))
    registry.register(HerdrHarnessAdapter(config["herdr"], command_runner))
    registry.register(HerdrBackendAdapter(store, config["herdr"], command_runner, at))
    registry.register(GitAdapter(config["git"], command_runner))
    registry.register(CallsignAdapter(store, config["callsign"], at))
    return registry
