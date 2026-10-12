"""Exercise local update and archive-opening routes with no external side effects."""

from contextlib import contextmanager
import fcntl
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import Application, Handler, OperationBusy, write_json
from vault.updater import UpdateError


class UpdateRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shiliao-update-api-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.source = self.root / "synthetic-source"
        self.source.mkdir()
        self.config = {"source": str(self.source), "destination": str(self.root / "archives"),
                       "schedule_enabled": False, "schedule_time": "22:00", "interval": "daily",
                       "include_media": True, "timezone": "Asia/Shanghai"}
        write_json(self.state / "config.json", self.config)
        self.update_state = {"supported": True, "current_version": "0.7.0", "status": "idle",
                             "latest_version": None, "download_kind": None, "download_bytes": 0,
                             "downloaded_bytes": 0, "full_bytes": 0, "error": None}
        self.updater = Mock()
        self.updater.status.side_effect = lambda: dict(self.update_state)
        self.updater.check.side_effect = lambda: self.update_state.update(status="available", latest_version="0.8.0")
        self.updater.download.side_effect = lambda: self.update_state.update(status="ready", download_kind="delta")
        self.updater.launch_install.side_effect = lambda _pid: self.update_state.update(status="installing")
        self.updater_class = self.patched("vault.updater.Updater", return_value=self.updater)
        self.scheduler = self.patched("app.Scheduler", autospec=True).return_value
        self.scheduler.status.return_value = {"installed": False}
        self.discover = self.patched("app.discover_sources", return_value=[])
        self.patched("app.wechat_running", return_value=False)
        self.patched("app.key_status", return_value={"configured": False})
        self.fetch = self.patched("vault.updater.fetch", side_effect=AssertionError("External fetch is forbidden"))
        self.process = self.patched("vault.updater.subprocess.Popen", side_effect=AssertionError("Installer launch is forbidden"))
        self.application = Application(self.state)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.application
        self.server_thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)

    def patched(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def close_server(self):
        self.application.update_stop.set()
        self.wait_updates()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.fetch.assert_not_called()
        self.process.assert_not_called()

    def request(self, method, path, data=None, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + self.application.token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, json.dumps(data).encode() if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_updates(self):
        deadline = time.monotonic() + 3
        while self.application.update_lock.locked() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(self.application.update_lock.locked(), "Synthetic update worker did not stop")

    def wait_job(self, expected="done"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.application.job_lock:
                job = dict(self.application.job or {})
            if job.get("status") != "running":
                self.assertEqual(job.get("status"), expected, job)
                return job
            time.sleep(.005)
        self.fail("Synthetic archive worker did not stop")

    @contextmanager
    def occupied_operation(self):
        with (self.state / "operation.lock").open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def archived_library(self, cache_hit=False):
        return SimpleNamespace(stats=lambda: {"conversations": 2, "messages": 5}, raw_cache_hit=cache_hit)

    def test_constructor_does_not_check_download_install_or_start_monitor(self):
        write_json(self.root / "second-state/config.json", self.config)
        with patch("app.threading.Thread") as thread:
            application = Application(self.root / "second-state")
        self.assertFalse(application.update_stop.is_set())
        thread.assert_not_called()
        self.updater.check.assert_not_called()
        self.updater.download.assert_not_called()
        self.updater.launch_install.assert_not_called()
        self.discover.assert_not_called()
        self.scheduler.install.assert_not_called()
        self.scheduler.uninstall.assert_not_called()

    def test_status_is_authenticated_and_defaults_to_enabled_without_triggering_updates(self):
        self.assertEqual(self.request("GET", "/api/update/status", authorized=False)[0], 401)
        status, result = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["current_version"], "0.7.0")
        status, overall = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(overall["updates"], result)
        self.updater.check.assert_not_called()
        self.updater.download.assert_not_called()

    def test_status_preserves_update_progress_and_unsupported_reason(self):
        self.update_state.update(status="downloading", download_kind="delta", downloaded_bytes=10,
                                 download_bytes=40, full_bytes=400)
        _, result = self.request("GET", "/api/update/status")
        for field in ("status", "download_kind", "downloaded_bytes", "download_bytes", "full_bytes"):
            self.assertEqual(result[field], self.update_state[field])
        self.update_state.update(supported=False, reason="Synthetic unsupported platform")
        _, result = self.request("GET", "/api/update/status")
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "Synthetic unsupported platform")

    def test_update_mutations_and_snapshot_open_require_authentication(self):
        with patch.object(self.application, "start_job") as start_job:
            for path, data in (("/api/update/check", {}), ("/api/update/settings", {"enabled": False}),
                               ("/api/update/install", {}), ("/api/snapshots/synthetic/open", {})):
                with self.subTest(path=path):
                    self.assertEqual(self.request("POST", path, data, authorized=False)[0], 401)
        self.assertEqual(self.application.config(), self.config)
        self.updater.check.assert_not_called()
        self.updater.launch_install.assert_not_called()
        start_job.assert_not_called()

    def test_settings_are_immediate_preserve_backup_configuration_and_check_when_enabled(self):
        with patch.object(self.application, "check_updates") as check:
            status, result = self.request("POST", "/api/update/settings", {"enabled": False})
            self.assertEqual(status, 200)
            self.assertFalse(result["enabled"])
            check.assert_not_called()
            status, result = self.request("POST", "/api/update/settings", {"enabled": True})
            self.assertEqual(status, 200)
            self.assertTrue(result["enabled"])
            check.assert_called_once_with()
        self.assertEqual(self.application.config(), {**self.config, "auto_update_enabled": True})
        self.scheduler.install.assert_not_called()
        self.scheduler.uninstall.assert_not_called()

    def test_settings_reject_non_boolean_values_without_changing_configuration(self):
        before = self.application.config_path.read_bytes()
        with patch.object(self.application, "check_updates") as check:
            for value in (None, 0, 1, "true", "false", [], {}):
                with self.subTest(value=value):
                    status, result = self.request("POST", "/api/update/settings", {"enabled": value})
                    self.assertEqual(status, 400)
                    self.assertIn("error", result)
            self.assertEqual(self.request("POST", "/api/update/settings", {})[0], 400)
            check.assert_not_called()
        self.assertEqual(self.application.config_path.read_bytes(), before)

    def test_check_is_asynchronous_deduplicates_busy_requests_and_downloads_once(self):
        entered, release = threading.Event(), threading.Event()

        def check():
            self.update_state["status"] = "checking"
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic check was not released")
            self.update_state["status"] = "available"

        self.updater.check.side_effect = check
        try:
            status, _ = self.request("POST", "/api/update/check", {})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request("POST", "/api/update/check", {})[0], 202)
            self.updater.check.assert_called_once_with()
            self.updater.download.assert_not_called()
            self.assertEqual(self.request("GET", "/api/update/status")[1]["status"], "checking")
        finally:
            release.set()
            self.wait_updates()
        self.updater.download.assert_called_once_with()
        self.assertEqual(self.application.update_status()["status"], "ready")
        self.updater.launch_install.assert_not_called()

    def test_check_skips_unsupported_ready_and_installing_states(self):
        for supported, phase in ((False, "idle"), (True, "ready"), (True, "installing")):
            with self.subTest(supported=supported, phase=phase):
                self.update_state.update(supported=supported, status=phase)
                self.assertEqual(self.request("POST", "/api/update/check", {})[0], 202)
                self.wait_updates()
        self.updater.check.assert_not_called()
        self.updater.download.assert_not_called()

    def test_current_version_check_does_not_download(self):
        self.updater.check.side_effect = lambda: self.update_state.update(status="idle")
        self.request("POST", "/api/update/check", {})
        self.wait_updates()
        self.updater.check.assert_called_once_with()
        self.updater.download.assert_not_called()

    def test_check_respects_cross_process_operation_lock_and_can_retry(self):
        with self.occupied_operation():
            self.assertEqual(self.request("POST", "/api/update/check", {})[0], 202)
            self.wait_updates()
            self.updater.check.assert_not_called()
        self.request("POST", "/api/update/check", {})
        self.wait_updates()
        self.updater.check.assert_called_once_with()
        self.updater.download.assert_called_once_with()

    def test_failed_check_releases_worker_lock_and_keeps_retry_available(self):
        def fail():
            self.update_state.update(status="error", error="update_failed")
            raise UpdateError("update_failed")
        self.updater.check.side_effect = fail
        self.request("POST", "/api/update/check", {})
        self.wait_updates()
        self.assertEqual(self.request("GET", "/api/update/status")[1]["error"], "update_failed")
        self.updater.download.assert_not_called()
        self.updater.check.side_effect = lambda: self.update_state.update(status="available", error=None)
        self.request("POST", "/api/update/check", {})
        self.wait_updates()
        self.assertEqual(self.updater.check.call_count, 2)
        self.assertEqual(self.application.update_status()["status"], "ready")

    def test_failed_download_does_not_install_or_leave_worker_busy(self):
        def fail():
            self.update_state.update(status="error", error="package_hash_mismatch")
            raise UpdateError("package_hash_mismatch")
        self.updater.download.side_effect = fail
        self.request("POST", "/api/update/check", {})
        self.wait_updates()
        self.assertEqual(self.application.update_status()["error"], "package_hash_mismatch")
        self.updater.launch_install.assert_not_called()

    def test_background_monitor_obeys_enabled_setting_and_twelve_hour_interval(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                write_json(self.application.config_path, {**self.config, "auto_update_enabled": enabled})
                stop = Mock()
                stop.is_set.side_effect = [False, True]
                self.application.update_stop = stop
                with patch.object(self.application, "check_updates") as check, patch("app.threading.Thread") as thread:
                    self.application.start_updates()
                    thread.call_args.kwargs["target"]()
                    self.assertEqual(check.call_count, int(enabled))
                    stop.wait.assert_called_once_with(12 * 60 * 60)
        self.application.update_stop = threading.Event()

    def test_install_rejects_active_jobs_before_calling_installer(self):
        self.update_state["status"] = "ready"
        self.application.job = {"id": "synthetic", "status": "running", "type": "backup", "progress": {}}
        status, result = self.request("POST", "/api/update/install", {})
        self.assertEqual(status, 400)
        self.assertEqual(result["code"], "operation_busy")
        self.updater.launch_install.assert_not_called()
        self.assertFalse(self.application.installing_update)
        self.assertFalse(self.application.update_stop.is_set())

    def test_failed_install_keeps_server_and_jobs_available(self):
        self.updater.launch_install.side_effect = UpdateError("staged_bundle_changed")
        status, result = self.request("POST", "/api/update/install", {})
        self.assertEqual(status, 400)
        self.assertEqual(result["code"], "staged_bundle_changed")
        self.assertFalse(self.application.installing_update)
        self.assertFalse(self.application.update_stop.is_set())
        self.assertEqual(self.request("GET", "/api/update/status")[0], 200)
        self.assertTrue(self.server_thread.is_alive())

    def test_ready_install_marks_lifecycle_stops_monitor_and_shuts_down_http_after_response(self):
        self.update_state["status"] = "ready"
        status, result = self.request("POST", "/api/update/install", {})
        self.assertEqual((status, result), (202, {"ok": True, "status": "installing"}))
        self.updater.launch_install.assert_called_once_with(os.getpid())
        self.assertTrue(self.application.installing_update)
        self.assertTrue(self.application.update_stop.is_set())
        with self.assertRaises(OperationBusy):
            self.application.install_update()
        with self.assertRaises(OperationBusy):
            self.application.start_job("reader_open", archive_path=str(self.root / "synthetic.zip"))
        self.server_thread.join(timeout=2)
        self.assertFalse(self.server_thread.is_alive())

    def test_snapshot_open_uses_registered_snapshot_path_and_reader_job(self):
        selected = str(self.root / "registered-raw.zip")
        with patch.object(self.application, "snapshots", return_value=[{"id": "synthetic", "path": selected}]), \
                patch.object(self.application, "start_job", return_value={"job_id": "synthetic-job"}) as start:
            status, result = self.request("POST", "/api/snapshots/synthetic/open", {"path": "/ignored.zip"})
        self.assertEqual((status, result), (202, {"job_id": "synthetic-job"}))
        start.assert_called_once_with("reader_open", archive_path=selected)

    def test_unknown_snapshot_does_not_start_reader_job(self):
        with patch.object(self.application, "snapshots", return_value=[]), patch.object(self.application, "start_job") as start:
            self.assertEqual(self.request("POST", "/api/snapshots/missing/open", {})[0], 404)
        start.assert_not_called()

    def test_picker_archive_route_validates_path_and_delegates_reader_job(self):
        selected = str(self.root / "raw-or-readable.zip")
        with patch.object(self.application, "start_job", return_value={"job_id": "synthetic-job"}) as start:
            for value in (None, False, 9, {}, "relative.zip"):
                with self.subTest(value=value):
                    self.assertEqual(self.request("POST", "/api/reader/archive", {"path": value})[0], 400)
            start.assert_not_called()
            self.assertEqual(self.request("POST", "/api/reader/archive", {"path": selected})[0], 202)
        start.assert_called_once_with("reader_open", archive_path=selected)

    def test_raw_reader_passes_expected_signature_and_keeps_local_library_isolated(self):
        selected = self.root / "synthetic-raw.zip"
        opened, progress = self.archived_library(cache_hit=True), Mock()
        previous_generation = self.application.reader_generation
        local_stats = self.application.library.stats()
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1, 2, 3]}) as inspect, \
                patch("vault.raw_archive.open_raw_archive", return_value=opened) as raw, \
                patch("vault.readable_archive.open_archive") as readable:
            result = self.application.open_reader_archive(selected, progress)
        inspect.assert_called_once_with(selected)
        raw.assert_called_once_with(selected, self.state, progress, expected_signature=[1, 2, 3])
        readable.assert_not_called()
        self.assertIs(self.application.reader_library, opened)
        self.assertEqual(self.application.library.stats(), local_stats)
        self.assertNotEqual(self.application.reader_generation, previous_generation)
        self.assertEqual(self.application.reader_source, {"archived": True, "name": selected.name, "format": "raw"})
        self.assertTrue(result["cache_hit"])
        self.assertFalse(result["linked_readable"])

    def test_readable_reader_does_not_invoke_raw_conversion(self):
        selected = self.root / "synthetic-readable.zip"
        opened = self.archived_library()
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "readable"}), \
                patch("vault.raw_archive.open_raw_archive") as raw, \
                patch("vault.readable_archive.open_archive", return_value=opened) as readable:
            result = self.application.open_reader_archive(selected)
        readable.assert_called_once_with(selected, self.state)
        raw.assert_not_called()
        self.assertEqual(result["format"], "readable")

    def test_raw_reader_prefers_a_valid_linked_readable_archive(self):
        selected, linked = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch.object(self.application, "linked_readable", return_value=linked), \
                patch("vault.raw_archive.open_raw_archive") as raw, \
                patch("vault.readable_archive.open_archive", return_value=self.archived_library()) as readable:
            result = self.application.open_reader_archive(selected)
        readable.assert_called_once_with(linked, self.state)
        raw.assert_not_called()
        self.assertTrue(result["linked_readable"])

    def test_same_backup_link_uses_both_file_signatures_and_is_exposed_in_snapshots(self):
        selected, linked = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
        selected.write_bytes(b"Synthetic raw archive")
        linked.write_bytes(b"Synthetic readable archive")
        self.application.link_readable(selected, linked)
        self.assertEqual(self.application.linked_readable(selected), linked)
        with patch.object(self.application.engine, "list_snapshots", return_value=[{"id": "synthetic", "path": str(selected)}]):
            item = self.application.snapshots()[0]
        self.assertEqual(item["readable_archive"], {"path": str(linked), "name": linked.name, "available": True})
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch("vault.raw_archive.open_raw_archive") as raw, \
                patch("vault.readable_archive.open_archive", return_value=self.archived_library()) as readable:
            result = self.application.open_reader_archive(selected)
        readable.assert_called_once_with(linked, self.state)
        raw.assert_not_called()
        self.assertTrue(result["linked_readable"])

    def test_changing_either_linked_file_invalidates_association_and_uses_raw_conversion(self):
        for changed_name in ("raw", "readable"):
            with self.subTest(changed=changed_name):
                selected, linked = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
                selected.write_bytes(b"Synthetic raw archive")
                linked.write_bytes(b"Synthetic readable archive")
                self.application.link_readable(selected, linked)
                changed = selected if changed_name == "raw" else linked
                changed.write_bytes(b"Changed synthetic archive with a different size")
                self.assertIsNone(self.application.linked_readable(selected))
                current_signature = self.application.archive_signature(selected)
                with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": current_signature}), \
                        patch("vault.raw_archive.open_raw_archive", return_value=self.archived_library()) as raw, \
                        patch("vault.readable_archive.open_archive") as readable:
                    result = self.application.open_reader_archive(selected)
                raw.assert_called_once_with(selected, self.state, None, expected_signature=current_signature)
                readable.assert_not_called()
                self.assertFalse(result["linked_readable"])

    def test_missing_linked_readable_falls_back_to_original_backup(self):
        selected, linked = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
        selected.write_bytes(b"Synthetic raw archive")
        linked.write_bytes(b"Synthetic readable archive")
        self.application.link_readable(selected, linked)
        linked.unlink()
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch("vault.raw_archive.open_raw_archive", return_value=self.archived_library()) as raw, \
                patch("vault.readable_archive.open_archive") as readable:
            self.application.open_reader_archive(selected)
        raw.assert_called_once()
        readable.assert_not_called()

    def test_unregistered_readable_archive_is_never_guessed_from_name_or_mtime(self):
        selected, older = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
        selected.write_bytes(b"Synthetic raw archive")
        older.write_bytes(b"Synthetic older readable archive")
        shared_time = 1700000000
        os.utime(selected, (shared_time, shared_time))
        os.utime(older, (shared_time, shared_time))
        self.assertIsNone(self.application.linked_readable(selected))
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch("vault.raw_archive.open_raw_archive", return_value=self.archived_library()) as raw, \
                patch("vault.readable_archive.open_archive") as readable:
            self.application.open_reader_archive(selected)
        raw.assert_called_once()
        readable.assert_not_called()

    def test_association_rejects_a_linked_file_replaced_by_a_symlink(self):
        for changed_name in ("raw", "readable"):
            with self.subTest(changed=changed_name):
                selected, linked = self.root / "synthetic-raw.zip", self.root / "synthetic-readable.zip"
                selected.unlink(missing_ok=True)
                linked.unlink(missing_ok=True)
                selected.write_bytes(b"Synthetic raw archive")
                linked.write_bytes(b"Synthetic readable archive")
                self.application.link_readable(selected, linked)
                changed = selected if changed_name == "raw" else linked
                target = self.root / f"synthetic-{changed_name}-target.zip"
                changed.rename(target)
                changed.symlink_to(target)
                self.assertIsNone(self.application.linked_readable(selected))

    def test_failed_raw_conversion_keeps_previous_reader_and_generation(self):
        old = self.archived_library()
        self.application.archive_library = old
        previous_source = dict(self.application.reader_source)
        previous_generation = self.application.reader_generation
        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch("vault.raw_archive.open_raw_archive", side_effect=ValueError("Synthetic missing key")):
            with self.assertRaisesRegex(ValueError, "Synthetic missing key"):
                self.application.open_reader_archive(self.root / "synthetic-raw.zip")
        self.assertIs(self.application.reader_library, old)
        self.assertEqual(self.application.reader_source, previous_source)
        self.assertEqual(self.application.reader_generation, previous_generation)

    def test_raw_reader_operation_lock_precedes_inspection_or_conversion(self):
        with patch("vault.raw_archive.inspect_archive") as inspect, patch("vault.raw_archive.open_raw_archive") as raw:
            with self.occupied_operation(), self.assertRaises(OperationBusy):
                self.application.open_reader_archive(self.root / "synthetic-raw.zip")
        inspect.assert_not_called()
        raw.assert_not_called()
        self.assertIsNone(self.application.archive_library)

    def test_raw_reader_job_reports_progress_and_switches_only_when_done(self):
        entered, release = threading.Event(), threading.Event()
        opened = self.archived_library()
        selected = str(self.root / "synthetic-raw.zip")

        def convert(_path, _state, progress, **_kwargs):
            progress({"phase": "raw_import", "done_messages": 5, "current_file": "Synthetic conversion"})
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic conversion was not released")
            return opened

        with patch("vault.raw_archive.inspect_archive", return_value={"format": "raw", "signature": [1]}), \
                patch("vault.raw_archive.open_raw_archive", side_effect=convert):
            try:
                status, result = self.request("POST", "/api/reader/archive", {"path": selected})
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.application.job["id"], result["job_id"])
                self.assertEqual(self.application.job["progress"]["phase"], "raw_import")
                self.assertIs(self.application.reader_library, self.application.library)
            finally:
                release.set()
            job = self.wait_job()
        self.assertEqual(job["type"], "reader_open")
        self.assertEqual(job["result"]["messages"], 5)
        self.assertIs(self.application.reader_library, opened)


if __name__ == "__main__":
    unittest.main()
