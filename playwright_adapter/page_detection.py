import os
import re

from .config import PROJECT_DIR
from .console import safe_print as print


def safe_page_snapshot(page, reason, project_dir: str = PROJECT_DIR):
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason or "failure").strip("-")
    screenshot_path = os.path.join(project_dir, f"zhihu_last_{safe_reason or 'failure'}.png")
    try:
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"📷 [Scraper] 已保存异常页面截图: {screenshot_path}")
    except Exception as exc:
        print(f"⚠️ [Scraper] 保存异常页面截图失败: {str(exc)[:120]}")
    return screenshot_path


def get_page_diagnostics(page):
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        url = page.url
    except Exception:
        url = ""
    return title, url


def detect_blocked_zhihu_page(page):
    title, url = get_page_diagnostics(page)
    if "account/unhuman" in url or "安全验证" in title:
        return f"知乎安全验证/反爬拦截，当前页面: {title or '未知标题'} {url}"
    if "signin" in url or "登录" in title:
        return f"知乎登录态可能失效，当前页面: {title or '未知标题'} {url}"
    return ""
