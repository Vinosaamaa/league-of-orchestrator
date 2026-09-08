"""Busy and unknown native activity never become a notification prompt."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.receive_guard import require_idle_receiver
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
    print('PASS: busy/blocked/unknown refuse wake, exact idle/done pass, replaced runtime refuses')


if __name__ == '__main__':
    main()
