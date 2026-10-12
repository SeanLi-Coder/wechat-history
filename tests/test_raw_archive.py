"""Open synthetic encrypted raw backups without touching live WeChat data."""

from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

from vault.backup import BackupEngine
from vault.library import Library
from vault.raw_archive import RawArchiveError, RawArchiveSource, inspect_archive, open_raw_archive
from vault.readable_archive import create_archive, open_archive
from vault.runtime import find_tool
from vault.wechat_access import save_verified_keys
from test_wechat_crypto import Cipher, encrypt_fixture, make_sqlite


CHAT = "synthetic-reader-contact"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
STAMP = 1781524800


@unittest.skipIf(Cipher is None, "Encrypted fixtures require cryptography")
class RawArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-raw-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.account = self.root / "synthetic-account"
        self.database = self.account / "db_storage"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.keys = {}
        self.plain = {}
        self.make_database("contact/contact.db", lambda db: db.executescript(
            "CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT);"
            f"INSERT INTO contact VALUES('{CHAT}','Synthetic contact','');"))
        def messages(db):
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.execute("INSERT INTO Name2Id VALUES(?)", (CHAT,))
            db.execute(f'''CREATE TABLE "{TABLE}"(local_id INTEGER,server_id INTEGER,
                local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content BLOB)''')
            db.execute(f'INSERT INTO "{TABLE}" VALUES(1,101,1,?,1,?)',
                       (STAMP, "只有归档里的合成文本".encode()))
        self.make_database("message/message_0.db", messages)
        self.home_patch = patch("pathlib.Path.home", return_value=self.root / "synthetic-home")
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def make_database(self, relative, populate):
        plain = self.root / (relative.replace("/", "-") + ".plain")
        make_sqlite(plain)
        with closing(sqlite3.connect(plain)) as db, db:
            db.execute("DROP TABLE messages")
            populate(db)
        self.plain[relative] = plain
        self.encrypt_database(relative)

    def encrypt_database(self, relative):
        key = hashlib.sha256(("fixture-key:" + relative).encode()).digest()
        self.keys[relative] = key.hex()
        target = self.database / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = self.plain[relative].read_bytes()
        self.assertEqual(content[20], 80)
        target.write_bytes(encrypt_fixture(content, key))

    def keys_ready(self):
        save_verified_keys(json.dumps({"keys": self.keys}).encode(), self.account, self.state)

    def backup(self, source=None):
        result = BackupEngine(self.root / "backup-state").create(
            str(source or self.account), str(self.root / "backups"), allow_running=True)
        return Path(result["path"])

    def assert_clean(self):
        cache = self.state / "opened-raw-archives"
        self.assertFalse(list(cache.glob(".raw-reader-*")))
        self.assertFalse(list(cache.rglob("wechat-keys.json")))
        self.assertFalse(list(cache.rglob("db_storage")))
        self.assertFalse(list(cache.rglob("wechat-read-*")))

    def rewrite(self, path, change):
        with zipfile.ZipFile(path) as source:
            entries = {item.filename: [item, source.read(item)] for item in source.infolist()}
        manifest = json.loads(entries["manifest.json"][1])
        change(entries, manifest)
        entries["manifest.json"][1] = json.dumps(manifest).encode()
        result = self.root / "modified.zip"
        with zipfile.ZipFile(result, "w") as target:
            for info, data in entries.values():
                target.writestr(info, data)
        return result

    def test_identifies_both_formats_and_rejects_ordinary_zip(self):
        info = inspect_archive(self.backup())
        self.assertEqual(info["format"], "raw")
        self.assertEqual(info["accounts"], 1)
        self.assertGreater(info["required_bytes"], info["original_bytes"])
        local = Library(self.root / "local")
        readable = create_archive(local, self.root / "readable")
        self.assertEqual(inspect_archive(readable["path"])["format"], "readable")
        ordinary = self.root / "ordinary.zip"
        with zipfile.ZipFile(ordinary, "w") as archive:
            archive.writestr("unrelated.txt", b"synthetic")
        with self.assertRaisesRegex(RawArchiveError, "拾聊"):
            inspect_archive(ordinary)

    def test_real_decryption_is_isolated_immutable_and_warm_cache_needs_no_keys(self):
        self.keys_ready()
        live = Library(self.state)
        live.import_file(b'{"messages":[{"text":"existing local message"}]}', "local.json")
        original_library = live.path.read_bytes()
        backup = self.backup()
        original_zip = backup.read_bytes()
        shutil.rmtree(self.account)
        progress = []
        opened = open_raw_archive(backup, self.state, progress.append)
        self.assertFalse(opened.raw_cache_hit)
        self.assertEqual(opened.stats(), {"messages": 1, "conversations": 1})
        self.assertEqual(opened.search("合成文本")["total"], 1)
        with self.assertRaises(sqlite3.OperationalError):
            with opened.connect() as db:
                db.execute("DELETE FROM messages")
        self.assertEqual(live.path.read_bytes(), original_library)
        self.assertEqual(backup.read_bytes(), original_zip)
        self.assertTrue(any(row["phase"] == "raw_extract" for row in progress))
        self.assertFalse(any(row.get("snapshot_ready") for row in progress))
        self.assertTrue(all("合成文本" not in json.dumps(row) for row in progress))
        (self.state / "wechat-keys.json").unlink()
        with patch("vault.raw_archive.import_account", side_effect=AssertionError("Cache must not import")):
            again = open_raw_archive(backup, self.state)
        self.assertTrue(again.raw_cache_hit)
        self.assertEqual(again.path, opened.path)
        self.assertEqual(opened.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(opened.state_dir.stat().st_mode & 0o777, 0o700)
        self.assert_clean()

    def test_missing_keys_is_actionable_and_never_extracts(self):
        with patch.object(RawArchiveSource, "extract") as extract:
            with self.assertRaises(RawArchiveError) as error:
                open_raw_archive(self.backup(), self.state)
            self.assertEqual(error.exception.code, "keys_required")
            self.assertIn("初始化", str(error.exception))
            extract.assert_not_called()
        self.assert_clean()

    def test_absent_auxiliary_databases_keep_missing_media_and_original_account_identity(self):
        with closing(sqlite3.connect(self.plain["message/message_0.db"])) as db, db:
            for ident, kind in ((2, 3), (3, 34), (4, 43)):
                db.execute(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,1,?)',
                           (ident, 100 + ident, kind, STAMP, b"Synthetic missing media"))
        self.encrypt_database("message/message_0.db")
        self.keys_ready()
        opened = open_raw_archive(self.backup(), self.state)
        conversation = opened.conversations()["conversations"][0]
        rows = opened.messages(conversation["id"])["messages"]
        assets = [asset for row in rows for asset in row.get("media", [])]
        self.assertEqual({item["kind"] for item in assets}, {"image", "audio", "video"})
        self.assertTrue(all(item["status"] == "missing" for item in assets))
        with opened.connect() as db:
            identities = {row[0] for row in db.execute("SELECT DISTINCT account_key FROM wechat_source_messages")}
        self.assertEqual(identities, {hashlib.sha256(str(self.database).encode()).hexdigest()})

    def test_insufficient_space_is_checked_before_extract(self):
        self.keys_ready()
        usage = shutil._ntuple_diskusage(10, 10, 0)
        path = self.backup()
        with patch("vault.raw_archive.shutil.disk_usage", return_value=usage), patch.object(RawArchiveSource, "extract") as extract:
            with self.assertRaises(RawArchiveError) as error:
                open_raw_archive(path, self.state)
            self.assertEqual(error.exception.code, "insufficient_space")
            extract.assert_not_called()
        self.assert_clean()

    def test_wrong_key_fails_without_leaving_plaintext_or_partial_library(self):
        self.keys_ready()
        (self.state / "wechat-keys.json").write_text(json.dumps({"keys": {name: "ab" * 32 for name in self.keys}}))
        with self.assertRaises(RawArchiveError) as error:
            open_raw_archive(self.backup(), self.state)
        self.assertEqual(error.exception.code, "keys_mismatch")
        self.assertNotIn("ab" * 32, str(error.exception))
        self.assert_clean()
        self.assertEqual(list((self.state / "opened-raw-archives").iterdir()), [])

    def test_body_hash_mismatch_is_rejected_before_import(self):
        self.keys_ready()
        def tamper(entries, _):
            name = next(name for name in entries if name.startswith("data/"))
            body = entries[name][1]
            entries[name][1] = bytes([body[0] ^ 1]) + body[1:]
        bad = self.rewrite(self.backup(), tamper)
        with patch("vault.raw_archive.import_account") as importer:
            with self.assertRaises(RawArchiveError) as error:
                open_raw_archive(bad, self.state)
            self.assertEqual(error.exception.code, "corrupt_archive")
            importer.assert_not_called()
        self.assert_clean()

    def test_unsafe_path_symlink_case_collision_and_parent_file_are_rejected(self):
        path = self.backup()
        def mutate(entries, manifest, kind):
            old = next(name for name in entries if name.startswith("data/"))
            item = manifest["files"][0]
            if kind == "symlink":
                entries[old][0].external_attr = (stat.S_IFLNK | 0o777) << 16
                return
            if kind == "traversal":
                info, body = entries.pop(old)
                info.filename = "data/../outside"
                entries[info.filename] = [info, body]
                item["path"] = "../outside"
                return
            new = item["path"].upper() if kind == "case" else item["path"] + "/child"
            body = b"synthetic collision"
            entries["data/" + new] = [zipfile.ZipInfo("data/" + new), body]
            manifest["files"].append({"path": new, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
            manifest["file_count"] += 1
            manifest["original_bytes"] += len(body)
        for kind in ("symlink", "traversal", "case", "parent"):
            with self.subTest(kind=kind):
                bad = self.rewrite(path, lambda entries, manifest: mutate(entries, manifest, kind))
                with self.assertRaises(RawArchiveError):
                    inspect_archive(bad)

    def test_original_signature_change_is_detected_before_extraction(self):
        path = self.backup()
        expected = inspect_archive(path)["signature"]
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
        with self.assertRaises(RawArchiveError) as error:
            open_raw_archive(path, self.state, expected_signature=expected)
        self.assertEqual(error.exception.code, "archive_changed")

    def test_mutation_during_copy_prevents_cache_publication(self):
        self.keys_ready()
        path = self.backup()
        changed = False
        def progress(_):
            nonlocal changed
            if not changed:
                changed = True
                with path.open("ab") as out:
                    out.write(b"changed")
        with self.assertRaises(RawArchiveError) as error:
            open_raw_archive(path, self.state, progress)
        self.assertEqual(error.exception.code, "archive_changed")
        self.assert_clean()
        self.assertEqual(list((self.state / "opened-raw-archives").iterdir()), [])

    def test_failed_import_cleans_private_keys_and_can_retry(self):
        self.keys_ready()
        path = self.backup()
        def fail(source, state, library, **_):
            self.assertEqual((state / "wechat-keys.json").stat().st_mode & 0o777, 0o600)
            library.import_file(b'{"messages":[{"text":"partial fixture"}]}', "partial.json")
            raise RuntimeError("Synthetic interruption")
        with patch("vault.raw_archive.import_account", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "Synthetic"):
                open_raw_archive(path, self.state)
        self.assert_clean()
        self.assertEqual(open_raw_archive(path, self.state).stats()["messages"], 1)

    def test_cache_database_corruption_is_rebuilt_and_zip_does_not_change(self):
        self.keys_ready()
        path = self.backup()
        original = path.read_bytes()
        first = open_raw_archive(path, self.state)
        first.path.write_bytes(b"damaged synthetic cache")
        second = open_raw_archive(path, self.state)
        self.assertFalse(second.raw_cache_hit)
        self.assertEqual(second.stats()["messages"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assert_clean()

    def test_direct_db_storage_and_parent_folder_layouts_are_supported(self):
        self.keys_ready()
        direct = self.backup(self.database)
        self.assertEqual(open_raw_archive(direct, self.state).stats()["messages"], 1)
        parent = self.root / "accounts"
        parent.mkdir()
        shutil.copytree(self.account, parent / self.account.name)
        nested = self.backup(parent)
        self.assertEqual(open_raw_archive(nested, self.state).stats()["messages"], 1)

    def test_multiple_accounts_are_not_guessed(self):
        self.keys_ready()
        parent = self.root / "accounts"
        for name in ("one", "two"):
            shutil.copytree(self.account, parent / name)
        path = self.backup(parent)
        self.assertEqual(inspect_archive(path)["accounts"], 2)
        with self.assertRaises(RawArchiveError) as error:
            open_raw_archive(path, self.state)
        self.assertEqual(error.exception.code, "multiple_accounts")

    @unittest.skipUnless(find_tool("ffmpeg") and find_tool("ffprobe"), "Synthetic media requires FFmpeg")
    def test_image_voice_video_avatar_survive_source_removal_and_readable_export(self):
        from PIL import Image
        picture = io.BytesIO()
        Image.new("RGB", (4, 4), (15, 80, 150)).save(picture, format="PNG")
        image = picture.getvalue()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x00\x00" * 2400)
        video = self.root / "fixture.mp4"
        subprocess.run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=32x32:r=10", "-t", "0.3", "-c:v", "mpeg4", str(video)],
                       check=True, capture_output=True)
        image_digest, video_digest = "a" * 32, "b" * 32
        image_path = self.account / "msg/attach" / hashlib.md5(CHAT.encode()).hexdigest() / "2026-06/Img" / (image_digest + ".dat")
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(image)
        video_path = self.account / "msg/video/2026-06" / (video_digest + ".mp4")
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(video.read_bytes())
        with closing(sqlite3.connect(self.plain["message/message_0.db"])) as db, db:
            for ident, kind in ((2, 3), (3, 34), (4, 43), (5, 3)):
                db.execute(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,1,?)',
                           (ident, 100 + ident, kind, STAMP, b"Synthetic media"))
        self.encrypt_database("message/message_0.db")
        def resource(db):
            db.executescript("CREATE TABLE ChatName2Id(user_name TEXT);"
                             "CREATE TABLE MessageResourceInfo(message_id INTEGER PRIMARY KEY,chat_id INTEGER,"
                             "message_local_type INTEGER,message_create_time INTEGER,message_local_id INTEGER,"
                             "message_svr_id INTEGER,packed_info BLOB);"
                             "CREATE TABLE MessageResourceDetail(message_id INTEGER,size INTEGER,status INTEGER,packed_info BLOB);")
            db.execute("INSERT INTO ChatName2Id VALUES(?)", (CHAT,))
            for ident, kind, digest, size in ((2, 3, image_digest, len(image)), (4, 43, video_digest, video.stat().st_size)):
                db.execute("INSERT INTO MessageResourceInfo VALUES(?,1,?,?,?,?,?)", (ident, kind, STAMP, ident, 100 + ident, digest.encode()))
                db.execute("INSERT INTO MessageResourceDetail VALUES(?,?,1,?)", (ident, size, b""))
        self.make_database("message/message_resource.db", resource)
        def voice(db):
            db.executescript("CREATE TABLE Name2Id(user_name TEXT); CREATE TABLE VoiceInfo("
                             "chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,svr_id INTEGER,voice_data BLOB);")
            db.execute("INSERT INTO Name2Id VALUES(?)", (CHAT,))
            db.execute("INSERT INTO VoiceInfo VALUES(1,?,3,103,?)", (STAMP, audio.getvalue()))
        self.make_database("message/media_0.db", voice)
        def avatar(db):
            db.execute("CREATE TABLE head_image(username TEXT,image_buffer BLOB)")
            db.execute("INSERT INTO head_image VALUES(?,?)", (CHAT, image))
        self.make_database("head_image/head_image.db", avatar)
        self.keys_ready()
        path = self.backup()
        shutil.rmtree(self.account)
        opened = open_raw_archive(path, self.state)
        self.assertEqual(opened.stats()["messages"], 5)
        conversation = opened.conversations()["conversations"][0]
        self.assertTrue(conversation["avatar_url"])
        messages = opened.messages(conversation["id"])["messages"]
        assets = [asset for item in messages for asset in item.get("media", [])]
        self.assertEqual({asset["kind"] for asset in assets if asset["status"] == "ready"}, {"image", "audio", "video"})
        self.assertEqual(sum(asset["status"] == "missing" for asset in assets), 1)
        expected = {}
        for asset in assets:
            if asset["status"] == "ready":
                with opened.media.open(asset["id"]) as (stream, info):
                    expected[asset["id"]] = stream.read()
                    self.assertEqual(hashlib.sha256(expected[asset["id"]]).hexdigest(), info["object_hash"])
        exported = create_archive(opened, self.root / "readable-output")
        self.assertEqual(exported["summary"]["messages"], 5)
        self.assertEqual(exported["summary"]["unavailable_media"], 1)
        path.unlink()
        shutil.rmtree(opened.state_dir)
        other_state = self.root / "other-reader"
        other_state.mkdir()
        independent = open_archive(exported["path"], other_state)
        for asset_id, body in expected.items():
            with independent.media.open(asset_id) as (stream, _):
                self.assertEqual(stream.read(), body)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
