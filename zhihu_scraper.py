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
from urllib.parse import quote, urlparse
from playwright.sync_api import sync_playwright

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
AUTHOR_NAME = "Juan"
DB_FILE = os.getenv("ZH_DB_FILE", "zhihu_articles.db")

ARCHIVE_ROOT_DIR = os.getenv("ARCHIVE_ROOT_DIR", "save_zhihu_activity")
LOCAL_ARCHIVE_ROOT_DIR = os.getenv("LOCAL_ARCHIVE_ROOT_DIR", os.path.join("data", "articles"))

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhihu.com/"
}

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
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS articles (title TEXT PRIMARY KEY, scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def is_article_exists(title):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM articles WHERE title = ?", (title,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def save_article_to_db(title):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO articles (title) VALUES (?)", (title,))
    conn.commit()
    conn.close()

# --- 文本与图片处理 ---
MAX_FILE_NAME_LENGTH = 100

def clean_file_name(title):
    illegal_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '，', '。', '\n', '\r']
    for char in illegal_chars: title = title.replace(char, "")
    return title.strip()[:MAX_FILE_NAME_LENGTH]

def get_save_dir_from_time_str(time_str: str) -> str:
    match = re.match(r"\[(\d{4})-(\d{2})-\d{2}_\d{2}-\d{2}\]", time_str)
    if match:
        year, month = match.groups()
    else:
        year, month = time.strftime("%Y"), time.strftime("%m")

    save_dir = os.path.join(ARCHIVE_ROOT_DIR, year, month)
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

def extract_answer_id_from_item(item):
    hrefs = item.evaluate(
        """
        (node) => Array.from(node.querySelectorAll('a[href]'))
            .map((el) => el.href || el.getAttribute('href') || '')
            .filter(Boolean)
        """
    )

    for href in hrefs:
        match = re.search(r"/question/\d+/answer/(\d+)", href)
        if match:
            answer_id = match.group(1)
            print(f"   ✅ 成功从链接提取 answer_id: {answer_id}")
            return answer_id

    zop_answer_id = item.evaluate(
        """
        (node) => {
            const contentItem = node.querySelector('.ContentItem');
            if (!contentItem) return null;
            const zopStr = contentItem.getAttribute('data-zop');
            if (!zopStr) return null;
            try {
                const zop = JSON.parse(zopStr);
                const type = String(zop.type || '').toLowerCase();
                if (type === 'answer') {
                    return String(zop.itemId || zop.item_id || zop.id || '');
                }
            } catch (e) {}
            return null;
        }
        """
    )
    if zop_answer_id:
        print(f"   ✅ 成功从 data-zop 提取 answer_id: {zop_answer_id}")
        return zop_answer_id

    print(f"   ⏭️ 当前动态未找到回答链接，扫描到链接数: {len(hrefs)}")
    return None

def fetch_first_page_comments_via_api(page, answer_id, limit=15):
    api_url = (
        f"https://www.zhihu.com/api/v4/answers/{answer_id}/root_comments"
        f"?limit={limit}&offset=0&order=normal&status=open"
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
        raise RuntimeError(f"评论 API 请求失败: HTTP {response.status}, body={response_text}")

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"评论 API 返回结构异常: keys={list(payload.keys())}")

    comments = []
    for item in data:
        author_info = item.get("author") or {}
        member_info = author_info.get("member") or {}
        author_name = (
            member_info.get("name")
            or author_info.get("name")
            or item.get("author_name")
            or "匿名用户"
        )

        raw_content = item.get("content") or item.get("comment") or item.get("text") or ""
        clean_content = clean_html_text(str(raw_content))
        if clean_content and "已删除" not in clean_content:
            comments.append(
                {
                    "author": normalize_text(str(author_name)),
                    "content": clean_content,
                }
            )

    print(f"   ✅ 评论 API 成功，原始数量 {len(data)}，有效数量 {len(comments)}")
    return comments

def format_comments_markdown(comments):
    if not comments:
        return ""

    lines = ["", "", "---", "### 💬 精选评论 (第一页)", ""]
    for comment in comments:
        content = comment["content"].replace("\n", "\n> ")
        lines.append(f"> **{comment['author']}**：{content}")
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
        print("")

def print_debug_comment_report(comments):
    if not comments:
        print("   ⚠️ 评论 API 调用成功，但没有返回有效评论。")
        return

    print(f"\n🧪 Debug 评论结果：共 {len(comments)} 条\n")
    for index, comment in enumerate(comments, start=1):
        content = normalize_text(comment["content"])
        print(f"{index}. {comment['author']}：{content[:240]}")

def run_debug_comments(url: str | None = None):
    print("\n🧪 [Debug] 只测试第一条动态评论，不写数据库、不保存文件、不推 GitHub。")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        if not os.path.exists("state.json"):
            print("❌ 找不到 state.json 凭证！")
            return 1

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state="state.json",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        try:
            target_url = normalize_target_url(url)
            print(f"👉 [Debug] 访问知乎页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)

            items = page.locator(".List-item")
            item_count = items.count()
            print(f"👉 [Debug] 当前页面动态卡片数: {item_count}")
            if item_count == 0:
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
            comments = fetch_first_page_comments_via_api(page, answer_id, limit=15)
            print_debug_full_report(card_text, answer_id, comments)
            return 0
        except Exception as e:
            print(f"❌ [Debug] 评论测试失败: {str(e)[:500]}")
            return 1
        finally:
            context.close()
            browser.close()

def download_img_and_replace_md_link(md_content, article_title, save_dir):
    img_sub_dir = clean_file_name(article_title)
    img_save_path = os.path.join(save_dir, img_sub_dir)
    img_pattern = re.compile(r"!\[(.*?)\]\((https?://.*?)\)")
    all_img = img_pattern.findall(md_content)
    
    if not all_img: return md_content
    if not os.path.exists(img_save_path): os.makedirs(img_save_path)

    for img_desc, img_url in all_img:
        try:
            img_suffix = img_url.split(".")[-1].lower()
            if img_suffix not in ["jpg", "png", "gif", "webp", "jpeg"]: img_suffix = "jpg"
            img_name = f"{clean_file_name(img_desc)[:10]}_{int(time.time()*1000)}.{img_suffix}"
            img_file_path = os.path.join(img_save_path, img_name)

            if not os.path.exists(img_file_path):
                time.sleep(random.uniform(0.1, 0.4)) 
                img_response = requests.get(img_url, headers=headers, timeout=10)
                img_response.raise_for_status()
                with open(img_file_path, "wb") as f: f.write(img_response.content)

            safe_rel_path = quote(f"{img_sub_dir}/{img_name}")
            md_content = md_content.replace(img_url, safe_rel_path)
        except: continue
    return md_content


def run_zhihu_scraper(limit=20, progress_callback=None, url: str | None = None): 
    if md is None:
        raise RuntimeError("缺少依赖 markdownify。请先运行: pip install -r requirements.txt")

    init_db()
    newly_scraped_titles = []
    collected_count = 0

    print("\n🚀 [Scraper] 正在启动无头浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True) 
        if not os.path.exists("state.json"):
            print("❌ 找不到 state.json 凭证！")
            return ["[报错] 缺失 state.json 登录凭证"]
            
        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            storage_state="state.json",
            timezone_id="Asia/Shanghai" 
        )
        page = context.new_page()

        target_url = normalize_target_url(url)
        print(f"👉 [Scraper] 访问知乎页面: {target_url}")
        try:
            page.goto(target_url, wait_until="domcontentloaded")
        except:
            time.sleep(3) 
            page.goto(target_url, wait_until="domcontentloaded")

        time.sleep(4)
        consecutive_exists_count = 0 

        while collected_count < limit:
            items = page.locator('.List-item')
            current_count = items.count()
            found_new_in_this_loop = False

            for i in range(current_count):
                if collected_count >= limit: break
                item = items.nth(i)

                try:
                    meta_el = item.locator('.ActivityItem-meta')
                    if meta_el.count() == 0: continue
                    meta_text = meta_el.inner_text(timeout=500).strip()
                    action_text_el = item.locator('.ActivityItem-metaTitle')
                    action_text = action_text_el.inner_text().strip() if action_text_el.count() > 0 else meta_text
                except: continue

                if not any(kw in action_text for kw in ["赞同", "发布", "发表"]): continue

                # 提取时间和标题
                try:
                    time_match = re.search(r"(\d{4}-\d{2}-\d{2})\s(\d{2}:\d{2})", meta_text)
                    time_str = f"[{time_match.group(1)}_{time_match.group(2).replace(':', '-')}]" if time_match else f"[{int(time.time())}]"
                except: time_str = f"[{int(time.time())}]"

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

                if is_article_exists(clean_title_str):
                    consecutive_exists_count += 1
                    if consecutive_exists_count > 10:
                        print("🛑 [Scraper] 连续遇到老文章，增量抓取结束。")
                        browser.close()
                        return newly_scraped_titles
                    continue
                
                consecutive_exists_count = 0 
                found_new_in_this_loop = True

                print(f"\n[Scraper] 处理新动态：{clean_title_str}")
                if progress_callback: progress_callback(collected_count + 1, limit, clean_title_str)

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
                    target_id = extract_answer_id_from_item(item)
                    if target_id:
                        print(f"   📡 识别为“回答”，提取到 ID [{target_id}]，发起 API 请求...")
                        comments = fetch_first_page_comments_via_api(page, target_id, limit=15)
                        comments_md_text = format_comments_markdown(comments)
                        if not comments_md_text:
                            print("   ⚠️ 接口调用成功，但没有可保存的有效评论。")
                    else:
                        print("   ⏭️ 当前动态非“回答”，跳过评论提取。")
                        
                except Exception as e:
                    print(f"   ⚠️ 评论提取发生异常: {str(e)[:300]}")
                # ==========================================

                # 拼接并下载图片
                final_md = download_img_and_replace_md_link(raw_md, clean_title_str, save_dir)
                final_md += comments_md_text

                md_file_path = os.path.join(save_dir, f"{clean_title_str}.md")
                with open(md_file_path, "w", encoding="utf-8") as f:
                    f.write(f"# {title}\n\n---\n\n{final_md}")  

                save_article_to_db(clean_title_str)
                newly_scraped_titles.append(clean_title_str)
                collected_count += 1

            if not found_new_in_this_loop:
                print("⏬ [Scraper] 向下滚动加载...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(2.5, 4.0))

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
                if title and title not in {"赞同了回答", "回答", "阅读全文", "展开全文"}:
                    return title
        except Exception:
            continue
    return "无标题内容"


def export_activity_item_from_profile(page, item, title: str, clean_title_str: str, save_dir: str) -> str:
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

    comments_md_text = ""
    try:
        target_id = extract_answer_id_from_item(item)
        if target_id:
            print(f"   📡 识别为回答，提取评论 answer_id={target_id}")
            comments = fetch_first_page_comments_via_api(page, target_id, limit=15)
            comments_md_text = format_comments_markdown(comments)
    except Exception as e:
        print(f"   ⚠️ 评论提取异常，跳过评论: {str(e)[:200]}")

    final_md = download_img_and_replace_md_link(raw_md, clean_title_str, save_dir)
    final_md += comments_md_text

    md_file_path = os.path.join(save_dir, f"{clean_title_str}.md")
    with open(md_file_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n---\n\n{final_md}")

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
):
    """
    本地历史回溯：只在个人主页动态流里展开正文并保存 Markdown。
    不打开详情页，不推 GitHub，不依赖 Telegram。
    """
    if md is None:
        raise RuntimeError("缺少依赖 markdownify。请先运行: pip install -r requirements.txt")

    from datetime import datetime, time as dt_time
    start_dt = datetime.combine(datetime.fromisoformat(start_date).date(), dt_time.min)
    end_dt = datetime.combine(datetime.fromisoformat(end_date).date(), dt_time.max).replace(microsecond=0)
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
    print(f"   seek 滚动等待: {seek_delay_min}-{seek_delay_max}s")
    print(f"   collect 滚动等待: {collect_delay_min}-{collect_delay_max}s")
    print(f"   seek 每轮只检查尾部卡片数: {seek_tail_count}")
    print(f"   seek 连续滚动次数: {seek_scroll_burst}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        if not os.path.exists("state.json"):
            print("❌ 找不到 state.json 凭证！")
            return saved

        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            storage_state="state.json",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        try:
            page.goto(target_url, wait_until="domcontentloaded")
            time.sleep(4)

            for scroll_idx in range(max_scrolls):
                items = page.locator('.List-item')
                current_count = items.count()
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

                    if not any(kw in action_text for kw in ["赞同", "发布", "发表"]):
                        continue

                    activity_dt, time_str = parse_activity_time(meta_text)
                    if not activity_dt:
                        continue
                    oldest_dt = activity_dt if oldest_dt is None else min(oldest_dt, activity_dt)
                    newest_dt = activity_dt if newest_dt is None else max(newest_dt, activity_dt)

                    title = extract_title_from_activity_item(item, action_text)
                    clean_title_str = clean_file_name(f"{time_str} {title}")
                    unique_key = clean_title_str
                    if unique_key not in seen_keys:
                        new_visible += 1
                        seen_keys.add(unique_key)

                    visible_records.append((item, activity_dt, time_str, title, clean_title_str))

                if phase == "seek" and oldest_dt and oldest_dt <= end_dt:
                    phase = "collect"
                    collect_next_index = scan_start
                    print(f"✅ [Backfill] 已找到 {end_date} 及以前的动态，进入 collect 阶段。当前最旧={oldest_dt}")

                if phase == "collect":
                    for item, activity_dt, time_str, title, clean_title_str in visible_records:
                        if limit and len(saved) >= limit:
                            print(f"🛑 [Backfill] 已达到采样上限 {limit}")
                            return saved

                        if activity_dt > end_dt:
                            continue
                        if activity_dt < start_dt:
                            print(f"🛑 [Backfill] 已滚动到起始日期以前: {activity_dt}")
                            return saved

                        if is_article_exists(clean_title_str):
                            continue

                        print(f"\n[Backfill] 导出动态：{clean_title_str}")
                        save_dir = get_flat_save_dir(target_output_dir) if flat_output else get_save_dir_from_time_str(time_str)
                        export_activity_item_from_profile(page, item, title, clean_title_str, save_dir)
                        save_article_to_db(clean_title_str)
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
        "--url",
        default=None,
        help="知乎可滚动列表页 URL，例如个人主页、回答页、文章页、收藏夹页等",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Markdown 输出目录，默认 data/articles",
    )
    parser.add_argument(
        "--backfill-local",
        action="store_true",
        help="本地历史回溯：只在主页动态流展开并保存 Markdown，不打开详情页",
    )
    parser.add_argument("--start-date", default="2020-01-01", help="动态发生日期起点，默认 2020-01-01")
    parser.add_argument("--end-date", default="2024-02-14", help="动态发生日期终点，默认 2024-02-14")
    parser.add_argument("--max-scrolls", type=int, default=10000, help="最大滚动次数，默认 10000")
    parser.add_argument("--seek-delay-min", type=float, default=0.6, help="seek 阶段每次滚动最短等待秒数")
    parser.add_argument("--seek-delay-max", type=float, default=1.2, help="seek 阶段每次滚动最长等待秒数")
    parser.add_argument("--collect-delay-min", type=float, default=1.2, help="collect 阶段每次滚动最短等待秒数")
    parser.add_argument("--collect-delay-max", type=float, default=2.0, help="collect 阶段每次滚动最长等待秒数")
    parser.add_argument("--seek-tail-count", type=int, default=30, help="seek 阶段每轮只检查最后 N 个卡片")
    parser.add_argument("--seek-scroll-burst", type=int, default=3, help="seek 阶段每轮连续滚动次数")
    args = parser.parse_args()

    if args.debug_comments:
        raise SystemExit(run_debug_comments(url=args.url))
    if args.backfill_local:
        print(run_local_backfill(
            start_date=args.start_date,
            end_date=args.end_date,
            limit=args.limit,
            max_scrolls=args.max_scrolls,
            flat_output=True,
            url=args.url,
            output_dir=args.output_dir,
            seek_delay_min=args.seek_delay_min,
            seek_delay_max=args.seek_delay_max,
            collect_delay_min=args.collect_delay_min,
            collect_delay_max=args.collect_delay_max,
            seek_tail_count=args.seek_tail_count,
            seek_scroll_burst=args.seek_scroll_burst,
        ))

    else:
        print(run_zhihu_scraper(limit=args.limit, url=args.url))
