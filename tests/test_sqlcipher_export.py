"""Exercise real SQLCipher WAL recovery on closed synthetic file copies."""

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import secrets
import selectors
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import vault.sqlcipher_export as exporter
from vault.sqlcipher_export import SQLCipherExportError, export_database


SQLCIPHER = Path("/opt/homebrew/bin/sqlcipher")


def create_wal_fixture(directory, key_hex):
    active = directory / "active.db"
    create_sql = (f'''PRAGMA key="x'{key_hex}'";\n'''
                  "PRAGMA cipher_compatibility=4;\n"
                  "CREATE TABLE records(id INTEGER PRIMARY KEY, text TEXT);\n"
                  "INSERT INTO records VALUES(1, 'Before WAL');\n")
    result = subprocess.run([str(SQLCIPHER), "-batch", "-bail", "-init", os.devnull, str(active)],
                            input=create_sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AssertionError("Could not construct the encrypted SQLCipher fixture.")
    command = [str(SQLCIPHER), "-batch", "-bail", "-init", os.devnull, str(active)]
    writer = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              bufsize=0)
    try:
        sql = (f'''PRAGMA key="x'{key_hex}'";\n'''
               "PRAGMA cipher_compatibility=4;\n"
               "PRAGMA journal_mode=WAL;\nPRAGMA wal_autocheckpoint=0;\n"
               "PRAGMA synchronous=FULL;\n"
               "INSERT INTO records VALUES(2, 'Committed in WAL');\n"
               ".print WAL_FIXTURE_READY\n")
        writer.stdin.write(sql.encode())
        writer.stdin.flush()
        output = b""
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as selector:
            selector.register(writer.stdout, selectors.EVENT_READ)
            while b"WAL_FIXTURE_READY" not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AssertionError("The SQLCipher WAL fixture did not become ready.")
                chunk = os.read(writer.stdout.fileno(), 4096)
                if not chunk:
                    raise AssertionError("The SQLCipher fixture writer exited before its commit marker.")
                output += chunk
        if not active.with_name(active.name + "-wal").stat().st_size:
            raise AssertionError("The fixture did not retain a committed WAL.")
        frozen = directory / "closed"
        frozen.mkdir()
        source = frozen / "database.db"
        for suffix in ("", "-wal", "-shm"):
            path = active.with_name(active.name + suffix)
            if path.exists():
                target = source.with_name(source.name + suffix)
                shutil.copyfile(path, target)
                target.chmod(0o400)
        # The copied source has no open database handle. The live writer is
        # closed only after its durable, idle state has been copied completely.
        return source
    finally:
        if writer.poll() is None:
            try:
                writer.communicate(b".quit\n", timeout=5)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.communicate(timeout=5)


@unittest.skipUnless(SQLCIPHER.is_file(), "A local SQLCipher executable is required")
class SQLCipherExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.key = secrets.token_hex(32)
        self.source = create_wal_fixture(self.root, self.key)
        self.destination = self.root / "exports" / "readable.db"

    def fingerprints(self):
        return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.source.parent.iterdir()}

    def assert_no_output(self):
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob(".shiliao-sqlcipher-*")), [])

    def test_export_reads_committed_wal_and_never_changes_sources(self):
        before = self.fingerprints()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = export_database(self.source, self.destination, self.key)
        self.assertTrue(result["included_wal"])
        self.assertEqual(result["file_count"], 1)
        with closing(sqlite3.connect(self.destination.as_uri() + "?mode=ro", uri=True)) as database:
            self.assertEqual(database.execute("SELECT * FROM records ORDER BY id").fetchall(),
                             [(1, "Before WAL"), (2, "Committed in WAL")])
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.fingerprints(), before)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(list(self.destination.parent.glob(".shiliao-sqlcipher-*")), [])
        main_only = self.root / "main-only.db"
        main_only.write_bytes(self.source.read_bytes())
        main_export = self.destination.parent / "without-wal.db"
        self.assertFalse(export_database(main_only, main_export, self.key)["included_wal"])
        with closing(sqlite3.connect(main_export.as_uri() + "?mode=ro", uri=True)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)

    def test_authenticated_wal_page_corruption_is_rejected(self):
        wal_path = self.source.with_name(self.source.name + "-wal")
        wal = bytearray(wal_path.read_bytes())
        page_size = struct.unpack(">I", wal[8:12])[0]
        frame_size = 24 + page_size
        self.assertEqual((len(wal) - 32) % frame_size, 0)
        # Preserve the SQLite WAL checksums so recovery reaches the deliberately
        # corrupted encrypted page and SQLCipher must validate its page HMAC.
        wal[32 + 24 + 100] ^= 1
        endian = "<" if struct.unpack(">I", wal[:4])[0] == 0x377F0682 else ">"
        first, second = struct.unpack(">II", wal[24:32])
        for offset in range(32, len(wal), frame_size):
            authenticated = wal[offset:offset + 8] + wal[offset + 24:offset + frame_size]
            words = struct.unpack(endian + "I" * (len(authenticated) // 4), authenticated)
            for index in range(0, len(words), 2):
                first = (first + words[index] + second) & 0xFFFFFFFF
                second = (second + words[index + 1] + first) & 0xFFFFFFFF
            wal[offset + 16:offset + 24] = struct.pack(">II", first, second)
        wal_path.chmod(0o600)
        wal_path.write_bytes(wal)
        with self.assertRaises(SQLCipherExportError):
            export_database(self.source, self.destination, self.key)
        self.assert_no_output()

    def test_wrong_key_fails_without_key_sql_or_plaintext_leaks(self):
        wrong_key = secrets.token_hex(32)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SQLCipherExportError) as error:
                export_database(self.source, self.destination, wrong_key)
        self.assertEqual(error.exception.code, "key_or_database_invalid")
        self.assertNotIn(wrong_key, str(error.exception))
        self.assertNotIn("PRAGMA", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertEqual(output.getvalue(), "")
        self.assert_no_output()

    def test_key_is_only_sent_on_stdin_and_process_receives_only_copy(self):
        actual_run = subprocess.run
        calls = []

        def inspect_run(arguments, **kwargs):
            self.assertNotIn(self.key, repr(arguments))
            self.assertNotIn(str(self.source), repr(arguments))
            self.assertIn(self.key, kwargs["input"])
            self.assertEqual(Path(kwargs["cwd"]).stat().st_mode & 0o777, 0o700)
            for file in Path(kwargs["cwd"]).iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            calls.append(True)
            return actual_run(arguments, **kwargs)

        with patch.object(exporter.subprocess, "run", side_effect=inspect_run):
            export_database(self.source, self.destination, self.key)
        self.assertEqual(calls, [True])

    def test_source_mutation_after_copy_prevents_publication(self):
        actual_export = exporter._run_export

        def mutate(*args):
            actual_export(*args)
            wal = self.source.with_name(self.source.name + "-wal")
            wal.chmod(0o600)
            with wal.open("ab") as stream:
                stream.write(b"changed")

        with patch.object(exporter, "_run_export", side_effect=mutate):
            with self.assertRaises(SQLCipherExportError) as error:
                export_database(self.source, self.destination, self.key)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_output()

    def test_sidecar_disappearance_is_detected(self):
        actual_export = exporter._run_export

        def remove_wal(*args):
            actual_export(*args)
            self.source.with_name(self.source.name + "-wal").unlink()

        with patch.object(exporter, "_run_export", side_effect=remove_wal):
            with self.assertRaises(SQLCipherExportError) as error:
                export_database(self.source, self.destination, self.key)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_output()

    def test_nonempty_rollback_journal_is_explicitly_rejected(self):
        self.source.with_name(self.source.name + "-journal").write_bytes(b"pending rollback")
        with self.assertRaises(SQLCipherExportError) as error:
            export_database(self.source, self.destination, self.key)
        self.assertEqual(error.exception.code, "pending_journal")
        self.assert_no_output()

    def test_source_and_sidecar_symlinks_are_rejected(self):
        link = self.root / "linked.db"
        link.symlink_to(self.source)
        with self.assertRaises(SQLCipherExportError) as error:
            export_database(link, self.destination, self.key)
        self.assertEqual(error.exception.code, "unsafe_source")
        wal = self.source.with_name(self.source.name + "-wal")
        wal.rename(self.root / "real-wal")
        wal.symlink_to(self.root / "real-wal")
        with self.assertRaises(SQLCipherExportError) as error:
            export_database(self.source, self.destination, self.key)
        self.assertEqual(error.exception.code, "unsafe_source")
        self.assert_no_output()

    def test_existing_destination_is_never_overwritten(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"Keep existing file")
        with self.assertRaises(SQLCipherExportError) as error:
            export_database(self.source, self.destination, self.key)
        self.assertEqual(error.exception.code, "destination_exists")
        self.assertEqual(self.destination.read_bytes(), b"Keep existing file")
        self.assertEqual(list(self.destination.parent.glob(".shiliao-sqlcipher-*")), [])

    def test_missing_source_sidecar_cannot_be_created_as_destination(self):
        journal = self.source.with_name(self.source.name + "-journal")
        before = self.fingerprints()
        for target in (journal, journal / "readable.db"):
            with self.subTest(nested=target != journal), self.assertRaises(SQLCipherExportError) as error:
                export_database(self.source, target, self.key)
            self.assertEqual(error.exception.code, "unsafe_destination")
        self.assertFalse(journal.exists())
        self.assertEqual(self.fingerprints(), before)

    def test_invalid_key_rejected_before_a_child_process_is_started(self):
        for key in ("", "a" * 63, "not-a-key", "'; SELECT 'unsafe'; --"):
            with self.subTest(key_length=len(key)), patch.object(exporter.subprocess, "run") as command:
                with self.assertRaises(SQLCipherExportError) as error:
                    export_database(self.source, self.destination, key)
                self.assertEqual(error.exception.code, "invalid_key")
                command.assert_not_called()
        self.assert_no_output()

    def test_timeout_diagnostics_are_sanitized_and_staging_is_removed(self):
        with patch.object(exporter.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "private process", 1, output="sensitive SQL and " + self.key)):
            with self.assertRaises(SQLCipherExportError) as error:
                export_database(self.source, self.destination, self.key)
        self.assertNotIn(self.key, str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()
