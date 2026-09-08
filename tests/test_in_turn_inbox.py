"""A busy recipient consumes durable updates without invoking a prompt adapter."""

from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.sqlite_inbox_ops import read, acknowledge, foreground_wait
from league.sqlite_prompt_triage_ops import next_input, commit
from league.storage import StorageRefusal
from request_lifecycle_fixture import create_context, GAREN_RUNTIME, SyntheticLifecycleSeeder
from storage_fixture import SHOTCALLER_ID


def main():
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
            # A killed wait loses its lease; supervision alone cannot defer delivery forever.
            clock.advance(31)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['action'] == 'wake'
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:two', clock.now())
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:one', clock.now(), release=True)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['reason'] == 'foreground_wait'
            foreground_wait(store, scope, SHOTCALLER_ID, GAREN_RUNTIME, 'wait:two', clock.now(), release=True)
            assert store.apply_supervision_delivery_policy(*policy_args, clock.now())['action'] == 'wake'
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
