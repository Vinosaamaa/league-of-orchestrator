"""Busy and unknown native activity never become a notification prompt."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.receive_guard import require_idle_receiver, deliver_idle_notification
from league.multiplexer_adapters.herdr.adapter import HerdrMultiplexerAdapter
import subprocess
from league.storage import StorageRefusal


def main():
    target = {'locator': 'pane:fixture', 'routing_name': 'fixture', 'session_ref': 'thread:fixture',
              'generation': 'generation:fixture', 'harness_kind': 'codex-thread'}

    class Multiplexer:
        capabilities = {'discover'}
        status = 'working'
        generation = 'generation:fixture'

        def discover(self):
            return [{'pane_id': 'pane:fixture', 'name': 'fixture', 'agent': 'codex',
                     'agent_session': {'value': 'thread:fixture'}, 'agent_status': self.status}]

        def runtime_generation(self, row, session):
            return self.generation

    mux = Multiplexer()
    for status in ('working', 'waiting', 'blocked', 'active', 'unknown', None):
        mux.status = status
        try:
            require_idle_receiver(mux, target)
        except StorageRefusal as exc:
            assert exc.code in {'receiver_busy', 'receiver_activity_unknown'}
        else:
            raise AssertionError(f'non-idle receiver accepted: {status}')
    for status in ('idle', 'done'):
        mux.status = status
        require_idle_receiver(mux, target)
    mux.generation = 'generation:replaced'
    try:
        require_idle_receiver(mux, target)
    except StorageRefusal as exc:
        assert exc.code == 'receiver_activity_unknown'
    else:
        raise AssertionError('replaced runtime accepted')
    mux.generation = 'generation:fixture'
    try:
        deliver_idle_notification(mux, target, 'update')
    except StorageRefusal as exc:
        assert exc.code == 'receiver_activity_unknown'
    else:
        raise AssertionError('unguarded transport accepted')

    class Runner:
        calls = []
        fail = False
        def run(self, args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1 if self.fail else 0,
                '{"error":{"code":"agent_not_idle"}}' if self.fail else '{"result":{"agent":{}}}', '')

    runner = Runner()
    adapter = HerdrMultiplexerAdapter(runner=runner, binary='fixture-herdr')
    observed = {'terminal_id': 'terminal:fixture', 'agent_session': {'value': 'thread:fixture'},
                'agent': 'codex', 'state_change_seq': 7}
    adapter.delivery_if_idle('fixture', 'update', observed=observed)
    assert runner.calls[-1] == ('fixture-herdr', 'agent', 'prompt-if-idle', 'fixture', 'update',
                                'terminal:fixture', 'thread:fixture', 'codex', '7')
    runner.fail = True
    try:
        adapter.delivery_if_idle('fixture', 'update', observed=observed)
    except StorageRefusal:
        pass
    else:
        raise AssertionError('native rejection accepted')
    assert len(runner.calls) == 2, 'refusal must not retry as ordinary prompt'
    observed.pop('state_change_seq')
    try:
        adapter.delivery_if_idle('fixture', 'update', observed=observed)
    except StorageRefusal as exc:
        assert exc.code == 'receiver_activity_unknown'
    else:
        raise AssertionError('missing state version accepted')
    assert len(runner.calls) == 2
    print('PASS: exact idle guard, conditional transport, no fallback, missing state version refuses')


if __name__ == '__main__':
    main()
