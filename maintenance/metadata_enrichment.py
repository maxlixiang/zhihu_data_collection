import argparse
import csv
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from pathlib import Path

from playwright.sync_api import sync_playwright

from zhihu_scraper import (
    PAGE_LOAD_TIMEOUT_MS,
    PROFILE_URL,
    SUPPORTED_ACTION_KEYWORDS,
    build_frontmatter,
    detect_blocked_zhihu_page,
    extract_content_metadata,
    extract_title_from_activity_item,
    normalize_target_url,
    normalize_text,
    parse_activity_time,
    safe_page_snapshot,
    should_archive_action,
)


FILENAME_TIME_RE = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})\]\s*(.*)$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
REQUIRED_COMMON_FIELDS = ("author", "published_at", "source_url", "source_type")


@dataclass
class ArchiveNote:
    path: Path
    relative_path: str
    activity_at: datetime
    title: str
    title_keys: tuple[str, ...]
    original_bytes: bytes
    original_text: str


@dataclass
class MatchResult:
    note: ArchiveNote
    metadata: dict
    missing_fields: list[str]


PROGRESS_FIELDS = (
    "path",
    "author",
    "activity_at",
    "published_at",
    "source_url",
    "source_type",
    "zhihu_answer_id",
    "missing_fields",
    "applied",
    "write_error",
)


def log(message=""):
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = str(message).encode(encoding, errors="replace").decode(encoding)
        print(safe_message, flush=True)


def create_run_dir(report_root):
    report_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = report_root / timestamp
    suffix = 2
    while run_dir.exists():
        run_dir = report_root / f"{timestamp}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_dir


def write_json_atomically(path, payload):
    temporary_path = path.with_name(path.name + ".tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with open(temporary_path, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


class ProgressRecorder:
    def __init__(self, run_dir, archive_root, start_date, end_date, apply_changes, target_count):
        self.run_dir = run_dir
        self.status_path = run_dir / "运行状态.json"
        self.progress_path = run_dir / "实时补全进度.csv"
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.matched_count = 0
        self.applied_count = 0
        self.write_error_count = 0
        self.last_path = ""
        self._closed = False
        self._base_status = {
            "archive_root": str(archive_root),
            "start_date": start_date,
            "end_date": end_date,
            "mode": "apply" if apply_changes else "dry-run",
            "target_count": target_count,
            "started_at": self.started_at,
        }
        self._progress_handle = open(self.progress_path, "w", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._progress_handle, fieldnames=PROGRESS_FIELDS)
        self._writer.writeheader()
        self._flush_progress()
        self.update_status("running")

    def _flush_progress(self):
        self._progress_handle.flush()
        os.fsync(self._progress_handle.fileno())

    def update_status(self, status, error=""):
        payload = {
            **self._base_status,
            "status": status,
            "matched": self.matched_count,
            "applied": self.applied_count,
            "write_errors": self.write_error_count,
            "last_path": self.last_path,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if error:
            payload["error"] = error
        write_json_atomically(self.status_path, payload)

    def record_match(self, result, applied, write_error=""):
        relative_path = result.note.relative_path
        self._writer.writerow(
            {
                "path": relative_path,
                "author": result.metadata.get("author", ""),
                "activity_at": result.metadata.get("activity_at", ""),
                "published_at": result.metadata.get("published_at", ""),
                "source_url": result.metadata.get("source_url", ""),
                "source_type": result.metadata.get("source_type", ""),
                "zhihu_answer_id": result.metadata.get("answer_id", ""),
                "missing_fields": ",".join(result.missing_fields),
                "applied": "yes" if applied else "no",
                "write_error": write_error,
            }
        )
        self.matched_count += 1
        self.applied_count += int(applied)
        self.write_error_count += int(bool(write_error))
        self.last_path = relative_path
        self._flush_progress()
        self.update_status("running")

    def finish(self, status, error=""):
        if self._closed:
            return
        self.update_status(status, error)
        self._progress_handle.close()
        self._closed = True


def normalize_title(value):
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def decode_utf8(raw_bytes, path):
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"不是有效 UTF-8: {path} ({exc})") from exc


def parse_archive_note(path, archive_root):
    match = FILENAME_TIME_RE.match(path.stem)
    if not match:
        return None, "文件名没有 [YYYY-MM-DD_HH-MM] 时间前缀"

    try:
        activity_at = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
        )
    except ValueError as exc:
        return None, f"文件名时间无效: {exc}"

    raw_bytes = path.read_bytes()
    try:
        text = decode_utf8(raw_bytes, path)
    except ValueError as exc:
        return None, str(exc)
    if "\ufffd" in text:
        return None, "文件包含 Unicode 替换字符 U+FFFD"
    if text.startswith("---\n") or text.startswith("---\r\n"):
        return None, "文件已经包含 YAML frontmatter"

    h1_match = H1_RE.search(text)
    h1_title = normalize_text(h1_match.group(1)) if h1_match else ""
    filename_title = normalize_text(match.group(6))
    title = h1_title or filename_title
    if not title:
        return None, "没有从一级标题或文件名解析到标题"

    keys = []
    for candidate in (h1_title, filename_title):
        key = normalize_title(candidate)
        if key and key not in keys:
            keys.append(key)
    if not keys:
        return None, "标题规范化后为空"

    note = ArchiveNote(
        path=path,
        relative_path=str(path.relative_to(archive_root)),
        activity_at=activity_at,
        title=title,
        title_keys=tuple(keys),
        original_bytes=raw_bytes,
        original_text=text,
    )
    return note, ""


def load_archive_notes(archive_root, start_dt, end_dt):
    notes = []
    skipped = []
    all_markdown = sorted(archive_root.rglob("*.md"))
    log(f"📚 扫描 Markdown: {len(all_markdown)} 个")

    for path in all_markdown:
        note, reason = parse_archive_note(path, archive_root)
        if note is None:
            skipped.append({"path": str(path.relative_to(archive_root)), "reason": reason})
            continue
        if start_dt <= note.activity_at <= end_dt:
            notes.append(note)

    log(f"🎯 日期范围内待补全: {len(notes)} 个")
    log(f"⏭️ 已有表头或无法解析: {len(skipped)} 个")
    return notes, skipped


def build_note_index(notes):
    index = {}
    for note in notes:
        minute_key = note.activity_at.strftime("%Y-%m-%d %H:%M")
        for title_key in note.title_keys:
            index.setdefault((minute_key, title_key), set()).add(note.relative_path)
    return index, {note.relative_path: note for note in notes}


def missing_metadata_fields(metadata):
    missing = [field for field in REQUIRED_COMMON_FIELDS if not metadata.get(field)]
    if metadata.get("source_type") == "answer" and not metadata.get("answer_id"):
        missing.append("zhihu_answer_id")
    return missing


def make_activity_metadata(item, title, activity_at, action_text):
    metadata = extract_content_metadata(item)
    metadata["activity_at"] = activity_at.strftime("%Y-%m-%d %H:%M")
    metadata["activity_action"] = normalize_text(action_text)
    metadata["title"] = title
    if metadata.get("source_type") == "answer" and metadata.get("answer_id"):
        source_url = metadata.get("source_url", "").strip()
        if "/question/" in source_url and "/answer/" not in source_url:
            metadata["source_url"] = f"{source_url.rstrip('/')}/answer/{metadata['answer_id']}"
    else:
        metadata["answer_id"] = ""
    return metadata


def crawl_and_match(
    notes,
    url,
    state_file,
    headed,
    max_scrolls,
    delay_min,
    delay_max,
    apply_changes,
    progress_recorder,
):
    if delay_min > delay_max:
        raise ValueError("delay-min 不能大于 delay-max")
    if not state_file.exists():
        raise FileNotFoundError(f"找不到登录态: {state_file}")

    index, notes_by_path = build_note_index(notes)
    unmatched_paths = set(notes_by_path)
    matched = {}
    applied_paths = set()
    write_errors = []
    ambiguous = {}
    seen_activities = set()
    seen_cards = set()
    consecutive_stale = 0
    reached_start = False
    next_scan_index = 0
    target_start = min(note.activity_at for note in notes)
    target_end = max(note.activity_at for note in notes)
    profile_url = normalize_target_url(url)

    log(f"🌐 主页: {profile_url}")
    log(f"📅 实际待匹配范围: {target_start:%Y-%m-%d %H:%M} 至 {target_end:%Y-%m-%d %H:%M}")
    log("ℹ️ 本程序不展开正文、不请求评论接口、不下载图片。")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            storage_state=str(state_file),
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        try:
            try:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as first_error:
                log(f"⚠️ 首次打开失败，3 秒后重试: {str(first_error)[:160]}")
                time.sleep(3)
                page.goto(profile_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            time.sleep(4)

            blocked_reason = detect_blocked_zhihu_page(page)
            if blocked_reason:
                safe_page_snapshot(page, "metadata_enrichment_blocked")
                raise RuntimeError(blocked_reason)

            for scroll_index in range(max_scrolls):
                blocked_reason = detect_blocked_zhihu_page(page)
                if blocked_reason:
                    safe_page_snapshot(page, "metadata_enrichment_blocked")
                    raise RuntimeError(blocked_reason)

                items = page.locator(".List-item")
                item_count = items.count()
                if item_count == 0:
                    safe_page_snapshot(page, "metadata_enrichment_no_items")
                    raise RuntimeError("没有找到知乎动态卡片 .List-item")

                if item_count < next_scan_index:
                    # 兼容可能采用虚拟列表、替换已有 DOM 节点的页面版本。
                    next_scan_index = 0
                scan_start = min(next_scan_index, item_count)

                oldest_visible = None
                newest_visible = None
                newly_seen = 0

                for index_in_page in range(scan_start, item_count):
                    item = items.nth(index_in_page)
                    try:
                        meta_locator = item.locator(".ActivityItem-meta")
                        if not meta_locator.count():
                            continue
                        meta_text = normalize_text(meta_locator.inner_text(timeout=700))
                        action_locator = item.locator(".ActivityItem-metaTitle")
                        action_text = (
                            normalize_text(action_locator.inner_text(timeout=700))
                            if action_locator.count()
                            else meta_text
                        )
                    except Exception:
                        continue

                    activity_at, _ = parse_activity_time(meta_text)
                    if not activity_at:
                        continue
                    oldest_visible = activity_at if oldest_visible is None else min(oldest_visible, activity_at)
                    newest_visible = activity_at if newest_visible is None else max(newest_visible, activity_at)
                    if activity_at < target_start:
                        reached_start = True

                    card_signature = (activity_at.strftime("%Y-%m-%d %H:%M"), action_text, index_in_page)
                    if card_signature not in seen_cards:
                        seen_cards.add(card_signature)
                        newly_seen += 1

                    if not should_archive_action(action_text):
                        continue
                    title = extract_title_from_activity_item(item, action_text)
                    title_key = normalize_title(title)
                    activity_key = (activity_at.strftime("%Y-%m-%d %H:%M"), title_key, action_text)
                    if activity_key in seen_activities:
                        continue
                    seen_activities.add(activity_key)

                    if not (target_start <= activity_at <= target_end):
                        continue
                    candidate_paths = set(index.get((activity_key[0], title_key), set())) & unmatched_paths
                    if not candidate_paths:
                        continue
                    if len(candidate_paths) > 1:
                        for relative_path in candidate_paths:
                            ambiguous[relative_path] = {
                                "reason": "同一分钟和标题对应多个旧文件，未自动写入",
                                "activity_at": activity_key[0],
                                "title": title,
                            }
                        continue

                    relative_path = next(iter(candidate_paths))
                    note = notes_by_path[relative_path]
                    metadata = make_activity_metadata(item, note.title, activity_at, action_text)
                    result = MatchResult(
                        note=note,
                        metadata=metadata,
                        missing_fields=missing_metadata_fields(metadata),
                    )
                    matched[relative_path] = result
                    unmatched_paths.remove(relative_path)

                    applied = False
                    write_error = ""
                    if apply_changes:
                        try:
                            prepend_frontmatter(result)
                            applied_paths.add(relative_path)
                            applied = True
                            log(f"✅ 已立即补全 [{len(applied_paths)}]: {relative_path}")
                        except Exception as exc:
                            write_error = str(exc)
                            write_errors.append({"path": relative_path, "reason": write_error})
                            log(f"⚠️ 写入失败: {relative_path}: {write_error}")
                    progress_recorder.record_match(result, applied, write_error)

                next_scan_index = item_count

                if newly_seen:
                    consecutive_stale = 0
                else:
                    consecutive_stale += 1

                oldest_text = oldest_visible.strftime("%Y-%m-%d %H:%M") if oldest_visible else "未知"
                newest_text = newest_visible.strftime("%Y-%m-%d %H:%M") if newest_visible else "未知"
                log(
                    f"⏬ [{scroll_index + 1}/{max_scrolls}] 卡片={item_count} 本轮扫描={item_count - scan_start} "
                    f"新动态={newly_seen} "
                    f"最新={newest_text} 最旧={oldest_text} 已匹配={len(matched)}/{len(notes)}"
                )

                if not unmatched_paths:
                    log("✅ 所有目标文件都已匹配。")
                    break
                if reached_start and oldest_visible and oldest_visible < target_start:
                    log("✅ 已滚动到目标起始时间以前。")
                    break
                if consecutive_stale >= 10:
                    log("⚠️ 连续多次没有加载出新动态，提前停止。")
                    break

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(delay_min, delay_max))
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    return matched, unmatched_paths, ambiguous, reached_start, applied_paths, write_errors


def prepend_frontmatter(match_result):
    note = match_result.note
    frontmatter = build_frontmatter(note.title, match_result.metadata)
    newline = "\r\n" if b"\r\n" in note.original_bytes[:4096] else "\n"
    prefix = (frontmatter.replace("\n", newline) + newline * 2).encode("utf-8")
    original_body = note.original_bytes
    if original_body.startswith(b"\xef\xbb\xbf"):
        original_body = original_body[3:]

    temporary_path = note.path.with_name(note.path.name + ".metadata_tmp")
    with open(temporary_path, "wb") as handle:
        handle.write(prefix)
        handle.write(original_body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, note.path)

    updated_bytes = note.path.read_bytes()
    updated_text = decode_utf8(updated_bytes, note.path)
    if "\ufffd" in updated_text:
        raise RuntimeError(f"写入后出现 Unicode 替换字符: {note.path}")
    if not updated_text.startswith("---"):
        raise RuntimeError(f"写入后 YAML 未位于文件开头: {note.path}")
    if not updated_bytes.endswith(original_body):
        raise RuntimeError(f"写入后原正文校验失败: {note.path}")


def write_csv(path, fieldnames, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_reports(
    run_dir,
    report_root,
    archive_root,
    start_date,
    end_date,
    apply_changes,
    matched,
    unmatched_paths,
    ambiguous,
    skipped,
    reached_start,
    applied_paths,
    write_errors,
):
    if run_dir is None:
        run_dir = create_run_dir(report_root)

    unmatched_rows = []
    for relative_path in sorted(unmatched_paths):
        if relative_path in ambiguous:
            reason = ambiguous[relative_path]["reason"]
        elif reached_start:
            reason = "主页动态中未匹配；可能已删除、隐藏、标题变化，或该动作不再展示"
        else:
            reason = "本次滚动未完整到达起始日期，暂不能判断是否已删除"
        unmatched_rows.append({"path": relative_path, "reason": reason})
    write_csv(run_dir / "未补全文件列表.csv", ("path", "reason"), unmatched_rows)

    incomplete_rows = []
    for relative_path, result in sorted(matched.items()):
        if result.missing_fields:
            incomplete_rows.append(
                {
                    "path": relative_path,
                    "missing_fields": ",".join(result.missing_fields),
                    "source_url": result.metadata.get("source_url", ""),
                }
            )
    write_csv(
        run_dir / "匹配但字段不完整.csv",
        ("path", "missing_fields", "source_url"),
        incomplete_rows,
    )

    matched_rows = []
    for relative_path, result in sorted(matched.items()):
        matched_rows.append(
            {
                "path": relative_path,
                "author": result.metadata.get("author", ""),
                "activity_at": result.metadata.get("activity_at", ""),
                "published_at": result.metadata.get("published_at", ""),
                "source_url": result.metadata.get("source_url", ""),
                "source_type": result.metadata.get("source_type", ""),
                "zhihu_answer_id": result.metadata.get("answer_id", ""),
                "applied": "yes" if relative_path in applied_paths else "no",
            }
        )
    write_csv(
        run_dir / "匹配结果.csv",
        ("path", "author", "activity_at", "published_at", "source_url", "source_type", "zhihu_answer_id", "applied"),
        matched_rows,
    )

    write_csv(run_dir / "跳过文件列表.csv", ("path", "reason"), skipped)
    write_csv(run_dir / "写入失败列表.csv", ("path", "reason"), write_errors)

    summary = {
        "archive_root": str(archive_root),
        "start_date": start_date,
        "end_date": end_date,
        "mode": "apply" if apply_changes else "dry-run",
        "matched": len(matched),
        "applied": len(applied_paths),
        "unmatched": len(unmatched_rows),
        "matched_but_incomplete": len(incomplete_rows),
        "skipped": len(skipped),
        "write_errors": len(write_errors),
        "reached_start_date": reached_start,
    }
    (run_dir / "汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, summary


def main():
    parser = argparse.ArgumentParser(description="为旧知乎 Markdown 原位补全 YAML 元数据")
    parser.add_argument(
        "--archive-root",
        default=str(Path(__file__).resolve().parents[1] / "data" / "articles"),
        help="旧 Markdown 根目录，默认当前项目的 data/articles",
    )
    parser.add_argument("--start-date", default="2023-01-01", help="动态日期起点")
    parser.add_argument("--end-date", default="2026-05-31", help="动态日期终点")
    parser.add_argument("--url", default=PROFILE_URL, help="知乎个人主页或 activities URL")
    parser.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parents[1] / "state.json"),
        help="Playwright 登录态",
    )
    parser.add_argument(
        "--report-dir",
        default=str(Path(__file__).resolve().parents[1] / "metadata_reports"),
        help="报告输出根目录",
    )
    parser.add_argument("--max-scrolls", type=int, default=20000)
    parser.add_argument("--delay-min", type=float, default=1.5)
    parser.add_argument("--delay-max", type=float, default=3.0)
    parser.add_argument("--headless", action="store_true", help="隐藏 Chromium；默认显示")
    parser.add_argument("--apply", action="store_true", help="原位写入 YAML；不传时只生成预览报告")
    args = parser.parse_args()

    archive_root = Path(args.archive_root).resolve()
    state_file = Path(args.state_file).resolve()
    report_root = Path(args.report_dir).resolve()
    if not archive_root.is_dir():
        raise NotADirectoryError(f"旧归档目录不存在: {archive_root}")

    start_dt = datetime.combine(date.fromisoformat(args.start_date), datetime_time.min)
    end_dt = datetime.combine(date.fromisoformat(args.end_date), datetime_time.max)
    if start_dt > end_dt:
        raise ValueError("start-date 不能晚于 end-date")

    log("=" * 60)
    log("知乎旧 Markdown 元数据补全")
    log(f"模式: {'原位写入' if args.apply else 'DRY-RUN（不会修改旧文件）'}")
    log(f"归档: {archive_root}")
    log(f"范围: {args.start_date} 至 {args.end_date}")
    log(f"动作: {', '.join(SUPPORTED_ACTION_KEYWORDS)}")
    log("=" * 60)

    notes, skipped = load_archive_notes(archive_root, start_dt, end_dt)
    if not notes:
        log("没有需要补全的文件。")
        return 0

    run_dir = create_run_dir(report_root)
    progress_recorder = ProgressRecorder(
        run_dir=run_dir,
        archive_root=archive_root,
        start_date=args.start_date,
        end_date=args.end_date,
        apply_changes=args.apply,
        target_count=len(notes),
    )
    log(f"📁 实时进度目录: {run_dir}")

    try:
        matched, unmatched_paths, ambiguous, reached_start, applied_paths, write_errors = crawl_and_match(
            notes=notes,
            url=args.url,
            state_file=state_file,
            headed=not args.headless,
            max_scrolls=args.max_scrolls,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            apply_changes=args.apply,
            progress_recorder=progress_recorder,
        )

        report_dir, summary = write_reports(
            run_dir=run_dir,
            report_root=report_root,
            archive_root=archive_root,
            start_date=args.start_date,
            end_date=args.end_date,
            apply_changes=args.apply,
            matched=matched,
            unmatched_paths=unmatched_paths,
            ambiguous=ambiguous,
            skipped=skipped,
            reached_start=reached_start,
            applied_paths=applied_paths,
            write_errors=write_errors,
        )
        progress_recorder.finish("completed")
    except KeyboardInterrupt:
        progress_recorder.finish("interrupted")
        log("\n用户中断；已成功补全的文件和实时进度均已保留。")
        log(f"📁 本次进度目录: {run_dir}")
        return 130
    except Exception as exc:
        progress_recorder.finish("failed", str(exc))
        log(f"📁 失败前的实时进度已保留: {run_dir}")
        raise
    log(f"\n📊 汇总: {json.dumps(summary, ensure_ascii=False)}")
    log(f"📁 报告目录: {report_dir}")
    if not args.apply:
        log("ℹ️ 这是 dry-run；确认报告后，用相同参数加 --apply 才会修改旧文件。")
    return 1 if write_errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("\n用户中断；已成功补全的文件会保留，再次运行会自动跳过。")
        raise SystemExit(130)
    except Exception as exc:
        log(f"\n❌ 执行失败: {exc}")
        raise SystemExit(1)
