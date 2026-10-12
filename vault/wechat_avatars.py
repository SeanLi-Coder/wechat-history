"""Import local WeChat head images into independently readable media objects."""

import hashlib
import io
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import warnings


MAX_AVATAR_BYTES = 8 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000


def _check_image_header(data):
    from PIL import Image
    from vault.media_codecs import MediaCodecError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if (picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}
                        or not 0 < picture.width * picture.height <= MAX_AVATAR_PIXELS
                        or getattr(picture, "n_frames", 1) != 1):
                    raise ValueError
    except Exception:
        raise MediaCodecError("invalid_avatar", "The avatar format or dimensions are not supported") from None


def avatar_identity(account_key, username):
    """Keep identically named contacts and different accounts independent."""
    if (not isinstance(account_key, str) or not re.fullmatch(r"[a-f0-9]{64}", account_key)
            or not isinstance(username, str) or not username or len(username) > 4096):
        raise ValueError("Invalid avatar identity")
    return hashlib.sha256((account_key + ":" + username).encode("utf-8")).hexdigest()


def import_avatars(root, library, account_id, progress=None):
    """Read a checkpointed local head_image database; never fetch remote URLs.

    Missing or unreadable pictures leave existing avatars intact. Image bytes
    are stored without recompression, and content hashes deduplicate objects.
    """
    from vault.media_codecs import MediaCodecError, _image_info
    from vault.wechat_reader import _assert_no_pending_journal, _file_state, _metadata_text, _open_source, _require_columns

    if not isinstance(account_id, str) or not account_id:
        raise ValueError("A stable account identity is required")
    root = Path(root).expanduser().absolute()
    path = root / "head_image/head_image.db"
    result = {"imported": 0, "unavailable": 0, "unsupported": 0}
    if root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Avatar database directories must not be symbolic links")
    if not path.exists():
        return {**result, "status": "unavailable"}
    before = _file_state(path)
    account_key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    pending = {}
    try:
        with _open_source(path) as source, tempfile.TemporaryDirectory(prefix=".avatars-", dir=library.state_dir) as folder:
            _require_columns(source, "head_image", {"username", "image_buffer"})
            columns = {row["name"] for row in source.execute('PRAGMA table_info("head_image")')}
            order = " ORDER BY update_time,rowid" if "update_time" in columns else " ORDER BY rowid"
            cursor = source.execute("""SELECT username,length(image_buffer) AS image_size,
                CASE WHEN typeof(image_buffer)='blob' AND length(image_buffer)<=?
                THEN image_buffer ELSE NULL END AS image_buffer FROM head_image""" + order, (MAX_AVATAR_BYTES,))
            for row in cursor:
                username = _metadata_text(row["username"])
                if not username or len(username) > 4096:
                    result["unsupported"] += 1
                    continue
                data = row["image_buffer"]
                if not isinstance(data, bytes) or not data:
                    result["unavailable"] += 1
                    continue
                try:
                    _check_image_header(data)
                    info = _image_info(data)
                    if (info["mime_type"] not in {"image/jpeg", "image/png", "image/gif", "image/webp"}
                            or info["width"] * info["height"] > MAX_AVATAR_PIXELS
                            or info["frames"] != 1):
                        raise MediaCodecError("invalid_avatar", "The avatar format is not supported")
                except MediaCodecError:
                    result["unsupported"] += 1
                    continue
                temporary = Path(folder) / "avatar"
                with temporary.open("wb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    out.write(data)
                asset = library.media.store_file(temporary, "image", info["mime_type"], "avatar." + info["format"])
                pending[avatar_identity(account_key, username)] = asset
                if progress and len(pending) % 100 == 0:
                    progress({"current_file": "正在保存本机联系人头像", "done_avatars": len(pending)})
            _assert_no_pending_journal(path)
            if _file_state(path) != before:
                raise ValueError("The avatar database changed during import")
        for identity_key, asset in pending.items():
            library.set_avatar(identity_key, asset)
        result["imported"] = len(pending)
        return {**result, "status": "done"}
    except sqlite3.DatabaseError:
        raise ValueError("The local avatar database has an unsupported schema") from None
