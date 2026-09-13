import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

from .content import clean_file_name


FRONTIER_SIZE = 20


@contextmanager
def connect(db_file: str):
    conn = sqlite3.connect(db_file, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_file: str) -> None:
    with connect(db_file) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS articles (
                title TEXT PRIMARY KEY,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_items (
                content_key TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                content_id TEXT NOT NULL,
                source_url TEXT,
                activity_time TEXT,
                archive_title TEXT NOT NULL,
                markdown_path TEXT,
                image_dir TEXT,
                source_method TEXT NOT NULL,
                file_sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_frontier (
                scope TEXT NOT NULL,
                rank INTEGER NOT NULL,
                content_key TEXT NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope, rank),
                UNIQUE (scope, content_key)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_runs (
                run_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                source_method TEXT NOT NULL,
                status TEXT NOT NULL,
                boundary_snapshot TEXT NOT NULL,
                seen_keys TEXT NOT NULL DEFAULT '[]',
                new_count INTEGER NOT NULL DEFAULT 0,
                boundary_hit INTEGER NOT NULL DEFAULT 0,
                reached_end INTEGER NOT NULL DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )"""
        )


def content_identity_from_url(url: str | None) -> tuple[str | None, str | None, str | None]:
    if not url:
        return None, None, None
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    patterns = [
        ("answer", r"/question/\d+/answer/(\d+)$"),
        ("answer", r"/answer/(\d+)$"),
        ("article", r"/p/(\d+)$"),
        ("pin", r"/pin/(\d+)$"),
    ]
    for content_type, pattern in patterns:
        match = re.search(pattern, path)
        if match:
            content_id = match.group(1)
            return content_type, content_id, f"{content_type}:{content_id}"
    return None, None, None


def is_known(db_file: str, content_key: str | None, archive_title: str | None = None) -> bool:
    init_db(db_file)
    with connect(db_file) as conn:
        if content_key:
            row = conn.execute(
                "SELECT 1 FROM archive_items WHERE content_key = ? AND status = 'success'",
                (content_key,),
            ).fetchone()
            if row:
                return True
        if archive_title:
            if content_key:
                mapped = conn.execute(
                    "SELECT 1 FROM archive_items WHERE archive_title = ?",
                    (archive_title,),
                ).fetchone()
                if mapped:
                    # 同一标题可能对应同一问题下的不同回答。既然新表已有稳定 ID，
                    # 就不再用旧表的标题命中另一个 ID。
                    return False
            row = conn.execute(
                "SELECT 1 FROM articles WHERE title = ?",
                (archive_title,),
            ).fetchone()
            return row is not None
    return False


def register_success(
    db_file: str,
    *,
    content_key: str,
    source_url: str,
    activity_time: str,
    archive_title: str,
    markdown_path: str,
    image_dir: str,
    source_method: str,
    file_sha256: str,
) -> None:
    content_type, content_id = content_key.split(":", 1)
    init_db(db_file)
    with connect(db_file) as conn:
        conn.execute("INSERT OR IGNORE INTO articles (title) VALUES (?)", (archive_title,))
        conn.execute(
            """INSERT INTO archive_items (
                content_key, content_type, content_id, source_url, activity_time,
                archive_title, markdown_path, image_dir, source_method, file_sha256, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success')
            ON CONFLICT(content_key) DO UPDATE SET
                source_url = excluded.source_url,
                activity_time = excluded.activity_time,
                archive_title = excluded.archive_title,
                markdown_path = excluded.markdown_path,
                image_dir = excluded.image_dir,
                source_method = excluded.source_method,
                file_sha256 = excluded.file_sha256,
                status = 'success'""",
            (
                content_key,
                content_type,
                content_id,
                source_url,
                activity_time,
                archive_title,
                markdown_path,
                image_dir,
                source_method,
                file_sha256,
            ),
        )


def normalize_activity_time(value: str) -> tuple[str, str]:
    value = value.strip().strip("[]").replace("_", " ")
    for fmt in ("%Y-%m-%d %H-%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d %H:%M"), dt.strftime("[%Y-%m-%d_%H-%M]")
        except ValueError:
            pass
    raise ValueError("activity_time 必须是 YYYY-MM-DD HH:MM 或 [YYYY-MM-DD_HH-MM]")


def build_archive_title(activity_time: str, title: str) -> str:
    _, time_str = normalize_activity_time(activity_time)
    return clean_file_name(f"{time_str} {title}")


def resolve_collision_title(output_dir: str, activity_time: str, title: str) -> str:
    normalized, time_str = normalize_activity_time(activity_time)
    base = clean_file_name(f"{time_str} {title}")
    if not os.path.exists(os.path.join(output_dir, base + ".md")):
        return base

    dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M")
    for suffix in range(1, 60):
        collision_time = dt.strftime("[%Y-%m-%d_%H-%M") + f"-{suffix:02d}]"
        candidate = clean_file_name(f"{collision_time} {title}")
        if not os.path.exists(os.path.join(output_dir, candidate + ".md")):
            return candidate
    raise RuntimeError("同一分钟内的同名内容冲突超过 59 个，无法生成安全文件名")


def begin_run(db_file: str, scope: str, source_method: str) -> dict:
    init_db(db_file)
    run_id = str(uuid.uuid4())
    with connect(db_file) as conn:
        boundary = [
            row["content_key"]
            for row in conn.execute(
                "SELECT content_key FROM archive_frontier WHERE scope = ? ORDER BY rank",
                (scope,),
            )
        ]
        conn.execute(
            """INSERT INTO archive_runs
               (run_id, scope, source_method, status, boundary_snapshot)
               VALUES (?, ?, ?, 'running', ?)""",
            (run_id, scope, source_method, json.dumps(boundary, ensure_ascii=False)),
        )
    return {"run_id": run_id, "scope": scope, "boundary_keys": boundary}


def record_seen(db_file: str, run_id: str, content_key: str) -> dict:
    init_db(db_file)
    with connect(db_file) as conn:
        run = conn.execute("SELECT * FROM archive_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise ValueError("找不到正在运行的归档任务")
        boundary = json.loads(run["boundary_snapshot"])
        seen = json.loads(run["seen_keys"])
        if content_key not in seen:
            seen.append(content_key)
            conn.execute(
                "UPDATE archive_runs SET seen_keys = ? WHERE run_id = ?",
                (json.dumps(seen, ensure_ascii=False), run_id),
            )
        return {
            "content_key": content_key,
            "boundary": content_key in boundary,
            "seen_count": len(seen),
        }


def increment_run_new_count(db_file: str, run_id: str | None) -> None:
    if not run_id:
        return
    with connect(db_file) as conn:
        conn.execute(
            "UPDATE archive_runs SET new_count = new_count + 1 WHERE run_id = ? AND status = 'running'",
            (run_id,),
        )


def abort_run(db_file: str, run_id: str, reason: str = "") -> dict:
    init_db(db_file)
    with connect(db_file) as conn:
        run = conn.execute("SELECT * FROM archive_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            raise ValueError("找不到归档任务")
        if run["status"] == "running":
            conn.execute(
                "UPDATE archive_runs SET status = 'incomplete', completed_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (run_id,),
            )
        return {"run_id": run_id, "status": "incomplete", "reason": reason}


def finish_run(
    db_file: str,
    run_id: str,
    *,
    boundary_hit: bool = False,
    reached_end: bool = False,
) -> dict:
    init_db(db_file)
    with connect(db_file) as conn:
        run = conn.execute("SELECT * FROM archive_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise ValueError("找不到正在运行的归档任务")
        seen = json.loads(run["seen_keys"])
        completed = boundary_hit or reached_end or not json.loads(run["boundary_snapshot"])
        status = "complete" if completed else "incomplete"
        conn.execute(
            """UPDATE archive_runs SET status = ?, boundary_hit = ?, reached_end = ?,
               completed_at = CURRENT_TIMESTAMP WHERE run_id = ?""",
            (status, int(boundary_hit), int(reached_end), run_id),
        )
        if completed and seen:
            conn.execute("DELETE FROM archive_frontier WHERE scope = ?", (run["scope"],))
            for rank, content_key in enumerate(seen[:FRONTIER_SIZE]):
                conn.execute(
                    "INSERT INTO archive_frontier (scope, rank, content_key) VALUES (?, ?, ?)",
                    (run["scope"], rank, content_key),
                )
        return {
            "run_id": run_id,
            "status": status,
            "boundary_hit": boundary_hit,
            "reached_end": reached_end,
            "seen_count": len(seen),
            "new_count": run["new_count"],
        }
