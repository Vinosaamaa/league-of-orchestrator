"""Schema support for a Shotcaller identity before any Squad exists."""

from __future__ import annotations


MIGRATION_NAME = "standalone-shotcaller-callsign-scope"

STATEMENTS = (
    "PRAGMA defer_foreign_keys=ON",
    "PRAGMA legacy_alter_table=ON",
    "ALTER TABLE callsign_assignments RENAME TO callsign_assignments_v13",
    """
    CREATE TABLE callsign_assignments (
      callsign_assignment_id TEXT PRIMARY KEY,
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      subject_id TEXT NOT NULL,
      agent_id TEXT REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
      role TEXT NOT NULL CHECK (role IN ('shotcaller','champion','hidden-worker')),
      scope_kind TEXT NOT NULL CHECK (scope_kind IN ('shotcaller','squad','task','worker')),
      scope_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('reserved','active','released','rolled_back','blocked')),
      reservation_position INTEGER,
      queue_version INTEGER NOT NULL CHECK (queue_version > 0),
      requirements_json TEXT NOT NULL DEFAULT '[]',
      acceptance_digest TEXT,
      release_receipt_digest TEXT,
      failure_receipt_digest TEXT,
      version INTEGER NOT NULL CHECK (version > 0),
      reserved_at TEXT NOT NULL,
      activated_at TEXT,
      released_at TEXT,
      UNIQUE (subject_id)
    )
    """,
    """
    INSERT INTO callsign_assignments
      (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
       role,scope_kind,scope_id,state,reservation_position,queue_version,
       requirements_json,acceptance_digest,release_receipt_digest,
       failure_receipt_digest,version,reserved_at,activated_at,released_at)
    SELECT callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
           role,scope_kind,scope_id,state,reservation_position,queue_version,
           requirements_json,acceptance_digest,release_receipt_digest,
           failure_receipt_digest,version,reserved_at,activated_at,released_at
      FROM callsign_assignments_v13
    """,
    "DROP TABLE callsign_assignments_v13",
    "CREATE INDEX ix_callsign_assignments_callsign_state ON callsign_assignments(callsign,state,reserved_at)",
    "CREATE INDEX ix_report_callsign_reserved ON callsign_assignments(reserved_at,callsign_assignment_id)",
    "CREATE INDEX ix_report_callsign_activated ON callsign_assignments(activated_at,callsign_assignment_id) WHERE activated_at IS NOT NULL",
    "CREATE INDEX ix_report_callsign_released ON callsign_assignments(released_at,callsign_assignment_id) WHERE released_at IS NOT NULL",
    "PRAGMA legacy_alter_table=OFF",
)
