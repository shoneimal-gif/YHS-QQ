"""One-shot SSH stdout exporter of verified, normalized source evidence.

This file is sent on SSH stdin, never installed on the observer host. Database
and committed WAL pages are copied into memory without opening SQLite against
the production files or creating shared-memory indexes. See SQLite's official
file format specification, sections 4.1, 4.2 and 4.5. Nothing in this module
connects to a broker, sends a message, or grants order authorization.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import sys
import time


_APP = Path('/opt/yhs-qq/alpha-personal')
_RUNTIME = Path('/var/lib/yhs-qq/alpha-personal')
_CONFIG = Path('/etc/yhs-qq/alpha_paper_config.json')
_COPIER = Path('/var/lib/yhs-qq/alpha-paper/copier.sqlite3')
_PREFIX = 'discord:1434960637561409689:1492437553171660921:'
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[1-9][0-9]{0,19}\Z')
_TIER = re.compile(r'T[1-9][0-9]{0,2}\Z')
_NUMBER = re.compile(r'(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?\Z')
_MAX_DB = 64 * 1024 * 1024
_MAX_WAL = 64 * 1024 * 1024
_MAX_STATE = 8 * 1024 * 1024
_SOURCE_REASONS = frozenset({
    'source_deleted_or_revised', 'source_update_verification_pending',
    'source_revision_unconfirmed', 'source_identifier_unconfirmed',
    'source_snapshot_invalid', 'unsupported_source_syntax',
    'source_provenance_mismatch', 'source_creation_time_mismatch',
    'non_live_or_replayed_source', 'source_before_live_activation',
    'source_expired', 'source_delivery_expired', 'timestamp_unconfirmed',
    'observer_not_live_healthy', 'observer_database_unavailable',
    'observer_status_unavailable', 'personal_observer_binding_mismatch',
    'personal_observer_schema_mismatch', 'personal_source_binding_required',
    'invalid_timing_policy', 'linked_personal_source_refused',
})
_ERRORS = _SOURCE_REASONS | frozenset({
    'source_snapshot_unsupported', 'source_snapshot_busy',
    'source_snapshot_invalid', 'source_snapshot_size_limit',
    'source_path_refused', 'source_configuration_unavailable',
    'source_activation_unavailable', 'source_export_unavailable',
    'source_import_unavailable', 'source_read_budget_exceeded',
})


class ExportError(ValueError):
    pass


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ExportError('source_snapshot_invalid')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ExportError('source_snapshot_invalid') from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _ordinary(path, maximum, *, optional=False):
    for parent in reversed(tuple(path.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ExportError('source_path_refused')
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ExportError('source_export_unavailable') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ExportError('source_path_refused')
    if not 0 <= info.st_size <= maximum:
        raise ExportError('source_snapshot_size_limit')
    return _identity(info)


def _read(path, maximum, *, optional=False):
    before = _ordinary(path, maximum, optional=optional)
    if before is None:
        return b'', None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != before:
            raise ExportError('source_snapshot_busy')
        data = stream.read(maximum + 1)
        after = _identity(os.fstat(stream.fileno()))
    if len(data) > maximum:
        raise ExportError('source_snapshot_size_limit')
    if before != after or before != _ordinary(path, maximum):
        raise ExportError('source_snapshot_busy')
    return data, before


def _checksum(data, endian, seed=(0, 0)):
    first, second = seed
    for left, right in struct.iter_unpack(endian + 'II', data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def _merge(database, wal):
    if len(database) < 512 or database[:16] != b'SQLite format 3\x00':
        raise ExportError('source_snapshot_invalid')
    page_size = int.from_bytes(database[16:18], 'big')
    page_size = 65536 if page_size == 1 else page_size
    if (not 512 <= page_size <= 65536 or page_size & (page_size - 1)
            or len(database) % page_size):
        raise ExportError('source_snapshot_invalid')
    result = bytearray(database)
    if wal:
        if len(wal) < 32:
            raise ExportError('source_snapshot_busy')
        magic, version, wal_page, _, salt1, salt2, sum1, sum2 = struct.unpack('>8I', wal[:32])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or wal_page != page_size:
            raise ExportError('source_snapshot_invalid')
        endian = '<' if magic == 0x377f0682 else '>'
        checksum = _checksum(wal[:24], endian)
        if checksum != (sum1, sum2):
            raise ExportError('source_snapshot_invalid')
        pending = {}
        for offset in range(32, len(wal) - (24 + page_size) + 1, 24 + page_size):
            page, commit, frame_salt1, frame_salt2, sum1, sum2 = struct.unpack('>6I', wal[offset:offset + 24])
            # A reset can leave an older tail behind; it ends the valid WAL.
            if (frame_salt1, frame_salt2) != (salt1, salt2):
                break
            content = wal[offset + 24:offset + 24 + page_size]
            checksum = _checksum(wal[offset:offset + 8], endian, checksum)
            checksum = _checksum(content, endian, checksum)
            if checksum != (sum1, sum2):
                raise ExportError('source_snapshot_busy')
            if not 1 <= page <= _MAX_DB // page_size or commit > _MAX_DB // page_size:
                raise ExportError('source_snapshot_size_limit')
            pending[page] = content
            if commit:
                if max(pending) > commit:
                    raise ExportError('source_snapshot_invalid')
                new_size = commit * page_size
                if len(result) < new_size:
                    result.extend(b'\x00' * (new_size - len(result)))
                for number, data in pending.items():
                    result[(number - 1) * page_size:number * page_size] = data
                del result[new_size:]
                pending.clear()
        # Uncommitted frames (including a partial final frame) are excluded.
    # The memory image contains committed WAL pages and has no disk WAL.
    result[18:20] = b'\x01\x01'
    return bytes(result)


def _memory_database(path, generations):
    if not hasattr(sqlite3.Connection, 'deserialize'):
        raise ExportError('source_snapshot_unsupported')
    wal_path, journal = Path(str(path) + '-wal'), Path(str(path) + '-journal')
    for _ in range(3):
        try:
            journal_before = _ordinary(journal, _MAX_WAL, optional=True)
            if journal_before is not None and journal_before[3]:
                raise ExportError('source_snapshot_busy')
            before = _ordinary(path, _MAX_DB)
            wal_before = _ordinary(wal_path, _MAX_WAL, optional=True)
            if wal_before is not None and wal_before[2] != before[2]:
                raise ExportError('source_path_refused')
            database, _ = _read(path, _MAX_DB)
            wal, _ = _read(wal_path, _MAX_WAL, optional=True)
            if (before != _ordinary(path, _MAX_DB)
                    or wal_before != _ordinary(wal_path, _MAX_WAL, optional=True)
                    or journal_before != _ordinary(journal, _MAX_WAL, optional=True)):
                raise ExportError('source_snapshot_busy')
            content = _merge(database, wal)
            # Do not admit a concurrently replaced/reset WAL after decoding.
            if (before != _ordinary(path, _MAX_DB)
                    or wal_before != _ordinary(wal_path, _MAX_WAL, optional=True)):
                raise ExportError('source_snapshot_busy')
            connection = sqlite3.connect(':memory:')
            try:
                connection.deserialize(content)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('PRAGMA temp_store=MEMORY')
                deadline = time.monotonic() + 3
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                generations.extend(((path, _MAX_DB, before), (wal_path, _MAX_WAL, wal_before),
                                    (journal, _MAX_WAL, journal_before)))
                return connection
            except Exception:
                connection.close()
                raise
        except ExportError as exc:
            if str(exc) != 'source_snapshot_busy':
                raise
    raise ExportError('source_snapshot_busy')


def _normalized(parsed):
    action, tier, contract = parsed.get('action_kind'), parsed.get('signal_tier'), parsed.get('contract')
    if action not in ('open', 'close') or (tier is not None and (not isinstance(tier, str) or not _TIER.fullmatch(tier))):
        raise ExportError('source_snapshot_invalid')
    if not isinstance(contract, dict):
        raise ExportError('source_snapshot_invalid')
    symbol, right, strike, expiry, occ = (contract.get(key) for key in (
        'underlying', 'right', 'strike_decimal', 'expiration', 'occ_symbol'))
    price = parsed.get('entry_reference_price_decimal' if action == 'open' else 'exit_reference_price_decimal')
    if (not isinstance(symbol, str) or not re.fullmatch(r'[A-Z]{1,6}', symbol) or right not in ('C', 'P')
            or not isinstance(strike, str) or not _NUMBER.fullmatch(strike)
            or not isinstance(price, str) or not _NUMBER.fullmatch(price)
            or not isinstance(expiry, str) or not isinstance(occ, str)):
        raise ExportError('source_snapshot_invalid')
    try:
        expiration, strike_value, price_value = date.fromisoformat(expiry), Decimal(strike), Decimal(price)
        encoded = int(strike_value * 1000)
        if (not 2000 <= expiration.year <= 2099 or not 0 < strike_value <= Decimal('99999.999')
                or strike_value * 1000 != encoded or not 0 < price_value <= Decimal('999999.999999')
                or occ != symbol + expiration.strftime('%y%m%d') + right + f'{encoded:08d}'):
            raise ValueError
    except (ValueError, InvalidOperation):
        raise ExportError('source_snapshot_invalid') from None
    return {'action': action, 'tier': tier,
            'contract': {'underlying': symbol, 'right': right, 'strike_decimal': strike,
                         'expiration': expiry, 'occ_symbol': occ},
            'reference_price_decimal': price}


def export_snapshot():
    """Read existing evidence in place; return only explicitly permitted fields."""
    now = datetime.now(timezone.utc)
    output = {'version': 1, 'mode': 'alpha_ibkr_read_only_source',
              'checked_utc': now.isoformat(), 'source_connected': False,
              'source_healthy': False, 'source_error': None,
              'source_session_started_utc': None, 'events': [],
              'orders_submitted': False}
    source = ledger = None
    generations = []
    try:
        sys.dont_write_bytecode = True
        # Resolve only the existing application's fixed source module.
        _ordinary(_APP / 'alpha_discord' / 'personal_source.py', 262144)
        sys.path.insert(0, str(_APP))
        from alpha_discord.personal_source import PersonalSource, PersonalSourceError
        config_raw, config_identity = _read(_CONFIG, 131072)
        if config_identity[2] != os.geteuid() or config_identity[3] == 0:
            raise ExportError('source_configuration_unavailable')
        config = json.loads(config_raw)
        if not isinstance(config, dict):
            raise ExportError('source_configuration_unavailable')
        source = PersonalSource(config, _RUNTIME)
        source.db = _memory_database(_RUNTIME / 'personal_observer.sqlite3', generations)
        feed = source.feed()  # Exact source/account/policy binding and live health.
        output.update(source_connected=True, source_healthy=True,
                      source_session_started_utc=_stamp(feed.get('connected_since_utc')).isoformat())
        ledger = _memory_database(_COPIER, generations)
        state_row = ledger.execute('SELECT payload FROM engine_state WHERE id=1 AND length(payload)<=?', (_MAX_STATE,)).fetchone()
        if state_row is None or not isinstance(state_row[0], str):
            raise ExportError('source_activation_unavailable')
        state = json.loads(state_row[0])
        if not isinstance(state, dict) or state.get('schema_version') != 1:
            raise ExportError('source_activation_unavailable')
        activated_at = _stamp(state.get('activated_at')).isoformat()
        if _stamp(activated_at) > now + timedelta(seconds=5):
            raise ExportError('source_activation_unavailable')
        cutoff = (now - timedelta(hours=8)).isoformat()
        rows = source.db.execute('''SELECT m.source_key,m.message_id,m.latest_revision,
            m.created_utc,m.first_received_utc FROM personal_messages m
            WHERE m.last_received_utc>=? OR EXISTS (
                SELECT 1 FROM personal_tombstones t WHERE t.message_id=m.message_id AND t.received_utc>=?)
            OR EXISTS (SELECT 1 FROM personal_update_checks u
                WHERE u.message_id=m.message_id AND u.received_utc>=?)
            ORDER BY m.last_received_utc DESC,m.source_key DESC LIMIT 50''', (cutoff, cutoff, cutoff)).fetchall()
        events = []
        for row in reversed(rows):
            key, revision, message_id = row['source_key'], row['latest_revision'], row['message_id']
            if (not isinstance(message_id, str) or not _ID.fullmatch(message_id) or int(message_id) >= 2**64
                    or key != _PREFIX + message_id or not isinstance(revision, str) or not _SHA.fullmatch(revision)):
                raise ExportError('source_snapshot_invalid')
            created, received = _stamp(row['created_utc']), _stamp(row['first_received_utc'])
            event = {'source_id': key, 'revision': revision,
                     'source_created_utc': created.isoformat(), 'source_received_utc': received.isoformat(),
                     'source_age_seconds': round((now - created).total_seconds(), 6),
                     'status': 'unavailable', 'reason': None, 'verified': False,
                     'broker_eligible': False, 'action': None, 'tier': None,
                     'contract': None, 'reference_price_decimal': None}
            try:
                parsed, _ = source.source(key, revision, activated_at)
                event.update(_normalized(parsed), status='verified', verified=True)
            except PersonalSourceError as exc:
                reason = exc.category if exc.category in _SOURCE_REASONS else 'source_snapshot_invalid'
                event['reason'] = reason
                event['status'] = ('invalidated' if reason == 'source_deleted_or_revised'
                                   else 'pending' if reason == 'source_update_verification_pending' else 'unavailable')
                if exc.permanent:
                    raise ExportError(reason) from None
            events.append(event)
        # Revalidate the live source and unchanged disk generation immediately
        # before emission; a later observation remains a new polling snapshot.
        source.feed()
        for path, maximum, expected in generations:
            if expected != _ordinary(path, maximum, optional=True):
                raise ExportError('source_snapshot_busy')
        if config_identity != _ordinary(_CONFIG, 131072):
            raise ExportError('source_snapshot_busy')
        output['events'] = events
    except Exception as exc:
        category = getattr(exc, 'category', None) or (str(exc) if isinstance(exc, ExportError) else None)
        output.update(source_healthy=False, source_error=category if category in _ERRORS else 'source_export_unavailable', events=[])
    finally:
        if source is not None:
            source.close()
        if ledger is not None:
            ledger.close()
    output['checked_utc'] = datetime.now(timezone.utc).isoformat()
    return output


if __name__ == '__main__':
    print(json.dumps(export_snapshot(), ensure_ascii=True, allow_nan=False, separators=(',', ':')))
