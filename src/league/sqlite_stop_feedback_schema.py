"""Durable one-time suppression for League's own Codex Stop feedback."""

from __future__ import annotations


MIGRATION_NAME = "exact-stop-feedback-suppression"

STATEMENTS = (
    "ALTER TABLE watcher_scopes ADD COLUMN pending_stop_feedback_digest TEXT",
    "ALTER TABLE watcher_scopes ADD COLUMN pending_stop_terminal_generation TEXT",
    "ALTER TABLE watcher_scopes ADD COLUMN pending_stop_wait_generation INTEGER CHECK (pending_stop_wait_generation IS NULL OR pending_stop_wait_generation >= 0)",
)
