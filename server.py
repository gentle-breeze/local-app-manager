import argparse, fcntl, hashlib, http.client, json, os, pathlib, re, secrets, signal, socket, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from lifecycle import LifecycleError, ManagedProcess, shutdown_config

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULTS = []  # Register applications through the UI; no machine-specific seeds.

def redact(text):
    return re.sub(r'(?im)^.*(?:token|secret|password|authorization|api[_-]?key).*$','[REDACTED sensitive line]',text)

def run(args):
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=2, text=True)
    except (OSError, subprocess.SubprocessError):
        return ''

def probe(app):
    """Only explicit loopback TCP/HTTP; no redirects, credentials or response bodies."""
    port = app['port']
    result = dict(reachable=False, health='未知', pid=None, uptime=None, cpu=None, memory=None, identity='未核实')
    if not port:
        return result
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.4):
            result['reachable'] = True
    except OSError:
        return result
    pids = run(['/usr/sbin/lsof', '-nP', '-iTCP:%s' % port, '-sTCP:LISTEN', '-t']).split()
    if len(set(pids)) == 1:
        pid = int(pids[0])
        cwd = run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
        if '\nn' + os.path.realpath(app['cwd']) + '\n' in cwd:
            result.update(pid=pid, identity='端口 + 工作目录已核实')
            fields = run(['/bin/ps', '-p', str(pid), '-o', 'etime=,%cpu=,rss=']).split()
            if len(fields) == 3:
                result.update(uptime=fields[0], cpu=fields[1], memory=round(int(fields[2])/1024, 1))
        else:
            result['identity'] = '端口被占用，应用身份未核实'
    if app.get('health'):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
        try:
            connection.request('GET', app['health'])
            status = connection.getresponse().status
            result['health'] = 'HTTP %s · %s' % (status, '可访问' if 200 <= status < 400 else '异常/需认证')
        except (OSError, http.client.HTTPException):
            result['health'] = 'HTTP 探测失败'
        finally:
            connection.close()
    return result

class Manager:
    def __init__(self, data):
        self.data = pathlib.Path(data)
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lockfile = open(self.data/'instance.lock', 'a')
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise RuntimeError('此数据目录已有管理器运行')
        self.guards = self.data/'unconfirmed'
        self.guards.mkdir(mode=0o700, exist_ok=True)
        self.path = self.data/'apps.json'
        self.apps = json.loads(self.path.read_text()) if self.path.exists() else DEFAULTS.copy()
        self.children, self.logs, self.runtimes = {}, {}, {}
        self.operations = {}
        self.closed = False
        self.lock = threading.RLock()

    def save(self, app):
        with self.lock:
            if set(app) - {'id','name','cwd','argv','port','health','readonly','logpath','stop_signal','stop_timeout','stop_policy'}:
                raise ValueError('不支持的配置字段')
            if self.closed:
                raise LifecycleError('管理器正在退出')
            app = app.copy()
            app.update(shutdown_config(app))
            old = next((a for a in self.apps if a['id'] == app.get('id')), None)
            if old and (self.running(old['id']) or (old['id'] in self.operations and self.operations[old['id']].locked()) or old['readonly'] != bool(app.get('readonly', True))):
                raise ValueError('只读或运行中的条目不可修改')
            if not isinstance(app.get('name'), str) or not 0 < len(app['name']) <= 80:
                raise ValueError('请输入应用名称')
            if app.get('cwd') and (not isinstance(app['cwd'], str) or not os.path.isabs(app['cwd']) or not os.path.isdir(app['cwd'])):
                raise ValueError('工作目录必须是现有绝对路径')
            app.setdefault('cwd', '')
            argv = app.get('argv', [])
            app['argv'] = argv
            if not app.get('readonly', True) and not app['cwd']:
                raise ValueError('受管应用需要工作目录')
            if not isinstance(argv, list) or not 0 <= len(argv) <= 40 or any(not isinstance(v,str) or len(v)>1024 or '\0' in v for v in argv):
                raise ValueError('命令必须是 JSON 字符串数组')
            if not app.get('readonly', True) and (not argv or not os.path.isabs(argv[0]) or not os.access(argv[0], os.X_OK)):
                raise ValueError('可执行文件必须是可执行的绝对路径')
            if type(app.get('port')) is not int or not 0 <= app['port'] <= 65535:
                raise ValueError('端口范围 0–65535；0 表示不探测')
            health = app.get('health', '')
            if not isinstance(health, str) or len(health)>200 or (health and not re.fullmatch(r'/[A-Za-z0-9/_-]*', health)):
                raise ValueError('健康路径仅支持无查询参数的本地路径')
            logpath = app.get('logpath', '')
            if logpath and (not isinstance(logpath,str) or not os.path.isabs(logpath) or not logpath.endswith('.log') or not os.path.isfile(logpath) or os.path.islink(logpath)):
                raise ValueError('日志需为明确指定的普通 .log 文件')
            app.update(id=old['id'] if old else secrets.token_hex(8), readonly=bool(app.get('readonly', True)), health=health)
            updated = [a for a in self.apps if a['id'] != app['id']] + [app]
            temp = self.data/'apps.tmp'
            fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(updated, f, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
            self.apps = updated

    def guard(self, ident):
        return self.guards / hashlib.sha256(ident.encode()).hexdigest()

    def runtime_state(self, ident):
        runtime = self.runtimes.get(ident)
        if runtime:
            return runtime.snapshot()
        uncertain = self.guard(ident).exists()
        return dict(managed=uncertain, lifecycle='error' if uncertain else 'stopped',
                    lifecycle_error='上次运行尚未确认停止；请人工核实，禁止重新启动' if uncertain else '', logs='')

    def running(self, ident):
        return self.runtime_state(ident)['managed']

    def action(self, ident, action):
        with self.lock:
            if self.closed:
                raise LifecycleError('管理器正在退出')
            app = next((a.copy() for a in self.apps if a['id']==ident), None)
            if not app or app['readonly']:
                raise ValueError('只管理明确登记、由本管理器启动的应用')
            if action not in ('start','stop','restart'):
                raise ValueError('未知操作')
            operation = self.operations.setdefault(ident, threading.Lock())
            if not operation.acquire(blocking=False):
                raise LifecycleError('应用正在执行启停操作；请等待本次操作完成')
        try:
            runtime = self.runtimes.get(ident)
            if action in ('stop','restart'):
                if runtime:
                    runtime.stop()
                elif self.guard(ident).exists():
                    raise LifecycleError('上次运行尚未确认停止；没有可安全操作的进程句柄')
            if action in ('start','restart'):
                with self.lock:
                    if self.closed:
                        raise LifecycleError('管理器正在退出')
                    if self.running(ident):
                        raise LifecycleError('应用尚未确认停止；不能重复启动')
                    if app['port'] and probe(app)['reachable']:
                        raise ValueError('端口已占用；不会接管既有进程')
                    runtime = ManagedProcess(app, self.lockfile.fileno(), redact, self.guard(ident))
                    self.runtimes[ident] = runtime
                    self.children[ident] = runtime.child
                    self.logs[ident] = runtime.logs
        finally:
            operation.release()

    def snapshot(self):
        with self.lock:
            apps = [dict(a, **self.runtime_state(a['id'])) for a in self.apps]
        for app in apps:
            app.update(probe(app))
            if app.get('logpath'):
                try:
                    fd = os.open(app['logpath'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as f:
                        import stat
                        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise OSError()
                        f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
                        app['logs'] = redact(f.read(16384).decode('utf-8','replace'))
                except OSError:
                    app['logs'] = '[manager] 指定日志不可读取' 
        return apps

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            runtimes = list(self.runtimes.values())
        # Notify every app before waiting on any; logs/status stay drainable.
        try:
            for runtime in runtimes:
                try:
                    runtime.request_stop(disconnect=True)
                except (LifecycleError, OSError) as error:
                    print('[manager] ' + str(error), file=sys.stderr)
            for runtime in runtimes:
                try:
                    runtime.wait_stopped()
                except (LifecycleError, OSError) as error:
                    print('[manager] ' + str(error), file=sys.stderr)
        finally:
            # Live supervisors retain the lock; crashed supervisors leave their
            # durable generation marker, so reopening cannot forget uncertainty.
            self.lockfile.close()

def serve(port=4320, data=ROOT/'.local'):
    manager = Manager(data)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, status, body, content='application/json'):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content+'; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(payload)
        def allowed(self):
            return self.headers.get('Host') == '127.0.0.1:%s' % self.server.server_port
        def do_GET(self):
            if not self.allowed(): return self.reply(403, {'error':'Host rejected'})
            if self.headers.get('Sec-Fetch-Site') in ('cross-site','same-site'):
                return self.reply(403, {'error':'Cross-site rejected'})
            if self.path == '/api/state':
                return self.reply(200, {'apps':manager.snapshot(), 'token':token, 'sampled':time.time()})
            files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if self.path not in files: return self.reply(404, {})
            name = files[self.path]
            self.reply(200, (ROOT/'web'/name).read_bytes(), {'html':'text/html','js':'text/javascript','css':'text/css'}[name.split('.')[-1]])
        def do_POST(self):
            origin = 'http://127.0.0.1:%s' % self.server.server_port
            if not self.allowed() or self.headers.get('Origin') != origin or self.headers.get('X-CSRF-Token') != token or self.headers.get('Content-Type') != 'application/json':
                return self.reply(403, {'error':'Origin / session verification failed'})
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16384: raise ValueError('请求过大或为空')
                body = json.loads(self.rfile.read(length))
                if self.path == '/api/apps': manager.save(body)
                elif self.path == '/api/action': manager.action(body['id'], body['action'])
                else: return self.reply(404, {})
                self.reply(200, {'ok':True})
            except LifecycleError as error:
                self.reply(409, {'error':str(error)})
            except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
                self.reply(400, {'error':'操作未完成：请检查配置、只读状态与端口占用；运行中的条目不能编辑。'})
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except Exception:
        manager.close(); raise
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    print('Local App Manager: http://127.0.0.1:%s' % server.server_port, flush=True)
    try: server.serve_forever()
    finally: server.server_close(); manager.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4320)
    parser.add_argument('--data-dir', default=str(ROOT/'.local'))
    args = parser.parse_args()
    serve(args.port, args.data_dir)
