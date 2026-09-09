"""A busy recipient consumes durable updates without invoking a prompt adapter."""

from pathlib import Path
from argparse import Namespace
from unittest.mock import patch
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.sqlite_inbox_ops import read, acknowledge, foreground_wait
from league.sqlite_prompt_triage_ops import next_input, commit
from league.storage import StorageRefusal
from request_lifecycle_fixture import create_context, GAREN_RUNTIME, SyntheticLifecycleSeeder
from storage_fixture import SHOTCALLER_ID, CHAMPION_ID
from league.sqlite_receiver_activity import finish_work
from league.canonical_delivery import dispatch_event


def test_cli_private_receipt():
    from league.cli import _delivery_inbox, _delivery_ack_inbox

    with tempfile.TemporaryDirectory(prefix='league-inbox-receipt-') as temporary:
        root = Path(temporary)
        _, store, clock = create_context(root)
        try:
            SyntheticLifecycleSeeder(store, clock).add_pending_delivery(
                event_id='event:compact', outbox_id='outbox:compact',
                recipient_agent_id=SHOTCALLER_ID, source_agent_id=SHOTCALLER_ID,
                update='Synthetic source accepted.')
            path = root / 'receipt.json'
            args = Namespace(owner_agent_id=SHOTCALLER_ID, runtime_instance_id=GAREN_RUNTIME,
                             at=clock.now(), limit=10, receipt_file=path)
            sentinel = root / 'preserved.json'
            sentinel.write_text('preserve')
            link = root / 'link.json'
            link.symlink_to(sentinel)
            for invalid in (Path('relative.json'), sentinel, link, root / 'absent' / 'receipt.json'):
                args.receipt_file = invalid
                try:
                    _delivery_inbox(store, args)
                except StorageRefusal as exc:
                    assert exc.code == 'invalid_receipt_path', exc.code
                else:
                    raise AssertionError('unsafe receipt path accepted')
            assert sentinel.read_text() == 'preserve'
            assert not store.connection.execute('SELECT 1 FROM outbox_dispatch_leases').fetchall()
            args.receipt_file = path
            compact, _ = _delivery_inbox(store, args)
            assert path.stat().st_mode & 0o777 == 0o600
            saved = json.loads(path.read_text())
            assert len(saved['items']) == 1
            assert compact['items'] == [{'event_id': 'event:compact', 'kind': 'agent_transition',
                                         'summary': 'Synthetic source accepted.'}]
            assert compact['receipt_file'] == str(path)
            assert 'envelope_sha256' not in json.dumps(compact)
            assert store.connection.execute("SELECT state FROM delivery_outbox WHERE outbox_id='outbox:compact'").fetchone()[0] == 'in_flight'
            result, _ = _delivery_ack_inbox(store, Namespace(receipt=path, at=clock.now()))
            assert len(result['received']) == 1
            assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
            SyntheticLifecycleSeeder(store, clock).add_pending_delivery(
                event_id='event:write-failure', outbox_id='outbox:write-failure',
                recipient_agent_id=SHOTCALLER_ID, source_agent_id=CHAMPION_ID,
                update='Synthetic pending update.')
            args.receipt_file = root / 'failed-receipt.json'
            with patch('league.cli.os.fsync', side_effect=OSError('synthetic disk failure')):
                try:
                    _delivery_inbox(store, args)
                except StorageRefusal as exc:
                    assert exc.code == 'receipt_write_failed', exc.code
                else:
                    raise AssertionError('failed durable write accepted')
            assert store.connection.execute("SELECT state FROM delivery_outbox WHERE outbox_id='outbox:write-failure'").fetchone()[0] == 'in_flight'
        finally:
            store.close()


def test_background_busy_boundary():
    with tempfile.TemporaryDirectory(prefix='league-receiver-work-') as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            store.configure_prompt_triage(SHOTCALLER_ID, True, 0, clock.now())
            store.intake_prompt('P:busy', SHOTCALLER_ID, GAREN_RUNTIME, 'codex', 'synthetic',
                                'input:busy', 'Explain two synthetic failures.', clock.now())
            scope = store.connection.execute(
                'SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=?',
                (SHOTCALLER_ID,),
            ).fetchone()['scope_id']
            # Reproduce the installed case: the old inline transaction is committed.
            with store._transaction():
                row = store.connection.execute('SELECT metadata_json FROM watcher_scopes WHERE scope_id=?',
                                               (scope,)).fetchone()
                metadata = json.loads(row['metadata_json'])
                metadata['shotcaller_turn'] = {'active': False, 'committed': True}
                store.connection.execute('UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?',
                                         (json.dumps(metadata), scope))
            SyntheticLifecycleSeeder(store, clock).add_pending_delivery(
                event_id='event:busy', outbox_id='outbox:busy', recipient_agent_id=SHOTCALLER_ID,
                source_agent_id=SHOTCALLER_ID, update='Synthetic accepted source.')

            class NoPrompt:
                def send(self, *args):
                    raise AssertionError('busy work must not invoke even an idle-reporting adapter')

            def dispatch():
                return dispatch_event(store, outbox_id='outbox:busy', event_id='event:busy',
                                      recipient_agent_id=SHOTCALLER_ID, at=clock.now(), adapter=NoPrompt())

            assert dispatch()['reason'] == 'receiver_work_active'
            from league.canonical_delivery import InstalledDeliveryAdapter
            from league.request_services import DeliveryUnavailable

            try:
                InstalledDeliveryAdapter(store=store, at=clock.now()).send('direct',
                    {'runtime_instance_id': GAREN_RUNTIME, 'generation': 'synthetic',
                     'harness_kind': 'codex-thread'},
                    {'event_id': 'event:busy', 'outbox_id': 'outbox:busy',
                     'recipient_agent_id': SHOTCALLER_ID, 'event_type': 'agent_transition'})
            except DeliveryUnavailable as exc:
                assert str(exc) == 'receiver_busy'
            else:
                raise AssertionError('claimed transport ignored newly observed receiver work')
            clock.advance(600)
            assert dispatch()['reason'] == 'receiver_work_active', 'silence is not idle'
            result = store.stop_decision(scope, SHOTCALLER_ID, 'terminal:blocked', clock.now())
            assert result['decision'] == 'block'
            assert dispatch()['reason'] == 'receiver_work_active'
            prior_stop = clock.now()
            clock.advance(1)
            received = read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())
            assert len(received['items']) == 1
            with store._transaction():
                finish_work(store, scope, SHOTCALLER_ID, prior_stop)
            acknowledge(store, received, clock.now())
            activity = json.loads(store.connection.execute(
                'SELECT metadata_json FROM watcher_scopes WHERE scope_id=?', (scope,),
            ).fetchone()['metadata_json'])['receiver_activity']
            assert activity['state'] == 'busy', 'old Stop or acknowledgement cleared newer work'
            store.set_allow_stop_once(scope, SHOTCALLER_ID)
            assert store.stop_decision(scope, SHOTCALLER_ID, 'terminal:allowed', clock.now())['decision'] == 'allow'
            activity = json.loads(store.connection.execute(
                'SELECT metadata_json FROM watcher_scopes WHERE scope_id=?', (scope,),
            ).fetchone()['metadata_json'])['receiver_activity']
            assert activity['state'] == 'idle'
            SyntheticLifecycleSeeder(store, clock).add_pending_delivery(
                event_id='event:idle', outbox_id='outbox:idle', recipient_agent_id=SHOTCALLER_ID,
                source_agent_id=CHAMPION_ID, update='Next synthetic update.')
            assert store.apply_supervision_delivery_policy(
                'outbox:idle', 'event:idle', SHOTCALLER_ID, clock.now())['action'] == 'wake'
            # The real acknowledgement path reserves the receiver after one native wake.
            from league.sqlite_outbox_ops import claim_outbox, acknowledge_outbox
            from league.storage_outbox import OutboxDispatchIdentity

            identity = OutboxDispatchIdentity('outbox:idle', 'event:idle', SHOTCALLER_ID,
                                             'dispatcher:test', 'attempt:test')
            claim = claim_outbox(store, identity, clock.after(60), clock.now())
            acknowledge_outbox(store, identity, claim['fence'], 'codex', 'direct_prompt',
                               'effect:test', clock.now())
            activity = json.loads(store.connection.execute(
                'SELECT metadata_json FROM watcher_scopes WHERE scope_id=?', (scope,),
            ).fetchone()['metadata_json'])['receiver_activity']
            assert activity['state'] == 'busy', 'native wake must reserve subsequent updates'
            with store._transaction():
                finish_work(store, scope, SHOTCALLER_ID, clock.now())
            # Even an empty checkpoint means this caller is working again.
            assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
            activity = json.loads(store.connection.execute(
                'SELECT metadata_json FROM watcher_scopes WHERE scope_id=?', (scope,),
            ).fetchone()['metadata_json'])['receiver_activity']
            assert activity['state'] == 'busy'
        finally:
            store.close()


def main():
    test_cli_private_receipt()
    test_background_busy_boundary()
    with tempfile.TemporaryDirectory(prefix='league-inbox-') as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            seeder = SyntheticLifecycleSeeder(store, clock)
            seeder.add_pending_delivery(event_id='event:inbox-test', outbox_id='outbox:inbox-test',
                                       recipient_agent_id=SHOTCALLER_ID, source_agent_id=SHOTCALLER_ID,
                                       update='Synthetic Champion finished its source slice.')
            store.configure_prompt_triage(SHOTCALLER_ID, True, 0, clock.now())
            store.intake_prompt('prompt:inbox-test', SHOTCALLER_ID, GAREN_RUNTIME, 'codex',
                                'synthetic', 'input:inbox-test',
                                'Explain a synthetic error and list its recovery steps.', clock.now())
            frame = next_input(store)
            commit(store, frame, {'items': [{'k': 'new_request', 's': 'Explain a synthetic error.',
                                             'r': None, 'd': None},
                                            {'k': 'new_request', 's': 'List its recovery steps.',
                                             'r': None, 'd': None}]}, clock.now())
            before = store.unresolved_requests(SHOTCALLER_ID)
            scope = store.connection.execute(
                'SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=?', (SHOTCALLER_ID,),
            ).fetchone()['scope_id']
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:one', clock.now())
            policy_args = ('outbox:inbox-test', 'event:inbox-test', SHOTCALLER_ID)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['reason'] == 'foreground_wait'
            # A killed wait loses its receive lease, not the independent known-work guard.
            clock.advance(31)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['reason'] == 'receiver_work_active'
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:two', clock.now())
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:one', clock.now(), release=True)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['reason'] == 'foreground_wait'
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:two', clock.now(), release=True)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['reason'] == 'receiver_work_active'
            received = read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())
            kinds = {item['envelope']['event_type'] for item in received['items']}
            assert 'agent_transition' in kinds and 'prompt_triaged' in kinds, kinds
            triaged = next(item['envelope'] for item in received['items']
                           if item['envelope']['event_type'] == 'prompt_triaged')
            checklist = json.loads(triaged['summary'])['requests']
            assert len(checklist) == 2
            assert len({item['id'] for item in checklist}) == 2
            assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
            tampered = {**received, 'items': [{**received['items'][0], 'envelope_sha256': 'bad'}]}
            try:
                acknowledge(store, tampered, clock.now())
            except StorageRefusal as exc:
                assert exc.code == 'source_event_mismatch'
            else:
                raise AssertionError('tampered inbox accepted')
            result = acknowledge(store, received, clock.now())
            assert all(row['state'] == 'delivered' for row in result['received'])
            assert all(row['idempotent'] for row in acknowledge(store, received, clock.now())['received'])
            assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
            assert store.unresolved_requests(SHOTCALLER_ID)['requests'] == before['requests']
        finally:
            store.close()
    with tempfile.TemporaryDirectory(prefix='league-inbox-interrupted-') as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            seeder = SyntheticLifecycleSeeder(store, clock)
            seeder.add_pending_delivery(event_id='event:interrupted-read',
                                       outbox_id='outbox:interrupted-read',
                                       recipient_agent_id=SHOTCALLER_ID,
                                       source_agent_id=SHOTCALLER_ID,
                                       update='Preserve this update if the reader is interrupted.')
            interrupted = read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())
            assert len(interrupted['items']) == 1
            clock.advance(61)
            try:
                acknowledge(store, interrupted, clock.now())
            except StorageRefusal as exc:
                assert exc.code == 'delivery_fenced', exc.code
            else:
                raise AssertionError('expired read acknowledged')
            recovered = read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())
            assert len(recovered['items']) == 1
            assert recovered['items'][0]['envelope'] == interrupted['items'][0]['envelope']
            assert recovered['items'][0]['fence'] > interrupted['items'][0]['fence']
            try:
                acknowledge(store, interrupted, clock.now())
            except StorageRefusal as exc:
                assert exc.code == 'delivery_fenced', exc.code
            else:
                raise AssertionError('old reader stole the recovered claim')
            acknowledge(store, recovered, clock.now())
            assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
        finally:
            store.close()
    print('PASS: Champion and triage inbox read/ack, bounded leases, exact retries, no false completion')


if __name__ == '__main__':
    main()
