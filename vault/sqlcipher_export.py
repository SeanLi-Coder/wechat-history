"""Export a frozen SQLCipher 4 database, including its committed WAL state."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile


CHUNK_SIZE = 1024 * 1024
SQLCIPHER_TIMEOUT = 600
SIDECARS = ("", "-wal", "-shm", "-journal")


class SQLCipherExportError(RuntimeError):
    """A sanitized error that never includes keys, SQL, or database values."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _signature(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _capture_source(source: Path) -> dict[str, tuple | None]:
    result = {}
    for suffix in SIDECARS:
        path = source.with_name(source.name + suffix)
        try:
            value = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise SQLCipherExportError("source_unavailable", "The encrypted database is unavailable.") from None
            result[suffix] = None
            continue
        if not stat.S_ISREG(value.st_mode):
            raise SQLCipherExportError("unsafe_source", "Database files and sidecars must be regular files, not symbolic links.")
        if suffix == "-journal" and value.st_size:
            raise SQLCipherExportError("pending_journal", "A nonempty rollback journal is not supported; use a closed, consistent snapshot.")
        result[suffix] = _signature(value)
    if not result[""][2] or result[""][2] % 4096:
        raise SQLCipherExportError("invalid_source", "The encrypted database has an unsupported size.")
    return result


def _check_source(source: Path, expected: dict[str, tuple | None]) -> None:
    try:
        current = _capture_source(source)
    except (OSError, SQLCipherExportError):
        raise SQLCipherExportError("source_changed", "The database or its sidecars changed during export.") from None
    if current != expected:
        raise SQLCipherExportError("source_changed", "The database or its sidecars changed during export.")


def _copy_file(source: Path, target: Path, expected: tuple) -> None:
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if _signature(os.fstat(source_fd)) != expected:
            raise SQLCipherExportError("source_changed", "A source file changed before copying.")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600)
        with os.fdopen(target_fd, "wb") as output:
            copied = 0
            while chunk := os.read(source_fd, CHUNK_SIZE):
                copied += len(chunk)
                if copied > expected[2]:
                    raise SQLCipherExportError("source_changed", "A source file grew during copying.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != expected[2] or _signature(os.fstat(source_fd)) != expected:
            raise SQLCipherExportError("source_changed", "A source file changed during copying.")
    finally:
        os.close(source_fd)


def _find_sqlcipher() -> str:
    for candidate in ("/opt/homebrew/bin/sqlcipher", "/usr/local/bin/sqlcipher"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    candidate = shutil.which("sqlcipher")
    if candidate:
        return candidate
    raise SQLCipherExportError("sqlcipher_unavailable", "SQLCipher is required to read a database with a WAL.")


def _section(lines: list[str], name: str) -> list[str]:
    start, end = f"SHILIAO_{name}_BEGIN", f"SHILIAO_{name}_END"
    if lines.count(start) != 1 or lines.count(end) != 1:
        raise SQLCipherExportError("export_failed", "SQLCipher did not complete its verification steps.")
    first, last = lines.index(start), lines.index(end)
    if first >= last:
        raise SQLCipherExportError("export_failed", "SQLCipher returned an incomplete verification result.")
    return [line for line in lines[first + 1:last] if line]


def _run_export(staged_source: Path, staged_output: Path, key_hex: str) -> None:
    # Only generated relative filenames are interpolated, and a strict hex key
    # is supplied on stdin. No key or SQL statement is ever put in argv.
    if staged_source.name != "source.db" or staged_output.name != "readable.sqlite3":
        raise SQLCipherExportError("export_failed", "Unexpected private staging layout.")
    sql = f""".echo off
.bail on
PRAGMA cipher_log_level = NONE;
PRAGMA key = "x'{key_hex}'";
PRAGMA cipher_compatibility = 4;
PRAGMA cipher_memory_security = ON;
SELECT count(*) FROM sqlite_master;
.print SHILIAO_CHECKPOINT_BEGIN
PRAGMA wal_checkpoint(TRUNCATE);
.print SHILIAO_CHECKPOINT_END
PRAGMA journal_mode = DELETE;
.print SHILIAO_CIPHER_BEGIN
PRAGMA cipher_integrity_check;
.print SHILIAO_CIPHER_END
.print SHILIAO_SQLITE_BEGIN
PRAGMA integrity_check;
.print SHILIAO_SQLITE_END
ATTACH DATABASE 'readable.sqlite3' AS plaintext KEY '';
PRAGMA plaintext.journal_mode = DELETE;
SELECT sqlcipher_export('plaintext');
DETACH DATABASE plaintext;
.print SHILIAO_EXPORT_COMPLETE
"""
    try:
        result = subprocess.run(
            [_find_sqlcipher(), "-batch", "-bail", "-noheader", "-init", os.devnull, staged_source.name],
            input=sql, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=staged_source.parent, timeout=SQLCIPHER_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SQLCipherExportError("export_failed", "SQLCipher could not complete the private database export.") from None
    if result.returncode:
        # Shell diagnostics can repeat a failing SQL statement. They must never
        # be propagated, logged, or included in a chained exception.
        raise SQLCipherExportError("key_or_database_invalid", "SQLCipher could not authenticate or read the database copy.")
    lines = [line.strip() for line in result.stdout.splitlines()]
    checkpoint = _section(lines, "CHECKPOINT")
    if len(checkpoint) != 1 or not re.fullmatch(r"0\|(-?\d+)\|\1", checkpoint[0]):
        raise SQLCipherExportError("checkpoint_failed", "The private WAL copy could not be fully checkpointed.")
    if _section(lines, "CIPHER") not in ([], ["ok"]):
        raise SQLCipherExportError("integrity_failed", "The encrypted database copy failed its integrity check.")
    if _section(lines, "SQLITE") != ["ok"] or "SHILIAO_EXPORT_COMPLETE" not in lines:
        raise SQLCipherExportError("integrity_failed", "The database copy failed its structural integrity check.")


def _verify_plaintext(path: Path) -> int:
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise SQLCipherExportError("invalid_output", "SQLCipher did not produce a readable SQLite database.")
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise SQLCipherExportError("invalid_output", "The exported SQLite database has an unfinished journal.")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        cursor = connection.execute("PRAGMA integrity_check")
        if cursor.fetchone() != ("ok",) or cursor.fetchone() is not None:
            raise SQLCipherExportError("invalid_output", "The exported SQLite database failed its integrity check.")
    finally:
        connection.close()
    return path.stat().st_size


def export_database(source: Path, destination: Path, key_hex: str) -> dict:
    """Export a closed SQLCipher 4 main file and committed WAL into new SQLite.

    Source files are opened only for byte reads. WAL recovery, checkpointing,
    authentication and export run inside a private copy. Every source/sidecar
    signature and its presence are checked after copying and before publishing.
    Existing destination files are never overwritten.
    """
    if not isinstance(key_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        raise SQLCipherExportError("invalid_key", "The database key must contain exactly 64 hexadecimal characters.")
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    published_inode = None
    completed = False
    try:
        expected = _capture_source(source)
        resolved_destination = destination.resolve()
        source_family = {source.with_name(source.name + suffix).resolve() for suffix in SIDECARS}
        if any(resolved_destination == path or path in resolved_destination.parents for path in source_family):
            raise SQLCipherExportError("unsafe_destination", "The encrypted source and its sidecars cannot be export destinations.")
        if os.path.lexists(destination):
            raise SQLCipherExportError("destination_exists", "The export destination already exists and was not changed.")
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        source_bytes = sum(value[2] for value in expected.values() if value is not None)
        if shutil.disk_usage(destination.parent).free < source_bytes * 3 + CHUNK_SIZE:
            raise SQLCipherExportError("insufficient_space", "There is not enough free space for a private database export.")
        with tempfile.TemporaryDirectory(prefix=".shiliao-sqlcipher-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            staging.chmod(0o700)
            staged_source = staging / "source.db"
            for suffix, signature in expected.items():
                if signature is not None:
                    _copy_file(source.with_name(source.name + suffix),
                               staging / (staged_source.name + suffix), signature)
            _check_source(source, expected)
            staged_output = staging / "readable.sqlite3"
            descriptor = os.open(staged_output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            _run_export(staged_source, staged_output, key_hex)
            exported_bytes = _verify_plaintext(staged_output)
            staged_output.chmod(0o600)
            with staged_output.open("rb") as stream:
                os.fsync(stream.fileno())
            _check_source(source, expected)
            # link publishes atomically without replacing a concurrent file.
            os.link(staged_output, destination)
            published_inode = staged_output.stat().st_ino
            directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            completed = True
            return {"file_count": 1, "original_bytes": source_bytes,
                    "exported_bytes": exported_bytes,
                    "included_wal": bool(expected["-wal"] and expected["-wal"][2])}
    except SQLCipherExportError:
        raise
    except FileExistsError:
        raise SQLCipherExportError("destination_exists", "The export destination already exists and was not changed.") from None
    except sqlite3.DatabaseError:
        raise SQLCipherExportError("invalid_output", "The exported SQLite database could not be verified.") from None
    except OSError:
        raise SQLCipherExportError("export_failed", "Database files could not be read or written; no partial export was kept.") from None
    finally:
        if published_inode is not None and not completed:
            try:
                if destination.lstat().st_ino == published_inode:
                    destination.unlink()
            except OSError:
                pass
