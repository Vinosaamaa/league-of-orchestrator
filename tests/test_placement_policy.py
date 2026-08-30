#!/usr/bin/env python3
"""Combined Shotcaller/Squad/Champion placement-policy acceptance."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.shotcaller_bootstrap import (  # noqa: E402
    HerdrShotcallerBootstrapAdapter,
    ShotcallerBootstrapService,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.visible_launch import VisibleChampionLaunchService  # noqa: E402
from lifecycle_fakes import FakeClock  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402
from test_shotcaller_bootstrap import (  # noqa: E402
    RecordingHerdr,
    _options as shotcaller_options,
    _seed_available_ashe,
    _spec as shotcaller_spec,
)
from test_visible_champion_launch import (  # noqa: E402
    FakeHerdrRunner,
    SHOTCALLER_PANE_ID,
    _adapter as champion_adapter,
    _context as champion_context,
    _options as champion_options,
    _spec as champion_spec,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-placement-policy-") as temporary:
        root = Path(temporary)
        state, _ = migrated_state(root, "shotcaller")
        shotcaller_worktree = root / "shotcaller" / "worktree"
        shotcaller_worktree.mkdir()
        shotcaller_runner = RecordingHerdr(shotcaller_worktree)
        with SQLiteStorage(state) as store:
            clock = FakeClock()
            _seed_available_ashe(store, clock)
            ShotcallerBootstrapService(
                store,
                HerdrShotcallerBootstrapAdapter(
                    shotcaller_options(shotcaller_worktree),
                    shotcaller_runner,
                    environment={
                        "HERDR_ENV": "1",
                        "HERDR_WORKSPACE_ID": "w1",
                        "HERDR_TAB_ID": "w1:t1",
                        "HERDR_PANE_ID": "w1:p1",
                    },
                ),
                clock,
            ).bootstrap(shotcaller_spec())
            assert store.connection.execute("SELECT COUNT(*) FROM squads").fetchone()[0] == 0

        champion_store, champion_clock, champion_worktree = champion_context(
            root, "champion"
        )
        options = champion_options(root)
        champion_runner = FakeHerdrRunner(champion_worktree)
        VisibleChampionLaunchService(
            champion_store,
            champion_adapter(options, champion_runner),
            options,
            champion_clock,
        ).launch(champion_spec(champion_worktree, "placement"))
        champion_store.close()

        shotcaller_layout = [
            call
            for call in shotcaller_runner.calls
            if call[:3]
            in {
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
            }
        ]
        champion_tabs = [
            call
            for call in champion_runner.calls
            if call[:3] == ("herdr", "tab", "create")
        ]
        champion_start = next(
            call
            for call in champion_runner.calls
            if call[:3] == ("herdr", "agent", "start")
        )
        assert shotcaller_layout == []
        assert len(champion_tabs) == 1
        assert champion_start[champion_start.index("--pane") + 1] == "w1:p99"
        assert champion_start[champion_start.index("--pane") + 1] != SHOTCALLER_PANE_ID
        assert not any(
            call[:3] == ("herdr", "pane", "split") for call in champion_runner.calls
        )
    print(
        "PASS: Shotcaller creation stays in-place without Squad/layout mutation; "
        "Champion creation owns one distinct new-tab root"
    )


if __name__ == "__main__":
    main()
