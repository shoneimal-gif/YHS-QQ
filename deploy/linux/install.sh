#!/usr/bin/env bash
# Install files only. This script never starts/enables the trading service or timer.
set -Eeuo pipefail
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fail 'Run with sudo on the Linux server.'
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
APP='/opt/yhs-qq/app'
STATE='/var/lib/yhs-qq'
RUNTIME='/var/lib/yhs-qq/runtime'
KEYS='/etc/yhs-qq'
[[ "$SOURCE" != "$APP" ]] || fail 'Install from a newly extracted deployment bundle, not the live app directory.'
[[ -f /etc/os-release ]] || fail 'Ubuntu 22.04+ or Debian 12+ is required.'
# This file is supplied by the operating system.
source /etc/os-release
case "${ID:-}" in
  ubuntu) [[ "${VERSION_ID%%.*}" -ge 22 ]] || fail 'Ubuntu 22.04+ is required.'
          if [[ "${VERSION_ID%%.*}" -eq 22 ]]; then
            [[ "${VERSION_ID#*.}" -ge 4 ]] || fail 'Ubuntu 22.04+ is required.'
          fi ;;
  debian) [[ "${VERSION_ID%%.*}" -ge 12 ]] || fail 'Debian 12+ is required.' ;;
  *) fail 'Only Ubuntu 22.04+ and Debian 12+ are supported.' ;;
esac
command -v systemctl >/dev/null || fail 'systemd is required.'
[[ -d /run/systemd/system ]] || fail 'systemd must be running as the service manager.'
for unit in yhs-qq.service yhs-qq.timer; do
  active_state="$(systemctl show "$unit" --property=ActiveState --value 2>/dev/null || true)"
  case "$active_state" in
    active|activating|reloading|deactivating)
      fail "$unit is $active_state. Stop it explicitly before upgrading; no running program has been stopped." ;;
  esac
done
timer_enabled_state="$(systemctl is-enabled yhs-qq.timer 2>/dev/null || true)"
case "$timer_enabled_state" in
  enabled|enabled-runtime)
    fail 'yhs-qq.timer is enabled. Disable it explicitly before upgrading to prevent a scheduled-start race; no timer has been changed.' ;;
esac

missing=()
for package in python3 ca-certificates tzdata; do
  package_state="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
  [[ "$package_state" == 'install ok installed' ]] || missing+=("$package")
done
if [[ "${#missing[@]}" -gt 0 ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y "${missing[@]}"
fi

/usr/bin/python3 - "$SOURCE" <<'PY'
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10+ is required.')
root = Path(sys.argv[1]).resolve()
manifest_path = root / 'CLOUD_MANIFEST.json'
if manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit('CLOUD_MANIFEST.json is required and must be a regular file.')
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
files = manifest.get('files') if isinstance(manifest, dict) else None
if not isinstance(files, dict) or not files:
    raise SystemExit('The manifest files mapping is missing.')
required = {
    'main.py', 'config.py', 'detector.py', 'mock_replay.py', 'paper_trader.py',
    'yhs_client.py', 'alpaca_client.py', 'alpaca_trader.py',
    'config.alpaca_paper_100k.json', 'alpaca_policy_100k.json',
    'deploy/linux/install.sh', 'deploy/linux/runner.py', 'deploy/linux/configure_keys.py',
    'deploy/linux/yhs-qq.service', 'deploy/linux/yhs-qq.timer',
}
if not required.issubset(files):
    raise SystemExit('The deployment bundle is incomplete.')
for name, expected in files.items():
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise SystemExit('Invalid manifest path.')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise SystemExit('Manifest paths must stay within the bundle.')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit('Symlinks are not allowed in deployment bundle contents.')
    if not current.resolve().is_relative_to(root) or not current.is_file():
        raise SystemExit('A manifest file is missing or outside the bundle.')
    if not isinstance(expected, str) or re.fullmatch(r'[0-9a-fA-F]{64}', expected) is None:
        raise SystemExit('Invalid SHA256 digest in the manifest.')
    digest = hashlib.sha256()
    with current.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise SystemExit('Deployment integrity check failed for: ' + name)
print('Verified all deployment files against CLOUD_MANIFEST.json.')
PY

# Never replace a runtime directory, credential directory, or app directory symlink.
for directory in /opt/yhs-qq "$APP" "$APP/deploy" "$APP/deploy/linux" "$STATE" "$RUNTIME" "$KEYS"; do
  [[ ! -L "$directory" ]] || fail "Refusing a symlink at $directory."
  [[ ! -e "$directory" || -d "$directory" ]] || fail "$directory is not a directory."
done
if [[ -e "$APP/runtime" || -L "$APP/runtime" ]]; then
  [[ -L "$APP/runtime" ]] || fail "$APP/runtime already exists as a real directory; migrate it before installing."
  [[ "$(readlink -f -- "$APP/runtime")" == "$RUNTIME" ]] || fail 'The app runtime symlink points at an unexpected directory.'
fi
if ! getent group yhsqq >/dev/null; then
  groupadd --system yhsqq
fi
if ! id yhsqq >/dev/null 2>&1; then
  useradd --system --gid yhsqq --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin yhsqq
fi
[[ "$(id -u yhsqq)" -ne 0 && "$(id -gn yhsqq)" == yhsqq ]] || fail 'The existing yhsqq account is not a suitable dedicated service account.'
service_shell="$(getent passwd yhsqq | cut -d: -f7)"
[[ "$service_shell" == /usr/sbin/nologin || "$service_shell" == /sbin/nologin || "$service_shell" == /bin/false ]] || fail 'The existing yhsqq account must have a non-login shell.'
install -d -m 0755 -o root -g root /opt/yhs-qq "$APP" "$APP/deploy" "$APP/deploy/linux"
install -d -m 0700 -o root -g root "$KEYS"
install -d -m 0700 -o yhsqq -g yhsqq "$STATE" "$RUNTIME"

/usr/bin/python3 - "$SOURCE" "$RUNTIME" "$(id -u yhsqq)" "$(id -g yhsqq)" <<'PY'
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile

source, target = Path(sys.argv[1]), Path(sys.argv[2])
uid, gid = int(sys.argv[3]), int(sys.argv[4])
if any(target.iterdir()):
    print('Existing runtime is nonempty: keeping every existing ledger, quota, and state file unchanged.')
else:
    files = json.loads((source / 'CLOUD_MANIFEST.json').read_text(encoding='utf-8'))['files']
    # Only a complete import becomes the live runtime. If interrupted, a hidden
    # staging directory may remain, while the live runtime stays empty/retryable.
    staging = Path(tempfile.mkdtemp(prefix='.runtime-import-', dir=target.parent))
    if staging.resolve().parent != target.parent.resolve() or staging.is_symlink():
        raise SystemExit('Runtime staging directory is outside the expected state directory.')
    os.chmod(staging, 0o700)
    os.chown(staging, uid, gid)
    copied = 0
    for name in sorted(files):
        if not name.startswith('runtime/'):
            continue
        relative = Path(name).relative_to('runtime')
        destination = staging / relative
        parent = staging
        for component in relative.parts[:-1]:
            parent = parent / component
            if parent.is_symlink():
                raise SystemExit('Refusing a symlink inside runtime.')
            parent.mkdir(mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700)
            os.chown(parent, uid, gid)
        data = (source / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != files[name].lower():
            raise SystemExit('Runtime source changed during import; live state was not replaced.')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), uid, gid)
        copied += 1
    for directory in [path for path in staging.rglob('*') if path.is_dir()] + [staging]:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    # POSIX rename replaces an empty directory atomically and refuses a nonempty
    # destination, so an independently created ledger is never overwritten.
    os.replace(staging, target)
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    print(f'Imported {copied} runtime files on initial installation.')
PY

for name in main.py config.py detector.py mock_replay.py paper_trader.py yhs_client.py alpaca_client.py alpaca_trader.py config.alpaca_paper_100k.json alpaca_policy_100k.json; do
  [[ ! -L "$APP/$name" ]] || fail "Refusing to overwrite an app file symlink: $name."
  [[ ! -e "$APP/$name" || -f "$APP/$name" ]] || fail "The app file path is not a regular file: $name."
  install -m 0644 -o root -g root -- "$SOURCE/$name" "$APP/$name"
done
for name in install.sh runner.py configure_keys.py yhs-qq.service yhs-qq.timer; do
  [[ ! -L "$APP/deploy/linux/$name" ]] || fail "Refusing to overwrite a deployment file symlink: $name."
  [[ ! -e "$APP/deploy/linux/$name" || -f "$APP/deploy/linux/$name" ]] || fail "The deployment file path is not a regular file: $name."
  install -m 0644 -o root -g root -- "$SOURCE/deploy/linux/$name" "$APP/deploy/linux/$name"
done
[[ ! -L "$APP/CLOUD_MANIFEST.json" ]] || fail 'Refusing to replace an app manifest symlink.'
[[ ! -e "$APP/CLOUD_MANIFEST.json" || -f "$APP/CLOUD_MANIFEST.json" ]] || fail 'The app manifest path is not a regular file.'
install -m 0644 -o root -g root -- "$SOURCE/CLOUD_MANIFEST.json" "$APP/CLOUD_MANIFEST.json"
if /usr/bin/python3 - "$SOURCE/CLOUD_MANIFEST.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding='utf-8') as stream:
    raise SystemExit(0 if 'CLOUD_DEPLOY.md' in json.load(stream)['files'] else 1)
PY
then
  [[ ! -L "$APP/CLOUD_DEPLOY.md" ]] || fail 'Refusing to replace a documentation symlink.'
  [[ ! -e "$APP/CLOUD_DEPLOY.md" || -f "$APP/CLOUD_DEPLOY.md" ]] || fail 'The documentation path is not a regular file.'
  install -m 0644 -o root -g root -- "$SOURCE/CLOUD_DEPLOY.md" "$APP/CLOUD_DEPLOY.md"
fi
if [[ ! -L "$APP/runtime" ]]; then
  ln -s -- "$RUNTIME" "$APP/runtime"
fi
for name in yhs-qq.service yhs-qq.timer; do
  [[ ! -L "/etc/systemd/system/$name" ]] || fail "Refusing to replace a systemd unit symlink: $name."
  [[ ! -e "/etc/systemd/system/$name" || -f "/etc/systemd/system/$name" ]] || fail "The systemd unit path is not a regular file: $name."
  install -m 0644 -o root -g root -- "$SOURCE/deploy/linux/$name" "/etc/systemd/system/$name"
done
systemctl daemon-reload
printf '%s\n' 'Installation complete. The service and timer have NOT been enabled or started.'
printf '%s\n' 'Configure keys with: sudo python3 /opt/yhs-qq/app/deploy/linux/configure_keys.py'
printf '%s\n' 'Then perform the read-only check and preview from CLOUD_DEPLOY.md before enabling the timer.'
