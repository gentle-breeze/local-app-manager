"""Linux lifecycle regressions using temporary, self-expiring synthetic apps only."""
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from server import Manager

ROOT = pathlib.Path(__file__).resolve().parent


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError('condition timed out')


def alive(pid):
    """Exited Linux zombies are not running applications."""
    try:
        state = pathlib.Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0]
        return state not in ('Z', 'X')
    except FileNotFoundError:
        return False


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process-group regression fixtures')
class ShutdownLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manager-lifecycle-')
        self.root = pathlib.Path(self.temp.name)
        self.manager = Manager(self.root / 'data')
        self.serial = 0
        self.workers = []

    def tearDown(self):
        # All app fixtures also expire on their own, even if a lifecycle assertion fails.
        for worker in self.workers:
            worker.join(timeout=12)
        try:
            self.manager.close()
        finally:
            self.temp.cleanup()

    def app(self, body, **settings):
        self.serial += 1
        directory = self.root / ('app-%s' % self.serial)
        directory.mkdir()
        script = directory / 'fixture.py'
        prelude = '''\
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(__file__).resolve().parent
def mark(name, value='ready'):
    (root / name).write_text(str(value))
def event(kind):
    with (root / 'events').open('a') as output:
        output.write('%s %s\\n' % (kind, os.getpid()))
def ready():
    event('start')
    mark('ready', os.getpid())
def run_until(deadline):
    while time.monotonic() < deadline:
        time.sleep(.02)
'''
        script.write_text(prelude + textwrap.dedent(body))
        app = dict(name='lifecycle-%s' % self.serial, cwd=str(directory),
                   argv=[sys.executable, '-u', str(script)], port=0,
                   health='', readonly=False)
        app.update(settings)
        self.manager.save(app)
        return self.manager.apps[-1], directory

    def start(self, app, directory):
        self.manager.action(app['id'], 'start')
        wait_for(lambda: (directory / 'ready').exists())
        return int((directory / 'ready').read_text())

    def state(self, app):
        return next(item for item in self.manager.snapshot() if item['id'] == app['id'])

    def events(self, directory):
        return (directory / 'events').read_text().splitlines()

    def background(self, function):
        errors = []
        def invoke():
            try:
                function()
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=invoke, daemon=True)
        self.workers.append(worker)
        worker.start()
        return worker, errors

    def test_slow_graceful_shutdown_drains_logs_and_waits_for_exit(self):
        app, directory = self.app('''
            def shutdown(*_):
                mark('signal')
                for _ in range(16):
                    print('x' * 32768, flush=True)
                    time.sleep(.08)
                mark('graceful_done')
                event('exit')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 8)
        ''', stop_timeout=3)
        pid = self.start(app, directory)
        before = time.monotonic()
        self.manager.action(app['id'], 'stop')
        self.assertGreater(time.monotonic() - before, 1.15)
        self.assertTrue((directory / 'graceful_done').exists())
        self.assertFalse(alive(pid))
        self.assertEqual(self.state(app)['lifecycle'], 'stopped')
        self.assertFalse(self.manager.running(app['id']))
        self.assertLessEqual(len(''.join(self.manager.logs[app['id']])), 160 * 2048)
        # Repeated stop must never signal a stale, already reaped PID.
        self.manager.action(app['id'], 'stop')

    def test_force_policy_only_kills_after_configured_deadline(self):
        app, directory = self.app('''
            signal.signal(signal.SIGTERM, lambda *_: mark('signal'))
            ready()
            run_until(time.monotonic() + 5)
            mark('natural_exit')
        ''', stop_timeout=.4, stop_policy='kill')
        pid = self.start(app, directory)
        before = time.monotonic()
        self.manager.action(app['id'], 'stop')
        elapsed = time.monotonic() - before
        self.assertGreaterEqual(elapsed, .35)
        self.assertLess(elapsed, 3)
        self.assertTrue((directory / 'signal').exists())
        self.assertFalse((directory / 'natural_exit').exists())
        self.assertFalse(alive(pid))
        self.assertEqual(self.state(app)['lifecycle'], 'stopped')

    def test_wait_policy_timeout_blocks_replacement_until_actual_exit(self):
        app, directory = self.app('''
            def shutdown(*_):
                mark('signal')
                time.sleep(1.3)
                event('exit')
                mark('graceful_done')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 6)
        ''', stop_timeout=.2, stop_policy='wait')
        pid = self.start(app, directory)
        original = self.manager.children[app['id']]
        with self.assertRaises(ValueError):
            self.manager.action(app['id'], 'stop')
        self.assertTrue(alive(pid))
        self.assertIsNone(original.poll())
        self.assertEqual(self.state(app)['lifecycle'], 'stop_timeout')
        self.assertTrue(self.manager.running(app['id']))
        with self.assertRaises(ValueError):
            self.manager.action(app['id'], 'start')
        with self.assertRaises(ValueError):
            self.manager.action(app['id'], 'restart')
        self.assertIs(self.manager.children[app['id']], original)
        self.assertEqual(len(self.events(directory)), 1)
        wait_for(lambda: not self.manager.running(app['id']))
        self.assertTrue((directory / 'graceful_done').exists())
        self.assertFalse(alive(pid))
        self.manager.action(app['id'], 'start')
        wait_for(lambda: len(self.events(directory)) == 3)
        self.assertEqual([entry.split()[0] for entry in self.events(directory)], ['start', 'exit', 'start'])
        self.assertIsNot(self.manager.children[app['id']], original)
        with self.assertRaises(ValueError):
            self.manager.action(app['id'], 'stop')
        wait_for(lambda: not self.manager.running(app['id']))

    def test_restart_is_ordered_after_graceful_exit(self):
        app, directory = self.app('''
            def shutdown(*_):
                time.sleep(1.2)
                event('exit')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 6)
        ''', stop_timeout=2)
        old_pid = self.start(app, directory)
        old = self.manager.children[app['id']]
        self.manager.action(app['id'], 'restart')
        wait_for(lambda: len(self.events(directory)) >= 3)
        self.assertEqual([entry.split()[0] for entry in self.events(directory)][:3], ['start', 'exit', 'start'])
        self.assertFalse(alive(old_pid))
        self.assertIsNotNone(old.poll())
        self.assertIsNot(self.manager.children[app['id']], old)

    def test_concurrent_actions_and_snapshot_do_not_bypass_stop_barrier(self):
        app, directory = self.app('''
            try:
                handle = os.open(root / 'active', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(handle)
            except FileExistsError:
                mark('overlap')
            def shutdown(*_):
                mark('signal')
                time.sleep(1.2)
                (root / 'active').unlink(missing_ok=True)
                event('exit')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 6)
        ''', stop_timeout=2)
        self.start(app, directory)
        stopping, stop_errors = self.background(lambda: self.manager.action(app['id'], 'stop'))
        wait_for(lambda: (directory / 'signal').exists())
        before = time.monotonic()
        state = self.state(app)
        self.assertLess(time.monotonic() - before, .5, 'snapshot blocked behind a shutdown wait')
        self.assertTrue(state['managed'])
        self.assertEqual(state['lifecycle'], 'stopping')
        restarting, restart_errors = self.background(lambda: self.manager.action(app['id'], 'restart'))
        duplicate, duplicate_errors = self.background(lambda: self.manager.action(app['id'], 'stop'))
        for worker in (stopping, restarting, duplicate):
            worker.join(timeout=7)
            self.assertFalse(worker.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertTrue(all(isinstance(error, ValueError) for error in restart_errors + duplicate_errors))
        self.assertFalse((directory / 'overlap').exists())
        events = [entry.split()[0] for entry in self.events(directory)]
        self.assertNotIn(('start', 'start'), list(zip(events, events[1:])))

    def test_natural_root_exit_still_waits_for_child_graceful_exit(self):
        app, directory = self.app('''
            worker = root / 'worker.py'
            worker.write_text("import pathlib, signal, sys, time, os\\n"
                "root = pathlib.Path(__file__).resolve().parent\\n"
                "def shutdown(*_):\\n"
                "    (root / 'child_signal').write_text('yes')\\n"
                "    time.sleep(1.3)\\n"
                "    (root / 'child_done').write_text('yes')\\n"
                "    sys.exit(0)\\n"
                "signal.signal(signal.SIGTERM, shutdown)\\n"
                "(root / 'child_ready').write_text(str(os.getpid()))\\n"
                "time.sleep(6)\\n")
            subprocess.Popen([sys.executable, str(worker)])
            deadline = time.monotonic() + 3
            while not (root / 'child_ready').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            ready()
        ''', stop_timeout=2)
        self.start(app, directory)
        wait_for(lambda: (directory / 'child_signal').exists())
        child_pid = int((directory / 'child_ready').read_text())
        self.assertTrue(alive(child_pid))
        self.assertTrue(self.manager.running(app['id']))
        wait_for(lambda: not self.manager.running(app['id']))
        self.assertTrue((directory / 'child_done').exists())
        self.assertFalse(alive(child_pid))
        self.assertEqual(self.state(app)['lifecycle'], 'stopped')

    def test_rapid_fork_root_exit_keeps_new_child_managed_until_exit(self):
        # Fork immediately before the root exits, without a child-ready handshake.
        # SIG_IGN is inherited across fork, so cleanup cannot terminate the child
        # before it has installed its own handler or written its ready marker.
        for iteration in range(4):
            with self.subTest(iteration=iteration):
                app, directory = self.app('''
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    ready()
                    if os.fork():
                        os._exit(0)
                    mark('child_ready', os.getpid())
                    time.sleep(.65)
                    mark('child_done')
                    os._exit(0)
                ''', stop_timeout=.05, stop_policy='wait')
                self.start(app, directory)
                wait_for(lambda: (directory / 'child_ready').exists())
                pid = int((directory / 'child_ready').read_text())
                deadline = time.monotonic() + 3
                while alive(pid) and time.monotonic() < deadline:
                    self.assertTrue(self.manager.running(app['id']),
                                    'newly forked group member was reported stopped')
                    time.sleep(.02)
                self.assertFalse(alive(pid))
                self.assertTrue((directory / 'child_done').exists())
                wait_for(lambda: not self.manager.running(app['id']))
                self.assertEqual(self.state(app)['lifecycle'], 'stopped')

    def test_natural_root_exit_force_cleans_uncooperative_child(self):
        app, directory = self.app('''
            worker = root / 'worker.py'
            worker.write_text("import pathlib, signal, time, os\\n"
                "root = pathlib.Path(__file__).resolve().parent\\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
                "(root / 'child_ready').write_text(str(os.getpid()))\\n"
                "time.sleep(6)\\n")
            subprocess.Popen([sys.executable, str(worker)])
            deadline = time.monotonic() + 3
            while not (root / 'child_ready').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            ready()
        ''', stop_timeout=.3, stop_policy='kill')
        self.start(app, directory)
        child_pid = int((directory / 'child_ready').read_text())
        wait_for(lambda: not self.manager.running(app['id']))
        self.assertFalse(alive(child_pid))
        self.assertEqual(self.state(app)['lifecycle'], 'stopped')

    def test_supervisor_crash_fails_closed_without_replacement(self):
        app, directory = self.app('''
            ready()
            run_until(time.monotonic() + 2)
        ''', stop_timeout=.2)
        pid = self.start(app, directory)
        supervisor = self.manager.children[app['id']]
        supervisor.kill()
        supervisor.wait(timeout=3)
        wait_for(lambda: self.state(app)['lifecycle'] == 'error')
        self.assertTrue(self.manager.running(app['id']))
        for action in ('start', 'restart'):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    self.manager.action(app['id'], action)
        self.assertIs(self.manager.children[app['id']], supervisor)
        self.assertEqual(len(self.events(directory)), 1)
        self.manager.close()
        self.assertTrue(self.manager.lockfile.closed)
        self.assertTrue(alive(pid), 'fixture must still exist when reopening the manager')
        self.manager = Manager(self.root / 'data')
        self.assertEqual(self.state(app)['lifecycle'], 'error')
        self.assertTrue(self.state(app)['managed'])
        self.assertTrue(self.manager.guard(app['id']).exists())
        for action in ('start', 'restart', 'stop'):
            with self.subTest(reopened_action=action):
                with self.assertRaises(ValueError):
                    self.manager.action(app['id'], action)
        self.assertNotIn(app['id'], self.manager.children)
        wait_for(lambda: not alive(pid))
        # Time passing (or a closed port) cannot clear an unconfirmed launch.
        self.assertEqual(self.state(app)['lifecycle'], 'error')
        self.assertTrue(self.state(app)['managed'])
        with self.assertRaises(ValueError):
            self.manager.action(app['id'], 'start')
        self.assertEqual(len(self.events(directory)), 1)

    def test_closed_supervisor_pipe_does_not_break_stop_or_manager_cleanup(self):
        for pathway in ('stop', 'close'):
            with self.subTest(pathway=pathway):
                other, other_directory = self.app('''
                    def shutdown(*_):
                        mark('graceful_done')
                        sys.exit(0)
                    signal.signal(signal.SIGTERM, shutdown)
                    ready()
                    run_until(time.monotonic() + 4)
                ''', stop_timeout=.5)
                other_pid = self.start(other, other_directory)
                app, directory = self.app('''
                    ready()
                    time.sleep(.25)
                ''', stop_timeout=.5)
                self.start(app, directory)
                runtime = self.manager.runtimes[app['id']]
                # Delay the status reader until after the supervisor has exited.
                # request_stop then buffers a write to a pipe with no reader;
                # flush raises BrokenPipeError and close tries to flush again.
                with runtime.condition:
                    runtime.child.wait(timeout=3)
                    self.assertFalse(runtime.finished)
                    self.assertFalse(runtime.child.stdin.closed)
                    if pathway == 'stop':
                        self.manager.action(app['id'], 'stop')
                    else:
                        self.manager.close()
                wait_for(lambda: runtime.finished)
                self.assertEqual(runtime.snapshot()['lifecycle'], 'stopped')
                self.assertTrue(runtime.child.stdin.closed)
                if pathway == 'stop':
                    self.assertTrue(alive(other_pid))
                    self.manager.action(other['id'], 'stop')
                else:
                    self.assertTrue(self.manager.lockfile.closed)
                self.assertFalse(alive(other_pid))
                self.assertTrue((other_directory / 'graceful_done').exists())
                self.assertFalse(self.manager.guard(app['id']).exists())
                self.assertFalse(self.manager.guard(other['id']).exists())
        # The close path must not strand either the lock or another app runtime.
        replacement = Manager(self.root / 'data')
        replacement.close()

    def test_sigint_is_configurable_without_sigterm(self):
        app, directory = self.app('''
            def interrupted(*_):
                mark('sigint')
                sys.exit(0)
            signal.signal(signal.SIGINT, interrupted)
            signal.signal(signal.SIGTERM, lambda *_: mark('unexpected_sigterm'))
            ready()
            run_until(time.monotonic() + 5)
        ''', stop_signal='SIGINT', stop_timeout=.5)
        self.start(app, directory)
        self.manager.action(app['id'], 'stop')
        self.assertTrue((directory / 'sigint').exists())
        self.assertFalse((directory / 'unexpected_sigterm').exists())

    def test_pipe_loss_applies_wait_policy_instead_of_unconditional_kill(self):
        app, directory = self.app('''
            def shutdown(*_):
                mark('signal')
                time.sleep(1.3)
                mark('graceful_done')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 6)
        ''', stop_timeout=.1, stop_policy='wait')
        supervisor = subprocess.Popen([sys.executable, str(ROOT / 'supervisor.py')],
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        try:
            supervisor.stdin.write((json.dumps(app) + '\n').encode())
            supervisor.stdin.flush()
            wait_for(lambda: (directory / 'ready').exists())
            pid = int((directory / 'ready').read_text())
            supervisor.stdin.close()
            wait_for(lambda: (directory / 'signal').exists())
            time.sleep(.25)
            self.assertIsNone(supervisor.poll())
            self.assertTrue(alive(pid))
            supervisor.wait(timeout=5)
            self.assertTrue((directory / 'graceful_done').exists())
            self.assertFalse(alive(pid))
        finally:
            if not supervisor.stdin.closed:
                supervisor.stdin.close()
            supervisor.wait(timeout=8)

    def test_wait_policy_close_retains_instance_lock_until_group_exits(self):
        app, directory = self.app('''
            def shutdown(*_):
                mark('signal')
                time.sleep(1.3)
                mark('graceful_done')
                sys.exit(0)
            signal.signal(signal.SIGTERM, shutdown)
            ready()
            run_until(time.monotonic() + 6)
        ''', stop_timeout=.1, stop_policy='wait')
        pid = self.start(app, directory)
        self.manager.close()
        self.assertTrue(alive(pid))
        with self.assertRaises(RuntimeError):
            Manager(self.root / 'data')
        wait_for(lambda: not alive(pid))
        wait_for(lambda: self.manager.children[app['id']].poll() is not None)
        replacement = Manager(self.root / 'data')
        replacement.close()
        self.assertTrue((directory / 'graceful_done').exists())

    def test_stop_settings_validate_finite_bounds_and_known_policies(self):
        valid = dict(name='validation', cwd=self.temp.name,
                     argv=[sys.executable, '-c', 'pass'], port=0, readonly=False)
        for field, values in {
            'stop_timeout': [-1, 3601, 10**1000, True, None, '1', float('nan'), float('inf')],
            'stop_signal': ['SIGKILL', 'TERM', 15, None, []],
            'stop_policy': ['force', None, True, []],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.manager.save(dict(valid, **{field: value}))
        for timeout in (0, .1, 3600):
            self.manager.save(dict(valid, stop_timeout=timeout, stop_policy='wait', stop_signal='SIGINT'))
        self.manager.save(valid)
        saved = self.manager.apps[-1]
        self.assertEqual(saved['stop_timeout'], 10)
        self.assertEqual(saved['stop_policy'], 'kill')
        self.assertEqual(saved['stop_signal'], 'SIGTERM')


if __name__ == '__main__':
    unittest.main()
