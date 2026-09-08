"""Prompt-only policy does not disable existing work or replay skipped input."""

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.storage import StorageRefusal
from league.sqlite_request_ops import untriaged_intake
from request_lifecycle_fixture import create_context, GAREN_RUNTIME
from storage_fixture import SHOTCALLER_ID


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-triage-policy-") as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            def capture(name: str):
                return store.intake_prompt(
                    name, SHOTCALLER_ID, GAREN_RUNTIME, "codex", "synthetic",
                    name, "Explain the error and fix the failing test.", clock.now(),
                )

            assert store.prompt_triage_policy(SHOTCALLER_ID)["mode"] == "inline"
            capture("pending-before-switch")
            before = store.request_turn_boundary(SHOTCALLER_ID)["obligations"]
            off = store.configure_prompt_triage(SHOTCALLER_ID, False, 0, clock.now())
            assert off["mode"] == "off" and off["version"] == 1
            skipped = capture("received-while-off")
            assert skipped["triage_state"] == "skipped"
            assert capture("received-while-off")["idempotent"]
            after = store.request_turn_boundary(SHOTCALLER_ID)["obligations"]
            for name in ("active_champions", "pending_assignments", "pending_deliveries", "cleanup_obligations"):
                assert after[name] == before[name], name
            assert after["unresolved_requests"] == before["unresolved_requests"] - 1
            assert store.unresolved_requests(SHOTCALLER_ID)["untriaged_prompt_count"] == 0
            assert not store.untriaged_intake(SHOTCALLER_ID)["prompts"]
            on = store.configure_prompt_triage(SHOTCALLER_ID, True, 1, clock.now())
            assert on["mode"] == "background" and on["version"] == 2
            assert store.unresolved_requests(SHOTCALLER_ID)["untriaged_prompt_count"] == 1
            capture("new-background-prompt")
            intake = untriaged_intake(store, SHOTCALLER_ID, background=True)
            assert {p["prompt_id"] for p in intake["prompts"]} == {
                "pending-before-switch", "new-background-prompt",
            }
            assert capture("received-while-off")["triage_state"] == "skipped"
            try:
                store.configure_prompt_triage(SHOTCALLER_ID, False, 1, clock.now())
            except StorageRefusal as exc:
                assert exc.code == "version_conflict"
            else:
                raise AssertionError("stale configuration was accepted")
        finally:
            store.close()
    print("PASS: prompt-only ON/OFF preserves Champion obligations and skipped prompt identity")


if __name__ == "__main__":
    main()
