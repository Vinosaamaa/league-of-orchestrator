"""Exact retained Champion recovery; synthetic state and native adapter only."""
import copy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from league.runtime_identity import reconcile_champion_identity
from league.sqlite_store import SQLiteStorage
from league.storage import RuntimeRegistrationCommand
from storage_fixture import CHAMPION_ID, SHOTCALLER_ID, AT2
from storage_test_support import seeded_state
from test_runtime_identity import preserved, refused


class Native:
    kind = 'herdr'

    def __init__(self, actor, owner):
        self.owner = {'pane_id': owner['address'], 'agent_session': {'value': owner['thread_id']}}
        self.champion = {'pane_id': actor['address'], 'name': actor['routing_name'], 'agent': 'codex',
                         'agent_session': {'value': actor['thread_id']}, 'foreground_cwd': actor['worktree'],
                         'workspace_id': 'w1', 'tab_id': 'w1:t2', 'terminal_id': 'terminal:restored'}
        self.reads = 0
        self.changed = False
        self.duplicate = False
        self.on_observation = None

    def calling_context(self):
        return {'pane_id': self.owner['pane_id']}

    def discover(self):
        return [self.owner, self.champion] + ([copy.deepcopy(self.champion)] if self.duplicate else [])

    def endpoint(self, *_):
        return SimpleNamespace(terminal_id=self.champion['terminal_id'])

    def inspect_restored(self, *_):
        self.reads += 1
        if self.on_observation:
            self.on_observation()
        return {'session_ref': self.champion['agent_session']['value'], 'agent': self.champion,
                'process_fingerprint': str(self.reads) if self.changed else 'process:unchanged'}


def exercise(parent):
    _, state, _ = seeded_state(parent, 'champion-recovery')
    with SQLiteStorage(state) as store:
        actor = dict(store.connection.execute('SELECT * FROM agent_instances WHERE agent_id=?', (CHAMPION_ID,)).fetchone())
        owner = store.agent_status(SHOTCALLER_ID)
        command = RuntimeRegistrationCommand(runtime_instance_id='runtime:champion-recovery',
            actor_agent_id=CHAMPION_ID, harness_kind='codex-thread', backend_kind='herdr',
            session_ref=actor['thread_id'], endpoint=actor['address'], runtime_generation='hook:old',
            status='failed', verified=False, at=AT2)
        store.register_runtime(command)
        request = dict(owner_agent_id=SHOTCALLER_ID, agent_id=CHAMPION_ID,
                       runtime_instance_id=command.runtime_instance_id, session_ref=command.session_ref,
                       endpoint=command.endpoint, expected_generation=command.runtime_generation)
        native = Native(actor, owner)
        call = lambda **kw: reconcile_champion_identity(store, request, multiplexer=native, at=AT2, **kw)
        dump = lambda: '\n'.join(store.connection.iterdump())
        original = dump()
        baseline = preserved(store)
        refused(lambda: call(owner_authorized=False), 'owner_authorization_required')
        native.changed = True
        refused(lambda: call(owner_authorized=True), 'champion_identity_unproven')
        native.changed = False
        native.duplicate = True
        refused(lambda: call(owner_authorized=True), 'champion_identity_unproven')
        native.duplicate = False
        for key, bad in [('name', 'wrong'), ('foreground_cwd', '/other'), ('pane_id', 'w1:p99')]:
            previous = native.champion[key]
            native.champion[key] = bad
            refused(lambda: call(owner_authorized=True), 'champion_identity_unproven')
            native.champion[key] = previous
        request['expected_generation'] = 'other'
        refused(lambda: call(owner_authorized=True), 'runtime_reconcile_version_conflict')
        request['expected_generation'] = 'hook:old'
        assert call(owner_authorized=True, check_only=True)['verified']
        assert dump() == original
        receipt = call(owner_authorized=True)
        assert receipt['status'] == 'active' and not receipt['process_effects']
        assert call(owner_authorized=True)['idempotent']
        assert preserved(store) == baseline
        native.on_observation = lambda: store.connection.execute(
            'UPDATE agent_instances SET version=version+1 WHERE agent_id=?', (CHAMPION_ID,))
        refused(lambda: call(owner_authorized=True), 'runtime_reconcile_version_conflict')
        # Even an idempotent retry checks the current owner within the write transaction.
        native.on_observation = lambda: store.connection.execute(
            'UPDATE agent_instances SET shotcaller_agent_id=NULL WHERE agent_id=?', (CHAMPION_ID,))
        refused(lambda: call(owner_authorized=True), 'runtime_reconcile_owner_mismatch')
        assert preserved(store) == baseline


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='league-champion-identity-') as directory:
        exercise(Path(directory))
    print('PASS: exact Champion recovery, two native observations, owner fence, refusals and unchanged task work')
