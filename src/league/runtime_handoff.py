"""Owner-authorized identity-only recovery after a process-preserving handoff.

The saved native inventory pins the previous terminal generation. Two live OS
observations must agree before any canonical write. This neither imports a new
agent nor needs display-publication records, and never completes task work.
"""
import json
from typing import Any, Mapping

from .restored_agent import SupervisorWatcherAdapter, restored_runtime_generation, _timestamp
from .storage_types import StorageRefusal


def reconcile_handoff(store: Any, before: Mapping[str, Any], *, multiplexer: Any, at: str,
                      watcher: Any = None, check_only: bool = False) -> dict[str, Any]:
    _timestamp(at)
    result = before.get('result') if isinstance(before, Mapping) else None
    prior = result.get('agents') if isinstance(result, Mapping) else None
    if (not isinstance(prior, list) or not prior or len(prior) > 1000
            or any(not isinstance(item, Mapping) for item in prior)):
        raise StorageRefusal('runtime_handoff_invalid', 'pre-handoff inventory is absent or unbounded')
    live = multiplexer.discover()
    prepared = []
    skipped = []
    watchers = watcher or SupervisorWatcherAdapter(store, at)
    rows = store.connection.execute('''SELECT r.*,a.agent_id,a.thread_id,a.role,a.callsign,a.routing_name
        FROM runtime_instances r JOIN agent_instances a ON a.agent_id=r.actor_agent_id
        WHERE r.backend_kind='herdr' AND r.status IN ('active','idle','failed')
        AND a.retired_at IS NULL AND a.role IN ('shotcaller','champion')''').fetchall()
    for raw in rows:
        row = dict(raw)
        matches = [item for item in prior if item.get('pane_id') == row['endpoint']
                   and item.get('name') == row['routing_name']
                   and (item.get('agent_session') or {}).get('value') == row['session_ref']]
        if len(matches) != 1:
            skipped.append({'runtime_instance_id': row['runtime_instance_id'], 'reason': 'outside_exact_handoff'})
            continue  # Outside this exact handoff; preserve unrelated legacy work.
        old = matches[0]
        current = [item for item in live if item.get('pane_id') == old.get('pane_id')]
        if len(current) != 1:
            raise StorageRefusal('runtime_handoff_changed', 'handoff pane is absent or ambiguous')
        current = current[0]
        keys = ('pane_id','name','agent','agent_session','workspace_id','tab_id')
        if any(current.get(key) != old.get(key) for key in keys):
            raise StorageRefusal('runtime_handoff_changed', 'handoff changed more than terminal generation')
        old_generation = restored_runtime_generation('herdr', old['terminal_id'], row['session_ref'])
        new_generation = restored_runtime_generation('herdr', current['terminal_id'], row['session_ref'])
        if row['runtime_generation'] not in {old_generation, new_generation}:
            skipped.append({'runtime_instance_id': row['runtime_instance_id'], 'reason': 'preexisting_generation_mismatch'})
            continue
        if row['thread_id'] != row['session_ref'] or row['harness_kind'].removesuffix('-thread') != current.get('agent'):
            raise StorageRefusal('runtime_handoff_changed', 'canonical provider identity changed')
        descriptor = {**row, 'cwd': current.get('foreground_cwd'), 'verify_native_process': True,
                      'tokens': {'sidebar_name': row['callsign']}}
        endpoint = multiplexer.endpoint(row['runtime_instance_id'], current)
        first = multiplexer.inspect_restored(descriptor, endpoint)
        second = multiplexer.inspect_restored(descriptor, endpoint)
        if (first['process_fingerprint'] != second['process_fingerprint']
            or any(obs['session_ref'] != row['session_ref']
                   or any(obs['agent'].get(key) != current.get(key) for key in (*keys, 'terminal_id'))
                   for obs in (first, second))):
            raise StorageRefusal('runtime_handoff_changed', 'live process changed during handoff verification')
        preflight = None
        if row['role'] == 'shotcaller':
            try:
                preflight = watchers.preflight(descriptor)
            except StorageRefusal as exc:
                if exc.code != 'restored_agent_watcher_mismatch' or row['runtime_generation'] != new_generation:
                    raise
                # Exact retry after the database committed but watcher rebind failed.
                preflight = watchers.preflight({**descriptor, 'runtime_generation': old_generation})
        prepared.append((row, descriptor, new_generation, preflight))
    if check_only:
        return {'schema': 'league.runtime-handoff.v1', 'verified': len(prepared), 'skipped': skipped,
                'runtime_instance_ids': [row['runtime_instance_id'] for row, *_ in prepared],
                'check_only': True, 'process_effects': False, 'task_completion_effects': False}
    receipts = []
    for row, descriptor, generation, preflight in prepared:
        receipt = store.reconcile_restored_runtime(row['runtime_instance_id'], row['agent_id'],
            row['thread_id'], row['session_ref'], 'herdr', row['endpoint'], row['runtime_generation'],
            row['endpoint'], generation, at, recover_observed_failure=True)
        if preflight is not None:
            try:
                watchers.bind(descriptor, receipt, preflight)
                watchers.verify(descriptor, receipt)
            except StorageRefusal:
                store.record_restored_runtime_recovery(row['runtime_instance_id'], row['agent_id'],
                    'handoff_watcher_pending', at, next_action='retry runtime reconcile-handoff with the original inventory')
                raise
            pending = store.connection.execute(
                "SELECT details_json FROM obligations WHERE dedupe_key=? AND state='open'",
                ('runtime-restore:' + row['runtime_instance_id'],)).fetchone()
            if pending is not None and json.loads(pending['details_json']).get('next_action') == \
                    'retry runtime reconcile-handoff with the original inventory':
                store.satisfy_restored_runtime_recovery(row['runtime_instance_id'], at)
        receipts.append(receipt)
    return {'schema': 'league.runtime-handoff.v1', 'reconciled': len(receipts), 'runtimes': receipts, 'skipped': skipped,
            'process_effects': False, 'task_completion_effects': False}
