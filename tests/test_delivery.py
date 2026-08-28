#!/usr/bin/env python3
"""Focused watcher/direct transition delivery regressions."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agent-watcher"
CHAMPION_THREAD_ID = "00000000-0000-4000-8000-000000000017"
WATCHER_STATE_SCHEMA = 2


def run(records: Path, state: Path, args: list[str], *, env=None, check=True):
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


def write_records(records: Path, *, address: str, thread_id: str) -> Path:
    shotcaller = records / "Garen"
    shotcaller.mkdir(parents=True, exist_ok=True)
    (shotcaller / "status.json").write_text(
        json.dumps(
            {
                "callsign": "Garen",
                "role": "shotcaller",
                "shotcaller": None,
                "kind": "codex-thread",
                "address": address,
                "thread_id": thread_id,
                "task": "Shotcaller coordination",
                "status": "active",
                "updated_at": "2026-08-26T01:40:00-07:00",
                "update": "Shotcaller delivery endpoint is active.",
                "blocker": None,
                "next": "Receive one material transition.",
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
        "address": "%2",
        "thread_id": CHAMPION_THREAD_ID,
        "backend": "tmux",
        "task_id": "runtime-delivery-bard",
        "repository": None,
        "issue": None,
        "branch": None,
        "worktree": None,
        "task": "Example lifecycle routing",
        "status": "working",
        "updated_at": "2026-08-26T01:40:01-07:00",
        "update": "Started the delivery test.",
        "blocker": None,
        "next": "Emit one material transition.",
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


def append_transition(
    champion: Path,
    *,
    at: str,
    status: str,
    update: str,
    state_lock: Path | None = None,
) -> None:
    def write() -> None:
        with (champion / "updates.jsonl").open("a") as handle:
            handle.write(json.dumps({"at": at, "status": status, "update": update}) + "\n")
        snapshot = json.loads((champion / "status.json").read_text())
        snapshot.update({"status": status, "updated_at": at, "update": update})
        (champion / "status.json").write_text(json.dumps(snapshot) + "\n")

    if state_lock is None:
        write()
        return
    state_lock.parent.mkdir(parents=True, exist_ok=True)
    with state_lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        write()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def baseline(records: Path, state: Path, args: list[str], *, env: dict[str, str]) -> dict:
    result = json.loads(run(records, state, args, env=env).stdout)
    durable = json.loads((state / "shotcallers" / "Garen" / "state.json").read_text())
    assert durable["schema"] == WATCHER_STATE_SCHEMA
    assert durable["initialized"] is True
    assert durable["pending_events"] == {}
    return result


def fake_adapters(root: Path) -> tuple[dict[str, str], Path]:
    binary = root / "bin"
    binary.mkdir(parents=True)
    log = root / "prompts.log"
    tmux = binary / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *display-message*)\n"
        "    [ \"${TMUX_OPEN:-0}\" = 1 ] || exit 1\n"
        "    printf '%s\\t%s\\t%s\\t%s\\n' '%7' '4242' '0' 'python3'\n"
        "    ;;\n"
        "  *send-keys*-l*)\n"
        "    for last do :; done\n"
        "    printf '%s\\n' \"$last\" >> \"$DELIVERY_LOG\"\n"
        "    ;;\n"
        "esac\n"
    )
    tmux.chmod(0o755)
    herdr = binary / "herdr"
    herdr.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'agent list'*)\n"
        "    if [ \"${HERDR_OPEN:-0}\" = 1 ]; then\n"
        "      printf '%s\\n' '{\"result\":{\"agents\":[{\"agent_session\":{\"value\":\"garen-herdr-session\"},\"agent_status\":\"idle\",\"pane_id\":\"w1:p7\"}]}}'\n"
        "    else\n"
        "      printf '%s\\n' '{\"result\":{\"agents\":[]}}'\n"
        "    fi\n"
        "    ;;\n"
        "  *'agent prompt'*)\n"
        "    for last do :; done\n"
        "    printf '%s\\n' \"$last\" >> \"$DELIVERY_LOG\"\n"
        "    ;;\n"
        "esac\n"
    )
    herdr.chmod(0o755)
    env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", DELIVERY_LOG=str(log))
    return env, log


def tmux_deliver_args() -> list[str]:
    return ["--shotcaller", "Garen", "deliver", "--adapter", "tmux", "--tmux-socket", "fake"]


def herdr_deliver_args() -> list[str]:
    return [
        "--shotcaller",
        "Garen",
        "deliver",
        "--adapter",
        "herdr",
        "--herdr-session",
        "fake",
    ]


def test_idle_running_direct_once_both_backends(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "tmux-records", root / "tmux-state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1"))
    append_transition(
        champion,
        at="2026-08-26T01:41:00-07:00",
        status="blocked",
        update="Need one tmux Shotcaller decision.",
    )
    tmux_env = dict(env, TMUX_OPEN="1")
    first = json.loads(run(records, state, tmux_deliver_args(), env=tmux_env).stdout)
    second = json.loads(run(records, state, tmux_deliver_args(), env=tmux_env).stdout)
    assert first["delivered"] is True and first["channel"] == "tmux"
    assert second["reason"] == "duplicate"
    assert log.read_text().splitlines() == [
        f"CHAMPION TRANSITION [{first['event_id']}] Bard blocked: Need one tmux Shotcaller decision."
    ]

    records, state = root / "herdr-records", root / "herdr-state"
    champion = write_records(records, address="w1:p7", thread_id="garen-herdr-session")
    baseline(records, state, herdr_deliver_args(), env=dict(env, HERDR_OPEN="1"))
    append_transition(
        champion,
        at="2026-08-26T01:42:00-07:00",
        status="ready_to_land",
        update="Exact Herdr delivery head is ready.",
    )
    herdr_env = dict(env, HERDR_OPEN="1")
    first = json.loads(run(records, state, herdr_deliver_args(), env=herdr_env).stdout)
    second = json.loads(run(records, state, herdr_deliver_args(), env=herdr_env).stdout)
    assert first["delivered"] is True and first["channel"] == "herdr"
    assert second["reason"] == "duplicate"
    assert log.read_text().splitlines()[-1] == (
        f"CHAMPION TRANSITION [{first['event_id']}] Bard ready_to_land: "
        "Exact Herdr delivery head is ready."
    )


def test_closed_preserved_and_non_material_silent(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "closed-records", root / "closed-state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="0"))
    append_transition(
        champion,
        at="2026-08-26T01:43:00-07:00",
        status="cancelled",
        update="Preserve this transition while Garen is closed.",
    )
    closed = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="0")).stdout)
    assert closed == {
        "delivered": False,
        "event_id": closed["event_id"],
        "preserved": True,
        "reason": "shotcaller-closed",
    }
    durable = json.loads((state / "shotcallers" / "Garen" / "state.json").read_text())
    assert closed["event_id"] in durable["pending_events"]
    delivered = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert delivered["delivered"] is True and len(log.read_text().splitlines()) == 1

    records, state = root / "progress-records", root / "progress-state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    initial = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert initial["reason"] == "no-pending-transition"
    append_transition(
        champion,
        at="2026-08-26T01:44:00-07:00",
        status="progress",
        update="Routine progress must stay silent.",
    )
    progress = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert progress["reason"] == "non-material"
    assert len(log.read_text().splitlines()) == 1


def test_delayed_material_event_is_suppressed_by_newer_working_state(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="0"))
    append_transition(
        champion,
        at="2026-08-26T09:53:02Z",
        status="blocked",
        update="sky is not defined.",
    )
    delayed = json.loads(
        run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="0")).stdout
    )
    assert delayed["reason"] == "shotcaller-closed" and delayed["preserved"] is True
    append_transition(
        champion,
        at="2026-08-26T09:54:14Z",
        status="working",
        update="Computer Use canary recovered.",
    )
    append_transition(
        champion,
        at="2026-08-26T09:54:52Z",
        status="working",
        update="Computer Use canary succeeded.",
    )
    result = json.loads(
        run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout
    )
    assert result["reason"] == "superseded" and result["event_ids"] == [delayed["event_id"]]
    assert not log.exists(), "superseded blocked event reached the Shotcaller"
    durable = json.loads((state / "shotcallers" / "Garen" / "state.json").read_text())
    assert durable["delivered_events"][delayed["event_id"]]["channel"] == "superseded"


def test_active_watcher_wins_without_direct_prompt(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
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
            "--poll-seconds",
            "0.03",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env, TMUX_OPEN="1"),
    )
    state_path = state / "shotcallers" / "Garen" / "state.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if waiter.poll() is not None:
            raise AssertionError(waiter.stderr.read())
        if state_path.exists():
            current = json.loads(state_path.read_text())
            if current.get("initialized") and current.get("wait_active"):
                break
        time.sleep(0.02)
    else:
        waiter.terminate()
        raise AssertionError("watcher did not become active")

    append_transition(
        champion,
        at="2026-08-26T01:45:00-07:00",
        status="ready_to_land",
        update="Watcher owns this delivery.",
        state_lock=state_path.with_name(".state.lock"),
    )
    routed = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    event = json.loads(output)
    assert event["status"] == "ready_to_land"
    assert routed["reason"] in {"watcher-active", "duplicate"}
    duplicate = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert duplicate["reason"] == "duplicate"
    assert not log.exists(), "watcher-owned event was also direct-prompted"


def test_disabled_preserves_without_prompt(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1"))
    run(records, state, ["--shotcaller", "Garen", "disable"], env=dict(env, TMUX_OPEN="1"))
    append_transition(
        champion,
        at="2026-08-26T01:46:00-07:00",
        status="blocked",
        update="Disabled delivery must remain durable and silent.",
    )
    disabled = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert disabled["reason"] == "disabled" and disabled["preserved"] is True
    assert not log.exists()
    run(records, state, ["--shotcaller", "Garen", "enable"], env=dict(env, TMUX_OPEN="1"))
    delivered = json.loads(run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1")).stdout)
    assert delivered["delivered"] is True and len(log.read_text().splitlines()) == 1


def test_first_install_and_schema_migration_baseline_historical_events(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    append_transition(
        champion,
        at="2026-08-26T02:00:00-07:00",
        status="blocked",
        update="Historical blocker predates watcher installation.",
    )

    first = baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1"))
    assert first["reason"] == "no-pending-transition"
    assert not log.exists(), "first installation replayed historical material events"

    state_path = state / "shotcallers" / "Garen" / "state.json"
    legacy = json.loads(state_path.read_text())
    legacy.update(
        {
            "schema": 1,
            "initialized": True,
            "offsets": {},
            "seen": [],
            "pending_events": {
                f"historical-event-{index}": {
                    "event": "champion-update",
                    "event_id": f"historical-event-{index}",
                    "record": str(champion),
                    "source_path": str(champion / "updates.jsonl"),
                    "source_offset": 0,
                    "callsign": "Bard",
                    "shotcaller": "Garen",
                    "status": "blocked",
                    "at": "2026-08-26T02:00:00-07:00",
                    "update": "Historical blocker predates watcher installation.",
                }
                for index in range(44)
            },
        }
    )
    state_path.write_text(json.dumps(legacy) + "\n")
    assert len(json.loads(state_path.read_text())["pending_events"]) == 44

    migrated = baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1"))
    assert migrated["reason"] == "no-pending-transition"
    assert not log.exists(), "current-schema migration replayed historical material events"


def test_supervise_baselines_old_callsign_and_routes_only_fresh_event(root: Path) -> None:
    records, state = root / "records", root / "state"
    bard = write_records(records, address="%7", thread_id="garen-tmux-session")
    aatrox = records / "Garen" / "champions" / "Aatrox"
    aatrox.mkdir()
    snapshot = json.loads((bard / "status.json").read_text())
    snapshot.update(
        {
            "callsign": "Aatrox",
            "thread_id": "00000000-0000-4000-8000-000000000015",
            "task_id": "historical-aatrox-fixture",
            "status": "blocked",
            "updated_at": "2026-08-26T01:59:00-07:00",
            "update": "Migrated Aatrox to the installed strict current watcher schema.",
        }
    )
    (aatrox / "status.json").write_text(json.dumps(snapshot) + "\n")
    (aatrox / "updates.jsonl").write_text(
        json.dumps(
            {
                "at": snapshot["updated_at"],
                "status": snapshot["status"],
                "update": snapshot["update"],
            }
        )
        + "\n"
    )
    waiter = subprocess.Popen(
        [
            str(CLI),
            "--records-root",
            str(records),
            "--state-dir",
            str(state),
            "--shotcaller",
            "Garen",
            "supervise",
            "--poll-seconds",
            "0.03",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    state_path = state / "shotcallers" / "Garen" / "state.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if waiter.poll() is not None:
            raise AssertionError(waiter.stderr.read())
        if state_path.exists():
            durable = json.loads(state_path.read_text())
            if durable.get("initialized") and durable.get("wait_active"):
                assert durable["pending_events"] == {}
                break
        time.sleep(0.02)
    else:
        waiter.terminate()
        raise AssertionError("supervise did not establish its historical baseline")
    time.sleep(0.1)
    assert waiter.poll() is None, "supervise replayed the historical Aatrox event"

    append_transition(
        bard,
        at="2026-08-26T02:02:00-07:00",
        status="ready_to_land",
        update="Fresh Bard transition after the installation baseline.",
        state_lock=state_path.with_name(".state.lock"),
    )
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    event = json.loads(output)
    assert event["callsign"] == "Bard"
    assert event["status"] == "ready_to_land"
    assert event["update"] == "Fresh Bard transition after the installation baseline."


def test_event_id_and_payload_are_one_verified_candidate(root: Path) -> None:
    env, log = fake_adapters(root / "adapters")
    records, state = root / "records", root / "state"
    champion = write_records(records, address="%7", thread_id="garen-tmux-session")
    baseline(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="1"))
    append_transition(
        champion,
        at="2026-08-26T02:01:00-07:00",
        status="ready_to_land",
        update="Live exact-once wake canary fixture.",
    )
    closed = json.loads(
        run(records, state, tmux_deliver_args(), env=dict(env, TMUX_OPEN="0")).stdout
    )
    assert closed["reason"] == "shotcaller-closed"

    state_path = state / "shotcallers" / "Garen" / "state.json"
    durable = json.loads(state_path.read_text())
    event_id = closed["event_id"]
    crossed = durable["pending_events"][event_id]
    crossed.update(
        {
            "callsign": "Aatrox",
            "status": "blocked",
            "update": "Migrated Aatrox to the installed strict current watcher schema.",
        }
    )
    state_path.write_text(json.dumps(durable) + "\n")

    refused = run(
        records,
        state,
        tmux_deliver_args(),
        env=dict(env, TMUX_OPEN="1"),
        check=False,
    )
    assert refused.returncode != 0
    assert "event candidate conflicts with its durable source" in refused.stderr
    assert not log.exists(), "cross-wired event id and payload reached the Shotcaller"
    preserved = json.loads(state_path.read_text())
    assert event_id in preserved["pending_events"]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-delivery-test.") as value:
        root = Path(value)
        test_idle_running_direct_once_both_backends(root / "direct")
        test_closed_preserved_and_non_material_silent(root / "preserve")
        test_delayed_material_event_is_suppressed_by_newer_working_state(root / "superseded")
        test_active_watcher_wins_without_direct_prompt(root / "watcher")
        test_disabled_preserves_without_prompt(root / "disabled")
        test_first_install_and_schema_migration_baseline_historical_events(root / "baseline")
        test_supervise_baselines_old_callsign_and_routes_only_fresh_event(root / "supervise-baseline")
        test_event_id_and_payload_are_one_verified_candidate(root / "candidate")
    print("PASS: atomic watcher/direct candidates, safe baselines, superseded-event silence, Herdr, and tmux")


if __name__ == "__main__":
    main()
