"""Own a live process group until *all* members have actually exited.

The direct child is deliberately not reaped until group cleanup is confirmed.
Its reserved PID anchors the PGID, so no signal can target a recycled group.
Applications which create another session/process group are not supported.
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

from lifecycle import KILL_CONFIRM_TIMEOUT, shutdown_config


def group_state(pgid):
    """Return (leader alive, any live member); zombies have already exited.

    killpg(pgid, 0) cannot distinguish dead, unreaped descendants from live ones.
    ps works on Linux and macOS; a failed/unparseable scan fails closed.
    """
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='],
                            capture_output=True, text=True, check=True, timeout=2)
    leader, alive = False, False
    found_leader = False
    for line in result.stdout.splitlines():
        pid, group, state = line.split()
        if int(group) != pgid:
            continue
        if int(pid) == pgid:
            found_leader = True
        if state[0] not in ('Z', 'X'):
            alive = True
            leader = leader or int(pid) == pgid
    if not found_leader:
        raise RuntimeError('reserved process-group leader missing')
    return leader, alive


def main(status_fd=None, guard_file=None, generation=None):
    def report(state, error=''):
        if status_fd is not None:
            try:
                os.write(status_fd, (json.dumps(dict(state=state, error=error)) + '\n').encode())
            except OSError:
                pass  # Parent loss must not interrupt cleanup.

    def confirmed_stopped():
        # Clear only this launch's marker, after actual group termination.
        # A crash before here intentionally leaves a durable restart barrier.
        if guard_file is not None:
            guard = pathlib.Path(guard_file)
            if json.loads(guard.read_text()).get('generation') != generation:
                raise RuntimeError('lifecycle generation marker changed')
            guard.unlink()
        report('stopped')

    # Do not leave a daemon reader holding Python's buffered-stdin lock at
    # interpreter shutdown. Unbuffered reads also cannot prefetch a stop command.
    config_line = bytearray()
    while len(config_line) <= 65536:
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte or byte == b'\n':
            break
        config_line.extend(byte)
    else:
        raise ValueError('application configuration too large')
    config = json.loads(config_line)
    shutdown = shutdown_config(config)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def control():
        # A stop command or parent/control-pipe loss both request shutdown.
        os.read(sys.stdin.fileno(), 1024)
        stop.set()

    threading.Thread(target=control, daemon=True).start()
    try:
        child = subprocess.Popen(config['argv'], cwd=config['cwd'],
                                 stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        print('[manager] application failed to start; check executable and directory', flush=True)
        confirmed_stopped()  # No application process was created.
        return

    def send_signal(number):
        try:
            os.killpg(child.pid, number)
        except ProcessLookupError:
            pass  # The last live member can exit between scan and signal.
        except OSError:
            report('error', '无法向受管进程组发送信号；禁止重新启动')

    report('running')
    deadline = None
    kill_deadline = None
    timeout_reported = False
    failed_scan = False
    empty_scans = 0
    while True:
        try:
            leader_alive, alive = group_state(child.pid)
            failed_scan = False
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if not failed_scan:
                report('error', '无法核实进程组状态；保留监管，禁止重新启动')
                failed_scan = True
            empty_scans = 0
            time.sleep(.1)
            continue
        # ps is a snapshot: repeat an empty scan so a child forked during the
        # first enumeration is observed before releasing the anchored PGID.
        empty_scans = empty_scans + 1 if not alive else 0
        if empty_scans >= 2:
            child.wait()  # Only now release the PID/PGID reservation.
            confirmed_stopped()
            return
        if not alive:
            time.sleep(.05)
            continue
        now = time.monotonic()
        if deadline is None and (stop.is_set() or not leader_alive):
            report('stopping')
            send_signal(getattr(signal, shutdown['stop_signal']))
            deadline = now + shutdown['stop_timeout']
        if deadline is not None and now >= deadline and not timeout_reported:
            timeout_reported = True
            if shutdown['stop_policy'] == 'kill':
                report('killing')
                send_signal(signal.SIGKILL)
                kill_deadline = now + KILL_CONFIRM_TIMEOUT
            else:
                report('stop_timeout', '停止等待超时；继续等待实际退出，不强制终止')
        if kill_deadline is not None and now >= kill_deadline:
            report('error', '强制停止后仍未确认退出；继续监管，禁止重新启动')
            kill_deadline = None
        time.sleep(.05)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--status-fd', type=int)
    parser.add_argument('--guard-file')
    parser.add_argument('--generation')
    args = parser.parse_args()
    main(args.status_fd, args.guard_file, args.generation)
