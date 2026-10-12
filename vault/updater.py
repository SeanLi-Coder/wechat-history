"""Authenticated, file-delta application updates; user archives are never moved."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import posixpath
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import unicodedata
import urllib.parse
import urllib.request
import zipfile

from vault import __version__


PUBLIC_KEY_B64 = "xogyeOSy3BJX7yByx4Pp8R98uzmYfPqZ9TF3k/eGuaM="
FEED_URL = "https://github.com/SeanLi-Coder/wechat-history/releases/latest/download/update-arm64.json"
APP_ID = "local.shiliao.archive"
MAX_METADATA = 8 * 1024 ** 2
MAX_PACKAGE = 512 * 1024 ** 2
MAX_TREE = 2 * 1024 ** 3
MAX_FILES = 30_000
HASH = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


class UpdateError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise UpdateError("duplicate_metadata_key")
        result[key] = value
    return result


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(UpdateError("invalid_metadata")))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise UpdateError("invalid_metadata") from exc


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise UpdateError("invalid_version")
    return tuple(map(int, value.split(".")))


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(ord(c) < 32 for c in value) or "\\" in value
            or unicodedata.normalize("NFC", value) != value):
        raise UpdateError("unsafe_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise UpdateError("unsafe_path")
    return value


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(files):
    return hashlib.sha256(canonical(files)).hexdigest()


def validate_tree(files):
    if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
        raise UpdateError("invalid_tree")
    entries, folded, total = {}, set(), 0
    for row in files:
        if not isinstance(row, dict):
            raise UpdateError("invalid_tree")
        name = safe_path(row.get("path"))
        if name in entries or name.casefold() in folded or not name.startswith("Contents/"):
            raise UpdateError("duplicate_or_unsafe_tree_path")
        folded.add(name.casefold())
        kind = row.get("type")
        mode = row.get("mode")
        if type(mode) is not int or mode < 0 or mode > 0o777 or mode & 0o022:
            if not (kind == "symlink" and mode == 0o777):
                raise UpdateError("unsafe_file_mode")
        if kind == "file":
            if (set(row) != {"path", "type", "mode", "size", "sha256"}
                    or type(row.get("size")) is not int or not 0 <= row["size"] <= MAX_PACKAGE
                    or not isinstance(row.get("sha256"), str) or not HASH.fullmatch(row["sha256"])):
                raise UpdateError("invalid_file_entry")
            total += row["size"]
        elif kind == "symlink":
            if set(row) != {"path", "type", "mode", "target"}:
                raise UpdateError("invalid_link_entry")
            target = row.get("target")
            if (not isinstance(target, str) or not target or target.startswith("/")
                    or "\\" in target or any(ord(c) < 32 for c in target)):
                raise UpdateError("unsafe_link")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if not resolved.startswith("Contents/"):
                raise UpdateError("unsafe_link")
        else:
            raise UpdateError("invalid_entry_type")
        entries[name] = row
    if total > MAX_TREE or files != sorted(files, key=lambda item: item["path"]):
        raise UpdateError("invalid_tree_order_or_size")
    for name, row in entries.items():
        parents = PurePosixPath(name).parents
        if any(parent.as_posix() in entries for parent in parents):
            raise UpdateError("file_or_link_is_parent")
    for name, row in entries.items():
        if row["type"] == "symlink":
            current, seen = name, set()
            while current in entries and entries[current]["type"] == "symlink":
                if current in seen:
                    raise UpdateError("cyclic_link")
                seen.add(current)
                current = posixpath.normpath(posixpath.join(posixpath.dirname(current), entries[current]["target"]))
                if not current.startswith("Contents/"):
                    raise UpdateError("unsafe_link")
            if current not in entries and not any(item.startswith(current + "/") for item in entries):
                raise UpdateError("missing_link_target")
    return entries


def inventory(bundle):
    bundle = Path(bundle)
    if not bundle.is_dir() or bundle.is_symlink():
        raise UpdateError("invalid_bundle")
    files = []
    for base, directories, names in os.walk(bundle, followlinks=False):
        for name in sorted(directories + names):
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            row = {"path": path.relative_to(bundle).as_posix(), "mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                row.update(type="symlink", target=os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                row.update(type="file", size=info.st_size, sha256=file_hash(path))
            else:
                raise UpdateError("special_bundle_file")
            files.append(row)
    files.sort(key=lambda row: row["path"])
    validate_tree(files)
    return files


def zip_inventory(path):
    """Read an application ZIP without extracting any member."""
    with zipfile.ZipFile(path, metadata_encoding="utf-8") as archive:
        roots, names, rows = set(), set(), []
        for member in archive.infolist():
            name = safe_path(member.filename.rstrip("/"))
            if name in names:
                raise UpdateError("duplicate_zip_member")
            names.add(name)
            parts = PurePosixPath(name).parts
            roots.add(parts[0])
            if not parts[0].endswith(".app"):
                raise UpdateError("invalid_zip_root")
            if member.is_dir():
                continue
            if len(parts) < 3:
                raise UpdateError("invalid_zip_member")
            mode = member.external_attr >> 16
            row = {"path": "/".join(parts[1:]), "mode": stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                if member.file_size > 4096:
                    raise UpdateError("unsafe_link")
                row.update(type="symlink", target=archive.read(member).decode("utf-8"))
            elif stat.S_ISREG(mode) or stat.S_IFMT(mode) == 0:
                if member.file_size > MAX_PACKAGE:
                    raise UpdateError("package_too_large")
                digest = hashlib.sha256()
                with archive.open(member) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                row.update(type="file", size=member.file_size, sha256=digest.hexdigest())
            else:
                raise UpdateError("special_zip_member")
            rows.append(row)
        if len(roots) != 1:
            raise UpdateError("invalid_zip_root")
        rows.sort(key=lambda row: row["path"])
        validate_tree(rows)
        return next(iter(roots)), rows


def _url(value, *, redirect=False, feed=False):
    if not isinstance(value, str):
        raise UpdateError("invalid_update_url")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
        raise UpdateError("invalid_update_url")
    if parsed.hostname == "github.com":
        prefix = "/SeanLi-Coder/wechat-history/releases/"
        if parsed.path.startswith(prefix + "download/") or (feed and parsed.path == prefix + "latest/download/update-arm64.json"):
            return value
    if redirect and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        return value
    raise UpdateError("untrusted_update_host")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _url(newurl, redirect=True)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(url, destination, limit, progress=None):
    """Fetch HTTPS with bounded output and a restricted redirect policy."""
    _url(url, feed=True)
    context = ssl.create_default_context()
    if Path("/etc/ssl/cert.pem").is_file():
        context.load_verify_locations("/etc/ssl/cert.pem")
    opener = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={"User-Agent": "Shiliao-Updater/1", "Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=30) as response, Path(destination).open("xb") as out:
            _url(response.url, redirect=True, feed=True)
            total = 0
            while True:
                block = response.read(min(1024 * 1024, limit + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise UpdateError("download_too_large")
                out.write(block)
                if progress:
                    progress(total)
    except BaseException:
        Path(destination).unlink(missing_ok=True)
        raise


def _asset(asset):
    if (not isinstance(asset, dict) or set(asset) != {"url", "sha256", "bytes"}
            or not isinstance(asset.get("sha256"), str) or not HASH.fullmatch(asset["sha256"])
            or type(asset.get("bytes")) is not int or not 0 < asset["bytes"] <= MAX_PACKAGE):
        raise UpdateError("invalid_asset")
    _url(asset["url"])


def verify_release(envelope, public_key_b64=PUBLIC_KEY_B64, *, minimum_sequence=0, now=None):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(envelope, dict) or set(envelope) != {"signed", "signature"}:
        raise UpdateError("invalid_signed_manifest")
    signed = envelope["signed"]
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(signed))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise UpdateError("invalid_update_signature") from exc
    if (not isinstance(signed, dict) or type(signed.get("schema")) is not int or signed.get("schema") != 1 or signed.get("app_id") != APP_ID
            or signed.get("architecture") != "arm64"):
        raise UpdateError("wrong_update_target")
    version_tuple(signed.get("version"))
    if type(signed.get("sequence")) is not int or signed["sequence"] < max(1, minimum_sequence):
        raise UpdateError("update_rollback_rejected")
    try:
        expiry = datetime.fromisoformat(signed["expires"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= (now or datetime.now(timezone.utc)):
            raise UpdateError("update_metadata_expired")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UpdateError("invalid_update_expiry") from exc
    _asset(signed.get("full"))
    target = signed.get("target")
    if not isinstance(target, dict) or set(target) != {"files", "tree_sha256"}:
        raise UpdateError("invalid_target_tree")
    validate_tree(target["files"])
    if tree_digest(target["files"]) != target["tree_sha256"]:
        raise UpdateError("target_tree_hash_mismatch")
    deltas = signed.get("deltas")
    if not isinstance(deltas, list) or len(deltas) > 8:
        raise UpdateError("invalid_delta_list")
    bases = set()
    for delta in deltas:
        if not isinstance(delta, dict) or set(delta) != {"base_version", "base_tree_sha256", "asset"}:
            raise UpdateError("invalid_delta")
        version_tuple(delta["base_version"])
        if (not isinstance(delta["base_tree_sha256"], str) or not HASH.fullmatch(delta["base_tree_sha256"])
                or delta["base_tree_sha256"] in bases):
            raise UpdateError("invalid_delta_base")
        bases.add(delta["base_tree_sha256"])
        _asset(delta["asset"])
    return signed


def verify_bundle(bundle, version):
    bundle = Path(bundle)
    try:
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        executable = safe_path(info["CFBundleExecutable"])
        if "/" in executable or info["CFBundleIdentifier"] != APP_ID or info["CFBundleShortVersionString"] != version:
            raise UpdateError("bundle_identity_mismatch")
        if info.get("ShiliaoBundledPython") != "python/bin/python3":
            raise UpdateError("bundle_runtime_mismatch")
        for path in (bundle / "Contents/MacOS" / executable, bundle / "Contents/Resources/python/bin/python3"):
            subprocess.run(["/usr/bin/lipo", "-verify_arch", "arm64", str(path)], check=True, capture_output=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True, capture_output=True)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise UpdateError("bundle_signature_or_identity_invalid") from exc


def reconstruct(base, package, destination, release, kind):
    """Create a fresh target tree, never patch an existing application in place."""
    base, package, destination = Path(base), Path(package), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise UpdateError("target_exists")
    entries = validate_tree(release["target"]["files"])
    try:
        destination.mkdir(mode=0o700, parents=True)
        with zipfile.ZipFile(package, metadata_encoding="utf-8") as archive:
            if kind == "full":
                root, actual = zip_inventory(package)
                if actual != release["target"]["files"]:
                    raise UpdateError("full_tree_mismatch")
                members = {row["path"]: root + "/" + row["path"] for row in actual if row["type"] == "file"}
            elif kind == "delta":
                members = {}
                for member in archive.infolist():
                    name = safe_path(member.filename)
                    if not name.startswith("payload/") or member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                        raise UpdateError("invalid_delta_member")
                    relative = name.removeprefix("payload/")
                    if relative in members or relative not in entries or entries[relative]["type"] != "file":
                        raise UpdateError("invalid_delta_member")
                    if member.file_size != entries[relative]["size"]:
                        raise UpdateError("delta_size_mismatch")
                    members[relative] = name
            else:
                raise UpdateError("invalid_package_kind")
            for name, row in entries.items():
                if row["type"] != "file":
                    continue
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if name in members:
                    source = archive.open(members[name])
                else:
                    path = base / name
                    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                        raise UpdateError("unsafe_base_file")
                    source = path.open("rb")
                with source, target.open("xb") as out:
                    digest, count = hashlib.sha256(), 0
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        count += len(block)
                        if count > row["size"]:
                            raise UpdateError("rebuilt_file_size_mismatch")
                        digest.update(block)
                        out.write(block)
                if count != row["size"] or digest.hexdigest() != row["sha256"]:
                    raise UpdateError("rebuilt_file_hash_mismatch")
                target.chmod(row["mode"])
            for name, row in entries.items():
                if row["type"] == "symlink":
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(row["target"])
                    if hasattr(os, "lchmod"):
                        os.lchmod(path, row["mode"])
        destination.chmod(0o755)
        if inventory(destination) != release["target"]["files"]:
            raise UpdateError("rebuilt_tree_mismatch")
        verify_bundle(destination, release["version"])
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_METADATA:
            raise UpdateError("local_metadata_too_large")
        return parse_json(stream.read(MAX_METADATA + 1))


class Updater:
    def __init__(self, state_dir, bundle_path=None, *, public_key_b64=None, feed_url=FEED_URL):
        self.state_dir = Path(state_dir).resolve()
        self.root = self.state_dir / "updates"
        if self.root.is_symlink():
            raise UpdateError("unsafe_update_directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if bundle_path is None:
            from vault.runtime import bundled_resources
            resources = bundled_resources()
            bundle_path = resources.parent.parent if resources else None
        self.bundle = Path(bundle_path).absolute() if bundle_path else None
        self.public_key = public_key_b64 or PUBLIC_KEY_B64
        self.feed_url = feed_url
        self._mutex = threading.Lock()
        self._action = threading.Lock()
        self.current_version = __version__
        if self.bundle and not self.bundle.is_symlink():
            try:
                self.current_version = plistlib.loads((self.bundle / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"]
            except (OSError, ValueError, KeyError):
                pass
        self.supported = platform.system() == "Darwin" and platform.machine() == "arm64" and self.bundle is not None
        self._state = {"supported": self.supported, "current_version": self.current_version, "status": "idle",
                       "latest_version": None, "download_kind": None, "download_bytes": 0, "downloaded_bytes": 0,
                       "full_bytes": 0, "error": None,
                       "reason": None if self.supported else "自动更新仅支持 Apple 芯片 Mac 上的拾聊应用。"}
        self._release = None
        try:
            envelope = _read_json(self.root / "release.json")
            release = verify_release(envelope, self.public_key, minimum_sequence=self._sequence())
            if version_tuple(release["version"]) > version_tuple(self.current_version):
                self._release = release
                self._state.update(latest_version=release["version"], full_bytes=release["full"]["bytes"], status="available")
                if (self.root / "pending.app").is_dir():
                    self._state["status"] = "ready"
        except (OSError, UpdateError):
            pass

    def _sequence(self):
        try:
            value = _read_json(self.root / "trusted.json")["sequence"]
            if type(value) is not int or value <= 0:
                raise UpdateError("invalid_trusted_sequence")
            return value
        except FileNotFoundError:
            return 0
        except (KeyError, TypeError):
            raise UpdateError("invalid_trusted_sequence") from None

    def _set(self, **values):
        with self._mutex:
            self._state.update(values)

    def status(self):
        with self._mutex:
            result = dict(self._state)
        try:
            last = _read_json(self.root / "install-result.json")
            if isinstance(last, dict) and last.get("status") in {"installed", "error"}:
                result["last_install"] = last
        except (OSError, UpdateError):
            pass
        return result

    @contextmanager
    def _busy(self):
        if not self.supported:
            raise UpdateError("unsupported_update_platform")
        if not self._action.acquire(blocking=False):
            raise UpdateError("update_busy")
        try:
            yield
        except Exception as exc:
            code = exc.code if isinstance(exc, UpdateError) else "update_failed"
            self._set(status="error", error=code)
            raise UpdateError(code) from None
        finally:
            self._action.release()

    def check(self):
        with self._busy(), tempfile.TemporaryDirectory(prefix="check-", dir=self.root) as temporary:
            self._set(status="checking", error=None)
            path = Path(temporary) / "feed.json"
            fetch(self.feed_url, path, MAX_METADATA)
            envelope = _read_json(path)
            release = verify_release(envelope, self.public_key, minimum_sequence=self._sequence())
            digest = hashlib.sha256(canonical(release)).hexdigest()
            try:
                trusted = _read_json(self.root / "trusted.json")
                if (trusted.get("sequence") == release["sequence"] and trusted.get("signed_sha256")
                        and trusted["signed_sha256"] != digest):
                    raise UpdateError("update_sequence_conflict")
            except FileNotFoundError:
                pass
            _write_json(self.root / "trusted.json", {"sequence": release["sequence"], "signed_sha256": digest})
            _write_json(self.root / "release.json", envelope)
            self._release = release
            newer = version_tuple(release["version"]) > version_tuple(self.current_version)
            self._set(status="available" if newer else "idle", latest_version=release["version"],
                      full_bytes=release["full"]["bytes"], downloaded_bytes=0)
        return self.status()

    def download(self):
        with self._busy():
            envelope = _read_json(self.root / "release.json")
            release = verify_release(envelope, self.public_key, minimum_sequence=self._sequence())
            if version_tuple(release["version"]) <= version_tuple(self.current_version):
                raise UpdateError("update_not_newer")
            pending = self.root / "pending.app"
            if pending.is_dir() and not pending.is_symlink():
                if inventory(pending) == release["target"]["files"]:
                    verify_bundle(pending, release["version"])
                    self._release = release
                    self._set(status="ready", error=None, latest_version=release["version"],
                              full_bytes=release["full"]["bytes"])
                    return self.status()
            current_tree = tree_digest(inventory(self.bundle))
            delta = next((d for d in release["deltas"] if d["base_tree_sha256"] == current_tree
                          and d["base_version"] == self.current_version and d["asset"]["bytes"] < release["full"]["bytes"]), None)
            kind, asset = ("delta", delta["asset"]) if delta else ("full", release["full"])
            self._set(status="downloading", error=None, download_kind=kind, download_bytes=asset["bytes"],
                      downloaded_bytes=0, latest_version=release["version"], full_bytes=release["full"]["bytes"])
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.root) as temporary:
                package = Path(temporary) / "package.zip"
                fetch(asset["url"], package, asset["bytes"], lambda size: self._set(downloaded_bytes=size))
                if package.stat().st_size != asset["bytes"] or file_hash(package) != asset["sha256"]:
                    raise UpdateError("package_hash_mismatch")
                staged = Path(temporary) / "new.app"
                reconstruct(self.bundle, package, staged, release, kind)
                pending = self.root / "pending.app"
                if pending.exists() or pending.is_symlink():
                    if pending.is_symlink():
                        raise UpdateError("unsafe_pending_bundle")
                    shutil.rmtree(pending)
                staged.rename(pending)
            self._release = release
            self._set(status="ready")
        return self.status()

    def launch_install(self, parent_pid=None):
        with self._busy():
            release = verify_release(_read_json(self.root / "release.json"), self.public_key,
                                     minimum_sequence=self._sequence())
            if version_tuple(release["version"]) <= version_tuple(self.current_version):
                raise UpdateError("update_not_newer")
            pending = self.root / "pending.app"
            if inventory(pending) != release["target"]["files"]:
                raise UpdateError("staged_bundle_changed")
            verify_bundle(pending, release["version"])
            if self.bundle.is_symlink() or not self.bundle.parent.is_dir() or not os.access(self.bundle.parent, os.W_OK):
                raise UpdateError("installation_directory_not_writable")
            descriptor = os.open(self.state_dir / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            holder = None
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise UpdateError("operation_busy") from None
                holder = Path(tempfile.mkdtemp(prefix=".shiliao-update-", dir=self.bundle.parent))
                staged, rollback = holder / "new.app", holder / "previous.app"
                shutil.copytree(pending, staged, symlinks=True)
                if inventory(staged) != release["target"]["files"]:
                    raise UpdateError("staged_bundle_changed")
                verify_bundle(staged, release["version"])
                pid = os.getpid() if parent_pid is None else parent_pid
                if type(pid) is not int or pid < 0:
                    raise UpdateError("invalid_parent_pid")
                helper = self.root / "install-helper.sh"
                checks = holder / "SHA256SUMS"
                checks.write_text("".join(row["sha256"] + "  " + row["path"] + "\n"
                                          for row in release["target"]["files"] if row["type"] == "file"))
                script = install_script(self.bundle, staged, rollback, checks, self.root / "install-result.json",
                                        pid, descriptor, release["version"], release["target"]["files"])
                helper.write_text(script)
                helper.chmod(0o700)
                process = subprocess.Popen(["/bin/sh", str(helper)], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           pass_fds=(descriptor,), start_new_session=True)
                self._set(status="installing", error=None)
                return {**self.status(), "helper_pid": process.pid}
            except BaseException:
                if holder is not None:
                    shutil.rmtree(holder, ignore_errors=True)
                raise
            finally:
                os.close(descriptor)


def install_script(bundle, staged, rollback, checks, result, parent_pid, descriptor, version, files):
    """Build a small fixed-command helper, independent of the replaced Python."""
    quote = lambda value: shlex.quote(str(value))
    ready = canonical({"status": "installed", "version": version, "recovery_bundle": str(rollback)}).decode()
    failed = canonical({"status": "error", "error": "installation_failed", "recovery_bundle": str(rollback)}).decode()
    lines = ["#!/bin/sh", "umask 077", "set -u", "bundle=" + quote(bundle), "staged=" + quote(staged),
             "rollback=" + quote(rollback), "result=" + quote(result),
             "failed() {", "  printf '%s\\n' " + quote(failed) + ' > "$result.tmp"',
             '  /bin/mv -f "$result.tmp" "$result"', "  exit 1", "}",
             "count=0", f"while [ {parent_pid} -gt 0 ] && /bin/kill -0 {parent_pid} 2>/dev/null; do",
             '  count=$((count+1)); [ "$count" -lt 120 ] || failed', "  /bin/sleep 1", "done",
             '[ ! -L "$bundle" ] && [ -d "$bundle" ] && [ ! -L "$staged" ] || failed',
             '( cd "$staged" && /usr/bin/shasum -a 256 -c ' + quote(checks) + ' >/dev/null 2>&1 ) || failed']
    for row in files:
        if row["type"] == "symlink":
            path = staged / row["path"]
            lines.append('[ "$(/usr/bin/readlink ' + quote(path) + ')" = ' + quote(row["target"]) + ' ] || failed')
    lines += ['/usr/bin/codesign --verify --deep --strict "$staged" 2>/dev/null || failed',
              '/bin/mv "$bundle" "$rollback" || failed',
              'if ! /bin/mv "$staged" "$bundle"; then /bin/mv "$rollback" "$bundle"; failed; fi',
              'if ! /usr/bin/codesign --verify --deep --strict "$bundle" 2>/dev/null; then',
              '  /bin/mv "$bundle" "$staged"; /bin/mv "$rollback" "$bundle"; failed', 'fi',
              "printf '%s\\n' " + quote(ready) + ' > "$result.tmp"', '/bin/mv -f "$result.tmp" "$result"',
              f"exec {descriptor}>&-", 'if ! /usr/bin/open -n "$bundle"; then',
              # LaunchServices returning does not prove successful application
              # startup. Retain the old bundle for explicit recovery instead of
              # racing a newly launched scheduled job with another replacement.
              '  failed', 'fi', 'exit 0', '']
    return "\n".join(lines)
