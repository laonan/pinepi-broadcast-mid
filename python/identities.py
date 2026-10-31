"""Named Producer/Device identities with separate, replaceable credentials."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Producer:
    id: str
    token: str = field(repr=False)
    targets: str | tuple[str, ...]
    description: str = ''


@dataclass(frozen=True)
class Device:
    id: str
    token: str = field(repr=False)
    description: str = ''


def unique_object(pairs):
    """Reject duplicate JSON keys without echoing potentially secret legacy keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError('config.json contains duplicate object keys')
        result[key] = value
    return result


class Identities:
    def __init__(self, config):
        if not isinstance(config, dict):
            raise RuntimeError('config.json must contain a JSON object')
        self.producers = {}
        self.devices = {}
        self._producer_tokens = {}
        self._device_tokens = {}
        producers = self._section(config, 'producers')
        devices = self._section(config, 'devices')
        for identity_id, entry in devices.items():
            token, description = self._entry(identity_id, entry, 'devices', {'token', 'description'})
            if token in self._device_tokens:
                raise RuntimeError('Each device must have a unique Device Token')
            device = Device(identity_id, token, description)
            self.devices[identity_id] = device
            self._device_tokens[token] = device
        for identity_id, entry in producers.items():
            token, description = self._entry(identity_id, entry, 'producers', {'token', 'description', 'targets'})
            if token in self._producer_tokens:
                raise RuntimeError('Each Producer must have a unique Producer Token')
            if token in self._device_tokens:
                raise RuntimeError('Producer and Device Tokens must be distinct')
            if 'targets' not in entry:
                raise RuntimeError('producers entries require targets: "*" or an array of device IDs')
            targets = entry['targets']
            if targets != '*':
                if not isinstance(targets, list) or any(not isinstance(t, str) or t not in self.devices for t in targets):
                    raise RuntimeError('Producer targets must be "*" or an array of configured device IDs')
                targets = tuple(dict.fromkeys(targets))
            producer = Producer(identity_id, token, targets, description)
            self.producers[identity_id] = producer
            self._producer_tokens[token] = producer
        if (set(self.producers) | set(self.devices)) & set(self.tokens):
            raise RuntimeError('Identity IDs must not be credentials; choose descriptive IDs')

    @staticmethod
    def _section(config, name):
        value = config.get(name)
        if not isinstance(value, dict) or not value:
            raise RuntimeError(f'config.json: {name} must be a nonempty object keyed by stable IDs')
        return value

    @staticmethod
    def _entry(identity_id, entry, section, allowed):
        if not isinstance(identity_id, str) or not identity_id.strip() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in identity_id) or len(identity_id) > 128:
            raise RuntimeError(f'{section} keys must be nonempty stable IDs of at most 128 characters')
        if not isinstance(entry, dict) or 'token' not in entry:
            raise RuntimeError(f'{section} entries require a token field; use stable IDs as keys (legacy token-keyed configuration is unsupported)')
        if set(entry) - allowed:
            raise RuntimeError(f'{section} entries support only {", ".join(sorted(allowed))}; identity is the mapping key')
        token = entry['token']
        if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise RuntimeError(f'{section}.token must be a nonempty printable ASCII credential without whitespace')
        description = entry.get('description', '')
        if not isinstance(description, str) or len(description) > 500:
            raise RuntimeError(f'{section}.description must be a string of at most 500 characters')
        return token, description.strip()

    @property
    def tokens(self):
        return (*self._producer_tokens, *self._device_tokens)

    def producer_for_token(self, token):
        return self._producer_tokens.get(token)

    def device_for_token(self, token):
        return self._device_tokens.get(token)
