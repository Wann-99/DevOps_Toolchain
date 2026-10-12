"""Offline upgrade checks: no GitHub publishing, Docker commands or production writes."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request
import zipfile

from ksq import upgrade


REQUIREMENTS = hashlib.sha256(b"fixture requirements").hexdigest()


def package(path, version, extra=None):
    manifest = {"format": 1, "version": version, "upgrade_protocol": 1, "preserve_state": True,
                "runtime_image": upgrade.RUNTIME_IMAGE, "requirements_sha256": REQUIREMENTS}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("KSQ_BUILD.json", json.dumps(manifest))
        for name in ("__main__.py", "ksq/cli.py", "ksq/web/templates/shell.html", "ksq/web/static/app.css", "ksq/upgrade.py"):
            archive.writestr(name, "offline fixture")
        if extra:
            archive.writestr(extra, "unsafe fixture")
    return {**manifest, "filename": path.name, "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "asset_url": "https://api.github.com/repos/" + upgrade.REPOSITORY + "/releases/assets/2"}


class UpgradeTransactions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deployment = self.root / "deployment"
        self.directory = self.deployment / "upgrade"
        self.directory.mkdir(parents=True)
        (self.deployment / "bin").mkdir()
        (self.deployment / "config").mkdir()
        (self.deployment / "data").mkdir()
        self.app = self.deployment / "bin/knowledge_shelf_query.bin"
        package(self.app, "v1.0.0")
        self.old_app = self.app.read_bytes()
        for name in upgrade.CONFIG_FILES:
            (self.deployment / "config" / name).write_bytes(b'{"configured": "keep me"}\n')
        self.original = {name: (self.deployment / "config" / name).read_bytes() for name in upgrade.CONFIG_FILES}
        (self.deployment / "data/records.json").write_bytes(b"business data remains untouched")
        self.new = self.root / "knowledge_shelf_query_v2.0.0.bin"
        self.release = package(self.new, "v2.0.0")
        self.manager = upgrade.Updater(self.directory, self.deployment, "fixture-user")
        self.manager.release = self.release
        self.commands = []

    def run_update(self, failure=None, ready=True):
        def command(*args):
            self.commands.append(args)
            if "update" in args:
                shutil.copyfile(args[-1], self.app)
                if failure:
                    (self.deployment / "config/order_config.json").write_text('{"changed": true}')
                if failure == "install":
                    raise subprocess.CalledProcessError(1, args)
            if "restart" in args and failure == "recover":
                raise subprocess.CalledProcessError(1, args)

        def health(version):
            if failure in {"health", "recover"} and version == "v2.0.0":
                raise ValueError("new application not healthy")
            self.assertEqual(upgrade.archive_manifest(self.app)["version"], version)

        with (
            patch.object(self.manager, "fetch_release", return_value=self.release),
            patch.object(upgrade, "github_open", side_effect=lambda *args, **kwargs: io.BytesIO(self.new.read_bytes())),
            patch.object(upgrade.subprocess, "check_output", side_effect=lambda args, **kwargs: upgrade.RUNTIME_IMAGE if "inspect" in args else REQUIREMENTS),
            patch.object(self.manager, "stop_application"),
            patch.object(self.manager, "command", side_effect=command),
            patch.object(self.manager, "verify_running", side_effect=health),
            patch.object(self.manager, "ready", return_value={"ready": ready, "reason": "订单尚未结束"}),
        ):
            self.manager.lock.acquire()
            self.manager.save("downloading", "offline check")
            self.manager.run("v2.0.0", "fixture session never persisted")

    def assert_preserved(self):
        self.assertEqual({name: (self.deployment / "config" / name).read_bytes() for name in upgrade.CONFIG_FILES}, self.original)
        self.assertEqual((self.deployment / "data/records.json").read_bytes(), b"business data remains untouched")
        self.assertFalse((self.directory / "application.incoming").exists())
        self.assertNotIn("fixture session", (self.directory / "state.json").read_text())

    def test_success_preserves_settings_accounts_task_states_and_data(self):
        self.run_update()
        self.assertEqual(upgrade.archive_manifest(self.app)["version"], "v2.0.0")
        self.assertEqual(self.manager.state["phase"], "succeeded")
        self.assertFalse(self.manager.state["maintenance"])
        self.assert_preserved()
        self.assertEqual((self.directory / "backup/application.bin").read_bytes(), self.old_app)

    def test_install_or_health_failure_restores_exact_previous_version_and_config(self):
        for failure in ("install", "health"):
            with self.subTest(failure=failure):
                self.app.write_bytes(self.old_app)
                self.run_update(failure)
                self.assertEqual(self.app.read_bytes(), self.old_app)
                self.assertEqual(self.manager.state["recovery"], "restored")
                self.assertFalse(self.manager.state["maintenance"])
                self.assert_preserved()

    def test_failed_recovery_stays_locked_and_reports_failure(self):
        self.run_update("recover")
        self.assertEqual(self.manager.state["recovery"], "failed")
        self.assertTrue(self.manager.state["maintenance"])
        with self.assertRaises(ValueError):
            self.manager.check()
        with self.assertRaises(ValueError):
            self.manager.start("v2.0.0", "fixture cookie")

    def test_unfinished_order_or_bad_checksum_never_stops_or_replaces_old_version(self):
        self.run_update(ready=False)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.app.read_bytes(), self.old_app)
        self.assertFalse((self.directory / "backup").exists())
        self.assert_preserved()
        self.release["sha256"] = "0" * 64
        self.run_update()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.app.read_bytes(), self.old_app)
        self.assert_preserved()

    def test_service_restart_recovers_interrupted_install(self):
        self.manager.backup()
        self.app.write_bytes(self.new.read_bytes())
        (self.deployment / "config/users.json").write_text('{"unexpected": true}')
        self.manager.save("installing", "power lost")
        recovered = upgrade.Updater(self.directory, self.deployment, "fixture-user")
        with patch.object(recovered, "stop_application"), patch.object(recovered, "command"), patch.object(recovered, "verify_running"):
            recovered.resume()
        self.assertEqual(self.app.read_bytes(), self.old_app)
        self.assertEqual(recovered.state["recovery"], "restored")
        self.assert_preserved()

    def test_restart_before_replacement_restarts_original_without_using_an_old_checkpoint(self):
        for state in ({"phase": "preparing", "busy": True}, {"phase": "failed", "restart_original": True}):
            with self.subTest(state=state):
                upgrade.write_json(self.directory / "state.json", state)
                recovered = upgrade.Updater(self.directory, self.deployment, "fixture-user")
                with patch.object(recovered, "recover") as rollback, patch.object(recovered, "command") as restart, \
                        patch.object(recovered, "verify_running") as health:
                    recovered.resume()
                rollback.assert_not_called()
                restart.assert_called_once_with("bash", str(self.deployment / "start.sh"), "restart")
                health.assert_called_once_with("v1.0.0")
                self.assertFalse(recovered.state["maintenance"])
                self.assertFalse(recovered.state["restart_original"])
                self.assert_preserved()

    def test_upgrade_log_keeps_one_history_and_passes_original_host_account(self):
        log = self.directory / "upgrade.log"
        log.write_bytes(b"x" * (5 * 1024 * 1024))
        with patch.object(upgrade.subprocess, "run") as execute:
            self.manager.command("bash", "fixture-start", "restart")
        self.assertEqual((self.directory / "upgrade.log.1").stat().st_size, 5 * 1024 * 1024)
        self.assertEqual(log.stat().st_size, 0)
        self.assertEqual(execute.call_args.kwargs["env"]["KSQ_HOST_FILES_USER"], "fixture-user")
        self.assertEqual(execute.call_args.kwargs["env"]["KSQ_ONLINE_UPGRADE"], "1")

    def test_duplicate_upgrade_is_rejected_and_logs_do_not_contain_session(self):
        self.manager.lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "已有升级"):
                self.manager.start("v2.0.0", "private fixture session")
        finally:
            self.manager.lock.release()

    def test_release_assets_must_belong_to_configured_repository(self):
        prefix = "https://api.github.com/repos/" + upgrade.REPOSITORY + "/releases/assets/"
        published = {"tag_name": "v2.0.0", "body": "release notes", "assets": [
            {"name": "ksq_release.json", "id": 1, "url": prefix + "1", "state": "uploaded"},
            {"name": self.new.name, "id": 2, "url": prefix + "2", "state": "uploaded", "size": self.release["size"]},
        ]}
        with patch.object(upgrade, "github_json", side_effect=[published, self.release.copy()]):
            self.assertEqual(self.manager.fetch_release()["notes"], "release notes")
        published["assets"][0]["url"] = "https://other.invalid/payload"
        with patch.object(upgrade, "github_json", return_value=published), self.assertRaises(ValueError):
            self.manager.fetch_release()

    def test_archive_traversal_and_dependency_mismatch_are_rejected(self):
        release = package(self.new, "v2.0.0", "../escape.py")
        with self.assertRaisesRegex(ValueError, "不安全"):
            upgrade.validate_package(self.new, release)
        release = package(self.new, "v2.0.0")
        release["requirements_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "不匹配"):
            upgrade.validate_package(self.new, release)

    def test_redirects_are_https_only_and_never_forward_github_token(self):
        request = Request("https://api.github.com/repos/fixture/releases/assets/1", headers={"Authorization": "Bearer fixture-secret"})
        redirected = upgrade.GithubRedirect().redirect_request(request, None, 302, "found", {}, "https://release-assets.githubusercontent.com/fixture")
        self.assertIsNone(redirected.get_header("Authorization"))
        for url in ("http://github.com/file", "https://github.com@other.invalid/file", "https://127.0.0.1/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                upgrade.GithubRedirect().redirect_request(request, None, 302, "found", {}, url)


if __name__ == "__main__":
    unittest.main()
