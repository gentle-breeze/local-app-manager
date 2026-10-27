"""Shutdown configuration and the manager side of the supervisor protocol."""
import collections
import json
import math
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time

DEFAULT_STOP_TIMEOUT = 10
KILL_CONFIRM_TIMEOUT = 5
SIGNALS = ('SIGTERM', 'SIGINT')


class LifecycleError(ValueError):
    pass


def shutdown_config(app):
    signal_name = app.get('stop_signal', 'SIGTERM')
    timeout = app.get('stop_timeout', DEFAULT_STOP_TIMEOUT)
    policy = app.get('stop_policy', 'kill')
    if signal_name not in SIGNALS:
        raise ValueError('停止信号仅支持 SIGTERM 或 SIGINT')
    if type(timeout) not in (int, float) or not 0 <= timeout <= 3600 or not math.isfinite(timeout):
        raise ValueError('停止超时必须是 0–3600 秒的有限数值')
    if policy not in ('kill', 'wait'):
        raise ValueError('超时策略仅支持 kill 或 wait')
    return dict(stop_signal=signal_name, stop_timeout=timeout, stop_policy=policy)


class ManagedProcess:
    """A private status pipe, not application output, proves group termination.

    An unexpected supervisor exit is deliberately *not* proof that the app died.
    Such a handle remains blocked rather than risking a duplicate or stale PID kill.
    """
    def __init__(self, app, lock_fd, redact, guard):
        self.config = shutdown_config(app)
        self.condition = threading.Condition()
        self.logs = collections.deque(maxlen=160)
        self.state = 'starting'
        self.error = ''
        self.stop_requested = False
        self.confirmed = False
        self.finished = False
        generation = secrets.token_hex(16)
        # No stored PID is ever trusted for signalling. An uncleared generation
        # only blocks future starts after a manager/supervisor failure.
        fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as marker:
            json.dump(dict(app_id=app['id'], generation=generation), marker)
            marker.flush()
            os.fsync(marker.fileno())
        status_r, status_w = os.pipe()
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'TMPDIR') if k in os.environ}
        try:
            self.child = subprocess.Popen(
                [sys.executable, str(pathlib.Path(__file__).with_name('supervisor.py')),
                 '--status-fd', str(status_w), '--guard-file', str(guard),
                 '--generation', generation],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(status_w, lock_fd), env=env)
        except BaseException:
            os.close(status_r)
            guard.unlink()  # Popen failed before the supervisor could run.
            raise
        finally:
            os.close(status_w)
        threading.Thread(target=self._collect, args=(redact,), daemon=True).start()
        threading.Thread(target=self._status, args=(status_r,), daemon=True).start()
        try:
            self.child.stdin.write((json.dumps(dict(app, **self.config)) + '\n').encode())
            self.child.stdin.flush()
        except (OSError, ValueError):
            # Retain this handle: inability to send config is not proof of cleanup.
            self._close_control()

    def _close_control(self):
        try:
            self.child.stdin.close()
        except (OSError, ValueError):
            pass  # Buffered close can repeat a prior BrokenPipeError.

    def _collect(self, redact):
        try:
            while True:
                chunk = self.child.stdout.readline(2048)
                if not chunk:
                    break
                with self.condition:
                    self.logs.append(redact(chunk.decode('utf-8', 'replace')))
        finally:
            self.child.stdout.close()

    def _status(self, fd):
        try:
            with os.fdopen(fd) as stream:
                for line in stream:
                    event = json.loads(line)
                    state = event['state']
                    with self.condition:
                        if state == 'stopped':
                            self.confirmed = True
                        elif state == 'running' and self.stop_requested:
                            pass  # An immediate stop may precede the startup report.
                        else:
                            self.state = 'stopping' if state == 'killing' else state
                            self.error = event.get('error', '')
                        self.condition.notify_all()
        except (OSError, ValueError, KeyError):
            with self.condition:
                self.error = '监管状态通道异常；未确认停止，禁止重新启动'
        finally:
            code = self.child.wait()
            with self.condition:
                if not self.child.stdin.closed:
                    self._close_control()
                self.finished = True
                if self.confirmed and code == 0:
                    self.state, self.error = 'stopped', ''
                else:
                    self.state = 'error'
                    self.error = self.error or '监管进程异常退出；未确认应用停止，禁止重新启动'
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(managed=self.state != 'stopped', lifecycle=self.state,
                        lifecycle_error=self.error, logs=''.join(self.logs))

    def wait_started(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.state == 'starting':
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LifecycleError('启动状态等待超时；保留监管，禁止重复启动')
                self.condition.wait(remaining)
            if self.state != 'running':
                raise LifecycleError(self.error or '新进程未能保持运行；请检查启动命令与运行依赖')

    def request_stop(self, disconnect=False):
        with self.condition:
            if self.state == 'stopped':
                return
            if self.finished:
                raise LifecycleError(self.error)
            if not self.stop_requested:
                self.stop_requested = True
                self.state, self.error = 'stopping', ''
                try:
                    self.child.stdin.write(b'stop\n')
                    self.child.stdin.flush()
                except (OSError, ValueError):
                    pass  # EOF also requests cleanup; wait for actual confirmation.
            if disconnect and not self.child.stdin.closed:
                self._close_control()

    def wait_stopped(self):
        timeout = self.config['stop_timeout']
        if self.config['stop_policy'] == 'kill':
            timeout += KILL_CONFIRM_TIMEOUT + .5
        # Allow the control/status threads and process-table probe to run, but do
        # not mistake a protocol timeout for permission to launch a replacement.
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.state != 'stopped':
                if self.finished or self.state == 'error':
                    raise LifecycleError(self.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.state = 'stop_timeout'
                    self.error = '停止等待超时；应用尚未确认退出，禁止重新启动'
                    raise LifecycleError(self.error)
                self.condition.wait(remaining)

    def stop(self):
        self.request_stop()
        self.wait_stopped()
