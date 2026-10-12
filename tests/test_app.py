"""Test scheduling boundaries and real loopback HTTP authorization."""

from datetime import datetime
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import Application, Handler, OperationBusy, TIMEZONE, schedule_due, write_json


class ScheduleDueTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "source": "/example/source", "destination": "/example/destination",
            "schedule_enabled": True, "interval": "daily", "schedule_time": "03:00",
        }

    def moment(self, hour, minute=0, second=0):
        return datetime(2026, 10, 12, hour, minute, second, tzinfo=TIMEZONE)

    def success(self, finished_at):
        return {"source": self.config["source"], "destination": self.config["destination"], "finished_at": finished_at}

    def test_disabled_schedule_never_runs(self):
        self.assertFalse(schedule_due({"schedule_enabled": False}, None, self.moment(12)))

    def test_daily_starts_at_the_selected_minute(self):
        self.assertFalse(schedule_due(self.config, None, self.moment(2, 59, 59)))
        self.assertTrue(schedule_due(self.config, None, self.moment(3)))

    def test_daily_catches_up_later_the_same_day(self):
        self.assertTrue(schedule_due(self.config, None, self.moment(12)))

    def test_daily_does_not_repeat_a_success_after_target(self):
        self.assertFalse(schedule_due(self.config, self.success(self.moment(3).isoformat()), self.moment(12)))
        self.assertTrue(schedule_due(self.config, self.success(self.moment(2).isoformat()), self.moment(12)))

    def test_daily_compares_absolute_time_across_offsets(self):
        previous = self.success("2026-10-11T20:00:00+00:00")
        self.assertFalse(schedule_due(self.config, previous, self.moment(12)))

    def test_previous_day_success_does_not_skip_today(self):
        previous = self.success("2026-10-11T23:59:00+08:00")
        self.assertTrue(schedule_due(self.config, previous, self.moment(3)))

    def test_hourly_waits_for_the_full_interval(self):
        self.config["interval"] = "hourly"
        previous = self.success(self.moment(9).isoformat())
        self.assertFalse(schedule_due(self.config, previous, self.moment(9, 59, 59)))
        self.assertTrue(schedule_due(self.config, previous, self.moment(10)))
        self.assertTrue(schedule_due(self.config, None, self.moment(1)))

    def test_success_for_different_paths_does_not_delay_backup(self):
        for key in ("source", "destination"):
            previous = self.success(self.moment(11).isoformat())
            previous[key] = "/different/path"
            self.assertTrue(schedule_due(self.config, previous, self.moment(12)))

    def test_malformed_success_time_does_not_disable_backup(self):
        self.assertTrue(schedule_due(self.config, self.success("not-a-time"), self.moment(12)))

    def test_fresh_daily_activation_before_target_does_not_backfill(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T14:00:00+08:00")
        self.assertFalse(schedule_due(self.config, None, self.moment(14)))
        self.assertFalse(schedule_due(self.config, None, self.moment(21, 59, 59)))
        self.assertTrue(schedule_due(self.config, None, self.moment(22)))

    def test_fresh_daily_activation_after_target_waits_until_next_target(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T22:10:00+08:00")
        self.assertFalse(schedule_due(self.config, None, self.moment(22, 15)))
        self.assertFalse(schedule_due(self.config, None, datetime(2026, 10, 13, 9, tzinfo=TIMEZONE)))
        self.assertTrue(schedule_due(self.config, None, datetime(2026, 10, 13, 22, tzinfo=TIMEZONE)))

    def test_missed_evening_backup_catches_up_after_midnight(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T14:00:00+08:00")
        self.assertTrue(schedule_due(self.config, None, datetime(2026, 10, 13, 9, tzinfo=TIMEZONE)))

    def test_evening_success_prevents_a_duplicate_next_morning(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T14:00:00+08:00")
        previous = self.success("2026-10-12T22:05:00+08:00")
        self.assertFalse(schedule_due(self.config, previous, datetime(2026, 10, 13, 9, tzinfo=TIMEZONE)))
        self.assertTrue(schedule_due(self.config, previous, datetime(2026, 10, 13, 22, tzinfo=TIMEZONE)))

    def test_catchup_success_covers_only_the_most_recent_target(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-10T14:00:00+08:00")
        previous = self.success("2026-10-13T09:00:00+08:00")
        self.assertFalse(schedule_due(self.config, previous, datetime(2026, 10, 13, 10, tzinfo=TIMEZONE)))
        self.assertTrue(schedule_due(self.config, previous, datetime(2026, 10, 13, 22, tzinfo=TIMEZONE)))

    def test_current_time_and_activation_offsets_use_shanghai_schedule(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T06:00:00+00:00")
        self.assertFalse(schedule_due(self.config, None, datetime.fromisoformat("2026-10-12T13:59:59+00:00")))
        self.assertTrue(schedule_due(self.config, None, datetime.fromisoformat("2026-10-12T14:00:00+00:00")))

    def test_naive_legacy_timestamps_are_interpreted_as_shanghai(self):
        self.config.update(schedule_time="22:00", schedule_activated_at="2026-10-12T14:00:00")
        previous = self.success("2026-10-12T22:05:00")
        self.assertFalse(schedule_due(self.config, previous, datetime(2026, 10, 13, 9)))

    def test_legacy_schedule_with_previous_success_can_catch_up(self):
        self.config["schedule_time"] = "22:00"
        previous = self.success("2026-10-11T22:05:00+08:00")
        self.assertTrue(schedule_due(self.config, previous, datetime(2026, 10, 13, 9, tzinfo=TIMEZONE)))


class ApplicationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.initial_config = {
            "source": str(self.source), "destination": str(self.root / "archives"),
            "schedule_enabled": False, "schedule_time": "03:00", "interval": "daily",
            "include_media": True, "timezone": "Asia/Shanghai",
        }
        write_json(self.state / "config.json", self.initial_config)
        patcher = patch("app.Scheduler", autospec=True)
        scheduler_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.scheduler = scheduler_class.return_value
        self.application = Application(self.state)

    def test_schedule_can_be_disabled_after_source_is_moved(self):
        previous = {**self.initial_config, "schedule_enabled": True}
        write_json(self.application.config_path, previous)
        self.source.rename(self.root / "moved-source")
        result = self.application.save_config({"schedule_enabled": False})
        self.assertFalse(result["schedule_enabled"])
        self.assertFalse(self.application.config()["schedule_enabled"])
        self.assertEqual(self.application.config()["source"], previous["source"])
        self.scheduler.uninstall.assert_called_once_with()
        self.scheduler.install.assert_not_called()

    def test_install_failure_restores_previous_settings(self):
        self.scheduler.install.side_effect = RuntimeError("Simulated installation failure")
        with self.assertRaisesRegex(RuntimeError, "Simulated installation failure"):
            self.application.save_config({"schedule_enabled": True, "schedule_time": "04:20"})
        self.assertEqual(self.application.config(), self.initial_config)
        self.scheduler.install.assert_called_once_with()
        self.scheduler.uninstall.assert_not_called()

    def test_enabling_daily_schedule_records_shanghai_activation_time(self):
        current = datetime.fromisoformat("2026-10-12T06:00:00+00:00")
        with patch("app.now", return_value=current):
            result = self.application.save_config({"schedule_enabled": True, "schedule_time": "22:00"})
        self.assertEqual(result["schedule_activated_at"], "2026-10-12T14:00:00+08:00")
        self.assertEqual(self.application.config()["schedule_activated_at"], result["schedule_activated_at"])
        self.assertFalse(schedule_due(result, None, current))

    def test_unchanged_enabled_schedule_preserves_activation_time(self):
        previous = {**self.initial_config, "schedule_enabled": True,
                    "schedule_activated_at": "2026-10-10T14:00:00+08:00"}
        write_json(self.application.config_path, previous)
        with patch("app.now") as clock:
            result = self.application.save_config({"schedule_enabled": True})
        self.assertEqual(result["schedule_activated_at"], previous["schedule_activated_at"])
        clock.assert_not_called()

    def test_legacy_enabled_schedule_gets_activation_time_on_save(self):
        previous = {**self.initial_config, "schedule_enabled": True, "schedule_time": "22:00"}
        write_json(self.application.config_path, previous)
        current = datetime(2026, 10, 12, 14, tzinfo=TIMEZONE)
        with patch("app.now", return_value=current):
            result = self.application.save_config({"schedule_enabled": True})
        self.assertEqual(result["schedule_activated_at"], current.isoformat())
        self.assertFalse(schedule_due(result, None, current))

    def test_changing_schedule_resets_activation_without_backfilling(self):
        current = datetime(2026, 10, 12, 23, tzinfo=TIMEZONE)
        cases = (
            ({"interval": "daily", "schedule_time": "03:00"}, {"schedule_time": "22:00"}),
            ({"interval": "hourly", "schedule_time": "22:00"}, {"interval": "daily"}),
        )
        for original, changes in cases:
            with self.subTest(changes=changes):
                previous = {**self.initial_config, **original, "schedule_enabled": True,
                            "schedule_activated_at": "2026-10-10T14:00:00+08:00"}
                write_json(self.application.config_path, previous)
                with patch("app.now", return_value=current):
                    result = self.application.save_config(changes)
                self.assertEqual(result["schedule_activated_at"], current.isoformat())
                self.assertFalse(schedule_due(result, None, current))

    def test_reenabling_schedule_does_not_backfill_disabled_period(self):
        previous = {**self.initial_config, "schedule_time": "22:00",
                    "schedule_activated_at": "2026-10-10T14:00:00+08:00"}
        write_json(self.application.config_path, previous)
        current = datetime(2026, 10, 12, 23, tzinfo=TIMEZONE)
        with patch("app.now", return_value=current):
            result = self.application.save_config({"schedule_enabled": True})
        self.assertEqual(result["schedule_activated_at"], current.isoformat())
        self.assertFalse(schedule_due(result, None, current))


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.state = self.root / "state"
        source = self.root / "synthetic-source"
        source.mkdir()
        self.config = {"source": str(source), "destination": str(self.root / "archives"),
                       "schedule_enabled": True, "schedule_time": "22:00", "interval": "daily",
                       "schedule_activated_at": "2026-10-12T14:00:00+08:00", "timezone": "Asia/Shanghai"}
        write_json(self.state / "config.json", self.config)
        scheduler = patch("app.Scheduler", autospec=True)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.application = Application(self.state)
        self.other = Application(self.state)

    def test_independent_instances_with_same_state_exclude_each_other(self):
        with self.application.operation_lock():
            with self.assertRaises(OperationBusy):
                with self.other.operation_lock():
                    self.fail("Another instance entered the operation")
        with self.other.operation_lock():
            with self.assertRaises(OperationBusy):
                with self.application.operation_lock():
                    self.fail("The first instance entered a held operation")
        self.assertEqual((self.state / "operation.lock").stat().st_mode & 0o777, 0o600)

    def test_same_instance_on_another_thread_cannot_inherit_reentrancy(self):
        results = []

        def attempt():
            try:
                with self.application.operation_lock():
                    results.append("acquired")
            except OperationBusy:
                results.append("busy")

        with self.application.operation_lock():
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, ["busy"])
        worker = threading.Thread(target=attempt)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["busy", "acquired"])

    def test_an_independent_process_observes_the_same_lock(self):
        program = """import sys
from app import Application, OperationBusy
application = Application(sys.argv[1])
try:
    with application.operation_lock():
        pass
except OperationBusy:
    raise SystemExit(73)
"""
        arguments = [sys.executable, "-B", "-c", program, str(self.state)]
        with self.application.operation_lock():
            blocked = subprocess.run(arguments, cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
        released = subprocess.run(arguments, cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 73, blocked.stderr.decode())
        self.assertEqual(released.returncode, 0, released.stderr.decode())
        self.assertEqual(blocked.stdout + released.stdout, b"")

    def test_nested_lock_survives_inner_exception_and_outer_exception_releases_it(self):
        with self.assertRaisesRegex(RuntimeError, "Outer failure"):
            with self.application.operation_lock():
                with self.assertRaisesRegex(RuntimeError, "Inner failure"):
                    with self.application.operation_lock():
                        raise RuntimeError("Inner failure")
                with self.application.operation_lock():
                    with self.assertRaises(OperationBusy):
                        with self.other.operation_lock():
                            self.fail("An inner exit released the outer lock")
                raise RuntimeError("Outer failure")
        with self.other.operation_lock():
            pass
        with self.application.operation_lock():
            pass

    def test_backup_can_reenter_its_own_lock_to_read_wechat(self):
        def fake_import(*args):
            with self.assertRaises(OperationBusy):
                with self.other.operation_lock():
                    self.fail("The backup released its lock during the nested import")
            return {"messages": 1}

        with patch.object(self.application.engine, "create", return_value={"id": "synthetic-snapshot"}) as create:
            with patch("app.key_status", return_value={"configured": True}), patch("app.import_account", side_effect=fake_import) as imported:
                result = self.application.backup_with_reader(self.config)
        self.assertEqual(result["reader"], {"messages": 1})
        create.assert_called_once()
        imported.assert_called_once()

    def test_scheduled_lock_conflict_is_deferred_and_retry_can_succeed(self):
        current = datetime(2026, 10, 12, 23, tzinfo=TIMEZONE)
        with patch("app.now", return_value=current), patch("app.wechat_running", return_value=False):
            with patch.object(self.application.engine, "create", return_value={"id": "synthetic-snapshot"}) as create:
                with self.other.operation_lock():
                    result = self.application.run_scheduled()
                self.assertEqual(result["status"], "deferred")
                self.assertIn("operation", result["reason"])
                create.assert_not_called()
                self.assertFalse((self.state / "last-success.json").exists())
                saved = json.loads((self.state / "schedule-result.json").read_text())
                self.assertEqual(saved["status"], "deferred")
                retry = self.application.run_scheduled()
                self.assertEqual(retry["status"], "done")
                create.assert_called_once()

    def start_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.application
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

        self.addCleanup(close)
        return server

    def post(self, server, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", path, body, {"Authorization": "Bearer " + self.application.token,
                                                   "Content-Type": "application/json", "X-Filename": "synthetic.json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_import_cannot_bypass_lock_and_succeeds_after_release(self):
        server = self.start_server()
        data = json.dumps({"title": "Synthetic", "messages": [{"sender": "Tester", "text": "Synthetic content"}]}).encode()
        with patch.object(self.application.library, "import_file", wraps=self.application.library.import_file) as imported:
            with self.other.operation_lock():
                status, result = self.post(server, "/api/import", data)
            self.assertEqual(status, 400)
            self.assertEqual(result["code"], "operation_busy")
            imported.assert_not_called()
            self.assertEqual(self.application.library.stats()["messages"], 0)
            status, result = self.post(server, "/api/import", data)
            self.assertEqual(status, 200)
            self.assertEqual(result["messages"], 1)
            imported.assert_called_once()
        self.assertEqual(self.application.library.stats()["messages"], 1)

    def test_http_key_save_and_demo_also_obey_operation_lock(self):
        server = self.start_server()
        with patch("app.save_verified_keys") as save_keys, patch.object(self.application.library, "demo") as demo:
            with self.other.operation_lock():
                for path in ("/api/wechat/keys", "/api/demo"):
                    with self.subTest(path=path):
                        status, result = self.post(server, path, b"{}")
                        self.assertEqual(status, 400)
                        self.assertEqual(result["code"], "operation_busy")
            save_keys.assert_not_called()
            demo.assert_not_called()


class LocalAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            token="test-private-token",
            config=Mock(return_value={"source": "test-source"}),
            save_config=Mock(return_value={"saved": True}),
            start_job=Mock(return_value={"job_id": "test-job"}),
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/config", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def authorized_headers(self, **extra):
        return {"Authorization": "Bearer " + self.app.token, **extra}

    def test_api_reads_require_bearer_token(self):
        status, _, data = self.request()
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data)["code"], "unauthorized")
        self.app.config.assert_not_called()

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.request(headers={"Authorization": "Bearer wrong-token"})[0], 401)
        self.app.config.assert_not_called()

    def test_non_ascii_token_returns_unauthorized_without_disconnecting(self):
        headers = {"Authorization": "Bearer \u00e9"}
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, _, data = self.request(method, headers=headers, body=b"{}" if method == "POST" else None)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(data)["code"], "unauthorized")
        self.app.config.assert_not_called()
        self.app.save_config.assert_not_called()

    def test_authorized_request_succeeds_without_browser_headers(self):
        status, headers, data = self.request(headers=self.authorized_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"source": "test-source"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_untrusted_host_cannot_rebind_to_local_server(self):
        headers = self.authorized_headers(Host=f"attacker.example:{self.server.server_port}")
        self.assertEqual(self.request(headers=headers)[0], 403)
        self.app.config.assert_not_called()

    def test_foreign_and_null_origins_are_blocked_even_with_token(self):
        for origin in ("https://attacker.example", "null"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers=self.authorized_headers(Origin=origin))[0], 403)
        self.app.config.assert_not_called()

    def test_cross_site_fetch_is_blocked_even_with_token(self):
        headers = self.authorized_headers(**{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(self.request(headers=headers)[0], 403)
        self.app.config.assert_not_called()

    def test_same_origin_browser_request_succeeds(self):
        headers = self.authorized_headers(Origin=f"http://127.0.0.1:{self.server.server_port}")
        self.assertEqual(self.request(headers=headers)[0], 200)

    def test_localhost_alias_is_accepted(self):
        host = f"localhost:{self.server.server_port}"
        headers = self.authorized_headers(Host=host, Origin="http://" + host)
        self.assertEqual(self.request(headers=headers)[0], 200)

    def test_no_token_cannot_mutate_settings_or_start_backup(self):
        for path in ("/api/config", "/api/backup"):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, body=b"{}")[0], 401)
        self.app.save_config.assert_not_called()
        self.app.start_job.assert_not_called()

    def test_authorized_write_rejects_cross_origin_request(self):
        headers = self.authorized_headers(Origin="https://attacker.example")
        self.assertEqual(self.request("POST", "/api/config", headers, b"{}")[0], 403)
        self.app.save_config.assert_not_called()

    def test_authorized_write_accepts_valid_object(self):
        headers = self.authorized_headers(**{"Content-Type": "application/json"})
        status, _, _ = self.request("POST", "/api/config", headers, b'{"schedule_enabled": false}')
        self.assertEqual(status, 200)
        self.app.save_config.assert_called_once_with({"schedule_enabled": False})

    def test_authorized_write_rejects_non_object_body(self):
        self.assertEqual(self.request("POST", "/api/config", self.authorized_headers(), b"[]")[0], 400)
        self.app.save_config.assert_not_called()

    def test_cors_preflight_does_not_grant_cross_origin_access(self):
        status, headers, _ = self.request("OPTIONS", headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 501)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main()
