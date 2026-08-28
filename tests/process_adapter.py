"""Explicit environment injection for the shared synthetic ``ps`` adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]


def fake_process_environment(
    temporary_root: Path,
    *,
    mode: str = "tracked",
    base: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    if mode not in {"stable", "tracked"}:
        raise ValueError("unsupported fake process adapter mode")
    marker_root = temporary_root / "fake-ps-markers"
    marker_root.mkdir(parents=True, exist_ok=True)
    environment = dict(base or os.environ)
    current_path = environment.get("PATH")
    environment.update(
        {
            "PATH": (
                f"{ROOT / 'tests/fakes'}{os.pathsep}{current_path}"
                if current_path
                else str(ROOT / "tests/fakes")
            ),
            "LEAGUE_TEST_PS_MODE": mode,
            "LEAGUE_TEST_PS_MARKER_ROOT": str(marker_root),
        }
    )
    return environment
