"""Validate private database keys and import a closed WeChat account locally."""

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from vault.wechat_crypto import decrypt_database, verify_key


def _message_databases(root):
    return sorted(path for path in (root / "message").glob("message_*.db")
                  if re.fullmatch(r"message_[0-9]+\.db", path.name))


def _auxiliary_databases(root):
    return sorted(path for path in (root / "message").glob("*.db")
                  if path.name == "message_resource.db" or re.fullmatch(r"media_[0-9]+\.db", path.name))


def database_root(source):
    source = Path(source).expanduser().resolve(strict=True)
    if source.name == "db_storage" and source.is_dir():
        return source
    if (source / "db_storage").is_dir():
        return source / "db_storage"
    roots = sorted(source.glob("*/db_storage"))
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1:
        raise ValueError("检测到多个微信账号，请在设置中选择要阅读的单个账号目录。")
    raise ValueError("没有找到微信 4.x 的 db_storage 目录，请检查数据来源。")


def parse_keys(data):
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("密钥文件过大。")
    try:
        document = json.loads(data)
        keys = document.get("keys")
        if not isinstance(keys, dict) or not keys or len(keys) > 1000:
            raise ValueError
        result = {}
        for relative, item in keys.items():
            path = PurePosixPath(relative)
            value = item.get("enc_key") if isinstance(item, dict) else item
            if (path.is_absolute() or ".." in path.parts or str(path) != relative or "\\" in relative
                    or not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value)):
                raise ValueError
            result[relative] = value.lower()
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("密钥文件格式不正确，需要 keys 对象及逐库的 64 位十六进制 enc_key。") from None


def save_verified_keys(data, source, state_dir):
    root = database_root(source)
    keys = parse_keys(data)
    verified = {}
    for relative, key in keys.items():
        path = root / relative
        if not path.is_file() or path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("密钥文件与所选账号的数据库不匹配。")
        if not verify_key(path, key):
            raise ValueError("数据库密钥校验未通过，未保存密钥文件。")
        verified[relative] = {"enc_key": key}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".keys-", dir=state_dir)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump({"keys": verified, "database_root": str(root)}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_dir / "wechat-keys.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"verified_databases": len(verified)}


def key_status(state_dir):
    path = Path(state_dir) / "wechat-keys.json"
    if not path.exists():
        return {"configured": False, "key_count": 0}
    try:
        keys = parse_keys(path.read_bytes())
        return {"configured": True, "key_count": len(keys)}
    except (ValueError, OSError):
        return {"configured": False, "key_count": 0, "error": "本机密钥文件无法读取，请重新导入。"}


def import_account(source, state_dir, library, assert_closed, progress=None, *,
                   account_id=None, include_missing_media=False):
    from vault.wechat_reader import import_decrypted
    assert_closed()
    if progress:
        progress({"done_files": 0, "total_files": 0,
                  "current_file": "正在访问微信数据；如 macOS 弹出访问其他 App 数据的提示，请选择允许。"})
    root = database_root(source)
    identity = str(root) if account_id is None else account_id
    if not isinstance(identity, str) or not identity:
        raise ValueError("账号来源标识无效。")
    key_path = Path(state_dir) / "wechat-keys.json"
    if not key_path.exists():
        raise ValueError("尚未配置本机数据库密钥，请先完成一次本地读取初始化。")
    keys = parse_keys(key_path.read_bytes())
    selected = _message_databases(root)
    auxiliary = _auxiliary_databases(root)
    selected.extend(auxiliary)
    contact = root / "contact/contact.db"
    if contact.is_file():
        selected.insert(0, contact)
    avatar_path = root / "head_image/head_image.db"
    avatar_available = avatar_path.is_file() and "head_image/head_image.db" in keys
    if avatar_available:
        if avatar_path.parent.is_symlink() or avatar_path.is_symlink():
            raise ValueError("头像数据库必须是账号目录内的普通文件。")
        selected.append(avatar_path)
    if not _message_databases(root):
        raise ValueError("未找到微信消息数据库。")
    for path in selected:
        relative = str(path.relative_to(root))
        if relative not in keys:
            raise ValueError("部分消息数据库缺少密钥，需要重新初始化；没有导入不完整的记录。")
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("消息数据库不能是指向其他目录的链接。")
    signatures = {path: path.stat() for path in selected}
    journals = {}
    for path in selected:
        for suffix in ("-wal", "-journal"):
            journal = path.with_name(path.name + suffix)
            info = journal.lstat() if os.path.lexists(journal) else None
            if info and not stat.S_ISREG(info.st_mode):
                raise ValueError("微信数据库日志必须是普通文件，不能是符号链接。")
            journals[journal] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if info else None
    expected_size = sum(info.st_size for info in signatures.values()) + sum(info[1] for info in journals.values() if info)
    if shutil.disk_usage(state_dir).free < expected_size * 3 + 1024 * 1024:
        raise ValueError("本机空间不足，请为解密副本和阅读库预留至少数据库大小三倍的空间。")
    with tempfile.TemporaryDirectory(prefix="wechat-read-", dir=state_dir) as folder:
        decrypted_root = Path(folder)
        for index, path in enumerate(selected):
            assert_closed()
            relative = str(path.relative_to(root))
            target = decrypted_root / relative
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            wal = path.with_name(path.name + "-wal")
            if journals[wal] and journals[wal][1]:
                from vault.sqlcipher_export import export_database
                export_database(path, target, keys[relative])
            else:
                decrypt_database(path, target, keys[relative])
            if progress:
                progress({"done_files": index + 1, "total_files": len(selected), "current_file": relative})
        assert_closed()
        for path, before in signatures.items():
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("微信数据库在读取期间发生变化，已取消本次导入。")
        for journal, before in journals.items():
            info = journal.lstat() if os.path.lexists(journal) else None
            after = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if info else None
            if before != after:
                raise ValueError("微信数据库的 WAL 或 journal 在读取期间发生变化，已取消本次导入。")
        if set(_message_databases(root)) != {path for path in selected if re.fullmatch(r"message_[0-9]+\.db", path.name)}:
            raise ValueError("微信消息分库在读取期间发生变化，已取消本次导入。")
        if _auxiliary_databases(root) != auxiliary:
            raise ValueError("微信媒体分库在读取期间发生变化，已取消本次导入。")
        if progress:
            progress({"done_files": len(selected), "total_files": len(selected), "snapshot_ready": True,
                      "current_file": "聊天快照已完成，可以重新打开微信；媒体仍在整理，请暂勿清理微信文件。"})
        result = import_decrypted(decrypted_root, library, account_id=identity, progress=progress)
        if avatar_available:
            from vault.wechat_avatars import import_avatars
            result["avatars"] = import_avatars(decrypted_root, library, identity, progress)
        if auxiliary or include_missing_media:
            from vault.media_import import import_media
            result["media"] = import_media(decrypted_root, root.parent, library, identity, progress)
        return result
