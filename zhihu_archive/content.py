import os
import random
import re
import time
from urllib.parse import quote, urlparse

import requests


MAX_FILE_NAME_LENGTH = 100
DEFAULT_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.zhihu.com/",
}


def clean_file_name(title: str) -> str:
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '，', '。', '\n', '\r']:
        title = title.replace(char, "")
    return title.strip()[:MAX_FILE_NAME_LENGTH]


def yaml_quote(value: str) -> str:
    value = "" if value is None else str(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_frontmatter(title: str, metadata: dict) -> str:
    fields = {
        "title": title,
        "author": metadata.get("author", ""),
        "activity_at": metadata.get("activity_at", ""),
        "activity_action": metadata.get("activity_action", ""),
        "published_at": metadata.get("published_at", ""),
        "source_url": metadata.get("source_url", ""),
        "source_type": metadata.get("source_type", ""),
        "zhihu_answer_id": metadata.get("answer_id", ""),
    }
    lines = ["---"]
    for key, value in fields.items():
        if value:
            lines.append(f"{key}: {yaml_quote(value)}")
    lines.append("---")
    return "\n".join(lines)


def download_img_and_replace_md_link(
    md_content: str,
    article_title: str,
    save_dir: str,
    *,
    strict: bool = False,
    request_headers: dict | None = None,
) -> str:
    """下载 Markdown 远程图片，并沿用现有的同名图片目录结构。"""
    img_sub_dir = clean_file_name(article_title)
    img_save_path = os.path.join(save_dir, img_sub_dir)
    img_pattern = re.compile(r"!\[(.*?)\]\((https?://.*?)\)")
    all_img = img_pattern.findall(md_content)

    if not all_img:
        return md_content
    os.makedirs(img_save_path, exist_ok=True)

    failures: list[str] = []
    for img_desc, img_url in all_img:
        try:
            img_path = urlparse(img_url).path
            img_suffix = os.path.splitext(img_path)[1].lstrip(".").lower()
            if img_suffix not in ["jpg", "png", "gif", "webp", "jpeg"]:
                img_suffix = "jpg"
            img_name = f"{clean_file_name(img_desc)[:10]}_{int(time.time() * 1000)}.{img_suffix}"
            img_file_path = os.path.join(img_save_path, img_name)

            time.sleep(random.uniform(0.1, 0.4))
            img_response = requests.get(
                img_url,
                headers=request_headers or DEFAULT_IMAGE_HEADERS,
                timeout=15,
            )
            img_response.raise_for_status()
            with open(img_file_path, "wb") as file_handle:
                file_handle.write(img_response.content)

            safe_rel_path = quote(f"{img_sub_dir}/{img_name}")
            md_content = md_content.replace(img_url, safe_rel_path)
        except Exception as exc:
            failures.append(f"{img_url}: {exc}")

    if strict and failures:
        raise RuntimeError("图片下载失败：" + " | ".join(failures[:3]))
    return md_content
