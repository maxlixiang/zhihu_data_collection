import os

from playwright.sync_api import sync_playwright

from .activity_parser import extract_answer_id_from_item, normalize_target_url, normalize_text
from .comments import fetch_comments_via_api
from .config import RuntimeConfig
from .console import safe_print as print
from .page_detection import detect_blocked_zhihu_page, safe_page_snapshot


def extract_debug_card_text(item):
    data = item.evaluate(
        """
        (node) => {
            const cloned = node.cloneNode(true);
            const removeSelectors = [
                '.ContentItem-actions', 'footer', '.Comments-container',
                '.CommentListV2', '[class*="CommentList"]', 'textarea',
                'input', '.CommentEditorV2', '.Comments-footer',
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
                title: pick(['h2 a', 'h2', '.ContentItem-title a', '.ContentItem-title', 'a[href*="/question/"][href*="/answer/"]']),
                author: pick(['.AuthorInfo-name', '.AuthorInfo .UserLink-link', '.ContentItem-meta .UserLink-link', '.UserLink-link', 'meta[itemprop="name"]', 'a[href*="/people/"]']),
                content: pick(['.RichText.ztext', '.RichContent-inner', '[itemprop="text"]', '.RichText']),
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
        print(f"### 评论 {index}")
        print(f"作者：{comment['author']}")
        print(normalize_text(comment["content"]))
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


def run_debug_comments(config: RuntimeConfig, url: str | None = None, headed: bool = False):
    print("\n🧪 [Debug] 只测试第一条动态评论，不写数据库、不保存文件、不推 GitHub。")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        if not os.path.exists(config.state_file):
            print(f"❌ 找不到登录凭证: {config.state_file}")
            return 1
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state=config.state_file,
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        try:
            target_url = normalize_target_url(url, config.default_url)
            print(f"👉 [Debug] 访问知乎页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
            page.wait_for_timeout(4000)
            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "debug-blocked", config.project_dir)
                print(f"❌ {blocked_reason}")
                return 1
            items = page.locator(".List-item")
            item_count = items.count()
            print(f"👉 [Debug] 当前页面动态卡片数: {item_count}")
            if item_count == 0:
                safe_page_snapshot(page, "debug-no-list-items", config.project_dir)
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
            except Exception as exc:
                print(f"⚠️ 展开全文失败，继续读取当前可见正文: {str(exc)[:120]}")
            card_text = extract_debug_card_text(item)
            answer_id = extract_answer_id_from_item(item)
            if not answer_id:
                print("❌ 第一条动态不是回答，或没有提取到 answer_id。")
                return 1
            print(f"📡 [Debug] 请求评论 API，answer_id={answer_id}")
            comments = fetch_comments_via_api(page, answer_id)
            print_debug_full_report(card_text, answer_id, comments)
            return 0
        except Exception as exc:
            print(f"❌ [Debug] 评论测试失败: {str(exc)[:500]}")
            return 1
        finally:
            context.close()
            browser.close()
