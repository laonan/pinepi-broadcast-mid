"""Exercise the real installer in a disposable tree with OS/service commands mocked.

No root privileges, network, production files or real systemd are used. Python
imports and configuration preflight run against the actual application code.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copytree(REPO / 'python', source / 'python', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(REPO / 'examples' / 'web', source / 'examples' / 'web')
    for name in ('requirements.txt', 'config.json.example', 'pinepi-broadcast-mid.service'):
        shutil.copyfile(REPO / name, source / name)
    paths = {
        '/opt/pinepi-broadcast-mid': str(tmp_path / 'app'),
        '/etc/pinepi-broadcast-mid': str(tmp_path / 'config'),
        '/var/lib/pinepi-broadcast-mid': str(tmp_path / 'state'),
        '/etc/systemd/system': str(tmp_path / 'units'),
        '/var/www/pinepi-broadcast-mid': str(tmp_path / 'web'),
    }
    script = (REPO / 'install.sh').read_text().replace('if [[ $EUID -ne 0 ]]; then', 'if false; then', 1)
    for old, new in paths.items():
        script = script.replace(old, new)
    (source / 'install.sh').write_text(script)
    for path in paths.values():
        Path(path).mkdir(parents=True)
    config = {
        'producers': {'web-console': {'description': 'Web console', 'token': 'producer-secret', 'targets': '*'}},
        'devices': {'hub': {'description': 'Home Hub', 'token': 'device-secret'}},
        'media': {'data_dir': paths['/var/lib/pinepi-broadcast-mid'] + '/media',
                  'public_base_url': 'https://mid.example.org',
                  'signing_key_base64': base64.b64encode(secrets.token_bytes(32)).decode()},
    }
    production_config = Path(paths['/etc/pinepi-broadcast-mid']) / 'config.json'
    production_config.write_text(json.dumps(config))
    # A local source config must NEVER overwrite an existing production config.
    (source / 'config.json').write_text('{"wrong": "source configuration"}')
    app = Path(paths['/opt/pinepi-broadcast-mid'])
    (app / 'broadcast_server_mid.py').write_text('legacy application\n')
    (app / 'venv').mkdir()
    (app / 'venv' / 'broken-env-marker').touch()
    unit = Path(paths['/etc/systemd/system']) / 'pinepi-broadcast-mid.service'
    unit.write_text('legacy unit\n')
    web = Path(paths['/var/www/pinepi-broadcast-mid'])
    (web / 'index.html').write_text('legacy web\n')
    state = Path(paths['/var/lib/pinepi-broadcast-mid'])
    (state / 'cached-delivery-marker').write_text('keep me')
    active = tmp_path / 'active'
    active.touch()
    command_log = tmp_path / 'commands'
    mocks = tmp_path / 'bin'
    mocks.mkdir()

    def executable(name, body):
        path = mocks / name
        path.write_text('#!' + sys.executable + '\n' + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    common = f'''
import os, sys
from pathlib import Path
args = sys.argv[1:]
with open({str(command_log)!r}, 'a') as log:
    log.write(Path(sys.argv[0]).name + ' ' + ' '.join(args) + '\\n')
'''
    executable('uname', 'print("Linux")\n')
    executable('apt-get', common)
    executable('id', common)
    executable('chown', common)
    executable('journalctl', common)
    executable('systemctl', common + f'''
active = Path({str(active)!r})
if args[0] == 'is-active': sys.exit(0 if active.exists() else 3)
if args[0] == 'stop':
    if not active.exists() and not Path({str(unit)!r}).exists(): sys.exit(5)
    active.unlink(missing_ok=True)
if args[0] == 'restart': active.touch()
''')
    executable('flock', '''
import fcntl, sys
fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
''')
    executable('install', common + f'''
clean = []
while args:
    arg = args.pop(0)
    if arg in ('-o', '-g'): args.pop(0)
    else: clean.append(arg)
os.execv({shutil.which('install')!r}, ['install', *clean])
''')
    executable('runuser', common + '''
args = args[args.index('--') + 1:]
os.execv(args[0], args)
''')
    executable('mv', common + '''
args = [arg for arg in args if arg not in ('-T', '-Tf')]
os.replace(args[0], args[1])
''')
    mock_python = executable('python3', common + f'''
if args[:2] == ['-m', 'venv']:
    binary = Path(args[2]) / 'bin' / 'python'
    binary.parent.mkdir(parents=True)
    binary.symlink_to({str(mocks / 'python3')!r})
    sys.exit(0)
if args[:2] == ['-m', 'pip']:
    if 'install' in args and Path({str(tmp_path / 'fail-pip')!r}).exists():
        print('simulated dependency installation failure', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if args and args[0].endswith('deploy_check.py') and args[1] == 'health':
    sys.exit(1 if Path({str(tmp_path / 'fail-health')!r}).exists() else 0)
os.execv({sys.executable!r}, [{sys.executable!r}, *args])
''')

    def run(*extra):
        env = {**os.environ, 'PATH': str(mocks) + os.pathsep + os.environ['PATH']}
        return subprocess.run(['bash', str(source / 'install.sh'), '--python', mock_python, *extra],
                              text=True, capture_output=True, env=env)

    return {'run': run, 'source': source, 'app': app, 'unit': unit, 'web': web, 'state': state,
            'config': production_config, 'active': active, 'log': command_log, 'root': tmp_path}


def test_upgrade_preserves_config_and_media_and_repairs_environment(deployment):
    d = deployment
    config_before = d['config'].read_bytes()
    result = d['run']()
    assert result.returncode == 0, result.stdout + result.stderr
    assert d['config'].read_bytes() == config_before
    assert d['state'].joinpath('cached-delivery-marker').read_text() == 'keep me'
    current = d['app'] / 'current'
    assert current.is_symlink()
    assert current.joinpath('media_store.py').exists()
    assert current.joinpath('venv/bin/python').exists()
    assert d['app'].joinpath('venv/broken-env-marker').exists()  # legacy install retained
    assert 'current/venv/bin/python -m uvicorn' in d['unit'].read_text()
    assert d['web'].is_symlink()
    assert d['web'].joinpath('message-protocol.js').exists()
    assert d['active'].exists()
    assert 'producer-secret' not in result.stdout + result.stderr
    log = d['log'].read_text()
    assert log.index('-m pip check') < log.index('systemctl stop')
    assert log.index('deploy_check.py validate') < log.index('systemctl stop')


def test_dependency_failure_leaves_running_service_and_web_untouched(deployment):
    d = deployment
    d['root'].joinpath('fail-pip').touch()
    result = d['run']()
    assert result.returncode != 0
    assert d['active'].exists()
    assert d['unit'].read_text() == 'legacy unit\n'
    assert d['web'].joinpath('index.html').read_text() == 'legacy web\n'
    assert not d['app'].joinpath('current').exists()
    assert 'systemctl stop' not in d['log'].read_text()


def test_invalid_config_aborts_before_stopping_old_service(deployment):
    d = deployment
    config = json.loads(d['config'].read_text())
    config['devices']['hub']['token'] = 'producer-secret'
    d['config'].write_text(json.dumps(config))
    result = d['run']()
    assert result.returncode != 0
    assert 'must be distinct' in result.stderr
    assert d['active'].exists()
    assert d['unit'].read_text() == 'legacy unit\n'
    assert 'systemctl stop' not in d['log'].read_text()


def test_failed_readiness_restores_legacy_service_and_web(deployment):
    d = deployment
    d['root'].joinpath('fail-health').touch()
    result = d['run']()
    assert result.returncode != 0
    assert 'restoring the previous' in result.stderr
    assert not d['app'].joinpath('current').exists()
    assert d['unit'].read_text() == 'legacy unit\n'
    assert d['web'].is_dir() and not d['web'].is_symlink()
    assert d['web'].joinpath('index.html').read_text() == 'legacy web\n'
    assert d['active'].exists()


def test_repeat_upgrade_and_rollback_restore_previous_release(deployment):
    d = deployment
    result = d['run']()
    assert result.returncode == 0, result.stderr
    previous = d['app'].joinpath('current').readlink()
    previous_web = d['web'].readlink()
    previous_unit = d['unit'].read_bytes()
    d['root'].joinpath('fail-health').touch()
    result = d['run']()
    assert result.returncode != 0
    assert d['app'].joinpath('current').readlink() == previous
    assert d['web'].readlink() == previous_web
    assert d['unit'].read_bytes() == previous_unit
    assert d['active'].exists()
    d['root'].joinpath('fail-health').unlink()
    result = d['run']()
    assert result.returncode == 0, result.stderr
    assert d['app'].joinpath('current').readlink() != previous


def test_first_install_creates_template_and_does_not_start_service(deployment):
    d = deployment
    d['config'].unlink()
    d['source'].joinpath('config.json').unlink()
    result = d['run']()
    assert result.returncode == 2
    assert 'REPLACE_WITH' in d['config'].read_text()
    assert not d['log'].exists() or 'systemctl restart' not in d['log'].read_text()
    assert 'Fill in credentials' in result.stderr


def test_web_directory_cannot_replace_media_or_configuration(deployment):
    d = deployment
    for unsafe in (str(d['state']), str(d['config'].parent), '/', '//', '/var/www/..'):
        result = d['run']('--web-dir', unsafe)
        assert result.returncode == 2
    assert d['state'].joinpath('cached-delivery-marker').exists()


def test_readiness_checks_local_api_without_proxy_credentials_or_redirects(monkeypatch):
    import importlib.util
    import urllib.error
    spec = importlib.util.spec_from_file_location('deploy_check_test', REPO / 'python/deploy_check.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    received = []

    class Opener:
        def open(self, request, timeout):
            received.append(request)
            assert request.full_url == 'http://127.0.0.1:8765/api/send'
            assert request.method == 'POST'
            assert not request.has_header('Authorization')
            raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)

    def build_opener(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], helper.NoRedirect)
        return Opener()

    monkeypatch.setattr(helper.urllib.request, 'build_opener', build_opener)
    helper.health()
    assert len(received) == 1
    assert helper.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://third-party.example') is None


def test_readiness_does_not_treat_a_static_200_page_as_success(monkeypatch):
    import contextlib
    import importlib.util
    spec = importlib.util.spec_from_file_location('deploy_check_test', REPO / 'python/deploy_check.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    received = []

    class Opener:
        def open(self, request, timeout):
            received.append(request)
            return contextlib.nullcontext(object())

    monkeypatch.setattr(helper.urllib.request, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(helper.time, 'sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='did not return HTTP 401'):
        helper.health()
    assert len(received) == 30


@pytest.mark.parametrize('fail_health', [False, True])
def test_first_valid_install_uses_source_config_and_handles_no_previous_unit(deployment, fail_health):
    d = deployment
    valid_config = d['config'].read_bytes()
    d['source'].joinpath('config.json').write_bytes(valid_config)
    d['config'].unlink()
    d['unit'].unlink()
    d['active'].unlink()
    shutil.rmtree(d['web'])
    if fail_health:
        d['root'].joinpath('fail-health').touch()
    result = d['run']()
    assert d['config'].read_bytes() == valid_config
    if fail_health:
        assert result.returncode != 0
        assert not d['unit'].exists()
        assert not d['active'].exists()
        assert not d['web'].exists()
        assert not d['app'].joinpath('current').exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert d['unit'].exists()
        assert d['active'].exists()
        assert d['web'].joinpath('message-protocol.js').exists()


def test_legacy_producer_configuration_is_rejected_without_exposing_secret_keys(deployment):
    d = deployment
    cfg = json.loads(d['config'].read_text())
    cfg['producers'] = {'legacy-secret': {'targets': '*'}}
    d['config'].write_text(json.dumps(cfg))
    result = d['run']()
    assert result.returncode != 0
    assert 'legacy token-keyed configuration is unsupported' in result.stderr
    assert 'legacy-secret' not in result.stdout + result.stderr
    assert d['active'].exists()
    assert 'systemctl stop' not in d['log'].read_text()
