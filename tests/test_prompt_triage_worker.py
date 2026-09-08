"""One persistent backend, no idle inference, durable failure, policy fencing."""

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.triage_worker import PromptTriageWorker
from league.sqlite_request_ops import untriaged_intake
from request_lifecycle_fixture import create_context, GAREN_RUNTIME
from storage_fixture import SHOTCALLER_ID


def main():
    with tempfile.TemporaryDirectory(prefix='league-triage-worker-') as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            state = store.state_root
            made, deliveries = [], []

            class Backend:
                calls = 0
                closed = False
                fail = False
                last_usage = {'inputTokens': 10, 'outputTokens': 5}

                def classify(self, value, cancelled):
                    assert not cancelled()
                    self.calls += 1
                    if self.fail:
                        raise ValueError('synthetic classifier failure')
                    return {'items': [{'k': 'new_request', 's': value['prompt'], 'r': None, 'd': None}]}

                def close(self):
                    self.closed = True

            def factory():
                backend = Backend()
                made.append(backend)
                return backend

            worker = PromptTriageWorker(state, factory, deliveries.append)
            assert not worker.process_one() and not made
            store.configure_prompt_triage(SHOTCALLER_ID, True, 0, clock.now())

            def capture(number):
                store.intake_prompt(f'worker:{number}', SHOTCALLER_ID, GAREN_RUNTIME, 'codex',
                                    'synthetic', f'worker-event:{number}', f'Explain item {number}.', clock.now())

            for number in range(4):
                capture(number)
                assert worker.process_one()
            assert len(made) == 1 and made[0].calls == 4 and len(deliveries) == 4
            assert not worker.process_one() and made[0].calls == 4
            capture(5)
            store.configure_prompt_triage(SHOTCALLER_ID, False, 1, clock.now())
            assert not worker.process_one()
            assert untriaged_intake(store, SHOTCALLER_ID, background=True)['untriaged_prompt_count'] == 1
            store.configure_prompt_triage(SHOTCALLER_ID, True, 2, clock.now())
            made[0].fail = True
            assert worker.process_one() and made[0].closed
            assert not worker.process_one() and len(made) == 1
            assert worker.last_error == 'triage_backend_unavailable'
            assert untriaged_intake(store, SHOTCALLER_ID, background=True)['untriaged_prompt_count'] == 1
            worker.close()
        finally:
            store.close()
    print('PASS: one classifier reused, no idle calls, OFF pause, durable failure without retry charges')


if __name__ == '__main__':
    main()
