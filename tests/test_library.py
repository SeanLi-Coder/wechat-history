import json
import tempfile
import unittest
from pathlib import Path

from vault.library import Library, parse_export


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_chatlab_import_search_pagination_and_repeat(self):
        payload = json.dumps({"meta": {"name": "Team"}, "members": [{"platformId": "1", "accountName": "Alice"}], "messages": [
            {"sender": "1", "content": "archive 100%_complete", "timestamp": 1703001600, "type": 0},
            {"sender": "1", "content": "archive normal", "timestamp": 1703001660, "type": 0},
        ]}).encode()
        self.assertEqual(self.library.import_file(payload, "chat.json")["messages"], 2)
        self.assertTrue(self.library.import_file(payload, "renamed.json")["duplicate"])
        conversations = self.library.conversations("100%_")["conversations"]
        self.assertEqual(len(conversations), 1)
        cid = conversations[0]["id"]
        first = self.library.messages(cid, limit=1)
        self.assertEqual(first["messages"][0]["sender"], "Alice")
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(self.library.messages(cid, query="100%_")["messages"]), 1)
        self.assertEqual(self.library.messages(cid, offset=1)["messages"][0]["text"], "archive normal")
        self.assertEqual(self.library.conversations("' OR 1=1 --")["conversations"], [])

    def test_failed_import_is_atomic(self):
        payload = json.dumps({"messages": [{"text": "ok"}, {"sender": "broken"}]}).encode()
        with self.assertRaises(ValueError):
            self.library.import_file(payload, "broken.json")
        self.assertEqual(self.library.stats(), {"conversations": 0, "messages": 0})

    def test_html_remains_plain_text(self):
        content = '<img src=x onerror="alert(1)">'
        self.library.import_file(json.dumps([{"text": content}]).encode(), "html.json")
        cid = self.library.conversations()["conversations"][0]["id"]
        self.assertEqual(self.library.messages(cid)["messages"][0]["text"], content)

    def test_txt_multiline_csv_and_jsonl(self):
        rows = parse_export(b"[2026-01-01 12:00:00] Alice: hello\nsecond line\n[2026-01-01 12:01:00] Bob: hi", "chat.txt")[0][1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "hello\nsecond line")
        groups = parse_export(b"conversation,sender,text,timestamp,is_self\nTeam,Alice,hello,1703001600,false\nOther,Bob,hi,1703001601,true", "chat.csv")
        self.assertEqual(len(groups), 2)
        self.assertFalse(groups[0][1][0]["is_self"])
        self.assertTrue(groups[1][1][0]["is_self"])
        data = b'{"_type":"header","meta":{"name":"Test"}}\n{"_type":"message","sender":"Alice","content":"hi","type":0,"timestamp":1703001600}'
        self.assertEqual(parse_export(data, "chat.jsonl")[0][0], "Test")

    def test_encrypted_database_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_export(b"not a readable database", "wechat.db")

    def test_mixed_timezones_sort_by_actual_time(self):
        self.library.import_file(json.dumps({"messages": [
            {"text": "later", "timestamp": "2026-01-01T09:00:00+08:00"},
            {"text": "earlier", "timestamp": "2026-01-01T00:30:00Z"},
        ]}).encode(), "timezone.json")
        cid = self.library.conversations()["conversations"][0]["id"]
        self.assertEqual([row["text"] for row in self.library.messages(cid)["messages"]], ["earlier", "later"])

    def test_malformed_metadata_and_jsonl_are_rejected(self):
        for payload, name in [(b'{"meta":[],"members":4,"messages":[]}', "bad.json"),
                              (b'{"_type":"header"}\n123', "bad.jsonl")]:
            with self.assertRaises(ValueError):
                parse_export(payload, name)


if __name__ == "__main__":
    unittest.main()
