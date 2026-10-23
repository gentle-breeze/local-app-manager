import http.client, json, os, pathlib, socket, subprocess, sys, tempfile, time, unittest
from server import Manager, probe

ROOT = pathlib.Path(__file__).resolve().parent

def wait_for(fn, timeout=6):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if fn(): return
        time.sleep(.05)
    raise AssertionError('condition timed out')

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.manager=Manager(self.temp.name)
        self.port=free_port()
        self.manager.save(dict(name='synthetic',cwd=self.temp.name,argv=[sys.executable,'-u','-m','http.server',str(self.port),'--bind','127.0.0.1'],port=self.port,health='/',readonly=False))
        self.app=self.manager.apps[-1]; self.ident=self.app['id']
    def tearDown(self):
        self.manager.close(); self.temp.cleanup()
    def test_lifecycle_duplicate_and_lock(self):
        with self.assertRaises(RuntimeError): Manager(self.temp.name)
        self.manager.action(self.ident,'start'); wait_for(lambda:probe(self.app)['reachable'])
        with self.assertRaises(ValueError): self.manager.action(self.ident,'start')
        with self.assertRaises(ValueError): self.manager.save(self.app.copy())
        old=self.manager.children[self.ident]
        self.manager.action(self.ident,'restart'); self.assertIsNotNone(old.poll())
        wait_for(lambda:probe(self.app)['reachable'])
        self.manager.action(self.ident,'stop'); wait_for(lambda:not probe(self.app)['reachable'])
        self.manager.action(self.ident,'stop') # stale exited handle cannot kill another PID
    def test_external_not_adopted(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',self.port)); listener.listen()
            with self.assertRaises(ValueError): self.manager.action(self.ident,'start')
            self.manager.action(self.ident,'stop')
            self.assertTrue(probe(self.app)['reachable'])
        with self.assertRaises(ValueError): self.manager.action('unregistered-app','stop')
    def test_readonly_minimal_and_log_tail(self):
        log=pathlib.Path(self.temp.name)/'sample.log'; log.write_text('x'*30000+'\nTOKEN=synthetic-secret\nlast line\n')
        self.manager.save(dict(name='observer',port=0,readonly=True,logpath=str(log)))
        snapshot=self.manager.snapshot()[-1]
        self.assertLessEqual(len(snapshot['logs']),16400)
        self.assertNotIn('synthetic-secret',snapshot['logs'])
        self.assertIn('last line',snapshot['logs'])
    def test_group_cleanup_and_bounded_logs(self):
        script=pathlib.Path(self.temp.name)/'tree.py'
        script.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-m","http.server",sys.argv[1],"--bind","127.0.0.1"])\nprint("x"*1000000,flush=True)\ntime.sleep(60)\n')
        self.app['argv']=[sys.executable,str(script),str(self.port)]
        self.manager.action(self.ident,'start'); wait_for(lambda:probe(self.app)['reachable'])
        self.manager.action(self.ident,'stop'); wait_for(lambda:not probe(self.app)['reachable'])
        self.assertLessEqual(len(''.join(self.manager.logs[self.ident])),160*2048)
    def test_parent_pipe_loss(self):
        child=subprocess.Popen([sys.executable,str(ROOT/'supervisor.py')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL)
        child.stdin.write((json.dumps(self.app)+'\n').encode()); child.stdin.flush()
        try:
            wait_for(lambda:probe(self.app)['reachable']); child.stdin.close()
            child.wait(timeout=5); wait_for(lambda:not probe(self.app)['reachable'])
        finally:
            if child.poll() is None: child.stdin.close(); child.wait(timeout=5)

class HTTP(unittest.TestCase):
    def test_security_and_shutdown(self):
        with tempfile.TemporaryDirectory() as data:
            port=free_port(); origin='http://127.0.0.1:%s'%port
            process=subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--port',str(port),'--data-dir',data],stdout=subprocess.DEVNULL)
            def request(method,path,body=None,headers=None):
                c=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                c.request(method,path,body,headers or {});r=c.getresponse();result=(r.status,r.read());c.close();return result
            try:
                wait_for(lambda:probe(dict(port=port,cwd='',health=''))['reachable'])
                self.assertEqual(request('GET','/',headers={'Host':'evil.test'})[0],403)
                self.assertEqual(request('GET','/api/state',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
                status,body=request('GET','/api/state'); self.assertEqual(status,200)
                token=json.loads(body)['token']
                self.assertEqual(request('POST','/api/apps','{}',{'Origin':origin,'Content-Type':'application/json'})[0],403)
                self.assertEqual(request('POST','/api/apps','{}',{'Origin':'http://evil.test','Content-Type':'application/json','X-CSRF-Token':token})[0],403)
                app=dict(name='test',port=0,readonly=True)
                self.assertEqual(request('POST','/api/apps',json.dumps(app),{'Origin':origin,'Content-Type':'application/json','X-CSRF-Token':token})[0],200)
                self.assertEqual(request('GET','/../../server.py')[0],404)
                self.assertTrue((pathlib.Path(data)/'apps.json').exists())
            finally:
                process.terminate();process.wait(timeout=8)

if __name__=='__main__': unittest.main()
