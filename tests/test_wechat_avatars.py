"""Synthetic local avatar ingestion, identity, and source-integrity checks."""

from contextlib import closing
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vault.library import Library
from vault.wechat_avatars import MAX_AVATAR_BYTES, avatar_identity, import_avatars
from vault.wechat_reader import WeChatImportError, import_decrypted
import test_wechat_reader as reader_fixtures

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Avatar fixtures require Pillow")
class WeChatAvatarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.root = self.work / "decrypted"
        self.path = self.root / "head_image/head_image.db"
        self.path.parent.mkdir(parents=True)
        self.library = Library(self.work / "library")
        self.account = "synthetic-account"
        self.account_key = hashlib.sha256(self.account.encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE head_image(username TEXT,md5 TEXT,image_buffer BLOB,update_time INTEGER)")

    def picture(self, color="green"):
        output = io.BytesIO()
        Image.new("RGB", (48, 48), color).save(output, format="PNG")
        return output.getvalue()

    def add(self, username, data, updated=1):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO head_image VALUES(?,?,?,?)", (username, "not-a-content-hash", data, updated))

    def assets(self):
        with self.library.connect() as db:
            return dict(db.execute("SELECT identity_key,asset_id FROM avatar_assets"))

    def test_preserves_original_bytes_deduplicates_and_keeps_source_unchanged(self):
        body = self.picture()
        self.add("wxid_synthetic_alice", body)
        self.add("wxid_synthetic_bob", body)
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        result = import_avatars(self.root, self.library, self.account)
        self.assertEqual(result, {"imported": 2, "unavailable": 0, "unsupported": 0, "status": "done"})
        assets = self.assets()
        self.assertEqual(len(assets), 2)
        self.assertEqual(len(set(assets.values())), 1)
        with self.library.media.open(next(iter(assets.values()))) as (stream, _):
            self.assertEqual(stream.read(), body)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertFalse(list(self.path.parent.glob("*-wal")))
        self.assertFalse(list(self.library.state_dir.glob(".avatars-*")))
        self.assertEqual(import_avatars(self.root, self.library, self.account)["imported"], 2)
        self.assertEqual(self.assets(), assets)

    def test_latest_valid_avatar_and_account_identity_are_independent(self):
        self.add("wxid_synthetic_person", self.picture("red"), updated=20)
        self.add("wxid_synthetic_person", self.picture("blue"), updated=10)
        import_avatars(self.root, self.library, self.account)
        first = self.assets()
        with self.library.media.open(next(iter(first.values()))) as (stream, _):
            self.assertEqual(stream.read(), self.picture("red"))
        import_avatars(self.root, self.library, "another-synthetic-account")
        self.assertEqual(len(self.assets()), 2)
        self.assertNotEqual(avatar_identity(self.account_key, "wxid_synthetic_person"),
                            avatar_identity(self.account_key, "wxid_synthetic_another"))

    def test_missing_corrupt_oversized_and_animated_pictures_do_not_become_avatars(self):
        self.add("wxid_synthetic_missing", None)
        self.add("wxid_synthetic_corrupt", b"synthetic-not-an-image")
        self.add("wxid_synthetic_oversized", b"x" * (MAX_AVATAR_BYTES + 1))
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(output, format="GIF", save_all=True,
                                                 append_images=[Image.new("RGB", (8, 8), "blue")])
        self.add("wxid_synthetic_animated", output.getvalue())
        result = import_avatars(self.root, self.library, self.account)
        self.assertEqual((result["imported"], result["unavailable"], result["unsupported"]), (0, 2, 2))
        self.assertFalse(self.assets())

    def test_source_change_never_publishes_avatar_identity_links(self):
        self.add("wxid_synthetic_person", self.picture())
        from vault.wechat_reader import _file_state
        original = _file_state(self.path)
        changed = (*original[:-1], original[-1] + 1)
        with patch("vault.wechat_reader._file_state", side_effect=[original, original, changed]):
            with self.assertRaisesRegex(ValueError, "changed"):
                import_avatars(self.root, self.library, self.account)
        self.assertFalse(self.assets())
        self.assertFalse(list(self.library.state_dir.glob(".avatars-*")))

    def test_dimension_and_animation_limits_are_checked_before_full_decode(self):
        large = io.BytesIO()
        Image.new("RGB", (2001, 2000), "green").save(large, format="PNG")
        self.add("wxid_synthetic_large_dimensions", large.getvalue())
        animated = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(animated, format="GIF", save_all=True,
                                                 append_images=[Image.new("RGB", (8, 8), "blue")])
        self.add("wxid_synthetic_multiple_frames", animated.getvalue())
        with patch("vault.media_codecs._image_info") as decoder:
            result = import_avatars(self.root, self.library, self.account)
        decoder.assert_not_called()
        self.assertEqual(result["unsupported"], 2)
        self.assertEqual(result["imported"], 0)

    def test_pending_journal_and_symlink_are_rejected(self):
        self.path.with_name(self.path.name + "-wal").write_bytes(b"synthetic WAL")
        with self.assertRaises(WeChatImportError):
            import_avatars(self.root, self.library, self.account)
        self.path.with_name(self.path.name + "-wal").unlink()
        original = self.path.with_suffix(".original")
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            import_avatars(self.root, self.library, self.account)
        self.assertFalse(self.assets())

    def test_absent_optional_database_and_invalid_identity(self):
        self.path.unlink()
        self.assertEqual(import_avatars(self.root, self.library, self.account)["status"], "unavailable")
        for account_key, username in (("bad", "person"), (self.account_key, ""), (self.account_key, "x" * 4097)):
            with self.assertRaises(ValueError):
                avatar_identity(account_key, username)


class WeChatAvatarIdentityTests(unittest.TestCase):
    def test_existing_message_identity_is_backfilled_and_updated_by_username(self):
        fixture = reader_fixtures.WeChatReaderTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        source = fixture.create_messages()
        import_decrypted(fixture.root, fixture.library, account_id="synthetic-stable-account")
        account_key = hashlib.sha256(b"synthetic-stable-account").hexdigest()
        with fixture.library.connect() as db:
            message_id = db.execute("SELECT id FROM messages").fetchone()[0]
            self.assertEqual(db.execute("SELECT identity_key FROM message_avatars WHERE message_id=?", (message_id,)).fetchone()[0],
                             avatar_identity(account_key, fixture.alice))
            self.assertEqual(db.execute("SELECT identity_key FROM conversation_avatars").fetchone()[0],
                             avatar_identity(account_key, fixture.alice))
            db.execute("DELETE FROM message_avatars")
            db.execute("DELETE FROM conversation_avatars")
        result = import_decrypted(fixture.root, fixture.library, account_id="synthetic-stable-account")
        self.assertEqual(result["messages"], 0)
        with fixture.library.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message_avatars").fetchone()[0], 1)
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("UPDATE Name2Id SET user_name=? WHERE rowid=3", (fixture.me,))
        import_decrypted(fixture.root, fixture.library, account_id="synthetic-stable-account")
        with fixture.library.connect() as db:
            self.assertEqual(db.execute("SELECT identity_key FROM message_avatars WHERE message_id=?", (message_id,)).fetchone()[0],
                             avatar_identity(account_key, fixture.me))


if __name__ == "__main__":
    unittest.main()
