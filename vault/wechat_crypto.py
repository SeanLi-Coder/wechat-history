"""Verify and decrypt closed SQLCipher 4 databases into private new copies.

This module accepts an already available raw key. It does not inspect processes,
obtain keys, change WeChat, or write to an encrypted source database.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile


PAGE_SIZE = 4096
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"


class WeChatCryptoError(RuntimeError):
    def __init__(self, message: str, code: str = "decrypt_failed"):
        super().__init__(message)
        self.code = code


def _key_bytes(key_hex: str) -> bytes:
    if not isinstance(key_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        raise WeChatCryptoError("The database key must contain exactly 64 hexadecimal characters.", "invalid_key")
    return bytes.fromhex(key_hex)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_source(source: Path):
    source = Path(source).expanduser().absolute()
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WeChatCryptoError("The encrypted database must be a regular file, not a symbolic link.", "invalid_source")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    stream = os.fdopen(descriptor, "rb")
    if _signature(os.fstat(stream.fileno())) != _signature(info):
        stream.close()
        raise WeChatCryptoError("The encrypted database changed before it could be opened.", "source_changed")
    return source, stream, _signature(info)


def _check_unchanged(source, stream, signature):
    if _signature(source.lstat()) != signature or _signature(os.fstat(stream.fileno())) != signature:
        raise WeChatCryptoError("The encrypted database changed during decryption; retry with a closed backup copy.", "source_changed")


def _journal_state(source):
    state = []
    for suffix in ("-wal", "-journal"):
        journal = source.with_name(source.name + suffix)
        try:
            info = journal.lstat()
        except FileNotFoundError:
            state.append(None)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise WeChatCryptoError(
                "The database has a nonempty WAL or journal. Close WeChat and create a consistent backup before decrypting.",
                "pending_wal",
            )
        state.append(_signature(info))
    return tuple(state)


def _mac_key(key, salt):
    return hashlib.pbkdf2_hmac("sha512", key, bytes(value ^ 0x3A for value in salt), 2, dklen=32)


def _verify_page(page, page_number, mac_key):
    if len(page) != PAGE_SIZE:
        return False
    start = 16 if page_number == 1 else 0
    actual = hmac.new(mac_key, page[start:4032] + struct.pack("<I", page_number), hashlib.sha512).digest()
    return hmac.compare_digest(actual, page[4032:4096])


def verify_key(source: Path, key_hex: str) -> bool:
    """Validate a raw key against page one, without exporting any plaintext."""
    try:
        key = _key_bytes(key_hex)
    except WeChatCryptoError:
        return False
    try:
        source, stream, signature = _open_source(source)
        with stream:
            page = stream.read(PAGE_SIZE)
            _check_unchanged(source, stream, signature)
        return len(page) == PAGE_SIZE and _verify_page(page, 1, _mac_key(key, page[:16]))
    except OSError:
        raise WeChatCryptoError("The encrypted database could not be read.", "source_unavailable") from None


def _aes_decryptor(key):
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            crypt = library.CCCrypt
        except (OSError, AttributeError):
            raise WeChatCryptoError("The system AES provider is unavailable.", "crypto_unavailable") from None
        crypt.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        crypt.restype = ctypes.c_int32
        key_buffer = ctypes.create_string_buffer(key, len(key))

        def decrypt(iv, encrypted):
            iv_buffer = ctypes.create_string_buffer(iv, len(iv))
            input_buffer = ctypes.create_string_buffer(encrypted, len(encrypted))
            output_buffer = ctypes.create_string_buffer(len(encrypted))
            output_size = ctypes.c_size_t()
            result = crypt(1, 0, 0, key_buffer, len(key), iv_buffer,
                           input_buffer, len(encrypted), output_buffer, len(output_buffer), ctypes.byref(output_size))
            if result != 0 or output_size.value != len(encrypted):
                raise WeChatCryptoError("A database page could not be decrypted.")
            return output_buffer.raw

        return decrypt

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise WeChatCryptoError("The cryptography package is required on this platform.", "crypto_unavailable") from None

    def decrypt(iv, encrypted):
        context = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return context.update(encrypted) + context.finalize()

    return decrypt


def decrypt_database(source: Path, destination: Path, key_hex: str) -> dict:
    """Authenticate every page and atomically create a new plaintext database.

    Existing output files are never overwritten. A nonempty WAL or rollback
    journal is rejected because decrypting the main file alone would omit data.
    """
    key = _key_bytes(key_hex)
    temporary = None
    try:
        source, stream, signature = _open_source(source)
        with stream:
            file_size = signature[2]
            if not file_size or file_size % PAGE_SIZE:
                raise WeChatCryptoError("The encrypted database size is incompatible with SQLCipher 4.", "invalid_source")
            destination = Path(destination).expanduser().absolute()
            if destination.resolve() == source.resolve():
                raise WeChatCryptoError("Decryption must write a new file; the encrypted source cannot be overwritten.", "unsafe_destination")
            if os.path.lexists(destination):
                raise WeChatCryptoError("The destination already exists and will not be overwritten.", "destination_exists")
            journals = _journal_state(source)
            first_page = stream.read(PAGE_SIZE)
            mac_key = _mac_key(key, first_page[:16])
            if not _verify_page(first_page, 1, mac_key):
                raise WeChatCryptoError("The key does not match this database, or its first page is damaged.", "key_mismatch")
            decrypt = _aes_decryptor(key)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".shiliao-decrypt-", suffix=".tmp", dir=destination.parent)
            temporary = Path(temporary_name)
            page_count = file_size // PAGE_SIZE
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                for page_number in range(1, page_count + 1):
                    page = first_page if page_number == 1 else stream.read(PAGE_SIZE)
                    if not _verify_page(page, page_number, mac_key):
                        raise WeChatCryptoError("A database page failed its integrity check; no readable copy was kept.", "integrity_failed")
                    start = 16 if page_number == 1 else 0
                    plaintext = decrypt(page[4016:4032], page[start:4016])
                    if page_number == 1:
                        plaintext = SQLITE_HEADER + plaintext
                        if plaintext[16:18] != b"\x10\x00" or plaintext[20] != RESERVE_SIZE:
                            raise WeChatCryptoError("The decrypted database has unsupported page settings.", "unsupported_format")
                    output.write(plaintext + b"\x00" * RESERVE_SIZE)
                output.flush()
                os.fsync(output.fileno())
            _check_unchanged(source, stream, signature)
            if _journal_state(source) != journals:
                raise WeChatCryptoError("The database journal changed during decryption; retry from a closed backup copy.", "source_changed")
            # A hard link atomically publishes the completed file and cannot
            # overwrite a destination created by another process in the meantime.
            os.link(temporary, destination)
            temporary.unlink()
            temporary = None
            return {"file_count": 1, "page_count": page_count}
    except FileExistsError:
        raise WeChatCryptoError("The destination already exists and will not be overwritten.", "destination_exists") from None
    except OSError:
        raise WeChatCryptoError("The database copy could not be read or written; no partial output was published.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
