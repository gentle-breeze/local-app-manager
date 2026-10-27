"""Opt-in, anonymous GitHub main deployments in independent release directories."""
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from supervisor import group_state

from lifecycle import LifecycleError


class DeploymentError(LifecycleError):
    cleanup_confirmed = True


# Keep anchored PID handles if process-table verification fails. Never reap or
# reuse these groups for signalling. A failed preparation must not be deployed.
_UNCERTAIN_CHILDREN = []


def deployment_config(app):
    url = app.get('github_url', '')
    if not isinstance(url, str) or (url and not re.fullmatch(
            r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', url)):
        raise ValueError('GitHub 地址必须是无凭据的 https://github.com/owner/repo 地址')
    if url and any(part in ('.', '..') for part in url.split('/')[-2:]):
        raise ValueError('GitHub 仓库地址无效')
    build = app.get('build_argv', [])
    if (not isinstance(build, list) or len(build) > 40 or
            any(not isinstance(v, str) or len(v) > 1024 or '\0' in v for v in build) or
            (build and (not os.path.isabs(build[0]) or not os.access(build[0], os.X_OK)))):
        raise ValueError('构建命令必须是 JSON 字符串数组，首项为可执行绝对路径')
    if build and not url:
        raise ValueError('构建命令需要 GitHub 地址')
    if url and app.get('readonly', True):
        raise ValueError('只读观察条目不能配置远程部署')
    return dict(github_url=url[:-4] if url.endswith('.git') else url, build_argv=build)


def _run(args, cwd, env, phase, timeout=300):
    """Bound output in memory and confirm the entire build/fetch group exited."""
    if not all(hasattr(os, name) for name in ('waitid', 'WNOWAIT', 'WEXITED', 'WNOHANG', 'P_PID')):
        raise DeploymentError('当前平台不能保留进程组退出凭据；远程部署不可用')
    try:
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    except OSError as error:
        raise DeploymentError(phase + '失败；请检查 Git、命令及目录权限') from error
    output = bytearray()
    def drain():
        try:
            while True:
                chunk = child.stdout.read(4096)
                if not chunk:
                    break
                output.extend(chunk[:max(0, 4096 - len(output))])
        finally:
            child.stdout.close()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    cleanup_deadline = None
    error = ''
    empty_scans = 0
    while True:
        try:
            # Preserve the leader PID until descendants have exited, like the
            # runtime supervisor. Popen.poll/wait here would release it early.
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            _, alive = group_state(child.pid)
            empty_scans = empty_scans + 1 if not alive else 0
            if exited and empty_scans >= 2:
                code = child.wait()
                reader.join(timeout=1)
                if error or code:
                    raise DeploymentError(error or phase + '失败（退出码 %s）；未部署新版本' % code)
                return bytes(output).decode('utf-8', 'replace').strip()
            if cleanup_deadline is None and ((exited and alive) or time.monotonic() >= deadline):
                error = (phase + '存在未退出的子进程；未部署新版本' if exited else
                         phase + '超时；未部署新版本')
                os.killpg(child.pid, signal.SIGKILL)
                cleanup_deadline = time.monotonic() + 5
        except DeploymentError:
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            empty_scans = 0
            if cleanup_deadline is None:
                error = phase + '无法核实进程组退出；未部署新版本'
                # The unreaped leader still anchors this private process group.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass
                cleanup_deadline = time.monotonic() + 5
        if cleanup_deadline is not None and time.monotonic() >= cleanup_deadline:
            _UNCERTAIN_CHILDREN.append(child)
            failure = DeploymentError(phase + '未确认进程组停止；保留准备目录，请人工核实')
            failure.cleanup_confirmed = False
            raise failure
        time.sleep(.03)


def _git(args, cwd, home):
    # Do not consult retained credentials, global URL rewrites, SSH, or hooks.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false')
    return _run(['git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
                 '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                 '-c', 'http.followRedirects=false', '-c', 'gc.auto=0',
                 '-c', 'maintenance.auto=false', *args], cwd, env, '拉取 main')


def release_argv(argv, source, release):
    """Relocate project paths, including aliases; reject paths escaping a release."""
    lexical_source = pathlib.Path(source).absolute()
    source = lexical_source.resolve()
    result = []
    for value in argv:
        original = value
        if os.path.isabs(value):
            path = pathlib.Path(os.path.normpath(value))
            try:
                relative = path.relative_to(lexical_source)
                path = source / relative
            except ValueError:
                path = path.resolve()
            try:
                value = str(release / path.relative_to(source))
            except ValueError:
                if str(lexical_source) + os.sep in original or str(source) + os.sep in original:
                    raise DeploymentError('项目路径指向工作目录外部；请使用发布目录内的路径')
        elif '..' in pathlib.PurePath(value).parts:
            raise DeploymentError('部署命令不支持上级目录路径')
        if any(str(root) + os.sep in value for root in (source, lexical_source)):
            raise DeploymentError('命令内嵌本地项目路径无法迁移；请使用独立参数或相对路径')
        result.append(value)
    return result


def prepare_release(app, data, phase):
    config = deployment_config(app)
    releases = pathlib.Path(data).resolve() / 'releases'
    try:
        releases.mkdir(mode=0o700, parents=True, exist_ok=True)
        release = pathlib.Path(tempfile.mkdtemp(prefix='release-', dir=releases))
    except OSError as error:
        raise DeploymentError('无法创建发布目录；未停止旧版本') from error
    try:
        phase('fetching')
        # Always fetch the configured URL directly; never use the working copy's
        # origin, branch, index, hooks or dirty files as deployment input.
        with tempfile.TemporaryDirectory(prefix='git-home-') as home:
            _git(['init', '--quiet', str(release)], releases, home)
            _git(['fetch', '--quiet', '--depth=1', '--no-tags', config['github_url'],
                  'refs/heads/main'], release, home)
            commit = _git(['rev-parse', '--verify', 'FETCH_HEAD^{commit}'], release, home)
            _git(['checkout', '--quiet', '--detach', commit], release, home)
        deployed = dict(app, cwd=str(release), argv=release_argv(app['argv'], app['cwd'], release))
        if config['build_argv']:
            phase('building')
            env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'TMPDIR') if k in os.environ}
            _run(release_argv(config['build_argv'], app['cwd'], release), release, env, '构建', 600)
        # Catch missing project scripts/files that existed in the source checkout
        # before stopping a healthy service. Dependency/runtime readiness remains
        # the responsibility of the explicitly configured build command.
        for original, target in zip(app['argv'][1:], deployed['argv'][1:]):
            source_path = pathlib.Path(original) if os.path.isabs(original) else pathlib.Path(app['cwd']) / original
            target_path = pathlib.Path(target) if os.path.isabs(target) else release / target
            if source_path.is_file() and not target_path.is_file():
                raise DeploymentError('新版本缺少启动命令引用的项目文件；未停止旧版本')
        if not os.path.isfile(deployed['argv'][0]) or not os.access(deployed['argv'][0], os.X_OK):
            raise DeploymentError('新版本启动命令不可执行；未停止旧版本')
        return deployed, commit
    except BaseException as error:
        if getattr(error, 'cleanup_confirmed', True):
            shutil.rmtree(release)
        raise
