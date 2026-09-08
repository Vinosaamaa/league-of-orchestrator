"""Synthetic process-preserving handoff recovery; never touches live state."""
import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from league.runtime_handoff import reconcile_handoff
from league.restored_agent import restored_runtime_generation
from league.sqlite_store import SQLiteStorage
from league.storage import RuntimeRegistrationCommand
from storage_test_support import seeded_state
from storage_fixture import CHAMPION_ID, SHOTCALLER_ID, AT2
from test_runtime_identity import preserved, refused, Watcher


class Native:
    def __init__(self, old):
        self.current = {**old, 'terminal_id': 'terminal:new'}
        self.reads = 0
        self.change_process = False

    def discover(self):
        return [self.current]

    def endpoint(self, *_):
        return object()

    def inspect_restored(self, *_):
        self.reads += 1
        return {'agent': self.current, 'session_ref': self.current['agent_session']['value'],
                'process_fingerprint': str(self.reads) if self.change_process else 'same-process'}


def exercise(parent, name, status, shotcaller=False):
    _, state, _ = seeded_state(parent, name)
    with SQLiteStorage(state) as store:
        actor = SHOTCALLER_ID if shotcaller else CHAMPION_ID
        agent = store.agent_status(actor)
        old = {'pane_id': agent['address'], 'name': agent.get('routing_name'), 'agent': 'codex',
               'agent_session': {'value': actor}, 'workspace_id': 'w1',
               'tab_id': 'w1:t2', 'terminal_id': 'terminal:old', 'foreground_cwd': '/synthetic'}
        generation = restored_runtime_generation('herdr', old['terminal_id'], actor)
        command = RuntimeRegistrationCommand(runtime_instance_id='runtime:handoff',
            actor_agent_id=actor, harness_kind='codex-thread', backend_kind='herdr',
            session_ref=actor, endpoint=old['pane_id'], runtime_generation=generation,
            status=status, verified=status != 'failed', at=AT2)
        store.register_runtime(command)
        before = {'result': {'agents': [old]}}
        baseline = preserved(store)
        native = Native(old)
        watcher = Watcher()
        call = lambda: reconcile_handoff(store, before, multiplexer=native, at=AT2, watcher=watcher)
        native.change_process = True
        refused(call, 'runtime_handoff_changed')
        assert preserved(store) == baseline
        native.change_process = False
        native.current['name'] = 'some-other-agent'
        refused(call, 'runtime_handoff_changed')
        native.current['name'] = old['name']
        unchanged = '\n'.join(store.connection.iterdump())
        assert reconcile_handoff(store, before, multiplexer=native, at=AT2,
                                 watcher=watcher, check_only=True)['verified'] == 1
        assert '\n'.join(store.connection.iterdump()) == unchanged
        if shotcaller:
            watcher.fail = True
            refused(call, 'synthetic_watcher_failure')
            assert preserved(store) == baseline
            watcher.fail = False
        receipt = call()
        assert receipt['reconciled'] == 1 and not receipt['process_effects']
        assert receipt['runtimes'][0]['status'] == ('active' if status == 'failed' else status)
        assert call()['runtimes'][0]['idempotent']
        assert preserved(store) == baseline
        if shotcaller:
            assert store.connection.execute("SELECT state FROM obligations WHERE kind='runtime_restore'").fetchone()[0] == 'satisfied'
        # A later failed observation at the same identity must recover, not just return success.
        with store._transaction():
            store.connection.execute("UPDATE runtime_instances SET status='failed',verified=0 WHERE runtime_instance_id=?",
                                     (command.runtime_instance_id,))
        assert not call()['runtimes'][0]['idempotent']
        assert call()['runtimes'][0]['idempotent']
        assert preserved(store) == baseline
        # An unrelated stale generation is reported, never silently repaired.
        unrelated = copy.deepcopy(before)
        unrelated['result']['agents'][0]['terminal_id'] = 'unrelated'
        native.current['terminal_id'] = 'other-current'
        result = reconcile_handoff(store, unrelated, multiplexer=native, at=AT2, watcher=Watcher())
        assert result['reconciled'] == 0 and result['skipped'][0]['reason'] == 'preexisting_generation_mismatch'
        assert preserved(store) == baseline
        for bad in (None, [], {}, {'result': None}, {'result': {'agents': [1]}}):
            refused(lambda: reconcile_handoff(store, bad, multiplexer=native, at=AT2), 'runtime_handoff_invalid')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='league-handoff-') as temporary:
        for status in ('active', 'idle', 'failed'):
            exercise(Path(temporary), status, status)
        exercise(Path(temporary), 'shotcaller', 'active', shotcaller=True)
    print('PASS: exact handoff recovery, failed same-generation retry, stale refusal, unchanged work')
