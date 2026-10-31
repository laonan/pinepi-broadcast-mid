import base64
import json
from pathlib import Path
import secrets
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from identities import Identities, unique_object


def config():
    return {
        'producers': {
            'web-console': {'token': 'producer-secret', 'description': 'Manual web console', 'targets': '*'},
            'news-feed': {'token': 'news-secret', 'description': 'Morning news', 'targets': ['living-room']},
        },
        'devices': {
            'living-room': {'token': 'device-secret', 'description': 'Living-room Hub'},
            'epaper': {'token': 'epaper-secret', 'description': 'Kitchen display'},
        },
    }


def test_named_identity_lookup_and_descriptions_are_separate_from_credentials():
    identities = Identities(config())
    producer = identities.producer_for_token('producer-secret')
    device = identities.device_for_token('device-secret')
    assert producer.id == 'web-console'
    assert producer.description == 'Manual web console'
    assert device.id == 'living-room'
    assert device.description == 'Living-room Hub'
    assert identities.producer_for_token('web-console') is None
    assert identities.producer_for_token('device-secret') is None
    assert identities.device_for_token('producer-secret') is None
    assert identities.device_for_token('Living-room Hub') is None
    assert 'producer-secret' not in repr(producer)
    assert 'device-secret' not in repr(device)


def test_optional_descriptions_do_not_change_permissions_or_identity():
    cfg = config()
    del cfg['producers']['web-console']['description']
    del cfg['devices']['living-room']['description']
    identities = Identities(cfg)
    assert identities.producers['web-console'].description == ''
    assert identities.devices['living-room'].description == ''
    assert identities.producers['news-feed'].targets == ('living-room',)
    cfg['producers']['news-feed']['targets'] = []
    assert Identities(cfg).producers['news-feed'].targets == ()


def test_old_token_keyed_structure_and_per_entry_identity_override_are_rejected():
    cfg = config()
    cfg['producers'] = {'legacy-secret': {'targets': '*'}}
    with pytest.raises(RuntimeError, match='legacy token-keyed configuration is unsupported') as error:
        Identities(cfg)
    assert 'legacy-secret' not in str(error.value)
    cfg = config()
    cfg['producers']['web-console']['id'] = 'another-identity'
    with pytest.raises(RuntimeError, match='mapping key'):
        Identities(cfg)


@pytest.mark.parametrize('case', ['duplicate_producer', 'duplicate_device', 'shared_role', 'unknown_target', 'missing_targets', 'invalid_targets', 'invalid_token', 'invalid_description', 'device_permissions'])
def test_invalid_credentials_descriptions_and_targets_fail_closed(case):
    cfg = config()
    if case == 'duplicate_producer': cfg['producers']['news-feed']['token'] = 'producer-secret'
    elif case == 'duplicate_device': cfg['devices']['epaper']['token'] = 'device-secret'
    elif case == 'shared_role': cfg['producers']['web-console']['token'] = 'device-secret'
    elif case == 'unknown_target': cfg['producers']['news-feed']['targets'] = ['unknown']
    elif case == 'missing_targets': del cfg['producers']['web-console']['targets']
    elif case == 'invalid_targets': cfg['producers']['web-console']['targets'] = 'living-room'
    elif case == 'invalid_token': cfg['producers']['web-console']['token'] = ' secret with spaces '
    elif case == 'invalid_description': cfg['devices']['living-room']['description'] = 42
    elif case == 'device_permissions': cfg['devices']['living-room']['targets'] = '*'
    with pytest.raises(RuntimeError): Identities(cfg)


def test_duplicate_json_keys_are_rejected_without_logging_legacy_secrets():
    with pytest.raises(RuntimeError, match='duplicate object keys') as error:
        json.loads('{"producers":{"secret-key":{},"secret-key":{}}}', object_pairs_hook=unique_object)
    assert 'secret-key' not in str(error.value)


@pytest.mark.parametrize('role', ['producers', 'devices'])
def test_installer_preflight_still_rejects_reusing_a_credential_as_signing_key(tmp_path, role):
    cfg = config()
    credential = secrets.token_urlsafe(32)
    identity_id = next(iter(cfg[role]))
    cfg[role][identity_id]['token'] = credential
    cfg['media'] = {'data_dir': str(tmp_path / 'media'), 'public_base_url': 'https://mid.example.org',
                    'signing_key_base64': base64.b64encode(credential.encode()).decode()}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'python' / 'deploy_check.py'),
                             'validate', '--config', str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'independent' in result.stderr
    assert credential not in result.stdout + result.stderr
