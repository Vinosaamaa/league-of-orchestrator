"""Four shipping regressions using temporary stores and fake native effects only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import os
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
import test_runtime_identity as fixture
from test_multiplexer_metadata import RestoredHerdr
from league.multiplexer_adapters.herdr.adapter import HerdrMultiplexerAdapter
from league.persistent_supervisor import PersistentSupervisor, handoff_transition_delivery
from league.restored_agent import SupervisorWatcherAdapter
from league.storage import StorageRefusal


class NativeProcess(RestoredHerdr):
    executable = 'codex'
    launcher = False
    changed = False
    stale_start = False
    os_executable = None
    reads = 0

    def __init__(self):
        super().__init__([{'agent_adapter_kind': 'codex', 'applies_to_source': 'herdr:codex',
                          'session_ref': fixture.SHOTCALLER_ID, 'cwd': '/synthetic/project'}])
        self.unavailable_reads = 0
        self.agents[0].update(workspace_id='w1', tab_id='w1:t1', pane_id='w1:p1',
                              terminal_id='terminal:new', name='garen', agent_status='working')

    def run(self, arguments, timeout_seconds=30):
        command = tuple(arguments)
        if command[0] == '/bin/ps':
            self.reads += 1
            stamp = '17:50:47' if self.changed and self.reads > 1 else '17:50:46'
            parent = 8122 if self.launcher else 100
            group = 8122 if self.launcher else 8123
            output = f'8123 {parent} {group} Fri Sep 4 {stamp} 2026 /synthetic/bin/{self.os_executable or self.executable}\n'
            if self.launcher:
                output += '8122 100 8122 Fri Sep 4 17:50:46 2026 /bin/zsh\n'
            return subprocess.CompletedProcess(command, 0, output, '')
        if command[1:3] == ('pane', 'process-info'):
            processes = [{'pid': 8123, 'argv0': self.executable,
                          'argv': [f'/synthetic/bin/{self.executable}', 'resume', fixture.SHOTCALLER_ID],
                          'cwd': '/synthetic/project'}]
            if self.stale_start:
                processes[0]['process_start'] = 'stale-native-metadata'
            if self.launcher:
                processes.append({'pid': 8122, 'argv': ['/bin/zsh', '/synthetic/launcher/codex',
                                                       'resume', fixture.SHOTCALLER_ID],
                                  'cwd': '/synthetic/project'})
            return self.completed(command, {'process_info': {'pane_id': 'w1:p1',
                'foreground_process_group_id': 8122 if self.launcher else 8123,
                'foreground_processes': processes}})
        return super().run(arguments, timeout_seconds)


class ShippingTests(unittest.TestCase):
    def test_native_executable_required_before_repair(self):
        for executable, launcher, changed in [('python3', False, False), ('codex', False, True),
                                              ('codex', False, False), ('codex', True, False)]:
            with self.subTest(executable=executable, launcher=launcher, changed=changed), tempfile.TemporaryDirectory() as tmp:
                store, _, request = fixture.seed(Path(tmp))
                with store:
                    runner = NativeProcess()
                    runner.executable, runner.launcher, runner.changed = executable, launcher, changed
                    watcher = fixture.Watcher()
                    with patch.object(watcher, 'bind', wraps=watcher.bind) as bind, patch.dict(os.environ, {
                        'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1', 'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1',
                    }):
                        action = lambda: fixture.repair(store, request, native=HerdrMultiplexerAdapter(runner, binary='herdr'), watcher=watcher)
                        if executable != 'codex' or changed:
                            with self.assertRaises(StorageRefusal):
                                action()
                            self.assertEqual(store.agent_status(fixture.SHOTCALLER_ID)['thread_id'], 'terminal-session')
                            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='runtime_identity_repaired'").fetchone()[0], 0)
                            bind.assert_not_called()
                        else:
                            self.assertTrue(action()['native_identity_verified'])

    def test_exact_retry_rebinds_recorded_old_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, request = fixture.seed(Path(tmp))
            with store:
                watcher = fixture.Watcher()
                watcher.fail = True
                fixture.refused(lambda: fixture.repair(store, request, watcher=watcher), 'runtime_identity_repair_pending')
                old = {'callsign': 'Garen', 'actor_agent_id': fixture.SHOTCALLER_ID,
                       'runtime_instance_id': 'runtime:legacy', 'session_ref': 'terminal-session',
                       'fence': 1, 'runtime_generation': 'terminal:old', 'endpoint': 'w1:p1'}
                adapter = SupervisorWatcherAdapter(store, fixture.AT2)
                registration = {'runtime_instance_id': 'runtime:legacy', 'wake_locator': 'unix:/synthetic/watcher.sock',
                                'fence': 1, 'leased_until': '2099-01-01T00:00:00Z', 'watcher_id': 'synthetic-watcher'}
                current = dict(old)
                kinds = []
                def send(locator, message, **kwargs):
                    kinds.append(message['kind'])
                    if message['kind'] == 'reconcile-restored-runtime':
                        current.update({key: value for key, value in message.items() if key != 'kind'})
                        current['fence'] += 1
                        registration['fence'] = current['fence']
                    return dict(current)
                with patch.object(store, 'watcher_registration', side_effect=lambda _: dict(registration)), patch(
                    'league.restored_agent.send_supervisor_message', side_effect=send):
                    self.assertTrue(fixture.repair(store, request, watcher=adapter)['idempotent'])
                self.assertEqual(kinds, ['ping', 'reconcile-restored-runtime', 'ping', 'ping'])
                self.assertEqual(store.connection.execute("SELECT state FROM obligations WHERE kind='runtime_restore'").fetchone()[0], 'satisfied')

    def test_cached_start_cannot_bypass_os_process_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, request = fixture.seed(Path(tmp))
            with store:
                runner = NativeProcess()
                runner.stale_start, runner.os_executable = True, 'python3'
                with patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
                    'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1'}):
                    with self.assertRaises(StorageRefusal):
                        fixture.repair(store, request, native=HerdrMultiplexerAdapter(runner, binary='herdr'))
                self.assertEqual(store.agent_status(fixture.SHOTCALLER_ID)['thread_id'], 'terminal-session')
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM obligations WHERE kind='runtime_restore'").fetchone()[0], 0)

    def test_pending_retry_refuses_foreign_and_stale_watcher_evidence(self):
        for field, value in [('session_ref', 'foreign'), ('runtime_instance_id', 'foreign'),
                             ('actor_agent_id', 'foreign'), ('runtime_generation', 'foreign'),
                             ('endpoint', 'foreign'), ('fence', 2), ('callsign', 'foreign')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                store, _, request = fixture.seed(Path(tmp))
                with store:
                    watcher = fixture.Watcher()
                    watcher.fail = True
                    fixture.refused(lambda: fixture.repair(store, request, watcher=watcher), 'runtime_identity_repair_pending')
                    old = {'callsign': 'Garen', 'actor_agent_id': fixture.SHOTCALLER_ID,
                           'runtime_instance_id': 'runtime:legacy', 'session_ref': 'terminal-session',
                           'runtime_generation': 'terminal:old', 'endpoint': 'w1:p1', 'fence': 1}
                    old[field] = value
                    registration = {'runtime_instance_id': 'runtime:legacy', 'wake_locator': 'unix:/synthetic/watcher.sock',
                                    'fence': 1, 'leased_until': '2099-01-01T00:00:00Z'}
                    adapter = SupervisorWatcherAdapter(store, fixture.AT2)
                    with patch.object(store, 'watcher_registration', return_value=registration), patch(
                        'league.restored_agent.send_supervisor_message', return_value=old) as send:
                        fixture.refused(lambda: fixture.repair(store, request, watcher=adapter), 'restored_agent_watcher_mismatch')
                    self.assertEqual([call.args[1]['kind'] for call in send.call_args_list], ['ping'])
                    self.assertEqual(store.connection.execute("SELECT state FROM obligations WHERE kind='runtime_restore'").fetchone()[0], 'open')
                    self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='runtime_identity_repaired'").fetchone()[0], 1)

    def test_pending_retry_accepts_exact_already_rebound_watcher_only(self):
        for fence, expired in ((2, False), (3, False), (2, True)):
            with self.subTest(fence=fence, expired=expired), tempfile.TemporaryDirectory() as tmp:
                store, _, request = fixture.seed(Path(tmp))
                with store:
                    watcher = fixture.Watcher()
                    watcher.fail = True
                    fixture.refused(lambda: fixture.repair(store, request, watcher=watcher), 'runtime_identity_repair_pending')
                    generation = store.connection.execute("SELECT runtime_generation FROM runtime_instances WHERE runtime_instance_id='runtime:legacy'").fetchone()[0]
                    current = {'callsign': 'Garen', 'actor_agent_id': fixture.SHOTCALLER_ID,
                               'runtime_instance_id': 'runtime:legacy', 'session_ref': fixture.SHOTCALLER_ID,
                               'runtime_generation': generation, 'endpoint': 'w1:p1', 'fence': fence}
                    registration = {'runtime_instance_id': 'runtime:legacy', 'wake_locator': 'unix:/synthetic/watcher.sock',
                                    'fence': fence, 'watcher_id': 'synthetic-watcher',
                                    'leased_until': '2000-01-01T00:00:00Z' if expired else '2099-01-01T00:00:00Z'}
                    adapter = SupervisorWatcherAdapter(store, fixture.AT2)
                    with patch.object(store, 'watcher_registration', return_value=registration), patch(
                        'league.restored_agent.send_supervisor_message', return_value=current) as send:
                        if fence == 2 and not expired:
                            self.assertTrue(fixture.repair(store, request, watcher=adapter)['watcher_verified'])
                            self.assertEqual(store.connection.execute("SELECT state FROM obligations WHERE kind='runtime_restore'").fetchone()[0], 'satisfied')
                            # Once settled, the old session has no exception to ordinary preflight.
                            send.return_value = {**current, 'session_ref': 'terminal-session', 'fence': 1}
                            fixture.refused(lambda: fixture.repair(store, request, watcher=adapter), 'restored_agent_watcher_mismatch')
                        else:
                            fixture.refused(lambda: fixture.repair(store, request, watcher=adapter), 'restored_agent_watcher_mismatch')
                        self.assertTrue(all(call.args[1]['kind'] == 'ping' for call in send.call_args_list))

    def test_pending_retry_refuses_changed_canonical_or_native_evidence(self):
        for changed in ('process', 'version', 'generation', 'obligation'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                store, _, request = fixture.seed(Path(tmp))
                with store:
                    watcher = fixture.Watcher()
                    watcher.fail = True
                    fixture.refused(lambda: fixture.repair(store, request, watcher=watcher), 'runtime_identity_repair_pending')
                    native = fixture.Native()
                    original = native.inspect_restored
                    if changed == 'process':
                        native.inspect_restored = lambda *_: {**original(), 'process_fingerprint': 'different-stable-process'}
                    elif changed == 'version':
                        store.connection.execute('UPDATE agent_instances SET version=version+1 WHERE agent_id=?', (fixture.SHOTCALLER_ID,))
                    elif changed == 'generation':
                        store.connection.execute("UPDATE runtime_instances SET runtime_generation='foreign' WHERE runtime_instance_id='runtime:legacy'")
                    else:
                        store.record_restored_runtime_recovery('runtime:legacy', fixture.SHOTCALLER_ID, 'foreign', fixture.AT2)
                    with patch.object(watcher, 'preflight') as preflight:
                        fixture.refused(lambda: fixture.repair(store, request, native=native, watcher=watcher), 'runtime_identity_repair_conflict')
                        preflight.assert_not_called()

    def test_champion_event_dispatch_does_not_starve_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = threading.Barrier(3)
            release = threading.Event()
            ping = threading.Event()
            replies = []
            class SlowWake:
                def send(self, *_):
                    started.wait(timeout=3)
                    release.wait(timeout=5)
            runtime = PersistentSupervisor(Path(tmp), wake_adapter=SlowWake(), store_factory=lambda _: nullcontext(None))
            state = {'actor_agent_id': 'synthetic-owner', 'fence': 1, 'runtime_generation': 'gen:1',
                     'callsign': 'Garen', 'squad_id': 'synthetic-squad', 'runtime_instance_id': 'synthetic-runtime',
                     'endpoint': 'synthetic:p1', 'session_ref': 'synthetic-thread'}
            message = {'kind': 'champion-event', 'fence': 1, 'runtime_generation': 'gen:1',
                       'envelope': {'event_id': 'synthetic-event', 'recipient_agent_id': 'synthetic-owner'}}
            def respond(connection, value):
                replies.append((connection, value))
                if connection == 'ping':
                    ping.set()
            with ThreadPoolExecutor(max_workers=2) as control, ThreadPoolExecutor(max_workers=2) as delivery:
                runtime._request_executor = control
                runtime._delivery_executor = delivery
                with patch.object(runtime, '_binding_state', return_value=state), patch.object(runtime, '_assert_fenced_registration'), patch.object(runtime, '_response', side_effect=respond):
                    try:
                        for index in (1, 2):
                            event = {**message, 'envelope': {**message['envelope'], 'event_id': f'synthetic-event-{index}'}}
                            self.assertTrue(runtime._submit(runtime._dispatch_message, f'wake{index}', event, control=True))
                        started.wait(timeout=3)
                        self.assertTrue(runtime._submit(runtime._dispatch_message, 'ping', {'kind': 'ping', 'actor_agent_id': 'synthetic-owner'}, control=True))
                        self.assertTrue(ping.wait(0.5), 'two deliveries starved the actual ping dispatch')
                        self.assertFalse(any(value.get('delivered') for _, value in replies))
                    finally:
                        release.set()
                        delivery.shutdown(wait=True)
            self.assertEqual(sum(value.get('delivered', False) for _, value in replies), 2)
            self.assertEqual({value['event_id'] for _, value in replies if value.get('delivered')},
                             {'synthetic-event-1', 'synthetic-event-2'})

    def test_delivery_capacity_refuses_and_rechecks_queued_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            started, release = threading.Event(), threading.Event()
            replies, effects = [], []
            class Wake:
                def send(self, state, envelope):
                    effects.append(envelope['event_id'])
                    started.set()
                    release.wait(5)
            runtime = PersistentSupervisor(Path(tmp), wake_adapter=Wake(), store_factory=lambda _: nullcontext(None))
            state = {'actor_agent_id': 'synthetic-owner', 'fence': 1, 'runtime_generation': 'gen:1',
                     'runtime_instance_id': 'synthetic-runtime', 'session_ref': 'synthetic-thread', 'endpoint': 'synthetic:p1'}
            message = {'kind': 'champion-event', 'fence': 1, 'runtime_generation': 'gen:1',
                       'envelope': {'event_id': 'synthetic-event', 'recipient_agent_id': 'synthetic-owner'}}
            with ThreadPoolExecutor(max_workers=1) as delivery:
                runtime._delivery_executor = delivery
                with patch.object(runtime, '_binding_state', side_effect=lambda _: dict(state)), patch.object(
                    runtime, '_assert_fenced_registration'), patch.object(runtime, '_response', side_effect=lambda c, v: replies.append(v)):
                    try:
                        runtime._dispatch_message('first', message)
                        self.assertTrue(started.wait(1))
                        runtime._dispatch_message('queued', message)
                        runtime._dispatch_message('overflow', message)
                        self.assertEqual(replies, [{'ok': False, 'error': 'supervisor_capacity_exceeded'}])
                        state['fence'] = 2
                    finally:
                        release.set()
                        delivery.shutdown(wait=True)
            self.assertEqual(effects, ['synthetic-event'])
            self.assertEqual(len(replies), 3)
            self.assertEqual(sum(value.get('delivered', False) for value in replies), 1)
            # Shutdown drains every retained connection; no cancelled job leaks its permit.
            for _ in range(2):
                self.assertTrue(runtime._delivery_slots.acquire(blocking=False))
            self.assertFalse(runtime._delivery_slots.acquire(blocking=False))

    def test_detached_handoff_contacts_exact_live_service(self):
        from test_supervisor_delivery import active_champion
        from league.persistent_supervisor import supervisor_wake_locator
        # Canonical route selection with a temporary fixture; transport is a fake boundary.
        with tempfile.TemporaryDirectory() as tmp:
            state, clock, _ = active_champion(Path(tmp))
            from league.sqlite_store import SQLiteStorage
            with SQLiteStorage(state) as store:
                runtime = PersistentSupervisor(state, callsign='Garen')
                runtime._register()
                binding = runtime._binding_state(fixture.SHOTCALLER_ID)
                store.set_supervision_attachment(binding['scope_id'], fixture.SHOTCALLER_ID, 'detached',
                    clock.now(), expected_watcher_id=binding['watcher_id'], expected_fence=binding['fence'])
                self.assertEqual(store.delivery_target(fixture.SHOTCALLER_ID, clock.now())['channel'], 'direct')
                with patch('league.persistent_supervisor.send_supervisor_message', return_value={'fence': binding['fence'], 'scheduled': True}) as send:
                    result = handoff_transition_delivery(store, outbox_id='synthetic-outbox', event_id='synthetic-event',
                                                         recipient_agent_id=fixture.SHOTCALLER_ID, at=clock.now())
                self.assertEqual(result['state'], 'scheduled')
                self.assertEqual(send.call_args.args[0], supervisor_wake_locator(state))
                self.assertEqual(send.call_args.args[1]['fence'], binding['fence'])
                registration = store.watcher_registration(fixture.SHOTCALLER_ID)
                for change in ({'runtime_instance_id': 'foreign'}, {'actor_agent_id': 'foreign'},
                               {'leased_until': '2000-01-01T00:00:00Z'}, {'wake_locator': 'sqlite-supervise:foreign'}):
                    with self.subTest(change=change), patch.object(store, 'watcher_registration', return_value={**registration, **change}), patch(
                        'league.persistent_supervisor.send_supervisor_message') as send:
                        result = handoff_transition_delivery(store, outbox_id='synthetic-outbox', event_id='synthetic-event',
                                                             recipient_agent_id=fixture.SHOTCALLER_ID, at=clock.now())
                        self.assertEqual(result['state'], 'pending')
                        send.assert_not_called()
                with patch('league.persistent_supervisor.send_supervisor_message', return_value={'fence': binding['fence'] + 1, 'scheduled': True}):
                    result = handoff_transition_delivery(store, outbox_id='synthetic-outbox', event_id='synthetic-event',
                                                         recipient_agent_id=fixture.SHOTCALLER_ID, at=clock.now())
                    self.assertEqual(result['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
