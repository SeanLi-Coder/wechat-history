"""Decode local WeChat media into private, independently readable copies.

DAT layout and macOS filename derivation are documented by the MIT-licensed
media_pipeline.py at gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f.
No process inspection, network requests, transcription, or source writes occur.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import warnings


MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
MAX_ANIMATION_PIXELS = 180_000_000
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_AUDIO_SECONDS = 600
MAX_VIDEO_BYTES = 8 * 1024 * 1024 * 1024
V1_MAGIC = bytes.fromhex("070856310807")
V2_MAGIC = bytes.fromhex("070856320807")


class MediaCodecError(RuntimeError):
    """An error safe to expose without source filenames or secret keys."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, repr=False)
class ImageKey:
    aes_key: bytes
    xor_key: int


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _tool(name):
    for base in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        path = base / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    path = shutil.which(name)
    if path:
        return path
    raise MediaCodecError("dependency_missing", f"The local {name} decoder is not installed.")


def _run(arguments, timeout=120):
    environment = os.environ.copy()
    environment.pop("FFREPORT", None)
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, env=environment, check=False)
    except (OSError, subprocess.SubprocessError):
        raise MediaCodecError("conversion_failed", "The local media decoder did not complete.") from None
    if result.returncode:
        raise MediaCodecError("conversion_failed", "The local media decoder could not read this media file.")
    return result.stdout


@contextmanager
def _conversion(source, destination, maximum, *, original_destination=None):
    source, destination = Path(source).expanduser().absolute(), Path(destination).expanduser().absolute()
    original_destination = (Path(original_destination).expanduser().absolute()
                            if original_destination is not None else None)
    try:
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise MediaCodecError("unsafe_source", "The media source must be a regular file, not a symbolic link.")
        if not 0 < before.st_size <= maximum:
            raise MediaCodecError("invalid_size", "The media source is empty or exceeds the supported size limit.")
        if source.resolve() == destination.resolve() or os.path.lexists(destination):
            raise MediaCodecError("destination_exists", "The media destination already exists and was not changed.")
        if original_destination is not None:
            if (original_destination.resolve() in (source.resolve(), destination.resolve())
                    or os.path.lexists(original_destination)):
                raise MediaCodecError("destination_exists", "The media destination already exists and was not changed.")
            original_destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if shutil.disk_usage(destination.parent).free < before.st_size * 2 + 1024 * 1024:
            raise MediaCodecError("insufficient_space", "There is not enough space for a private media conversion.")
        with tempfile.TemporaryDirectory(prefix=".shiliao-media-", dir=destination.parent) as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            staged = directory / "source.bin"
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if _signature(os.fstat(descriptor)) != _signature(before):
                    raise MediaCodecError("source_changed", "The media source changed before conversion.")
                with staged.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    copied = 0
                    while chunk := os.read(descriptor, 1024 * 1024):
                        copied += len(chunk)
                        if copied > before.st_size:
                            raise MediaCodecError("source_changed", "The media source changed during conversion.")
                        output.write(chunk)
                if copied != before.st_size or _signature(os.fstat(descriptor)) != _signature(before):
                    raise MediaCodecError("source_changed", "The media source changed during conversion.")
            finally:
                os.close(descriptor)
            output = directory / "converted.bin"
            output.touch(mode=0o600, exist_ok=False)
            yield staged, output
            outputs = [(output, destination)]
            original = directory / "original.bin"
            if original_destination is not None and original.exists():
                outputs.append((original, original_destination))
            for pending, target in outputs:
                if not pending.is_file() or not pending.stat().st_size:
                    raise MediaCodecError("conversion_failed", "The media conversion produced no readable output.")
                pending.chmod(0o600)
                with pending.open("rb") as stream:
                    os.fsync(stream.fileno())
            if _signature(source.lstat()) != _signature(before):
                raise MediaCodecError("source_changed", "The media source changed during conversion.")
            published = []
            try:
                for pending, target in outputs:
                    identity = pending.stat()
                    os.link(pending, target)
                    published.append((target, identity.st_dev, identity.st_ino))
            except BaseException:
                for target, device, inode in reversed(published):
                    try:
                        current = target.lstat()
                        if (current.st_dev, current.st_ino) == (device, inode):
                            target.unlink()
                    except OSError:
                        pass
                raise
    except MediaCodecError:
        raise
    except FileExistsError:
        raise MediaCodecError("destination_exists", "The media destination already exists and was not changed.") from None
    except OSError:
        raise MediaCodecError("media_io_failed", "Media files could not be read or written.") from None


def discover_image_keys(account_root, app_data=None):
    """Return private, unverified DAT candidates from local account/cache names.

    Candidates are accepted only when decode_image validates the complete image.
    This does not extract process memory or save/print credentials.
    """
    account = Path(account_root).expanduser()
    if account.name == "db_storage":
        account = account.parent
    own = account.name
    identities = {own, re.sub(r"_[a-zA-Z0-9]{4}$", "", own)}
    if own.lower().startswith("wxid_"):
        identities.add("_".join(own.split("_")[:2]))
    bases = {Path(app_data).expanduser()} if app_data is not None else {
        Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"
    }
    if app_data is None:
        for parent in account.parents:
            if parent.name == "xwechat_files":
                bases.add(parent.parent / "app_data")
    codes = set()
    for base in bases:
        for relative in ("net/kvcomm", "ilink/kvcomm"):
            for index, path in enumerate((base / relative).glob("key_*_*.statistic")):
                if index >= 10000:
                    break
                match = re.fullmatch(r"key_([0-9]{1,20})_.*\.statistic", path.name)
                if match:
                    codes.add(int(match[1]))
    result, seen = [], set()
    for code in sorted(codes):
        for identity in sorted(identities):
            aes = hashlib.md5((str(code) + identity).encode("utf-8")).hexdigest()[:16].encode("ascii")
            candidate = (aes, code & 255)
            if candidate not in seen:
                result.append(ImageKey(*candidate))
                seen.add(candidate)
            if len(result) >= 128:
                return result
    return result


def _key(value):
    if not isinstance(value, ImageKey) or not isinstance(value.aes_key, bytes) or len(value.aes_key) != 16:
        raise MediaCodecError("invalid_image_key", "An image key requires 16 AES bytes and one XOR byte.")
    if type(value.xor_key) is not int or not 0 <= value.xor_key <= 255:
        raise MediaCodecError("invalid_image_key", "An image key requires 16 AES bytes and one XOR byte.")
    return value


@lru_cache(maxsize=256)
def _xor_table(value):
    return bytes(item ^ value for item in range(256))


def _xor(data, value):
    return data.translate(_xor_table(value))


def _dat_decode(data, key):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    if len(data) < 15:
        raise ValueError
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    encrypted_size = aes_size + (16 - aes_size % 16) if aes_size else 0
    if encrypted_size + xor_size > len(data) - 15:
        raise ValueError
    prefix = b""
    if encrypted_size:
        decryptor = Cipher(algorithms.AES(key.aes_key), modes.ECB()).decryptor()
        padded = decryptor.update(data[15:15 + encrypted_size]) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        prefix = unpadder.update(padded) + unpadder.finalize()
        if len(prefix) != aes_size:
            raise ValueError
    tail = data[15 + encrypted_size:]
    return prefix + (tail[:-xor_size] + _xor(tail[-xor_size:], key.xor_key)
                     if xor_size else tail)


def _image_info(data):
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            warnings.filterwarnings("error", message="Image appears to be a malformed MPO file.*", category=UserWarning)
            warnings.filterwarnings("error", message="Truncated File Read.*", category=UserWarning)
            with Image.open(io.BytesIO(data)) as picture:
                image_format = picture.format
                if image_format not in ("JPEG", "MPO", "PNG", "GIF", "WEBP", "BMP", "TIFF"):
                    raise ValueError
                if image_format == "MPO" and not data.startswith(b"\xff\xd8\xff"):
                    raise ValueError
                if picture.width * picture.height > MAX_IMAGE_PIXELS:
                    raise ValueError
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                frames = getattr(picture, "n_frames", 1)
                if not 1 <= frames <= 1000:
                    raise ValueError
                width, height = picture.size
                total_pixels = 0
                for index in range(frames):
                    picture.seek(index)
                    pixels = picture.width * picture.height
                    total_pixels += pixels
                    if pixels > MAX_IMAGE_PIXELS or total_pixels > MAX_ANIMATION_PIXELS:
                        raise ValueError
                    picture.load()
        extension, mime = {"JPEG": ("jpg", "image/jpeg"), "MPO": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                           "GIF": ("gif", "image/gif"), "WEBP": ("webp", "image/webp"),
                           "BMP": ("bmp", "image/bmp"), "TIFF": ("tiff", "image/tiff")}[image_format]
        info = {"format": extension, "mime_type": mime, "width": width, "height": height,
                "frames": frames, "first_frame_only": image_format == "MPO" and frames > 1, "bytes": len(data)}
        if image_format == "MPO":
            info["original_format"] = "mpo"
        return info
    except Exception:
        raise MediaCodecError("invalid_image", "The image format or decryption key could not be validated.") from None


def _wxgf_image(data, output):
    positions = [match.start() for match in re.finditer(b"\x00\x00\x00\x01\x40|\x00\x00\x01\x40", data)]
    if not positions:
        raise MediaCodecError("unsupported_image", "This WXGF image has no supported HEVC stream.")
    stream = output.parent / "image.hevc"
    stream.write_bytes(data[min(positions):])
    stream.chmod(0o600)
    _run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
          "-f", "hevc", "-i", str(stream), "-frames:v", "1", "-map_metadata", "-1",
          "-f", "image2", "-vcodec", "png", "-y", str(output)], timeout=30)
    info = _image_info(output.read_bytes())
    info["first_frame_only"] = True
    return info


def decode_image(source, destination, key=None, *, keys=None, preserve_wxgf=False,
                 original_destination=None):
    """Decode plain, legacy XOR, V1 or V2 DAT; preserve standard image bytes.

    Supply ImageKey or an iterable of ImageKey candidates for V2. The destination
    name is exact; use the returned format/mime_type rather than its suffix.
    WXGF returns a clearly marked PNG first-frame preview, not an animation.
    MPO retains every original byte and validates every frame; original_format
    is mpo and first_frame_only describes the browser's first-picture display.
    Set preserve_wxgf=True to keep its validated, decrypted original container
    instead; that result has playable=False and must not be served as an image.
    Alternatively, original_destination saves the original WXGF alongside its
    preview with one decode. Both targets must be new paths on the same filesystem;
    a failed publication removes any file published by this call. Standard images
    do not create this companion. original_saved reports whether it was created.
    """
    if preserve_wxgf and original_destination is not None:
        raise MediaCodecError("invalid_destination", "Choose an original-only output or a preview with its original.")
    candidates = ([_key(key)] if key is not None else []) + ([_key(value) for value in keys] if keys is not None else [])
    if len(candidates) > 128:
        raise MediaCodecError("invalid_image_key", "Too many image key candidates were supplied.")
    with _conversion(source, destination, MAX_IMAGE_BYTES,
                     original_destination=original_destination) as (staged, output):
        data = staged.read_bytes()
        if data.startswith(V2_MAGIC):
            if not candidates:
                raise MediaCodecError("image_key_missing", "This V2 image needs a locally verified image key.")
            variants = ((_dat_decode, value) for value in candidates)
            mode = "v2"
        elif data.startswith(V1_MAGIC):
            preferred = [data[-1] ^ value for value in (0xD9, 0x82, 0x3B)]
            order = list(dict.fromkeys(preferred + list(range(256))))
            variants = ((_dat_decode, ImageKey(b"cfcd208495d565ef", xor)) for xor in order)
            mode = "v1"
        elif data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"BM", b"II*\x00", b"MM\x00*", b"wxgf")):
            variants = ((lambda raw, unused: raw, None),)
            mode = "plain"
        else:
            xors = list(dict.fromkeys(data[0] ^ magic for magic in (0xFF, 0x89, 0x47, 0x52, 0x42, 0x49, 0x4D)))
            variants = ((_xor, xor) for xor in xors)
            mode = "legacy_xor"
        for decoder, candidate in variants:
            try:
                raw = decoder(data, candidate)
                if raw.startswith(b"wxgf"):
                    info = _wxgf_image(raw, output)
                    info["original_format"] = "wxgf"
                    if preserve_wxgf:
                        output.write_bytes(raw)
                        info.update(format="wxgf", mime_type="application/octet-stream", frames=None,
                                    first_frame_only=False, bytes=len(raw), playable=False)
                    elif original_destination is not None:
                        original = output.with_name("original.bin")
                        original.touch(mode=0o600, exist_ok=False)
                        original.write_bytes(raw)
                        info["original_saved"] = True
                else:
                    info = _image_info(raw)
                    output.write_bytes(raw)
                info.setdefault("playable", True)
                info.setdefault("original_saved", False)
                return {**info, "decoder": mode}
            except (ValueError, MediaCodecError):
                output.unlink(missing_ok=True)
                continue
            except ImportError:
                raise MediaCodecError("dependency_missing", "Pillow and cryptography are required for image decoding.") from None
        raise MediaCodecError("invalid_image", "The image format or decryption key could not be validated.")


def _probe(path):
    output = _run([_tool("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
                   "-show_entries", "format=duration,format_name:stream=codec_name,codec_type,width,height",
                   "-of", "json", str(path)], timeout=30)
    try:
        result = json.loads(output)
        duration = float(result["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return result, duration
    except (ValueError, KeyError, TypeError):
        raise MediaCodecError("invalid_media", "The media duration or streams could not be validated.") from None


def convert_audio(source, destination, *, format="flac"):
    """Convert SILK/AMR or regular audio; FLAC preserves decoded PCM samples."""
    settings = {"flac": (["-c:a", "flac", "-compression_level", "8"], "audio/flac"),
                "wav": (["-c:a", "pcm_s16le"], "audio/wav"),
                "mp3": (["-c:a", "libmp3lame", "-q:a", "3"], "audio/mpeg"),
                "m4a": (["-c:a", "aac", "-b:a", "64k"], "audio/mp4"),
                "opus": (["-c:a", "libopus", "-b:a", "32k"], "audio/ogg")}
    if format not in settings:
        raise MediaCodecError("unsupported_format", "The requested audio output format is unsupported.")
    with _conversion(source, destination, MAX_AUDIO_BYTES) as (staged, output):
        with staged.open("rb") as stream:
            header = stream.read(16)
        is_silk = header.startswith((b"\x02#!SILK_V3", b"#!SILK_V3"))
        input_options = []
        audio_source = staged
        if is_silk:
            try:
                import importlib.util
                available = importlib.util.find_spec("pilk") is not None
            except (ImportError, ValueError):
                available = False
            if not available:
                raise MediaCodecError("dependency_missing", "The local pilk SILK decoder is not installed.")
            audio_source = output.parent / "audio.pcm"
            code = ("import os,resource,sys,pilk; os.umask(0o077); "
                    f"resource.setrlimit(resource.RLIMIT_FSIZE,({MAX_AUDIO_SECONDS * 48000},{MAX_AUDIO_SECONDS * 48000})); "
                    "pilk.decode(sys.argv[1],sys.argv[2],pcm_rate=24000)")
            _run([sys.executable, "-c", code, str(staged), str(audio_source)], timeout=60)
            size = audio_source.stat().st_size
            if size <= 0 or size % 2 or size >= MAX_AUDIO_SECONDS * 48000:
                raise MediaCodecError("invalid_audio", "The decoded voice exceeds the supported duration or is invalid.")
            input_options = ["-f", "s16le", "-ar", "24000", "-ac", "1"]
        else:
            if not header.startswith((b"#!AMR", b"RIFF", b"fLaC", b"OggS", b"ID3", b"\xff")) and header[4:8] != b"ftyp":
                raise MediaCodecError("unsupported_audio", "The source is not a supported local audio container.")
            info, duration = _probe(staged)
            if duration > MAX_AUDIO_SECONDS or not any(item.get("codec_type") == "audio" for item in info.get("streams", [])):
                raise MediaCodecError("invalid_audio", "The audio is missing or exceeds the supported duration.")
        codec_options, mime = settings[format]
        container = {"m4a": "mp4", "opus": "ogg"}.get(format, format)
        _run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
              *input_options, "-i", str(audio_source), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
              *codec_options, "-f", container, "-y", str(output)])
        _, duration = _probe(output)
        return {"format": format, "mime_type": mime, "duration": duration,
                "bytes": output.stat().st_size, "lossless": format in ("flac", "wav"), "source_silk": is_silk}


def prepare_video(source, destination):
    """Remux a local MP4/MOV into standalone MP4 without re-encoding streams."""
    with _conversion(source, destination, MAX_VIDEO_BYTES) as (staged, output):
        with staged.open("rb") as stream:
            header = stream.read(16)
        if header[4:8] != b"ftyp":
            raise MediaCodecError("unsupported_video", "The source is not a supported MP4 or MOV video.")
        info, duration = _probe(staged)
        videos = [item for item in info.get("streams", []) if item.get("codec_type") == "video"]
        if not videos:
            raise MediaCodecError("invalid_video", "The local file has no readable video stream.")
        _run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
              "-i", str(staged), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
              "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", "-y", str(output)], timeout=300)
        verified, output_duration = _probe(output)
        if not any(item.get("codec_type") == "video" for item in verified.get("streams", [])) or abs(output_duration - duration) > 1:
            raise MediaCodecError("invalid_video", "The remuxed video did not preserve the source duration.")
        return {"format": "mp4", "mime_type": "video/mp4", "duration": output_duration,
                "width": videos[0].get("width"), "height": videos[0].get("height"),
                "codec": videos[0].get("codec_name"), "bytes": output.stat().st_size, "lossless": True}
