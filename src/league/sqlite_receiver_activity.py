"""Receiver work boundaries, independent of semantic request transactions.

Call inside the caller's storage transaction. Silence or lease expiry is not
proof of idle: only an allowed Stop clears observed work. A crashed receiver
therefore stays conservative until its supported recovery/Stop boundary.
"""

import json

from .sqlite_request_ops import _time
from .storage_types import StorageRefusal


def require_no_known_work(store, owner):
    """Recheck immediately before transport, including an already-claimed outbox."""
    from .sqlite_watcher_ops import resolve_supervisor_scope

    if store.connection.execute(
        "SELECT 1 FROM agent_instances WHERE agent_id=? AND role='shotcaller' AND retired_at IS NULL",
        (owner,),
    ).fetchone() is None:
        return
    scope = resolve_supervisor_scope(store, owner)
    row = store.connection.execute('SELECT metadata_json FROM watcher_scopes WHERE scope_id=?',
                                   (scope['scope_id'],)).fetchone()
    if row and json.loads(row['metadata_json']).get('receiver_activity', {}).get('state') == 'busy':
        raise StorageRefusal('receiver_busy', 'observed work requires in-turn inbox delivery')


def mark_busy(store, owner, at, *, scope_id=None):
    from .sqlite_watcher_ops import resolve_supervisor_scope

    _time(at, 'receiver activity time')
    actor = store.connection.execute(
        "SELECT 1 FROM agent_instances WHERE agent_id=? AND role='shotcaller' AND retired_at IS NULL",
        (owner,),
    ).fetchone()
    if actor is None:
        return
    if scope_id is None:
        scope_id = resolve_supervisor_scope(store, owner)['scope_id']
    row = store.connection.execute(
        'SELECT metadata_json FROM watcher_scopes WHERE scope_id=? AND actor_agent_id=?',
        (scope_id, owner),
    ).fetchone()
    if row is None:
        return
    metadata = json.loads(row['metadata_json'])
    prior = metadata.get('receiver_activity', {})
    # A delayed callback must not move the work boundary backwards.
    if prior.get('at') and _time(prior['at'], 'prior activity') > _time(at, 'activity'):
        return
    metadata['receiver_activity'] = {'state': 'busy', 'at': at}
    store.connection.execute('UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?',
                             (json.dumps(metadata, separators=(',', ':')), scope_id))


def finish_work(store, scope_id, owner, at):
    row = store.connection.execute(
        'SELECT metadata_json FROM watcher_scopes WHERE scope_id=? AND actor_agent_id=?',
        (scope_id, owner),
    ).fetchone()
    if row is None:
        return
    metadata = json.loads(row['metadata_json'])
    prior = metadata.get('receiver_activity')
    if not isinstance(prior, dict) or prior.get('state') != 'busy':
        return
    if _time(prior['at'], 'receiver activity') > _time(at, 'Stop boundary'):
        return
    metadata['receiver_activity'] = {'state': 'idle', 'at': at}
    store.connection.execute('UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?',
                             (json.dumps(metadata, separators=(',', ':')), scope_id))
