"""Stable CLI and compatibility facade for the Playwright Zhihu collector."""

import argparse
from datetime import date

from zhihu_archive.content import build_frontmatter, clean_file_name, download_img_and_replace_md_link

from playwright_adapter.activity_parser import (
    SUPPORTED_ACTION_KEYWORDS,
    clean_html_text,
    clean_zhihu_time_text,
    extract_answer_id_from_item,
    extract_content_identity_from_item,
    extract_content_metadata,
    extract_title_from_activity_item,
    format_zhihu_iso_time,
    normalize_source_url,
    normalize_target_url as _normalize_target_url,
    normalize_text,
    parse_activity_time,
    should_archive_action,
    slug_from_url,
)
from playwright_adapter.backfill import run_backfill
from playwright_adapter.comments import (
    COMMENT_API_PAGE_SIZE,
    DEFAULT_HOT_COMMENT_LIMIT,
    DEFAULT_REPLY_LIMIT_PER_COMMENT,
    DEFAULT_ROOT_COMMENT_LIMIT,
    MAX_ROOT_COMMENT_PAGES,
    extract_comment_author_name,
    fetch_child_comments_via_api,
    fetch_comments_via_api,
    fetch_first_page_comments_via_api,
    format_comments_markdown,
    parse_api_comment,
)
from playwright_adapter.config import (
    DEFAULT_URL,
    PAGE_LOAD_TIMEOUT_MS,
    PROJECT_DIR,
    default_runtime_config,
    with_runtime_overrides,
)
from playwright_adapter.console import safe_print as print
from playwright_adapter.debug import (
    extract_debug_card_text,
    print_debug_comment_report,
    print_debug_full_report,
    run_debug_comments as _run_debug_comments,
)
from playwright_adapter.exporter import (
    export_activity_item,
    get_flat_save_dir as _get_flat_save_dir,
    get_save_dir_from_time_str as _get_save_dir_from_time_str,
    init_db as _init_db,
    is_article_exists as _is_article_exists,
    save_article_to_db as _save_article_to_db,
)
from playwright_adapter.incremental import resolve_incremental_new_limit, run_incremental
from playwright_adapter.page_detection import (
    detect_blocked_zhihu_page,
    get_page_diagnostics,
    safe_page_snapshot as _safe_page_snapshot,
)


PROFILE_URL = DEFAULT_URL
AUTHOR_NAME = "Juan"
_runtime_config = default_runtime_config()


def _sync_legacy_path_constants():
    global ARCHIVE_ROOT_DIR, LOCAL_ARCHIVE_ROOT_DIR, DB_FILE, STATE_FILE
    ARCHIVE_ROOT_DIR = _runtime_config.archive_root_dir
    LOCAL_ARCHIVE_ROOT_DIR = _runtime_config.local_archive_root_dir
    DB_FILE = _runtime_config.db_file
    STATE_FILE = _runtime_config.state_file


_sync_legacy_path_constants()


def configure_runtime_paths(output_dir=None, db_file=None, state_file=None):
    """Apply CLI path overrides while preserving legacy module-level constants."""
    global _runtime_config
    _runtime_config = with_runtime_overrides(
        _runtime_config,
        output_dir=output_dir,
        db_file=db_file,
        state_file=state_file,
    )
    _sync_legacy_path_constants()
    return _runtime_config


def normalize_target_url(url: str | None = None) -> str:
    return _normalize_target_url(url, _runtime_config.default_url)


def safe_page_snapshot(page, reason):
    return _safe_page_snapshot(page, reason, _runtime_config.project_dir)


def init_db():
    return _init_db(_runtime_config)


def is_article_exists(title, content_key=None):
    return _is_article_exists(_runtime_config, title, content_key)


def save_article_to_db(title, content_identity=None, markdown_path="", source_method="playwright"):
    return _save_article_to_db(
        _runtime_config,
        title,
        content_identity,
        markdown_path,
        source_method,
    )


def get_save_dir_from_time_str(time_str: str, root_dir: str | None = None) -> str:
    return _get_save_dir_from_time_str(_runtime_config, time_str, root_dir)


def get_flat_save_dir(output_dir: str | None = None) -> str:
    return _get_flat_save_dir(_runtime_config, output_dir)


def export_activity_item_from_profile(*args, **kwargs):
    return export_activity_item(_runtime_config, *args, **kwargs)


def run_debug_comments(url: str | None = None, headed: bool = False):
    return _run_debug_comments(_runtime_config, url=url, headed=headed)


def run_zhihu_scraper(
    limit=20,
    progress_callback=None,
    url: str | None = None,
    headed: bool = True,
    include_comments: bool = True,
    continue_to_boundary: bool = False,
    max_new: int = 200,
):
    return run_incremental(
        _runtime_config,
        limit=limit,
        progress_callback=progress_callback,
        url=url,
        headed=headed,
        include_comments=include_comments,
        continue_to_boundary=continue_to_boundary,
        max_new=max_new,
    )


def run_local_backfill(
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
    return run_backfill(
        _runtime_config,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        max_scrolls=max_scrolls,
        flat_output=flat_output,
        url=url,
        output_dir=output_dir,
        seek_delay_min=seek_delay_min,
        seek_delay_max=seek_delay_max,
        collect_delay_min=collect_delay_min,
        collect_delay_max=collect_delay_max,
        seek_tail_count=seek_tail_count,
        seek_scroll_burst=seek_scroll_burst,
        headed=headed,
        include_comments=include_comments,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知乎动态归档爬虫")
    parser.add_argument(
        "--debug-comments",
        action="store_true",
        help="只测试第一条动态评论，不写数据库、不保存文件、不推 GitHub",
    )
    parser.add_argument("--limit", type=int, default=5, help="正常抓取模式下最多处理的新动态数量，默认 5")
    parser.add_argument(
        "--continue-to-boundary",
        action="store_true",
        help="达到 --limit 后若尚未命中旧边界，继续采集到边界或 --max-new",
    )
    parser.add_argument("--max-new", type=int, default=200, help="继续到旧边界模式的新内容安全上限，默认 200")
    parser.add_argument("--url", default=None, help="知乎可滚动列表页 URL，例如个人主页、回答页、文章页、收藏夹页等")
    parser.add_argument("--output-dir", default=None, help="Markdown 输出目录，默认 data/articles")
    parser.add_argument("--db-file", default=DB_FILE, help="共享去重数据库路径")
    parser.add_argument("--state-file", default=STATE_FILE, help="Playwright 登录态文件路径")
    parser.add_argument("--headless", action="store_true", help="使用无头浏览器；默认显示浏览器窗口")
    parser.add_argument("--no-comments", action="store_true", help="不采集评论")
    parser.add_argument(
        "--backfill-local",
        action="store_true",
        help="本地历史回溯：只在主页动态流展开并保存 Markdown，不打开详情页",
    )
    parser.add_argument("--start-date", default="2020-01-01", help="动态发生日期起点，默认 2020-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="动态发生日期终点，默认今天")
    parser.add_argument("--max-scrolls", type=int, default=10000, help="最大滚动次数，默认 10000")
    parser.add_argument("--seek-delay-min", type=float, default=0.6, help="seek 阶段每次滚动最短等待秒数")
    parser.add_argument("--seek-delay-max", type=float, default=1.2, help="seek 阶段每次滚动最长等待秒数")
    parser.add_argument("--collect-delay-min", type=float, default=1.2, help="collect 阶段每次滚动最短等待秒数")
    parser.add_argument("--collect-delay-max", type=float, default=2.0, help="collect 阶段每次滚动最长等待秒数")
    parser.add_argument("--seek-tail-count", type=int, default=30, help="seek 阶段每轮只检查最后 N 个卡片")
    parser.add_argument("--seek-scroll-burst", type=int, default=3, help="seek 阶段每轮连续滚动次数")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_runtime_paths(args.output_dir, args.db_file, args.state_file)
    if not args.backfill_local and not args.debug_comments:
        try:
            resolve_incremental_new_limit(args.limit, args.continue_to_boundary, args.max_new)
        except ValueError as exc:
            parser.error(str(exc))
    if args.debug_comments:
        return run_debug_comments(url=args.url, headed=not args.headless)
    if args.backfill_local:
        print(
            run_local_backfill(
                start_date=args.start_date,
                end_date=args.end_date,
                limit=args.limit,
                max_scrolls=args.max_scrolls,
                flat_output=False,
                url=args.url,
                output_dir=args.output_dir,
                seek_delay_min=args.seek_delay_min,
                seek_delay_max=args.seek_delay_max,
                collect_delay_min=args.collect_delay_min,
                collect_delay_max=args.collect_delay_max,
                seek_tail_count=args.seek_tail_count,
                seek_scroll_burst=args.seek_scroll_burst,
                headed=not args.headless,
                include_comments=not args.no_comments,
            )
        )
    else:
        print(
            run_zhihu_scraper(
                limit=args.limit,
                url=args.url,
                headed=not args.headless,
                include_comments=not args.no_comments,
                continue_to_boundary=args.continue_to_boundary,
                max_new=args.max_new,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
