"""Resolve and archive local WeChat media independently of the live app."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import errno
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time

from vault.media_codecs import MediaCodecError, decode_image, discover_image_keys, convert_audio, prepare_video
from vault.wechat_media import MediaLocator
from vault.media_store import MAX_OBJECT, MIMES


THUMBNAIL_DETAIL = "仅本机缩略图，未找到完整原图。"
MPO_DETAIL = "多画面照片，当前显示首张；完整原文件已保留。"
MAX_PENDING_INPUT_BYTES = 512 * 1024 * 1024
MAX_PENDING_ITEMS = 48
SYSTEM_IO_ERRORS = {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE}


DETAILS = {
    "missing": "本机没有找到已完整下载的媒体，原消息仍已保留。",
    "ambiguous": "找到了多个候选文件，为避免显示错误内容，暂未关联。",
    "unsafe": "媒体路径校验未通过。",
    "unsupported": "该媒体格式目前无法完整解码，原消息仍已保留。",
    "error": "媒体读取未完成，可在退出微信后再次更新。",
    "locked": "该图片需要有效的本机图片密钥才能打开。",
}


def _prepare(locator, located, kind, keys, work):
    folder = Path(tempfile.mkdtemp(prefix="item-", dir=work))
    original = folder / "original"
    failure = "unsupported"
    for candidate in located["candidates"]:
        try:
            for entry in folder.iterdir():
                if entry.is_file():
                    entry.unlink()
            if "voice_data" in candidate:
                original.write_bytes(candidate["voice_data"])
                original.chmod(0o600)
            else:
                with locator.open_candidate(candidate) as inp, original.open("wb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    shutil.copyfileobj(inp, out, 1024 * 1024)
            detail = ""
            originals = []
            if kind == "image":
                output = folder / "image"
                preserved = folder / "original.wxgf"
                info = decode_image(original, output, keys=keys, original_destination=preserved)
                if candidate.get("thumbnail"):
                    detail = THUMBNAIL_DETAIL
                if info.get("original_format") == "mpo":
                    detail += MPO_DETAIL
                if info.get("original_format") == "wxgf":
                    if not info.get("original_saved"):
                        failure = "unsupported"
                        continue
                    originals.append((preserved, "application/octet-stream", "original.wxgf"))
                    detail += "首帧预览；此 WXGF 源文件已保存在归档中。"
            elif kind == "audio":
                output = folder / "voice.flac"
                info = convert_audio(original, output, format="flac")
                originals.append((original, "audio/silk" if info.get("source_silk") else "application/octet-stream", "original.voice"))
            else:
                output = folder / "video.mp4"
                info = prepare_video(original, output)
            if info.get("mime_type") not in MIMES.get(kind, set()) or not 0 < output.stat().st_size <= MAX_OBJECT:
                failure = "unsupported"
                continue
            return {"folder": folder, "status": "ready", "path": output,
                    "mime_type": info["mime_type"], "filename": kind + "." + info["format"],
                    "detail": detail, "originals": originals}
        except MediaCodecError as error:
            if error.code in {"insufficient_space", "media_io_failed", "dependency_missing"}:
                raise
            failure = "locked" if error.code == "image_key_missing" else "unsupported"
        except OSError as error:
            if error.errno in SYSTEM_IO_ERRORS:
                raise
            failure = "error"
        except ValueError:
            failure = "error"
    return {"folder": folder, "status": failure, "detail": DETAILS[failure]}


def import_media(decrypted_root, account_root, library, account_id, progress=None):
    account_key = hashlib.sha256(str(account_id).encode()).hexdigest()
    keys = discover_image_keys(account_root)
    reusable = library.media.begin_import(account_key)
    counts = {"ready": 0, "missing": 0, "locked": 0, "unsupported": 0, "error": 0, "skipped": 0}
    processed = 0
    last_report = 0.0

    def report(force=False):
        nonlocal last_report
        current = time.monotonic()
        if progress and (force or current - last_report >= 1):
            last_report = current
            progress({"done_files": processed, "total_files": 0,
                      "current_file": f"正在整理媒体：已处理 {processed:,} 条，可打开 {counts['ready'] + counts['skipped']:,} 条",
                      "media": dict(counts)})

    with tempfile.TemporaryDirectory(prefix="media-import-", dir=library.state_dir) as work:
        with MediaLocator(decrypted_root, account_root) as locator, library.connect() as lookup:
            with ThreadPoolExecutor(max_workers=min(6, os.cpu_count() or 2), thread_name_prefix="media-convert") as executor:
                pending = []
                pending_bytes = 0
                waiting = []
                waiting_bytes = 0

                def flush_batch(items):
                    nonlocal processed
                    # Decoder subprocesses and their I/O must finish before
                    # acquiring SQLite's write transaction.
                    completed = [(message_id, kind, revision, preserve, future.result() if future is not None else fallback)
                                 for message_id, kind, revision, preserve, future, fallback in items]
                    batch = library.media.batch() if hasattr(library.media, "batch") else nullcontext()
                    with batch:
                        for message_id, kind, revision, preserve, prepared in completed:
                            try:
                                status = prepared["status"]
                                if status == "ready":
                                    asset_id = library.media.store_file(prepared["path"], kind, prepared["mime_type"],
                                                                        prepared["filename"], prepared["detail"])
                                    for path, mime, name in prepared["originals"]:
                                        library.media.store_file(path, "original", mime, name)
                                    library.media.link(message_id, kind, asset_id)
                                    library.media.record_import_revision(message_id, kind, revision)
                                    counts["ready"] += 1
                                elif preserve:
                                    # An unsuccessful upgrade must not remove
                                    # a valid preview for this exact revision.
                                    counts["skipped"] += 1
                                else:
                                    status = status if status in counts else "unsupported"
                                    library.media.unavailable(message_id, kind, status, prepared.get("detail", DETAILS[status]))
                                    library.media.record_import_revision(message_id, kind, revision)
                                    counts[status] += 1
                                processed += 1
                                report()
                            finally:
                                if prepared.get("folder"):
                                    shutil.rmtree(prepared["folder"])
                def flush_waiting():
                    nonlocal waiting, waiting_bytes
                    if waiting:
                        flush_batch(waiting)
                        waiting = []
                        waiting_bytes = 0

                def rotate_pending():
                    nonlocal waiting, waiting_bytes, pending, pending_bytes
                    # While the main thread stores the previous batch, the
                    # next batch can decode. There are at most two bounded
                    # batches, and only this thread ever writes to SQLite.
                    flush_waiting()
                    waiting, waiting_bytes = pending, pending_bytes
                    pending = []
                    pending_bytes = 0

                for ref in locator.iter_media_messages():
                    row = lookup.execute("""SELECT message_id,source_digest FROM wechat_source_messages
                        WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?""",
                        (account_key, ref["database_name"], ref["table_name"].lower(), str(ref["local_id"]))).fetchone()
                    if row is None:
                        continue
                    message_id, revision, kind = row[0], row[1], ref["kind"]
                    existing = reusable.get((message_id, kind))
                    same_revision = existing is not None and existing["source_digest"] == revision
                    if same_revision and not existing["detail"].startswith(THUMBNAIL_DETAIL):
                        counts["skipped"] += 1
                        processed += 1
                        report()
                        continue
                    try:
                        located = locator.locate(ref)
                    except OSError as error:
                        if error.errno in SYSTEM_IO_ERRORS:
                            raise
                        located = {"status": "error"}
                    except ValueError:
                        located = {"status": "error"}
                    if located["status"] == "ready":
                        candidates = located["candidates"]
                        if same_revision and candidates and all(candidate.get("thumbnail") for candidate in candidates):
                            counts["skipped"] += 1
                            processed += 1
                            report()
                            continue
                        input_bytes = max((candidate.get("size", len(candidate.get("voice_data", b"")))
                                           for candidate in candidates), default=0)
                        # The two batches share one input budget. A single
                        # oversized file runs alone rather than blocking
                        # imports of valid, large local videos forever.
                        if waiting and waiting_bytes + pending_bytes + input_bytes > MAX_PENDING_INPUT_BYTES:
                            flush_waiting()
                        if pending and pending_bytes + input_bytes > MAX_PENDING_INPUT_BYTES:
                            flush_batch(pending)
                            pending = []
                            pending_bytes = 0
                        future = executor.submit(_prepare, locator, located, kind, keys, work)
                        pending.append((message_id, kind, revision, same_revision, future, None))
                        pending_bytes += input_bytes
                    else:
                        status = located["status"]
                        pending.append((message_id, kind, revision, same_revision, None,
                                        {"status": status if status in counts else "unsupported", "detail": DETAILS.get(status, DETAILS["error"])}))
                    if len(pending) >= MAX_PENDING_ITEMS or pending_bytes >= MAX_PENDING_INPUT_BYTES:
                        rotate_pending()
                flush_waiting()
                if pending:
                    flush_batch(pending)
    report(True)
    return {**counts, "processed": processed, "storage": library.media.stats()}
