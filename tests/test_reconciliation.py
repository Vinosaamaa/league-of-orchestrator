#!/usr/bin/env python3
"""Focused atomic transition and runtime/status reconciliation tests."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agent-watcher"
MODULE_PATH = ROOT / "src" / "agent_watcher.py"
CHAMPION_THREAD_ID = "00000000-0000-4000-8000-000000000017"


def run(records: Path, state: Path, *args: str, env=None, check=True):
    result = subprocess.run(
        [str(CLI), "--records-root", str(records), "--state-dir", str(state), *args],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{args}: {result.stderr}")
    return result


def write_records(records: Path, *, adapter: str) -> Path:
    shotcaller = records / "Garen"
    shotcaller.mkdir(parents=True)
    shotcaller_address = "w1:p1" if adapter == "herdr" else "%1"
    champion_address = "w1:p2" if adapter == "herdr" else "%2"
    (shotcaller / "status.json").write_text(
        json.dumps(
            {
                "callsign": "Garen",
                "role": "shotcaller",
                "shotcaller": None,
                "kind": "codex-thread",
                "address": shotcaller_address,
                "thread_id": "garen-session",
                "task": "Shotcaller reconciliation",
                "status": "active",
                "updated_at": "2026-08-26T02:00:00-07:00",
                "update": "Shotcaller endpoint is active.",
                "blocker": None,
                "next": "Receive reconciled lifecycle evidence.",
            }
        )
        + "\n"
    )
    champion = shotcaller / "champions" / "Bard"
    champion.mkdir(parents=True)
    snapshot = {
        "callsign": "Bard",
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": champion_address,
        "thread_id": CHAMPION_THREAD_ID,
        "backend": adapter,
        "task_id": "runtime-reconciliation-bard",
        "repository": None,
        "issue": None,
        "branch": None,
        "worktree": None,
        "task": "Example lifecycle routing",
        "status": "working",
        "updated_at": "2026-08-26T02:00:01-07:00",
        "update": "Champion endpoint is working.",
        "blocker": None,
        "next": "Complete the implementation.",
    }
    (champion / "status.json").write_text(json.dumps(snapshot) + "\n")
    (champion / "updates.jsonl").write_text(
        json.dumps(
            {
                "at": snapshot["updated_at"],
                "status": snapshot["status"],
                "update": snapshot["update"],
            }
        )
        + "\n"
    )
    return champion


def fake_adapters(root: Path) -> tuple[dict[str, str], Path]:
    binary = root / "bin"
    binary.mkdir(parents=True)
    log = root / "prompts.log"
    snapshot_log = root / "snapshots.log"
    tmux = binary / "tmux"
    tmux.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if 'list-panes' in args:\n"
        "    with open(os.environ['SNAPSHOT_LOG'], 'a', encoding='utf-8') as handle: handle.write('tmux\\n')\n"
        "    print('%1\\t4201\\t0\\tpython3')\n"
        "    status = os.environ.get('TMUX_CHAMPION', 'running')\n"
        "    if status != 'closed': print('%2\\t4202\\t0\\t' + ('zsh' if status == 'settled' else 'python3'))\n"
        "elif 'display-message' in args:\n"
        "    target = args[args.index('-t') + 1]\n"
        "    if target == '%2' and os.environ.get('TMUX_CHAMPION') == 'closed': raise SystemExit(1)\n"
        "    print(f'{target}\\t4201\\t0\\tpython3')\n"
        "elif 'send-keys' in args and '-l' in args:\n"
        "    with open(os.environ['PROMPT_LOG'], 'a', encoding='utf-8') as handle: handle.write(args[-1] + '\\n')\n"
    )
    tmux.chmod(0o755)
    herdr = binary / "herdr"
    herdr.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if sys.argv[-2:] == ['agent', 'list']:\n"
        "    with open(os.environ['SNAPSHOT_LOG'], 'a', encoding='utf-8') as handle: handle.write('herdr\\n')\n"
        "    agents = [{'agent_session': {'value': 'garen-session'}, 'agent_status': 'idle', 'pane_id': 'w1:p1'}]\n"
        "    status = os.environ.get('HERDR_CHAMPION', 'working')\n"
        "    if status != 'closed':\n"
        f"        agents.append({{'agent_session': {{'value': '{CHAMPION_THREAD_ID}'}}, 'agent_status': status, 'pane_id': 'w1:p2'}})\n"
        "    agents.append({'agent_session': {'value': '00000000-0000-4000-8000-000000000015'}, 'agent_status': 'working', 'pane_id': 'w1:p3'})\n"
        "    print(json.dumps({'result': {'agents': agents}}))\n"
        "elif 'prompt' in sys.argv:\n"
        "    with open(os.environ['PROMPT_LOG'], 'a', encoding='utf-8') as handle:\n"
        "        handle.write(sys.argv[-1] + '\\n')\n"
    )
    herdr.chmod(0o755)
    return dict(
        os.environ,
        PATH=f"{binary}:{os.environ['PATH']}",
        PROMPT_LOG=str(log),
        SNAPSHOT_LOG=str(snapshot_log),
    ), log


def test_atomic_transition_and_no_partial_write(root: Path) -> None:
    records, state = root / "records", root / "state"
    champion = write_records(records, adapter="tmux")
    result = json.loads(
        run(
            records,
            state,
            "transition",
            "--no-deliver",
            "--record",
            str(champion),
            "--status",
            "blocked",
            "--update",
            "Blocked on exact Shotcaller authority.",
            "--blocker",
            "Landing authority is absent.",
            "--next",
            "Await a Shotcaller decision.",
            "--at",
            "2026-08-26T02:01:00-07:00",
        ).stdout
    )
    snapshot = json.loads((champion / "status.json").read_text())
    transitions = [json.loads(line) for line in (champion / "updates.jsonl").read_text().splitlines()]
    assert result["status"] == "blocked"
    assert transitions[-1] == {
        "at": snapshot["updated_at"],
        "status": snapshot["status"],
        "update": snapshot["update"],
    }

    specification = importlib.util.spec_from_file_location("agent_watcher_atomic_test", MODULE_PATH)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    before_status = (champion / "status.json").read_bytes()
    before_updates = (champion / "updates.jsonl").read_bytes()
    original_write = module._write_json_atomic

    def fail_status_write(path, value):
        if Path(path).name == "status.json":
            raise OSError("injected snapshot replacement failure")
        return original_write(path, value)

    module._write_json_atomic = fail_status_write
    try:
        module.transition_record(
            records,
            champion,
            "failed",
            "Synthetic failure must roll back.",
            "Preserve the exact record.",
            "Injected failure.",
            "2026-08-26T02:02:00-07:00",
        )
    except module.WatcherError as exc:
        assert "without a partial record" in str(exc)
    else:
        raise AssertionError("injected transition write unexpectedly succeeded")
    assert (champion / "status.json").read_bytes() == before_status
    assert (champion / "updates.jsonl").read_bytes() == before_updates


def test_transition_routes_through_shared_delivery(root: Path) -> None:
    env, prompt_log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, adapter="tmux")
    result = json.loads(
        run(
            records,
            state,
            "transition",
            "--record",
            str(champion),
            "--status",
            "blocked",
            "--update",
            "Atomic transition must wake the exact Shotcaller.",
            "--next",
            "Await the exact Shotcaller response.",
            "--at",
            "2026-08-26T02:04:00-07:00",
            "--adapter",
            "tmux",
            "--tmux-socket",
            "fake",
            env=dict(env, TMUX_CHAMPION="running"),
        ).stdout
    )
    assert result["delivery"]["delivered"] is True
    assert len(prompt_log.read_text().splitlines()) == 1


def reconcile_args(adapter: str) -> list[str]:
    args = ["--shotcaller", "Garen", "reconcile", "--adapter", adapter, "--consecutive", "2"]
    args += ["--herdr-session", "fake"] if adapter == "herdr" else ["--tmux-socket", "fake"]
    return args


def add_running_champion(records: Path) -> None:
    champion = records / "Garen" / "champions" / "Zilean"
    champion.mkdir(parents=True)
    snapshot = {
        "callsign": "Zilean",
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": "w1:p3",
        "thread_id": "00000000-0000-4000-8000-000000000015",
        "backend": "herdr",
        "task_id": "runtime-reconciliation-zilean",
        "repository": None,
        "issue": None,
        "branch": None,
        "worktree": None,
        "task": "Batched snapshot control",
        "status": "working",
        "updated_at": "2026-08-26T02:00:02-07:00",
        "update": "Control Champion remains working.",
        "blocker": None,
        "next": "Remain active during the batch test.",
    }
    (champion / "status.json").write_text(json.dumps(snapshot) + "\n")
    (champion / "updates.jsonl").write_text(
        json.dumps({"at": snapshot["updated_at"], "status": "working", "update": snapshot["update"]})
        + "\n"
    )


def test_automatic_wait_batched_cadence(root: Path) -> None:
    env, prompt_log = fake_adapters(root / "adapters")
    snapshot_log = root / "adapters" / "snapshots.log"
    records, state = root / "records", root / "state"
    champion = write_records(records, adapter="herdr")
    add_running_champion(records)
    before_status = (champion / "status.json").read_bytes()
    before_updates = (champion / "updates.jsonl").read_bytes()
    waiter = subprocess.Popen(
        [
            str(CLI),
            "--records-root",
            str(records),
            "--state-dir",
            str(state),
            "--shotcaller",
            "Garen",
            "wait",
            "--adapter",
            "herdr",
            "--herdr-session",
            "fake",
            "--poll-seconds",
            "0.03",
            "--reconcile-seconds",
            "1.0",
            "--reconcile-consecutive",
            "2",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env, HERDR_CHAMPION="done"),
    )
    state_path = state / "shotcallers" / "Garen" / "state.json"
    baseline_deadline = time.monotonic() + 3
    while time.monotonic() < baseline_deadline:
        if waiter.poll() is not None:
            raise AssertionError(waiter.stderr.read())
        if state_path.exists() and json.loads(state_path.read_text()).get("initialized"):
            break
        time.sleep(0.02)
    else:
        waiter.terminate()
        raise AssertionError("automatic watcher did not baseline")
    time.sleep(0.2)
    assert not snapshot_log.exists(), "runtime adapter was queried on ordinary file polls"
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        count = len(snapshot_log.read_text().splitlines()) if snapshot_log.exists() else 0
        if count >= 1:
            break
        time.sleep(0.02)
    assert count == 1 and waiter.poll() is None, (
        count,
        waiter.returncode,
        waiter.stderr.read() if waiter.poll() is not None else "",
    )
    output, error = waiter.communicate(timeout=4)
    assert not error, error
    event = json.loads(output)
    assert event["event"] == "champion_stalled" and event["callsign"] == "Bard"
    assert len(snapshot_log.read_text().splitlines()) == 2
    assert not prompt_log.exists(), "watcher-owned reconciliation event was directly prompted"
    assert (champion / "status.json").read_bytes() == before_status
    assert (champion / "updates.jsonl").read_bytes() == before_updates


def test_transient_suppression_and_stable_herdr_done(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, adapter="herdr")
    original_status = (champion / "status.json").read_bytes()
    original_updates = (champion / "updates.jsonl").read_bytes()
    first = json.loads(run(records, state, *reconcile_args("herdr"), env=dict(env, HERDR_CHAMPION="done")).stdout)
    reset = json.loads(run(records, state, *reconcile_args("herdr"), env=dict(env, HERDR_CHAMPION="working")).stdout)
    again = json.loads(run(records, state, *reconcile_args("herdr"), env=dict(env, HERDR_CHAMPION="done")).stdout)
    assert first["queued"] == [] and reset["queued"] == [] and again["queued"] == []
    assert not log.exists()
    delivered = json.loads(run(records, state, *reconcile_args("herdr"), env=dict(env, HERDR_CHAMPION="done")).stdout)
    duplicate = json.loads(run(records, state, *reconcile_args("herdr"), env=dict(env, HERDR_CHAMPION="done")).stdout)
    assert len(delivered["queued"]) == 1 and delivered["delivery"]["delivered"] is True
    assert duplicate["queued"] == [] and len(log.read_text().splitlines()) == 1
    assert (champion / "status.json").read_bytes() == original_status
    assert (champion / "updates.jsonl").read_bytes() == original_updates


def test_stable_tmux_closed_terminal_silence_and_dedup(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    snapshot_log = root / "adapters" / "snapshots.log"
    records, state = root / "records", root / "state"
    champion = write_records(records, adapter="tmux")
    first = json.loads(run(records, state, *reconcile_args("tmux"), env=dict(env, TMUX_CHAMPION="closed")).stdout)
    delivered = json.loads(run(records, state, *reconcile_args("tmux"), env=dict(env, TMUX_CHAMPION="closed")).stdout)
    duplicate = json.loads(run(records, state, *reconcile_args("tmux"), env=dict(env, TMUX_CHAMPION="closed")).stdout)
    assert first["queued"] == []
    assert len(delivered["queued"]) == 1 and delivered["delivery"]["delivered"] is True
    assert duplicate["queued"] == [] and len(log.read_text().splitlines()) == 1

    run(
        records,
        state,
        "transition",
        "--no-deliver",
        "--record",
        str(champion),
        "--status",
        "completed",
        "--update",
        "Champion work is complete.",
        "--next",
        "Await Shotcaller review.",
        "--at",
        "2026-08-26T02:03:00-07:00",
    )
    snapshots_before_terminal = len(snapshot_log.read_text().splitlines())
    terminal = json.loads(run(records, state, *reconcile_args("tmux"), env=dict(env, TMUX_CHAMPION="closed")).stdout)
    assert terminal["queued"] == [] and terminal["observations"][0]["runtime"] == "terminal"
    assert len(snapshot_log.read_text().splitlines()) == snapshots_before_terminal
    assert len(log.read_text().splitlines()) == 1


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-reconciliation-test.") as value:
        root = Path(value)
        test_atomic_transition_and_no_partial_write(root / "atomic")
        test_transition_routes_through_shared_delivery(root / "transition-delivery")
        test_automatic_wait_batched_cadence(root / "automatic")
        test_transient_suppression_and_stable_herdr_done(root / "herdr")
        test_stable_tmux_closed_terminal_silence_and_dedup(root / "tmux")
    print("PASS: atomic transitions and debounced read-only Herdr/tmux runtime reconciliation")


if __name__ == "__main__":
    main()
