"""Independent Discord reader configuration; no broker credentials or writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def utc_now():
    return datetime.now(timezone.utc)


def atomic_json(path: Path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Linked state refused')
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_config(path: Path):
    result = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    names = {'source_guild_id', 'source_channel_id', 'target_guild_id', 'target_channel_id',
             'max_signal_age_seconds', 'observer_only'}
    if not isinstance(result, dict) or set(result) != names:
        raise ValueError('Discord reader configuration is incomplete')
    for key in names - {'observer_only', 'max_signal_age_seconds'}:
        value = result[key]
        if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value) or int(value) >= 2**64:
            raise ValueError('Discord channel bindings require exact IDs')
    if result['observer_only'] is not True:
        raise ValueError('This reader only observes; it cannot enable orders')
    if type(result['max_signal_age_seconds']) is not int or not 10 <= result['max_signal_age_seconds'] <= 300:
        raise ValueError('Use a bounded source freshness interval')
    return result


def load_credentials():
    selected = os.environ.get('ALPHA_DISCORD_CREDENTIAL_FILE')
    credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
    if not selected and not credential_dir:
        raise ValueError('Use the dedicated Discord reader credential file')
    path = Path(selected) if selected else Path(credential_dir) / 'discord'
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Credentials must be a regular private file')
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('Credential file is too large')
    values = json.loads(raw)
    if not isinstance(values, dict) or set(values) != {'DISCORD_BOT_TOKEN'}:
        raise ValueError('Use only the reader Bot Token')
    token = values['DISCORD_BOT_TOKEN']
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{32,512}', token):
        raise ValueError('Bot Token format is invalid')
    return values
