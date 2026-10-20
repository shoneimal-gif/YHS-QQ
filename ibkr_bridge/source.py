"""Read normalized source snapshots over an explicitly pinned SSH connection.

The existing observer keeps all Discord credentials. This client never stores
them, modifies SSH settings, forwards ports, or talks to any trading API.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
from typing import Any, Mapping


_PREFIX = 'discord:1434960637561409689:1492437553171660921:'
_SOURCE_KEYS = {'host', 'user', 'port', 'identity_file', 'known_hosts_file', 'poll_seconds'}
_EVENT_KEYS = {'source_id', 'revision', 'source_created_utc', 'source_received_utc', 'source_age_seconds',
               'status', 'reason', 'verified', 'broker_eligible', 'action', 'tier', 'contract', 'reference_price_decimal'}
_ENVELOPE_KEYS = {'version', 'mode', 'checked_utc', 'source_connected', 'source_healthy',
                  'source_error', 'source_session_started_utc', 'events', 'orders_submitted'}
_REASONS = frozenset({
    'source_deleted_or_revised', 'source_update_verification_pending',
    'source_revision_unconfirmed', 'source_identifier_unconfirmed',
    'source_snapshot_invalid', 'unsupported_source_syntax', 'source_provenance_mismatch',
    'source_creation_time_mismatch', 'non_live_or_replayed_source',
    'source_before_live_activation', 'source_expired', 'source_delivery_expired',
    'timestamp_unconfirmed', 'observer_not_live_healthy', 'observer_database_unavailable',
    'observer_status_unavailable', 'personal_observer_binding_mismatch',
    'personal_observer_schema_mismatch', 'personal_source_binding_required',
    'invalid_timing_policy', 'linked_personal_source_refused',
    'source_snapshot_unsupported', 'source_snapshot_busy', 'source_snapshot_size_limit',
    'source_path_refused', 'source_configuration_unavailable', 'source_activation_unavailable',
    'source_export_unavailable', 'source_import_unavailable', 'source_read_budget_exceeded',
})
_DECIMAL = re.compile(r'(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?\Z')


class SourceError(ValueError):
    """Fixed safe diagnostic category, never raw SSH stderr or remote text."""
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _ordinary_file(value, maximum, *, allow_hardlinks=False):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
        raise SourceError('source_local_path_refused')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or value.startswith(('\\\\', '//')):
        raise SourceError('source_local_path_refused')
    try:
        for item in reversed((path,) + tuple(path.parents)):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
                raise SourceError('source_local_path_refused')
        if (not stat.S_ISREG(info.st_mode) or (not allow_hardlinks and info.st_nlink != 1)
                or not 0 < info.st_size <= maximum):
            raise SourceError('source_local_path_refused')
        return path
    except OSError:
        raise SourceError('source_local_path_unavailable') from None


def _digest(path, maximum, *, allow_hardlinks=False):
    _ordinary_file(str(path), maximum, allow_hardlinks=allow_hardlinks)
    try:
        before = path.stat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise SourceError('source_local_pin_changed')
            raw = stream.read(maximum + 1)
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) > maximum):
            raise SourceError('source_local_pin_changed')
        return hashlib.sha256(raw).digest()
    except OSError:
        raise SourceError('source_local_path_unavailable') from None


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise SourceError('source_response_invalid')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise SourceError('source_response_invalid') from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError('source_response_invalid')
        result[key] = value
    return result


def _reason(value):
    return value is None or (isinstance(value, str) and value in _REASONS)


def _validated_response(raw):
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(SourceError('source_response_invalid')))
    except (UnicodeError, ValueError, RecursionError):
        raise SourceError('source_response_invalid') from None
    if (not isinstance(result, dict) or set(result) != _ENVELOPE_KEYS or type(result['version']) is not int
            or result['version'] != 1 or result['mode'] != 'alpha_ibkr_read_only_source'
            or type(result['source_connected']) is not bool or type(result['source_healthy']) is not bool
            or result['orders_submitted'] is not False or not _reason(result['source_error'])
            or not isinstance(result['events'], list) or len(result['events']) > 50
            or (result['source_healthy'] and (not result['source_connected'] or result['source_error'] is not None))
            or (not result['source_healthy'] and (result['events'] or result['source_error'] is None))):
        raise SourceError('source_response_invalid')
    checked = _stamp(result['checked_utc'])
    if not -5 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 30:
        raise SourceError('source_response_stale')
    if result['source_session_started_utc'] is not None:
        if (_stamp(result['source_session_started_utc']) - checked).total_seconds() > 5:
            raise SourceError('source_response_invalid')
    elif result['source_healthy']:
        raise SourceError('source_response_invalid')
    seen = set()
    for event in result['events']:
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise SourceError('source_response_invalid')
        key, revision, status = event['source_id'], event['revision'], event['status']
        if (not isinstance(key, str) or not key.startswith(_PREFIX)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', key[len(_PREFIX):]) or int(key[len(_PREFIX):]) >= 2**64
                or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)
                or key in seen or not isinstance(status, str) or status not in {'verified', 'invalidated', 'pending', 'unavailable'}
                or type(event['verified']) is not bool or event['verified'] != (status == 'verified')
                or event['broker_eligible'] is not False or not _reason(event['reason'])
                or (status == 'verified' and event['reason'] is not None)
                or (status != 'verified' and event['reason'] is None)
                or (status == 'pending' and event['reason'] != 'source_update_verification_pending')
                or (status == 'invalidated' and event['reason'] != 'source_deleted_or_revised')):
            raise SourceError('source_response_invalid')
        seen.add(key)
        created, received = _stamp(event['source_created_utc']), _stamp(event['source_received_utc'])
        age = event['source_age_seconds']
        snowflake_created = datetime.fromtimestamp(((int(key[len(_PREFIX):]) >> 22) + 1420070400000) / 1000, timezone.utc)
        if (type(age) not in (int, float) or not math.isfinite(age)
                or abs((created - snowflake_created).total_seconds()) > 1
                or abs((checked - created).total_seconds() - age) > 20):
            raise SourceError('source_response_invalid')
        if status != 'verified':
            if any(event[name] is not None for name in ('action', 'tier', 'contract', 'reference_price_decimal')):
                raise SourceError('source_response_invalid')
            continue
        if (not -5 <= (checked - created).total_seconds() <= 120
                or not -5 <= (checked - received).total_seconds() <= 120
                or event['action'] not in ('open', 'close')
                or (event['tier'] is not None and (not isinstance(event['tier'], str) or not re.fullmatch(r'T[1-9][0-9]{0,2}', event['tier'])))):
            raise SourceError('source_response_invalid')
        contract, price = event['contract'], event['reference_price_decimal']
        if (not isinstance(contract, dict) or set(contract) != {'underlying', 'right', 'strike_decimal', 'expiration', 'occ_symbol'}
                or not isinstance(contract['underlying'], str) or not re.fullmatch(r'[A-Z]{1,6}', contract['underlying'])
                or contract['right'] not in ('C', 'P') or not isinstance(contract['strike_decimal'], str)
                or not _DECIMAL.fullmatch(contract['strike_decimal']) or not isinstance(price, str) or not _DECIMAL.fullmatch(price)
                or not isinstance(contract['expiration'], str) or not isinstance(contract['occ_symbol'], str)):
            raise SourceError('source_response_invalid')
        try:
            expiry, strike, value = date.fromisoformat(contract['expiration']), Decimal(contract['strike_decimal']), Decimal(price)
            encoded = int(strike * 1000)
            if (not 2000 <= expiry.year <= 2099 or not 0 < strike <= Decimal('99999.999')
                    or strike * 1000 != encoded or not 0 < value <= Decimal('999999.999999')
                    or contract['occ_symbol'] != contract['underlying'] + expiry.strftime('%y%m%d') + contract['right'] + f'{encoded:08d}'):
                raise ValueError
        except (ValueError, InvalidOperation):
            raise SourceError('source_response_invalid') from None
    return result


class SourceClient:
    """Synchronous bounded snapshot client; each call is read-only."""

    def __init__(self, config: Mapping[str, Any]):
        if not isinstance(config, Mapping) or set(config) not in (_SOURCE_KEYS, _SOURCE_KEYS | {'ssh_exe'}):
            raise SourceError('source_configuration_invalid')
        host, user, port, poll = (config[key] for key in ('host', 'user', 'port', 'poll_seconds'))
        if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host)
                or '..' in host or not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user)
                or type(port) is not int or not 1 <= port <= 65535
                or type(poll) not in (int, float) or not math.isfinite(poll) or not 1 <= poll <= 60):
            raise SourceError('source_configuration_invalid')
        default_exe = str(Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32' / 'OpenSSH' / 'ssh.exe')
        if os.name != 'nt':
            default_exe = '/usr/bin/ssh'
        # Windows component-store executables can have legitimate hard links.
        # Keep the executable's exact path/content pinned; private input files
        # must still each have a single ordinary directory entry.
        self.ssh = _ordinary_file(config.get('ssh_exe', default_exe), 64 * 1024 * 1024,
                                  allow_hardlinks=True)
        self.identity = _ordinary_file(config['identity_file'], 65536)
        self.known_hosts = _ordinary_file(config['known_hosts_file'], 1024 * 1024)
        self.exporter = _ordinary_file(str(Path(__file__).with_name('source_export.py').absolute()), 131072)
        self.files = ((self.ssh, 64 * 1024 * 1024, True), (self.identity, 65536, False),
                      (self.known_hosts, 1024 * 1024, False), (self.exporter, 131072, False))
        self.pins = tuple(_digest(path, maximum, allow_hardlinks=links)
                          for path, maximum, links in self.files)
        self.host, self.user, self.port, self.poll_seconds = host, user, port, float(poll)

    def close(self):
        """No persistent SSH session or background collector is retained."""

    def _check_pins(self):
        if tuple(_digest(path, maximum, allow_hardlinks=links)
                 for path, maximum, links in self.files) != self.pins:
            raise SourceError('source_local_pin_changed')

    def snapshot(self) -> dict[str, Any]:
        self._check_pins()
        try:
            with self.exporter.open('rb') as stream:
                program = stream.read(131073)
        except OSError:
            raise SourceError('source_local_path_unavailable') from None
        if len(program) > 131072 or hashlib.sha256(program).digest() != self.pins[3]:
            raise SourceError('source_local_pin_changed')
        null_path = 'NUL' if os.name == 'nt' else '/dev/null'
        argv = [str(self.ssh), '-F', null_path, '-T', '-i', str(self.identity),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'KexAlgorithms=curve25519-sha256', '-o', 'HostKeyAlgorithms=ssh-ed25519',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
                '-o', 'UserKnownHostsFile=' + str(self.known_hosts),
                '-o', 'GlobalKnownHostsFile=' + null_path,
                '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                '-o', 'PreferredAuthentications=publickey', '-o', 'NumberOfPasswordPrompts=0',
                '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=8',
                '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
                '-o', 'LogLevel=ERROR', '-p', str(self.port),
                self.user + '@' + self.host, 'python3 -B -']
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            raise SourceError('source_ssh_unavailable') from None
        output, errors = bytearray(), bytearray()
        overflow = threading.Event()
        write_failed = threading.Event()

        def kill():
            try:
                process.kill()
            except OSError:
                pass

        def drain(stream, target, maximum):
            try:
                while chunk := stream.read(4096):
                    if len(target) + len(chunk) > maximum:
                        overflow.set()
                        kill()
                        break
                    target.extend(chunk)
            except OSError:
                overflow.set()

        def send():
            try:
                process.stdin.write(program)
                process.stdin.close()
            except (OSError, ValueError):
                write_failed.set()
                kill()

        readers = [threading.Thread(target=drain, args=(process.stdout, output, 262144), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, errors, 16384), daemon=True)]
        writer = threading.Thread(target=send, daemon=True)
        started = []
        try:
            deadline = time.monotonic() + 20
            for worker in readers + [writer]:
                worker.start()
                started.append(worker)
            try:
                # Stdin can block while SSH negotiates authentication. Keep
                # that write in its own thread under this same process budget.
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                kill()
                raise SourceError('source_ssh_timeout') from None
            except OSError:
                raise SourceError('source_ssh_failed') from None
            join_deadline = time.monotonic() + 3
            for worker in started:
                worker.join(timeout=max(0, join_deadline - time.monotonic()))
            if overflow.is_set() or any(worker.is_alive() for worker in started):
                raise SourceError('source_response_size_limit')
            self._check_pins()
            if process.returncode != 0 or write_failed.is_set():
                raise SourceError('source_ssh_failed')
            return _validated_response(bytes(output))
        finally:
            cleanup_deadline = time.monotonic() + 3
            if process.poll() is None:
                kill()
                try:
                    process.wait(timeout=max(0.001, cleanup_deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for worker in started:
                worker.join(timeout=max(0, cleanup_deadline - time.monotonic()))
            # A Python buffered stream's close can wait on another thread's
            # blocking write/read lock. Never perform that close until its
            # owner has stopped; these workers are daemon threads and bounded
            # joins prevent a stalled remote pipe from freezing the monitor.
            owners = ((process.stdin, writer), (process.stdout, readers[0]),
                      (process.stderr, readers[1]))
            for stream, owner in owners:
                if owner.is_alive():
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
