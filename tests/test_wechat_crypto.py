"""Round-trip independently encrypted synthetic SQLite databases."""

import ctypes
import ctypes.util
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None

from vault.wechat_crypto import WeChatCryptoError, decrypt_database, verify_key
import vault.wechat_crypto as crypto


def make_sqlite(path):
    library = ctypes.CDLL(ctypes.util.find_library("sqlite3"))
    library.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    library.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    library.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.sqlite3_close.argtypes = [ctypes.c_void_p]
    database = ctypes.c_void_p()
    if library.sqlite3_open(str(path).encode(), ctypes.byref(database)):
        raise RuntimeError("Could not create synthetic SQLite fixture")
    try:
        if library.sqlite3_exec(database, b"PRAGMA page_size=4096;", None, None, None):
            raise RuntimeError("Could not set fixture page size")
        reserve = ctypes.c_int(80)
        if library.sqlite3_file_control(database, b"main", 38, ctypes.byref(reserve)):
            raise RuntimeError("Could not set SQLite fixture reserved bytes")
        sql = "CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT);"
        for index in range(64):
            sql += f"INSERT INTO messages(body) VALUES('synthetic-message-{index}-{'x' * 350}');"
        if library.sqlite3_exec(database, sql.encode(), None, None, None):
            raise RuntimeError("Could not populate synthetic SQLite fixture")
    finally:
        library.sqlite3_close(database)
    return path.read_bytes()


def encrypt_fixture(plaintext, key):
    salt = bytes(range(16))
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(value ^ 0x3A for value in salt), 2, 32)
    result = bytearray()
    for offset in range(0, len(plaintext), 4096):
        page_number = offset // 4096 + 1
        page = plaintext[offset:offset + 4096]
        iv = hashlib.sha256(f"fixture-iv-{page_number}".encode()).digest()[:16]
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        start = 16 if page_number == 1 else 0
        encrypted = encryptor.update(page[start:4016]) + encryptor.finalize()
        authenticated = encrypted + iv
        mac = hmac.new(mac_key, authenticated + struct.pack("<I", page_number), hashlib.sha512).digest()
        result.extend((salt if page_number == 1 else b"") + authenticated + mac)
    return bytes(result)


@unittest.skipIf(Cipher is None, "Independent encrypted fixtures require cryptography")
class WeChatCryptoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        fixture = self.root / "synthetic.sqlite3"
        self.plaintext = make_sqlite(fixture)
        fixture.unlink()
        self.assertEqual(self.plaintext[20], 80)
        self.assertGreater(len(self.plaintext), 8192)
        self.key = secrets.token_bytes(32)
        self.encrypted = encrypt_fixture(self.plaintext, self.key)
        self.source = self.root / "encrypted.db"
        self.source.write_bytes(self.encrypted)
        self.destination = self.root / "readable.db"

    def assert_no_plaintext_output(self):
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".shiliao-decrypt-*.tmp")))

    def test_roundtrip_is_exact_and_readable_by_standard_sqlite(self):
        self.assertTrue(verify_key(self.source, self.key.hex()))
        result = decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(result, {"file_count": 1, "page_count": len(self.plaintext) // 4096})
        self.assertEqual(self.destination.read_bytes(), self.plaintext)
        self.assertEqual(self.source.read_bytes(), self.encrypted)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(f"file:{self.destination}?mode=ro", uri=True) as database:
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 64)

    def test_wrong_key_is_rejected_without_retaining_plaintext(self):
        wrong_key = bytes(value ^ 1 for value in self.key).hex()
        self.assertFalse(verify_key(self.source, wrong_key))
        with self.assertRaises(WeChatCryptoError) as failure:
            decrypt_database(self.source, self.destination, wrong_key)
        self.assertEqual(failure.exception.code, "key_mismatch")
        self.assertNotIn(wrong_key, str(failure.exception))
        self.assert_no_plaintext_output()

    def test_tampered_later_page_is_rejected_after_first_page_verifies(self):
        modified = bytearray(self.encrypted)
        modified[4096 + 50] ^= 1
        self.source.write_bytes(modified)
        self.assertTrue(verify_key(self.source, self.key.hex()))
        with self.assertRaises(WeChatCryptoError) as failure:
            decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(failure.exception.code, "integrity_failed")
        self.assert_no_plaintext_output()

    def test_nonempty_wal_is_rejected(self):
        self.source.with_name(self.source.name + "-wal").write_bytes(b"synthetic pending WAL")
        with self.assertRaises(WeChatCryptoError) as failure:
            decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(failure.exception.code, "pending_wal")
        self.assert_no_plaintext_output()

    def test_empty_wal_is_allowed(self):
        self.source.with_name(self.source.name + "-wal").touch()
        self.assertEqual(decrypt_database(self.source, self.destination, self.key.hex())["file_count"], 1)

    def test_source_can_never_be_overwritten(self):
        with self.assertRaises(WeChatCryptoError) as failure:
            decrypt_database(self.source, self.source, self.key.hex())
        self.assertEqual(failure.exception.code, "unsafe_destination")
        self.assertEqual(self.source.read_bytes(), self.encrypted)

    def test_existing_destination_is_preserved(self):
        self.destination.write_bytes(b"keep this file")
        with self.assertRaises(WeChatCryptoError):
            decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(self.destination.read_bytes(), b"keep this file")

    def test_source_mutation_prevents_publication(self):
        original_factory = crypto._aes_decryptor

        def factory(key):
            original = original_factory(key)
            changed = False

            def decrypt(iv, encrypted):
                nonlocal changed
                if not changed:
                    with self.source.open("ab") as output:
                        output.write(b"changed")
                    changed = True
                return original(iv, encrypted)

            return decrypt

        with patch.object(crypto, "_aes_decryptor", side_effect=factory):
            with self.assertRaises(WeChatCryptoError) as failure:
                decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(failure.exception.code, "source_changed")
        self.assert_no_plaintext_output()

    def test_wal_created_during_decryption_prevents_publication(self):
        original_factory = crypto._aes_decryptor

        def factory(key):
            self.source.with_name(self.source.name + "-wal").write_bytes(b"synthetic pending WAL")
            return original_factory(key)

        with patch.object(crypto, "_aes_decryptor", side_effect=factory):
            with self.assertRaises(WeChatCryptoError) as failure:
                decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(failure.exception.code, "pending_wal")
        self.assert_no_plaintext_output()

    def test_invalid_key_and_truncated_database_are_rejected(self):
        self.assertFalse(verify_key(self.source, "not a key"))
        with self.assertRaises(WeChatCryptoError):
            decrypt_database(self.source, self.destination, "not a key")
        self.source.write_bytes(self.encrypted[:-1])
        with self.assertRaises(WeChatCryptoError) as failure:
            decrypt_database(self.source, self.destination, self.key.hex())
        self.assertEqual(failure.exception.code, "invalid_source")
        self.assert_no_plaintext_output()


if __name__ == "__main__":
    unittest.main()
