"""Bounded in-turn receive using the existing outbox lease and receipt protocol."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Any
import uuid

from . import sqlite_outbox_ops as outbox
from .sqlite_request_ops import _time
from .storage_outbox import OutboxDispatchIdentity
from .storage import StorageRefusal


def _runtime(store: Any, owner: str, runtime: str) -> None:
    row = store.connection.execute(
        "SELECT 1 FROM runtime_instances r JOIN agent_instances a ON a.agent_id=r.actor_agent_id "
        "WHERE r.runtime_instance_id=? AND r.actor_agent_id=? AND r.verified=1 "
        "AND r.status IN ('active','idle') AND a.role='shotcaller' AND a.retired_at IS NULL",
        (runtime, owner),
    ).fetchone()
    if row is None:
        raise StorageRefusal("runtime_unverified", "inbox requires the exact verified Shotcaller runtime")


def foreground_wait(store: Any, scope: str, owner: str, runtime: str, token: str,
                    at: str, *, release: bool = False) -> None:
    """Lease only an outstanding receive tool, not general supervision state."""
    now = _time(at, 'foreground receive time')
    with store._transaction():
        _runtime(store, owner, runtime)
        row = store.connection.execute(
            'SELECT metadata_json FROM watcher_scopes WHERE scope_id=? AND actor_agent_id=?',
            (scope, owner),
        ).fetchone()
        if row is None:
            raise StorageRefusal('scope_conflict', 'foreground receive scope is not owned')
        metadata = json.loads(row['metadata_json'])
        prior = metadata.get('foreground_inbox')
        if release:
            if not isinstance(prior, dict) or prior.get('token') != token:
                return
            metadata.pop('foreground_inbox')
        else:
            if (isinstance(prior, dict) and prior.get('token') != token
                and _time(prior['expires_at'], 'foreground receive expiry') > now):
                raise StorageRefusal('wait_active', 'another foreground receive owns the lease')
            metadata['foreground_inbox'] = {
                'token': token, 'runtime_instance_id': runtime,
                'expires_at': (now + timedelta(seconds=30)).isoformat(),
            }
        store.connection.execute('UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?',
                                 (json.dumps(metadata, separators=(',', ':')), scope))


def read(store: Any, owner: str, runtime: str, at: str, limit: int = 10) -> dict[str, Any]:
    """Present updates, not completion. A short lease prevents concurrent push.

    An interrupted reader leaves no false receipt: unacknowledged leases expire
    through the existing outbox recovery path. Controls retain their executor.
    """
    now = _time(at, "inbox read time")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise StorageRefusal("invalid_limit", "inbox limit must be between 1 and 20")
    expires = (now + timedelta(seconds=60)).isoformat()
    items = []
    with store._transaction():
        _runtime(store, owner, runtime)
        rows = store.connection.execute(
            "SELECT o.outbox_id,o.event_id FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id "
            "WHERE o.recipient_agent_id=? AND o.state IN ('pending','in_flight') "
            "AND julianday(o.available_at)<=julianday(?) AND e.event_type!='owner_stop_control' "
            "AND NOT EXISTS (SELECT 1 FROM outbox_dispatch_leases l WHERE l.outbox_id=o.outbox_id "
            "AND julianday(l.leased_until)>julianday(?)) "
            "ORDER BY o.available_at,o.outbox_id LIMIT ?", (owner, at, at, limit),
        ).fetchall()
        for row in rows:
            identity = OutboxDispatchIdentity(
                row['outbox_id'], row['event_id'], owner,
                'inbox:' + runtime, 'inbox-attempt:' + str(uuid.uuid4()),
            )
            claim = outbox.claim_outbox(store, identity, expires, at)
            if claim['state'] != 'in_flight':
                continue
            inspected = outbox.inspect_outbox(store, row['outbox_id'], row['event_id'], owner)
            items.append({**inspected, 'attempt_id': identity.attempt_id, 'fence': claim['fence']})
    return {'owner_agent_id': owner, 'runtime_instance_id': runtime,
            'lease_expires_at': expires, 'items': items}


def acknowledge(store: Any, receipt: dict[str, Any], at: str) -> dict[str, Any]:
    """Explicit tool-result receipt; never classify or mark a request answered."""
    now = _time(at, "inbox acknowledgement time")
    if not isinstance(receipt, dict) or not isinstance(receipt.get('items'), list):
        raise StorageRefusal('invalid_delivery', 'inbox acknowledgement requires its read receipt')
    owner, runtime = receipt.get('owner_agent_id'), receipt.get('runtime_instance_id')
    if not isinstance(owner, str) or not isinstance(runtime, str) or len(receipt['items']) > 20:
        raise StorageRefusal('invalid_delivery', 'inbox receipt identity or count is invalid')
    results = []
    with store._transaction():
        _runtime(store, owner, runtime)
        for item in receipt['items']:
            if not isinstance(item, dict) or not isinstance(item.get('envelope'), dict):
                raise StorageRefusal('invalid_delivery', 'inbox item is invalid')
            if type(item.get('fence')) is not int or item['fence'] < 1 or not isinstance(item.get('attempt_id'), str):
                raise StorageRefusal('invalid_delivery', 'inbox item has no exact dispatch claim')
            env = item['envelope']
            inspected = outbox.inspect_outbox(store, env.get('outbox_id'), env.get('event_id'), owner)
            if inspected != {'envelope': env, 'envelope_sha256': item.get('envelope_sha256')}:
                raise StorageRefusal('source_event_mismatch', 'inbox receipt does not match its source')
            if env['event_type'] == 'owner_stop_control':
                raise StorageRefusal('delivery_control_effect_required', 'reading a control is not execution')
            identity = OutboxDispatchIdentity(env['outbox_id'], env['event_id'], owner,
                                             'inbox:' + runtime, item.get('attempt_id'))
            lease = store.connection.execute(
                'SELECT leased_until FROM outbox_dispatch_leases WHERE outbox_id=?', (env['outbox_id'],),
            ).fetchone()
            if lease is not None and _time(lease['leased_until'], 'inbox lease expiry') <= now:
                raise StorageRefusal('delivery_fenced', 'inbox read expired; read the pending update again')
            effect = hashlib.sha256((runtime + ':' + item['envelope_sha256']).encode()).hexdigest()
            results.append(outbox.acknowledge_outbox(
                store, identity, item.get('fence', 0), 'inbox', 'recipient_read', effect, at,
            ))
    return {'owner_agent_id': owner, 'received': results}
