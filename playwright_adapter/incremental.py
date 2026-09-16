import os
import random
import time

from playwright.sync_api import sync_playwright

from zhihu_archive import store as archive_store
from zhihu_archive.content import clean_file_name

from .activity_parser import (
    extract_content_identity_from_item,
    extract_content_metadata,
    extract_title_from_activity_item,
    normalize_target_url,
    normalize_text,
    parse_activity_time,
    should_archive_action,
)
from .config import RuntimeConfig
from .console import safe_print as print
from .exporter import (
    export_activity_item,
    get_save_dir_from_time_str,
    init_db,
    is_article_exists,
    require_markdownify,
    save_article_to_db,
)
from .page_detection import detect_blocked_zhihu_page, get_page_diagnostics, safe_page_snapshot


MAX_SCROLL_ATTEMPTS = 20
MAX_STALE_SCROLLS = 3


def resolve_incremental_new_limit(
    limit: int,
    continue_to_boundary: bool = False,
    max_new: int = 200,
    has_boundary: bool = True,
) -> int:
    if limit <= 0:
        raise ValueError("日常增量采集的 --limit 必须大于 0")
    if max_new <= 0:
        raise ValueError("--max-new 必须大于 0")
    if continue_to_boundary and max_new < limit:
        raise ValueError("使用 --continue-to-boundary 时，--max-new 不能小于 --limit")
    return max_new if continue_to_boundary and has_boundary else limit


def run_incremental(
    config: RuntimeConfig,
    limit=20,
    progress_callback=None,
    url: str | None = None,
    headed: bool = True,
    include_comments: bool = True,
    continue_to_boundary: bool = False,
    max_new: int = 200,
):
    require_markdownify()
    init_db(config)
    newly_scraped_titles = []
    collected_count = 0
    soft_limit_reported = False
    display_mode = "可见" if headed else "无头"
    print(f"\n🚀 [Scraper] 正在启动{display_mode}浏览器...")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        if not os.path.exists(config.state_file):
            print(f"❌ 找不到登录凭证: {config.state_file}")
            return ["[报错] 缺失 state.json 登录凭证"]
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state=config.state_file,
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        target_url = normalize_target_url(url, config.default_url)
        archive_run = archive_store.begin_run(config.db_file, target_url, "playwright")
        archive_run_id = archive_run["run_id"]
        boundary_extension_active = continue_to_boundary and bool(archive_run["boundary_keys"])
        effective_limit = resolve_incremental_new_limit(
            limit,
            continue_to_boundary,
            max_new,
            has_boundary=bool(archive_run["boundary_keys"]),
        )
        if continue_to_boundary and not archive_run["boundary_keys"]:
            print(f"ℹ️ [Scraper] 当前没有旧边界，本轮仍按 --limit {limit} 采集并建立初始边界。")
        print(f"👉 [Scraper] 访问知乎页面: {target_url}")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
        except Exception as first_error:
            print(f"⚠️ [Scraper] 首次加载失败，准备重试: {str(first_error)[:160]}")
            time.sleep(3)
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
            except Exception as second_error:
                safe_page_snapshot(page, "page-load-failed", config.project_dir)
                archive_store.abort_run(config.db_file, archive_run_id, "页面连续两次加载失败")
                browser.close()
                return [f"[报错] 页面加载失败: {str(second_error)[:160]}"]

        time.sleep(4)
        blocked_reason = detect_blocked_zhihu_page(page)
        if blocked_reason:
            safe_page_snapshot(page, "blocked-or-login", config.project_dir)
            archive_store.abort_run(config.db_file, archive_run_id, blocked_reason)
            browser.close()
            return [f"[报错] {blocked_reason}"]

        consecutive_exists_count = 0
        scroll_attempts = 0
        stale_scrolls = 0
        last_item_count = 0
        last_scroll_height = 0

        while collected_count < effective_limit:
            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "blocked-during-scroll", config.project_dir)
                archive_store.abort_run(config.db_file, archive_run_id, blocked_reason)
                browser.close()
                return newly_scraped_titles + [f"[报错] {blocked_reason}"]

            items = page.locator('.List-item')
            current_count = items.count()
            if current_count == 0:
                safe_page_snapshot(page, "no-list-items", config.project_dir)
                title, current_url = get_page_diagnostics(page)
                reason = f"未找到动态卡片，页面可能改版或登录态失效: {title} {current_url}"
                archive_store.abort_run(config.db_file, archive_run_id, reason)
                browser.close()
                return newly_scraped_titles + [f"[报错] {reason}"]
            found_new_in_this_loop = False

            for index in range(current_count):
                if collected_count >= effective_limit:
                    break
                item = items.nth(index)
                try:
                    meta_el = item.locator('.ActivityItem-meta')
                    if meta_el.count() == 0:
                        continue
                    meta_text = meta_el.inner_text(timeout=500).strip()
                    action_text_el = item.locator('.ActivityItem-metaTitle')
                    action_text = action_text_el.inner_text().strip() if action_text_el.count() > 0 else meta_text
                except Exception:
                    continue
                if not should_archive_action(action_text):
                    continue

                activity_dt, time_str = parse_activity_time(meta_text)
                title = extract_title_from_activity_item(item, action_text)
                clean_title_str = clean_file_name(f"{time_str} {title}")
                save_dir = get_save_dir_from_time_str(config, time_str)
                content_metadata = extract_content_metadata(item)
                content_identity = extract_content_identity_from_item(item)
                if content_identity:
                    content_identity["activity_time"] = time_str
                    if content_metadata.get("source_url"):
                        content_identity["url"] = content_metadata["source_url"]
                    observed = archive_store.record_seen(
                        config.db_file,
                        archive_run_id,
                        content_identity["content_key"],
                    )
                    if observed["boundary"]:
                        result = archive_store.finish_run(
                            config.db_file,
                            archive_run_id,
                            boundary_hit=True,
                        )
                        print(f"🛑 [Scraper] 命中上轮边界，增量抓取完成: {result}")
                        browser.close()
                        return newly_scraped_titles

                if is_article_exists(
                    config,
                    clean_title_str,
                    content_identity.get("content_key") if content_identity else None,
                ):
                    consecutive_exists_count += 1
                    if consecutive_exists_count > 10 and not archive_run["boundary_keys"]:
                        print("🛑 [Scraper] 连续遇到老文章，增量抓取结束。")
                        result = archive_store.finish_run(config.db_file, archive_run_id)
                        print(f"⚠️ [Scraper] 未命中边界，本轮不更新边界: {result}")
                        browser.close()
                        return newly_scraped_titles
                    continue

                consecutive_exists_count = 0
                found_new_in_this_loop = True
                if content_identity:
                    clean_title_str = archive_store.resolve_collision_title(save_dir, time_str, title)
                print(f"\n[Scraper] 处理新动态：{clean_title_str}")
                if progress_callback:
                    progress_callback(collected_count + 1, effective_limit, clean_title_str)
                md_file_path = export_activity_item(
                    config,
                    page,
                    item,
                    title,
                    clean_title_str,
                    save_dir,
                    activity_dt,
                    action_text,
                    content_identity=content_identity,
                    include_comments=include_comments,
                )
                save_article_to_db(config, clean_title_str, content_identity, md_file_path)
                archive_store.increment_run_new_count(config.db_file, archive_run_id)
                newly_scraped_titles.append(clean_title_str)
                collected_count += 1
                if (
                    boundary_extension_active
                    and not soft_limit_reported
                    and collected_count >= limit
                    and collected_count < effective_limit
                ):
                    print(
                        f"⚠️ [Scraper] 已达到预期数量 {limit}，但尚未命中旧边界，"
                        f"将继续扫描（安全上限 {max_new}）……"
                    )
                    soft_limit_reported = True

            if not found_new_in_this_loop:
                scroll_attempts += 1
                try:
                    current_scroll_height = page.evaluate("document.body.scrollHeight")
                except Exception:
                    current_scroll_height = 0
                if current_count <= last_item_count and current_scroll_height <= last_scroll_height:
                    stale_scrolls += 1
                else:
                    stale_scrolls = 0
                last_item_count = max(last_item_count, current_count)
                last_scroll_height = max(last_scroll_height, current_scroll_height)
                if scroll_attempts >= MAX_SCROLL_ATTEMPTS or stale_scrolls >= MAX_STALE_SCROLLS:
                    reason = (
                        f"滚动未能找到新内容或上轮边界，"
                        f"尝试={scroll_attempts}，连续无变化={stale_scrolls}"
                    )
                    safe_page_snapshot(page, "scroll-stalled", config.project_dir)
                    result = archive_store.abort_run(config.db_file, archive_run_id, reason)
                    print(f"🛑 [Scraper] {reason}: {result}")
                    browser.close()
                    return newly_scraped_titles + [f"[报错] {reason}"]
                print("⏬ [Scraper] 向下滚动加载...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(2.5, 4.0))
            else:
                scroll_attempts = 0
                stale_scrolls = 0

        result = archive_store.finish_run(config.db_file, archive_run_id)
        if result["status"] == "complete":
            print(f"✅ [Scraper] 已建立初始采集边界: {result}")
        elif boundary_extension_active:
            print(
                f"⚠️ [Scraper] 已达到安全上限 {max_new}，"
                f"仍未命中旧边界，本轮不视为完整采集: {result}"
            )
        else:
            print(f"⚠️ [Scraper] 已达到数量上限，未命中旧边界时不视为完整采集: {result}")
        browser.close()
    return newly_scraped_titles
