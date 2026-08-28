"""Reviewed v6 schema statements for callsign queueing and guarded rollover."""

from __future__ import annotations


MIGRATION_NAME = "guarded-rollover-and-shuffled-callsign-queue"
SHUFFLE_VERSION = 1
CHAMPION_SEED = "league.callsign.queue.v1:champion"
SHOTCALLER_SEED = "league.callsign.queue.v1:shotcaller"
HIDDEN_WORKER_SEED = "league.callsign.queue.v1:hidden-worker"


STATEMENTS = (
    "ALTER TABLE callsigns ADD COLUMN capability_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE runtime_instances ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE squads ADD COLUMN owner_fence INTEGER NOT NULL DEFAULT 1",
    """
    CREATE TABLE callsign_queue_meta (
      pool_role TEXT PRIMARY KEY CHECK (pool_role IN ('shotcaller','champion','hidden-worker')),
      seed TEXT NOT NULL,
      shuffle_version INTEGER NOT NULL CHECK (shuffle_version > 0),
      queue_version INTEGER NOT NULL CHECK (queue_version > 0),
      initialized_at TEXT NOT NULL
    )
    """,
    f"""
    INSERT INTO callsign_queue_meta(pool_role,seed,shuffle_version,queue_version,initialized_at)
    SELECT role,CASE role
      WHEN 'shotcaller' THEN '{SHOTCALLER_SEED}'
      WHEN 'champion' THEN '{CHAMPION_SEED}'
      ELSE '{HIDDEN_WORKER_SEED}' END,
      {SHUFFLE_VERSION},1,'1970-01-01T00:00:00Z'
      FROM (SELECT 'shotcaller' role UNION ALL SELECT 'champion' UNION ALL SELECT 'hidden-worker')
    """,
    """
    CREATE TABLE callsign_capabilities (
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      capability TEXT NOT NULL,
      PRIMARY KEY (callsign,capability)
    )
    """,
    """
    CREATE TABLE callsign_queue (
      callsign TEXT PRIMARY KEY REFERENCES callsigns(callsign),
      pool_role TEXT NOT NULL REFERENCES callsign_queue_meta(pool_role),
      queue_position INTEGER,
      state TEXT NOT NULL CHECK (state IN ('available','reserved','active')),
      reservation_assignment_id TEXT,
      version INTEGER NOT NULL CHECK (version > 0),
      updated_at TEXT NOT NULL,
      CHECK ((state='active' AND queue_position IS NULL) OR
             (state<>'active' AND queue_position IS NOT NULL)),
      CHECK ((state='reserved') = (reservation_assignment_id IS NOT NULL)),
      UNIQUE (pool_role,queue_position)
    )
    """,
    """
    WITH classified AS (
      SELECT c.callsign,c.pool_role,m.seed,
             CASE WHEN l.callsign IS NULL THEN 'available'
                  WHEN a.agent_id IS NULL OR a.kind='unbound' THEN 'reserved'
                  ELSE 'active' END queue_state,
             COALESCE(l.reserved_at,c.last_released_at,'1970-01-01T00:00:00Z') changed_at
        FROM callsigns c
        JOIN callsign_queue_meta m ON m.pool_role=c.pool_role
        LEFT JOIN callsign_leases l ON l.callsign=c.callsign
        LEFT JOIN agent_instances a ON a.agent_id=l.agent_id
    ), positioned AS (
      SELECT callsign,pool_role,queue_state,changed_at,
             CASE WHEN queue_state='active' THEN NULL ELSE
               ROW_NUMBER() OVER (
                 PARTITION BY pool_role
                 ORDER BY league_shuffle_key(seed,pool_role,callsign),callsign
               ) - 1 END queue_position
        FROM classified
    )
    INSERT INTO callsign_queue
      (callsign,pool_role,queue_position,state,reservation_assignment_id,version,updated_at)
    SELECT p.callsign,p.pool_role,p.queue_position,p.queue_state,
           CASE WHEN p.queue_state='reserved' THEN
             COALESCE((SELECT 'callsign-assignment:'||ta.task_assignment_id
                         FROM callsign_leases l
                         JOIN task_assignments ta ON ta.champion_agent_id=l.agent_id
                        WHERE l.callsign=p.callsign LIMIT 1),
                      'legacy-reservation:'||p.callsign)
                ELSE NULL END,
           1,p.changed_at
      FROM positioned p
    """,
    """
    WITH ordered AS (
      SELECT pool_role,callsign,
             LAG(callsign) OVER (PARTITION BY pool_role ORDER BY queue_position) previous
        FROM callsign_queue WHERE queue_position IS NOT NULL
    ), pool_order AS (
      SELECT pool_role,COUNT(*) queue_count,
             SUM(CASE WHEN previous IS NOT NULL AND previous>callsign THEN 1 ELSE 0 END)
               inversions
        FROM ordered GROUP BY pool_role
    )
    UPDATE callsign_queue SET queue_position=queue_position+1000000
     WHERE pool_role IN (
       SELECT pool_role FROM pool_order WHERE queue_count>1 AND inversions=0
     )
    """,
    """
    WITH pool_counts AS (
      SELECT pool_role,COUNT(*) queue_count FROM callsign_queue
       WHERE queue_position IS NOT NULL GROUP BY pool_role
    )
    UPDATE callsign_queue AS queue
       SET queue_position=(queue.queue_position-1000000-1+pool_counts.queue_count)
                          %pool_counts.queue_count
      FROM pool_counts
     WHERE queue.queue_position>=1000000
       AND pool_counts.pool_role=queue.pool_role
    """,
    "ALTER TABLE callsign_assignments RENAME TO callsign_assignments_legacy",
    """
    CREATE TABLE callsign_assignments (
      callsign_assignment_id TEXT PRIMARY KEY,
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      subject_id TEXT NOT NULL,
      agent_id TEXT REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
      role TEXT NOT NULL CHECK (role IN ('shotcaller','champion','hidden-worker')),
      scope_kind TEXT NOT NULL CHECK (scope_kind IN ('squad','task','worker')),
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
       role,scope_kind,scope_id,state,
       reservation_position,queue_version,requirements_json,acceptance_digest,
       release_receipt_digest,failure_receipt_digest,version,reserved_at,activated_at,released_at)
    SELECT old.callsign_assignment_id,old.callsign,'agent:'||old.agent_id,old.agent_id,NULL,
           ai.role,'task',old.task_id,
           CASE old.state WHEN 'blocked' THEN 'blocked' ELSE old.state END,
           q.queue_position,1,'[]',NULL,NULL,NULL,1,
           old.reserved_at,old.activated_at,old.released_at
      FROM callsign_assignments_legacy old
      JOIN agent_instances ai ON ai.agent_id=old.agent_id
      JOIN callsign_queue q ON q.callsign=old.callsign
    """,
    """
    INSERT INTO callsign_assignments
      (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
       role,scope_kind,scope_id,state,
       reservation_position,queue_version,requirements_json,acceptance_digest,
       release_receipt_digest,failure_receipt_digest,version,reserved_at,activated_at,released_at)
    SELECT CASE WHEN q.state='reserved' THEN q.reservation_assignment_id
                ELSE 'migrated-live:'||l.callsign END,l.callsign,
           CASE WHEN l.agent_id IS NULL THEN 'attempt:'||COALESCE(l.launch_attempt_id,l.callsign)
                ELSE 'agent:'||l.agent_id END,
           l.agent_id,NULL,c.pool_role,
           CASE c.pool_role WHEN 'shotcaller' THEN 'squad'
                            WHEN 'champion' THEN 'task' ELSE 'worker' END,
           COALESCE(ai.task_id,(SELECT s.squad_id FROM squads s WHERE s.shotcaller_agent_id=l.agent_id),
                    l.launch_attempt_id,'legacy:'||l.callsign),
           q.state,q.queue_position,1,'[]',NULL,NULL,NULL,1,l.reserved_at,
           CASE WHEN q.state='active' THEN l.reserved_at ELSE NULL END,NULL
      FROM callsign_leases l
      JOIN callsigns c ON c.callsign=l.callsign
      JOIN callsign_queue q ON q.callsign=l.callsign
      LEFT JOIN agent_instances ai ON ai.agent_id=l.agent_id
     WHERE NOT EXISTS (
       SELECT 1 FROM callsign_assignments current_assignment
        WHERE current_assignment.callsign=l.callsign
          AND current_assignment.state IN ('reserved','active')
     )
    """,
    "DROP TABLE callsign_assignments_legacy",
    "CREATE INDEX ix_callsign_queue_scan ON callsign_queue(pool_role,state,queue_position,callsign)",
    "CREATE INDEX ix_callsign_assignments_callsign_state ON callsign_assignments(callsign,state,reserved_at)",
    "CREATE UNIQUE INDEX ux_runtime_session_identity ON runtime_instances(harness_kind,session_ref)",
    """
    CREATE TABLE shotcaller_intake (
      agent_id TEXT PRIMARY KEY REFERENCES agent_instances(agent_id),
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      state TEXT NOT NULL CHECK (state IN ('accepting','draining','closed')),
      fence INTEGER NOT NULL CHECK (fence > 0),
      version INTEGER NOT NULL CHECK (version > 0),
      updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX ux_squad_accepting_shotcaller ON shotcaller_intake(squad_id) WHERE state='accepting'",
    """
    INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
    SELECT s.shotcaller_agent_id,s.squad_id,'accepting',s.owner_fence,1,s.updated_at FROM squads s
    """,
    """
    CREATE TABLE squad_champions (
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      champion_agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id),
      joined_at TEXT NOT NULL,
      PRIMARY KEY (squad_id,champion_agent_id)
    )
    """,
    """
    INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at)
    SELECT s.squad_id,a.agent_id,a.updated_at
      FROM squads s JOIN agent_instances a ON a.shotcaller_agent_id=s.shotcaller_agent_id
     WHERE a.role='champion' AND a.retired_at IS NULL
    """,
    """
    CREATE TABLE rollover_operations (
      operation_id TEXT PRIMARY KEY,
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      predecessor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      successor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      callsign_assignment_id TEXT NOT NULL UNIQUE REFERENCES callsign_assignments(callsign_assignment_id),
      state TEXT NOT NULL CHECK (state IN ('prepared','acknowledged','switched','completed','aborted')),
      authority_kind TEXT NOT NULL CHECK (authority_kind IN ('explicit','automatic')),
      authority_digest TEXT NOT NULL,
      required_capabilities_json TEXT NOT NULL,
      plan_json TEXT NOT NULL,
      plan_digest TEXT NOT NULL,
      handoff_digest TEXT NOT NULL,
      expected_owner_version INTEGER NOT NULL CHECK (expected_owner_version > 0),
      expected_owner_fence INTEGER NOT NULL CHECK (expected_owner_fence > 0),
      snapshot_id TEXT NOT NULL UNIQUE,
      acknowledgement_digest TEXT,
      successor_runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
      owner_event_id TEXT UNIQUE REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED,
      owner_outbox_id TEXT UNIQUE REFERENCES delivery_outbox(outbox_id) DEFERRABLE INITIALLY DEFERRED,
      switch_receipt_digest TEXT,
      cleanup_receipt_digest TEXT,
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE active_champion_snapshots (
      snapshot_id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL UNIQUE REFERENCES rollover_operations(operation_id)
        DEFERRABLE INITIALLY DEFERRED,
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
      total_count INTEGER NOT NULL CHECK (total_count >= 0),
      page_bound INTEGER NOT NULL CHECK (page_bound BETWEEN 1 AND 500),
      expires_at TEXT NOT NULL,
      digest TEXT NOT NULL,
      created_at TEXT NOT NULL
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
    "PRAGMA defer_foreign_keys=ON",
    """
    CREATE TABLE events_v6 (
      event_id TEXT PRIMARY KEY,
      agent_id TEXT REFERENCES agent_instances(agent_id),
      task_id TEXT REFERENCES tasks(task_id),
      squad_id TEXT REFERENCES squads(squad_id),
      entity_version INTEGER NOT NULL CHECK (entity_version > 0),
      event_type TEXT NOT NULL,
      status TEXT,
      update_text TEXT NOT NULL,
      occurred_at TEXT NOT NULL,
      detail_json TEXT NOT NULL DEFAULT '{}',
      request_id TEXT REFERENCES requests(request_id),
      aggregate_kind TEXT,
      aggregate_id TEXT,
      event_seq INTEGER,
      source_event_id TEXT,
      CHECK ((agent_id IS NOT NULL) + (task_id IS NOT NULL) + (squad_id IS NOT NULL) = 1)
    )
    """,
    """
    INSERT INTO events_v6
      (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
       occurred_at,detail_json,request_id,aggregate_kind,aggregate_id,event_seq,source_event_id)
    SELECT event_id,agent_id,task_id,NULL,entity_version,event_type,status,update_text,
           occurred_at,detail_json,request_id,aggregate_kind,aggregate_id,event_seq,source_event_id
      FROM events
    """,
    "DROP TABLE events",
    "ALTER TABLE events_v6 RENAME TO events",
    """
    CREATE UNIQUE INDEX ux_agent_event_version
      ON events(agent_id,entity_version)
      WHERE agent_id IS NOT NULL AND event_type IN
        ('agent_transition','callsign_reserved','callsign_activated','callsign_released',
         'callsign_reservation_rolled_back','legacy_transition')
    """,
    """
    CREATE UNIQUE INDEX ux_task_event_version
      ON events(task_id,entity_version)
      WHERE task_id IS NOT NULL AND event_type='task_owner_transferred'
    """,
    "CREATE UNIQUE INDEX ux_events_event_seq ON events(event_seq)",
    "CREATE INDEX ix_events_aggregate ON events(aggregate_kind,aggregate_id,event_seq)",
    "CREATE INDEX ix_events_occurred ON events(occurred_at,event_id)",
    """
    CREATE TRIGGER events_fill_sequence
    AFTER INSERT ON events WHEN NEW.event_seq IS NULL
    BEGIN
      UPDATE events SET event_seq=NEW.rowid WHERE rowid=NEW.rowid;
    END
    """,
    "CREATE UNIQUE INDEX ux_owner_changed_per_rollover ON events(aggregate_kind,aggregate_id,entity_version) WHERE event_type='owner_changed'",
)
