"""Verify media association and retry isolation with disposable source identities."""

from contextlib import closing
import errno
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from vault.library import Library
from vault.media_codecs import MediaCodecError
from vault.media_import import MPO_DETAIL, THUMBNAIL_DETAIL, import_media
from vault.wechat_reader import import_decrypted


def table_for(username):
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


class SyntheticLocator:
    def __init__(self, references, outcomes):
        self.references = references
        self.outcomes = outcomes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_media_messages(self):
        yield from self.references

    def locate(self, reference):
        identity = (reference["database_name"], reference["table_name"].lower(), str(reference["local_id"]))
        self.calls.append(identity)
        outcome = self.outcomes[identity]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, bytes):
            return {"status": "ready", "candidates": [{"voice_data": outcome}]}
        if isinstance(outcome, dict):
            return outcome
        return {"status": outcome, "candidates": []}


class MediaImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-media-import-test-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name).resolve()
        self.root = self.work / "decrypted"
        (self.root / "message").mkdir(parents=True)
        self.account = self.work / "synthetic-account"
        self.account.mkdir()
        self.library = Library(self.work / "library")
        self.account_id = "synthetic-account-A"
        self.references = []
        self.outcomes = {}
        for database, usernames in (("message_0.db", ["synthetic-alice", "synthetic-bob"]),
                                    ("message_1.db", ["synthetic-alice"])):
            with closing(sqlite3.connect(self.root / "message" / database)) as db, db:
                db.execute("CREATE TABLE Name2Id(user_name TEXT)")
                for username in usernames:
                    db.execute("INSERT INTO Name2Id(user_name) VALUES(?)", (username,))
                    table = table_for(username)
                    db.execute(f'''CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER,
                        local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, message_content BLOB)''')
                    db.execute(f'INSERT INTO "{table}" VALUES(1,1001,3,1791612000,1,?)', (b"Synthetic image placeholder",))
                    reference = {"database_name": database, "table_name": table,
                                 "local_id": "1", "kind": "image"}
                    self.references.append(reference)
                    identity = self.identity(reference)
                    self.outcomes[identity] = (database + ":" + username).encode()
        import_decrypted(self.root, self.library, account_id=self.account_id)

    @staticmethod
    def identity(reference):
        return (reference["database_name"], reference["table_name"].lower(), str(reference["local_id"]))

    def message_id(self, reference, account_id=None):
        key = hashlib.sha256((account_id or self.account_id).encode()).hexdigest()
        with self.library.connect() as db:
            return db.execute("""SELECT message_id FROM wechat_source_messages
                WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?""",
                (key, *self.identity(reference))).fetchone()[0]

    def associated(self, reference, account_id=None):
        message_id = self.message_id(reference, account_id)
        return self.library.media.for_messages([message_id]).get(message_id, [])

    @staticmethod
    def decode(source, output, **_):
        payload = Path(source).read_bytes()
        if payload == b"locked":
            raise MediaCodecError("image_key_missing", "Synthetic image requires a key")
        if payload == b"unsupported":
            raise MediaCodecError("unsupported_format", "Synthetic unsupported format")
        if payload == b"disk-full":
            raise OSError(errno.ENOSPC, "Synthetic destination is full")
        if payload == b"codec-space":
            raise MediaCodecError("insufficient_space", "Synthetic conversion space failure")
        if payload.startswith(b"slow:"):
            time.sleep(.03)
        Path(output).write_bytes(payload)
        if payload == b"bmp":
            return {"mime_type": "image/bmp", "format": "bmp"}
        return {"mime_type": "image/png", "format": "png"}

    def change_source(self, reference, to_text=False):
        with closing(sqlite3.connect(self.root / "message" / reference["database_name"])) as db, db:
            db.execute(f'UPDATE "{reference["table_name"]}" SET server_id=server_id+1' +
                       (",local_type=1,message_content='Updated synthetic text'" if to_text else ""))
        import_decrypted(self.root, self.library, account_id=self.account_id)

    def mark_thumbnail(self, reference):
        asset = self.associated(reference)[0]["id"]
        with self.library.connect() as db:
            db.execute("UPDATE media_assets SET detail=? WHERE id=?", (THUMBNAIL_DETAIL, asset))
        return asset

    def run_import(self, references=None, outcomes=None, progress=None):
        locator = SyntheticLocator(self.references if references is None else references,
                                   self.outcomes if outcomes is None else outcomes)
        with patch("vault.media_import.MediaLocator", return_value=locator), \
                patch("vault.media_import.discover_image_keys", return_value=[]), \
                patch("vault.media_import.decode_image", side_effect=self.decode) as decoder:
            result = import_media(self.root, self.account, self.library, self.account_id, progress)
        return result, locator, decoder

    def test_accounts_shards_and_conversations_cannot_share_local_message_identity(self):
        other_account = "synthetic-account-B"
        import_decrypted(self.root, self.library, account_id=other_account)
        result, _, _ = self.run_import()
        self.assertEqual(result["ready"], 3)
        assets = set()
        for reference in self.references:
            media = self.associated(reference)
            self.assertEqual(len(media), 1)
            assets.add(media[0]["id"])
            with self.library.media.open(media[0]["id"]) as (stream, _):
                self.assertEqual(stream.read(), self.outcomes[self.identity(reference)])
            self.assertEqual(self.associated(reference, other_account), [])
        self.assertEqual(len(assets), 3)

    def test_unknown_source_message_is_ignored_instead_of_linking_another_record(self):
        unknown = {**self.references[0], "local_id": "999"}
        result, locator, decoder = self.run_import(references=[unknown])
        self.assertEqual(result["processed"], 0)
        self.assertEqual(locator.calls, [])
        decoder.assert_not_called()
        self.assertEqual(self.library.media.stats()["unique_objects"], 0)

    def test_repeat_import_skips_ready_links_without_reading_or_decoding_again(self):
        self.run_import()
        before = {self.message_id(ref): self.associated(ref)[0]["id"] for ref in self.references}
        result, locator, decoder = self.run_import(outcomes={})
        self.assertEqual((result["ready"], result["skipped"], result["processed"]), (0, 3, 3))
        self.assertEqual(locator.calls, [])
        decoder.assert_not_called()
        self.assertEqual(before, {self.message_id(ref): self.associated(ref)[0]["id"] for ref in self.references})
        self.assertEqual(self.library.media.stats()["unique_objects"], 3)

    def test_missing_media_is_retried_while_existing_ready_links_are_retained(self):
        initial = dict(self.outcomes)
        initial[self.identity(self.references[0])] = "missing"
        self.assertEqual(self.run_import(outcomes=initial)[0]["missing"], 1)
        second, locator, decoder = self.run_import()
        self.assertEqual((second["ready"], second["skipped"]), (1, 2))
        self.assertEqual(locator.calls, [self.identity(self.references[0])])
        self.assertEqual(decoder.call_count, 1)
        self.assertEqual(self.associated(self.references[0])[0]["status"], "ready")

    def test_mpo_import_keeps_original_file_and_explains_first_picture_display(self):
        stream = io.BytesIO()
        Image.new("RGB", (32, 24), "blue").save(
            stream, format="MPO", save_all=True, append_images=[Image.new("RGB", (16, 12), "red")])
        raw = stream.getvalue()
        reference = self.references[0]
        locator = SyntheticLocator([reference], {self.identity(reference): raw})
        with patch("vault.media_import.MediaLocator", return_value=locator), \
                patch("vault.media_import.discover_image_keys", return_value=[]):
            result = import_media(self.root, self.account, self.library, self.account_id)
        self.assertEqual((result["ready"], result["processed"]), (1, 1))
        media = self.associated(reference)[0]
        self.assertEqual(media["mime_type"], "image/jpeg")
        self.assertEqual(media["filename"], "image.jpg")
        self.assertEqual(media["detail"], MPO_DETAIL)
        with self.library.media.open(media["id"]) as (stored, _):
            self.assertEqual(stored.read(), raw)
        self.assertEqual(self.library.media.stats()["unique_objects"], 1)

    def test_parallel_completion_and_codec_failure_do_not_swap_message_media(self):
        outcomes = dict(self.outcomes)
        outcomes[self.identity(self.references[0])] = b"slow:first-message-private-bytes"
        outcomes[self.identity(self.references[1])] = b"locked"
        outcomes[self.identity(self.references[2])] = b"third-message-private-bytes"
        reports = []
        result, _, _ = self.run_import(outcomes=outcomes, progress=reports.append)
        self.assertEqual((result["ready"], result["locked"], result["processed"]), (2, 1, 3))
        for index, reference in enumerate(self.references):
            media = self.associated(reference)[0]
            if index == 1:
                self.assertEqual(media["status"], "locked")
                self.assertNotIn("url", media)
            else:
                with self.library.media.open(media["id"]) as (stream, _):
                    self.assertEqual(stream.read(), outcomes[self.identity(reference)])
        self.assertEqual(reports[-1]["done_files"], 3)
        self.assertNotIn("private-bytes", json.dumps(reports))
        self.assertEqual(list(self.library.state_dir.glob("media-import-*")), [])

    def test_location_failures_are_local_to_the_affected_messages(self):
        outcomes = dict(self.outcomes)
        outcomes[self.identity(self.references[0])] = PermissionError("Synthetic denied file")
        outcomes[self.identity(self.references[1])] = "ambiguous"
        result, _, _ = self.run_import(outcomes=outcomes)
        self.assertEqual((result["ready"], result["error"], result["unsupported"]), (1, 1, 1))
        self.assertEqual([self.associated(ref)[0]["status"] for ref in self.references],
                         ["error", "unsupported", "ready"])

    def test_batch_storage_failure_rolls_back_links_without_cross_linking_records(self):
        original_store = self.library.media.store_file
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Synthetic disk full")
            return original_store(*args, **kwargs)

        with patch.object(self.library.media, "store_file", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "Synthetic disk full"):
                self.run_import()
        for reference in self.references:
            self.assertEqual(self.associated(reference), [])
        with self.library.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_import_revisions").fetchone()[0], 0)
        self.assertEqual(list(self.library.state_dir.glob("media-import-*")), [])

    def test_updated_source_revision_replaces_prior_ready_media(self):
        self.run_import()
        reference = self.references[0]
        old = self.associated(reference)[0]["id"]
        self.change_source(reference)
        self.assertEqual(self.associated(reference), [])
        outcomes = dict(self.outcomes)
        outcomes[self.identity(reference)] = b"new-revision-image"
        result, locator, _ = self.run_import(outcomes=outcomes)
        self.assertEqual((result["ready"], result["skipped"]), (1, 2))
        self.assertEqual(locator.calls, [self.identity(reference)])
        self.assertNotEqual(old, self.associated(reference)[0]["id"])
        with self.library.media.open(self.associated(reference)[0]["id"]) as (stream, _):
            self.assertEqual(stream.read(), b"new-revision-image")

    def test_changed_revision_failure_cannot_keep_stale_ready_media(self):
        self.run_import()
        reference = self.references[0]
        self.change_source(reference)
        outcomes = dict(self.outcomes)
        outcomes[self.identity(reference)] = "missing"
        result, _, _ = self.run_import(outcomes=outcomes)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(self.associated(reference)[0]["status"], "missing")

    def test_media_changed_to_text_removes_old_link_without_a_media_ref(self):
        self.run_import()
        reference = self.references[0]
        self.change_source(reference, to_text=True)
        self.run_import(references=self.references[1:])
        self.assertEqual(self.associated(reference), [])
        self.assertEqual(self.associated(self.references[1])[0]["status"], "ready")

    def test_unversioned_ready_media_is_verified_again_once(self):
        self.run_import()
        reference = self.references[0]
        with self.library.connect() as db:
            db.execute("DELETE FROM media_import_revisions WHERE message_id=?", (self.message_id(reference),))
        result, locator, _ = self.run_import()
        self.assertEqual((result["ready"], result["skipped"]), (1, 2))
        self.assertEqual(locator.calls, [self.identity(reference)])
        self.assertEqual(self.run_import()[0]["skipped"], 3)

    def test_thumbnail_upgrade_retries_same_revision_and_replaces_preview(self):
        self.run_import()
        reference = self.references[0]
        old = self.mark_thumbnail(reference)
        outcomes = dict(self.outcomes)
        outcomes[self.identity(reference)] = b"full-resolution-image"
        result, locator, _ = self.run_import(outcomes=outcomes)
        self.assertEqual((result["ready"], result["skipped"]), (1, 2))
        self.assertEqual(locator.calls, [self.identity(reference)])
        self.assertNotEqual(old, self.associated(reference)[0]["id"])
        self.assertEqual(self.associated(reference)[0]["detail"], "")

    def test_failed_thumbnail_upgrade_retains_existing_same_revision_preview(self):
        self.run_import()
        reference = self.references[0]
        old = self.mark_thumbnail(reference)
        for unavailable in ("missing", b"locked", b"unsupported"):
            outcomes = dict(self.outcomes)
            outcomes[self.identity(reference)] = unavailable
            result, _, _ = self.run_import(outcomes=outcomes)
            self.assertEqual(result["skipped"], 3)
            self.assertEqual(self.associated(reference)[0]["id"], old)
            self.assertEqual(self.associated(reference)[0]["status"], "ready")

    def test_existing_thumbnail_is_not_decoded_again_without_full_candidates(self):
        self.run_import()
        reference = self.references[0]
        old = self.mark_thumbnail(reference)
        outcomes = {self.identity(reference): {"status": "ready", "candidates": [
            {"voice_data": b"thumbnail-is-still-local", "thumbnail": True}]}}
        result, locator, decoder = self.run_import(outcomes=outcomes)
        self.assertEqual((result["ready"], result["skipped"]), (0, 3))
        self.assertEqual(locator.calls, [self.identity(reference)])
        decoder.assert_not_called()
        self.assertEqual(self.associated(reference)[0]["id"], old)

    def test_changed_revision_thumbnail_cannot_skip_revalidation(self):
        self.run_import()
        reference = self.references[0]
        old = self.mark_thumbnail(reference)
        self.change_source(reference)
        outcomes = {self.identity(reference): {"status": "ready", "candidates": [
            {"voice_data": b"new-revision-thumbnail", "thumbnail": True}]}}
        result, _, decoder = self.run_import(outcomes=outcomes)
        self.assertEqual((result["ready"], result["skipped"]), (1, 2))
        decoder.assert_called_once()
        self.assertNotEqual(self.associated(reference)[0]["id"], old)

    def test_wxgf_companion_is_saved_from_one_validated_decode(self):
        decoder = self.decode
        payload = self.outcomes[self.identity(self.references[0])]

        def decode_with_original(source, output, original_destination=None, **kwargs):
            if Path(source).read_bytes() == payload:
                self.assertIsNotNone(original_destination)
                Path(output).write_bytes(b"Synthetic PNG preview")
                Path(original_destination).write_bytes(b"wxgf Synthetic original container")
                return {"mime_type": "image/png", "format": "png", "original_format": "wxgf",
                        "original_saved": True}
            return decoder(source, output, **kwargs)

        with patch.object(self, "decode", side_effect=decode_with_original):
            result, _, calls = self.run_import()
        self.assertEqual(result["ready"], 3)
        self.assertEqual(calls.call_count, 3)
        with self.library.connect() as db:
            original = db.execute("SELECT id FROM media_assets WHERE kind='original'").fetchone()
        self.assertIsNotNone(original)
        with self.library.media.open(original[0]) as (stream, _):
            self.assertEqual(stream.read(), b"wxgf Synthetic original container")

    def test_unsupported_output_mime_does_not_abort_other_messages(self):
        outcomes = dict(self.outcomes)
        outcomes[self.identity(self.references[0])] = b"bmp"
        result, _, _ = self.run_import(outcomes=outcomes)
        self.assertEqual((result["unsupported"], result["ready"]), (1, 2))
        self.assertEqual(self.associated(self.references[0])[0]["status"], "unsupported")

    def test_disk_full_and_codec_space_failure_abort_without_false_statuses(self):
        for payload, error in ((b"disk-full", OSError), (b"codec-space", MediaCodecError)):
            outcomes = dict(self.outcomes)
            outcomes[self.identity(self.references[0])] = payload
            with self.assertRaises(error):
                self.run_import(outcomes=outcomes)
            self.assertTrue(all(not self.associated(ref) for ref in self.references))
            self.assertEqual(list(self.library.state_dir.glob("media-import-*")), [])

    def test_decoder_wait_does_not_hold_database_writer_lock(self):
        decoder = self.decode

        def decode_with_write_probe(*args, **kwargs):
            with closing(sqlite3.connect(self.library.path, timeout=.2)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            return decoder(*args, **kwargs)

        with patch.object(self, "decode", side_effect=decode_with_write_probe):
            self.assertEqual(self.run_import()[0]["ready"], 3)

    def test_byte_bounded_batches_checkpoint_and_retry_after_failure(self):
        original_store = self.library.media.store_file
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.ENOSPC, "Synthetic storage failure")
            return original_store(*args, **kwargs)

        with patch("vault.media_import.MAX_PENDING_INPUT_BYTES", 1), \
                patch.object(self.library.media, "store_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.run_import()
        self.assertEqual(self.associated(self.references[0])[0]["status"], "ready")
        self.assertEqual(self.associated(self.references[1]), [])
        result, _, _ = self.run_import()
        self.assertEqual((result["ready"], result["skipped"]), (2, 1))

    def test_next_batch_decodes_while_previous_batch_is_stored(self):
        original_decode, original_store = self.decode, self.library.media.store_file
        second_started, first_storing = threading.Event(), threading.Event()
        second_payload = self.outcomes[self.identity(self.references[1])]
        stored = 0

        def decode_waiting_for_storage(source, output, **kwargs):
            if Path(source).read_bytes() == second_payload:
                second_started.set()
                if not first_storing.wait(2):
                    raise AssertionError("The next batch was not prefetched during storage")
            return original_decode(source, output, **kwargs)

        def store_with_overlap(*args, **kwargs):
            nonlocal stored
            stored += 1
            if stored == 1:
                self.assertTrue(second_started.wait(2))
                first_storing.set()
            return original_store(*args, **kwargs)

        with patch("vault.media_import.MAX_PENDING_ITEMS", 1), \
                patch.object(self, "decode", side_effect=decode_waiting_for_storage), \
                patch.object(self.library.media, "store_file", side_effect=store_with_overlap):
            self.assertEqual(self.run_import()[0]["ready"], 3)
        self.assertTrue(first_storing.is_set())

    def test_two_batches_share_the_input_byte_budget(self):
        original_decode, original_store = self.decode, self.library.media.store_file
        second_started = threading.Event()
        second_payload = self.outcomes[self.identity(self.references[1])]
        stored = 0

        def track_decode(source, output, **kwargs):
            if Path(source).read_bytes() == second_payload:
                second_started.set()
            return original_decode(source, output, **kwargs)

        def store_before_next_decode(*args, **kwargs):
            nonlocal stored
            stored += 1
            if stored == 1:
                self.assertFalse(second_started.wait(.05))
            return original_store(*args, **kwargs)

        limit = max(len(value) for value in self.outcomes.values())
        with patch("vault.media_import.MAX_PENDING_ITEMS", 1), \
                patch("vault.media_import.MAX_PENDING_INPUT_BYTES", limit), \
                patch.object(self, "decode", side_effect=track_decode), \
                patch.object(self.library.media, "store_file", side_effect=store_before_next_decode):
            self.assertEqual(self.run_import()[0]["ready"], 3)
        self.assertTrue(second_started.is_set())

    def test_prefetched_conversion_is_cleaned_after_storage_failure(self):
        original_store = self.library.media.store_file
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.ENOSPC, "Synthetic storage failure")
            return original_store(*args, **kwargs)

        with patch("vault.media_import.MAX_PENDING_ITEMS", 1), \
                patch.object(self.library.media, "store_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.run_import()
        self.assertEqual(self.associated(self.references[0])[0]["status"], "ready")
        self.assertEqual(self.associated(self.references[1]), [])
        self.assertEqual(self.associated(self.references[2]), [])
        self.assertEqual(list(self.library.state_dir.glob("media-import-*")), [])
        result, _, _ = self.run_import()
        self.assertEqual((result["ready"], result["skipped"]), (2, 1))

    def test_revision_change_during_conversion_rolls_back_new_associations(self):
        decoder = self.decode
        target = self.message_id(self.references[0])

        def change_revision(*args, **kwargs):
            with self.library.connect() as db:
                db.execute("UPDATE wechat_source_messages SET source_digest=? WHERE message_id=?", ("a" * 64, target))
            return decoder(*args, **kwargs)

        with patch.object(self, "decode", side_effect=change_revision):
            with self.assertRaisesRegex(ValueError, "source message changed"):
                self.run_import()
        self.assertTrue(all(not self.associated(ref) for ref in self.references))

    def test_readable_archive_preserves_revision_guard_after_source_update(self):
        from vault.readable_archive import create_archive, open_archive
        self.run_import()
        reference = self.references[0]
        self.change_source(reference)
        archive = create_archive(self.library, self.work / "archives")
        (self.work / "reader").mkdir()
        opened = open_archive(archive["path"], self.work / "reader")
        self.assertEqual(opened.media.for_messages([self.message_id(reference)]), {})
        self.assertEqual(opened.media.for_messages([self.message_id(self.references[1])])[
            self.message_id(self.references[1])][0]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
