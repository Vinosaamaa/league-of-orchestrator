#!/usr/bin/env python3
"""Focused baseline lifecycle-routing regressions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agent-watcher"
TRANSITION_SEQUENCE = 0
CHAMPION_THREAD_ID = "00000000-0000-4000-8000-000000000017"


def transition_time() -> str:
    global TRANSITION_SEQUENCE
    TRANSITION_SEQUENCE += 1
    minutes, seconds = divmod(TRANSITION_SEQUENCE, 60)
    return f"2026-08-26T01:{minutes:02d}:{seconds:02d}-07:00"


def run(args, *, records: Path, state: Path, check: bool = True, input_text: str | None = None, env=None):
    result = subprocess.run(
        [str(CLI), "--records-root", str(records), "--state-dir", str(state), *args],
        text=True,
        input=input_text,
        capture_output=True,
        timeout=30,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{args}: {result.returncode}: {result.stderr}")
    return result


def write_shotcaller(records: Path, callsign: str, session_id: str) -> None:
    directory = records / callsign
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(
        json.dumps(
            {
                "callsign": callsign,
                "role": "shotcaller",
                "shotcaller": None,
                "kind": "codex-thread",
                "address": session_id,
                "thread_id": session_id,
                "task": "Shotcaller coordination",
                "status": "active",
                "updated_at": "2026-08-26T01:00:00-07:00",
                "update": "Shotcaller fixture is active.",
                "blocker": None,
                "next": "Receive exact lifecycle transitions.",
            }
        )
        + "\n"
    )


def write_champion(
    records: Path,
    shotcaller: str,
    callsign: str,
    status: str = "working",
    *,
    task_id: str | None = None,
    thread_id: str | None = None,
    address: str | None = None,
    backend: str = "tmux",
):
    directory = records / shotcaller / "champions" / callsign
    directory.mkdir(parents=True, exist_ok=True)
    at = transition_time()
    update = "Started the assigned lifecycle task."
    snapshot = {
        "callsign": callsign,
        "role": "champion",
        "shotcaller": shotcaller,
        "kind": "codex-thread",
        "address": address or ("w1:p1" if backend == "herdr" else "%7"),
        "thread_id": thread_id or CHAMPION_THREAD_ID,
        "backend": backend,
        "repository": None,
        "issue": None,
        "branch": None,
        "worktree": None,
        "task_id": task_id or f"runtime-{callsign.lower()}",
        "task": "Example lifecycle routing",
        "status": status,
        "updated_at": at,
        "update": update,
        "blocker": None,
        "next": "Continue the focused lifecycle test.",
    }
    (directory / "status.json").write_text(json.dumps(snapshot) + "\n")
    (directory / "updates.jsonl").write_text(
        json.dumps({"at": at, "status": status, "update": update}) + "\n"
    )
    return directory


def append_update(directory: Path, status: str, update: str) -> None:
    at = transition_time()
    with (directory / "updates.jsonl").open("r") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with (directory / "updates.jsonl").open("a") as handle:
            handle.write(json.dumps({"at": at, "status": status, "update": update}) + "\n")
        snapshot = json.loads((directory / "status.json").read_text())
        snapshot.update({"status": status, "updated_at": at, "update": update})
        (directory / "status.json").write_text(json.dumps(snapshot) + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def wait_process(records: Path, state: Path, shotcaller: str = "Garen"):
    return subprocess.Popen(
        [
            str(CLI),
            "--records-root",
            str(records),
            "--state-dir",
            str(state),
            "--shotcaller",
            shotcaller,
            "wait",
            "--poll-seconds",
            "0.03",
            "--liveness-seconds",
            "0.05",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_until_baselined(
    state: Path, process: subprocess.Popen, shotcaller: str = "Garen", after_generation: int = 0
) -> None:
    state_path = state / "shotcallers" / shotcaller / "state.json"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        if state_path.exists():
            current = json.loads(state_path.read_text())
            if current.get("initialized") and int(current.get("wait_generation", 0)) > after_generation:
                return
        time.sleep(0.02)
    process.terminate()
    raise AssertionError("scoped watcher did not baseline")


def test_correct_shotcaller_blocked_once_and_user_priority(root: Path) -> None:
    records, state = root / "records", root / "state"
    write_shotcaller(records, "Garen", "garen-session")
    write_shotcaller(records, "Darius", "darius-session")
    bard = write_champion(records, "Garen", "Bard")
    darius_worker = write_champion(records, "Darius", "Ahri")
    process = wait_process(records, state)
    wait_until_baselined(state, process)
    append_update(darius_worker, "blocked", "wrong roster")
    time.sleep(0.15)
    assert process.poll() is None, "Darius event woke Garen"
    append_update(bard, "blocked", "needs gate")
    output, _ = process.communicate(timeout=3)
    event = json.loads(output)
    assert event["event"] == "champion-update" and event["shotcaller"] == "Garen"

    state_path = state / "shotcallers/Garen/state.json"
    generation = int(json.loads(state_path.read_text()).get("wait_generation", 0))
    duplicate = wait_process(records, state)
    wait_until_baselined(state, duplicate, after_generation=generation)
    assert duplicate.poll() is None, "blocked event prompted twice"
    hook = run(
        ["codex-user-prompt-hook"],
        records=records,
        state=state,
        input_text=json.dumps({"session_id": "garen-session", "prompt": "new user work"}),
    )
    assert json.loads(hook.stdout) == {}
    resumed, _ = duplicate.communicate(timeout=3)
    assert json.loads(resumed) == {"event": "user-message", "priority": "user", "shotcaller": "Garen"}


def test_disabled_stays_off(root: Path) -> None:
    records, state = root / "records", root / "state"
    write_shotcaller(records, "Garen", "garen-session")
    write_champion(records, "Garen", "Bard")
    run(["--shotcaller", "Garen", "disable"], records=records, state=state)
    disabled = run(["--shotcaller", "Garen", "wait"], records=records, state=state)
    assert json.loads(disabled.stdout)["event"] == "disabled"
    stop = run(
        ["codex-stop-hook"],
        records=records,
        state=state,
        input_text=json.dumps({"session_id": "garen-session", "stop_hook_active": False}),
    )
    assert json.loads(stop.stdout) == {}


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def landing_fixture(
    root: Path,
    *,
    disposition: str = "landed",
    merge_method: str = "ordinary",
    main_change_before_merge: bool = False,
    release_type: str = "web_deployment",
):
    root.mkdir(parents=True)
    repository = root / "repository"
    remote = root / "remote.git"
    worktree = root / "worktree"
    records = root / "records"
    state = root / "state"
    branch = "agent/bard/17-lifecycle-routing"
    task_id = "example-repository-17"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    git(repository, "config", "user.name", "Lifecycle Test")
    git(repository, "config", "user.email", "lifecycle@example.invalid")
    (repository / "README.md").write_text("base\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "base")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    git(repository, "worktree", "add", "-b", branch, str(worktree), "main")
    (worktree / "feature.txt").write_text("tested\n")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "tested head")
    git(worktree, "push", "-u", "origin", branch)
    tested_head = git(worktree, "rev-parse", "HEAD")
    merge_commit = tested_head
    if disposition == "landed":
        if main_change_before_merge:
            (repository / "unrelated-main.txt").write_text("later main\n")
            git(repository, "add", "unrelated-main.txt")
            git(repository, "commit", "-m", "unrelated main change")
        if merge_method == "ordinary":
            git(repository, "merge", "--no-ff", branch, "-m", "land exact head")
        elif merge_method == "squash":
            git(repository, "merge", "--squash", branch)
            git(repository, "commit", "-m", "squash land exact tested tree")
        else:
            raise AssertionError(f"unsupported fixture merge method: {merge_method}")
        merge_commit = git(repository, "rev-parse", "HEAD")

    write_shotcaller(records, "Garen", "garen-session")
    record = write_champion(
        records,
        "Garen",
        "Bard",
        "ready_to_land",
        task_id=task_id,
        thread_id=CHAMPION_THREAD_ID,
        address="%7",
    )
    append_update(record, "ready_to_land", "PR head and CI delivered to Garen")
    callsign_pool = root / "league-champions.json"
    callsign_pool.write_text(
        json.dumps(
            {
                "available": {"shotcaller": [], "champion": []},
                "in_use": {
                    "Bard": {"role": "champion", "record": str(record)}
                },
            }
        )
        + "\n"
    )
    manifest = {
        "schema": 2,
        "task_id": task_id,
        "generated_by": {"role": "shotcaller", "callsign": "Garen", "thread_id": "garen-session"},
        "target": {
            "role": "champion",
            "callsign": "Bard",
            "shotcaller": "Garen",
            "thread_id": CHAMPION_THREAD_ID,
            "address": "%7",
            "record_dir": str(record),
            "persistent": False,
        },
        "disposition": disposition,
        "issue": {"number": 17, "url": "https://example.invalid/example/repository/issues/17"},
        "repository_url": "https://example.invalid/example/repository",
        "repository_path": str(repository),
        "worktree": str(worktree),
        "branch": branch,
        "tested_head": tested_head,
        "published_ref": f"origin/{branch}",
        "clean_state": True,
        "no_unpublished_commits": True,
        "adapter": "tmux",
        "identity": {"socket": "isolated", "pane_id": "%7", "thread_id": CHAMPION_THREAD_ID},
        "expected_identity": {"socket": "isolated", "pane_id": "%7", "thread_id": CHAMPION_THREAD_ID},
        "grace_elapsed": True,
        "terminal_or_idle": True,
        "pending_decision_clear": True,
        "landed_at": "2026-08-25T23:50:00-07:00",
        "small_evidence_files": [],
        "callsign_release": {
            "pool": str(callsign_pool),
            "callsign": "Bard",
            "role": "champion",
        },
    }
    durable_identity = {
        "callsign": "Bard",
        "role": "champion",
        "shotcaller": "Garen",
        "thread_id": CHAMPION_THREAD_ID,
        "address": "%7",
        "backend": "tmux",
        "task_id": task_id,
        "repository": "https://example.invalid/example/repository",
        "issue": 17,
        "branch": branch,
        "worktree": str(worktree),
    }
    status = json.loads((record / "status.json").read_text())
    status.update(durable_identity)
    (record / "status.json").write_text(json.dumps(status) + "\n")
    manifest["durable_identity"] = durable_identity
    if disposition == "landed":
        manifest.update(
            {
                "pr": {
                    "number": 24,
                    "url": "https://example.invalid/example/repository/pull/24",
                    "head": tested_head,
                    "green": True,
                    "ci_url": "https://example.invalid/example/repository/actions/runs/24",
                    "ci_receipt": "all required checks passed for exact head",
                    "changed_files": ["feature.txt"],
                },
                "merge": {
                    "commit": merge_commit,
                    "head": tested_head,
                    "url": f"https://example.invalid/example/repository/commit/{merge_commit}",
                    "changed_files": ["feature.txt"],
                },
            }
        )
        if release_type == "local_install":
            source = root / "source-agent-watcher.py"
            installed = root / "installed-agent-watcher.py"
            source.write_bytes(b"#!/usr/bin/env python3\nprint('installed watcher')\n")
            installed.write_bytes(source.read_bytes())
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest["release"] = {
                "type": "local_install",
                "revision": merge_commit,
                "required_revision": merge_commit,
                "source": {"path": str(source), "sha256": digest},
                "installed": {"path": str(installed), "sha256": digest},
                "parity": True,
                "receipt": "installed/source watcher bytes match exact merged revision",
                "smoke": {
                    "passed": True,
                    "receipt": "isolated Herdr and tmux lifecycle smoke passed",
                },
            }
        elif release_type != "web_deployment":
            raise AssertionError(f"unsupported fixture release type: {release_type}")
        else:
            manifest.update(
                {
                    "deployment": {
                        "revision": merge_commit,
                        "required_revision": merge_commit,
                        "url": "https://example.invalid/example/repository/releases/tag/test",
                        "receipt": "installed revision checksum matched merge",
                    },
                    "post_deploy_smoke": {
                        "passed": True,
                        "url": "https://example.invalid/example/repository/actions/runs/25",
                        "receipt": "safe tmux and Herdr smoke passed",
                    },
                }
            )
    else:
        manifest["rejection"] = {
            "explicit": True,
            "authorized_by": "user",
            "url": "https://example.invalid/example/repository/issues/17#issuecomment-rejected",
            "receipt": "user explicitly rejected this exact tested head",
        }
    manifest_path = root / "teardown-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return records, state, record, manifest_path, manifest, repository, worktree, branch


def teardown_args(manifest_path: Path, archive_root: Path, *, execute: bool = False):
    args = [
        "teardown",
        "--adapter",
        "tmux",
        "--manifest",
        str(manifest_path),
        "--archive-dir",
        str(archive_root),
    ]
    if execute:
        args.append("--execute")
    return args


def test_landed_merge_accepts_ancestor_or_matching_tree(root: Path) -> None:
    for merge_method in ("ordinary", "squash"):
        fixture = landing_fixture(root / merge_method, merge_method=merge_method)
        records, state, _, manifest_path, manifest, repository, _, _ = fixture
        tested_head = manifest["tested_head"]
        merge_commit = manifest["merge"]["commit"]
        ancestry = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", tested_head, merge_commit],
            check=False,
        )
        assert (ancestry.returncode == 0) is (merge_method == "ordinary")
        if merge_method == "squash":
            assert git(repository, "rev-parse", f"{tested_head}^{{tree}}") == git(
                repository, "rev-parse", f"{merge_commit}^{{tree}}"
            )
        verified = run(
            teardown_args(manifest_path, records), records=records, state=state
        )
        assert json.loads(verified.stdout)["verified"] is True

    fixture = landing_fixture(root / "different-tree", merge_method="squash")
    records, state, _, manifest_path, manifest, repository, _, _ = fixture
    (repository / "post-merge-change.txt").write_text("different tree\n")
    git(repository, "add", "post-merge-change.txt")
    git(repository, "commit", "-m", "different landed tree")
    different_commit = git(repository, "rev-parse", "HEAD")
    manifest["merge"].update(
        {
            "commit": different_commit,
            "url": f"{manifest['repository_url']}/commit/{different_commit}",
            "changed_files": ["post-merge-change.txt"],
        }
    )
    manifest["pr"]["changed_files"] = ["post-merge-change.txt"]
    manifest["deployment"].update(
        {"revision": different_commit, "required_revision": different_commit}
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    refused = run(
        teardown_args(manifest_path, records),
        records=records,
        state=state,
        check=False,
    )
    assert refused.returncode != 0
    assert "changed-file integration proof failed" in refused.stderr


def test_squash_merge_with_later_main_and_local_install(root: Path) -> None:
    bad_records, bad_state, _, bad_manifest_path, bad_manifest, _, _, _ = landing_fixture(
        root / "bad-parity",
        merge_method="squash",
        main_change_before_merge=True,
        release_type="local_install",
    )
    Path(bad_manifest["release"]["installed"]["path"]).write_bytes(b"different\n")
    refused = run(
        teardown_args(bad_manifest_path, bad_records),
        records=bad_records,
        state=bad_state,
        check=False,
    )
    assert refused.returncode != 0 and "installed watcher hash changed" in refused.stderr

    smoke_records, smoke_state, _, smoke_manifest_path, smoke_manifest, _, _, _ = landing_fixture(
        root / "bad-smoke",
        merge_method="squash",
        main_change_before_merge=True,
        release_type="local_install",
    )
    smoke_manifest["release"]["smoke"]["passed"] = False
    smoke_manifest_path.write_text(json.dumps(smoke_manifest) + "\n")
    refused = run(
        teardown_args(smoke_manifest_path, smoke_records),
        records=smoke_records,
        state=smoke_state,
        check=False,
    )
    assert refused.returncode != 0 and "local install smoke is not green" in refused.stderr

    records, state, record, manifest_path, manifest, repository, worktree, branch = landing_fixture(
        root / "local-install",
        merge_method="squash",
        main_change_before_merge=True,
        release_type="local_install",
    )
    manifest["merge"].pop("changed_files")
    manifest["pr"].pop("changed_files")
    manifest_path.write_text(json.dumps(manifest) + "\n")
    verified = run(
        teardown_args(manifest_path, records), records=records, state=state
    )
    result = json.loads(verified.stdout)
    assert result["verified"] is True and result["dry_run"] is True

    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in *display-message*) printf '%s\\n' '%7' ;; esac\n"
    )
    tmux.chmod(0o755)
    completed = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        env=dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}"),
    )
    result = json.loads(completed.stdout)
    assert result["executed"] is True
    archive = records / "Garen/archive/2026-08-25/Bard/example-repository-17"
    archived_status = json.loads((archive / "status.json").read_text())
    assert archived_status["status"] == "completed"
    assert not (record / "status.json").exists()
    assert not worktree.exists() and not git(repository, "branch", "--list", branch)


def test_launch_preflight_and_backend_display(root: Path) -> None:
    records, state = root / "records", root / "state"
    write_shotcaller(records, "Garen", "garen-session")
    pool = root / "league-champions.json"
    pool.write_text(
        json.dumps(
            {
                "available": {"shotcaller": [], "champion": ["Bard", "Zed"]},
                "in_use": {},
            }
        )
        + "\n"
    )
    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    log = root / "herdr.log"
    herdr = fake_bin / "herdr"
    herdr.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$HERDR_LOG\"\n"
        "case \"$*\" in\n"
        "  *'agent list'*) printf '%s\\n' '{\"result\":{\"agents\":[]}}' ;;\n"
        "  *'agent get bard'*) printf '%s\\n' '{\"result\":{\"agent\":{\"name\":\"bard\",\"kind\":\"codex\",\"pane_id\":\"w1:p1\",\"agent_session\":{\"value\":\"00000000-0000-4000-8000-000000000017\"}}}}' ;;\n"
        "esac\n"
    )
    herdr.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", HERDR_LOG=str(log))
    launched = run(
        [
            "--shotcaller",
            "Garen",
            "launch",
            "--pool",
            str(pool),
            "--callsign",
            "Bard",
            "--name",
            "bard",
            "--display",
            "codex",
            "--task-id",
            "runtime-bard-launch",
            "--task",
            "Launch display regression",
            "--thread-id",
            CHAMPION_THREAD_ID,
            "--address",
            "w1:p1",
            "--herdr-session",
            "garen-session",
        ],
        records=records,
        state=state,
        env=env,
    )
    result = json.loads(launched.stdout)
    assert result["name"] == "bard" and result["display"] == "codex"
    snapshot = json.loads((records / "Garen/champions/Bard/status.json").read_text())
    assert snapshot["routing_name"] == "bard" and snapshot["display_agent"] == "codex"
    assert "agent start bard --kind codex --pane w1:p1" in log.read_text()
    assert "agent get bard" in log.read_text()

    blocked = run(
        [
            "--shotcaller",
            "Garen",
            "preflight",
            "--pool",
            str(pool),
            "--callsign",
            "Bard",
            "--name",
            "bard",
            "--display",
            "codex",
            "--thread-id",
            "00000000-0000-4000-8000-000000000073",
            "--address",
            "w1:p2A",
            "--herdr-session",
            "garen-session",
        ],
        records=records,
        state=state,
        env=env,
        check=False,
    )
    assert blocked.returncode != 0 and "callsign pool already assigns 'Bard'" in blocked.stderr

    duplicate_records, duplicate_state = root / "duplicates/records", root / "duplicates/state"
    write_shotcaller(duplicate_records, "Garen", "garen-session")
    write_champion(
        duplicate_records,
        "Garen",
        "Bard",
        backend="herdr",
        address="w1:p2A",
    )
    write_champion(
        duplicate_records,
        "Garen",
        "Ahri",
        backend="herdr",
        address="w1:p2A",
    )
    duplicate_pool = root / "duplicates/league-champions.json"
    duplicate_pool.parent.mkdir(parents=True, exist_ok=True)
    duplicate_pool.write_text(
        json.dumps({"available": {"shotcaller": [], "champion": ["Zed"]}, "in_use": {}}) + "\n"
    )
    duplicate = run(
        [
            "--shotcaller",
            "Garen",
            "preflight",
            "--pool",
            str(duplicate_pool),
            "--callsign",
            "Zed",
            "--name",
            "zed",
            "--display",
            "cursor",
            "--thread-id",
            "00000000-0000-4000-8000-00000000008d",
            "--address",
            "w1:p9Z",
            "--herdr-session",
            "garen-session",
        ],
        records=duplicate_records,
        state=duplicate_state,
        env=env,
        check=False,
    )
    assert duplicate.returncode != 0
    assert "duplicate herdr-endpoint" in duplicate.stderr and "Bard" in duplicate.stderr and "Ahri" in duplicate.stderr


def test_ready_to_land_and_proof_gated_teardown(root: Path) -> None:
    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    log = root / "tmux.log"
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TMUX_TEST_LOG\"\n"
        "case \"$*\" in *display-message*) printf '%s\\n' \"${TMUX_LIVE_PANE:-%7}\" ;; esac\n"
    )
    tmux.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", TMUX_TEST_LOG=str(log))

    records, state, record, manifest_path, manifest, repository, worktree, branch = landing_fixture(root / "safe")
    updates_path = record / "updates.jsonl"
    update_lines = updates_path.read_bytes().splitlines(keepends=True)
    watcher_state = state / "shotcallers" / "Garen" / "state.json"
    watcher_state.parent.mkdir(parents=True)
    watcher_state.write_text(
        json.dumps(
            {
                "schema": 2,
                "initialized": True,
                "offsets": {str(updates_path): len(update_lines[0])},
                "seen": [],
                "pending_events": {},
                "delivered_events": {},
            }
        )
        + "\n"
    )
    ready = run(
        ["--shotcaller", "Garen", "supervise", "--poll-seconds", "0.02"],
        records=records,
        state=state,
        env=env,
    )
    event = json.loads(ready.stdout)
    assert event["event"] == "champion-ready-to-land" and event["teardown_eligible"] is False
    assert worktree.exists() and (record / "status.json").exists() and not log.exists()

    red = dict(manifest)
    red["pr"] = dict(manifest["pr"], green=False)
    red_path = root / "safe" / "red-manifest.json"
    red_path.write_text(json.dumps(red) + "\n")
    refused = run(teardown_args(red_path, records), records=records, state=state, check=False, env=env)
    assert refused.returncode != 0 and "red or conflicting PR proof" in refused.stderr, refused.stderr
    assert worktree.exists() and (record / "status.json").exists() and not log.exists()

    completed = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        env=env,
    )
    result = json.loads(completed.stdout)
    assert result["executed"] is True and result["released_callsign"] == "Bard"
    assert "Bard" in json.loads(Path(manifest["callsign_release"]["pool"]).read_text())["available"]["champion"]
    assert not (record / "status.json").exists()
    archive = records / "Garen/archive/2026-08-25/Bard/example-repository-17"
    assert sorted(path.name for path in archive.iterdir()) == [
        "status.json",
        "task.json",
        "teardown-manifest.json",
        "updates.jsonl",
    ]
    archived_manifest = json.loads((archive / "teardown-manifest.json").read_text())
    archived_status = json.loads((archive / "status.json").read_text())
    archived_task = json.loads((archive / "task.json").read_text())
    assert archived_manifest["pr"]["head"] == manifest["tested_head"]
    assert archived_manifest["durable_identity"] == manifest["durable_identity"]
    assert archived_task["durable_identity"] == manifest["durable_identity"]
    assert {
        key: archived_status[key] for key in manifest["durable_identity"]
    } == manifest["durable_identity"]
    assert archived_manifest["teardown_result"]["remote_branch_deleted"] is False
    watcher_status = run(["status"], records=records, state=state, env=env)
    assert json.loads(watcher_status.stdout)["active_champions"] == 0
    assert not worktree.exists() and not git(repository, "branch", "--list", branch)
    assert "send-keys -t %7 C-c" in log.read_text() and "kill-pane -t %7" in log.read_text()

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "dirty")
    (worktree / "dirty.txt").write_text("uncommitted\n")
    refused = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        check=False,
        env=env,
    )
    assert refused.returncode != 0 and "worktree is dirty" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    records, state, record, manifest_path, _, _, worktree, _ = landing_fixture(root / "live-mismatch")
    mismatch_env = dict(env, TMUX_LIVE_PANE="%8")
    refused = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        check=False,
        env=mismatch_env,
    )
    assert refused.returncode != 0 and "live endpoint identity conflicts" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()


def test_archive_collision_and_secret_exclusion(root: Path) -> None:
    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/bin/sh\ncase \"$*\" in *display-message*) printf '%s\\n' '%7' ;; esac\nexit 0\n"
    )
    tmux.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "collision")
    collision = records / "Garen/archive/2026-08-25/Bard/example-repository-17"
    collision.mkdir(parents=True)
    refused = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        check=False,
        env=env,
    )
    assert refused.returncode != 0 and "archive collision" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "secret")
    secret = root / "secret" / "small.log"
    secret.write_text("password=synthetic-rejection-fixture\n")
    manifest["small_evidence_files"] = [str(secret)]
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        check=False,
        env=env,
    )
    assert refused.returncode != 0 and "may contain secrets" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    manifest["small_evidence_files"] = []
    manifest["access_token"] = "synthetic-rejection-fixture"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(
        teardown_args(manifest_path, records),
        records=records,
        state=state,
        check=False,
        env=env,
    )
    assert refused.returncode != 0 and "sensitive manifest key" in refused.stderr

    del manifest["access_token"]
    manifest["pr"]["ci_receipt"] = "Bearer synthetic-rejection-fixture"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(
        teardown_args(manifest_path, records),
        records=records,
        state=state,
        check=False,
        env=env,
    )
    assert refused.returncode != 0 and "secret-like content" in refused.stderr


def test_shotcaller_and_rejected_work_boundaries(root: Path) -> None:
    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "role")
    manifest["target"]["role"] = "shotcaller"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(teardown_args(manifest_path, records), records=records, state=state, check=False)
    assert refused.returncode != 0 and "persistent supervisors are ineligible" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(
        root / "rejected", disposition="rejected"
    )
    manifest["rejection"]["explicit"] = False
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(teardown_args(manifest_path, records), records=records, state=state, check=False)
    assert refused.returncode != 0 and "exact user authority" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(
        root / "cancelled", disposition="rejected"
    )
    append_update(record, "cancelled", "User-authorized rejection preserved for teardown proof.")
    allowed = run(teardown_args(manifest_path, records), records=records, state=state)
    assert json.loads(allowed.stdout)["verified"] is True
    assert worktree.exists() and (record / "status.json").exists()


def task_resource(
    records: Path,
    state: Path,
    manifest: dict,
    process: subprocess.Popen,
    registry_path: Path,
    *,
    registry_owner: str = "Bard",
    process_start: str | None = None,
) -> None:
    inspected = json.loads(
        run(["resource-inspect", "--pid", str(process.pid)], records=records, state=state).stdout
    )
    start = process_start or inspected["process_start"]
    resource = {
        "kind": "process",
        "resource_id": "chrome-devtools-bridge-1",
        "pid": process.pid,
        "task_id": manifest["task_id"],
        "owner": "Bard",
        "endpoint": "chrome-devtools-axi://task-bridge",
        "generation": "generation-1",
        "process_start": start,
    }
    registered = dict(resource, owner=registry_owner)
    registry_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "resources": {resource["resource_id"]: registered},
                "shared_agent_chrome": {"owners": []},
            }
        )
        + "\n"
    )
    manifest["resource_registry"] = str(registry_path)
    manifest["task_resources"] = [resource]


def test_task_owned_resource_cleanup_and_refusals(root: Path) -> None:
    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/bin/sh\ncase \"$*\" in *display-message*) printf '%s\\n' '%7' ;; esac\nexit 0\n"
    )
    tmux.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")

    records, state, _, manifest_path, manifest, _, _, _ = landing_fixture(root / "owned")
    process = subprocess.Popen(["sleep", "60"])
    registry = root / "owned" / "resource-registry.json"
    task_resource(records, state, manifest, process, registry)
    manifest_path.write_text(json.dumps(manifest) + "\n")
    result = run(
        teardown_args(manifest_path, records, execute=True),
        records=records,
        state=state,
        env=env,
    )
    process.wait(timeout=3)
    payload = json.loads(result.stdout)
    assert payload["task_resources"] == [
        {"exit_verified": True, "pid": process.pid, "resource_id": "chrome-devtools-bridge-1"}
    ]
    assert json.loads(registry.read_text())["resources"] == {}

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "owner")
    process = subprocess.Popen(["sleep", "60"])
    registry = root / "owner" / "resource-registry.json"
    try:
        task_resource(records, state, manifest, process, registry, registry_owner="Other")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        refused = run(
            teardown_args(manifest_path, records), records=records, state=state, check=False, env=env
        )
        assert refused.returncode != 0 and "registry ownership conflicts" in refused.stderr
        assert process.poll() is None and worktree.exists() and (record / "status.json").exists()
    finally:
        process.terminate()
        process.wait(timeout=3)

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "reuse")
    process = subprocess.Popen(["sleep", "60"])
    registry = root / "reuse" / "resource-registry.json"
    try:
        task_resource(records, state, manifest, process, registry, process_start="PID-REUSED")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        refused = run(
            teardown_args(manifest_path, records), records=records, state=state, check=False, env=env
        )
        assert refused.returncode != 0 and "PID generation conflicts" in refused.stderr
        assert process.poll() is None and worktree.exists() and (record / "status.json").exists()
    finally:
        process.terminate()
        process.wait(timeout=3)

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "stale")
    stale = {
        "kind": "process",
        "resource_id": "stale-client",
        "pid": 999999,
        "task_id": manifest["task_id"],
        "owner": "Bard",
        "endpoint": "chrome-devtools-axi://stale",
        "generation": "generation-stale",
        "process_start": "stale",
    }
    registry = root / "stale" / "resource-registry.json"
    registry.write_text(json.dumps({"schema": 1, "resources": {"stale-client": stale}}) + "\n")
    manifest["resource_registry"] = str(registry)
    manifest["task_resources"] = [stale]
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(teardown_args(manifest_path, records), records=records, state=state, check=False, env=env)
    assert refused.returncode != 0 and "stale or unavailable" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()

    records, state, record, manifest_path, manifest, _, worktree, _ = landing_fixture(root / "shared")
    registry = root / "shared" / "resource-registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": 1,
                "resources": {},
                "shared_agent_chrome": {
                    "owners": [{"task_id": "other-task", "owner": "Other", "generation": "lease-1"}]
                },
            }
        )
        + "\n"
    )
    manifest["resource_registry"] = str(registry)
    manifest["task_resources"] = [
        {
            "kind": "shared-agent-chrome",
            "resource_id": "agent-chrome",
            "task_id": manifest["task_id"],
            "owner": "Bard",
            "endpoint": "fixture://owned-process",
            "generation": "lease-2",
            "action": "restart",
        }
    ]
    manifest_path.write_text(json.dumps(manifest) + "\n")
    refused = run(teardown_args(manifest_path, records), records=records, state=state, check=False, env=env)
    assert refused.returncode != 0 and "active lease owners" in refused.stderr
    assert worktree.exists() and (record / "status.json").exists()


def test_hidden_worker_lead_and_model_routing(root: Path) -> None:
    records, state = root / "records", root / "state"
    records.mkdir(parents=True)
    pool = root / "scientists.json"
    pool.write_text(json.dumps({"schema": 1, "available": ["Curie", "Turing"], "active": {}}) + "\n")
    assignment = json.loads(
        run(
            [
                "hidden-worker",
                "allocate",
                "--pool",
                str(pool),
                "--owner",
                "Garen",
                "--worker-id",
                "worker-1",
                "--model",
                "explicit-model",
                "--effort",
                "high",
                "--reason",
                "specialized bounded check",
            ],
            records=records,
            state=state,
        ).stdout
    )
    assert assignment["callsign"] == "Curie" and not (records / "Garen/champions/Curie").exists()
    evidence = root / "worker-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "identity": {"callsign": "Curie", "worker_id": "worker-1", "owner": "Garen"},
                "terminal_or_idle": True,
                "result_delivered": True,
                "unpublished_work_reconciled": False,
            }
        )
        + "\n"
    )
    unsafe = run(
        ["hidden-worker", "release", "--pool", str(pool), "--evidence", str(evidence)],
        records=records,
        state=state,
        check=False,
    )
    assert unsafe.returncode != 0 and "unpublished_work_reconciled" in unsafe.stderr
    worker_evidence = json.loads(evidence.read_text())
    worker_evidence["unpublished_work_reconciled"] = True
    evidence.write_text(json.dumps(worker_evidence) + "\n")
    released = json.loads(
        run(
            ["hidden-worker", "release", "--pool", str(pool), "--evidence", str(evidence)],
            records=records,
            state=state,
        ).stdout
    )
    assert released["released"] is True and "Curie" in json.loads(pool.read_text())["available"]

    config = root / "lead.json"
    event = root / "event.json"
    relay_state = root / "relay-state.json"
    config.write_text('{"lead":null}\n')
    event.write_text('{"status":"blocked","update":"needs user"}\n')
    absent = json.loads(
        run(
            ["lead-relay", "--config", str(config), "--event", str(event), "--relay-state", str(relay_state)],
            records=records,
            state=state,
        ).stdout
    )
    assert absent == {"durable": True, "reason": "lead-unassigned", "relayed": False}

    delivery = root / "delivery.py"
    delivery_log = root / "delivery.jsonl"
    delivery.write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as handle: handle.write(sys.stdin.read())\n"
    )
    delivery_command = f"python3 {delivery} {delivery_log}"
    config.write_text('{"lead":{"callsign":"Garen"}}\n')
    first = json.loads(
        run(
            [
                "lead-relay",
                "--config",
                str(config),
                "--event",
                str(event),
                "--relay-state",
                str(relay_state),
                "--delivery-command",
                delivery_command,
            ],
            records=records,
            state=state,
        ).stdout
    )
    assert first["relayed"] is True and first["lead"] == "Garen"
    config.write_text('{"lead":{"callsign":"Jarvan"}}\n')
    reassigned = json.loads(
        run(
            [
                "lead-relay",
                "--config",
                str(config),
                "--event",
                str(event),
                "--relay-state",
                str(relay_state),
                "--delivery-command",
                delivery_command,
            ],
            records=records,
            state=state,
        ).stdout
    )
    assert reassigned["relayed"] is True and reassigned["lead"] == "Jarvan"
    delivered = [json.loads(line) for line in delivery_log.read_text().splitlines()]
    assert [payload["lead"]["callsign"] for payload in delivered] == ["Garen", "Jarvan"]

    model_config = root / "models.json"
    model_config.write_text(
        json.dumps(
            {
                "tiers": {
                    "COORDINATOR": {"model": "coordinator", "effort": "high"},
                    "WORKER_FAST": {"model": "fast", "effort": "medium"},
                    "WORKER_STRONG": {"model": "strong", "effort": "xhigh"},
                }
            }
        )
        + "\n"
    )
    routed = json.loads(
        run(
            [
                "route-model",
                "--config",
                str(model_config),
                "--task-profile",
                "bounded",
                "--model",
                "user/model-exact",
                "--effort",
                "ultra",
            ],
            records=records,
            state=state,
        ).stdout
    )
    assert routed["model"] == "user/model-exact" and routed["effort"] == "ultra"
    assert routed["explicit"] == {"effort": True, "model": True}


def test_codex_hook_install_is_idempotent(root: Path) -> None:
    records, state = root / "records", root / "state"
    records.mkdir(parents=True)
    hooks = root / "hooks.json"
    hooks.write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep-me"}]}]}}\n')
    args = ["install-codex-hooks", "--hooks", str(hooks), "--command", "/stable/agent-watcher"]
    run(args, records=records, state=state)
    run(args, records=records, state=state)
    document = json.loads(hooks.read_text())
    commands = [
        handler["command"]
        for groups in document["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert commands.count("keep-me") == 1
    assert commands.count("/stable/agent-watcher codex-stop-hook") == 1
    assert commands.count("/stable/agent-watcher codex-user-prompt-hook") == 1


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-lifecycle-test.") as directory:
        root = Path(directory)
        test_correct_shotcaller_blocked_once_and_user_priority(root / "routing")
        test_disabled_stays_off(root / "disabled")
        test_landed_merge_accepts_ancestor_or_matching_tree(root / "merge-proof")
        test_squash_merge_with_later_main_and_local_install(root / "issue-40-release")
        test_launch_preflight_and_backend_display(root / "issue-40-launch")
        test_ready_to_land_and_proof_gated_teardown(root / "teardown")
        test_archive_collision_and_secret_exclusion(root / "archive")
        test_shotcaller_and_rejected_work_boundaries(root / "boundaries")
        test_task_owned_resource_cleanup_and_refusals(root / "resources")
        test_hidden_worker_lead_and_model_routing(root / "worker")
        test_codex_hook_install_is_idempotent(root / "hooks")
    print("PASS: scoped wake, merge/squash/later-main proof, local-install release, atomic launch naming, ready_to_land preservation, proof-gated teardown, canonical archive/secret exclusion, exact task-resource cleanup, hidden-worker release, optional Lead, exact model routing, and idempotent hooks")


if __name__ == "__main__":
    main()
