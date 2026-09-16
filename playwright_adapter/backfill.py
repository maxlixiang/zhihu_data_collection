import os
import random
import time
from datetime import datetime, time as datetime_time

from playwright.sync_api import sync_playwright

from zhihu_archive import store as archive_store
from zhihu_archive.content import clean_file_name

from .activity_parser import (
    SUPPORTED_ACTION_KEYWORDS,
    extract_content_identity_from_item,
    extract_title_from_activity_item,
    normalize_target_url,
    parse_activity_time,
    should_archive_action,
    slug_from_url,
)
from .config import RuntimeConfig
from .console import safe_print as print
from .exporter import (
    export_activity_item,
    get_flat_save_dir,
    get_save_dir_from_time_str,
    init_db,
    is_article_exists,
    require_markdownify,
    save_article_to_db,
)
from .page_detection import detect_blocked_zhihu_page, safe_page_snapshot


def run_backfill(
    config: RuntimeConfig,
    start_date: str,
    end_date: str,
    limit: int = 0,
    max_scrolls: int = 10000,
    flat_output: bool = True,
    url: str | None = None,
    output_dir: str | None = None,
    seek_delay_min: float = 0.6,
    seek_delay_max: float = 1.2,
    collect_delay_min: float = 1.2,
    collect_delay_max: float = 2.0,
    seek_tail_count: int = 30,
    seek_scroll_burst: int = 3,
    headed: bool = True,
    include_comments: bool = True,
):
    """Backfill a date range from the profile activity stream without opening detail pages."""
    require_markdownify()
    if seek_delay_min > seek_delay_max or collect_delay_min > collect_delay_max:
        raise ValueError("延迟参数的最小值不能大于最大值")
    start_dt = datetime.combine(datetime.fromisoformat(start_date).date(), datetime_time.min)
    end_dt = datetime.combine(datetime.fromisoformat(end_date).date(), datetime_time.max).replace(microsecond=0)
    if start_dt > end_dt:
        raise ValueError("start-date 不能晚于 end-date")
    target_url = normalize_target_url(url, config.default_url)
    target_slug = slug_from_url(target_url)
    target_output_dir = output_dir or config.local_archive_root_dir

    init_db(config)
    saved = []
    seen_keys = set()
    consecutive_no_new_visible = 0
    phase = "seek"
    collect_next_index = 0

    print("\n🚀 [Backfill] 本地历史回溯启动")
    print(f"   目标 URL: {target_url}")
    print(f"   目标标识: {target_slug}")
    print(f"   时间范围: {start_dt} ~ {end_dt}")
    print(f"   输出目录: {os.path.abspath(target_output_dir if flat_output else config.archive_root_dir)}")
    print(f"   采样上限: {limit if limit else '不限'}")
    print(f"   动作类型: {', '.join(SUPPORTED_ACTION_KEYWORDS)}")
    print(f"   评论采集: {'开启' if include_comments else '关闭'}")
    print(f"   seek 滚动等待: {seek_delay_min}-{seek_delay_max}s")
    print(f"   collect 滚动等待: {collect_delay_min}-{collect_delay_max}s")
    print(f"   seek 每轮只检查尾部卡片数: {seek_tail_count}")
    print(f"   seek 连续滚动次数: {seek_scroll_burst}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        if not os.path.exists(config.state_file):
            print(f"❌ 找不到登录凭证: {config.state_file}")
            return saved
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state=config.state_file,
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        try:
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
            except Exception as first_error:
                print(f"⚠️ [Backfill] 首次加载失败，准备重试: {str(first_error)[:160]}")
                time.sleep(3)
                page.goto(target_url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
            time.sleep(4)
            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "backfill-blocked-or-login", config.project_dir)
                print(f"❌ [Backfill] {blocked_reason}")
                return saved

            for scroll_idx in range(max_scrolls):
                blocked_reason = detect_blocked_zhihu_page(page)
                if blocked_reason:
                    safe_page_snapshot(page, "backfill-blocked-during-scroll", config.project_dir)
                    print(f"❌ [Backfill] {blocked_reason}")
                    return saved
                items = page.locator('.List-item')
                current_count = items.count()
                if current_count == 0:
                    safe_page_snapshot(page, "backfill-no-list-items", config.project_dir)
                    print("❌ [Backfill] 没有找到动态卡片，可能登录态失效或页面结构变化。")
                    return saved

                new_visible = 0
                oldest_dt = None
                newest_dt = None
                visible_records = []
                if phase == "seek":
                    scan_start = max(0, current_count - seek_tail_count)
                    scan_end = current_count
                else:
                    scan_start = min(collect_next_index, current_count)
                    scan_end = current_count

                for index in range(scan_start, scan_end):
                    if limit and len(saved) >= limit:
                        print(f"🛑 [Backfill] 已达到采样上限 {limit}")
                        return saved
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
                    if not activity_dt:
                        continue
                    oldest_dt = activity_dt if oldest_dt is None else min(oldest_dt, activity_dt)
                    newest_dt = activity_dt if newest_dt is None else max(newest_dt, activity_dt)
                    title = extract_title_from_activity_item(item, action_text)
                    clean_title_str = clean_file_name(f"{time_str} {title}")
                    content_identity = extract_content_identity_from_item(item)
                    if content_identity:
                        content_identity["activity_time"] = time_str
                    unique_key = content_identity.get("content_key") if content_identity else clean_title_str
                    if unique_key not in seen_keys:
                        new_visible += 1
                        seen_keys.add(unique_key)
                    visible_records.append(
                        (item, activity_dt, time_str, title, clean_title_str, content_identity, action_text)
                    )

                if phase == "seek" and oldest_dt and oldest_dt <= end_dt:
                    phase = "collect"
                    collect_next_index = scan_start
                    print(f"✅ [Backfill] 已找到 {end_date} 及以前的动态，进入 collect 阶段。当前最旧={oldest_dt}")

                if phase == "collect":
                    for (
                        item, activity_dt, time_str, title, clean_title_str,
                        content_identity, action_text,
                    ) in visible_records:
                        if limit and len(saved) >= limit:
                            print(f"🛑 [Backfill] 已达到采样上限 {limit}")
                            return saved
                        if activity_dt > end_dt:
                            continue
                        if activity_dt < start_dt:
                            print(f"🛑 [Backfill] 已滚动到起始日期以前: {activity_dt}")
                            return saved
                        if is_article_exists(
                            config,
                            clean_title_str,
                            content_identity.get("content_key") if content_identity else None,
                        ):
                            continue
                        print(f"\n[Backfill] 导出动态：{clean_title_str}")
                        save_dir = (
                            get_flat_save_dir(config, target_output_dir)
                            if flat_output
                            else get_save_dir_from_time_str(config, time_str, target_output_dir)
                        )
                        if content_identity:
                            clean_title_str = archive_store.resolve_collision_title(save_dir, time_str, title)
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
                        saved.append(clean_title_str)
                    collect_next_index = scan_end

                consecutive_no_new_visible = 0 if new_visible else consecutive_no_new_visible + 1
                oldest_text = oldest_dt.strftime("%Y-%m-%d %H:%M") if oldest_dt else "N/A"
                newest_text = newest_dt.strftime("%Y-%m-%d %H:%M") if newest_dt else "N/A"
                print(
                    f"⏬ [Backfill {phase} {scroll_idx + 1}/{max_scrolls}] "
                    f"卡片={current_count} 扫描={scan_end - scan_start} 新可见={new_visible} "
                    f"连续无新={consecutive_no_new_visible} 最新={newest_text} "
                    f"最旧={oldest_text} 已保存={len(saved)}"
                )
                if consecutive_no_new_visible >= 20:
                    print("🛑 [Backfill] 连续多次无新可见动态，停止。")
                    return saved
                if phase == "seek":
                    for burst_index in range(max(1, seek_scroll_burst)):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        if burst_index < seek_scroll_burst - 1:
                            time.sleep(0.2)
                    time.sleep(random.uniform(seek_delay_min, seek_delay_max))
                else:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(random.uniform(collect_delay_min, collect_delay_max))
        except Exception as exc:
            safe_page_snapshot(page, "backfill-failed", config.project_dir)
            print(f"❌ [Backfill] 运行失败: {str(exc)[:300]}")
            return saved
        finally:
            context.close()
            browser.close()
    return saved
