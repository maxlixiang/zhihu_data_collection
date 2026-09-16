import os
import time
import re
import requests
import random
import sqlite3
import builtins
import html
import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlparse
from playwright.sync_api import sync_playwright

from zhihu_archive import store as archive_store
from zhihu_archive.content import build_frontmatter, clean_file_name, download_img_and_replace_md_link

try:
    from markdownify import markdownify as md
except ModuleNotFoundError:
    md = None

# 🌟 强制刷新所有 print 输出，防止 Docker 吞弃日志
def print(*args, **kwargs):
    kwargs['flush'] = True
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_args = [
            str(arg).encode(encoding, errors="replace").decode(encoding)
            for arg in args
        ]
        builtins.print(*safe_args, **kwargs)

DEFAULT_URL = os.getenv("ZHIHU_URL", "https://www.zhihu.com/people/li-xiang-57-76")
PROFILE_URL = DEFAULT_URL
AUTHOR_NAME = "Juan"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.getenv("ZH_DB_FILE", os.path.join(PROJECT_DIR, "zhihu_articles.db"))
STATE_FILE = os.getenv("ZHIHU_STATE_FILE", os.path.join(PROJECT_DIR, "state.json"))

ARCHIVE_ROOT_DIR = os.getenv(
    "ARCHIVE_ROOT_DIR",
    os.path.join(PROJECT_DIR, "data", "articles"),
)
LOCAL_ARCHIVE_ROOT_DIR = os.getenv("LOCAL_ARCHIVE_ROOT_DIR", ARCHIVE_ROOT_DIR)
PAGE_LOAD_TIMEOUT_MS = 30000
MAX_SCROLL_ATTEMPTS = 20
MAX_STALE_SCROLLS = 3
DEFAULT_ROOT_COMMENT_LIMIT = 30
DEFAULT_HOT_COMMENT_LIMIT = 2
DEFAULT_REPLY_LIMIT_PER_COMMENT = 10
COMMENT_API_PAGE_SIZE = 20
MAX_ROOT_COMMENT_PAGES = 5
SUPPORTED_ACTION_KEYWORDS = ("赞同", "发布", "发表", "收藏", "喜欢")

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhihu.com/"
}


def configure_runtime_paths(output_dir=None, db_file=None, state_file=None):
    """允许命令行覆盖文章目录、去重数据库和登录态路径。"""
    global ARCHIVE_ROOT_DIR, LOCAL_ARCHIVE_ROOT_DIR, DB_FILE, STATE_FILE
    if output_dir:
        ARCHIVE_ROOT_DIR = os.path.abspath(output_dir)
        LOCAL_ARCHIVE_ROOT_DIR = ARCHIVE_ROOT_DIR
    if db_file:
        DB_FILE = os.path.abspath(db_file)
    if state_file:
        STATE_FILE = os.path.abspath(state_file)


def should_archive_action(action_text):
    return any(keyword in (action_text or "") for keyword in SUPPORTED_ACTION_KEYWORDS)


def safe_page_snapshot(page, reason):
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason or "failure").strip("-")
    screenshot_path = os.path.join(PROJECT_DIR, f"zhihu_last_{safe_reason or 'failure'}.png")
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

def normalize_target_url(url: str | None = None) -> str:
    """支持完整知乎 URL，或 people slug；完整 URL 原样使用，不追加新路径。"""
    value = (url or DEFAULT_URL).strip().rstrip("/")
    if not value:
        value = DEFAULT_URL
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/people/"):
        value = value.split("/people/", 1)[1].strip("/")
    return f"https://www.zhihu.com/people/{value}"

def slug_from_url(target_url: str) -> str:
    path = urlparse(target_url).path.strip("/")
    parts = path.split("/")
    if "people" in parts:
        idx = parts.index("people")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if "collection" in parts and len(parts) > 1:
        return f"collection-{parts[-1]}"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", path).strip("-") or "zhihu"

# --- 数据库操作 ---
def init_db():
    archive_store.init_db(DB_FILE)

def is_article_exists(title, content_key=None):
    return archive_store.is_known(DB_FILE, content_key, title)

def save_article_to_db(title, content_identity=None, markdown_path="", source_method="playwright"):
    if not content_identity or not content_identity.get("content_key"):
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT OR IGNORE INTO articles (title) VALUES (?)", (title,))
        conn.commit()
        conn.close()
        return
    archive_store.register_success(
        DB_FILE,
        content_key=content_identity["content_key"],
        source_url=content_identity.get("url", ""),
        activity_time=content_identity.get("activity_time", ""),
        archive_title=title,
        markdown_path=os.path.abspath(markdown_path) if markdown_path else "",
        image_dir=os.path.abspath(os.path.splitext(markdown_path)[0]) if markdown_path else "",
        source_method=source_method,
        file_sha256="",
    )

# --- 文本与图片处理 ---
def get_save_dir_from_time_str(time_str: str, root_dir: str | None = None) -> str:
    match = re.match(r"\[(\d{4})-(\d{2})-\d{2}_\d{2}-\d{2}\]", time_str)
    if match:
        year, month = match.groups()
    else:
        year, month = time.strftime("%Y"), time.strftime("%m")

    save_dir = os.path.join(root_dir or ARCHIVE_ROOT_DIR, year, month)
    os.makedirs(save_dir, exist_ok=True)
    return save_dir

def get_flat_save_dir(output_dir: str | None = None) -> str:
    target = output_dir or LOCAL_ARCHIVE_ROOT_DIR
    os.makedirs(target, exist_ok=True)
    return target

# 🌟 完全采用你验证过的纯净正则清洗方案
def clean_html_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()

def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_source_url(url: str) -> str:
    url = (url or "").strip()
    return f"https:{url}" if url.startswith("//") else url


def clean_zhihu_time_text(value: str) -> str:
    value = normalize_text(value)
    if not value:
        return ""
    value = re.sub(r"^(发布于|编辑于)", "", value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?", value)
    return match.group(0) if match else value


def format_zhihu_iso_time(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        china_time = parsed.astimezone(timezone(timedelta(hours=8)))
        return china_time.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def extract_content_metadata(item):
    data = item.evaluate(
        """
        (node) => {
            const result = {
                author: '', published_at: '', edited_at: '', source_url: '',
                source_type: '', answer_id: '', date_created: '', date_modified: ''
            };
            const pickText = (selectors) => {
                for (const selector of selectors) {
                    const el = node.querySelector(selector);
                    const text = el && (el.innerText || el.textContent || '').trim();
                    if (text) return text;
                }
                return '';
            };
            const contentItem = node.querySelector('.ContentItem');
            if (contentItem) {
                const zopStr = contentItem.getAttribute('data-zop');
                if (zopStr) {
                    try {
                        const zop = JSON.parse(zopStr);
                        result.author = String(zop.authorName || zop.author_name || '');
                        result.source_type = String(zop.type || '').toLowerCase();
                        result.answer_id = String(zop.itemId || zop.item_id || zop.id || '');
                    } catch (e) {}
                }
            }
            if (!result.author) {
                result.author = pickText([
                    '.AuthorInfo-name', '.AuthorInfo .UserLink-link',
                    '.ContentItem-meta .UserLink-link', 'a[href*="/people/"]'
                ]);
            }
            const timeLink = node.querySelector(
                '.ContentItem-time a[href], a[data-tooltip*="发布于"][href], a[aria-label*="发布于"][href]'
            );
            if (timeLink) {
                result.source_url = timeLink.href || timeLink.getAttribute('href') || '';
                result.published_at = timeLink.getAttribute('data-tooltip')
                    || timeLink.getAttribute('aria-label') || '';
                result.edited_at = timeLink.innerText || timeLink.textContent || '';
            }
            const urlMeta = node.querySelector('meta[itemprop="url"]');
            const createdMeta = node.querySelector('meta[itemprop="dateCreated"]');
            const modifiedMeta = node.querySelector('meta[itemprop="dateModified"]');
            if (!result.source_url && urlMeta) result.source_url = urlMeta.getAttribute('content') || '';
            if (createdMeta) result.date_created = createdMeta.getAttribute('content') || '';
            if (modifiedMeta) result.date_modified = modifiedMeta.getAttribute('content') || '';
            return result;
        }
        """
    )
    published_at = clean_zhihu_time_text(data.get("published_at", ""))
    if not published_at:
        published_at = format_zhihu_iso_time(data.get("date_created", ""))
    source_type = normalize_text(data.get("source_type", "")).lower()
    if source_type == "post":
        source_type = "article"
    return {
        "author": normalize_text(data.get("author", "")),
        "published_at": published_at,
        "edited_at": clean_zhihu_time_text(data.get("edited_at", ""))
        or format_zhihu_iso_time(data.get("date_modified", "")),
        "source_url": normalize_source_url(data.get("source_url", "")),
        "source_type": source_type,
        "answer_id": normalize_text(data.get("answer_id", "")) if source_type == "answer" else "",
    }

def extract_content_identity_from_item(item):
    hrefs = item.evaluate(
        """
        (node) => Array.from(node.querySelectorAll('a[href]'))
            .map((el) => el.href || el.getAttribute('href') || '')
            .filter(Boolean)
        """
    )

    for href in hrefs:
        content_type, content_id, content_key = archive_store.content_identity_from_url(href)
        if content_key:
            print(f"   ✅ 成功从链接提取内容 ID: {content_key}")
            return {
                "content_type": content_type,
                "content_id": content_id,
                "content_key": content_key,
                "url": href,
            }

    zop_identity = item.evaluate(
        """
        (node) => {
            const contentItem = node.querySelector('.ContentItem');
            if (!contentItem) return null;
            const zopStr = contentItem.getAttribute('data-zop');
            if (!zopStr) return null;
            try {
                const zop = JSON.parse(zopStr);
                const type = String(zop.type || '').toLowerCase();
                if (['answer', 'article', 'post', 'pin'].includes(type)) {
                    return {type, id: String(zop.itemId || zop.item_id || zop.id || '')};
                }
            } catch (e) {}
            return null;
        }
        """
    )
    if zop_identity and zop_identity.get("id"):
        content_type = "article" if zop_identity["type"] == "post" else zop_identity["type"]
        content_id = zop_identity["id"]
        content_key = f"{content_type}:{content_id}"
        if content_type == "answer":
            url = f"https://www.zhihu.com/answer/{content_id}"
        elif content_type == "article":
            url = f"https://zhuanlan.zhihu.com/p/{content_id}"
        else:
            url = f"https://www.zhihu.com/pin/{content_id}"
        print(f"   ✅ 成功从 data-zop 提取内容 ID: {content_key}")
        return {
            "content_type": content_type,
            "content_id": content_id,
            "content_key": content_key,
            "url": url,
        }

    print(f"   ⏭️ 当前动态未找到回答链接，扫描到链接数: {len(hrefs)}")
    return None


def extract_answer_id_from_item(item):
    identity = extract_content_identity_from_item(item)
    if identity and identity["content_type"] == "answer":
        return identity["content_id"]
    return None

def extract_comment_author_name(comment):
    author_info = comment.get("author") or {}
    member_info = author_info.get("member") or {}
    return normalize_text(
        str(
            member_info.get("name")
            or author_info.get("name")
            or comment.get("author_name")
            or "匿名用户"
        )
    )


def parse_api_comment(comment):
    raw_content = comment.get("content") or comment.get("comment") or comment.get("text") or ""
    clean_content = clean_html_text(str(raw_content))
    if not clean_content or "已删除" in clean_content:
        return None

    reply_to = comment.get("reply_to_author") or {}
    reply_to_member = reply_to.get("member") or {}
    reply_to_name = normalize_text(
        str(reply_to_member.get("name") or reply_to.get("name") or "")
    )
    return {
        "id": str(comment.get("id") or ""),
        "author": extract_comment_author_name(comment),
        "content": clean_content,
        "vote_count": int(comment.get("vote_count") or 0),
        "reply_to": reply_to_name,
        "child_comment_count": int(comment.get("child_comment_count") or 0),
        "replies": [],
    }


def fetch_child_comments_via_api(page, root_comment_id, limit=DEFAULT_REPLY_LIMIT_PER_COMMENT):
    api_url = (
        f"https://www.zhihu.com/api/v4/comments/{root_comment_id}/child_comments"
        f"?limit={limit}&offset=0"
    )
    response = page.context.request.get(
        api_url,
        headers={
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "fetch",
            "referer": page.url,
        },
    )
    if not response.ok:
        response_text = response.text()[:200]
        raise RuntimeError(f"评论回复 API 请求失败: HTTP {response.status}, body={response_text}")

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"评论回复 API 返回结构异常: keys={list(payload.keys())}")
    replies = [parsed for item in data if (parsed := parse_api_comment(item))]
    return replies[:limit]


def fetch_first_page_comments_via_api(
    page,
    answer_id,
    limit=DEFAULT_ROOT_COMMENT_LIMIT,
    hot_comment_limit=DEFAULT_HOT_COMMENT_LIMIT,
    reply_limit=DEFAULT_REPLY_LIMIT_PER_COMMENT,
):
    comments = []
    source_items = []
    seen_ids = set()
    raw_count = 0
    offset = 0

    for page_number in range(MAX_ROOT_COMMENT_PAGES):
        if len(comments) >= limit:
            break
        page_limit = min(COMMENT_API_PAGE_SIZE, limit - len(comments))
        api_url = (
            f"https://www.zhihu.com/api/v4/answers/{answer_id}/root_comments"
            f"?limit={page_limit}&offset={offset}&order=normal&status=open"
        )
        response = page.context.request.get(
            api_url,
            headers={
                "accept": "application/json, text/plain, */*",
                "x-requested-with": "fetch",
                "referer": page.url,
            },
        )
        if not response.ok:
            response_text = response.text()[:200]
            if page_number == 0:
                raise RuntimeError(
                    f"评论 API 请求失败: HTTP {response.status}, body={response_text}"
                )
            print(
                f"   ⚠️ 顶层评论第 {page_number + 1} 页请求失败，"
                f"保留已获取的 {len(comments)} 条: HTTP {response.status}"
            )
            break

        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            if page_number == 0:
                raise RuntimeError(f"评论 API 返回结构异常: keys={list(payload.keys())}")
            print(f"   ⚠️ 顶层评论第 {page_number + 1} 页结构异常，停止分页。")
            break
        if not data:
            break

        raw_count += len(data)
        offset += len(data)
        for item in data:
            parsed = parse_api_comment(item)
            comment_id = parsed["id"] if parsed else ""
            if parsed and (not comment_id or comment_id not in seen_ids):
                comments.append(parsed)
                source_items.append(item)
                if comment_id:
                    seen_ids.add(comment_id)
            if len(comments) >= limit:
                break

        if len(data) < page_limit:
            break
        if len(comments) < limit:
            page.wait_for_timeout(random.randint(300, 800))

    for index, (comment, source_item) in enumerate(
        zip(comments[:hot_comment_limit], source_items[:hot_comment_limit]),
        start=1,
    ):
        child_count = comment["child_comment_count"]
        if child_count <= 0 or not comment["id"]:
            continue

        embedded = [
            parsed
            for child in (source_item.get("child_comments") or [])
            if (parsed := parse_api_comment(child))
        ][:reply_limit]
        expected_count = min(child_count, reply_limit)
        if len(embedded) >= expected_count:
            comment["replies"] = embedded
            continue

        try:
            page.wait_for_timeout(random.randint(300, 800))
            comment["replies"] = fetch_child_comments_via_api(
                page,
                comment["id"],
                limit=reply_limit,
            )
        except Exception as exc:
            comment["replies"] = embedded
            print(
                f"   ⚠️ 第 {index} 条热门评论的回复提取失败，"
                f"保留 {len(embedded)} 条内嵌回复: {str(exc)[:160]}"
            )

    reply_count = sum(len(comment["replies"]) for comment in comments)
    print(
        f"   ✅ 评论 API 成功，分页原始数量 {raw_count}，"
        f"有效顶层评论 {len(comments)}，热门评论回复 {reply_count}"
    )
    return comments

def format_comments_markdown(comments):
    if not comments:
        return ""

    lines = ["", "", "---", f"### 💬 精选评论（最多 {DEFAULT_ROOT_COMMENT_LIMIT} 条）", ""]
    for comment in comments:
        content = comment["content"].replace("\n", "\n> ")
        lines.append(f"> **{comment['author']}**：{content}")
        for reply in comment.get("replies") or []:
            reply_content = reply["content"].replace("\n", "\n>> ")
            reply_target = f" 回复 **{reply['reply_to']}**" if reply.get("reply_to") else ""
            lines.append(f">> ↳ **{reply['author']}**{reply_target}：{reply_content}")
            lines.append(">>")
        lines.append(">")
    return "\n".join(lines)

def extract_debug_card_text(item):
    data = item.evaluate(
        """
        (node) => {
            const cloned = node.cloneNode(true);
            const removeSelectors = [
                '.ContentItem-actions',
                'footer',
                '.Comments-container',
                '.CommentListV2',
                '[class*="CommentList"]',
                'textarea',
                'input',
                '.CommentEditorV2',
                '.Comments-footer',
            ];
            for (const selector of removeSelectors) {
                cloned.querySelectorAll(selector).forEach((el) => el.remove());
            }

            const pick = (selectors) => {
                for (const selector of selectors) {
                    const element = cloned.querySelector(selector);
                    if (!element) continue;
                    const text = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (text) return text;
                }
                return '';
            };

            return {
                title: pick([
                    'h2 a',
                    'h2',
                    '.ContentItem-title a',
                    '.ContentItem-title',
                    'a[href*="/question/"][href*="/answer/"]',
                ]),
                author: pick([
                    '.AuthorInfo-name',
                    '.AuthorInfo .UserLink-link',
                    '.ContentItem-meta .UserLink-link',
                    '.UserLink-link',
                    'meta[itemprop="name"]',
                    'a[href*="/people/"]',
                ]),
                content: pick([
                    '.RichText.ztext',
                    '.RichContent-inner',
                    '[itemprop="text"]',
                    '.RichText',
                ]),
            };
        }
        """
    )
    return {
        "title": normalize_text(data.get("title", "")) or "未提取到标题",
        "author": normalize_text(data.get("author", "")) or "未提取到作者",
        "content": normalize_text(data.get("content", "")) or "未提取到正文",
    }

def print_debug_full_report(card_text, answer_id, comments):
    print("\n========== Debug 完整抓取结果 ==========")
    print(f"\n标题：{card_text['title']}")
    print(f"作者：{card_text['author']}")
    print(f"answer_id：{answer_id}")
    print("\n## 正文\n")
    print(card_text["content"])
    print("\n## 评论\n")
    if not comments:
        print("未提取到有效评论")
        return

    for index, comment in enumerate(comments, start=1):
        content = normalize_text(comment["content"])
        print(f"### 评论 {index}")
        print(f"作者：{comment['author']}")
        print(content)
        for reply_index, reply in enumerate(comment.get("replies") or [], start=1):
            reply_target = f" 回复 {reply['reply_to']}" if reply.get("reply_to") else ""
            print(
                f"  - 回复 {reply_index}：{reply['author']}{reply_target}："
                f"{normalize_text(reply['content'])}"
            )
        print("")

def print_debug_comment_report(comments):
    if not comments:
        print("   ⚠️ 评论 API 调用成功，但没有返回有效评论。")
        return

    print(f"\n🧪 Debug 评论结果：共 {len(comments)} 条\n")
    for index, comment in enumerate(comments, start=1):
        content = normalize_text(comment["content"])
        print(f"{index}. {comment['author']}：{content[:240]}")

def run_debug_comments(url: str | None = None, headed: bool = False):
    print("\n🧪 [Debug] 只测试第一条动态评论，不写数据库、不保存文件、不推 GitHub。")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        if not os.path.exists(STATE_FILE):
            print(f"❌ 找不到登录凭证: {STATE_FILE}")
            return 1

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state=STATE_FILE,
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        try:
            target_url = normalize_target_url(url)
            print(f"👉 [Debug] 访问知乎页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(4000)

            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "debug-blocked")
                print(f"❌ {blocked_reason}")
                return 1

            items = page.locator(".List-item")
            item_count = items.count()
            print(f"👉 [Debug] 当前页面动态卡片数: {item_count}")
            if item_count == 0:
                safe_page_snapshot(page, "debug-no-list-items")
                print("❌ 没有找到 .List-item，可能登录态失效或页面结构变化。")
                return 1

            item = items.first
            try:
                meta_text = item.locator(".ActivityItem-meta").inner_text(timeout=1000).strip()
                print(f"👉 [Debug] 第一条动态 meta: {normalize_text(meta_text)}")
            except Exception:
                print("⚠️ 未能读取第一条动态 meta，继续尝试提取 answer_id。")

            try:
                expand_btn = item.locator('button:has-text("阅读全文"), button:has-text("展开全文")')
                if expand_btn.count() > 0:
                    expand_btn.first.evaluate("node => node.click()")
                    page.wait_for_timeout(1500)
                    print("👉 [Debug] 已尝试展开第一条动态全文。")
            except Exception as e:
                print(f"⚠️ 展开全文失败，继续读取当前可见正文: {str(e)[:120]}")

            card_text = extract_debug_card_text(item)
            answer_id = extract_answer_id_from_item(item)
            if not answer_id:
                print("❌ 第一条动态不是回答，或没有提取到 answer_id。")
                return 1

            print(f"📡 [Debug] 请求评论 API，answer_id={answer_id}")
            comments = fetch_first_page_comments_via_api(page, answer_id)
            print_debug_full_report(card_text, answer_id, comments)
            return 0
        except Exception as e:
            print(f"❌ [Debug] 评论测试失败: {str(e)[:500]}")
            return 1
        finally:
            context.close()
            browser.close()

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


def run_zhihu_scraper(
    limit=20,
    progress_callback=None,
    url: str | None = None,
    headed: bool = True,
    include_comments: bool = True,
    continue_to_boundary: bool = False,
    max_new: int = 200,
):
    if md is None:
        raise RuntimeError("缺少依赖 markdownify。请先运行: pip install -r requirements.txt")

    init_db()
    newly_scraped_titles = []
    collected_count = 0
    soft_limit_reported = False

    display_mode = "可见" if headed else "无头"
    print(f"\n🚀 [Scraper] 正在启动{display_mode}浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        if not os.path.exists(STATE_FILE):
            print(f"❌ 找不到登录凭证: {STATE_FILE}")
            return ["[报错] 缺失 state.json 登录凭证"]
            
        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            storage_state=STATE_FILE,
            timezone_id="Asia/Shanghai" 
        )
        page = context.new_page()

        target_url = normalize_target_url(url)
        archive_run = archive_store.begin_run(DB_FILE, target_url, "playwright")
        archive_run_id = archive_run["run_id"]
        boundary_extension_active = continue_to_boundary and bool(archive_run["boundary_keys"])
        effective_limit = resolve_incremental_new_limit(
            limit,
            continue_to_boundary,
            max_new,
            has_boundary=bool(archive_run["boundary_keys"]),
        )
        if continue_to_boundary and not archive_run["boundary_keys"]:
            print(
                f"ℹ️ [Scraper] 当前没有旧边界，本轮仍按 --limit {limit} 采集并建立初始边界。"
            )
        print(f"👉 [Scraper] 访问知乎页面: {target_url}")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as first_error:
            print(f"⚠️ [Scraper] 首次加载失败，准备重试: {str(first_error)[:160]}")
            time.sleep(3)
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as second_error:
                safe_page_snapshot(page, "page-load-failed")
                archive_store.abort_run(DB_FILE, archive_run_id, "页面连续两次加载失败")
                browser.close()
                return [f"[报错] 页面加载失败: {str(second_error)[:160]}"]

        time.sleep(4)
        blocked_reason = detect_blocked_zhihu_page(page)
        if blocked_reason:
            safe_page_snapshot(page, "blocked-or-login")
            archive_store.abort_run(DB_FILE, archive_run_id, blocked_reason)
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
                safe_page_snapshot(page, "blocked-during-scroll")
                archive_store.abort_run(DB_FILE, archive_run_id, blocked_reason)
                browser.close()
                return newly_scraped_titles + [f"[报错] {blocked_reason}"]

            items = page.locator('.List-item')
            current_count = items.count()
            if current_count == 0:
                safe_page_snapshot(page, "no-list-items")
                title, current_url = get_page_diagnostics(page)
                reason = f"未找到动态卡片，页面可能改版或登录态失效: {title} {current_url}"
                archive_store.abort_run(DB_FILE, archive_run_id, reason)
                browser.close()
                return newly_scraped_titles + [f"[报错] {reason}"]
            found_new_in_this_loop = False

            for i in range(current_count):
                if collected_count >= effective_limit:
                    break
                item = items.nth(i)

                try:
                    meta_el = item.locator('.ActivityItem-meta')
                    if meta_el.count() == 0: continue
                    meta_text = meta_el.inner_text(timeout=500).strip()
                    action_text_el = item.locator('.ActivityItem-metaTitle')
                    action_text = action_text_el.inner_text().strip() if action_text_el.count() > 0 else meta_text
                except: continue

                if not should_archive_action(action_text):
                    continue

                # 提取动态发生时间、标题和文章元数据
                activity_dt, time_str = parse_activity_time(meta_text)

                is_pin = "想法" in action_text
                if is_pin:
                    try:
                        author_el = item.locator('.AuthorInfo-name').first
                        author_name = author_el.inner_text().strip().split('\n')[0].strip() if author_el.count() > 0 else "未知作者"
                    except: author_name = "未知作者"
                    title = f"{author_name}_想法"
                else:
                    try:
                        title_el = item.locator('.ContentItem-title')
                        title = title_el.inner_text().strip() if title_el.count() > 0 else "无标题内容"
                    except: title = "无标题内容"

                clean_title_str = clean_file_name(f"{time_str} {title}")
                save_dir = get_save_dir_from_time_str(time_str)
                content_metadata = extract_content_metadata(item)
                content_metadata["activity_at"] = (
                    activity_dt.strftime("%Y-%m-%d %H:%M") if activity_dt else ""
                )
                content_metadata["activity_action"] = normalize_text(action_text)
                content_identity = extract_content_identity_from_item(item)
                if content_identity:
                    content_identity["activity_time"] = time_str
                    if content_metadata.get("source_url"):
                        content_identity["url"] = content_metadata["source_url"]
                    elif content_identity.get("url"):
                        content_metadata["source_url"] = content_identity["url"]
                    if content_identity.get("content_type") == "answer":
                        content_metadata["source_type"] = "answer"
                        content_metadata["answer_id"] = content_identity["content_id"]
                    observed = archive_store.record_seen(
                        DB_FILE,
                        archive_run_id,
                        content_identity["content_key"],
                    )
                    if observed["boundary"]:
                        result = archive_store.finish_run(
                            DB_FILE,
                            archive_run_id,
                            boundary_hit=True,
                        )
                        print(f"🛑 [Scraper] 命中上轮边界，增量抓取完成: {result}")
                        browser.close()
                        return newly_scraped_titles

                if is_article_exists(
                    clean_title_str,
                    content_identity.get("content_key") if content_identity else None,
                ):
                    consecutive_exists_count += 1
                    if consecutive_exists_count > 10 and not archive_run["boundary_keys"]:
                        print("🛑 [Scraper] 连续遇到老文章，增量抓取结束。")
                        result = archive_store.finish_run(DB_FILE, archive_run_id)
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

                item.scroll_into_view_if_needed()
                time.sleep(random.uniform(0.5, 1.2))

                # === 提取正文 Markdown ===
                expand_btn = item.locator('button:has-text("阅读全文"), button:has-text("展开全文")')
                if expand_btn.count() > 0:
                    try:
                        expand_btn.first.evaluate("node => node.click()")
                        time.sleep(random.uniform(1.5, 2.5)) 
                    except: pass

                try:
                    content_box = item.locator('.RichContent-inner, .RichText').first
                    raw_md = "\n".join([line for line in md(content_box.inner_html(), heading_style="ATX").split("\n") if line.strip()])
                except Exception as e:
                    raw_md = f"【⚠️ 正文提取失败】{str(e)[:40]}"

                # ==========================================
                # 🌟 核心重构：融合成功脚本的提取逻辑
                # ==========================================
                comments_md_text = ""
                try:
                    # 与 testzhihu 的成功脚本保持同一条链路：先从链接提取 answer_id，再请求评论 API。
                    target_id = (
                        content_identity["content_id"]
                        if content_identity and content_identity["content_type"] == "answer"
                        else None
                    )
                    if target_id and include_comments:
                        print(f"   📡 识别为“回答”，提取到 ID [{target_id}]，发起 API 请求...")
                        comments = fetch_first_page_comments_via_api(page, target_id)
                        comments_md_text = format_comments_markdown(comments)
                        if not comments_md_text:
                            print("   ⚠️ 接口调用成功，但没有可保存的有效评论。")
                    elif target_id:
                        print("   ⏭️ 已通过 --no-comments 关闭评论提取。")
                    else:
                        print("   ⏭️ 当前动态非“回答”，跳过评论提取。")
                        
                except Exception as e:
                    print(f"   ⚠️ 评论提取发生异常: {str(e)[:300]}")
                # ==========================================

                # 拼接并下载图片
                final_md = download_img_and_replace_md_link(raw_md, clean_title_str, save_dir)
                final_md += comments_md_text

                md_file_path = os.path.join(save_dir, f"{clean_title_str}.md")
                frontmatter = build_frontmatter(title, content_metadata)
                with open(md_file_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(f"{frontmatter}\n\n# {title}\n\n---\n\n{final_md}")

                save_article_to_db(clean_title_str, content_identity, md_file_path)
                archive_store.increment_run_new_count(DB_FILE, archive_run_id)
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
                    safe_page_snapshot(page, "scroll-stalled")
                    result = archive_store.abort_run(DB_FILE, archive_run_id, reason)
                    print(f"🛑 [Scraper] {reason}: {result}")
                    browser.close()
                    return newly_scraped_titles + [f"[报错] {reason}"]

                print("⏬ [Scraper] 向下滚动加载...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(2.5, 4.0))
            else:
                scroll_attempts = 0
                stale_scrolls = 0

        result = archive_store.finish_run(DB_FILE, archive_run_id)
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


def parse_activity_time(meta_text: str):
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s(\d{2}:\d{2})", meta_text or "")
    if not match:
        return None, f"[{int(time.time())}]"
    dt_text = f"{match.group(1)} {match.group(2)}"
    try:
        from datetime import datetime
        dt = datetime.strptime(dt_text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, f"[{int(time.time())}]"
    time_str = f"[{match.group(1)}_{match.group(2).replace(':', '-')}]"
    return dt, time_str


def extract_title_from_activity_item(item, action_text: str) -> str:
    is_pin = "想法" in (action_text or "")
    if is_pin:
        try:
            author_el = item.locator('.AuthorInfo-name').first
            return author_el.inner_text().strip().split('\n')[0].strip() if author_el.count() > 0 else "未知作者_想法"
        except Exception:
            return "未知作者_想法"

    selectors = [
        '.ContentItem-title',
        'h2 a',
        'h2',
        'a[href*="/question/"][href*="/answer/"]',
        'a[href*="/p/"]',
    ]
    for selector in selectors:
        try:
            loc = item.locator(selector)
            if loc.count() > 0:
                title = normalize_text(loc.first.inner_text(timeout=1000))
                if title and title not in {"赞同了回答", "收藏了回答", "回答", "阅读全文", "展开全文"}:
                    return title
        except Exception:
            continue
    return "无标题内容"


def export_activity_item_from_profile(
    page,
    item,
    title: str,
    clean_title_str: str,
    save_dir: str,
    activity_dt,
    action_text: str,
    include_comments: bool = True,
) -> str:
    item.scroll_into_view_if_needed()
    time.sleep(random.uniform(0.7, 1.5))

    expand_btn = item.locator('button:has-text("阅读全文"), button:has-text("展开全文"), button:has-text("阅读原文")')
    if expand_btn.count() > 0:
        try:
            expand_btn.first.evaluate("node => node.click()")
            time.sleep(random.uniform(1.8, 3.0))
        except Exception:
            pass

    try:
        content_box = item.locator('.RichContent-inner, .RichText').first
        raw_md = "\n".join([
            line for line in md(content_box.inner_html(), heading_style="ATX").split("\n")
            if line.strip()
        ])
    except Exception as e:
        raw_md = f"【⚠️ 正文提取失败】{str(e)[:80]}"

    content_metadata = extract_content_metadata(item)
    content_metadata["activity_at"] = activity_dt.strftime("%Y-%m-%d %H:%M")
    content_metadata["activity_action"] = normalize_text(action_text)

    comments_md_text = ""
    try:
        target_id = extract_answer_id_from_item(item)
        if target_id:
            content_metadata["source_type"] = "answer"
            content_metadata["answer_id"] = content_metadata.get("answer_id") or target_id
        if target_id and include_comments:
            print(f"   📡 识别为回答，提取评论 answer_id={target_id}")
            comments = fetch_first_page_comments_via_api(page, target_id)
            comments_md_text = format_comments_markdown(comments)
    except Exception as e:
        print(f"   ⚠️ 评论提取异常，跳过评论: {str(e)[:200]}")

    final_md = download_img_and_replace_md_link(raw_md, clean_title_str, save_dir)
    final_md += comments_md_text

    md_file_path = os.path.join(save_dir, f"{clean_title_str}.md")
    frontmatter = build_frontmatter(title, content_metadata)
    with open(md_file_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{frontmatter}\n\n# {title}\n\n---\n\n{final_md}\n")

    print(f"   ✅ 已保存 Markdown: {md_file_path}")
    return md_file_path


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
    """
    本地历史回溯：只在个人主页动态流里展开正文并保存 Markdown。
    不打开详情页，不推 GitHub，不依赖 Telegram。
    """
    if md is None:
        raise RuntimeError("缺少依赖 markdownify。请先运行: pip install -r requirements.txt")
    if seek_delay_min > seek_delay_max or collect_delay_min > collect_delay_max:
        raise ValueError("延迟参数的最小值不能大于最大值")

    from datetime import datetime, time as dt_time
    start_dt = datetime.combine(datetime.fromisoformat(start_date).date(), dt_time.min)
    end_dt = datetime.combine(datetime.fromisoformat(end_date).date(), dt_time.max).replace(microsecond=0)
    if start_dt > end_dt:
        raise ValueError("start-date 不能晚于 end-date")
    target_url = normalize_target_url(url)
    target_slug = slug_from_url(target_url)
    target_output_dir = output_dir or LOCAL_ARCHIVE_ROOT_DIR

    init_db()
    saved = []
    seen_keys = set()
    consecutive_no_new_visible = 0
    phase = "seek"
    collect_next_index = 0

    print("\n🚀 [Backfill] 本地历史回溯启动")
    print(f"   目标 URL: {target_url}")
    print(f"   目标标识: {target_slug}")
    print(f"   时间范围: {start_dt} ~ {end_dt}")
    print(f"   输出目录: {os.path.abspath(target_output_dir if flat_output else ARCHIVE_ROOT_DIR)}")
    print(f"   采样上限: {limit if limit else '不限'}")
    print(f"   动作类型: {', '.join(SUPPORTED_ACTION_KEYWORDS)}")
    print(f"   评论采集: {'开启' if include_comments else '关闭'}")
    print(f"   seek 滚动等待: {seek_delay_min}-{seek_delay_max}s")
    print(f"   collect 滚动等待: {collect_delay_min}-{collect_delay_max}s")
    print(f"   seek 每轮只检查尾部卡片数: {seek_tail_count}")
    print(f"   seek 连续滚动次数: {seek_scroll_burst}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        if not os.path.exists(STATE_FILE):
            print(f"❌ 找不到登录凭证: {STATE_FILE}")
            return saved

        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            storage_state=STATE_FILE,
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        try:
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as first_error:
                print(f"⚠️ [Backfill] 首次加载失败，准备重试: {str(first_error)[:160]}")
                time.sleep(3)
                page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            time.sleep(4)

            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "backfill-blocked-or-login")
                print(f"❌ [Backfill] {blocked_reason}")
                return saved

            for scroll_idx in range(max_scrolls):
                blocked_reason = detect_blocked_zhihu_page(page)
                if blocked_reason:
                    safe_page_snapshot(page, "backfill-blocked-during-scroll")
                    print(f"❌ [Backfill] {blocked_reason}")
                    return saved

                items = page.locator('.List-item')
                current_count = items.count()
                if current_count == 0:
                    safe_page_snapshot(page, "backfill-no-list-items")
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

                for i in range(scan_start, scan_end):
                    if limit and len(saved) >= limit:
                        print(f"🛑 [Backfill] 已达到采样上限 {limit}")
                        return saved

                    item = items.nth(i)
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
                        item,
                        activity_dt,
                        time_str,
                        title,
                        clean_title_str,
                        content_identity,
                        action_text,
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
                            clean_title_str,
                            content_identity.get("content_key") if content_identity else None,
                        ):
                            continue

                        print(f"\n[Backfill] 导出动态：{clean_title_str}")
                        save_dir = (
                            get_flat_save_dir(target_output_dir)
                            if flat_output
                            else get_save_dir_from_time_str(time_str, target_output_dir)
                        )
                        if content_identity:
                            clean_title_str = archive_store.resolve_collision_title(save_dir, time_str, title)
                        md_file_path = export_activity_item_from_profile(
                            page,
                            item,
                            title,
                            clean_title_str,
                            save_dir,
                            activity_dt,
                            action_text,
                            include_comments=include_comments,
                        )
                        save_article_to_db(clean_title_str, content_identity, md_file_path)
                        saved.append(clean_title_str)
                    collect_next_index = scan_end

                if new_visible:
                    consecutive_no_new_visible = 0
                else:
                    consecutive_no_new_visible += 1

                oldest_text = oldest_dt.strftime("%Y-%m-%d %H:%M") if oldest_dt else "N/A"
                newest_text = newest_dt.strftime("%Y-%m-%d %H:%M") if newest_dt else "N/A"
                print(
                    f"⏬ [Backfill {phase} {scroll_idx + 1}/{max_scrolls}] "
                    f"卡片={current_count} 扫描={scan_end - scan_start} 新可见={new_visible} 连续无新={consecutive_no_new_visible} "
                    f"最新={newest_text} 最旧={oldest_text} 已保存={len(saved)}"
                )

                if consecutive_no_new_visible >= 20:
                    print("🛑 [Backfill] 连续多次无新可见动态，停止。")
                    return saved

                if phase == "seek":
                    for burst_idx in range(max(1, seek_scroll_burst)):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        if burst_idx < seek_scroll_burst - 1:
                            time.sleep(0.2)
                    time.sleep(random.uniform(seek_delay_min, seek_delay_max))
                else:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(random.uniform(collect_delay_min, collect_delay_max))

        except Exception as exc:
            safe_page_snapshot(page, "backfill-failed")
            print(f"❌ [Backfill] 运行失败: {str(exc)[:300]}")
            return saved
        finally:
            context.close()
            browser.close()

    return saved

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知乎动态归档爬虫")
    parser.add_argument(
        "--debug-comments",
        action="store_true",
        help="只测试第一条动态评论，不写数据库、不保存文件、不推 GitHub",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="正常抓取模式下最多处理的新动态数量，默认 5",
    )
    parser.add_argument(
        "--continue-to-boundary",
        action="store_true",
        help="达到 --limit 后若尚未命中旧边界，继续采集到边界或 --max-new",
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=200,
        help="继续到旧边界模式的新内容安全上限，默认 200",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="知乎可滚动列表页 URL，例如个人主页、回答页、文章页、收藏夹页等",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Markdown 输出目录，默认 data/articles",
    )
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
    parser.add_argument(
        "--end-date",
        default=date.today().isoformat(),
        help="动态发生日期终点，默认今天",
    )
    parser.add_argument("--max-scrolls", type=int, default=10000, help="最大滚动次数，默认 10000")
    parser.add_argument("--seek-delay-min", type=float, default=0.6, help="seek 阶段每次滚动最短等待秒数")
    parser.add_argument("--seek-delay-max", type=float, default=1.2, help="seek 阶段每次滚动最长等待秒数")
    parser.add_argument("--collect-delay-min", type=float, default=1.2, help="collect 阶段每次滚动最短等待秒数")
    parser.add_argument("--collect-delay-max", type=float, default=2.0, help="collect 阶段每次滚动最长等待秒数")
    parser.add_argument("--seek-tail-count", type=int, default=30, help="seek 阶段每轮只检查最后 N 个卡片")
    parser.add_argument("--seek-scroll-burst", type=int, default=3, help="seek 阶段每轮连续滚动次数")
    args = parser.parse_args()
    configure_runtime_paths(args.output_dir, args.db_file, args.state_file)

    if not args.backfill_local and not args.debug_comments:
        try:
            resolve_incremental_new_limit(
                args.limit,
                args.continue_to_boundary,
                args.max_new,
            )
        except ValueError as exc:
            parser.error(str(exc))

    if args.debug_comments:
        raise SystemExit(run_debug_comments(url=args.url, headed=not args.headless))
    if args.backfill_local:
        print(run_local_backfill(
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
        ))

    else:
        print(run_zhihu_scraper(
            limit=args.limit,
            url=args.url,
            headed=not args.headless,
            include_comments=not args.no_comments,
            continue_to_boundary=args.continue_to_boundary,
            max_new=args.max_new,
        ))
