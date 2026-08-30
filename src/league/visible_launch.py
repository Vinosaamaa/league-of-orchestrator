"""One recoverable SQLite-backed visible Champion launch through Herdr/Codex."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .request_services import AssignmentService, AssignmentSpec, LaunchAdapterError
from .issue_first import IssueVerifier
from .storage import Storage, StorageRefusal


MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAX_CONTEXT_BYTES = 4096
SAFE_ROUTING_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_EFFORT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
THREAD_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TASK_LABEL_WORD = re.compile(r"[A-Za-z0-9]+")
TASK_LABEL_NOISE = {
    "a",
    "across",
    "add",
    "after",
    "and",
    "champion",
    "create",
    "exact",
    "for",
    "fix",
    "full",
    "implement",
    "issue",
    "league",
    "make",
    "one",
    "research",
    "repair",
    "restore",
    "preserve",
    "the",
    "through",
}


class CommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    """Run one bounded Herdr command without exposing unbounded terminal output."""

    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.run(
                    list(arguments),
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_seconds,
                    check=False,
                )
                outputs: list[str] = []
                for stream in (stdout, stderr):
                    size = stream.tell()
                    if size > MAX_COMMAND_OUTPUT_BYTES:
                        raise StorageRefusal(
                            "launch_adapter_output_too_large",
                            "Herdr launch adapter output exceeded its bound",
                        )
                    stream.seek(0)
                    outputs.append(stream.read().decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise StorageRefusal(
                "launch_adapter_failed", "Herdr launch adapter output was not UTF-8"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageRefusal(
                "launch_adapter_failed", "Herdr launch adapter command did not complete"
            ) from exc
        return subprocess.CompletedProcess(
            list(arguments), process.returncode, outputs[0], outputs[1]
        )


@dataclass(frozen=True)
class VisibleLaunchOptions:
    workspace_id: str
    task_label: str
    model: str
    effort: str
    league_command: str
    state_root: str
    startup_timeout_ms: int = 120_000


class _Clock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def after(self, seconds: int) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")


class _AssignmentIds:
    def __init__(self, assignment_id: str) -> None:
        self.assignment_id = assignment_id

    def new(self, kind: str) -> str:
        return f"{kind}:{self.assignment_id}:active"


def derived_assignment_id(request_id: str, task_id: str) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"league.assignment\0{request_id}\0{task_id}")
    return f"assignment:{value}"


def derived_champion_agent_id(assignment_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"league.champion\0{assignment_id}"))


def derive_task_label(task_summary: str) -> str:
    if (
        not isinstance(task_summary, str)
        or not task_summary.strip()
        or task_summary.strip() != task_summary
        or any(character in task_summary for character in "\r\n\0")
    ):
        raise StorageRefusal(
            "launch_scope_invalid", "Champion task summary cannot derive a display label"
        )
    words = TASK_LABEL_WORD.findall(task_summary.replace("-", " "))
    lowered = [word.lower() for word in words]
    proper = [
        word
        for index, word in enumerate(words)
        if index > 0
        and (word.isupper() or word[:1].isupper())
        and word.lower() not in TASK_LABEL_NOISE
    ]
    if len(proper) >= 2:
        selected = proper[:2]
    else:
        meaningful = [
            word
            for word in words
            if word.lower() not in TASK_LABEL_NOISE and not word.isdigit()
        ]
        repair_actions = {"fix", "preserve", "repair", "restore"}
        if len(proper) == 1:
            proper_index = words.index(proper[0])
            following = [
                word
                for word in words[proper_index + 1 :]
                if word.lower() not in TASK_LABEL_NOISE and not word.isdigit()
            ]
            selected = [proper[0], following[0] if following else "Work"]
        elif lowered and lowered[0] in repair_actions and meaningful:
            selected = [meaningful[0], "Repair"]
        elif len(meaningful) >= 2:
            selected = meaningful[-2:]
        elif meaningful:
            selected = [meaningful[0], "Work"]
        else:
            selected = ["Scoped", "Work"]
    normalized = [
        "Meter" if word.lower() == "metering" else word for word in selected
    ]
    label = " ".join(
        word if word.isupper() else word[:1].upper() + word[1:]
        for word in normalized
    )
    if len(label.split()) != 2 or len(label) > 48:
        raise StorageRefusal(
            "launch_scope_invalid", "derived Champion display task must be two words"
        )
    return label


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _result_object(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "launch_adapter_failed", f"{label} returned malformed JSON"
        ) from exc
    if completed.returncode != 0 or not isinstance(value, dict) or "error" in value:
        raise StorageRefusal("launch_adapter_failed", f"{label} refused or failed")
    result = value.get("result")
    if not isinstance(result, dict):
        raise StorageRefusal(
            "launch_adapter_failed", f"{label} returned no result object"
        )
    return result


def _agent_object(result: Mapping[str, Any]) -> dict[str, Any]:
    value: Any = result.get("agent", result)
    if not isinstance(value, Mapping):
        raise StorageRefusal(
            "launch_identity_unverified", "Herdr returned no exact agent identity"
        )
    return dict(value)


def _session_id(agent: Mapping[str, Any]) -> str | None:
    session = agent.get("agent_session")
    value = session.get("value") if isinstance(session, Mapping) else None
    return str(value) if isinstance(value, str) else None


def _session_source(agent: Mapping[str, Any]) -> str | None:
    session = agent.get("agent_session")
    value = session.get("source") if isinstance(session, Mapping) else None
    return str(value) if isinstance(value, str) and value else None


def _validate_options(options: VisibleLaunchOptions) -> None:
    if not re.fullmatch(r"w[0-9A-Za-z]+", options.workspace_id):
        raise StorageRefusal("launch_scope_invalid", "Herdr workspace identity is invalid")
    words = options.task_label.split()
    if (
        not words
        or len(words) > 2
        or len(options.task_label) > 48
        or options.task_label.strip() != options.task_label
        or any(character in options.task_label for character in "\r\n\0")
    ):
        raise StorageRefusal(
            "launch_scope_invalid", "Champion display task must contain one or two words"
        )
    if not SAFE_MODEL.fullmatch(options.model) or not SAFE_EFFORT.fullmatch(options.effort):
        raise StorageRefusal("launch_scope_invalid", "Codex model or effort is invalid")
    state_root = Path(options.state_root)
    league_command = Path(options.league_command)
    if (
        not state_root.is_absolute()
        or state_root == Path("/")
        or not state_root.is_dir()
        or state_root.is_symlink()
        or not league_command.is_absolute()
        or not league_command.is_file()
    ):
        raise StorageRefusal(
            "launch_scope_invalid", "League command and canonical state root must be exact"
        )
    if not 1_000 <= options.startup_timeout_ms <= 300_000:
        raise StorageRefusal("launch_scope_invalid", "Herdr startup timeout is invalid")


def _codex_trust_root(worktree: Path) -> Path:
    marker = worktree / ".git"
    if marker.is_dir() and not marker.is_symlink():
        return worktree.resolve()
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 4096:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree has no exact Git identity"
        )
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch Git identity could not be read"
        ) from exc
    if not line.startswith("gitdir: ") or "\n" in line or "\0" in line:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch Git identity is malformed"
        )
    supplied = Path(line.removeprefix("gitdir: "))
    git_dir = (
        supplied if supplied.is_absolute() else marker.parent / supplied
    ).resolve(strict=False)
    common_dir = git_dir.parent.parent
    repository = common_dir.parent
    backref = git_dir / "gitdir"
    try:
        recorded_marker = Path(backref.read_text(encoding="utf-8").strip()).resolve()
    except (OSError, UnicodeError) as exc:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree registration is incomplete"
        ) from exc
    exact = (
        git_dir.is_dir()
        and git_dir.parent.name == "worktrees"
        and common_dir.name == ".git"
        and common_dir.is_dir()
        and not common_dir.is_symlink()
        and repository.is_dir()
        and not repository.is_symlink()
        and recorded_marker == marker.resolve()
    )
    if not exact:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree registration is not exact"
        )
    return repository.resolve()


class HerdrCodexLaunchAdapter:
    """Own one exact new Herdr tab and its generated Codex thread identity."""

    def __init__(
        self,
        options: VisibleLaunchOptions,
        runner: CommandRunner | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        resume_thread_id: str | None = None,
    ) -> None:
        _validate_options(options)
        self.options = options
        self.runner = runner or SubprocessRunner()
        self.environment = dict(environment or os.environ)
        if resume_thread_id is not None and THREAD_UUID.fullmatch(resume_thread_id) is None:
            raise StorageRefusal(
                "thread_identity_missing", "Codex resume requires one exact archived thread UUID"
            )
        self.resume_thread_id = resume_thread_id
        self._created: dict[str, str] | None = None
        self._receipt: dict[str, Any] | None = None
        if self.environment.get("HERDR_ENV") != "1":
            raise StorageRefusal(
                "launch_scope_invalid", "visible Herdr launch requires the current Herdr session"
            )
        if self.environment.get("HERDR_WORKSPACE_ID") != options.workspace_id:
            raise StorageRefusal(
                "launch_scope_invalid", "requested Herdr workspace is not the current workspace"
            )

    def _command(
        self,
        arguments: Sequence[str],
        label: str,
        *,
        timeout_seconds: int = 30,
        allow_silent_success: bool = False,
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
        completed = self.runner.run(arguments, timeout_seconds=timeout_seconds)
        if (
            allow_silent_success
            and completed.returncode == 0
            and completed.stdout == ""
            and completed.stderr == ""
        ):
            return {}, completed
        return _result_object(completed, label), completed

    def _agent_list(self) -> list[dict[str, Any]]:
        result, _ = self._command(("herdr", "agent", "list"), "Herdr agent list")
        agents = result.get("agents")
        if not isinstance(agents, list) or any(not isinstance(item, Mapping) for item in agents):
            raise StorageRefusal(
                "launch_identity_unverified", "Herdr agent inventory is malformed"
            )
        return [dict(item) for item in agents]

    def _get_agent(self, routing_name: str) -> dict[str, Any]:
        result, _ = self._command(
            ("herdr", "agent", "get", routing_name), "Herdr agent inspection"
        )
        return _agent_object(result)

    def _matching_agent(self, routing_name: str) -> dict[str, Any] | None:
        matches = [item for item in self._agent_list() if item.get("name") == routing_name]
        if len(matches) > 1:
            raise StorageRefusal(
                "launch_identity_conflict", "Herdr routing name is ambiguous"
            )
        return matches[0] if matches else None

    def _report_verified_resume_session(
        self,
        spec: AssignmentSpec,
        pane_id: str,
        state_change_seq: int,
    ) -> bool:
        """Publish Herdr session metadata only after the exact Codex resume process exists."""

        result, _ = self._command(
            ("herdr", "pane", "process-info", "--pane", pane_id),
            "Herdr Codex resume process inspection",
        )
        process_info = result.get("process_info")
        processes = (
            process_info.get("foreground_processes")
            if isinstance(process_info, Mapping)
            else None
        )
        if not isinstance(processes, list) or any(
            not isinstance(item, Mapping) for item in processes
        ):
            raise StorageRefusal(
                "launch_identity_unverified",
                "Herdr resume process inventory is malformed",
            )
        codex_processes = [item for item in processes if item.get("name") == "codex"]
        if not codex_processes:
            return False
        worktree = str(Path(spec.worktree).resolve())
        expected_tail = [
            "resume",
            "--model",
            self.options.model,
            "--config",
            f'model_reasoning_effort="{self.options.effort}"',
            "--add-dir",
            self.options.state_root,
            "--cd",
            worktree,
            str(self.resume_thread_id),
        ]
        exact = []
        for process in codex_processes:
            arguments = process.get("argv")
            if not isinstance(arguments, list) or any(
                not isinstance(value, str) for value in arguments
            ):
                continue
            try:
                resume_index = arguments.index("resume")
            except ValueError:
                continue
            if (
                arguments[resume_index:] == expected_tail
                and process.get("cwd") == worktree
            ):
                exact.append(process)
        if len(codex_processes) != 1 or len(exact) != 1:
            raise StorageRefusal(
                "thread_identity_ambiguous",
                "foreground Codex process is not the exact archived resume and binding",
            )
        self._command(
            (
                "herdr",
                "pane",
                "report-agent-session",
                pane_id,
                "--source",
                "herdr:codex",
                "--agent",
                "codex",
                "--agent-session-id",
                str(self.resume_thread_id),
                "--session-start-source",
                "codex-resume",
                "--seq",
                str(state_change_seq + 1),
            ),
            "Herdr Codex resume session report",
            allow_silent_success=True,
        )
        return True

    def _verify_agent(
        self,
        agent: Mapping[str, Any],
        spec: AssignmentSpec,
        pane_id: str,
        terminal_id: str,
    ) -> dict[str, str]:
        thread_id = _session_id(agent)
        state_change_seq = agent.get("state_change_seq")
        expected_cwd = str(Path(spec.worktree).resolve())
        exact = (
            agent.get("name") == str(spec.callsign).lower()
            and agent.get("agent") == "codex"
            and agent.get("workspace_id") == self.options.workspace_id
            and agent.get("pane_id") == pane_id
            and agent.get("terminal_id") == terminal_id
            and agent.get("cwd") == expected_cwd
            and agent.get("foreground_cwd") == expected_cwd
            and isinstance(thread_id, str)
            and THREAD_UUID.fullmatch(thread_id) is not None
            and isinstance(state_change_seq, int)
            and state_change_seq >= 0
        )
        if not exact:
            raise StorageRefusal(
                "launch_identity_unverified",
                "Herdr/Codex identity, endpoint, generated thread, or worktree did not verify",
            )
        return {
            "pane_id": pane_id,
            "terminal_id": terminal_id,
            "thread_id": thread_id,
            "routing_name": str(spec.callsign).lower(),
            "state_change_seq": str(state_change_seq),
        }

    def _await_initial_session(
        self,
        agent: Mapping[str, Any],
        spec: AssignmentSpec,
        pane_id: str,
        terminal_id: str,
    ) -> dict[str, Any]:
        """Persist a just-started Codex thread before assignment activation."""
        if _session_id(agent) is not None:
            return dict(agent)
        expected_cwd = str(Path(spec.worktree).resolve())
        exact_private_launch = (
            agent.get("name") == str(spec.callsign).lower()
            and agent.get("agent") == "codex"
            and agent.get("workspace_id") == self.options.workspace_id
            and agent.get("pane_id") == pane_id
            and agent.get("terminal_id") == terminal_id
            and agent.get("cwd") == expected_cwd
            and agent.get("foreground_cwd") == expected_cwd
            and agent.get("interactive_ready") is True
        )
        if not exact_private_launch:
            raise StorageRefusal(
                "launch_identity_unverified",
                "new Codex endpoint did not expose one exact pre-context session identity",
            )
        if self.resume_thread_id is not None:
            session_reported = False
            for _ in range(120):
                published = self._get_agent(str(spec.callsign).lower())
                if _session_id(published) == self.resume_thread_id:
                    return published
                if (
                    published.get("pane_id") != pane_id
                    or published.get("terminal_id") != terminal_id
                    or published.get("cwd") != expected_cwd
                    or published.get("foreground_cwd") != expected_cwd
                ):
                    break
                if not session_reported:
                    state_change_seq = published.get("state_change_seq")
                    if not isinstance(state_change_seq, int) or state_change_seq < 0:
                        raise StorageRefusal(
                            "launch_identity_unverified",
                            "Herdr resume endpoint has no exact metadata sequence",
                        )
                    session_reported = self._report_verified_resume_session(
                        spec, pane_id, state_change_seq
                    )
                time.sleep(0.1)
            raise StorageRefusal(
                "thread_identity_ambiguous",
                "Codex did not publish the exact archived thread after resume",
            )
        nonce = _sha256(spec.assignment_id.encode("utf-8"))[:12]
        self._command(
            (
                "herdr",
                "agent",
                "prompt",
                str(spec.callsign).lower(),
                f"League launch identity handshake {nonce} only. "
                "Do not inspect or change files. Reply exactly READY.",
            ),
            "Herdr Codex identity handshake",
        )
        for _ in range(120):
            published = self._get_agent(str(spec.callsign).lower())
            session_id = _session_id(published)
            if isinstance(session_id, str) and THREAD_UUID.fullmatch(session_id):
                return published
            if (
                published.get("pane_id") != pane_id
                or published.get("terminal_id") != terminal_id
                or published.get("cwd") != expected_cwd
                or published.get("foreground_cwd") != expected_cwd
            ):
                break
            time.sleep(0.1)
        raise StorageRefusal(
            "launch_identity_unverified",
            "Codex did not publish one authoritative session after the launch handshake",
        )

    def _title_owner(self, assignment_id: str) -> str:
        return _sha256(assignment_id.encode("utf-8"))[:16]

    def _title_source(self, assignment_id: str) -> str:
        return "league-launch-" + self._title_owner(assignment_id)

    def _report_title(
        self,
        *,
        pane_id: str,
        assignment_id: str,
        callsign: str,
        applies_to_source: str,
        sequence: int,
    ) -> None:
        title = f"{callsign} · {self.options.task_label}"
        self._command(
            (
                "herdr",
                "pane",
                "report-metadata",
                pane_id,
                "--source",
                self._title_source(assignment_id),
                "--applies-to-source",
                applies_to_source,
                "--agent",
                "codex",
                "--display-agent",
                "codex",
                "--title",
                title,
                "--token",
                f"sidebar_name={callsign}",
                "--token",
                f"task_label={self.options.task_label}",
                "--token",
                f"thread_title={title}",
                "--token",
                f"launch_title_owner={self._title_owner(assignment_id)}",
                "--token",
                f"launch_title_source={self._title_source(assignment_id)}",
                "--token",
                f"launch_title_applies_to={applies_to_source}",
                "--seq",
                str(sequence),
            ),
            "Herdr Champion metadata",
            allow_silent_success=True,
        )

    def _title_exact(
        self, agent: Mapping[str, Any], callsign: str, assignment_id: str
    ) -> bool:
        expected = f"{callsign} · {self.options.task_label}"
        terminal_titles = {
            agent.get("terminal_title"),
            agent.get("terminal_title_stripped"),
        }
        tokens = agent.get("tokens")
        return bool(
            isinstance(tokens, Mapping)
            and agent.get("metadata_source") == self._title_source(assignment_id)
            and tokens.get("sidebar_name") == callsign
            and tokens.get("task_label") == self.options.task_label
            and tokens.get("thread_title") == expected
            and tokens.get("launch_title_owner")
            == self._title_owner(assignment_id)
            and tokens.get("launch_title_source")
            == self._title_source(assignment_id)
            and tokens.get("launch_title_applies_to") == _session_source(agent)
            and terminal_titles <= {expected, f"{expected} | codex"}
        )

    def _verify_title(
        self,
        routing_name: str,
        callsign: str,
        assignment_id: str,
        *,
        stable_observations: int,
    ) -> dict[str, Any]:
        prior_key: tuple[str, int] | None = None
        consecutive = 0
        for _ in range(50):
            agent = self._get_agent(routing_name)
            if self._title_exact(agent, callsign, assignment_id):
                source = agent.get("metadata_source")
                applies_to_source = _session_source(agent)
                sequence = agent.get("state_change_seq")
                if (
                    isinstance(source, str)
                    and isinstance(applies_to_source, str)
                    and isinstance(sequence, int)
                ):
                    key = (source, sequence)
                    consecutive = consecutive + 1 if key == prior_key else 1
                    prior_key = key
                    if consecutive >= stable_observations:
                        expected = f"{callsign} · {self.options.task_label}"
                        return {
                            "source": source,
                            "applies_to_source": applies_to_source,
                            "state_change_seq": sequence,
                            "sidebar_name": callsign,
                            "task_label": self.options.task_label,
                            "thread_title": expected,
                            "terminal_title": expected,
                        }
            else:
                prior_key = None
                consecutive = 0
            time.sleep(0.1)
        raise StorageRefusal(
            "launch_title_unverified",
            "Champion sidebar, thread, and terminal title did not verify",
        )

    def _stabilize_title_after_context(
        self, receipt: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self._created is None:
            raise StorageRefusal(
                "launch_title_restore_refused",
                "Champion title restoration has no owned launch endpoint",
            )
        routing_name = str(receipt.get("routing_name", ""))
        callsign = str(receipt.get("callsign", ""))
        assignment_id = str(receipt.get("assignment_id", ""))
        agent = self._get_agent(routing_name)
        tokens = agent.get("tokens")
        observed_thread = _session_id(agent)
        applies_to_source = _session_source(agent)
        presentation_source = agent.get("metadata_source")
        sequence = agent.get("state_change_seq")
        owned = bool(
            agent.get("name") == routing_name
            and agent.get("agent") == "codex"
            and agent.get("pane_id") == self._created.get("pane_id")
            and agent.get("terminal_id") == self._created.get("terminal_id")
            and agent.get("cwd") == self._created.get("worktree")
            and agent.get("foreground_cwd") == self._created.get("worktree")
            and observed_thread == receipt.get("thread_id")
            and isinstance(applies_to_source, str)
            and isinstance(presentation_source, str)
            and presentation_source
            in {applies_to_source, self._title_source(assignment_id)}
            and isinstance(sequence, int)
            and isinstance(tokens, Mapping)
            and tokens.get("launch_title_owner")
            == self._title_owner(assignment_id)
            and tokens.get("launch_title_source")
            == self._title_source(assignment_id)
            and tokens.get("launch_title_applies_to") == applies_to_source
        )
        if not owned:
            raise StorageRefusal(
                "launch_title_restore_refused",
                "Champion display metadata changed outside the owned launch transaction",
            )
        if not self._title_exact(agent, callsign, assignment_id):
            self._report_title(
                pane_id=str(self._created["pane_id"]),
                assignment_id=assignment_id,
                callsign=callsign,
                applies_to_source=str(applies_to_source),
                sequence=int(sequence) + 1,
            )
        observation = self._verify_title(
            routing_name,
            callsign,
            assignment_id,
            stable_observations=2,
        )
        self._created["state_change_seq"] = str(observation["state_change_seq"])
        return observation

    def verify_active_title(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        routing_name = str(receipt.get("routing_name", ""))
        agent = self._get_agent(routing_name)
        terminal_id = agent.get("terminal_id")
        thread_id = _session_id(agent)
        worktree = str(Path(str(receipt.get("worktree", ""))).resolve())
        generation = (
            "herdr:"
            + _sha256(f"{terminal_id}\0{thread_id}".encode("utf-8"))[:24]
            if isinstance(terminal_id, str) and isinstance(thread_id, str)
            else ""
        )
        exact = bool(
            agent.get("name") == routing_name
            and agent.get("agent") == "codex"
            and agent.get("pane_id") == receipt.get("endpoint")
            and agent.get("cwd") == worktree
            and agent.get("foreground_cwd") == worktree
            and thread_id == receipt.get("thread_id")
            and generation == receipt.get("runtime_generation")
            and isinstance(agent.get("state_change_seq"), int)
        )
        if not exact:
            raise StorageRefusal(
                "launch_title_restore_refused",
                "active Champion endpoint no longer matches the launch receipt",
            )
        self._created = {
            "pane_id": str(receipt["endpoint"]),
            "terminal_id": str(terminal_id),
            "thread_id": str(thread_id),
            "routing_name": routing_name,
            "worktree": worktree,
            "state_change_seq": str(agent["state_change_seq"]),
        }
        return self._stabilize_title_after_context(receipt)

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        worktree = Path(spec.worktree)
        if (
            not worktree.is_absolute()
            or not worktree.is_dir()
            or worktree.is_symlink()
            or spec.callsign is None
        ):
            raise LaunchAdapterError("invalid_launch_worktree")
        _codex_trust_root(worktree)
        routing_name = str(spec.callsign).lower()
        if not SAFE_ROUTING_NAME.fullmatch(routing_name):
            raise LaunchAdapterError("invalid_launch_routing_name")
        if self._matching_agent(routing_name) is not None:
            raise LaunchAdapterError("launch_routing_conflict")
        try:
            result, _ = self._command(
                (
                    "herdr",
                    "tab",
                    "create",
                    "--workspace",
                    self.options.workspace_id,
                    "--cwd",
                    str(worktree.resolve()),
                    "--label",
                    f"{spec.callsign} · {self.options.task_label}",
                    "--no-focus",
                ),
                "Herdr tab creation",
            )
            tab = result.get("tab")
            pane = result.get("root_pane")
            if not isinstance(tab, Mapping) or not isinstance(pane, Mapping):
                raise StorageRefusal(
                    "launch_identity_unverified", "Herdr tab receipt is incomplete"
                )
            tab_id = tab.get("tab_id")
            pane_id = pane.get("pane_id")
            terminal_id = pane.get("terminal_id")
            if not all(isinstance(value, str) and value for value in (tab_id, pane_id, terminal_id)):
                raise StorageRefusal(
                    "launch_identity_unverified", "Herdr endpoint receipt is incomplete"
                )
            self._created = {
                "tab_id": str(tab_id),
                "pane_id": str(pane_id),
                "terminal_id": str(terminal_id),
                "routing_name": routing_name,
                "worktree": str(worktree.resolve()),
            }
            codex_arguments = [
                "--model",
                self.options.model,
                "--config",
                f'model_reasoning_effort="{self.options.effort}"',
                "--add-dir",
                self.options.state_root,
            ]
            if self.resume_thread_id is not None:
                codex_arguments = [
                    "resume",
                    *codex_arguments,
                    "--cd",
                    str(worktree.resolve()),
                    self.resume_thread_id,
                ]
            self._command(
                (
                    "herdr",
                    "agent",
                    "start",
                    routing_name,
                    "--kind",
                    "codex",
                    "--pane",
                    str(pane_id),
                    "--timeout",
                    str(self.options.startup_timeout_ms),
                    "--",
                    *codex_arguments,
                ),
                "Herdr Codex start",
                timeout_seconds=(self.options.startup_timeout_ms // 1000) + 10,
            )
            agent = self._await_initial_session(
                self._get_agent(routing_name), spec, str(pane_id), str(terminal_id)
            )
            identity = self._verify_agent(
                agent, spec, str(pane_id), str(terminal_id)
            )
            self._created.update(identity)
            applies_to_source = _session_source(agent)
            observed_sequence = agent.get("state_change_seq")
            if not isinstance(applies_to_source, str) or not isinstance(
                observed_sequence, int
            ):
                raise StorageRefusal(
                    "launch_title_unverified",
                    "Champion metadata authority source or sequence is missing",
                )
            self._report_title(
                pane_id=str(pane_id),
                assignment_id=spec.assignment_id,
                callsign=str(spec.callsign),
                applies_to_source=applies_to_source,
                sequence=observed_sequence + 1,
            )
            observation = self._verify_title(
                routing_name,
                str(spec.callsign),
                spec.assignment_id,
                stable_observations=1,
            )
            self._created["state_change_seq"] = str(
                observation["state_change_seq"]
            )
        except LaunchAdapterError:
            raise
        except Exception as exc:
            cleanup_proven = self.cleanup(None)
            failure_class = (
                exc.code if isinstance(exc, StorageRefusal) else "launch_adapter_failure"
            )
            raise LaunchAdapterError(
                failure_class,
                cleanup_required=self._created is not None,
                cleanup_proven=cleanup_proven,
            ) from exc
        assert self._created is not None
        runtime_generation = "herdr:" + _sha256(
            (
                self._created["terminal_id"]
                + "\0"
                + self._created["thread_id"]
            ).encode("utf-8")
        )[:24]
        self._receipt = {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            "thread_id": self._created["thread_id"],
            "endpoint": self._created["pane_id"],
            "runtime_generation": runtime_generation,
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "routing_name": routing_name,
            "display_agent": "codex",
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }
        return dict(self._receipt)

    def deliver_context(
        self, receipt: Mapping[str, Any], context: str
    ) -> dict[str, Any]:
        body = context.encode("utf-8")
        if not body or len(body) > MAX_CONTEXT_BYTES:
            raise LaunchAdapterError("launch_context_invalid", cleanup_required=True)
        routing_name = str(receipt.get("routing_name", ""))
        agent = self._get_agent(routing_name)
        observed_thread = _session_id(agent)
        exact_generation = (
            self._created is not None
            and agent.get("pane_id") == self._created.get("pane_id")
            and agent.get("terminal_id") == self._created.get("terminal_id")
            and agent.get("cwd") == self._created.get("worktree")
            and agent.get("foreground_cwd") == self._created.get("worktree")
            and str(agent.get("state_change_seq"))
            == self._created.get("state_change_seq")
        )
        if (
            agent.get("pane_id") != receipt.get("endpoint")
            or agent.get("agent") != "codex"
            or (
                observed_thread != receipt.get("thread_id")
                and not (observed_thread is None and exact_generation)
            )
        ):
            raise LaunchAdapterError("launch_context_identity_mismatch", cleanup_required=True)
        try:
            _, completed = self._command(
                (
                    "herdr",
                    "agent",
                    "prompt",
                    routing_name,
                    context,
                    "--wait",
                    "--timeout",
                    "30000",
                ),
                "Herdr Champion context delivery",
                timeout_seconds=35,
            )
        except StorageRefusal as exc:
            raise LaunchAdapterError(
                "launch_context_delivery_failed", cleanup_required=True
            ) from exc
        try:
            display_receipt = self._stabilize_title_after_context(receipt)
        except StorageRefusal as exc:
            raise LaunchAdapterError(exc.code, cleanup_required=True) from exc
        return {
            "context_sha256": _sha256(body),
            "bytes": len(body),
            "effect_sha256": _sha256(
                _stable_json(
                    {
                        "prompt_effect_sha256": _sha256(
                            completed.stdout.encode("utf-8")
                        ),
                        "display_receipt": display_receipt,
                    }
                ).encode("utf-8")
            ),
            "display_receipt": display_receipt,
        }

    def cleanup(self, receipt: Mapping[str, Any] | None) -> bool:
        identity = dict(self._created or {})
        if receipt is not None:
            identity.setdefault("routing_name", str(receipt.get("routing_name", "")))
            identity.setdefault("pane_id", str(receipt.get("endpoint", "")))
            identity.setdefault("thread_id", str(receipt.get("thread_id", "")))
        routing_name = identity.get("routing_name")
        pane_id = identity.get("pane_id")
        if not routing_name or not pane_id:
            return self._created is None
        try:
            agent = self._matching_agent(routing_name)
            if agent is not None:
                expected_thread = identity.get("thread_id")
                if agent.get("pane_id") != pane_id:
                    return False
                observed_thread = _session_id(agent)
                expected_sequence = identity.get("state_change_seq")
                exact_endpoint = (
                    agent.get("terminal_id") == identity.get("terminal_id")
                    and agent.get("cwd") == identity.get("worktree")
                    and agent.get("foreground_cwd") == identity.get("worktree")
                    and (
                        expected_sequence is None
                        or str(agent.get("state_change_seq")) == expected_sequence
                    )
                )
                if expected_thread or observed_thread is not None:
                    if (
                        not exact_endpoint
                        or (
                            expected_thread
                            and observed_thread is not None
                            and observed_thread != expected_thread
                        )
                    ):
                        return False
                    completed = self.runner.run(
                        (
                            "herdr",
                            "agent",
                            "prompt",
                            routing_name,
                            "/exit",
                            "--wait",
                            "--timeout",
                            "30000",
                        ),
                        timeout_seconds=35,
                    )
                    if completed.returncode != 0:
                        return False
                else:
                    pending_exact = (
                        agent.get("launch_pending") is True
                        and observed_thread is None
                        and exact_endpoint
                    )
                    if not pending_exact:
                        return False
            completed = self.runner.run(
                ("herdr", "pane", "close", pane_id), timeout_seconds=30
            )
            if completed.returncode != 0:
                return False
            return self._matching_agent(routing_name) is None
        except Exception:
            return False


def render_launch_context(
    spec: AssignmentSpec,
    receipt: Mapping[str, Any],
    options: VisibleLaunchOptions,
) -> str:
    text = "\n".join(
        (
            f"League assignment: {spec.task_summary}",
            f"Callsign: {spec.callsign}",
            f"Agent ID: {spec.champion_agent_id}",
            f"Task ID: {spec.task_id}",
            f"Assignment ID: {spec.assignment_id}",
            f"Runtime ID: {receipt['runtime_instance_id']}",
            f"Shotcaller agent ID: {spec.coordinator_agent_id}",
            f"Repository: {spec.repository}",
            f"Issue: {spec.issue}",
            f"Branch: {spec.branch}",
            f"Worktree: {spec.worktree}",
            f"League command: {options.league_command}",
            f"League state root: {options.state_root}",
            "Use only the stable League SQLite commands for status, task transitions, delivery, and cleanup.",
            "Use league assign run for launch and canonical task/cleanup commands for lifecycle writes.",
            "First record a working task transition with the exact runtime ID, then perform only this bounded assignment.",
        )
    )
    if len(text.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise StorageRefusal(
            "launch_context_too_large", "bounded League assignment context exceeds 4096 bytes"
        )
    return text


class VisibleChampionLaunchService:
    """Compose canonical assignment phases, real launch, context, and exact failure cleanup."""

    def __init__(
        self,
        store: Storage,
        adapter: HerdrCodexLaunchAdapter,
        options: VisibleLaunchOptions,
        clock: Any | None = None,
        issue_verifier: IssueVerifier | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.options = options
        self.clock = clock or _Clock()
        self.issue_verifier = issue_verifier or getattr(adapter, "issue_verifier", None)

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        if self.issue_verifier is None:
            raise StorageRefusal(
                "issue_verification_required",
                "visible repository work requires issue verification before launch",
            )
        issue_receipt = self.issue_verifier.verify(spec, self.clock.now())
        spec = replace(spec, issue_receipt=issue_receipt)
        prior = None
        try:
            prior = self.store.assignment_launch_context(spec.assignment_id)
        except StorageRefusal as exc:
            if exc.code != "assignment_unknown":
                raise
        if prior is not None and prior["context_delivery"] is not None:
            prepared = AssignmentService(
                self.store,
                self.adapter,
                self.clock,
                _AssignmentIds(spec.assignment_id),
            ).assign(spec)
            if prepared["state"] != "active":
                raise StorageRefusal(
                    "assignment_conflict",
                    "delivered assignment retry is no longer active",
                )
            receipt = prior["acceptance_receipt"]
            if receipt is None:
                raise StorageRefusal(
                    "assignment_incomplete", "context receipt has no activation identity"
                )
            context = render_launch_context(
                AssignmentSpec(**{**vars(spec), "callsign": prior["callsign"]}),
                receipt,
                self.options,
            )
            digest = _sha256(context.encode("utf-8"))
            if prior["context_delivery"]["context_sha256"] != digest:
                raise StorageRefusal(
                    "assignment_context_conflict",
                    "assignment retry produced different bounded context",
                )
            try:
                observation = self.adapter.verify_active_title(receipt)
            except StorageRefusal as exc:
                return self.store.fail_assignment_title_validation(
                    spec.assignment_id,
                    prior["version"],
                    exc.code,
                    f"event:{spec.assignment_id}:title-validation-failed",
                    f"outbox:{spec.assignment_id}:title-validation-failed",
                    self.clock.now(),
                )
            if prior["context_delivery"].get("display_receipt") != observation:
                observation_digest = _sha256(
                    _stable_json(observation).encode("utf-8")
                )[:16]
                self.store.record_assignment_title_revalidation(
                    spec.assignment_id,
                    prior["version"],
                    observation,
                    f"event:{spec.assignment_id}:title-revalidated:{observation_digest}",
                    self.clock.now(),
                )
            return {
                "assignment_id": spec.assignment_id,
                "task_id": spec.task_id,
                "state": "active",
                "version": prior["version"],
                "runtime_instance_id": prior["runtime_instance_id"],
                "callsign": prior["callsign"],
                "context_delivery": {
                    **prior["context_delivery"],
                    "display_receipt": observation,
                },
                "idempotent": True,
            }
        try:
            outcome = AssignmentService(
                self.store,
                self.adapter,
                self.clock,
                _AssignmentIds(spec.assignment_id),
            ).assign(spec)
        except Exception as exc:
            if self.adapter._created is None:
                raise
            launch_state = self.store.assignment_launch_context(spec.assignment_id)
            cleanup_proven = self.adapter.cleanup(
                getattr(self.adapter, "_receipt", None)
            )
            failure_class = (
                f"launch_{exc.code}"
                if isinstance(exc, StorageRefusal)
                else f"launch_adapter_{type(exc).__name__.lower()}"
            )
            return self.store.block_assignment(
                spec.assignment_id,
                launch_state["version"],
                failure_class,
                True,
                cleanup_proven,
                self.clock.now(),
            )
        if outcome["state"] == "cleanup_pending" and self.adapter._created is not None:
            launch_state = self.store.assignment_launch_context(spec.assignment_id)
            receipt = launch_state["acceptance_receipt"]
            if receipt is None and self.adapter.cleanup(None):
                return self.store.block_assignment(
                    spec.assignment_id,
                    outcome["version"],
                    str(launch_state["failure_class"] or "launch_activation_failed"),
                    True,
                    True,
                    self.clock.now(),
                )
            return outcome
        if outcome["state"] != "active":
            return outcome
        launch_state = self.store.assignment_launch_context(spec.assignment_id)
        receipt = launch_state["acceptance_receipt"]
        if not isinstance(receipt, Mapping):
            raise StorageRefusal(
                "assignment_incomplete", "active assignment has no launch receipt"
            )
        try:
            context = render_launch_context(
                AssignmentSpec(**{**vars(spec), "callsign": launch_state["callsign"]}),
                receipt,
                self.options,
            )
            effect = self.adapter.deliver_context(receipt, context)
        except (LaunchAdapterError, StorageRefusal) as exc:
            failure_class = (
                exc.failure_class
                if isinstance(exc, LaunchAdapterError)
                else exc.code
            )
            failure = self.store.fail_assignment_context_delivery(
                spec.assignment_id,
                launch_state["version"],
                failure_class,
                f"event:{spec.assignment_id}:context-failed",
                f"outbox:{spec.assignment_id}:context-failed",
                self.clock.now(),
            )
            if not self.adapter.cleanup(receipt):
                return failure
            cleanup_receipt = _sha256(
                _stable_json(
                    {
                        "assignment_id": spec.assignment_id,
                        "endpoint": receipt["endpoint"],
                        "runtime_generation": receipt["runtime_generation"],
                        "thread_id": receipt["thread_id"],
                    }
                ).encode("utf-8")
            )
            try:
                self.store.close_runtime_for_cleanup(
                    str(receipt["runtime_instance_id"]),
                    str(receipt["endpoint"]),
                    str(receipt["runtime_generation"]),
                    self.clock.now(),
                )
                self.store.release_callsign(
                    f"callsign-assignment:{spec.assignment_id}",
                    2,
                    cleanup_receipt,
                    self.clock.now(),
                )
                return self.store.settle_assignment_launch_cleanup(
                    spec.assignment_id,
                    failure["version"],
                    cleanup_receipt,
                    self.clock.now(),
                )
            except StorageRefusal:
                return failure
        delivery = self.store.record_assignment_context_delivery(
            spec.assignment_id,
            launch_state["version"],
            effect["context_sha256"],
            effect["bytes"],
            effect["effect_sha256"],
            effect["display_receipt"],
            f"event:{spec.assignment_id}:context-delivered",
            self.clock.now(),
        )
        return {
            **outcome,
            "version": delivery["version"],
            "callsign": launch_state["callsign"],
            "context_delivery": delivery,
            "idempotent": False,
        }


__all__ = [
    "CommandRunner",
    "HerdrCodexLaunchAdapter",
    "MAX_CONTEXT_BYTES",
    "SubprocessRunner",
    "VisibleChampionLaunchService",
    "VisibleLaunchOptions",
    "derived_assignment_id",
    "derived_champion_agent_id",
    "derive_task_label",
    "render_launch_context",
]
