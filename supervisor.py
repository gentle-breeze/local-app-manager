"""Own one process group; parent pipe loss also triggers cleanup. No PID recovery."""
import json, os, signal, subprocess, sys, threading, time

def main():
    config = json.loads(sys.stdin.readline())
    os.setsid()
    signal.signal(signal.SIGTERM, lambda *_: None)
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    try:
        child = subprocess.Popen(config['argv'], cwd=config['cwd'], stdin=subprocess.DEVNULL)
        while child.poll() is None and not stop.wait(.1):
            pass
    except Exception:
        print('[manager] application failed to start; check executable and directory', flush=True)
    finally:
        os.killpg(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        os.killpg(os.getpid(), signal.SIGKILL)

if __name__ == '__main__':
    main()
