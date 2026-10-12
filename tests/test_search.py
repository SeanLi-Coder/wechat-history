"""Verify literal search, indexed context, immutable archives and HTTP access."""

from contextlib import contextmanager
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app import Handler
from vault.library import Library, normalize_time
from vault.readable_archive import create_archive, open_archive


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-search-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = Library(self.root / "library")
        self.library.import_file(json.dumps({"conversations": [
            {"title": "周末摄影", "messages": [
                {"sender": "张三 Alice", "text": "正文专有词：海边日落", "timestamp": "2026-01-01T10:00:00Z"},
                {"sender": "我", "text": "收到你的照片", "is_self": True, "timestamp": "2026-01-01T10:01:00Z"},
                {"sender": "A%_\\Coder", "text": "Literal 100%_complete \\ file", "timestamp": "2026-01-01T10:02:00Z"},
                {"sender": "Bob", "text": "<script>synthetic()</script>", "timestamp": "2026-01-01T10:03:00Z"},
            ]},
            {"title": "工作群", "messages": [
                {"sender": "张三 ALICE", "text": "评审时间已确定", "timestamp": "2026-01-02T10:00:00Z"},
                {"sender": "Carol", "text": "Sunset notes", "timestamp": "2026-01-02T10:01:00Z"},
            ]},
            {"title": "上海旅行", "messages": [
                {"sender": "同行者", "text": "明天出发", "timestamp": "2026-01-03T10:00:00Z"},
            ]},
        ]}, ensure_ascii=False).encode(), "synthetic-search.json")
        self.conversations = {row["title"]: row["id"] for row in self.library.conversations()["conversations"]}

    def test_global_matches_title_sender_and_body_with_distinct_semantics(self):
        sender = self.library.search("alice")
        self.assertEqual(sender["total"], 2)
        self.assertEqual(sender["conversation_total"], 2)
        self.assertEqual([row["conversation_title"] for row in sender["messages"]], ["工作群", "周末摄影"])
        self.assertEqual(self.library.search("张三")["total"], 2)
        title = self.library.search("上海")
        self.assertEqual((title["conversation_total"], title["total"]), (1, 0))
        self.assertEqual(title["conversations"][0]["title"], "上海旅行")
        body = self.library.search("正文专有词")
        self.assertEqual((body["conversation_total"], body["total"]), (0, 1))
        self.assertEqual(body["messages"][0]["conversation_id"], self.conversations["周末摄影"])
        self.assertFalse(body["messages"][0]["is_self"])
        self.assertTrue(self.library.search("收到")["messages"][0]["is_self"])
        self.assertEqual(self.library.search("sunset")["total"], 1)

    def test_literal_wildcards_backslash_and_html_are_not_interpreted(self):
        self.assertEqual(self.library.search("100%_")["total"], 1)
        self.assertEqual(self.library.search("A%_\\")["total"], 1)
        self.assertEqual(self.library.search("%_")["total"], 1)
        self.assertEqual(self.library.search("' OR 1=1 --")["total"], 0)
        html = self.library.search("<script>")["messages"][0]["text"]
        self.assertEqual(html, "<script>synthetic()</script>")

    def test_empty_global_query_does_not_read_or_dump_messages(self):
        with patch.object(self.library, "connect", side_effect=AssertionError("Unexpected database read")):
            result = self.library.search("  ")
        self.assertEqual((result["query"], result["conversations"], result["messages"], result["total"]), ("", [], [], 0))

    def test_pagination_total_out_of_range_and_newest_first(self):
        first = self.library.search("张三", limit=1)
        second = self.library.search("张三", offset=1, limit=1)
        beyond = self.library.search("张三", offset=20, limit=1)
        self.assertEqual((first["total"], first["has_more"], first["offset"], first["limit"]), (2, True, 0, 1))
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["messages"][0]["id"], second["messages"][0]["id"])
        self.assertGreater(first["messages"][0]["timestamp"], second["messages"][0]["timestamp"])
        self.assertEqual((beyond["total"], beyond["messages"], beyond["has_more"]), (2, [], False))

    def test_conversation_results_report_their_limit_instead_of_hiding_truncation(self):
        self.library.import_file(json.dumps({"conversations": [
            {"title": f"检索联系人 {index}", "messages": [{"sender": "Other", "text": "ordinary", "timestamp": 1700000000 + index}]}
            for index in range(58)]}, ensure_ascii=False).encode(), "many-conversations.json")
        result = self.library.search("检索联系人")
        self.assertEqual((result["conversation_total"], result["conversation_limit"], len(result["conversations"])), (58, 50, 50))
        self.assertTrue(result["conversations_has_more"])
        self.assertEqual(result["total"], 0)

    def test_existing_local_search_includes_senders_and_preserves_body_matching(self):
        self.assertEqual(len(self.library.conversations("ALICE")["conversations"]), 2)
        self.assertEqual(len(self.library.conversations("正文专有词")["conversations"]), 1)
        local = self.library.messages(self.conversations["周末摄影"], query="alice")
        self.assertEqual((local["total"], local["messages"][0]["sender"]), (1, "张三 Alice"))

    def test_invalid_queries_and_pages_are_rejected(self):
        for query in ("a" * 201, "bad\x00query", None):
            with self.subTest(query_type=type(query).__name__):
                with self.assertRaises(ValueError):
                    self.library.search(query)
        for offset, limit in ((-1, 50), (10_000_001, 50), (0, 0), (0, 101), (True, 50), (0, 1.5)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises(ValueError):
                    self.library.search("x", offset, limit)

    def add_context_conversation(self):
        self.library.import_file(json.dumps({"title": "Context", "messages": [
            {"sender": "Alice" if index % 2 else "Bob", "text": f"context-{index}",
             "timestamp": "2026-02-01T10:00:00Z", "is_self": bool(index % 2)}
            for index in range(230)]}).encode(), "context.json")
        cid = self.library.conversations("Context")["conversations"][0]["id"]
        rows = self.library.messages(cid, limit=500)["messages"]
        path = self.root / "synthetic-media.png"
        path.write_bytes(b"Synthetic image bytes")
        asset = self.library.media.store_file(path, "image", "image/png")
        self.library.media.link(rows[115]["id"], "image", asset)
        return cid, rows

    def test_context_contains_exact_target_media_and_contiguous_before_after_pages(self):
        cid, rows = self.add_context_conversation()
        self.assertEqual([row["text"] for row in rows], [f"context-{index}" for index in range(229, -1, -1)])
        context = self.library.context(cid, rows[115]["id"], limit=60)
        self.assertEqual((context["offset"], context["total"], context["limit"]), (85, 230, 60))
        self.assertTrue(context["has_before"] and context["has_after"])
        self.assertEqual(context["target_message_id"], rows[115]["id"])
        self.assertEqual([row["id"] for row in context["messages"]], [row["id"] for row in rows[85:145]])
        target = context["messages"][30]
        self.assertEqual(target["media"][0]["status"], "ready")
        self.assertEqual(target["text"], "context-114")
        self.assertFalse(target["is_self"])
        before = self.library.messages(cid, offset=25, limit=60)["messages"]
        after = self.library.messages(cid, offset=145, limit=60)["messages"]
        combined = before + context["messages"] + after
        self.assertEqual([row["id"] for row in combined], [row["id"] for row in rows[25:205]])
        self.assertEqual(len({row["id"] for row in combined}), 180)
        self.assertTrue(all(left["id"] > right["id"] for left, right in zip(combined, combined[1:])))

    def test_context_edges_and_cross_conversation_ids(self):
        cid, rows = self.add_context_conversation()
        first = self.library.context(cid, rows[0]["id"], limit=60)
        last = self.library.context(cid, rows[-1]["id"], limit=60)
        self.assertEqual((first["offset"], first["has_before"], first["has_after"]), (0, False, True))
        self.assertEqual((last["offset"], last["has_before"], last["has_after"]), (170, True, False))
        with self.assertRaises(KeyError):
            self.library.context(self.conversations["上海旅行"], rows[0]["id"])
        for message_id, limit in ((0, 60), (-1, 60), (2 ** 63, 60), (rows[0]["id"], 201)):
            with self.assertRaises(ValueError):
                self.library.context(cid, message_id, limit)

    def test_immutable_older_archive_supports_search_and_context_without_rewrite(self):
        cid, rows = self.add_context_conversation()
        with self.library.connect() as db:
            db.execute("DROP TABLE media_import_revisions")
        archive = create_archive(self.library, self.root / "archives")
        path = Path(archive["path"])
        before = hashlib.sha256(path.read_bytes()).digest()
        reader_root = self.root / "reader"
        reader_root.mkdir()
        opened = open_archive(path, reader_root)
        self.assertEqual(opened.search("alice")["total"], 117)
        archived_messages = opened.messages(cid, offset=110, limit=10)["messages"]
        self.assertEqual([row["text"] for row in archived_messages],
                         [f"context-{index}" for index in range(119, 109, -1)])
        result = opened.context(cid, rows[115]["id"], limit=60)
        self.assertEqual((result["offset"], result["has_before"], result["has_after"]), (85, True, True))
        self.assertEqual([row["id"] for row in result["messages"]], [row["id"] for row in rows[85:145]])
        self.assertEqual(result["messages"][30]["media"][0]["status"], "ready")
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        with opened.connect() as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)


class DateSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-date-search-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = Library(self.root / "library")
        self.library.import_file(json.dumps({"conversations": [
            {"title": "Calendar Person", "messages": [
                {"sender": "Before", "text": "before", "timestamp": "2023-12-31T15:59:59.999999Z"},
                {"sender": "Needle Sender", "text": "needle start", "timestamp": "2024-01-01T00:00:00+08:00"},
                {"sender": "Other", "text": "needle fraction", "timestamp": "2023-12-31T16:00:00.000001Z"},
                {"sender": "Other", "text": "middle", "timestamp": "2024-01-01T08:00:00"},
                {"sender": "Other", "text": "needle end", "timestamp": "2024-01-01T15:59:59.999999Z"},
                {"sender": "OutsideAlias", "text": "after", "timestamp": "2024-01-02T00:00:00+08:00"},
                {"sender": "Unknown", "text": "unknown"},
            ]},
            {"title": "Needle historical title", "messages": [
                {"sender": "Historical", "text": "historical", "timestamp": "2023-12-01T00:00:00Z"}]},
            {"title": "Quiet chat", "messages": [
                {"sender": "GhostAlias", "text": "outside sender", "timestamp": "2023-12-31T00:00:00Z"},
                {"sender": "Other", "text": "quiet inside", "timestamp": "2024-01-01T09:00:00+08:00"}]},
        ]}).encode(), "calendar.json")
        self.cid = self.library.conversations("Calendar Person")["conversations"][0]["id"]
        self.day = {"date_from": "2024-01-01", "date_to": "2024-01-01"}

    def test_shanghai_midnight_includes_exact_start_and_fractional_end(self):
        result = self.library.messages(self.cid, **self.day)
        self.assertEqual(result["total"], 4)
        self.assertEqual([row["text"] for row in result["messages"]],
                         ["needle end", "middle", "needle fraction", "needle start"])
        self.assertEqual(result["messages"][-1]["timestamp"], "2023-12-31T16:00:00+00:00")
        self.assertEqual(result["messages"][-2]["timestamp"], "2023-12-31T16:00:00.000001+00:00")
        self.assertEqual(normalize_time(1704038400000), "2023-12-31T16:00:00+00:00")
        self.assertEqual(normalize_time(1704038400.000001), "2023-12-31T16:00:00.000001+00:00")

    def test_single_sided_dates_exclude_unknown_times_and_no_dates_keep_them(self):
        self.assertEqual(self.library.messages(self.cid)["total"], 7)
        after = self.library.messages(self.cid, date_from="2024-01-01")
        before = self.library.messages(self.cid, date_to="2024-01-01")
        self.assertEqual((after["total"], before["total"]), (5, 5))
        self.assertEqual(after["messages"][0]["text"], "after")
        self.assertEqual(before["messages"][-1]["text"], "before")
        self.assertTrue(all(row["timestamp"] for row in after["messages"] + before["messages"]))
        self.assertEqual(self.library.search("unknown")["total"], 1)
        self.assertEqual(self.library.search("unknown", date_to="2024-01-01")["total"], 0)

    def test_pure_dates_and_keyword_are_combined_for_counts_previews_and_senders(self):
        pure = self.library.search(**self.day)
        self.assertEqual((pure["query"], pure["total"], pure["conversation_total"]), ("", 5, 2))
        summaries = {row["title"]: row for row in pure["conversations"]}
        main = summaries["Calendar Person"]
        self.assertEqual((main["message_count"], main["preview"], main["last_message_at"]),
                         (4, "needle end", "2024-01-01T15:59:59.999999+00:00"))
        keyword = self.library.search("needle", **self.day)
        self.assertEqual((keyword["total"], keyword["conversation_total"]), (3, 1))
        self.assertEqual(keyword["conversations"][0]["message_count"], 4)
        self.assertEqual(self.library.search("Calendar Person", **self.day)["conversation_total"], 1)
        self.assertEqual(self.library.search("Calendar Person", **self.day)["total"], 0)
        for query in ("OutsideAlias", "GhostAlias", "historical"):
            with self.subTest(query=query):
                result = self.library.search(query, **self.day)
                self.assertEqual((result["total"], result["conversation_total"]), (0, 0))
        self.assertEqual(self.library.messages(self.cid, "needle", **self.day)["total"], 3)

    def test_date_filtered_pagination_stays_descending_without_duplicates(self):
        rows = []
        for offset in (0, 2, 4, 6):
            result = self.library.search(offset=offset, limit=2, **self.day)
            self.assertEqual(result["total"], 5)
            self.assertEqual(result["has_more"], offset + len(result["messages"]) < 5)
            rows.extend(result["messages"])
        self.assertEqual(len({row["id"] for row in rows}), 5)
        self.assertEqual([row["text"] for row in rows],
                         ["needle end", "quiet inside", "middle", "needle fraction", "needle start"])
        local = [row for offset in (0, 2) for row in self.library.messages(
            self.cid, offset=offset, limit=2, **self.day)["messages"]]
        self.assertEqual([row["id"] for row in local], [row["id"] for row in rows if row["conversation_id"] == self.cid])

    def test_leap_day_and_supported_calendar_extremes(self):
        self.library.import_file(json.dumps({"title": "Leap", "messages": [
            {"text": "leap before", "timestamp": "2024-02-28T23:59:59.999999+08:00"},
            {"text": "leap first", "timestamp": "2024-02-29T00:00:00+08:00"},
            {"text": "leap last", "timestamp": "2024-02-29T23:59:59.999999+08:00"},
            {"text": "leap after", "timestamp": "2024-03-01T00:00:00+08:00"},
            {"text": "latest representable local day", "timestamp": "9999-12-31T15:59:59.999999Z"},
            {"text": "outside local calendar", "timestamp": "9999-12-31T16:00:00Z"},
            {"text": "earliest representable UTC day", "timestamp": "0001-01-01T00:00:00Z"},
        ]}).encode(), "leap.json")
        leap = self.library.search(date_from="2024-02-29", date_to="2024-02-29")
        self.assertEqual([row["text"] for row in leap["messages"]], ["leap last", "leap first"])
        self.assertEqual(self.library.search(date_from="0001-01-01", date_to="0001-01-01")["total"], 1)
        latest = self.library.search(date_from="9999-12-31", date_to="9999-12-31")
        self.assertEqual([row["text"] for row in latest["messages"]], ["latest representable local day"])

    def test_invalid_dates_and_reversed_ranges_are_rejected_before_reading(self):
        invalid = [(value, "") for value in (None, 20240101, "2024-1-01", "20240101", "2024/01/01",
            "2023-02-29", "2024-02-30", "2024-13-01", "0000-01-01", "10000-01-01",
            "2024-01-01T00:00:00Z", " 2024-01-01", "２０２４-01-01", "' OR 1=1 --")]
        invalid.extend([("", "2023-02-29"), ("2024-02-01", "2024-01-31")])
        with patch.object(self.library, "connect", side_effect=AssertionError("Unexpected database read")):
            for first, last in invalid:
                for method, args in ((self.library.search, ()), (self.library.messages, (self.cid,))):
                    with self.subTest(first=first, last=last, method=method.__name__):
                        with self.assertRaises(ValueError):
                            method(*args, date_from=first, date_to=last)

    def test_context_retains_unfiltered_neighbors_and_avatar_fields(self):
        source = self.root / "synthetic.png"
        source.write_bytes(b"Synthetic avatar bytes")
        asset = self.library.media.store_file(source, "image", "image/png")
        identity = hashlib.sha256(b"synthetic-account:synthetic-user").hexdigest()
        self.library.set_avatar(identity, asset)
        target = self.library.messages(self.cid, "needle start", **self.day)["messages"][0]
        with self.library.connect() as db:
            db.execute("INSERT INTO conversation_avatars VALUES(?,?)", (self.cid, identity))
            db.execute("INSERT INTO message_avatars VALUES(?,?)", (target["id"], identity))
        result = self.library.search("needle start", **self.day)
        self.assertEqual(result["messages"][0]["avatar_url"], "/api/media/" + asset)
        conversations = self.library.search("Calendar Person", **self.day)["conversations"]
        self.assertEqual(conversations[0]["avatar_url"], "/api/media/" + asset)
        context = self.library.context(self.cid, target["id"], limit=20)
        self.assertEqual(context["total"], 7)
        self.assertIn("before", [row["text"] for row in context["messages"]])
        self.assertIn("after", [row["text"] for row in context["messages"]])

    def test_older_archive_supports_dates_without_rewriting_database_or_zip(self):
        with self.library.connect() as db:
            for table in ("avatar_assets", "conversation_avatars", "message_avatars"):
                db.execute(f"DROP TABLE {table}")
        path = Path(create_archive(self.library, self.root / "archives")["path"])
        before = hashlib.sha256(path.read_bytes()).digest()
        reader = self.root / "reader"
        reader.mkdir()
        opened = open_archive(path, reader)
        database_before = hashlib.sha256(opened.path.read_bytes()).digest()
        self.assertEqual(opened.search(**self.day)["total"], 5)
        self.assertEqual(opened.messages(self.cid, **self.day)["total"], 4)
        self.assertEqual(opened.search("needle", **self.day)["messages"][0]["avatar_url"], None)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        self.assertEqual(hashlib.sha256(opened.path.read_bytes()).digest(), database_before)
        with opened.connect() as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)


class SearchScaleTests(unittest.TestCase):
    def test_query_work_scales_with_messages_instead_of_conversations_times_messages(self):
        """Count SQLite VM operations instead of relying on flaky wall times."""
        with tempfile.TemporaryDirectory(prefix="shiliao-search-scale-") as temporary:
            costs = []
            for conversation_count in (100, 500):
                library = Library(Path(temporary) / str(conversation_count))
                messages_per_conversation = 10_000 // conversation_count
                with library.connect() as db:
                    db.executemany("INSERT INTO conversations VALUES(?,?,?)",
                        ((f"c-{index}", f"Synthetic {index}", "scale") for index in range(conversation_count)))
                    db.executemany("INSERT INTO messages(conversation_id,sender,text,timestamp,is_self) VALUES(?,?,?,?,0)",
                        ((f"c-{conversation}", "needle-target" if index == messages_per_conversation - 1 else "Ordinary",
                          "ordinary body", "2026-01-01T00:00:00Z")
                         for conversation in range(conversation_count) for index in range(messages_per_conversation)))
                original_connect = library.connect
                ticks = [0]

                def tick():
                    ticks[0] += 1
                    return 0

                @contextmanager
                def measured_connect():
                    with original_connect() as db:
                        db.set_progress_handler(tick, 100)
                        yield db

                with patch.object(library, "connect", measured_connect):
                    result = library.search("needle-target")
                    local_results = library.conversations("needle-target")
                self.assertEqual((result["total"], result["conversation_total"]), (conversation_count, conversation_count))
                self.assertEqual(len(local_results["conversations"]), conversation_count)
                costs.append(ticks[0] * 100)
            self.assertLess(costs[1], costs[0] * 2.5, f"Search work grew with the conversation multiplier: {costs}")
            self.assertLess(max(costs), 10_000 * 250, f"Search performed excessive repeated scans: {costs}")


class SearchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-search-http-")
        self.addCleanup(self.temporary.cleanup)
        self.library = Library(Path(self.temporary.name) / "library")
        self.library.import_file(json.dumps({"title": "HTTP", "messages": [
            {"sender": "Alice", "text": "HTTP needle", "timestamp": 1700000000}]}).encode(), "http.json")
        self.conversation = self.library.conversations()["conversations"][0]["id"]
        self.message = self.library.messages(self.conversation)["messages"][0]["id"]
        self.application = SimpleNamespace(token="synthetic-search-token", reader_library=self.library)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.application
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path, headers=headers if headers is not None else {"Authorization": "Bearer " + self.application.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_search_context_and_read_only_active_source(self):
        status, result = self.request("/api/search?" + urlencode({"q": "alice", "limit": 1}))
        self.assertEqual((status, result["total"]), (200, 1))
        status, context = self.request(f"/api/conversations/{self.conversation}/context?message_id={self.message}&limit=60")
        self.assertEqual((status, context["target_message_id"]), (200, self.message))
        self.assertIn("media", context["messages"][0])
        self.application.reader_library = Library(Path(self.temporary.name) / "another-source")
        self.assertEqual(self.request("/api/search?q=alice")[1]["total"], 0)

    def test_authentication_and_parameter_errors(self):
        for headers in ({}, {"Cookie": "shiliao_media=" + self.application.token}):
            self.assertEqual(self.request("/api/search?q=alice", headers)[0], 401)
        for query in ({"q": "a" * 201}, {"offset": -1}, {"limit": 101}, {"limit": "not-a-number"}):
            self.assertEqual(self.request("/api/search?" + urlencode(query))[0], 400)
        self.assertEqual(self.request(f"/api/conversations/{self.conversation}/context?message_id=999999")[0], 404)
        self.assertEqual(self.request(f"/api/conversations/{self.conversation}/context?message_id=0")[0], 400)

    def test_date_parameters_support_pure_dates_and_report_invalid_calendar_ranges(self):
        routes = ("/api/search", f"/api/conversations/{self.conversation}/messages")
        for route in routes:
            with self.subTest(route=route):
                status, result = self.request(route + "?" + urlencode({
                    "date_from": "2023-11-15", "date_to": "2023-11-15"}))
                self.assertEqual((status, result["total"]), (200, 1))
                self.assertEqual(self.request(route + "?date_from=2023-11-16")[1]["total"], 0)
                self.assertEqual(self.request(route + "?date_to=2023-11-14")[1]["total"], 0)
                self.assertEqual(self.request(route + "?date_from=2023-11-15&q=absent")[1]["total"], 0)
                for query in ({"date_from": "2023-02-29"}, {"date_to": "2024-1-1"},
                              {"date_from": "2024-03-02", "date_to": "2024-03-01"}):
                    self.assertEqual(self.request(route + "?" + urlencode(query))[0], 400)
        status, context = self.request(f"/api/conversations/{self.conversation}/context?message_id={self.message}&date_from=2025-01-01")
        self.assertEqual((status, context["total"]), (200, 1))


if __name__ == "__main__":
    unittest.main()
