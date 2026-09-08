"""Exercise persistent stdio transport against a synthetic model process only."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.triage_codex import CodexClassifier
from league.storage import StorageRefusal


def fake_server():
    def emit(value):
        print(json.dumps(value), flush=True)

    turns = 0
    for line in sys.stdin:
        message = json.loads(line)
        method = message['method']
        result = {}
        if method == 'initialized':
            continue
        if method == 'config/read':
            result = {'config': {'mcp_servers': {'fixture': {}}, 'plugins': {'fixture': {}}}}
        if method == 'thread/start':
            params = message['params']
            assert params['ephemeral'] and params['sandbox'] == 'read-only'
            assert params['config']['mcp_servers']['fixture']['enabled'] is False
            assert params['config']['plugins']['fixture']['enabled'] is False
            assert params['config']['agents']['enabled'] is False
            result = {'thread': {'id': 'fixture-thread'}}
        if method == 'thread/unsubscribe':
            assert turns == 1 and message['params']['threadId'] == 'fixture-thread'
            turns = 0
        if method == 'turn/start':
            assert turns == 0
            turns += 1
            emit({'id': message['id'], 'result': {'turn': {'id': 'fixture-turn'}}})
            data = json.loads(message['params']['input'][0]['text'])
            output = {'items': [{'k': 'new_request', 's': data['prompt'], 'r': None, 'd': None}]}
            emit({'method': 'item/completed', 'params': {'threadId': 'fixture-thread', 'turnId': 'fixture-turn',
                  'item': {'type': 'agentMessage', 'text': json.dumps(output)}}})
            emit({'method': 'turn/completed', 'params': {'threadId': 'fixture-thread',
                  'turn': {'id': 'fixture-turn', 'status': 'completed'}}})
            continue
        emit({'id': message['id'], 'result': result})


def main():
    processes = []
    def spawn(command, **options):
        assert command[1:] == ['app-server', '--listen', 'stdio://']
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--fixture'], **options)
        processes.append(process)
        return process

    with tempfile.TemporaryDirectory(prefix='league-stdio-fixture-') as temporary:
        backend = CodexClassifier(Path(sys.executable), Path(temporary), model='fixture-model', popen=spawn)
        try:
            for text in ['Explain a synthetic error.', 'Explain another synthetic error.']:
                result = backend.classify({'prompt': text, 'existing': []}, lambda: False)
                assert result['items'][0]['s'] == text
            assert len(processes) == 1
            assert backend.last_usage['elapsed_seconds'] >= 0
        finally:
            backend.close()
        assert processes[0].poll() is not None

        def blocked_spawn(command, **options):
            return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **options)

        blocked = CodexClassifier(Path(sys.executable), Path(temporary),
                                  model='fixture-model', popen=blocked_spawn)
        try:
            try:
                blocked._send({'text': 'x' * 900_000}, time.monotonic() + 0.2, lambda: False)
            except TimeoutError:
                pass
            else:
                raise AssertionError('stalled input reader exceeded the send bound')
            try:
                blocked._send({'text': 'cancelled'}, time.monotonic() + 5, lambda: True)
            except StorageRefusal as exc:
                assert exc.code == 'triage_cancelled'
            else:
                raise AssertionError('input transport ignored cancellation')
        finally:
            blocked.close()
        assert blocked.process.poll() is not None
    print('PASS: persistent stdio process, isolated request contexts, bounded/cancellable input, exact child shutdown')


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        fake_server()
    else:
        main()
