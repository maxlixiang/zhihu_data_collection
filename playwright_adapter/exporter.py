import os
import random
import re
import sqlite3
import time

from zhihu_archive import store as archive_store
from zhihu_archive.content import build_frontmatter, download_img_and_replace_md_link

from .activity_parser import (
    extract_content_identity_from_item,
    extract_content_metadata,
    normalize_text,
)
from .comments import fetch_comments_via_api, format_comments_markdown
from .config import RuntimeConfig
from .console import safe_print as print

try:
    from markdownify import markdownify as md
except ModuleNotFoundError:
    md = None


def require_markdownify():
    if md is None:
        raise RuntimeError("缺少依赖 markdownify。请先运行: pip install -r requirements.txt")


def init_db(config: RuntimeConfig):
    archive_store.init_db(config.db_file)


def is_article_exists(config: RuntimeConfig, title, content_key=None):
    return archive_store.is_known(config.db_file, content_key, title)


def save_article_to_db(
    config: RuntimeConfig,
    title,
    content_identity=None,
    markdown_path="",
    source_method="playwright",
):
    if not content_identity or not content_identity.get("content_key"):
        with sqlite3.connect(config.db_file) as conn:
            conn.execute("INSERT OR IGNORE INTO articles (title) VALUES (?)", (title,))
        return
    archive_store.register_success(
        config.db_file,
        content_key=content_identity["content_key"],
        source_url=content_identity.get("url", ""),
        activity_time=content_identity.get("activity_time", ""),
        archive_title=title,
        markdown_path=os.path.abspath(markdown_path) if markdown_path else "",
        image_dir=os.path.abspath(os.path.splitext(markdown_path)[0]) if markdown_path else "",
        source_method=source_method,
        file_sha256="",
    )


def get_save_dir_from_time_str(
    config: RuntimeConfig,
    time_str: str,
    root_dir: str | None = None,
) -> str:
    match = re.match(r"\[(\d{4})-(\d{2})-\d{2}_\d{2}-\d{2}\]", time_str)
    if match:
        year, month = match.groups()
    else:
        year, month = time.strftime("%Y"), time.strftime("%m")
    save_dir = os.path.join(root_dir or config.archive_root_dir, year, month)
    os.makedirs(save_dir, exist_ok=True)
    return save_dir


def get_flat_save_dir(config: RuntimeConfig, output_dir: str | None = None) -> str:
    target = output_dir or config.local_archive_root_dir
    os.makedirs(target, exist_ok=True)
    return target


def export_activity_item(
    config: RuntimeConfig,
    page,
    item,
    title: str,
    clean_title_str: str,
    save_dir: str,
    activity_dt,
    action_text: str,
    *,
    content_identity=None,
    include_comments: bool = True,
) -> str:
    require_markdownify()
    item.scroll_into_view_if_needed()
    time.sleep(random.uniform(0.7, 1.5))

    expand_btn = item.locator(
        'button:has-text("阅读全文"), button:has-text("展开全文"), button:has-text("阅读原文")'
    )
    if expand_btn.count() > 0:
        try:
            expand_btn.first.evaluate("node => node.click()")
            time.sleep(random.uniform(1.8, 3.0))
        except Exception:
            pass

    try:
        content_box = item.locator('.RichContent-inner, .RichText').first
        raw_md = "\n".join(
            line
            for line in md(content_box.inner_html(), heading_style="ATX").split("\n")
            if line.strip()
        )
    except Exception as exc:
        raw_md = f"【⚠️ 正文提取失败】{str(exc)[:80]}"

    content_metadata = extract_content_metadata(item)
    content_metadata["activity_at"] = (
        activity_dt.strftime("%Y-%m-%d %H:%M") if activity_dt else ""
    )
    content_metadata["activity_action"] = normalize_text(action_text)
    if content_identity is None:
        content_identity = extract_content_identity_from_item(item)
    if content_identity:
        if content_identity.get("url"):
            content_metadata["source_url"] = content_identity["url"]
        content_type = content_identity.get("content_type")
        if content_type:
            content_metadata["source_type"] = content_type
        if content_type == "answer":
            content_metadata["answer_id"] = content_identity.get("content_id", "")

    comments_md_text = ""
    target_id = (
        content_identity.get("content_id")
        if content_identity and content_identity.get("content_type") == "answer"
        else None
    )
    if target_id and include_comments:
        try:
            print(f"   📡 识别为回答，提取评论 answer_id={target_id}")
            comments = fetch_comments_via_api(page, target_id)
            comments_md_text = format_comments_markdown(comments)
            if not comments_md_text:
                print("   ⚠️ 接口调用成功，但没有可保存的有效评论。")
        except Exception as exc:
            print(f"   ⚠️ 评论提取异常，跳过评论: {str(exc)[:200]}")
    elif target_id:
        print("   ⏭️ 已通过 --no-comments 关闭评论提取。")
    else:
        print("   ⏭️ 当前动态非“回答”，跳过评论提取。")

    final_md = download_img_and_replace_md_link(raw_md, clean_title_str, save_dir)
    final_md += comments_md_text
    md_file_path = os.path.join(save_dir, f"{clean_title_str}.md")
    frontmatter = build_frontmatter(title, content_metadata)
    with open(md_file_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{frontmatter}\n\n# {title}\n\n---\n\n{final_md}\n")
    print(f"   ✅ 已保存 Markdown: {md_file_path}")
    return md_file_path


export_activity_item_from_profile = export_activity_item
