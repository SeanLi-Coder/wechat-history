#!/usr/bin/env python3
"""Exercise the real local UI using disposable, entirely synthetic data.

The schedule remains disabled throughout this test. System source discovery,
WeChat process detection, and launchd are replaced with isolated test stubs.
No WeChat data is read or backed up. Screenshots contain synthetic records only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
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
import zipfile
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
    app.Application.start_updates = lambda self: None
    original_post = app.Handler.do_POST

    def fixture_post(handler):
        if urlsplit(handler.path).path in {"/api/pick-folder", "/api/pick-archive", "/api/pick-import-archive", "/api/wechat/import", "/api/wechat/keys", "/api/reader/export/reveal", "/api/update/check", "/api/update/settings", "/api/update/install"}:
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


def synthetic_date_conversation():
    return {"title": "合成日期边界", "messages": [
        {"sender": "日期同伴", "text": "日期边界 之前", "timestamp": "2023-12-31T15:59:59Z"},
        {"sender": "日期同伴", "text": "日期边界 开始", "timestamp": "2023-12-31T16:00:00Z"},
        {"sender": "日期同伴", "text": "日期边界 中午", "timestamp": "2024-01-01T04:00:00Z"},
        {"sender": "日期同伴", "text": "日期边界 结束", "timestamp": "2024-01-01T15:59:59.999Z"},
        {"sender": "日期同伴", "text": "日期边界 次日", "timestamp": "2024-01-01T16:00:00Z"},
    ]}


def apply_date_search(page, scope, query="", date_from="", date_to=""):
    details = page.locator(f"#{scope}-date-filter")
    if not details.evaluate("node => node.open"):
        details.locator("summary").click()
    keyword = page.locator("#conversation-search" if scope == "global" else "#message-search")
    expected = {"q": query, "date_from": date_from, "date_to": date_to}

    def matching_response(response):
        parsed = urlsplit(response.url)
        if not (parsed.path == "/api/search" if scope == "global" else parsed.path.endswith("/messages")):
            return False
        params = parse_qs(parsed.query, keep_blank_values=True)
        return all(params.get(name, [""])[0] == value for name, value in expected.items())

    with page.expect_response(matching_response):
        keyword.fill(query)
        # Filling the keyword can close the local date popover.
        if not details.evaluate("node => node.open"):
            details.locator("summary").click()
        page.locator(f"#{scope}-date-from").fill(date_from)
        page.locator(f"#{scope}-date-to").fill(date_to)
    key = json.dumps([query, date_from, date_to], ensure_ascii=False, separators=(",", ":"))
    target = "#global-search-status" if scope == "global" else "#message-list"
    expect(page.locator(target)).to_have_attribute("data-filters", key)


def check_date_filters(page, checks):
    """Exercise Shanghai calendar boundaries under a different browser timezone."""
    search = page.locator("#conversation-search")
    apply_date_search(page, "global", date_from="2024-01-01", date_to="2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(3)
    expect(page.locator(".search-result-heading time")).to_have_text([
        "2024-01-01 23:59", "2024-01-01 12:00", "2024-01-01 00:00"])
    assert page.locator(".search-result-heading time").evaluate_all(
        "nodes => nodes.every(node => node.dateTime.endsWith('Z') && node.title.includes('北京时间'))")
    expect(page.locator("#global-date-filter")).to_contain_text("按北京时间")
    page.locator("#global-date-filter summary").click()
    expect(page.locator("#global-date-label")).to_have_text("日期 · 已选")
    button = page.locator('[data-action="select-conversation"]').filter(has_text="合成日期边界")
    expect(button.locator("time")).to_have_text("2024-01-01 23:59")
    button.click()
    expect(page.locator(".message-item")).to_have_count(3)
    expect(page.locator("#message-date-from")).to_have_value("2024-01-01")
    expect(page.locator("#message-date-to")).to_have_value("2024-01-01")
    expect(page.locator(".message-meta time")).to_have_text([
        "2024-01-01 23:59", "2024-01-01 12:00", "2024-01-01 00:00"])
    page.locator(".search-message-result").filter(has_text="日期边界 开始").click()
    expect(page.locator(".message-item")).to_have_count(5)
    expect(page.locator(".message-item.is-target .message-text")).to_have_text("日期边界 开始")
    expect(page.locator("#message-date-from")).to_have_value("")
    expect(page.locator("#message-date-to")).to_have_value("")
    expect(page.locator("#global-date-from")).to_have_value("2024-01-01")
    expect(page.locator("#message-context-caption")).to_contain_text("前后文")
    checks.append("pure_date_search_inclusive_shanghai_midnight_and_unfiltered_context")

    apply_date_search(page, "global", "日期边界", date_from="2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(4)
    apply_date_search(page, "global", "日期边界", date_to="2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(4)
    apply_date_search(page, "global", date_to="2023-12-31")
    expect(page.locator(".search-message-result")).to_have_count(1)
    expect(page.locator(".search-result-preview")).to_have_text("日期边界 之前")
    apply_date_search(page, "global", "中午", "2024-01-01", "2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(1)
    apply_date_search(page, "message", "日期边界", date_from="2024-01-01")
    expect(page.locator(".message-item")).to_have_count(4)
    apply_date_search(page, "message", date_to="2024-01-01")
    expect(page.locator(".message-item")).to_have_count(4)
    apply_date_search(page, "message", "中午", "2024-01-01", "2024-01-01")
    expect(page.locator(".message-item")).to_have_count(1)
    expect(page.locator(".message-meta time")).to_have_text("2024-01-01 12:00")
    page.locator('#message-date-filter [data-action="clear-dates"]').click()
    expect(page.locator("#message-date-from")).to_have_value("")
    expect(page.locator("#message-search")).to_have_value("中午")
    checks.append("single_sided_dates_keyword_intersection_and_clear_dates_only")

    apply_date_search(page, "global", "中午", "2024-01-01", "2024-01-01")
    page.locator("#global-date-from").fill("2024-01-02")
    expect(page.locator("#conversation-list")).to_contain_text("结束日期不能早于开始日期")
    expect(page.locator(".search-message-result")).to_have_count(0)
    expect(page.locator('[data-action="search-more"]')).to_have_count(0)
    apply_date_search(page, "message", "中午", "2024-01-01", "2024-01-01")
    page.locator("#message-date-from").fill("2024-01-02")
    expect(page.locator("#message-list")).to_contain_text("结束日期不能早于开始日期")
    expect(page.locator(".message-item")).to_have_count(0)
    page.locator("#message-date-from").press("Escape")
    expect(page.locator(".message-item")).to_have_count(5)
    expect(page.locator("#message-search")).to_have_value("")
    expect(page.locator("#message-date-from")).to_have_value("")
    expect(page.locator("#message-date-to")).to_have_value("")
    search.press("Escape")
    expect(search).to_have_value("")
    expect(page.locator("#global-date-from")).to_have_value("")
    expect(page.locator("#global-date-to")).to_have_value("")
    expect(page.locator("#global-search-status")).to_be_hidden()
    page.locator("#global-date-filter").evaluate("node => { node.open = false; }")
    checks.append("invalid_date_ranges_hide_old_results_and_escape_clears_all_filters")


def check_date_pagination(page, day, title, total, checks):
    requests = []

    def record_request(request):
        parsed = urlsplit(request.url)
        if parsed.path == "/api/search" or parsed.path.endswith("/messages"):
            requests.append((parsed.path, parse_qs(parsed.query, keep_blank_values=True)))

    page.on("request", record_request)
    try:
        apply_date_search(page, "global", date_from=day, date_to=day)
        expect(page.locator(".search-message-result")).to_have_count(50)
        first = page.locator(".search-message-result").evaluate_all("nodes => nodes.map(node => node.dataset.id)")
        page.locator('[data-action="search-more"]').click()
        expect(page.locator(".search-message-result")).to_have_count(100)
        combined = page.locator(".search-message-result").evaluate_all("nodes => nodes.map(node => node.dataset.id)")
        assert combined[:50] == first and len(set(combined)) == 100
        page.locator('[data-action="select-conversation"]').filter(has_text=title).click()
        expect(page.locator(".message-item")).to_have_count(100)
        first = page.locator(".message-item").evaluate_all("nodes => nodes.map(node => node.dataset.messageId)")
        expect(page.locator("#message-date-from")).to_have_value(day)
        expect(page.locator("#message-date-to")).to_have_value(day)
        page.locator('[data-action="load-more"]').click()
        expect(page.locator(".message-item")).to_have_count(min(200, total))
        combined = page.locator(".message-item").evaluate_all("nodes => nodes.map(node => node.dataset.messageId)")
        assert combined[:100] == first and len(set(combined)) == len(combined)
        assert all(params.get("date_from") == [day] and params.get("date_to") == [day]
                   for _, params in requests), "Pagination lost the active date range"
        assert any(path == "/api/search" and params.get("offset") == ["50"] for path, params in requests)
        assert any(path.endswith("/messages") and params.get("offset") == ["100"] for path, params in requests)
        checks.append("pure_date_global_and_local_pagination_preserve_range_and_order")
    finally:
        page.remove_listener("request", record_request)
    page.locator("#conversation-search").press("Escape")
    page.locator("#message-search").press("Escape")
    expect(page.locator("#message-date-from")).to_have_value("")


def check_date_races(page, checks):
    """Hold old responses while only the date range changes."""
    pending = []
    armed = {"scope": "global"}
    matching = re.compile(r"/api/(?:search|conversations/[^/]+/messages)\?")

    def intercept(route):
        parsed = urlsplit(route.request.url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        scope = "global" if parsed.path == "/api/search" else "message"
        if (armed["scope"] == scope and params.get("q") == ["日期边界"]
                and params.get("date_to") == ["2024-01-01"] and not params.get("date_from")):
            armed["scope"] = None
            pending.append((route, route.fetch()))
        else:
            route.continue_()

    def start_old(scope):
        keyword = page.locator("#conversation-search" if scope == "global" else "#message-search")
        keyword.fill("日期边界")
        details = page.locator(f"#{scope}-date-filter")
        if not details.evaluate("node => node.open"):
            details.locator("summary").click()
        page.locator(f"#{scope}-date-from").fill("")
        page.locator(f"#{scope}-date-to").fill("2024-01-01")
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert pending, "The controlled date search did not start"

    page.route(matching, intercept)
    try:
        start_old("global")
        apply_date_search(page, "global", "日期边界", date_from="2024-01-02")
        expect(page.locator(".search-message-result")).to_have_count(1)
        route, response = pending.pop()
        route.fulfill(response=response)
        page.wait_for_timeout(150)
        expect(page.locator(".search-message-result")).to_have_count(1)
        expect(page.locator(".search-result-preview")).to_have_text("日期边界 次日")
        page.locator('[data-action="select-conversation"]').filter(has_text="合成日期边界").click()
        expect(page.locator(".message-item")).to_have_count(1)
        armed["scope"] = "message"
        start_old("message")
        apply_date_search(page, "message", "日期边界", date_from="2024-01-02")
        route, response = pending.pop()
        route.fulfill(response=response)
        page.wait_for_timeout(150)
        expect(page.locator(".message-item")).to_have_count(1)
        expect(page.locator(".message-text")).to_have_text("日期边界 次日")
        page.locator("#message-date-from").evaluate("""input => {
            input.value = '2024-01-01';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            document.querySelector('.search-message-result').click();
        }""")
        expect(page.locator(".message-item.is-target .message-text")).to_have_text("日期边界 次日")
        page.wait_for_timeout(450)
        expect(page.locator(".message-item")).to_have_count(5)
        expect(page.locator(".message-item.is-target")).to_have_count(1)
        expect(page.locator("#message-date-from")).to_have_value("")
        checks.append("date_only_response_races_and_pending_local_timer_preserve_new_context")
    finally:
        page.unroute(matching, intercept)
        for route, response in pending:
            route.fulfill(response=response)
    page.locator("#conversation-search").press("Escape")
    page.locator("#message-search").press("Escape")


def check_mobile_dates(page, checks, screenshot=None):
    viewport = page.viewport_size
    page.set_viewport_size({"width": 400, "height": 900})
    if not page.locator("#conversation-search").is_visible():
        page.locator('[data-action="reader-back"]').click()
    apply_date_search(page, "global", date_from="2024-01-01", date_to="2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth === 400")
    page.locator(".search-message-result").filter(has_text="日期边界 开始").click()
    expect(page.locator(".message-item")).to_have_count(5)
    page.locator('[data-action="toggle-message-search"]').click()
    apply_date_search(page, "message", date_from="2024-01-01", date_to="2024-01-01")
    expect(page.locator(".message-item")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth === 400")
    assert page.locator("#message-date-filter .date-filter-panel").evaluate(
        "node => node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= 400")
    if screenshot:
        page.screenshot(path=str(screenshot))
    page.locator("#message-date-from").press("Escape")
    expect(page.locator(".message-item")).to_have_count(5)
    page.locator('[data-action="reader-back"]').click()
    page.locator("#conversation-search").press("Escape")
    page.locator("#global-date-filter").evaluate("node => { node.open = false; }")
    page.set_viewport_size(viewport)
    checks.append("date_filters_and_full_timestamps_fit_400px_viewport")


def seed_synthetic_avatars(library, work: Path, conversation_id, message_ids) -> dict:
    """Use distinct generated PNGs and explicit identities, never display names."""
    urls = {}
    identities = {}
    for name, color in (("conversation", (72, 148, 98)), ("sender", (190, 115, 71))):
        def chunk(kind, content):
            return (struct.pack(">I", len(content)) + kind + content
                    + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))

        pixels = b"".join(b"\0" + bytes(color) * 32 for _ in range(32))
        body = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        source = work / ("synthetic-avatar-" + name + ".png")
        source.write_bytes(body)
        asset_id = library.media.store_file(source, "image", "image/png", filename=source.name)
        identity = hashlib.sha256(("synthetic-account:" + name).encode()).hexdigest()
        library.set_avatar(identity, asset_id)
        identities[name] = identity
        urls[name] = "/api/media/" + asset_id
    with library.connect() as connection:
        connection.execute("INSERT INTO conversation_avatars(conversation_id,identity_key) VALUES(?,?)",
                           (conversation_id, identities["conversation"]))
        connection.executemany("INSERT INTO message_avatars(message_id,identity_key) VALUES(?,?)",
                               [(message_id, identities["sender"]) for message_id in set(message_ids)])
    return urls


def expect_avatar(avatar, url):
    image = avatar.locator("img.avatar-image")
    expect(image).to_have_count(1)
    expect(image).to_have_attribute("src", url)
    expect(image).to_have_attribute("loading", "lazy")
    expect(image).to_have_attribute("decoding", "async")
    image.scroll_into_view_if_needed()
    expect(avatar).to_have_class(re.compile(r"\bhas-avatar\b"))
    assert image.evaluate("image => image.naturalWidth === 32 && getComputedStyle(image).objectFit === 'cover'")
    expect(avatar.locator(".avatar-fallback")).to_be_hidden()


def check_avatar_fallbacks(page, conversation_id, message_count, checks):
    """Reject untrusted URLs and handle failed or absent avatars in real UI flows."""
    mode = {"value": "legacy"}
    broken = "/api/media/" + "e" * 64
    conversation_routes = re.compile(r"/api/conversations(?:\?|/|$)")
    requests = []

    def record_request(request):
        if "/api/media/" in request.url or urlsplit(request.url).hostname == "avatar.invalid":
            requests.append(request.url)

    def avatar_response(route):
        response = route.fetch()
        payload = response.json()
        items = payload.get("conversations", []) + payload.get("messages", [])
        if "conversation" in payload:
            items.append(payload["conversation"])
        for item in items:
            if mode["value"] == "legacy":
                item.pop("avatar_url", None)
            else:
                item["avatar_url"] = mode["value"]
        route.fulfill(response=response, json=payload)

    def broken_response(route):
        route.fulfill(status=200, content_type="image/png", body=b"synthetic invalid image")

    def refresh():
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/conversations"):
            page.locator("#conversation-search").press("Escape")
        button = page.locator(f'[data-action="select-conversation"][data-id="{conversation_id}"]')
        with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/messages")):
            button.click()
        expect(page.locator(".message-item")).to_have_count(message_count)
        return button

    page.on("request", record_request)
    page.route(conversation_routes, avatar_response)
    page.route("**" + broken, broken_response)
    try:
        button = refresh()
        expect(button.locator(".avatar-image")).to_have_count(0)
        expect(page.locator("#conversation-header-avatar .avatar-fallback")).to_be_visible()
        expect(page.locator(".message-avatar .avatar-image")).to_have_count(0)
        checks.append("old_responses_without_avatar_fields_keep_text_avatars")

        mode["value"] = broken
        button = refresh()
        expect(button.locator(".avatar-fallback")).to_be_visible()
        expect(button.locator(".avatar-image")).to_have_count(0)
        expect(page.locator("#conversation-header-avatar .avatar-image")).to_have_count(0)
        expect(page.locator(".message-avatar").first.locator(".avatar-image")).to_have_count(0)
        assert any(urlsplit(url).path == broken for url in requests), "The broken image fallback was not exercised"
        checks.append("avatar_decode_failure_restores_text_without_broken_image")

        unsafe_urls = ["https://avatar.invalid/image.png", "//avatar.invalid/image.png",
                       "data:image/png;base64,AA==", "javascript:window.avatarXss=true",
                       broken + "?token=forbidden", broken + "#fragment", broken + "\n",
                       "/api/media/not-a-sha256", "/api/media/../status"]
        for url in unsafe_urls:
            mode["value"] = url
            before = len(requests)
            button = refresh()
            expect(button.locator(".avatar-image")).to_have_count(0)
            expect(page.locator("#conversation-header-avatar .avatar-image")).to_have_count(0)
            expect(page.locator(".message-avatar .avatar-image")).to_have_count(0)
            assert len(requests) == before, "An unsafe avatar URL triggered a request"
        assert not page.evaluate("Boolean(window.avatarXss)")
        checks.append("avatar_urls_reject_remote_data_script_query_fragment_and_invalid_paths")
    finally:
        page.unroute(conversation_routes, avatar_response)
        page.unroute("**" + broken, broken_response)
        page.remove_listener("request", record_request)
    refresh()


def check_synthetic_avatars(page, work, state, checks):
    sys.path.insert(0, str(PROJECT))
    from vault.library import Library

    library = Library(state)
    conversation = next(item for item in library.conversations()["conversations"] if "周末计划" in item["title"])
    rows = library.messages(conversation["id"])["messages"]
    message_id = next(message["id"] for message in rows if message["text"].startswith("周末一起去公园"))
    avatars = seed_synthetic_avatars(library, work, conversation["id"], [message_id])
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/conversations"):
        page.locator("#conversation-search").press("Escape")
    button = page.locator(f'[data-action="select-conversation"][data-id="{conversation["id"]}"]')
    expect_avatar(button.locator(".conversation-avatar"), avatars["conversation"])
    button.click()
    expect(page.locator(".message-item")).to_have_count(3)
    expect_avatar(page.locator("#conversation-header-avatar"), avatars["conversation"])
    expect_avatar(page.locator(f'.message-item[data-message-id="{message_id}"] .message-avatar'), avatars["sender"])
    expect(page.locator(".message-item.self .avatar-image")).to_have_count(0)
    expect(page.locator(".message-item").first.locator(".avatar-image")).to_have_count(0)
    checks.append("list_heading_and_message_avatars_use_explicit_identities_only")

    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search"
                              and parse_qs(urlsplit(response.url).query).get("q") == ["公园"]):
        page.locator("#conversation-search").fill("公园")
    expect(page.locator("#global-search-status")).to_have_attribute("data-query", "公园")
    result = page.locator(f'[data-action="open-search-message"][data-id="{message_id}"]')
    expect_avatar(result.locator(".search-result-avatar"), avatars["sender"])
    result.click()
    expect(page.locator(".message-item.is-target")).to_have_attribute("data-message-id", str(message_id))
    expect_avatar(page.locator(".message-item.is-target .message-avatar"), avatars["sender"])
    expect_avatar(page.locator("#conversation-header-avatar"), avatars["conversation"])
    checks.append("global_search_and_context_keep_distinct_conversation_and_sender_avatars")
    check_avatar_fallbacks(page, conversation["id"], 3, checks)
    return avatars


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
    original = work / "smoke-original"
    original.write_bytes(b"Synthetic original attachment bytes\n" * 4096)
    library.media.store_file(original, "original", "application/octet-stream", filename="synthetic-original.dat")
    unavailable_conversation = next(item for item in library.conversations()["conversations"] if "工作札记" in item["title"])
    unavailable_message = library.messages(unavailable_conversation["id"])["messages"][0]
    library.media.unavailable(unavailable_message["id"], "image", status="missing")
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


def check_readable_archive(page, work: Path, state: Path, job_finished, checks: list, avatars: dict, artifacts: Path) -> Path:
    """Prove a saved ZIP can play media after every original fixture is deleted."""
    page.unroute("**/api/conversations/*/messages?*")
    clear_toasts(page)
    configuration = (state / "config.json").read_bytes()
    chosen_destination = {"path": work / "one-time-export"}
    reveals = []

    def select_destination(route):
        assert route.request.post_data_json == {"purpose": "export"}
        route.fulfill(status=200, content_type="application/json", json={"path": str(chosen_destination["path"])})

    def reveal_export(route):
        reveals.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", json={"ok": True})

    page.route("**/api/pick-folder", select_destination)
    page.route("**/api/reader/export/reveal", reveal_export)

    def export_current(source_name):
        expect(page.locator('[data-action="reader-export"]')).to_be_visible()
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/export-info") as information:
            page.locator('[data-action="reader-export"]').click()
        assert information.value.status == 200
        info = information.value.json()
        expect(page.locator("#reader-export-dialog")).to_be_visible()
        expect(page.locator("#export-source")).to_contain_text(source_name)
        expect(page.locator("#export-stats")).to_contain_text("114")
        expect(page.locator("#export-stats .export-stat").nth(0).locator("strong")).to_have_text("4")
        expect(page.locator("#export-stats .export-stat").nth(2).locator("strong")).to_have_text("6")
        expect(page.locator("#export-media-detail")).to_contain_text("已保存原件 1")
        expect(page.locator("#export-unavailable")).to_contain_text("1 条媒体缺失或暂不可用")
        expect(page.locator("#export-scope-note")).to_contain_text("不受关键词、日期筛选或当前会话影响")
        expect(page.locator("#export-destination")).to_have_value(info["default_destination"])
        page.locator('[data-action="export-pick-folder"]').click()
        expect(page.locator("#export-destination")).to_have_value(str(chosen_destination["path"]))
        if not info["source"]["archived"]:
            page.screenshot(path=str(artifacts / "export-all-desktop.png"))
            viewport = page.viewport_size
            page.set_viewport_size({"width": 400, "height": 900})
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert page.evaluate("document.documentElement.scrollWidth === 400")
            assert page.locator("#reader-export-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
            page.screenshot(path=str(artifacts / "export-all-mobile.png"))
            page.locator("#export-start").scroll_into_view_if_needed()
            expect(page.locator("#export-start")).to_be_visible()
            page.screenshot(path=str(artifacts / "export-all-mobile-actions.png"))
            page.set_viewport_size(viewport)
        if not info["source"]["archived"]:
            held = []

            def hold_export_response(route):
                held.append((route, route.fetch()))

            page.route("**/api/reader/export", hold_export_response)
            page.locator("#export-start").click()
            deadline = time.monotonic() + 10
            while not held and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert held, "The controlled export request did not start"
            expect(page.locator("#export-progress")).to_be_visible()
            page.keyboard.press("Escape")
            page.locator('[data-action="reader-export"]').click()
            expect(page.locator("#export-progress")).to_be_visible()
            expect(page.locator("#export-start")).to_be_hidden()
            route, response = held.pop()
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/export") as exported:
                route.fulfill(response=response)
            page.unroute("**/api/reader/export", hold_export_response)
            checks.append("export_progress_survives_close_and_reopen_during_pending_start")
        else:
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/export") as exported:
                page.locator("#export-start").click()
        assert exported.value.status == 202, "The reader export did not start."
        assert exported.value.request.post_data_json == {"destination": str(chosen_destination["path"]), "source_id": info["source_id"]}
        result = job_finished("reader_export")
        archive = Path(result["path"])
        assert archive.parent == chosen_destination["path"] and archive.is_file()
        assert result["objects"] == 6, "The ZIP did not include three media objects, both avatars and the compressed original."
        assert result["summary"]["messages"] == 114 and result["summary"]["conversations"] == 4
        assert result["summary"]["unavailable_media"] == 1
        assert result["source"] == info["source"]
        expect(page.locator("#export-result")).to_be_visible()
        expect(page.locator("#export-result-summary")).to_contain_text("114 条消息")
        expect(page.locator("#export-result-summary")).to_contain_text("ZIP 大小")
        expect(page.locator("#export-result-path")).to_have_text(str(archive))
        page.locator('[data-action="export-reveal"]').click()
        assert reveals[-1] == {"path": str(archive)}
        page.keyboard.press("Escape")
        expect(page.locator("#reader-export-dialog")).to_be_hidden()
        expect(page.locator('[data-action="reader-export"]')).to_be_focused()
        if not info["source"]["archived"]:
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status"):
                page.reload(wait_until="domcontentloaded")
            page.locator('.nav-item[data-page="reader"]').click()
            page.locator('[data-action="reader-export"]').click()
            expect(page.locator("#export-result")).to_be_visible()
            expect(page.locator("#export-result-path")).to_have_text(str(archive))
            page.keyboard.press("Escape")
            checks.append("completed_export_result_is_restored_after_browser_reload")
        assert (state / "config.json").read_bytes() == configuration, "A one-time export changed backup settings"
        return archive

    apply_date_search(page, "global", "中午", "2024-01-01", "2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(1)
    page.locator("#message-search").fill("公园")
    expect(page.locator(".message-item")).to_have_count(1)
    archive = export_current("本机记录")
    checks.append("all_export_ignores_search_filters_and_uses_one_time_folder_with_summary_and_reveal")
    checks.append("export_dialog_is_keyboard_accessible_and_fits_400px")
    expect(page.locator('[data-action="reader-open"]')).to_be_enabled()

    for folder in (state / "media/objects", state / "media/cache"):
        assert folder.is_relative_to(work)
        shutil.rmtree(folder)
    for name in ("smoke-image", "smoke-audio", "smoke-video", "synthetic-video.mp4", "smoke-original",
                 "synthetic-avatar-conversation.png", "synthetic-avatar-sender.png"):
        (work / name).unlink()
    session = page.context.new_cdp_session(page)
    session.send("Network.clearBrowserCache")
    session.detach()

    selected_archive = {"path": archive}

    def select_archive(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"path": str(selected_archive["path"])}))

    page.route("**/api/pick-archive", select_archive)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/archive") as opened:
        page.locator('[data-action="reader-open"]').click()
    assert opened.value.status == 202, "The readable ZIP did not start opening."
    job_finished("reader_open")
    expect(page.locator("#reader-source-title")).to_contain_text(archive.name)
    expect(page.locator("#reader-job")).to_be_hidden()
    expect(page.locator('[data-action="reader-live"]')).to_be_visible()
    expect(page.locator('[data-action="reader-export"]')).to_be_visible()
    chosen_destination["path"] = work / "archive-copy"
    copied = export_current(archive.name)
    with zipfile.ZipFile(archive) as first, zipfile.ZipFile(copied) as second:
        media_names = [name for name in first.namelist() if name.startswith("media/objects/")]
        assert len(media_names) == 6 and any(name.endswith(".gz") for name in media_names)
        assert all(first.read(name) == second.read(name) for name in media_names), "Re-export changed stored media bytes"
    checks.append("opened_archive_can_export_all_unextracted_media_and_preserve_gzip_original_bytes")
    selected_archive["path"] = copied
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/archive"):
        page.locator('[data-action="reader-open"]').click()
    job_finished("reader_open")
    expect(page.locator("#reader-source-title")).to_contain_text(copied.name)
    archive.unlink()
    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
    expect(page.locator(".media-card")).to_have_count(3)
    expect_avatar(page.locator("#conversation-header-avatar"), avatars["conversation"])
    expect_avatar(page.locator(".message-avatar:has(.avatar-image)"), avatars["sender"])
    page.locator(".message-image").scroll_into_view_if_needed()
    wait_until(lambda: page.locator(".message-image").evaluate("node => node.naturalWidth === 960"), "archived image decoding")
    for kind in ("audio", "video"):
        wait_until(lambda: page.locator(f"{kind}.media-player").evaluate("node => node.readyState >= 1"), "archived media metadata")
        page.locator(f"{kind}.media-player").evaluate("async player => { await player.play(); player.pause(); }")
    assert not (state / "media/objects").exists(), "Opening the ZIP unexpectedly restored the original media store."
    assert list((state / "opened-archives").glob("*/media/objects/*/*")), "The readable archive did not lazily extract media."
    checks.append("readable_zip_plays_media_after_original_source_and_store_removal")
    checks.append("readable_zip_shows_avatars_after_original_source_and_store_removal")
    page.locator("#messages-scroll").evaluate("element => { element.scrollTop = 0; }")
    clear_toasts(page)
    return copied


def check_archive_import(page, work: Path, state: Path, archive: Path, job_finished, api_get,
                         checks: list, avatars: dict, artifacts: Path) -> None:
    """Merge only disposable archives, with no native file picker or production data."""
    from vault.library import Library
    from vault.readable_archive import create_archive

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#reader-management").evaluate("node => { node.open = false; }")
    selected = {"path": str(archive)}
    page.route("**/api/pick-import-archive", lambda route: route.fulfill(json=selected))
    top_import = page.locator('.reader-topbar [data-action="reader-import"]')
    expect(top_import).to_be_visible()
    configuration = (state / "config.json").read_bytes()

    def preview():
        if not page.locator("#reader-import-dialog").evaluate("node => node.open"):
            top_import.click()
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/import-info") as response:
            page.locator('[data-action="import-pick-archive"]').click()
        return response.value

    def start(info, hold=False):
        if hold:
            pending = []
            page.route("**/api/reader/import", lambda route: pending.append((route, route.fetch())))
            page.locator("#import-start").click()
            deadline = time.monotonic() + 10
            while not pending and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert pending, "The synthetic archive import did not start"
            expect(page.locator("#import-progress")).to_be_visible()
            page.keyboard.press("Escape")
            top_import.click()
            expect(page.locator("#import-progress")).to_be_visible()
            expect(page.locator("#import-start")).to_be_hidden()
            route, response = pending.pop()
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/import") as imported:
                route.fulfill(response=response)
            page.unroute("**/api/reader/import")
        else:
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/import") as imported:
                page.locator("#import-start").click()
        assert imported.value.status == 202
        body = imported.value.request.post_data_json
        assert body == {"path": selected["path"], "preview_id": info["preview_id"]}
        assert len(imported.value.request.post_data) < 4096, "ZIP bytes were uploaded through the browser"
        result = job_finished("reader_import")
        expect(page.locator("#import-result")).to_be_visible()
        expect(page.locator("#reader-source-title")).to_have_text("本机记录")
        assert not api_get("/api/status")["reader_source"]["archived"]
        return result

    # Keep the archive view active while importing back into the local library.
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reader/archive"):
        page.locator('[data-action="reader-open"]').click()
    job_finished("reader_open")
    expect(page.locator("#reader-source-title")).to_contain_text(archive.name)
    apply_date_search(page, "global", "中午", "2024-01-01", "2024-01-01")
    expect(page.locator(".search-message-result")).to_have_count(1)
    response = preview()
    assert response.status == 200
    info = response.json()
    assert info["summary"]["messages"] == 114
    expect(page.locator("#import-stats")).to_contain_text("114")
    expect(page.locator("#import-unavailable")).to_contain_text("1 条媒体缺失或暂不可用")
    expect(page.locator("#import-disk")).to_contain_text("本机磁盘空间")
    expect(page.locator("#import-scope-note")).to_contain_text("保留已有内容")
    page.screenshot(path=str(artifacts / "import-desktop.png"))
    page.set_viewport_size({"width": 400, "height": 900})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert page.evaluate("document.documentElement.scrollWidth === 400")
    assert page.locator("#reader-import-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
    page.screenshot(path=str(artifacts / "import-mobile.png"))
    page.locator("#import-start").scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / "import-mobile-actions.png"))
    page.set_viewport_size({"width": 1440, "height": 1000})

    def insufficient_preview(route):
        response = route.fetch()
        payload = response.json()
        payload["available_bytes"] = 0
        payload["unverified_media"] = 2
        route.fulfill(response=response, json=payload)

    page.route("**/api/reader/import-info", insufficient_preview)
    preview()
    expect(page.locator("#import-error")).to_contain_text("本机可用空间不足")
    expect(page.locator("#import-start")).to_be_disabled()
    expect(page.locator("#import-unavailable")).to_contain_text("2 项旧版微信媒体缺少版本校验信息")
    expect(page.locator("#import-media-detail")).to_contain_text("部分关联待确认")
    page.unroute("**/api/reader/import-info", insufficient_preview)
    info = preview().json()
    expect(page.locator("#import-error")).to_be_hidden()
    expect(page.locator("#import-unavailable")).not_to_contain_text("缺少版本校验信息")
    checks.append("archive_import_disables_merge_when_preview_reports_insufficient_disk_space")
    result = start(info, hold=True)
    assert result["added"]["messages"] == 0 and result["skipped"]["messages"] == 114
    expect(page.locator("#import-result-duplicates")).to_contain_text("114 条重复消息")
    expect(page.locator("#conversation-search")).to_have_value("")
    expect(page.locator("#global-date-from")).to_have_value("")
    assert Library(state).stats()["messages"] == 114
    checks.append("archive_import_uses_path_preview_and_merges_into_local_without_duplicate_messages")
    checks.append("archive_import_progress_survives_close_reopen_and_clears_archive_filters_on_completion")
    checks.append("archive_import_preview_and_actions_fit_400px_without_zip_browser_upload")

    def legacy_import_status(route):
        response = route.fetch()
        payload = response.json()
        if payload.get("job", {}).get("type") == "reader_import":
            payload["job"]["result"]["unverified_media"] = 2
        route.fulfill(response=response, json=payload)

    page.route("**/api/status", legacy_import_status)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status"):
        page.reload(wait_until="domcontentloaded")
    go_to(page, "reader")
    top_import.click()
    expect(page.locator("#import-result")).to_be_visible()
    expect(page.locator("#import-result-duplicates")).to_contain_text("114 条重复消息")
    expect(page.locator("#import-result-summary")).to_contain_text("2 项旧版微信媒体缺少版本校验信息，原件已保留")
    expect(page.locator("#import-result-summary")).to_contain_text("未经确认的关联暂不显示")
    page.unroute("**/api/status", legacy_import_status)
    checks.append("legacy_unverified_media_is_explicit_in_preview_and_import_completion")
    page.keyboard.press("Escape")
    expect(top_import).to_be_focused()
    checks.append("archive_import_completion_restores_after_reload_and_keyboard_close_restores_focus")

    # Include new records, shared media and an additional compressed original.
    fixture = work / "new-import-fixture"
    fixture.mkdir()
    incoming = Library(fixture / "state")
    literal = '<img src=x onerror="window.__importXss=true"> Imported fixture text'
    incoming.import_file(json.dumps({"title": "合成导入 · 新会话", "messages": [
        {"sender": "归档好友", "text": literal, "timestamp": "2025-06-02T13:14:00+08:00"}
    ]}).encode(), "new-fixture.json")
    conversation = incoming.conversations()["conversations"][0]
    message_id = incoming.messages(conversation["id"])["messages"][0]["id"]
    for name, (mime, data) in synthetic_media(fixture).items():
        path = fixture / name
        path.write_bytes(data)
        kind = name.removeprefix("smoke-")
        asset = incoming.media.store_file(path, kind, mime, filename=name)
        incoming.media.link(message_id, kind, asset)
    fresh_avatars = seed_synthetic_avatars(incoming, fixture, conversation["id"], [message_id])
    original = fixture / "original.dat"
    original.write_bytes(b"New synthetic original archive attachment\n" * 4096)
    incoming.media.store_file(original, "original", "application/octet-stream", filename=original.name)
    saved = create_archive(incoming, fixture / "archives")
    incoming_zip = Path(saved["path"]).with_name("synthetic-<img src=x onerror=window.__importXss=true>.zip")
    Path(saved["path"]).rename(incoming_zip)
    selected["path"] = str(incoming_zip)
    response = preview()
    assert response.status == 200
    expect(page.locator("#import-source")).to_have_text(incoming_zip.name)
    expect(page.locator("#import-source img")).to_have_count(0)
    result = start(response.json())
    assert result["added"]["messages"] == 1 and result["added"]["conversations"] == 1
    assert Library(state).stats()["messages"] == 115
    expect(page.locator("#import-result-summary")).to_contain_text("1 条消息")
    checks.append("archive_import_preserves_existing_records_and_treats_untrusted_names_as_text")
    response = preview()
    result = start(response.json())
    assert result["added"]["messages"] == 0 and result["skipped"]["messages"] == 1
    assert Library(state).stats()["messages"] == 115
    expect(page.locator("#import-result-duplicates")).to_contain_text("1 条重复消息")
    checks.append("repeated_archive_import_skips_existing_messages_and_media")
    incoming_zip.unlink()
    archive.unlink()
    shutil.rmtree(fixture)
    shutil.rmtree(state / "opened-archives")
    session = page.context.new_cdp_session(page)
    session.send("Network.clearBrowserCache")
    session.detach()
    page.locator('[data-action="import-view"]').click()
    page.locator('[data-action="select-conversation"]').filter(has_text="合成导入 · 新会话").click()
    expect(page.locator(".message-bubble")).to_contain_text(literal)
    assert not page.evaluate("Boolean(window.__importXss)")
    expect_avatar(page.locator("#conversation-header-avatar"), fresh_avatars["conversation"])
    expect_avatar(page.locator(".message-avatar"), fresh_avatars["sender"])
    wait_until(lambda: page.locator(".message-image").evaluate("node => node.naturalWidth === 960"), "imported image decoding")
    for kind in ("audio", "video"):
        player = page.locator(f"{kind}.media-player")
        wait_until(lambda: player.evaluate("node => node.readyState >= 1"), "imported media metadata")
        player.evaluate("async node => { await node.play(); node.pause(); node.currentTime = 0.5; }")
        assert player.evaluate("node => Math.abs(node.currentTime - 0.5) < 0.1")
    assert list((state / "media/objects").glob("*/*.gz")), "Compressed originals were not imported"
    checks.append("imported_images_audio_video_avatars_and_compressed_originals_survive_zip_and_cache_removal")

    invalid = work / "invalid-import.zip"
    invalid.write_bytes(b"This is not a readable archive")
    selected["path"] = str(invalid)
    before = Library(state).stats()
    response = preview()
    assert response.status == 400
    expect(page.locator("#import-error")).to_be_visible()
    expect(page.locator("#import-start")).to_be_hidden()
    expect(page.locator("#import-preview")).to_be_hidden()
    assert Library(state).stats() == before
    checks.append("invalid_archive_preview_clears_stale_preview_and_leaves_local_records_unchanged")

    text_file = work / "import-text.txt"
    text_file.write_text("[2025-01-01 08:00:00] Fixture sender: Synthetic text import remains available", encoding="utf-8")
    page.locator("#import-text-method > summary").click()
    with page.expect_file_chooser() as chooser:
        page.locator('[data-action="import"]').click()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/import") as response:
        chooser.value.set_files(text_file)
    assert response.value.status == 200
    expect(page.locator("#import-text-status")).to_contain_text("文字导入完成")
    expect(page.locator('[data-action="import"]')).to_be_enabled()
    assert Library(state).stats()["messages"] == 116
    page.locator("#import-file").evaluate("""node => {
        const file = new File(['synthetic'], 'too-large.txt', {type:'text/plain'});
        Object.defineProperty(file, 'size', {value: 100 * 1024 * 1024 + 1});
        Object.defineProperty(node, 'files', {value: [file], configurable:true});
        node.dispatchEvent(new Event('change', {bubbles:true}));
        delete node.files;
    }""")
    expect(page.locator("#import-text-status")).to_contain_text("文件超过 100 MB")
    assert Library(state).stats()["messages"] == 116
    page.keyboard.press("Escape")
    assert (state / "config.json").read_bytes() == configuration
    checks.append("text_import_remains_available_in_import_dialog_with_100mb_limit_and_unchanged_settings")


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


def check_update_settings(page, config_path: Path, artifacts: Path, checks: list) -> None:
    """Exercise update UI with same-origin synthetic responses, never a real updater."""
    saved_config = config_path.read_bytes()
    updates = {"supported": True, "enabled": True, "current_version": "0.7.0", "status": "idle",
               "latest_version": None, "download_kind": None, "download_bytes": 0,
               "downloaded_bytes": 0, "full_bytes": 0, "error": None}
    fixture = {"hold_check": True, "pending": [], "calls": [], "busy_job": False, "fail_setting": False}

    def status_response(route):
        response = route.fetch()
        payload = response.json()
        payload["updates"] = dict(updates)
        if fixture["busy_job"]:
            payload["job"] = {"id": "synthetic-update-busy", "type": "reader_open", "status": "running", "progress": {}}
        route.fulfill(response=response, json=payload)

    def update_response(route):
        request = route.request
        path = urlsplit(request.url).path
        assert request.headers.get("authorization", "").startswith("Bearer "), "The update API was not authenticated"
        if path == "/api/update/status":
            route.fulfill(json=updates)
            return
        fixture["calls"].append(path)
        if path.endswith("/settings"):
            if fixture["fail_setting"]:
                route.fulfill(status=500, json={"error": "合成测试：设置未保存，请重试。"})
                return
            assert set(request.post_data_json) == {"enabled"}
            updates["enabled"] = request.post_data_json["enabled"]
        elif path.endswith("/check"):
            updates.update(status="downloading", latest_version="0.8.0", download_kind="delta",
                           download_bytes=8 * 1024 ** 2, downloaded_bytes=2 * 1024 ** 2,
                           full_bytes=80 * 1024 ** 2, error=None)
            if fixture["hold_check"]:
                fixture["pending"].append(route)
                return
        elif path.endswith("/install"):
            assert request.post_data_json == {}
        else:
            raise AssertionError("Unexpected synthetic update endpoint")
        route.fulfill(json={"ok": True})

    def refresh():
        page.evaluate("() => refreshStatus({silent: true})")

    page.route("**/api/status", status_response)
    page.route("**/api/update/*", update_response)
    try:
        refresh()
        expect(page.locator("#update-enabled")).to_be_checked()
        expect(page.locator("#update-current-version")).to_have_text("0.7.0")
        page.locator("#update-enabled").uncheck()
        expect(page.locator("#update-enabled")).not_to_be_checked()
        expect(page.locator("#update-enabled")).to_be_enabled()
        assert updates["enabled"] is False
        fixture["fail_setting"] = True
        page.locator("#update-enabled").check()
        expect(page.locator("#update-error")).to_contain_text("设置未保存")
        expect(page.locator("#update-enabled")).not_to_be_checked()
        fixture["fail_setting"] = False
        page.locator("#update-enabled").check()
        expect(page.locator("#update-enabled")).to_be_checked()
        expect(page.locator("#update-error")).to_be_hidden()
        assert config_path.read_bytes() == saved_config, "Update settings changed the backup configuration"
        checks.append("update_setting_saves_immediately_and_failure_restores_previous_value")

        page.locator("#update-check").click()
        expect(page.locator("#update-badge")).to_have_text("检查中")
        expect(page.locator("#update-check")).to_be_disabled()
        assert fixture["pending"]
        fixture["pending"].pop().fulfill(json={"ok": True})
        expect(page.locator("#update-download-kind")).to_have_text("增量下载")
        expect(page.locator("#update-progress-text")).to_have_text("2 MB / 8 MB")
        expect(page.locator("#update-download-size")).to_contain_text("本次增量下载 8 MB · 完整安装包 80 MB")
        assert page.locator("#update-progress").evaluate("node => [node.value, node.max]") == [2 * 1024 ** 2, 8 * 1024 ** 2]
        expect(page.locator("#update-install")).to_be_hidden()
        page.locator("#update-card").screenshot(path=str(artifacts / "updates-desktop.png"))
        checks.append("update_check_loading_and_actual_delta_download_progress_are_visible")

        updates.update(status="error", error='合成校验失败 <img src="https://invalid.example/x">')
        refresh()
        expect(page.locator("#update-error")).to_contain_text("合成校验失败 <img")
        assert page.locator("#update-card img").count() == 0
        expect(page.locator("#update-check-label")).to_have_text("重试更新")
        updates["error"] = "invalid_update_signature"
        refresh()
        expect(page.locator("#update-error")).to_contain_text("更新签名验证未通过")
        updates["error"] = "update_failed"
        refresh()
        expect(page.locator("#update-error")).to_contain_text("检查网络连接和可用磁盘空间")
        checks.append("update_integrity_and_network_error_codes_have_actionable_chinese_messages")
        fixture["hold_check"] = False
        page.locator("#update-check").click()
        expect(page.locator("#update-error")).to_be_hidden()
        updates.update(download_kind="full", download_bytes=80 * 1024 ** 2, downloaded_bytes=16 * 1024 ** 2)
        refresh()
        expect(page.locator("#update-download-kind")).to_have_text("完整版本下载")
        expect(page.locator("#update-progress-text")).to_have_text("16 MB / 80 MB")
        checks.append("update_failure_is_literal_text_and_retry_full_download_remains_available")

        updates.update(status="ready", downloaded_bytes=80 * 1024 ** 2)
        fixture["busy_job"] = True
        refresh()
        expect(page.locator("#update-install")).to_be_visible()
        expect(page.locator("#update-install")).to_be_disabled()
        expect(page.locator("#update-status")).to_contain_text("当前任务完成后")
        fixture["busy_job"] = False
        refresh()
        expect(page.locator("#update-install")).to_be_enabled()
        page.set_viewport_size({"width": 400, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth") == 400
        assert page.locator("#update-card").evaluate("node => node.scrollWidth <= node.clientWidth")
        page.locator("#update-card").scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / "updates-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        checks.append("ready_update_waits_for_archive_jobs_and_fits_400px")

        page.locator("#update-install").click()
        expect(page.locator("#update-status")).to_contain_text("正在重启拾聊")
        expect(page.locator("#update-install")).to_be_disabled()
        expect(page.locator("#connection-banner")).to_be_hidden()
        assert fixture["calls"].count("/api/update/install") == 1
        assert page.evaluate("() => state.updateRestartPending") is True
        checks.append("update_install_is_explicit_and_old_window_stops_polling")
    finally:
        for route in fixture["pending"]:
            route.fulfill(json={"ok": True})
        page.unroute("**/api/status", status_response)
        page.unroute("**/api/update/*", update_response)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#connection-banner")).to_be_hidden()


def check_backup_reading_entry(page, snapshot: dict, checks: list) -> None:
    """Verify raw/readable routing and progress without reading encrypted personal data."""
    fixture = {"job": None, "readable": False, "calls": []}

    def status_response(route):
        response = route.fetch()
        payload = response.json()
        if fixture["job"]:
            payload["job"] = fixture["job"]
        for item in payload.get("snapshots", []):
            if item["id"] == snapshot["id"]:
                if fixture["readable"]:
                    item["readable_archive"] = {"path": str(Path(snapshot["path"]).with_name("readable-synthetic.zip")),
                                                "name": "readable-synthetic.zip", "available": True}
                else:
                    item.pop("readable_archive", None)
        route.fulfill(response=response, json=payload)

    def start_open(route):
        path = urlsplit(route.request.url).path
        fixture["calls"].append(path)
        assert route.request.headers.get("authorization", "").startswith("Bearer ")
        if path.endswith("/open"):
            assert route.request.post_data_json == {}
        else:
            assert route.request.post_data_json == {"path": snapshot["path"]}
        fixture["job"] = {"id": "synthetic-raw-reader", "type": "reader_open", "status": "running",
                          "progress": {"phase": "raw_extract", "done_files": 1, "total_files": 2,
                                       "done_bytes": 1024, "total_bytes": 2048, "current_file": "正在读取原始备份文件"}}
        route.fulfill(status=202, json={"job_id": fixture["job"]["id"]})

    page.route("**/api/status", status_response)
    page.route("**/api/snapshots/*/open", start_open)
    page.route("**/api/reader/archive", start_open)
    page.route("**/api/pick-archive", lambda route: route.fulfill(json={"path": snapshot["path"]}))
    try:
        page.evaluate("() => refreshStatus({silent: true})")
        go_to(page, "archives")
        expect(page.locator("#all-archives .archive-kinds")).to_contain_text("原始备份")
        expect(page.locator("#all-archives .archive-kinds")).to_contain_text("打开时整理聊天")
        page.locator('#all-archives [data-action="snapshot-open"]').click()
        expect(page.locator("#page-reader")).to_be_visible()
        expect(page.locator("#reader-job")).to_contain_text("正在校验并展开原始备份")
        fixture["job"]["progress"].update(phase="raw_import", current_file="正在整理聊天与媒体")
        page.evaluate("() => refreshStatus({silent: true})")
        expect(page.locator("#reader-job")).to_contain_text("正在将原始备份整理为可读聊天")
        fixture["job"].update(status="error", code="raw_keys_missing", error="这份原始备份需要有效密钥，请先导入匹配的密钥文件，再重新打开。")
        page.evaluate("() => refreshStatus({silent: true})")
        expect(page.locator("#reader-job")).to_contain_text("需要有效密钥")
        expect(page.locator('[data-action="reader-open"]')).to_be_enabled()
        checks.append("raw_backup_has_open_chat_entry_and_reports_conversion_phases_and_missing_keys")

        fixture["readable"] = True
        fixture["job"] = None
        page.evaluate("() => refreshStatus({silent: true})")
        go_to(page, "archives")
        expect(page.locator("#all-archives .archive-kinds")).to_contain_text("可读归档已准备")
        expect(page.locator('#all-archives [data-action="snapshot-open"]')).to_have_attribute("title", "打开这份备份对应的可读归档")
        go_to(page, "reader")
        expect(page.locator('[data-action="reader-open"]')).to_have_attribute("title", "打开拾聊可读归档或微信原始备份 ZIP")
        page.locator('[data-action="reader-open"]').click()
        expect(page.locator("#reader-job")).to_contain_text("正在校验并展开原始备份")
        assert fixture["calls"] == [f"/api/snapshots/{snapshot['id']}/open", "/api/reader/archive"]
        checks.append("linked_readable_archive_is_labeled_and_reader_picker_accepts_raw_backup_path")
    finally:
        page.unroute("**/api/status", status_response)
        page.unroute("**/api/snapshots/*/open", start_open)
        page.unroute("**/api/reader/archive", start_open)
        page.unroute("**/api/pick-archive")
        page.evaluate("() => refreshStatus({silent: true})")


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
        document["conversations"].append(synthetic_date_conversation())
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
                                                  locale="zh-CN", timezone_id="America/Los_Angeles",
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
                    check_update_settings(page, config_path, artifacts, checks)

                    go_to(page, "overview")
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/backup") as backup_response:
                        page.locator('#page-overview .hero-content [data-action="backup"]').click()
                    assert backup_response.value.status == 202, "The UI did not start a backup."
                    snapshot = job_finished("backup")
                    assert snapshot["file_count"] == 2, "The backup contains an unexpected file count."
                    assert Path(snapshot["path"]).is_file(), "The ZIP archive was not published."
                    expect(page.locator("#stat-snapshots")).to_have_text("1")
                    checks.append("synthetic_source_zip_backup")
                    expect(page.locator('#overview-job [data-action="snapshot-open"]')).to_be_visible()
                    checks.append("completed_backup_offers_open_chat_action")

                    go_to(page, "archives")
                    with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/verify")) as verify_response:
                        page.locator('#all-archives [data-action="verify"]').click()
                    assert verify_response.value.status == 202, "The UI did not start verification."
                    verification = job_finished("verify")
                    assert verification["ok"] is True, "The archive failed content verification."
                    assert verification["checked_files"] == 2, "Verification did not cover both files."
                    checks.append("zip_sha256_verification")
                    check_backup_reading_entry(page, snapshot, checks)

                    go_to(page, "reader")
                    open_management(page)
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/import") as import_response:
                        page.locator("#import-file").set_input_files(export_path)
                    assert import_response.value.status == 200, "The import request failed."
                    imported = api_get("/api/status")["library"]
                    assert imported["messages"] == 114 and imported["conversations"] == 4, "The import lost records."
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(4)
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
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(4)
                    checks.append("conversation_and_message_search")
                    avatars = check_synthetic_avatars(page, work, state, checks)
                    check_date_filters(page, checks)
                    check_date_pagination(page, "2024-03-01", "合成分页 · 顺序校验", 105, checks)
                    check_date_races(page, checks)
                    check_mobile_dates(page, checks, artifacts / "date-mobile.png")
                    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
                    expect(page.locator(".message-item")).to_have_count(3)
                    clear_toasts(page)
                    reader_screenshot = artifacts / "reader.png"
                    page.screenshot(path=str(reader_screenshot), full_page=True)
                    screenshots.append(str(reader_screenshot))

                    go_to(page, "overview")
                    expect(page.locator("#stat-messages")).to_have_text("114")
                    clear_toasts(page)
                    overview_screenshot = artifacts / "overview.png"
                    page.screenshot(path=str(overview_screenshot), full_page=True)
                    screenshots.append(str(overview_screenshot))

                    go_to(page, "reader")
                    check_synthetic_media(page, work, state, checks)
                    media_screenshot = artifacts / "media.png"
                    page.screenshot(path=str(media_screenshot), full_page=True)
                    screenshots.append(str(media_screenshot))
                    readable_archive = check_readable_archive(page, work, state, job_finished, checks, avatars, artifacts)
                    apply_date_search(page, "global", date_from="2024-01-01", date_to="2024-01-01")
                    expect(page.locator(".search-message-result")).to_have_count(3)
                    page.locator(".search-message-result").filter(has_text="日期边界 开始").click()
                    expect(page.locator(".message-item")).to_have_count(5)
                    expect(page.locator("#message-context-caption")).to_contain_text("不受日期限制")
                    page.locator("#conversation-search").press("Escape")
                    page.locator('[data-action="select-conversation"]').filter(has_text="周末计划").click()
                    expect(page.locator(".media-card")).to_have_count(3)
                    checks.append("saved_readable_archive_supports_date_search_and_complete_context")

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

                    check_archive_import(page, work, state, readable_archive, job_finished, api_get,
                                         checks, avatars, artifacts)

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
