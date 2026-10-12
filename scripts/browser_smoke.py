#!/usr/bin/env python3
"""Exercise the real local UI using disposable, entirely synthetic data.

The schedule remains disabled throughout this test. System source discovery,
WeChat process detection, and launchd are replaced with isolated test stubs.
No WeChat data is read or backed up. Screenshots contain synthetic records only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import wave
import zlib
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def serve_fixture(state: Path) -> None:
    """Run the application against one disposable fixture without OS discovery."""
    state = state.resolve()
    marker = state / ".synthetic-browser-state"
    if (not state.parent.name.startswith("shiliao-browser-smoke-")
            or not marker.is_file() or marker.read_text() != "synthetic browser smoke"):
        raise ValueError("Only a generated synthetic browser state may be served")
    sys.path.insert(0, str(PROJECT))
    import app

    config = json.loads((state / "config.json").read_text())

    def blocked(*_args, **_kwargs):
        raise RuntimeError("System actions are disabled in browser fixtures")

    app.discover_sources = lambda: [{"path": config["source"], "label": "Synthetic source",
                                    "exists": True, "readable": True, "kind": "synthetic"}]
    app.wechat_running = lambda: False
    app.Scheduler.status = lambda self: {"installed": False, "loaded": False, "error": None}
    app.Scheduler.install = blocked
    app.Scheduler.uninstall = blocked
    original_post = app.Handler.do_POST

    def fixture_post(handler):
        if urlsplit(handler.path).path in {"/api/pick-folder", "/api/wechat/import", "/api/wechat/keys"}:
            return handler.response({"error": "System actions are disabled in browser fixtures"}, 405)
        return original_post(handler)

    app.Handler.do_POST = fixture_post
    sys.argv = [str(PROJECT / "app.py"), "--state-dir", str(state), "--no-browser"]
    app.main()


def open_management(page):
    management = page.locator("#reader-management")
    if not management.evaluate("node => node.open"):
        management.locator("summary").click()
    return management


def synthetic_media(work: Path) -> dict:
    """Create abstract fixtures without reading any personal media."""
    width, height = 960, 640

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))

    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            pixels.extend((89 + x * 95 // width, 129 + y * 55 // height, 102))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b""))
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg is required to create the synthetic video fixture.")
    video = work / "synthetic-video.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=0x7eac89:s=320x180:d=1:r=12", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    return {"smoke-image": ("image/png", png), "smoke-audio": ("audio/wav", audio.getvalue()),
            "smoke-video": ("video/mp4", video.read_bytes())}


def check_synthetic_media(page, work: Path, state: Path, checks: list) -> None:
    """Play synthetic objects through the real store, cookie and HTTP endpoints."""
    sys.path.insert(0, str(PROJECT))
    from vault.library import Library

    fixtures = synthetic_media(work)
    library = Library(state)
    conversation = next(item for item in library.conversations()["conversations"] if "周末计划" in item["title"])
    message_id = next(message["id"] for message in library.messages(conversation["id"])["messages"]
                      if message["text"].startswith("周末一起去公园"))
    for identifier, kind in (("smoke-image", "image"), ("smoke-audio", "audio"), ("smoke-video", "video")):
        mime, body = fixtures[identifier]
        source = work / identifier
        source.write_bytes(body)
        asset_id = library.media.store_file(source, kind, mime, filename=identifier)
        library.media.link(message_id, kind, asset_id)
    ready_media = library.media.for_messages([message_id])[message_id]
    ready_media.sort(key=lambda item: ["image", "audio", "video"].index(item["kind"]))
    media_requests = []
    unexpected_requests = []
    base_origin = urlsplit(page.url).netloc

    def record_request(request):
        parsed = urlsplit(request.url)
        if parsed.netloc != base_origin:
            unexpected_requests.append(request.url)
        if parsed.path.startswith("/api/media/"):
            media_requests.append(request.url)
            assert not parsed.query, "Media URLs must not contain tokens or query parameters."

    page.on("request", record_request)

    media = list(ready_media)
    media.extend({"id": "smoke-" + status, "kind": "image", "status": status,
                  "detail": "Private SQL implementation detail must remain hidden"}
                 for status in ("missing", "locked", "unsupported", "error"))
    media.extend([
        {"id": "smoke-external", "kind": "image", "status": "ready", "url": "https://example.invalid/private.png"},
        {"id": "smoke-query", "kind": "image", "status": "ready", "url": "/api/media/smoke-image?token=forbidden"},
        {"id": "smoke-broken", "kind": "audio", "status": "ready", "url": "/api/media/" + "f" * 64},
    ])

    def inject_media(route):
        response = route.fetch()
        payload = response.json()
        for message in payload.get("messages", []):
            if message["id"] == message_id:
                message["media"] = media
        route.fulfill(response=response, json=payload)

    page.route("**/api/conversations/*/messages?*", inject_media)
    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
    expect(page.locator(".media-card")).to_have_count(len(media))
    expect(page.locator(f'.message-item[data-message-id="{message_id}"] .message-bubble')).to_contain_text("公园")
    image = page.locator(".message-image")
    image.scroll_into_view_if_needed()
    wait_until(lambda: image.evaluate("node => node.naturalWidth === 960"), "synthetic image decoding")
    expect(image).to_have_attribute("loading", "lazy")
    image_button = page.locator(".media-image-button")
    image_button.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#media-preview-dialog")).to_be_visible()
    expect(page.locator("#media-preview-meta")).to_contain_text("960 × 640")
    page.locator("#media-preview-size").click()
    expect(page.locator("#media-preview-size")).to_have_attribute("aria-pressed", "true")
    assert page.locator(".media-preview-image").evaluate("image => image.getBoundingClientRect().width") == 960
    page.keyboard.press("Escape")
    expect(page.locator("#media-preview-dialog")).not_to_be_visible()
    expect(image_button).to_be_focused()
    image_button.click()
    page.locator("#close-media-preview").click()
    expect(page.locator("#media-preview-dialog")).not_to_be_visible()
    checks.append("synthetic_image_preview_keyboard_and_original_size")

    for kind in ("audio", "video"):
        player = page.locator(f"{kind}.media-player")
        expect(player).to_have_count(1)
        expect(player).to_have_attribute("preload", "metadata")
        assert player.evaluate("player => player.controls && !player.autoplay"), "Native playback controls are missing."
        wait_until(lambda: player.evaluate("node => node.readyState >= 1"), "synthetic media metadata")
        assert player.evaluate("player => player.duration") > 0, "The synthetic media metadata could not be decoded."
        player.evaluate("async player => { await player.play(); player.pause(); }")
        player.evaluate("player => { player.currentTime = 0.5; }")
        assert player.evaluate("player => Math.abs(player.currentTime - 0.5) < 0.1"), "Media seeking failed."
    checks.append("synthetic_audio_and_video_metadata_playback_and_seek")
    cookies = page.context.cookies()
    media_cookie = next(cookie for cookie in cookies if cookie["name"] == "shiliao_media")
    assert media_cookie["httpOnly"] and media_cookie["path"] == "/api/media/"
    assert media_cookie["sameSite"] == "Strict"
    assert "shiliao_media" not in page.evaluate("document.cookie")
    partial = page.evaluate("""async url => {
        const response = await fetch(url, { headers: { Range: 'bytes=0-15' } });
        return { status: response.status, length: (await response.arrayBuffer()).byteLength,
            range: response.headers.get('Content-Range') };
    }""", ready_media[0]["url"])
    assert partial["status"] == 206 and partial["length"] == 16
    assert partial["range"].startswith("bytes 0-15/")
    assert page.evaluate("async () => (await fetch('/api/status')).status") == 401
    checks.append("real_media_store_httponly_cookie_and_http_range")
    expect(page.locator(".media-fallback")).to_have_count(7)
    expect(page.locator("#message-list")).to_contain_text("原文件不在本机")
    expect(page.locator("#message-list")).to_contain_text("暂不支持")
    assert "Private SQL" not in page.locator("#message-list").inner_text()
    assert not unexpected_requests, "Media rendering attempted an external network request."
    assert media_requests, "The native players did not request any local media."
    checks.append("media_failures_preserve_text_and_reject_external_or_token_urls")
    page.locator("#messages-scroll").evaluate("element => { element.scrollTop = 0; }")


def check_readable_archive(page, work: Path, state: Path, job_finished, checks: list) -> None:
    """Prove a saved ZIP can play media after every original fixture is deleted."""
    page.unroute("**/api/conversations/*/messages?*")
    clear_toasts(page)
    open_management(page)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/export") as exported:
        page.locator('[data-action="reader-export"]').click()
    assert exported.value.status == 202, "The reader export did not start."
    result = job_finished("reader_export")
    archive = Path(result["path"])
    assert archive.is_relative_to(work) and archive.is_file(), "The readable ZIP was not saved to the test directory."
    assert result["objects"] == 3, "The readable ZIP did not include all three media objects."
    expect(page.locator('[data-action="reader-open"]')).to_be_enabled()

    for folder in (state / "media/objects", state / "media/cache"):
        assert folder.is_relative_to(work)
        shutil.rmtree(folder)
    for name in ("smoke-image", "smoke-audio", "smoke-video", "synthetic-video.mp4"):
        (work / name).unlink()
    session = page.context.new_cdp_session(page)
    session.send("Network.clearBrowserCache")
    session.detach()

    def select_archive(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"path": str(archive)}))

    page.route("**/api/pick-archive", select_archive)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/archive") as opened:
        page.locator('[data-action="reader-open"]').click()
    assert opened.value.status == 202, "The readable ZIP did not start opening."
    job_finished("reader_open")
    expect(page.locator("#reader-source-title")).to_contain_text(archive.name)
    expect(page.locator("#reader-job")).to_be_hidden()
    expect(page.locator('[data-action="reader-live"]')).to_be_visible()
    expect(page.locator('[data-action="reader-export"]')).to_be_hidden()
    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
    expect(page.locator(".media-card")).to_have_count(3)
    page.locator(".message-image").scroll_into_view_if_needed()
    wait_until(lambda: page.locator(".message-image").evaluate("node => node.naturalWidth === 960"), "archived image decoding")
    for kind in ("audio", "video"):
        wait_until(lambda: page.locator(f"{kind}.media-player").evaluate("node => node.readyState >= 1"), "archived media metadata")
        page.locator(f"{kind}.media-player").evaluate("async player => { await player.play(); player.pause(); }")
    assert not (state / "media/objects").exists(), "Opening the ZIP unexpectedly restored the original media store."
    assert list((state / "opened-archives").glob("*/media/objects/*/*")), "The readable archive did not lazily extract media."
    checks.append("readable_zip_plays_media_after_original_source_and_store_removal")
    page.locator("#messages-scroll").evaluate("element => { element.scrollTop = 0; }")
    clear_toasts(page)


def redact(value: object, token: str = "") -> str:
    text = str(value)
    if token:
        text = text.replace(token, "[redacted]")
    return re.sub(r"#token=[^\s\"'<>]+", "#token=[redacted]", text)


def wait_until(check, description: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}.")


def clear_toasts(page):
    for _ in range(20):
        close = page.locator(".toast-close").first
        if close.count() == 0:
            break
        close.click(timeout=2000)


def go_to(page, name: str):
    page.locator(f'.nav-item[data-page="{name}"]').click()
    expect(page.locator(f"#page-{name}")).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")


def assert_newest_first(page):
    rows = page.locator(".message-item").evaluate_all("""nodes => nodes.map(node => ({
        id: Number(node.dataset.messageId), timestamp: node.querySelector('.message-meta time').dateTime
    }))""")
    keys = [(row["timestamp"], row["id"]) for row in rows]
    assert keys == sorted(keys, reverse=True), "Messages are not ordered by descending timestamp and ID"
    assert len({row["id"] for row in rows}) == len(rows), "Message pagination repeated an ID"


def check_mobile_width(page, name: str) -> dict:
    go_to(page, name)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    dimensions = page.evaluate("""() => ({
        page: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
        overflow: [...document.querySelectorAll('body *')].filter(element => {
            if (!element.getClientRects().length) return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && (rect.right > window.innerWidth + 1 || rect.left < -1);
        }).slice(0, 12).map(element => ({
            tag: element.tagName, id: element.id,
            class: typeof element.className === 'string' ? element.className : '',
            width: Math.round(element.getBoundingClientRect().width)
        }))
    })""")
    if dimensions["page"] > dimensions["viewport"] + 1:
        raise AssertionError(f"Horizontal overflow on {name}: {json.dumps(dimensions)}")
    return {"page": name, "document_width": dimensions["page"],
            "viewport_width": dimensions["viewport"]}


def run_smoke(chrome: Path | None, artifacts: Path) -> dict:
    if chrome is not None and not chrome.is_file():
        raise FileNotFoundError("The requested browser executable was not found.")
    if not (PROJECT / "static/app.js").is_file():
        raise FileNotFoundError("static/app.js must exist before running the browser smoke test.")
    artifacts.mkdir(parents=True, exist_ok=True)
    token = ""
    browser = None
    process = None
    checks = []
    screenshots = []
    page_errors = []
    with tempfile.TemporaryDirectory(prefix="shiliao-browser-smoke-") as temporary:
        work = Path(temporary).resolve()
        source, destination, state = work / "source", work / "archives", work / "state"
        source.mkdir()
        state.mkdir(mode=0o700)
        (state / ".synthetic-browser-state").write_text("synthetic browser smoke")
        (source / "messages.txt").write_text("Synthetic backup payload\n" * 1024)
        (source / "attachments").mkdir()
        (source / "attachments/sample.bin").write_bytes(bytes(range(256)) * 64)
        (source / "empty-directory").mkdir()
        config = {
            "source": str(source), "destination": str(work / "initial-archives"),
            "schedule_enabled": False, "schedule_time": "03:00", "interval": "daily",
            "include_media": True, "timezone": "Asia/Shanghai",
        }
        config_path = state / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
        export_path = work / "synthetic-chat.json"
        html_text = ('HTML 测试：<img src=x onerror="window.__smokeXss = true">'
                     '<script>window.__smokeXss = true</script>')
        document = {"conversations": [
            {"title": "合成测试 · 周末计划", "messages": [
                {"sender": "小林", "text": "周末一起去公园散步，备份测试样例。",
                 "timestamp": "2026-10-10T14:00:00+08:00"},
                {"sender": "我", "text": "好的，下午三点见。", "is_self": True,
                 "timestamp": "2026-10-10T14:01:00+08:00"},
                {"sender": "小林", "text": html_text,
                 "timestamp": "2026-10-10T14:02:00+08:00"},
            ]},
            {"title": "合成测试 · 工作札记", "messages": [
                {"sender": "同事", "text": "项目资料已经归档。",
                 "timestamp": "2026-10-09T11:00:00+08:00"},
            ]},
        ]}
        pagination_base = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
        pagination_messages = [{"sender": "合成测试", "text": f"第 {index:03d} 条合成分页记录",
                                "timestamp": (pagination_base + timedelta(minutes=index)).isoformat()}
                               for index in range(105)]
        pagination_messages[4]["timestamp"] = pagination_messages[5]["timestamp"]
        document["conversations"].append({"title": "合成分页 · 顺序校验", "messages": pagination_messages})
        expected_pages = [message["text"] for message in reversed(pagination_messages)]
        export_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        server_log_path = work / "server.log"
        try:
            with server_log_path.open("w+b") as server_log:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--serve-fixture", str(state)],
                    cwd=PROJECT, stdin=subprocess.DEVNULL,
                    stdout=server_log, stderr=subprocess.STDOUT,
                )

                def runtime_ready():
                    if process.poll() is not None:
                        raise RuntimeError("The test server exited: " + server_log_path.read_text())
                    try:
                        return json.loads((state / "runtime.json").read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        return None

                runtime = wait_until(runtime_ready, "the local test server")
                parsed = urlsplit(runtime["url"])
                token = parse_qs(parsed.fragment)["token"][0]
                base_url = f"{parsed.scheme}://{parsed.netloc}"
                with sync_playwright() as playwright:
                    options = {"headless": True, "args": ["--mute-audio", "--disable-background-networking", "--disable-component-update", "--disable-sync"]}
                    if chrome:
                        options["executable_path"] = str(chrome)
                    browser = playwright.chromium.launch(**options)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                                  locale="zh-CN", timezone_id="Asia/Shanghai",
                                                  reduced_motion="reduce", service_workers="block")
                    external_requests = []

                    def local_requests_only(route):
                        requested = urlsplit(route.request.url)
                        if f"{requested.scheme}://{requested.netloc}" != base_url:
                            external_requests.append(requested.path)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_requests_only)
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: page_errors.append(redact(error, token)))
                    dialogs = []

                    def dismiss_unexpected_dialog(dialog):
                        dialogs.append(dialog.type)
                        dialog.dismiss()

                    page.on("dialog", dismiss_unexpected_dialog)

                    def api_get(path):
                        response = context.request.get(base_url + path,
                                                       headers={"Authorization": "Bearer " + token})
                        if response.status != 200:
                            raise AssertionError(f"API read failed: {path}, HTTP {response.status}")
                        return response.json()

                    def job_finished(kind):
                        def check():
                            status = api_get("/api/status")
                            job = status.get("job")
                            if not job or job.get("type") != kind or job.get("status") == "running":
                                return None
                            if job["status"] != "done":
                                raise AssertionError(f"The {kind} job failed: {job.get('error', 'unknown error')}")
                            return job["result"]
                        return wait_until(check, f"the {kind} job", timeout=30)

                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status") as status_response:
                        page.goto(runtime["url"], wait_until="domcontentloaded")
                    if status_response.value.status != 200:
                        raise AssertionError("The initial authenticated status request failed.")
                    expect(page.locator("#page-overview")).to_be_visible()
                    expect(page.locator("#connection-banner")).to_be_hidden()
                    checks.append("authenticated_startup")

                    go_to(page, "settings")
                    page.locator("#source-path").fill(str(source))
                    page.locator("#destination-path").fill(str(destination))
                    page.locator("#schedule-enabled").uncheck()
                    page.locator("#schedule-time").fill("22:15")
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/config"
                                              and response.request.method == "POST") as save_response:
                        page.locator("#save-settings").click()
                    if save_response.value.status != 200:
                        raise AssertionError("The settings form did not save successfully.")
                    expect(page.locator("#settings-save-status")).to_have_text("设置已保存")
                    saved = json.loads(config_path.read_text())
                    assert saved["source"] == str(source), "The source setting was not saved."
                    assert saved["destination"] == str(destination), "The destination setting was not saved."
                    assert saved["schedule_time"] == "22:15", "The time setting was not saved."
                    assert saved["schedule_enabled"] is False, "Scheduling must remain disabled."
                    checks.append("settings_save_with_schedule_disabled")

                    go_to(page, "overview")
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/backup") as backup_response:
                        page.locator('#page-overview .hero-content [data-action="backup"]').click()
                    assert backup_response.value.status == 202, "The UI did not start a backup."
                    snapshot = job_finished("backup")
                    assert snapshot["file_count"] == 2, "The backup contains an unexpected file count."
                    assert Path(snapshot["path"]).is_file(), "The ZIP archive was not published."
                    expect(page.locator("#stat-snapshots")).to_have_text("1")
                    checks.append("synthetic_source_zip_backup")

                    go_to(page, "archives")
                    with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/verify")) as verify_response:
                        page.locator('#all-archives [data-action="verify"]').click()
                    assert verify_response.value.status == 202, "The UI did not start verification."
                    verification = job_finished("verify")
                    assert verification["ok"] is True, "The archive failed content verification."
                    assert verification["checked_files"] == 2, "Verification did not cover both files."
                    checks.append("zip_sha256_verification")

                    go_to(page, "reader")
                    open_management(page)
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/import") as import_response:
                        page.locator("#import-file").set_input_files(export_path)
                    assert import_response.value.status == 200, "The import request failed."
                    imported = api_get("/api/status")["library"]
                    assert imported["messages"] == 109 and imported["conversations"] == 3, "The import lost records."
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(3)
                    expect(page.locator("#conversation-title")).to_have_text("合成测试 · 周末计划")
                    expect(page.locator(".message-bubble")).to_have_count(3)
                    expect(page.locator(".message-bubble").first).to_have_text(html_text)
                    expect(page.locator("#conversation-meta")).to_contain_text("最新在前")
                    assert_newest_first(page)
                    assert page.locator("#message-list img, #message-list script").count() == 0, "HTML was interpreted as markup."
                    assert not page.evaluate("Boolean(window.__smokeXss)"), "Imported HTML executed."
                    assert not dialogs, "An unexpected JavaScript dialog was shown."
                    checks.append("chinese_json_import_and_literal_html")

                    page.locator('[data-action="select-conversation"]').filter(has_text="合成分页 · 顺序校验").click()
                    expect(page.locator(".message-item")).to_have_count(100)
                    assert page.locator(".message-text").all_text_contents() == expected_pages[:100]
                    assert_newest_first(page)
                    expect(page.locator('[data-action="load-more"]')).to_contain_text("加载更早的消息")
                    page.locator('[data-action="load-more"]').click()
                    expect(page.locator(".message-item")).to_have_count(105)
                    assert page.locator(".message-text").all_text_contents() == expected_pages
                    assert_newest_first(page)
                    expect(page.locator('[data-action="load-more"]')).to_have_count(0)
                    checks.append("newest_first_and_equal_timestamp_page_boundary_continuity")
                    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
                    expect(page.locator(".message-item")).to_have_count(3)

                    page.locator("#conversation-search").fill("周末")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(1)
                    expect(page.locator('[data-action="select-conversation"] strong')).to_have_text("合成测试 · 周末计划")
                    page.locator("#message-search").fill("公园")
                    expect(page.locator(".message-bubble")).to_have_count(1)
                    expect(page.locator(".message-bubble")).to_contain_text("公园")
                    page.locator("#message-search").fill("smoke-no-match")
                    expect(page.locator(".message-bubble")).to_have_count(0)
                    expect(page.locator("#message-list")).to_contain_text("没有找到相关消息")
                    page.locator("#message-search").fill("")
                    expect(page.locator(".message-bubble")).to_have_count(3)
                    page.locator("#conversation-search").fill("smoke-no-match")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(0)
                    page.locator("#conversation-search").fill("")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(3)
                    checks.append("conversation_and_message_search")
                    clear_toasts(page)
                    reader_screenshot = artifacts / "reader.png"
                    page.screenshot(path=str(reader_screenshot), full_page=True)
                    screenshots.append(str(reader_screenshot))

                    go_to(page, "overview")
                    expect(page.locator("#stat-messages")).to_have_text("109")
                    clear_toasts(page)
                    overview_screenshot = artifacts / "overview.png"
                    page.screenshot(path=str(overview_screenshot), full_page=True)
                    screenshots.append(str(overview_screenshot))

                    go_to(page, "reader")
                    check_synthetic_media(page, work, state, checks)
                    media_screenshot = artifacts / "media.png"
                    page.screenshot(path=str(media_screenshot), full_page=True)
                    screenshots.append(str(media_screenshot))
                    check_readable_archive(page, work, state, job_finished, checks)

                    page.set_viewport_size({"width": 400, "height": 900})
                    mobile_widths = [check_mobile_width(page, name)
                                     for name in ("overview", "archives", "settings", "reader")]
                    media_overflow = page.locator(".media-card").evaluate_all("""cards => cards.filter(card => {
                        const bounds = card.getBoundingClientRect();
                        const container = card.closest('.messages-scroll').getBoundingClientRect();
                        return bounds.right > container.right + 1 || bounds.left < container.left - 1;
                    }).map(card => ({card: card.getBoundingClientRect().toJSON(),
                        container: card.closest('.messages-scroll').getBoundingClientRect().toJSON(),
                        row: card.closest('.message-item').getBoundingClientRect().toJSON(),
                        content: card.closest('.message-content').getBoundingClientRect().toJSON()}))""")
                    assert not media_overflow, "Media cards were clipped inside the narrow reader: " + json.dumps(media_overflow)
                    mobile_screenshot = artifacts / "mobile.png"
                    page.screenshot(path=str(mobile_screenshot), full_page=True)
                    screenshots.append(str(mobile_screenshot))
                    checks.append("all_pages_fit_400px_viewport")

                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/live") as live_response:
                        page.locator('[data-action="reader-live"]').click()
                    assert live_response.value.status == 200
                    expect(page.locator("#reader-source-title")).to_have_text("本机记录")
                    expect(page.locator('[data-action="reader-live"]')).to_be_hidden()
                    open_management(page)
                    expect(page.locator('[data-action="reader-export"]')).to_be_visible()
                    assert not api_get("/api/status")["reader_source"]["archived"]
                    checks.append("return_from_archive_to_live_library")

                    final_config = api_get("/api/config")
                    assert final_config["schedule_enabled"] is False, "Scheduling changed during the test."
                    assert not external_requests, "The synthetic browser attempted an external request."
                    if page_errors:
                        raise AssertionError("Browser page errors: " + json.dumps(page_errors))
                    checks.append("no_browser_page_errors")
                    browser.close()
                    browser = None
                    return {"ok": True, "checks": checks, "page_errors": page_errors,
                            "mobile_widths": mobile_widths, "screenshots": screenshots}
        except Exception:
            raise RuntimeError(redact(traceback.format_exc(), token)) from None
        finally:
            # The Playwright context closes the browser on exceptional exits.
            # Calling browser.close after that context ends can deadlock.
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Chrome smoke test of Shiliao.")
    parser.add_argument("--chrome", "--browser", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, default=PROJECT / ".test-artifacts")
    parser.add_argument("--serve-fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        serve_fixture(args.serve_fixture)
        return 0
    try:
        chrome = args.chrome or (DEFAULT_CHROME if DEFAULT_CHROME.exists() else None)
        result = run_smoke(chrome.expanduser().resolve() if chrome else None, args.artifacts.expanduser().resolve())
    except Exception as error:
        print(json.dumps({"ok": False, "error": redact(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
