"""Native reply context is bounded, session-local, and separate from answers."""

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from league.canonical_watcher import handle_brokered_hook
from league.triage_context import MAX_CONTEXT_BYTES, preceding_context, record_native_reply
from league.sqlite_prompt_triage_ops import compact_input, next_input
from request_lifecycle_fixture import create_context, GAREN_RUNTIME, GAREN_RUNTIME_TWO, JARVAN_ID
from storage_fixture import SHOTCALLER_ID


def main():
    with tempfile.TemporaryDirectory(prefix='league-context-') as temporary:
        _, store, clock = create_context(Path(temporary))
        try:
            store.configure_prompt_triage(SHOTCALLER_ID, True, 0, clock.now())
            session = 'session:' + GAREN_RUNTIME
            payload = {'hook_event_name': 'Stop', 'session_id': session,
                       'turn_id': 'context-turn', 'stop_hook_active': False,
                       'last_assistant_message': 'For the live wait test, say start when ready.'}
            # Exercise the real bound Stop entrypoint without running supervision.
            with patch.object(store, 'stop_decision', return_value={'decision': 'allow'}):
                result = handle_brokered_hook(store, {'command': 'codex-stop-hook',
                    'payload': payload, 'shotcaller': None, 'session_id': None})
            assert result['hook_output'] == {}
            events = store.connection.execute(
                "SELECT detail_json FROM events WHERE event_type='triage_reply_context'"
            ).fetchall()
            assert len(events) == 1
            assert json.loads(events[0]['detail_json'])['assistant']['text'] == payload['last_assistant_message']

            # Separate synthetic turn for exact ordering and expiry assertions.
            payload['turn_id'] = 'synthetic-context-turn'
            at = clock.now()
            record_native_reply(store, SHOTCALLER_ID, payload, at)
            record_native_reply(store, SHOTCALLER_ID, payload, at)
            count = store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='triage_reply_context'"
            ).fetchone()[0]
            assert count == 2, 'replayed native response created a duplicate context event'
            store.intake_prompt('context-input', SHOTCALLER_ID, GAREN_RUNTIME, 'codex',
                                session, 'context-input', 'start', at)
            frame = next_input(store)
            value = compact_input(frame)
            assert value['prompt'] == 'start'
            assert value['context']['previous_assistant']['text'] == payload['last_assistant_message']
            assert value['context']['previous_assistant']['truncated'] is False
            assert 'runtime' not in json.dumps(value), 'mechanical identities leaked into model input'
            prompt = frame['prompts'][0]
            assert preceding_context(store, JARVAN_ID, prompt) == {}
            assert preceding_context(store, SHOTCALLER_ID,
                                     {**prompt, 'session_ref': 'foreign'}) == {}
            assert preceding_context(store, SHOTCALLER_ID,
                                     {**prompt, 'runtime_instance_id': GAREN_RUNTIME_TWO}) == {}
            assert preceding_context(store, SHOTCALLER_ID,
                                     {**prompt, 'created_at': '2025-01-01T00:00:00Z'}) == {}
            assert preceding_context(store, SHOTCALLER_ID,
                                     {**prompt, 'created_at': '2030-01-01T00:00:00Z'}) == {}
            for changed in ({'last_assistant_message': ''}, {'session_id': 'foreign'},
                            {'hook_event_name': 'UserPromptSubmit'}, {'turn_id': None}):
                record_native_reply(store, SHOTCALLER_ID, {**payload, **changed}, at)
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='triage_reply_context'"
            ).fetchone()[0] == count
            record_native_reply(store, SHOTCALLER_ID,
                {**payload, 'turn_id': 'long-reply', 'last_assistant_message': '界' * 5000}, at)
            bounded = preceding_context(store, SHOTCALLER_ID, prompt)['previous_assistant']
            assert bounded['truncated'] and len(bounded['text'].encode()) <= MAX_CONTEXT_BYTES
            assert '\ufffd' not in bounded['text']
            # Capturing context never answers the original request or emits a delivery.
            assert store.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox d JOIN events e USING(event_id) "
                "WHERE e.event_type='triage_reply_context'"
            ).fetchone()[0] == 0
            store.configure_prompt_triage(SHOTCALLER_ID, False, 1, clock.now())
            record_native_reply(store, SHOTCALLER_ID,
                                {**payload, 'turn_id': 'while-off'}, at)
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='triage_reply_context'"
            ).fetchone()[0] == count + 1
        finally:
            store.close()
    print('PASS: bound native reply capture, exact prompt preservation, bounded context, isolation, expiry, replay')


if __name__ == '__main__':
    main()
