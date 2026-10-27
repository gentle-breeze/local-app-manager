import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import deployment
from lifecycle import LifecycleError
from server import Manager


def git(cwd, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c',
                                   'user.email=fixture@example.invalid', *args],
                                  cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.source = self.root / 'working'
        self.source.mkdir()
        git(self.source, 'init', '-b', 'main')
        (self.source / 'app.py').write_text('local dirty code')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'local')
        git(self.source, 'remote', 'add', 'origin', 'https://github.com/example/wrong-repo')
        (self.source / 'app.py').write_text('uncommitted changes must survive')
        (self.source / 'untracked').write_text('keep me')
        self.remotes = {}
        for name in ('one', 'two'):
            repo = self.root / name
            repo.mkdir()
            git(repo, 'init', '-b', 'main')
            self.write_version(repo, name)
            self.remotes['https://github.com/example/' + name] = str(repo)
        self.manager = Manager(self.root / 'data')
        self.original_git = deployment._git
        self.transport = patch('deployment._git', side_effect=self.fixture_git)
        self.transport.start()

    def tearDown(self):
        self.manager.close()
        self.transport.stop()
        self.temp.cleanup()

    def fixture_git(self, args, cwd, home):
        # Only the test transport permits local fixture repositories. Production
        # validation and argv construction remain unchanged.
        args = [self.remotes.get(arg, arg) for arg in args]
        env = dict(PATH=os.environ['PATH'], HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
        return deployment._run(['git', '-c', 'protocol.file.allow=always', *args],
                               cwd, env, '拉取配置分支')

    def write_version(self, repo, version):
        (repo / 'app.py').write_text("import time\nprint(%r, flush=True)\nwhile True: time.sleep(.1)\n" % version)
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', version)

    def app(self, name='one', **extra):
        app = dict(name=name, cwd=str(self.source), port=0, readonly=False,
                   argv=[sys.executable, str(self.source / 'app.py')],
                   github_url='https://github.com/example/' + name, **extra)
        self.manager.save(app)
        return self.manager.apps[-1]['id']

    def wait_log(self, ident, text):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if text in self.manager.runtimes[ident].snapshot()['logs']:
                return
            time.sleep(.02)
        self.fail('application did not report expected fetched version ' + text)

    def test_fetches_latest_main_not_dirty_source_or_mismatched_origin(self):
        before = git(self.source, 'status', '--porcelain')
        ident = self.app()
        self.manager.action(ident, 'start')
        self.wait_log(ident, 'one')
        old = self.manager.runtimes[ident]
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        self.write_version(repo, 'new main')
        self.manager.action(ident, 'restart')
        self.wait_log(ident, 'new main')
        self.assertEqual(old.snapshot()['lifecycle'], 'stopped')
        self.assertEqual(self.manager.deployments[ident]['deployed_commit'], git(repo, 'rev-parse', 'HEAD'))
        self.assertEqual(git(self.source, 'status', '--porcelain'), before)
        self.assertEqual((self.source / 'app.py').read_text(), 'uncommitted changes must survive')
        self.assertEqual((self.source / 'untracked').read_text(), 'keep me')
        self.assertEqual(git(self.source, 'remote', 'get-url', 'origin'), 'https://github.com/example/wrong-repo')

    def test_project_urls_are_independent(self):
        one, two = self.app('one'), self.app('two')
        for ident, name in ((one, 'one'), (two, 'two')):
            self.manager.action(ident, 'restart')
            self.wait_log(ident, name)
        self.assertNotEqual(self.manager.active_apps[one]['cwd'], self.manager.active_apps[two]['cwd'])
        self.assertNotEqual(self.manager.deployments[one]['deployed_commit'], self.manager.deployments[two]['deployed_commit'])

    def test_fetch_failure_leaves_running_service_untouched(self):
        ident = self.app()
        self.manager.action(ident, 'start')
        self.wait_log(ident, 'one')
        old = self.manager.runtimes[ident]
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        git(repo, 'branch', '-m', 'not-main')
        with self.assertRaisesRegex(deployment.DeploymentError, '拉取配置分支失败'):
            self.manager.action(ident, 'restart')
        self.assertIs(self.manager.runtimes[ident], old)
        self.assertEqual(old.snapshot()['lifecycle'], 'running')
        self.assertEqual(self.manager.deployments[ident]['deployment_phase'], 'error')
        self.assertEqual(len(list((self.root / 'data/releases').iterdir())), 1)

    def test_build_failure_leaves_running_service_and_revision(self):
        ident = self.app(build_argv=[sys.executable, '-c', "from pathlib import Path; assert not Path('broken').exists()"])
        self.manager.action(ident, 'start')
        self.wait_log(ident, 'one')
        old = self.manager.runtimes[ident]
        revision = self.manager.deployments[ident]['deployed_commit']
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        (repo / 'broken').touch()
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'broken build')
        with self.assertRaisesRegex(deployment.DeploymentError, '构建失败'):
            self.manager.action(ident, 'restart')
        self.assertIs(self.manager.runtimes[ident], old)
        self.assertEqual(old.snapshot()['lifecycle'], 'running')
        self.assertEqual(self.manager.deployments[ident]['deployed_commit'], revision)

    def test_build_can_generate_release_startup_file(self):
        ident = self.app(build_argv=[sys.executable, '-c', "from pathlib import Path; Path('built.py').write_text(Path('app.py').read_text())"])
        self.manager.apps[-1]['argv'][1] = 'built.py'
        self.manager.action(ident, 'start')
        self.wait_log(ident, 'one')
        self.assertFalse((self.source / 'built.py').exists())

    def test_operations_and_edits_are_serialized_during_prepare(self):
        ident = self.app()
        entered, finish = threading.Event(), threading.Event()
        errors = []
        def prepare(*args):
            entered.set(); finish.wait(5)
            raise deployment.DeploymentError('synthetic fetch failure')
        def restart():
            try: self.manager.action(ident, 'restart')
            except deployment.DeploymentError as error: errors.append(error)
        with patch('server.prepare_release', side_effect=prepare):
            worker = threading.Thread(target=restart); worker.start()
            self.assertTrue(entered.wait(2))
            try:
                for action in ('start', 'stop', 'restart'):
                    with self.assertRaises(LifecycleError): self.manager.action(ident, action)
                with self.assertRaises(ValueError): self.manager.save(self.manager.apps[-1])
            finally:
                finish.set(); worker.join(5)
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.manager.operations[ident].locked())

    def test_uncertain_guard_prevents_fetch_and_launch(self):
        ident = self.app()
        self.manager.guard(ident).write_text('{}')
        with patch('server.prepare_release') as prepare:
            with self.assertRaises(LifecycleError): self.manager.action(ident, 'restart')
            prepare.assert_not_called()

    def test_close_during_prepare_does_not_launch(self):
        ident = self.app()
        original = deployment.prepare_release
        def prepare(*args):
            result = original(*args)
            self.manager.close()
            return result
        with patch('server.prepare_release', side_effect=prepare):
            with self.assertRaises(LifecycleError): self.manager.action(ident, 'restart')
        self.assertFalse(self.manager.runtimes)
        self.assertEqual(list((self.root / 'data/releases').iterdir()), [])

    def test_config_rejects_credentials_and_non_github_transport(self):
        for url in ('https://user:secret@github.com/a/b', '/tmp/repo', 'file:///tmp/repo',
                    'https://github.com/a/b?token=x', 'https://github.com/a/..',
                    'git@github.com:a/b', '--upload-pack=x', 'https://evil.test/a/b'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                deployment.deployment_config(dict(github_url=url, readonly=False))
        self.assertEqual(deployment.deployment_config(dict(github_url='https://github.com/a/b.git', readonly=False))['github_url'], 'https://github.com/a/b')

    def test_production_git_does_not_inherit_credentials(self):
        with patch('deployment._run', return_value='') as run:
            self.original_git(['fetch', 'https://github.com/a/b', 'refs/heads/main'], self.source, self.root / 'home')
        args, cwd, env, phase = run.call_args.args
        self.assertIn('credential.helper=', args)
        self.assertIn('protocol.file.allow=never', args)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertEqual(env['HOME'], str(self.root / 'home'))
        self.assertNotIn('GITHUB_TOKEN', env)

    def test_symlinked_cwd_and_absolute_script_use_release(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        self.manager.save(dict(name='alias', cwd=str(alias), port=0, readonly=False,
                               argv=[sys.executable, str(alias / 'app.py')],
                               github_url='https://github.com/example/one'))
        ident = self.manager.apps[-1]['id']
        self.manager.action(ident, 'start')
        self.wait_log(ident, 'one')
        self.assertTrue(self.manager.active_apps[ident]['argv'][1].startswith(str(self.root / 'data/releases')))

    def test_path_traversal_and_embedded_source_path_are_rejected(self):
        release = self.root / 'release'
        for arg in (str(self.source / '../other.py'), '../app.py', '--file=' + str(self.source / 'app.py')):
            with self.subTest(arg=arg), self.assertRaises(deployment.DeploymentError):
                deployment.release_argv([arg], str(self.source), release)
        normalized = str(self.source / '../working/app.py')
        self.assertEqual(deployment.release_argv([normalized], str(self.source), release), [str(release / 'app.py')])

    def test_relative_data_directory_produces_absolute_release(self):
        ident = self.app()
        app = self.manager.apps[-1]
        relative = os.path.relpath(self.root / 'relative-data')
        prepared, _ = deployment.prepare_release(app, relative, lambda _: None)
        self.assertTrue(pathlib.Path(prepared['cwd']).is_absolute())
        self.assertTrue((pathlib.Path(prepared['cwd']) / 'app.py').is_file())

    def test_missing_remote_script_does_not_stop_old_service(self):
        ident = self.app()
        self.manager.action(ident, 'start'); self.wait_log(ident, 'one')
        old = self.manager.runtimes[ident]
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        git(repo, 'rm', 'app.py'); git(repo, 'commit', '-m', 'remove script')
        with self.assertRaisesRegex(deployment.DeploymentError, '缺少启动命令'):
            self.manager.action(ident, 'restart')
        self.assertEqual(old.snapshot()['lifecycle'], 'running')

    def test_immediate_exit_is_not_reported_as_healthy_deployment(self):
        ident = self.app()
        self.manager.action(ident, 'start'); self.wait_log(ident, 'one')
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        (repo / 'app.py').write_text('raise RuntimeError("synthetic crash")')
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'crash')
        try:
            self.manager.action(ident, 'restart')
        except LifecycleError:
            pass  # Can fail before or just after the startup event.
        deadline = time.monotonic() + 5
        while self.manager.running(ident) and time.monotonic() < deadline:
            time.sleep(.02)
        state = self.manager.snapshot()[0]
        self.assertIn(state['deployment_phase'], ('error', 'exited'))
        self.assertTrue(state['deployment_error'])
        self.assertTrue(pathlib.Path(state['previous_cwd']).is_dir())

    def test_build_descendants_are_cleaned_for_success_failure_and_timeout(self):
        for code in (0, 1, None):
            marker = self.root / ('descendant-' + str(code))
            script = ("import os,time,sys\n"
                      "pid=os.fork()\n"
                      "if pid == 0:\n"
                      " time.sleep(.5)\n"
                      " open(%r,'w').write('leaked')\n"
                      " time.sleep(10)\n"
                      "else:\n" % str(marker))
            script += ' time.sleep(10)\n' if code is None else ' sys.exit(%s)\n' % code
            with self.subTest(code=code), self.assertRaises(deployment.DeploymentError):
                deployment._run([sys.executable, '-c', script], self.source, {}, '构建', .1)
            time.sleep(.55)
            self.assertFalse(marker.exists(), 'background build child survived cleanup')

    def test_output_is_bounded_without_blocking_verbose_build(self):
        output = deployment._run([sys.executable, '-c', "print('x'*1000000)"], self.source, {}, '构建')
        self.assertEqual(len(output), 4096)

    def test_unconfirmed_preparation_blocks_after_manager_reopen(self):
        ident = self.app()
        guard = self.manager.prepare_guards / self.manager.guard(ident).name
        guard.write_text('{}')
        self.manager.close()
        self.manager = Manager(self.root / 'data')
        with patch('server.prepare_release') as prepare:
            with self.assertRaisesRegex(LifecycleError, '准备进程'):
                self.manager.action(ident, 'restart')
            prepare.assert_not_called()

    def test_missing_build_executable_does_not_leave_crash_barrier(self):
        executable = self.root / 'builder'
        executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o700)
        ident = self.app(build_argv=[str(executable)])
        executable.unlink()
        with self.assertRaises(ValueError):
            self.manager.action(ident, 'start')
        self.assertFalse((self.manager.prepare_guards / self.manager.guard(ident).name).exists())
        app = dict(self.manager.apps[-1], build_argv=[])
        self.manager.save(app)
        self.manager.action(ident, 'start'); self.wait_log(ident, 'one')

    def test_default_branch_and_legacy_config(self):
        self.assertEqual(deployment.deployment_config({})['github_branch'], 'main')
        self.assertEqual(deployment.deployment_config({'github_branch': ''})['github_branch'], 'main')
        ident = self.app()
        self.manager.apps[-1].pop('github_branch')  # Pre-feature persisted entry.
        self.manager.action(ident, 'start'); self.wait_log(ident, 'one')

    def test_configured_slash_branch_is_fetched_independently_per_project(self):
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        git(repo, 'checkout', '-b', 'release/stable')
        self.write_version(repo, 'stable branch version')
        stable = self.app(github_branch='release/stable')
        main = self.app(github_branch='main')
        other = self.app('two', github_branch='main')
        for ident, message in ((stable, 'stable branch version'), (main, 'one'), (other, 'two')):
            self.manager.action(ident, 'start'); self.wait_log(ident, message)
        self.assertEqual(self.manager.deployments[stable]['deployed_commit'], git(repo, 'rev-parse', 'release/stable'))
        self.assertEqual(self.manager.deployments[main]['deployed_commit'], git(repo, 'rev-parse', 'main'))
        self.assertNotEqual(self.manager.deployments[stable]['deployed_commit'], self.manager.deployments[main]['deployed_commit'])
        saved = __import__('json').loads(self.manager.path.read_text())
        self.assertEqual(next(a for a in saved if a['id'] == stable)['github_branch'], 'release/stable')

    def test_missing_configured_branch_does_not_stop_old_service(self):
        repo = pathlib.Path(self.remotes['https://github.com/example/one'])
        git(repo, 'branch', 'release/stable')
        ident = self.app(github_branch='release/stable')
        self.manager.action(ident, 'start'); self.wait_log(ident, 'one')
        old = self.manager.runtimes[ident]
        git(repo, 'branch', '-D', 'release/stable')
        with self.assertRaisesRegex(deployment.DeploymentError, '拉取配置分支失败'):
            self.manager.action(ident, 'restart')
        self.assertIs(self.manager.runtimes[ident], old)
        self.assertEqual(old.snapshot()['lifecycle'], 'running')

    def test_invalid_branch_names_rejected_before_fetch(self):
        invalid = [None, 12, [], '--upload-pack=x', '-bad', 'a:b', 'a*b', 'a?b',
                   'a^b', 'a~b', 'a[b', 'a\\b', 'a b', 'a\n', 'a\0b',
                   'a..b', '../main', 'a//b', '/main', 'main/', '.hidden',
                   'a/.hidden', 'a.lock', 'a.lock/b', 'a.', '@', 'a@{b}', 'a' * 201]
        for branch in invalid:
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.app(github_branch=branch)
        self.assertFalse(list(self.manager.prepare_guards.iterdir()))
        self.assertFalse((self.manager.data / 'releases').exists())

    def test_timeout_is_explicit(self):
        with self.assertRaisesRegex(deployment.DeploymentError, '构建超时'):
            deployment._run([sys.executable, '-c', 'import time; time.sleep(10)'],
                            self.source, {}, '构建', .05)


if __name__ == '__main__':
    unittest.main()
