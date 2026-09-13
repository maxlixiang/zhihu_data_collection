import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from zhihu_archive.store import (
    abort_run,
    begin_run,
    build_archive_title,
    content_identity_from_url,
    finish_run,
    init_db,
    is_known,
    record_seen,
    register_success,
    resolve_collision_title,
)
from clipboard_bridge import calculate_comment_target, extract_title_and_body, ingest
from zhihu_scraper import should_archive_action


class ArchiveWorkflowTests(unittest.TestCase):
    def test_content_identity(self):
        self.assertEqual(
            content_identity_from_url("https://www.zhihu.com/question/123/answer/456?utm=x"),
            ("answer", "456", "answer:456"),
        )
        self.assertEqual(
            content_identity_from_url("https://zhuanlan.zhihu.com/p/789"),
            ("article", "789", "article:789"),
        )

    def test_frontmatter_and_body(self):
        raw = "---\ntitle: 示例标题\nurl: https://www.zhihu.com/question/1/answer/2\n---\n正文\n"
        title, body, metadata = extract_title_and_body(raw)
        self.assertEqual(title, "示例标题")
        self.assertEqual(body, "正文\n")
        self.assertEqual(metadata["url"], "https://www.zhihu.com/question/1/answer/2")

    def test_plain_copy_removes_title_line(self):
        title, body, _ = extract_title_and_body("示例标题\n\n正文内容")
        self.assertEqual(title, "示例标题")
        self.assertEqual(body, "正文内容\n")

    def test_comment_target(self):
        self.assertEqual(calculate_comment_target(18), 18)
        self.assertEqual(calculate_comment_target(204), 102)
        self.assertEqual(calculate_comment_target(1000), 200)

    def test_supported_activity_actions(self):
        for action in ("赞同了回答", "发布了文章", "发表了想法", "收藏了回答", "喜欢了文章"):
            self.assertTrue(should_archive_action(action), action)
        self.assertFalse(should_archive_action("关注了用户"))

    def test_collision_only_adds_seconds_when_needed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = build_archive_title("2026-09-13 21:08", "同一问题")
            self.assertEqual(base, "[2026-09-13_21-08] 同一问题")
            Path(temp_dir, base + ".md").write_text("existing", encoding="utf-8")
            collision = resolve_collision_title(temp_dir, "2026-09-13 21:08", "同一问题")
            self.assertEqual(collision, "[2026-09-13_21-08-01] 同一问题")

    def test_abort_run_does_not_advance_frontier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(temp_dir, "archive.db")
            run = begin_run(db_file, "scope:test", "playwright")
            record_seen(db_file, run["run_id"], "answer:999")
            result = abort_run(db_file, run["run_id"], "页面异常")
            self.assertEqual(result["status"], "incomplete")

            next_run = begin_run(db_file, "scope:test", "playwright")
            self.assertEqual(next_run["boundary_keys"], [])

    def test_shared_database_and_frontier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(temp_dir, "archive.db")
            init_db(db_file)
            register_success(
                db_file,
                content_key="answer:456",
                source_url="https://www.zhihu.com/question/123/answer/456",
                activity_time="2026-09-13 21:08",
                archive_title="[2026-09-13_21-08] 示例",
                markdown_path="example.md",
                image_dir="",
                source_method="computer_use",
                file_sha256="abc",
            )
            self.assertTrue(is_known(db_file, "answer:456", None))
            first = begin_run(db_file, "profile:test", "computer_use")
            record_seen(db_file, first["run_id"], "answer:456")
            self.assertEqual(finish_run(db_file, first["run_id"])["status"], "complete")

            second = begin_run(db_file, "profile:test", "playwright")
            observed = record_seen(db_file, second["run_id"], "answer:456")
            self.assertTrue(observed["boundary"])
            self.assertEqual(
                finish_run(db_file, second["run_id"], boundary_hit=True)["status"],
                "complete",
            )

    def test_same_title_different_content_id_is_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(temp_dir, "archive.db")
            archive_title = "[2026-09-13_21-08] 同一问题"
            register_success(
                db_file,
                content_key="answer:1",
                source_url="https://www.zhihu.com/question/9/answer/1",
                activity_time="2026-09-13 21:08",
                archive_title=archive_title,
                markdown_path="one.md",
                image_dir="",
                source_method="computer_use",
                file_sha256="abc",
            )
            self.assertFalse(is_known(db_file, "answer:2", archive_title))

    def test_bridge_ingest_writes_expected_layout_and_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "copied.md")
            source.write_text("稳定标题\n\n正文内容\n", encoding="utf-8")
            output_dir = Path(temp_dir, "articles")
            db_file = str(Path(temp_dir, "archive.db"))
            result = ingest(
                Namespace(
                    input_file=str(source),
                    stdin=False,
                    title=None,
                    author="示例作者",
                    activity_action="赞同了回答",
                    published_at="2026-09-12 20:01",
                    source_type="answer",
                    content_url="https://www.zhihu.com/question/10/answer/20",
                    content_key=None,
                    activity_time="2026-09-13 21:08",
                    db_file=db_file,
                    output_dir=str(output_dir),
                    allow_remote_images=False,
                    run_id=None,
                )
            )
            expected = output_dir / "2026" / "09" / "[2026-09-13_21-08] 稳定标题.md"
            self.assertEqual(result["status"], "saved")
            self.assertEqual(Path(result["markdown_path"]), expected)
            saved_text = expected.read_text(encoding="utf-8")
            self.assertTrue(saved_text.startswith("---\n"))
            self.assertIn('title: "稳定标题"', saved_text)
            self.assertIn('author: "示例作者"', saved_text)
            self.assertIn('activity_at: "2026-09-13 21:08"', saved_text)
            self.assertIn('activity_action: "赞同了回答"', saved_text)
            self.assertIn('published_at: "2026-09-12 20:01"', saved_text)
            self.assertIn('source_type: "answer"', saved_text)
            self.assertIn('zhihu_answer_id: "20"', saved_text)
            self.assertIn("\n# 稳定标题\n", saved_text)
            self.assertTrue(is_known(db_file, "answer:20", None))

    def test_bridge_rejects_incomplete_metadata_before_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "copied.md")
            source.write_text("示例标题\n\n正文内容\n", encoding="utf-8")
            output_dir = Path(temp_dir, "articles")
            with self.assertRaisesRegex(ValueError, "author"):
                ingest(
                    Namespace(
                        input_file=str(source),
                        stdin=False,
                        title=None,
                        author=None,
                        activity_action="赞同了回答",
                        published_at="2026-09-12 20:01",
                        source_type="answer",
                        content_url="https://www.zhihu.com/question/10/answer/21",
                        content_key=None,
                        activity_time="2026-09-13 21:08",
                        db_file=str(Path(temp_dir, "archive.db")),
                        output_dir=str(output_dir),
                        allow_remote_images=False,
                        run_id=None,
                    )
                )
            self.assertEqual(list(output_dir.rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
