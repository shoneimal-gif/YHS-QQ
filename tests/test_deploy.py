"""Linux 部署离线检查：仅临时凭据/账本，不安装服务或调用网络。"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_module("cloud_runner", "deploy/linux/runner.py")
configure = load_module("cloud_configure", "deploy/linux/configure_keys.py")
packager = load_module("cloud_packager", "deploy/package_cloud.py")
API = {"ALPACA_API_KEY": "synthetic-api", "ALPACA_SECRET_KEY": "synthetic-secret", "YHS_API_KEY": "synthetic-yhs"}
WEBHOOK = "https://discord.com/api/webhooks/123456789/synthetic-token"
NORMAL_WEBHOOK = "https://discord.com/api/v10/webhooks/123456789/synthetic-token"


def installer_blocks():
    script = (ROOT / "deploy/linux/install.sh").read_text(encoding="utf-8")
    return re.findall(r"<<'PY'\n(.*?)\nPY", script, re.S)


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yhs-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)


class CloudCredentialCase(TemporaryCase):
    def read(self, values):
        target = self.directory / "api"
        target.write_text(json.dumps(values), encoding="utf-8")
        return runner.read_credentials_file(target)

    def test_existing_three_api_keys_remain_compatible(self):
        self.assertEqual(self.read(API), API)

    def test_optional_discord_uses_official_validator_without_network(self):
        values = self.read({**API, "YHS_DISCORD_WEBHOOK": WEBHOOK})
        self.assertEqual(values["YHS_DISCORD_WEBHOOK"], NORMAL_WEBHOOK)

    def test_missing_unknown_empty_and_control_fields_are_rejected(self):
        for values in ({"ALPACA_API_KEY": "synthetic"}, {**API, "UNKNOWN": "synthetic"},
                       {**API, "YHS_API_KEY": " "}, {**API, "YHS_API_KEY": "test\nkey"},
                       {**API, "YHS_DISCORD_WEBHOOK": "https://example.invalid/webhook"}):
            with self.subTest(fields=sorted(values)), self.assertRaises(ValueError):
                self.read(values)

    def test_duplicate_keys_and_oversize_credentials_are_rejected(self):
        target = self.directory / "api"
        target.write_text('{"YHS_API_KEY":"one","YHS_API_KEY":"two"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            runner.read_credentials_file(target)
        target.write_bytes(b"x" * 65537)
        with self.assertRaises(ValueError):
            runner.read_credentials_file(target)

    def test_credential_symlink_is_not_followed(self):
        target = self.directory / "secret"
        target.write_text(json.dumps(API), encoding="utf-8")
        link = self.directory / "api"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            runner.read_credentials_file(link)

    def test_runner_modes_only_provide_discord_to_explicit_paper_execution(self):
        previous_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), previous_path))
        for mode in ("--check", "--preview", None):
            observed = {}

            def fake_main(arguments):
                observed["arguments"] = arguments
                observed["credentials"] = {name: os.environ.get(name) for name in runner.KEY_NAMES | runner.NOTIFICATION_KEY_NAMES}
                return 37

            inherited = {"YHS_WECOM_WEBHOOK": "synthetic-legacy", "YHS_PUSHPLUS_TOKEN": "synthetic-legacy",
                         "ALPACA_API_KEY": "synthetic-old"}
            with self.subTest(mode=mode), patch.dict(os.environ, inherited, clear=True), \
                    patch.object(runner, "read_credentials", return_value={**API, "YHS_DISCORD_WEBHOOK": NORMAL_WEBHOOK}), \
                    patch.object(runner.os, "chdir"), patch.object(sys, "argv", ["runner.py"] + ([mode] if mode else [])), \
                    patch.dict(sys.modules, {"main": types.SimpleNamespace(main=fake_main)}):
                self.assertEqual(runner.run(), 37)
                self.assertEqual(observed["credentials"]["ALPACA_API_KEY"], API["ALPACA_API_KEY"])
                self.assertIsNone(observed["credentials"]["YHS_WECOM_WEBHOOK"])
                self.assertIsNone(observed["credentials"]["YHS_PUSHPLUS_TOKEN"])
                self.assertEqual(observed["credentials"]["YHS_DISCORD_WEBHOOK"], NORMAL_WEBHOOK if mode is None else None)
                self.assertEqual("--execute-paper" in observed["arguments"], mode is None)
                self.assertEqual(dict(os.environ), inherited)

    def test_runner_restores_environment_when_application_fails(self):
        previous_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), previous_path))
        def fail(arguments):
            raise RuntimeError("synthetic application failure")
        with patch.dict(os.environ, {"YHS_DISCORD_WEBHOOK": "synthetic-old"}, clear=True), \
                patch.object(runner, "read_credentials", return_value=API), patch.object(runner.os, "chdir"), \
                patch.object(sys, "argv", ["runner.py"]), patch.dict(sys.modules, {"main": types.SimpleNamespace(main=fail)}):
            with self.assertRaisesRegex(RuntimeError, "synthetic application"):
                runner.run()
            self.assertEqual(dict(os.environ), {"YHS_DISCORD_WEBHOOK": "synthetic-old"})

    @contextlib.contextmanager
    def configuration(self, answers):
        original_stat = Path.stat
        def root_owned_directory(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == self.directory:
                fields = list(result)
                fields[4] = 0
                return os.stat_result(fields)
            return result
        with patch.object(configure, "DIRECTORY", self.directory), \
                patch.object(configure, "DESTINATION", self.directory / "credentials.json"), \
                patch.object(configure.os, "geteuid", return_value=0), \
                patch.object(sys.stdin, "isatty", return_value=True), patch.object(Path, "stat", root_owned_directory), \
                patch.object(configure.getpass, "getpass", side_effect=answers), \
                patch.dict(sys.modules, {"runner": runner}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            yield

    def test_configuration_saves_hidden_discord_and_private_permissions(self):
        with self.configuration(["new-api", "new-secret", "new-yhs", WEBHOOK]):
            self.assertEqual(configure.configure(["--discord"]), 0)
        target = self.directory / "credentials.json"
        self.assertEqual(json.loads(target.read_text())["YHS_DISCORD_WEBHOOK"], NORMAL_WEBHOOK)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertFalse(list(self.directory.glob(".credentials-*")))

    def test_api_rotation_preserves_discord_and_explicit_removal_preserves_ledger(self):
        target = self.directory / "credentials.json"
        target.write_text(json.dumps({**API, "YHS_DISCORD_WEBHOOK": NORMAL_WEBHOOK}))
        ledger = self.directory / "synthetic_ledger.json"
        ledger.write_bytes(b'{"synthetic": true}')
        with self.configuration(["new-api", "new-secret", "new-yhs"]):
            self.assertEqual(configure.configure([]), 0)
        self.assertEqual(json.loads(target.read_text())["YHS_DISCORD_WEBHOOK"], NORMAL_WEBHOOK)
        with self.configuration(["other-api", "other-secret", "other-yhs"]):
            self.assertEqual(configure.configure(["--remove-discord"]), 0)
        self.assertNotIn("YHS_DISCORD_WEBHOOK", json.loads(target.read_text()))
        self.assertEqual(ledger.read_bytes(), b'{"synthetic": true}')

    def test_invalid_webhook_keeps_previous_file_unchanged(self):
        target = self.directory / "credentials.json"
        original = json.dumps(API).encode()
        target.write_bytes(original)
        with self.configuration(["new-api", "new-secret", "new-yhs", "https://example.invalid"]):
            self.assertEqual(configure.configure(["--discord"]), 1)
        self.assertEqual(target.read_bytes(), original)

    def test_interrupted_atomic_save_keeps_previous_file_and_removes_temporary(self):
        target = self.directory / "credentials.json"
        original = json.dumps(API).encode()
        target.write_bytes(original)
        with self.configuration(["new-api", "new-secret", "new-yhs"]), \
                patch.object(configure.os, "replace", side_effect=OSError("synthetic interrupted rename")):
            with self.assertRaises(OSError):
                configure.configure([])
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".credentials-*")))


class DeploymentArchiveCase(TemporaryCase):
    def make_bundle(self):
        source = self.directory / "source"
        source.mkdir()
        paths = [*packager.SOURCE_FILES, *("deploy/linux/" + name for name in packager.LINUX_FILES),
                 "CLOUD_DEPLOY.md", "DISCORD_NOTIFICATIONS.md", "runtime/alpaca_accounts/synthetic-account/alpaca_paper_state.json",
                 "runtime/alpaca_accounts/synthetic-account/discord_outbox.json"]
        files = {}
        for name in paths:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'{"synthetic": true}\n')
            files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = {"contains_credentials": False, "files": files}
        (source / "CLOUD_MANIFEST.json").write_text(json.dumps(manifest))
        return source, manifest

    def execute_block(self, number, arguments):
        with patch.object(sys, "argv", ["offline-installer-block", *map(str, arguments)]), \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(installer_blocks()[number], "install.sh:offline-block", "exec"), {})

    def test_manifest_verifier_accepts_complete_synthetic_bundle(self):
        source, _ = self.make_bundle()
        self.execute_block(0, [source])

    def test_manifest_verifier_rejects_tampering_and_symlinks(self):
        source, _ = self.make_bundle()
        target = source / "main.py"
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(SystemExit, "integrity"):
            self.execute_block(0, [source])
        target.unlink()
        target.symlink_to(source / "config.py")
        with self.assertRaisesRegex(SystemExit, "Symlinks"):
            self.execute_block(0, [source])

    def test_manifest_verifier_rejects_escape_and_private_credentials(self):
        source, manifest = self.make_bundle()
        for name in ("../escape.json", "runtime/.private_config/discord.json", "runtime/credentials.json"):
            with self.subTest(path=name):
                changed = {**manifest, "files": {**manifest["files"], name: "0" * 64}}
                (source / "CLOUD_MANIFEST.json").write_text(json.dumps(changed))
                with self.assertRaises(SystemExit):
                    self.execute_block(0, [source])

    def test_initial_runtime_import_preserves_ledger_and_outbox(self):
        source, _ = self.make_bundle()
        target = self.directory / "state/runtime"
        target.mkdir(parents=True)
        self.execute_block(1, [source, target, os.getuid(), os.getgid()])
        self.assertEqual((target / "alpaca_accounts/synthetic-account/alpaca_paper_state.json").read_bytes(), b'{"synthetic": true}\n')
        self.assertEqual((target / "alpaca_accounts/synthetic-account/discord_outbox.json").read_bytes(), b'{"synthetic": true}\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertFalse(list(target.parent.glob(".runtime-import-*")))

    def test_existing_runtime_is_kept_byte_for_byte(self):
        source, _ = self.make_bundle()
        target = self.directory / "state/runtime"
        target.mkdir(parents=True)
        ledger = target / "existing_synthetic_ledger.json"
        ledger.write_bytes(b'{"cash": 98765, "synthetic": true}')
        self.execute_block(1, [source, target, os.getuid(), os.getgid()])
        self.assertEqual(list(target.iterdir()), [ledger])
        self.assertEqual(ledger.read_bytes(), b'{"cash": 98765, "synthetic": true}')

    def test_interrupted_import_never_publishes_partial_runtime(self):
        source, _ = self.make_bundle()
        target = self.directory / "state/runtime"
        target.mkdir(parents=True)
        with patch("os.replace", side_effect=OSError("synthetic import interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.execute_block(1, [source, target, os.getuid(), os.getgid()])
        self.assertEqual(list(target.iterdir()), [])
        self.execute_block(1, [source, target, os.getuid(), os.getgid()])
        self.assertTrue((target / "alpaca_accounts/synthetic-account/alpaca_paper_state.json").is_file())

    def test_source_changed_during_import_does_not_publish_live_state(self):
        source, _ = self.make_bundle()
        target = self.directory / "state/runtime"
        target.mkdir(parents=True)
        (source / "runtime/alpaca_accounts/synthetic-account/alpaca_paper_state.json").write_bytes(b"changed")
        with self.assertRaisesRegex(SystemExit, "changed during import"):
            self.execute_block(1, [source, target, os.getuid(), os.getgid()])
        self.assertEqual(list(target.iterdir()), [])

    def test_package_preserves_synthetic_runtime_and_excludes_private_configuration(self):
        source, _ = self.make_bundle()
        private = source / ".private_config/discord.json"
        private.parent.mkdir()
        private.write_text('{"webhook_url":"synthetic-not-a-real-webhook"}')
        archive_path = self.directory / "offline-bundle.tar.gz"
        with patch.object(packager, "ROOT", source), patch.object(sys, "argv", ["package_cloud.py", "--output", str(archive_path)]), \
                contextlib.redirect_stdout(io.StringIO()):
            packager.main()
        with tarfile.open(archive_path) as archive:
            names = archive.getnames()
            self.assertIn("DISCORD_NOTIFICATIONS.md", names)
            self.assertIn("runtime/alpaca_accounts/synthetic-account/alpaca_paper_state.json", names)
            self.assertIn("runtime/alpaca_accounts/synthetic-account/discord_outbox.json", names)
            self.assertNotIn(".private_config/discord.json", names)
            self.assertFalse(any(name.endswith(".lock") for name in names))
            manifest = json.load(archive.extractfile("CLOUD_MANIFEST.json"))
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.extractfile(name).read()).hexdigest(), digest)

    def test_packager_refuses_private_credentials_in_runtime(self):
        source, _ = self.make_bundle()
        credential = source / "runtime/.private_config/discord.json"
        credential.parent.mkdir()
        credential.write_bytes(b"synthetic-private")
        with patch.object(packager, "ROOT", source), self.assertRaisesRegex(RuntimeError, "密钥文件"):
            packager.runtime_files()


if __name__ == "__main__":
    unittest.main()
