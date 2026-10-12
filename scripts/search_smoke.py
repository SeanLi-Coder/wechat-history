#!/usr/bin/env python3
"""Verify search and context navigation using an isolated synthetic library.

No production state, account directories, system schedules, or external network
requests are used. Optional screenshots contain generated fixtures only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from browser_smoke import (apply_date_search, check_avatar_fallbacks, check_date_filters, check_date_pagination,
                           check_date_races, check_mobile_dates, expect_avatar,
                           seed_synthetic_avatars, synthetic_date_conversation)

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
TITLE = "好友 · 秋日计划"


@contextmanager
def synthetic_server(work):
    import app
    from vault.library import Library

    state = work / "state"
    library = Library(state)
    source = work / "synthetic-source"
    source.mkdir()
    configuration = {"source": str(source), "destination": str(work / "archives"),
                     "schedule_enabled": False, "schedule_time": "22:00", "interval": "daily",
                     "timezone": "Asia/Shanghai", "include_media": True}
    (state / "config.json").write_text(json.dumps(configuration), encoding="utf-8")
    base = datetime(2024, 9, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    messages = [{"sender": "我" if index % 2 else "阿岚", "is_self": bool(index % 2),
                 "text": f"合成消息 {index + 1}：" + ("公园里的日常" if index < 130 else "普通对话"),
                 "timestamp": (base + timedelta(minutes=index)).isoformat()}
                for index in range(350)]
    messages[175]["text"] = '银河计划：<img src=x onerror="window.searchXss=true"><script>window.searchXss=true</script>'
    messages[176]["text"] = "字面值 100%_ready 与 [brackets].*"
    messages[177]["text"] = "CaseFoldKeyword"
    messages[249]["timestamp"] = messages[250]["timestamp"]
    messages[25]["sender"] = messages[226]["sender"] = "测试昵称"
    document = {"conversations": [
        {"title": TITLE, "messages": messages},
        {"title": "林然", "messages": [{"sender": "林然", "text": "海边见", "timestamp": base.isoformat()}]},
        {"title": "产品讨论", "messages": [{"sender": "小夏", "text": "合成方案", "timestamp": base.isoformat()}]},
    ] + [{"title": f"合成群 {index + 1}", "messages": [
        {"sender": "共享昵称", "text": "范围提示测试", "timestamp": base.isoformat()}]} for index in range(51)]}
    document["conversations"].append(synthetic_date_conversation())
    library.import_file(json.dumps(document, ensure_ascii=False).encode(), "synthetic-search.json")
    conversation_id = next(row["id"] for row in library.conversations()["conversations"] if row["title"] == TITLE)
    rows = library.messages(conversation_id, limit=500)["messages"]
    target = next(row["id"] for row in rows if row["text"].startswith("银河计划："))
    ordered = sorted(rows, key=lambda row: (row["timestamp"], row["id"]), reverse=True)
    expected_ids = [row["id"] for row in ordered]
    sender_ids = [row["id"] for row in ordered if row["sender"] == "测试昵称"]
    avatars = seed_synthetic_avatars(library, work, conversation_id, [target, expected_ids[0], *sender_ids])
    application = app.Application(state)
    application.status = lambda: {
        "app_name": "拾聊", "version": "test", "config": configuration, "sources": [], "snapshots": [],
        "job": None, "schedule": {"installed": False}, "library": library.stats(),
        "media": library.media.stats(), "reader_source": {"archived": False, "name": "合成记录"},
        "wechat_reader": {"configured": False}, "wechat_running": False,
    }

    class Handler(app.Handler):
        def do_POST(self):
            self.response({"error": "Mutations are disabled in search fixtures"}, 405)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.app = application
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", application.token, conversation_id, target, expected_ids, sender_ids, avatars
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fit(page):
    assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), "The page overflows the viewport"
    pane = page.locator("#messages-scroll")
    if pane.is_visible():
        assert pane.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), "Messages overflow their pane"


def rendered_ids(page):
    return page.locator(".message-item").evaluate_all("nodes => nodes.map(node => Number(node.dataset.messageId))")


def run(browser_path=None, artifacts=None):
    checks = []
    errors, unexpected = [], []
    with tempfile.TemporaryDirectory(prefix="shiliao-search-smoke-") as temporary:
        with synthetic_server(Path(temporary)) as (origin, token, conversation_id, target_id, expected_ids, sender_ids, avatars):
            with sync_playwright() as playwright:
                options = {"headless": True, "args": ["--mute-audio", "--disable-background-networking", "--disable-component-update", "--disable-sync"]}
                executable = browser_path or (CHROME if CHROME.exists() else None)
                if executable:
                    options["executable_path"] = str(executable)
                browser = playwright.chromium.launch(**options)
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN",
                                                  timezone_id="America/Los_Angeles", reduced_motion="reduce", service_workers="block")
                    stale_routes = []

                    def route_request(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        if f"{parsed.scheme}://{parsed.netloc}" != origin or request.method not in {"GET", "HEAD"}:
                            unexpected.append({"method": request.method, "path": parsed.path})
                            route.abort()
                            return
                        if parsed.path == "/api/search" and parse_qs(parsed.query).get("q") == ["delay-old"]:
                            stale_routes.append(route)
                        else:
                            route.continue_()

                    context.route("**/*", route_request)
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    page.on("console", lambda event: errors.append("console_error") if event.type == "error" else None)
                    page.expose_function("recordSearchCsp", lambda _: errors.append("csp_error"))
                    page.add_init_script("document.addEventListener('securitypolicyviolation', event => window.recordSearchCsp(event.effectiveDirective));")
                    page.goto(origin + "/#token=" + token, wait_until="domcontentloaded")
                    page.locator('.nav-item[data-page="reader"]').click()
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(55)
                    search = page.locator("#conversation-search")
                    conversation = page.locator(f'[data-action="select-conversation"][data-id="{conversation_id}"]')
                    expect_avatar(conversation.locator(".conversation-avatar"), avatars["conversation"])
                    conversation.click()
                    expect(page.locator(".message-item")).to_have_count(100)
                    expect_avatar(page.locator("#conversation-header-avatar"), avatars["conversation"])
                    expect_avatar(page.locator(".message-item").first.locator(".message-avatar"), avatars["sender"])
                    expect(page.locator(".message-item.self .message-avatar .avatar-image")).to_have_count(1)
                    expect(page.locator(".message-item").nth(1).locator(".avatar-fallback")).to_be_visible()
                    checks.append("explicit_identity_avatars_in_list_heading_and_messages_without_sender_guessing")
                    check_avatar_fallbacks(page, conversation_id, 100, checks)
                    check_date_filters(page, checks)
                    check_date_pagination(page, "2024-09-15", TITLE, 350, checks)
                    check_date_races(page, checks)
                    if artifacts:
                        artifacts.mkdir(parents=True, exist_ok=True)
                    check_mobile_dates(page, checks, artifacts / "synthetic-date-mobile.png" if artifacts else None)

                    def query(value):
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search"
                                                  and parse_qs(urlsplit(response.url).query).get("q") == [value]):
                            search.fill(value)
                        expect(page.locator("#global-search-status")).to_have_attribute("data-query", value)
                        expect(page.locator("#global-search-status")).not_to_contain_text("正在搜索")

                    query("测试昵称")
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(2)
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(1)
                    expect(page.locator(".search-match-reason")).to_have_text("会话内发送者匹配 · 查看此会话")
                    expect(page.locator(".search-result-sender mark")).to_have_count(2)
                    expect_avatar(page.locator(".search-message-result").first.locator(".search-result-avatar"), avatars["sender"])
                    search.press("ArrowDown")
                    expect(page.locator('[data-action="select-conversation"]')).to_be_focused()
                    page.keyboard.press("Enter")
                    expect(page.locator("#conversation-title")).to_have_text(TITLE)
                    expect(page.locator(".message-item")).to_have_count(100)
                    expect(page.locator("#conversation-meta")).to_contain_text("最新在前")
                    assert rendered_ids(page) == expected_ids[:100], "The initial page is not newest first"
                    page.locator("#message-search").fill("测试昵称")
                    expect(page.locator(".message-item")).to_have_count(2)
                    assert rendered_ids(page) == sender_ids, "Filtered messages changed the descending order"
                    expect(page.locator(".message-meta mark")).to_have_count(2)
                    page.locator("#message-search").press("Escape")
                    expect(page.locator("#message-search")).to_have_value("")
                    expect(page.locator(".message-item")).to_have_count(100)
                    page.locator("#message-search").fill("测试昵称")
                    page.locator("#message-search").press("Enter")
                    expect(page.locator(".message-item")).to_have_count(2)
                    checks.append("global_and_conversation_sender_search_with_keyboard")

                    apply_date_search(page, "global", "公园", "2024-09-15", "2024-09-15")
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(50)
                    page.locator('[data-action="search-more"]').click()
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(100)
                    page.locator('[data-action="search-more"]').click()
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(130)
                    expect(page.locator('[data-action="search-more"]')).to_have_count(0)
                    assert len(set(page.locator('[data-action="open-search-message"]').evaluate_all("nodes => nodes.map(node => node.dataset.id)"))) == 130
                    checks.append("global_keyword_pagination_without_duplicates")
                    page.locator('#global-date-filter [data-action="clear-dates"]').click()
                    expect(page.locator("#global-date-from")).to_have_value("")

                    query("共享昵称")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(50)
                    expect(page.locator(".search-limit-note")).to_contain_text("前 50 个")
                    expect(page.locator("#global-search-status")).to_contain_text("51 个")
                    checks.append("conversation_limit_is_explicit")

                    query("银河计划")
                    target = page.locator(f'[data-action="open-search-message"][data-id="{target_id}"]')
                    expect(target).to_have_count(1)
                    expect(target.locator("mark")).to_have_text("银河计划")
                    expect_avatar(target.locator(".search-result-avatar"), avatars["sender"])
                    target.click()
                    expect(page.locator(".message-item.is-target")).to_have_count(1)
                    expect(page.locator(".message-item.is-target .message-text mark")).to_have_text("银河计划")
                    expect(page.locator("#message-context-bar")).to_be_visible()
                    expect(page.locator(".message-item")).to_have_count(100)
                    expect(page.locator(".message-item.is-target")).to_be_focused()
                    expect_avatar(page.locator("#conversation-header-avatar"), avatars["conversation"])
                    expect_avatar(page.locator(".message-item.is-target .message-avatar"), avatars["sender"])
                    checks.append("global_message_search_and_context_use_sender_avatars")
                    window_start = expected_ids.index(rendered_ids(page)[0])
                    assert rendered_ids(page) == expected_ids[window_start:window_start + 100]
                    expect(page.locator(".message-time-divider").first).to_contain_text("2024")
                    assert page.locator(".message-text img,.message-text script,.search-result-preview img").count() == 0
                    assert not page.evaluate("Boolean(window.searchXss)")
                    expect(page.locator('[data-action="load-earlier"]')).to_have_text("加载更新的消息")
                    page.locator('[data-action="load-earlier"]').click()
                    expect(page.locator(".message-item")).to_have_count(200)
                    window_start -= 100
                    assert rendered_ids(page) == expected_ids[window_start:window_start + 200], "Newer context pages skipped or repeated the boundary"
                    expect(page.locator('[data-action="load-more"]')).to_have_text("加载更早的消息")
                    page.locator('[data-action="load-more"]').click()
                    expect(page.locator(".message-item")).to_have_count(300)
                    assert rendered_ids(page) == expected_ids[window_start:window_start + 300], "Older context pages skipped or repeated the boundary"
                    page.locator('[data-action="exit-message-context"]').click()
                    expect(page.locator("#message-context-bar")).to_be_hidden()
                    expect(page.locator(".message-item")).to_have_count(100)
                    expect(page.locator(".message-item.is-target")).to_have_count(0)
                    checks.append("context_target_highlight_bidirectional_pagination_and_safe_text")
                    assert rendered_ids(page) == expected_ids[:100]
                    for visible in (200, 300, 350):
                        expect(page.locator('[data-action="load-more"]')).to_contain_text("加载更早的消息")
                        page.locator('[data-action="load-more"]').click()
                        expect(page.locator(".message-item")).to_have_count(visible)
                        assert rendered_ids(page) == expected_ids[:visible], "Descending pages are not continuous, including equal timestamps"
                    expect(page.locator('[data-action="load-more"]')).to_have_count(0)
                    checks.append("newest_first_default_filtered_and_contiguous_pagination")

                    for literal in ("100%_ready", "[brackets].*", "casefoldkeyword"):
                        query(literal)
                        expect(page.locator('[data-action="open-search-message"]')).to_have_count(1)
                        expect(page.locator(".search-result-preview mark")).to_have_count(1)
                    query("no-such-synthetic-result")
                    expect(page.locator("#conversation-list")).to_contain_text("没有找到匹配结果")
                    page.locator('[data-action="clear-global-search"]').first.click()
                    expect(search).to_have_value("")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(55)
                    checks.append("literal_casefold_empty_and_clear_search")

                    search.fill("delay-old")
                    deadline = time.monotonic() + 5
                    while not stale_routes and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    assert stale_routes, "The controlled old request did not start"
                    query("银河计划")
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(1)
                    stale_routes.pop().fulfill(status=200, content_type="application/json", json={
                        "query": "delay-old", "conversations": [], "conversation_total": 0,
                        "messages": [], "total": 0, "offset": 0, "limit": 50})
                    page.wait_for_timeout(100)
                    expect(page.locator('[data-action="open-search-message"]')).to_have_count(1)
                    expect(search).to_have_value("银河计划")
                    checks.append("late_global_search_response_is_ignored")

                    # A previous conversation's deferred search must not erase
                    # a newly selected message context after its timer expires.
                    page.locator("#message-search").evaluate("""input => {
                        input.value = 'stale-local-search';
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        document.querySelector('[data-action="open-search-message"]').click();
                    }""")
                    expect(page.locator(".message-item.is-target")).to_have_count(1)
                    page.wait_for_timeout(450)
                    expect(page.locator(".message-item.is-target")).to_have_attribute("data-message-id", str(target_id))
                    expect(page.locator("#message-context-bar")).to_be_visible()
                    expect(page.locator("#message-search")).to_have_value("")
                    checks.append("pending_local_search_cannot_erase_new_message_context")

                    page.set_viewport_size({"width": 400, "height": 900})
                    page.locator('[data-action="reader-back"]').click()
                    expect(search).to_be_visible()
                    fit(page)
                    if artifacts:
                        artifacts.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(artifacts / "synthetic-search-mobile.png"))
                    page.locator('[data-action="open-search-message"]').click()
                    expect(page.locator(".message-item.is-target")).to_have_count(1)
                    expect(search).to_be_hidden()
                    fit(page)
                    page.locator('[data-action="toggle-message-search"]').click()
                    page.locator("#message-search").fill("测试昵称")
                    expect(page.locator(".message-item")).to_have_count(2)
                    expect(page.locator("#message-context-bar")).to_be_hidden()
                    fit(page)
                    page.locator('[data-action="reader-back"]').click()
                    expect(search).to_be_visible()
                    search.press("Escape")
                    expect(page.locator('[data-action="select-conversation"]')).to_have_count(55)
                    checks.append("mobile_results_context_sender_filter_back_and_escape")
                    if artifacts:
                        page.set_viewport_size({"width": 1440, "height": 900})
                        query("银河计划")
                        expect(page.locator('[data-action="open-search-message"]')).to_have_count(1)
                        page.locator('[data-action="open-search-message"]').click()
                        expect(page.locator(".message-item.is-target")).to_have_count(1)
                        page.screenshot(path=str(artifacts / "synthetic-search-desktop.png"))
                    assert not errors, "Browser errors were reported: " + repr(errors)
                    assert not unexpected, "Unexpected requests were blocked: " + repr(unexpected)
                    checks.append("no_browser_csp_external_requests_or_mutations")
                    return {"ok": True, "checks": checks, "browser_errors": errors}
                finally:
                    browser.close()


def main():
    parser = argparse.ArgumentParser(description="Run the isolated, synthetic global-search browser checks")
    parser.add_argument("--browser", type=Path, help="Chrome/Chromium executable; otherwise use local Chrome or Playwright Chromium")
    parser.add_argument("--artifacts", type=Path, help="Optional output directory for synthetic screenshots")
    args = parser.parse_args()
    result = run(args.browser, args.artifacts)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
