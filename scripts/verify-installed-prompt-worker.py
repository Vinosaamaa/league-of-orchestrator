#!/usr/bin/env python3
"""Opt-in real-classifier smoke against installed code and synthetic records only."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-root', required=True, type=Path)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--effort', default='high')
    parser.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error('this smoke requests exactly two real classifier turns; explicit opt-in required')
    installed = args.installed_root.resolve(strict=True)
    manifest = json.loads((installed / 'candidate-manifest.json').read_bytes())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((installed / name).read_bytes()).hexdigest() == digest, name
    sys.path[:0] = [str(installed / 'src'), str(Path(__file__).resolve().parents[1] / 'tests')]
    import league
    assert Path(league.__file__).resolve().is_relative_to(installed)
    from league.triage_codex import CodexClassifier
    from league.triage_worker import PromptTriageWorker
    from league.sqlite_inbox_ops import read, acknowledge
    from league.storage import AnswerRequestCommand
    from request_lifecycle_fixture import create_context, dispatch_request, GAREN_RUNTIME
    from storage_fixture import SHOTCALLER_ID

    class Clock:
        def now(self):
            return datetime.now(timezone.utc).isoformat()
        def after(self, seconds):
            return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    clock = Clock()
    results, backends, deliveries = [], [], []
    with tempfile.TemporaryDirectory(prefix='league-installed-worker-') as temporary:
        _, store, _ = create_context(Path(temporary))
        workspace = Path(temporary) / 'classifier'
        workspace.mkdir()
        def factory():
            backend = CodexClassifier(args.executable.resolve(strict=True), workspace,
                                      model=args.model, effort=args.effort)
            backends.append(backend)
            return backend
        worker = PromptTriageWorker(store.state_root, factory, deliveries.append)
        try:
            policy = store.configure_prompt_triage(SHOTCALLER_ID, True, 0, clock.now())
            for number, (prompt, count, answer) in enumerate((
                ('What is 2 + 2?', 1, '2 + 2 = 4.'),
                ('Please answer both independent questions: (1) What is the capital of France? '
                 '(2) How many days are in a standard non-leap year?', 2,
                 'The capital of France is Paris. A standard non-leap year has 365 days.'),
            ), 1):
                started = time.monotonic()
                store.intake_prompt(f'installed-fixture:{number}', SHOTCALLER_ID, GAREN_RUNTIME,
                    'codex', 'session:' + GAREN_RUNTIME, f'installed-input:{number}', prompt, clock.now())
                assert worker.process_one()
                assert worker.last_error is None, worker.last_error
                classification = time.monotonic() - started
                receipt = read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())
                items = [item for item in receipt['items'] if item['envelope']['event_type'] == 'prompt_triaged']
                assert len(items) == 1, items
                requests = json.loads(items[0]['envelope']['summary'])['requests']
                assert len(requests) == count and len({r['id'] for r in requests}) == count, requests
                assert all(r['state'] != 'answered' for r in requests)
                acknowledge(store, receipt, clock.now())
                for request in requests:
                    request_id = request['id']
                    token = 'fixture-claim:' + request_id
                    store.claim_request(request_id, GAREN_RUNTIME, token, clock.after(300), clock.now())
                    dispatch_request(store, clock, request_id, token, 'fixture-dispatch:' + request_id, 'question', 'direct')
                    version = store.connection.execute('SELECT version FROM requests WHERE request_id=?', (request_id,)).fetchone()[0]
                    result = store.answer_request(AnswerRequestCommand(request_id, token, version,
                        'fixture-response:' + request_id, 'codex', 'session:' + GAREN_RUNTIME,
                        'fixture-reference:' + request_id, 'durable', hashlib.sha256(answer.encode()).hexdigest(),
                        answer, 'fixture-answer-event:' + request_id, clock.now()))
                    assert result['state'] == 'answered'
                assert not read(store, SHOTCALLER_ID, GAREN_RUNTIME, clock.now())['items']
                results.append({'prompt': number, 'requests': count, 'classification_commit_seconds': classification,
                                'through_answer_seconds': time.monotonic() - started,
                                'usage': backends[0].last_usage})
                print(json.dumps({'phase': 'answered-synthetic', **results[-1]}), flush=True)
            assert len(backends) == 1 and len(deliveries) == 2
            assert not worker.process_one()  # No idle inference.
            store.configure_prompt_triage(SHOTCALLER_ID, False, policy['version'], clock.now())
            store.intake_prompt('fixture:off', SHOTCALLER_ID, GAREN_RUNTIME, 'codex',
                'session:' + GAREN_RUNTIME, 'fixture:off-input', 'This prompt is captured while OFF.', clock.now())
            assert not worker.process_one()
            assert store.connection.execute("SELECT triage_mode FROM prompts WHERE prompt_id='fixture:off'").fetchone()[0] == 'off'
            worker.close()
            assert backends[0].process.poll() is not None
            print(json.dumps({'phase': 'passed', 'installed_head': manifest['accepted_head'],
                'model_calls': 2, 'classifier_processes': 1, 'same_process_reused': True,
                'synthetic_requests_answered': 3, 'off_preserved': True, 'exact_child_exited': True,
                'live_database_touched': False, 'results': results}), flush=True)
        finally:
            worker.close()
            store.close()


if __name__ == '__main__':
    main()
