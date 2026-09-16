import os
import json
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
from playwright_adapter.config import RuntimeConfig
from playwright_adapter.exporter import export_activity_item
from playwright_adapter.page_detection import detect_blocked_zhihu_page
from zhihu_scraper import (
    DEFAULT_HOT_COMMENT_LIMIT,
    DEFAULT_REPLY_LIMIT_PER_COMMENT,
    DEFAULT_ROOT_COMMENT_LIMIT,
    fetch_first_page_comments_via_api,
    format_comments_markdown,
    parse_activity_time,
    resolve_incremental_new_limit,
    should_archive_action,
)


class FakeCommentResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload

    def text(self):
        return ""


class FakeCommentRequest:
    def __init__(self, root_comments, replies_by_root):
        self.root_comments = root_comments
        self.replies_by_root = replies_by_root
        self.urls = []

    def get(self, url, headers=None):
        self.urls.append(url)
        if "/root_comments" in url:
            query = parse_qs(urlparse(url).query)
            limit = int(query["limit"][0])
            offset = int(query["offset"][0])
            return FakeCommentResponse(
                {
                    "data": self.root_comments[offset : offset + limit],
                    "paging": {"is_end": offset + limit >= len(self.root_comments)},
                }
            )
        root_id = url.split("/comments/", 1)[1].split("/", 1)[0]
        return FakeCommentResponse(
            {"data": self.replies_by_root.get(root_id, []), "paging": {"is_end": True}}
        )


class FakeCommentPage:
    def __init__(self, root_comments, replies_by_root):
        self.url = "https://www.zhihu.com/answer/123"
        self.context = type("Context", (), {})()
        self.context.request = FakeCommentRequest(root_comments, replies_by_root)
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeExportLocator:
    def __init__(self, *, count=0, html=""):
        self._count = count
        self._html = html

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_html(self):
        return self._html

    def evaluate(self, script):
        return None


class FakeExportItem:
    def scroll_into_view_if_needed(self):
        return None

    def locator(self, selector):
        if "button:has-text" in selector:
            return FakeExportLocator(count=0)
        if ".RichContent-inner" in selector:
            return FakeExportLocator(count=1, html="<p>正文内容</p>")
        return FakeExportLocator(count=0)

    def evaluate(self, script):
        return {
            "author": "测试作者",
            "published_at": "发布于 2026-09-16 20:00",
            "edited_at": "",
            "source_url": "https://www.zhihu.com/question/1/answer/2",
            "source_type": "answer",
            "answer_id": "2",
            "date_created": "",
            "date_modified": "",
        }


class FakeDiagnosticPage:
    def __init__(self, title, url):
        self._title = title
        self.url = url

    def title(self):
        return self._title


class ArchiveWorkflowTests(unittest.TestCase):
    def test_page_detection_for_login_and_security_challenge(self):
        login = detect_blocked_zhihu_page(
            FakeDiagnosticPage("登录 - 知乎", "https://www.zhihu.com/signin")
        )
        blocked = detect_blocked_zhihu_page(
            FakeDiagnosticPage("安全验证", "https://www.zhihu.com/account/unhuman")
        )
        normal = detect_blocked_zhihu_page(
            FakeDiagnosticPage("个人主页", "https://www.zhihu.com/people/test")
        )
        self.assertIn("登录态可能失效", login)
        self.assertIn("安全验证", blocked)
        self.assertEqual(normal, "")

    def test_structured_activity_fixtures(self):
        fixture_path = Path(__file__).parent / "fixtures" / "activity_cases.json"
        cases = json.loads(fixture_path.read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(should_archive_action(case["action"]), case["name"] != "unsupported")
                activity_at, time_str = parse_activity_time(case["meta_text"])
                self.assertIsNotNone(activity_at)
                self.assertTrue(time_str.startswith("[2026-09-"))
                identity = content_identity_from_url(case["url"])
                self.assertEqual(identity, (case["content_type"], case["content_id"], case["content_key"]))

    def test_shared_exporter_writes_expected_markdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = RuntimeConfig(
                project_dir=temp_dir,
                default_url="https://www.zhihu.com/people/test",
                archive_root_dir=temp_dir,
                local_archive_root_dir=temp_dir,
                db_file=str(Path(temp_dir, "archive.db")),
                state_file=str(Path(temp_dir, "state.json")),
            )
            identity = {
                "content_type": "answer",
                "content_id": "2",
                "content_key": "answer:2",
                "url": "https://www.zhihu.com/question/1/answer/2",
            }
            with patch("playwright_adapter.exporter.time.sleep"):
                path = export_activity_item(
                    config,
                    page=None,
                    item=FakeExportItem(),
                    title="测试标题",
                    clean_title_str="[2026-09-17_08-30] 测试标题",
                    save_dir=temp_dir,
                    activity_dt=datetime(2026, 9, 17, 8, 30),
                    action_text="赞同了回答",
                    content_identity=identity,
                    include_comments=False,
                )
            saved = Path(path).read_text(encoding="utf-8")
            self.assertIn('title: "测试标题"', saved)
            self.assertIn('author: "测试作者"', saved)
            self.assertIn('zhihu_answer_id: "2"', saved)
            self.assertIn("# 测试标题", saved)
            self.assertIn("正文内容", saved)

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

    def test_program_comment_limits_and_nested_replies(self):
        self.assertEqual(DEFAULT_ROOT_COMMENT_LIMIT, 30)
        self.assertEqual(DEFAULT_HOT_COMMENT_LIMIT, 2)
        self.assertEqual(DEFAULT_REPLY_LIMIT_PER_COMMENT, 10)

        def api_comment(comment_id, author, content, child_count=0, reply_to=""):
            item = {
                "id": comment_id,
                "author": {"member": {"name": author}},
                "content": content,
                "child_comment_count": child_count,
                "child_comments": [],
            }
            if reply_to:
                item["reply_to_author"] = {"member": {"name": reply_to}}
            return item

        roots = [
            api_comment("root-1", "热门甲", "顶层甲", child_count=12),
            api_comment("root-2", "热门乙", "顶层乙", child_count=11),
            api_comment("root-3", "普通丙", "顶层丙", child_count=8),
        ] + [
            api_comment(f"root-{i}", f"普通{i}", f"顶层{i}")
            for i in range(4, 36)
        ]
        replies = {
            "root-1": [
                api_comment(f"reply-1-{i}", f"回复甲{i}", f"内容甲{i}", reply_to="热门甲")
                for i in range(12)
            ],
            "root-2": [
                api_comment(f"reply-2-{i}", f"回复乙{i}", f"内容乙{i}", reply_to="热门乙")
                for i in range(11)
            ],
            "root-3": [
                api_comment(f"reply-3-{i}", f"回复丙{i}", f"内容丙{i}", reply_to="普通丙")
                for i in range(8)
            ],
        }
        page = FakeCommentPage(roots, replies)
        comments = fetch_first_page_comments_via_api(page, "123")

        self.assertEqual(len(comments), 30)
        self.assertEqual(len(comments[0]["replies"]), 10)
        self.assertEqual(len(comments[1]["replies"]), 10)
        self.assertEqual(comments[2]["replies"], [])
        root_urls = [url for url in page.context.request.urls if "/root_comments" in url]
        self.assertEqual(len(root_urls), 2)
        self.assertIn("limit=20&offset=0", root_urls[0])
        self.assertIn("limit=10&offset=20", root_urls[1])
        self.assertEqual(len(page.context.request.urls), 4)
        self.assertNotIn("root-3/child_comments", "\n".join(page.context.request.urls))

        rendered = format_comments_markdown(comments)
        self.assertIn("最多 30 条", rendered)
        self.assertIn("↳ **回复甲0** 回复 **热门甲**", rendered)
        self.assertNotIn("回复甲10", rendered)
        self.assertNotIn("回复丙1", rendered)

    def test_supported_activity_actions(self):
        for action in ("赞同了回答", "发布了文章", "发表了想法", "收藏了回答", "喜欢了文章"):
            self.assertTrue(should_archive_action(action), action)
        self.assertFalse(should_archive_action("关注了用户"))

    def test_incremental_limit_modes(self):
        self.assertEqual(resolve_incremental_new_limit(30), 30)
        self.assertEqual(resolve_incremental_new_limit(30, True, 200), 200)
        self.assertEqual(resolve_incremental_new_limit(30, True, 200, False), 30)
        with self.assertRaisesRegex(ValueError, "--max-new 不能小于 --limit"):
            resolve_incremental_new_limit(30, True, 20)
        with self.assertRaisesRegex(ValueError, "--limit 必须大于 0"):
            resolve_incremental_new_limit(0)

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
                    expected_title="稳定标题",
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
                        expected_title="示例标题",
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

    def test_bridge_rejects_stale_clipboard_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "copied.md")
            source.write_text("旧剪贴板标题\n\n旧内容\n", encoding="utf-8")
            output_dir = Path(temp_dir, "articles")
            with self.assertRaisesRegex(ValueError, "剪贴板标题与当前页面不匹配"):
                ingest(
                    Namespace(
                        input_file=str(source),
                        stdin=False,
                        title=None,
                        expected_title="当前知乎标题",
                        author="示例作者",
                        activity_action="赞同了回答",
                        published_at="2026-09-12 20:01",
                        source_type="answer",
                        content_url="https://www.zhihu.com/question/10/answer/22",
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
