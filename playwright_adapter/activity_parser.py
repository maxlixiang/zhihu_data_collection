import html
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from zhihu_archive import store as archive_store

from .config import DEFAULT_URL
from .console import safe_print as print


SUPPORTED_ACTION_KEYWORDS = ("赞同", "发布", "发表", "收藏", "喜欢")


def should_archive_action(action_text):
    return any(keyword in (action_text or "") for keyword in SUPPORTED_ACTION_KEYWORDS)


def normalize_target_url(url: str | None = None, default_url: str = DEFAULT_URL) -> str:
    value = (url or default_url).strip().rstrip("/")
    if not value:
        value = default_url
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
        if len(parts) > idx + 1:
            return parts[idx + 1]
    if "collection" in parts and len(parts) > 1:
        return f"collection-{parts[-1]}"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", path).strip("-") or "zhihu"


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


def parse_activity_time(meta_text: str):
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s(\d{2}:\d{2})", meta_text or "")
    if not match:
        return None, f"[{int(time.time())}]"
    dt_text = f"{match.group(1)} {match.group(2)}"
    try:
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
            author_name = author_el.inner_text().strip().split('\n')[0].strip() if author_el.count() > 0 else "未知作者"
            return f"{author_name}_想法"
        except Exception:
            return "未知作者_想法"
    selectors = [
        '.ContentItem-title', 'h2 a', 'h2',
        'a[href*="/question/"][href*="/answer/"]', 'a[href*="/p/"]',
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
