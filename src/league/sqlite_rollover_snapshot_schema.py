"""Schema support for immutable switched-rollover snapshot revisions."""

from __future__ import annotations


MIGRATION_NAME = "immutable-switched-rollover-snapshot-revisions"

STATEMENTS = (
    "PRAGMA defer_foreign_keys=ON",
    "PRAGMA legacy_alter_table=ON",
    "ALTER TABLE active_champion_snapshot_rows RENAME TO active_champion_snapshot_rows_v16",
    "ALTER TABLE active_champion_snapshots RENAME TO active_champion_snapshots_v16",
    """
    CREATE TABLE active_champion_snapshots (
      snapshot_id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL REFERENCES rollover_operations(operation_id)
        DEFERRABLE INITIALLY DEFERRED,
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
      total_count INTEGER NOT NULL CHECK (total_count >= 0),
      page_bound INTEGER NOT NULL CHECK (page_bound BETWEEN 1 AND 500),
      expires_at TEXT NOT NULL,
      digest TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (operation_id,snapshot_version)
    )
    """,
    """
    CREATE TABLE active_champion_snapshot_rows (
      snapshot_id TEXT NOT NULL REFERENCES active_champion_snapshots(snapshot_id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
      champion_agent_id TEXT NOT NULL,
      task_id TEXT,
      callsign TEXT NOT NULL,
      binding_digest TEXT NOT NULL,
      row_digest TEXT NOT NULL,
      PRIMARY KEY (snapshot_id,ordinal),
      UNIQUE (snapshot_id,champion_agent_id)
    )
    """,
    """
    INSERT INTO active_champion_snapshots
      (snapshot_id,operation_id,squad_id,snapshot_version,total_count,page_bound,
       expires_at,digest,created_at)
    SELECT snapshot_id,operation_id,squad_id,snapshot_version,total_count,page_bound,
           expires_at,digest,created_at
      FROM active_champion_snapshots_v16
    """,
    """
    INSERT INTO active_champion_snapshot_rows
      (snapshot_id,ordinal,champion_agent_id,task_id,callsign,binding_digest,row_digest)
    SELECT snapshot_id,ordinal,champion_agent_id,task_id,callsign,binding_digest,row_digest
      FROM active_champion_snapshot_rows_v16
    """,
    "DROP TABLE active_champion_snapshot_rows_v16",
    "DROP TABLE active_champion_snapshots_v16",
    "CREATE INDEX ix_rollover_snapshot_revisions ON active_champion_snapshots(operation_id,snapshot_version)",
    "PRAGMA legacy_alter_table=OFF",
)
