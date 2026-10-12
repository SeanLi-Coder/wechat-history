"""Exercise native media authorization and byte ranges on a real loopback server."""

import http.client
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app import Handler
from vault.library import Library


class MediaHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-media-http-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = Library(self.root / "library")
        self.payload = bytes(range(256)) * 8
        source = self.root / "synthetic.png"
        source.write_bytes(self.payload)
        self.asset_id = self.library.media.store_file(source, "image", "image/png", "synthetic.png")
        self.path = "/api/media/" + self.asset_id
        self.app = SimpleNamespace(token="synthetic-private-media-token", reader_library=self.library,
                                   config=Mock(return_value={"schedule_enabled": False}),
                                   save_config=Mock(), start_job=Mock(), library=self.library)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path=None, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path or self.path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def cookie(self, **extra):
        return {"Cookie": "shiliao_media=" + self.app.token, **extra}

    def bearer(self, **extra):
        return {"Authorization": "Bearer " + self.app.token, **extra}

    def test_authenticated_api_issues_a_scoped_httponly_cookie(self):
        status, headers, _ = self.request(path="/api/config", headers=self.bearer())
        self.assertEqual(status, 200)
        cookies = SimpleCookie(headers["Set-Cookie"])
        media_cookie = cookies["shiliao_media"]
        self.assertEqual(media_cookie.value, self.app.token)
        self.assertTrue(media_cookie["httponly"])
        self.assertEqual(media_cookie["samesite"], "Strict")
        self.assertEqual(media_cookie["path"], "/api/media/")

    def test_media_get_accepts_cookie_or_bearer_without_token_in_url(self):
        for headers in (self.cookie(), self.bearer()):
            with self.subTest(headers=list(headers)):
                status, response_headers, data = self.request(headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(data, self.payload)
                self.assertEqual(response_headers["Content-Type"], "image/png")
                self.assertEqual(response_headers["Content-Length"], str(len(self.payload)))
                self.assertEqual(response_headers["Accept-Ranges"], "bytes")
                self.assertEqual(response_headers["Cache-Control"], "no-store")
                self.assertEqual(response_headers["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)

    def test_media_cookie_cannot_authorize_general_reads_or_mutations(self):
        for method, path in (("GET", "/api/config"), ("GET", "/api/conversations"),
                             ("POST", "/api/config"), ("POST", "/api/backup"),
                             ("POST", "/api/reader/export"), ("POST", "/api/reader/live"),
                             ("POST", self.path)):
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, self.cookie(), b"{}" if method == "POST" else None)
                self.assertEqual(status, 401)
        self.app.config.assert_not_called()
        self.app.save_config.assert_not_called()
        self.app.start_job.assert_not_called()

    def test_missing_wrong_and_query_string_tokens_do_not_authorize_media(self):
        cases = ((self.path, {}), (self.path, {"Cookie": "shiliao_media=wrong"}),
                 (self.path, {"Authorization": "Bearer wrong"}),
                 (self.path + "?token=" + self.app.token, {}),
                 (self.path, {"Cookie": 'shiliao_media="unterminated'}))
        for path, headers in cases:
            with self.subTest(path=path.split("?")[0], headers=list(headers)):
                self.assertEqual(self.request(path=path, headers=headers)[0], 401)

    def test_cross_origin_cross_site_and_rebound_host_reject_cookie_access(self):
        cases = ({"Origin": "https://example.invalid"}, {"Sec-Fetch-Site": "cross-site"},
                 {"Host": f"attacker.invalid:{self.server.server_port}"})
        for extra in cases:
            with self.subTest(extra=extra):
                self.assertEqual(self.request(headers=self.cookie(**extra))[0], 403)

    def test_inclusive_open_ended_suffix_and_clamped_ranges(self):
        size = len(self.payload)
        cases = (("bytes=0-0", 0, 0), ("bytes=17-31", 17, 31),
                 ("bytes=2040-", 2040, size - 1), ("bytes=-7", size - 7, size - 1),
                 ("bytes=2038-999999999999999999999", 2038, size - 1),
                 ("bytes=-999999999999999999999", 0, size - 1))
        for value, start, end in cases:
            with self.subTest(value=value):
                status, headers, data = self.request(headers=self.cookie(Range=value))
                self.assertEqual(status, 206)
                self.assertEqual(data, self.payload[start:end + 1])
                self.assertEqual(headers["Content-Length"], str(end - start + 1))
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{size}")

    def test_unsatisfiable_and_malformed_ranges_return_416_without_data(self):
        values = ("bytes=2048-", "bytes=4096-8192", "bytes=100-99", "bytes=-0", "bytes=-",
                  "bytes=", "items=0-1", "bytes=0-1,3-4", "bytes=not-a-number", "bytes=+1-2")
        for value in values:
            with self.subTest(value=value):
                status, headers, data = self.request(headers=self.cookie(Range=value))
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{len(self.payload)}")
                self.assertEqual(headers["Content-Length"], "0")
                self.assertEqual(data, b"")

    def test_head_reports_matching_full_and_partial_lengths_without_body(self):
        for requested, status, length in ((None, 200, len(self.payload)), ("bytes=5-12", 206, 8)):
            with self.subTest(range=requested):
                headers = self.cookie(**({"Range": requested} if requested else {}))
                actual, response_headers, body = self.request("HEAD", headers=headers)
                self.assertEqual(actual, status)
                self.assertEqual(response_headers["Content-Length"], str(length))
                self.assertEqual(body, b"")

    def test_unknown_or_invalid_asset_ids_never_expose_local_paths(self):
        for suffix in ("f" * 64, "../config.json", "%2e%2e%2fconfig.json", self.asset_id + "/extra"):
            with self.subTest(suffix=suffix):
                status, _, body = self.request(path="/api/media/" + suffix, headers=self.cookie())
                self.assertEqual(status, 404)
                self.assertNotIn(str(self.root).encode(), body)
                self.assertEqual(json.loads(body)["error"], "Media is unavailable")

    def test_active_archive_library_controls_media_resolution(self):
        self.app.reader_library = Library(self.root / "empty-archive-library")
        self.assertEqual(self.request(headers=self.cookie())[0], 404)
        self.app.reader_library = self.library
        self.assertEqual(self.request(headers=self.cookie())[2], self.payload)


if __name__ == "__main__":
    unittest.main()
