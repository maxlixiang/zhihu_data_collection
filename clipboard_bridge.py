import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from zhihu_archive.content import build_frontmatter, download_img_and_replace_md_link
from zhihu_archive.store import (
    begin_run,
    build_archive_title,
    content_identity_from_url,
    finish_run,
    increment_run_new_count,
    init_db,
    is_known,
    normalize_activity_time,
    record_seen,
    register_success,
    resolve_collision_title,
)


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_FILE = os.getenv("ZH_DB_FILE", os.path.join(PROJECT_DIR, "zhihu_articles.db"))
DEFAULT_OUTPUT_DIR = os.getenv(
    "LOCAL_ARCHIVE_ROOT_DIR",
    os.path.join(PROJECT_DIR, "data", "articles"),
)
DEFAULT_SCOPE = "https://www.zhihu.com/people/li-xiang-57-76"


def read_windows_clipboard_text() -> str:
    if os.name != "nt":
        raise RuntimeError("系统剪贴板读取仅支持 Windows；测试时请使用 --input-file 或 --stdin")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    cf_unicode_text = 13
    if not user32.OpenClipboard(None):
        raise RuntimeError("无法打开 Windows 剪贴板")
    try:
        handle = user32.GetClipboardData(cf_unicode_text)
        if not handle:
            raise RuntimeError("剪贴板中没有 Unicode 文本")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("无法锁定剪贴板数据")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def parse_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    normalized = markdown.replace("\r\n", "\n").lstrip("\ufeff")
    metadata: dict[str, str] = {}
    if normalized.startswith("---\n"):
        end = normalized.find("\n---", 4)
        if end != -1:
            for line in normalized[4:end].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip()] = value.strip()
            body_start = end + 4
            body = normalized[body_start:].lstrip("\n")
            return metadata, body
    return metadata, normalized


def extract_title_and_body(markdown: str, explicit_title: str | None = None) -> tuple[str, str, dict]:
    metadata, body = parse_frontmatter(markdown)
    title = explicit_title or unquote_frontmatter_value(metadata.get("title"))
    if not title:
        lines = body.splitlines()
        first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
        if first_index is None:
            raise ValueError("剪贴板 Markdown 为空")
        first = lines[first_index].strip()
        title = re.sub(r"^#+\s*", "", first).strip()
        del lines[first_index]
        body = "\n".join(lines).lstrip("\n")
    elif body.lstrip().startswith(title):
        body_lines = body.lstrip().splitlines()
        if body_lines and body_lines[0].strip() == title:
            body = "\n".join(body_lines[1:]).lstrip("\n")
    return title, body.rstrip() + "\n", metadata


def load_markdown(args: argparse.Namespace) -> str:
    if args.input_file:
        return Path(args.input_file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return read_windows_clipboard_text()


def calculate_comment_target(total: int) -> int:
    if total < 0:
        raise ValueError("评论总数不能为负数")
    return total if total <= 30 else min((total + 1) // 2, 200)


def unquote_frontmatter_value(value: str | None) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def normalize_title_for_comparison(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().rstrip("。！？.!?")


def ingest(args: argparse.Namespace) -> dict:
    raw_markdown = load_markdown(args)
    title, body, metadata = extract_title_and_body(raw_markdown, args.title)
    expected_title = getattr(args, "expected_title", None)
    if expected_title and normalize_title_for_comparison(title) != normalize_title_for_comparison(
        expected_title
    ):
        raise ValueError(
            f"剪贴板标题与当前页面不匹配：剪贴板={title!r}，当前页面={expected_title!r}"
        )
    source_url = args.content_url or unquote_frontmatter_value(
        metadata.get("source_url") or metadata.get("url")
    )
    content_type, content_id, derived_key = content_identity_from_url(source_url)
    content_key = args.content_key or derived_key
    if not source_url:
        raise ValueError("缺少内容 URL；请启用油猴脚本的复制 frontmatter，或传入 --content-url")
    if not content_key or not content_type or not content_id:
        if args.content_key and ":" in args.content_key:
            content_type, content_id = args.content_key.split(":", 1)
        else:
            raise ValueError("无法从 URL 识别回答、文章或想法 ID")

    base_title = build_archive_title(args.activity_time, title)
    if is_known(args.db_file, content_key, base_title):
        return {"status": "already_known", "content_key": content_key, "archive_title": base_title}

    normalized_activity_time, _ = normalize_activity_time(args.activity_time)
    activity_year, activity_month = normalized_activity_time[:7].split("-")
    output_dir = os.path.join(
        os.path.abspath(args.output_dir),
        activity_year,
        activity_month,
    )
    os.makedirs(output_dir, exist_ok=True)
    archive_title = resolve_collision_title(output_dir, args.activity_time, title)
    localized_body = download_img_and_replace_md_link(
        body,
        archive_title,
        output_dir,
        strict=not args.allow_remote_images,
    )
    archive_metadata = {
        "author": getattr(args, "author", None)
        or unquote_frontmatter_value(metadata.get("author")),
        "activity_at": normalized_activity_time,
        "activity_action": getattr(args, "activity_action", None)
        or unquote_frontmatter_value(metadata.get("activity_action")),
        "published_at": getattr(args, "published_at", None)
        or unquote_frontmatter_value(metadata.get("published_at") or metadata.get("created")),
        "source_url": source_url,
        "source_type": getattr(args, "source_type", None) or content_type,
        "answer_id": content_id if content_type == "answer" else "",
    }
    required_metadata = ("author", "activity_at", "activity_action", "published_at", "source_url", "source_type")
    missing_metadata = [key for key in required_metadata if not archive_metadata.get(key)]
    if missing_metadata:
        raise ValueError("缺少完整归档所需元数据: " + ", ".join(missing_metadata))
    frontmatter = build_frontmatter(title, archive_metadata)
    final_markdown = f"{frontmatter}\n\n# {title}\n\n---\n\n{localized_body}"
    markdown_path = os.path.join(output_dir, archive_title + ".md")
    with open(markdown_path, "w", encoding="utf-8", newline="\n") as file_handle:
        file_handle.write(final_markdown)

    saved_bytes = Path(markdown_path).read_bytes()
    if len(saved_bytes) < 16:
        raise RuntimeError("Markdown 文件写入后过小，拒绝登记成功")
    saved_text = saved_bytes.decode("utf-8")
    if not saved_text.startswith("---\n") or f'\ntitle: "{title}"\n' not in saved_text:
        raise RuntimeError("Markdown UTF-8 回读校验失败")
    digest = hashlib.sha256(saved_bytes).hexdigest()
    image_dir = os.path.join(output_dir, archive_title)
    register_success(
        args.db_file,
        content_key=content_key,
        source_url=source_url,
        activity_time=args.activity_time,
        archive_title=archive_title,
        markdown_path=markdown_path,
        image_dir=image_dir if os.path.isdir(image_dir) else "",
        source_method="computer_use",
        file_sha256=digest,
    )
    increment_run_new_count(args.db_file, args.run_id)
    return {
        "status": "saved",
        "content_key": content_key,
        "archive_title": archive_title,
        "markdown_path": markdown_path,
        "image_dir": image_dir if os.path.isdir(image_dir) else None,
        "sha256": digest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Computer Use + 知乎油猴插件本地归档桥接程序")
    parser.add_argument("--db-file", default=DEFAULT_DB_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    comment_target = subparsers.add_parser("comment-target")
    comment_target.add_argument("--total", required=True, type=int)

    begin = subparsers.add_parser("begin-run")
    begin.add_argument("--scope", default=DEFAULT_SCOPE)
    begin.add_argument("--source", default="computer_use")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--content-key", required=True)
    inspect.add_argument("--archive-title")

    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--run-id")
    ingest_parser.add_argument("--activity-time", required=True)
    ingest_parser.add_argument("--content-url")
    ingest_parser.add_argument("--content-key")
    ingest_parser.add_argument("--title")
    ingest_parser.add_argument("--expected-title")
    ingest_parser.add_argument("--author")
    ingest_parser.add_argument("--activity-action")
    ingest_parser.add_argument("--published-at")
    ingest_parser.add_argument("--source-type")
    ingest_parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    ingest_parser.add_argument("--input-file")
    ingest_parser.add_argument("--stdin", action="store_true")
    ingest_parser.add_argument("--allow-remote-images", action="store_true")

    finish = subparsers.add_parser("finish-run")
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--boundary-hit", action="store_true")
    finish.add_argument("--reached-end", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init-db":
            init_db(args.db_file)
            result = {"status": "ready", "db_file": os.path.abspath(args.db_file)}
        elif args.command == "comment-target":
            result = {"total": args.total, "target": calculate_comment_target(args.total)}
        elif args.command == "begin-run":
            result = begin_run(args.db_file, args.scope, args.source)
        elif args.command == "inspect":
            result = record_seen(args.db_file, args.run_id, args.content_key)
            result["known"] = is_known(args.db_file, args.content_key, args.archive_title)
        elif args.command == "ingest":
            result = ingest(args)
        elif args.command == "finish-run":
            result = finish_run(
                args.db_file,
                args.run_id,
                boundary_hit=args.boundary_hit,
                reached_end=args.reached_end,
            )
        else:
            parser.error("未知命令")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
