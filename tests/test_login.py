import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from playwright_adapter.login import LoginPreflightError, create_login_context, ensure_logged_in
from playwright_adapter.incremental import run_incremental


class Page:
    def __init__(self, urls, *, cards=True, network=False):
        self.urls = iter(urls)
        self.url = "about:blank"
        self.cards = cards
        self.network = network
        self.visits = []
    def goto(self, url, **kwargs):
        self.visits.append(url)
        if self.network: raise RuntimeError("offline")
        self.url = next(self.urls)
    def is_closed(self): return False
    def title(self): return "知乎"
    def locator(self, selector):
        locator = Mock()
        locator.first = locator
        if not self.cards: locator.wait_for.side_effect = RuntimeError("no cards")
        return locator


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="zhihu-login-test-"))
        self.state = self.root / "state.json"
        self.state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        self.config = SimpleNamespace(state_file=str(self.state), project_dir=str(self.root), page_load_timeout_ms=100)
        self.context = Mock()
        self.context.storage_state.side_effect = lambda **kwargs: Path(kwargs['path']).write_text(
            '{"cookies": [], "origins": []}', encoding="utf-8")
        self.target = "https://www.zhihu.com/people/test"
        self.sleep = patch("playwright_adapter.login.time.sleep").start()
        self.snapshot = patch("playwright_adapter.login.safe_page_snapshot").start()
        self.addCleanup(patch.stopall)

    def check(self, page, **kwargs):
        return ensure_logged_in(page, self.context, self.config, self.target,
            headed=kwargs.pop("headed", True), interactive=kwargs.pop("interactive", True), **kwargs)

    def test_valid_login_no_prompt_no_state_overwrite(self):
        before = self.state.read_bytes(); prompt = Mock()
        self.check(Page([self.target]), prompt=prompt)
        prompt.assert_not_called(); self.context.storage_state.assert_not_called()
        self.assertEqual(before, self.state.read_bytes())

    def test_expired_login_revalidated_and_saved_in_same_context(self):
        page = Page(["https://www.zhihu.com/signin", self.target]); prompt = Mock(return_value="")
        self.check(page, prompt=prompt)
        self.assertEqual(page.visits, [self.target, self.target])
        prompt.assert_called_once(); self.context.storage_state.assert_called_once()
        self.assertEqual(json.loads(self.state.read_text()), {"cookies": [], "origins": []})

    def test_missing_state_opens_login_page_before_target(self):
        page=Page(["https://www.zhihu.com/signin",self.target])
        self.check(page,prompt=lambda msg:"",login_needed=True)
        self.assertEqual(page.visits,["https://www.zhihu.com/signin",self.target])
        self.context.storage_state.assert_called_once()

    def test_enter_before_login_keeps_waiting_until_success(self):
        before=self.state.read_bytes()
        page=Page(["https://www.zhihu.com/signin"]*2+[self.target])
        def prompt(msg):
            self.assertEqual(self.state.read_bytes(),before)
            self.context.storage_state.assert_not_called()
            return ""
        self.check(page,prompt=prompt)
        self.assertEqual(len(page.visits),3)
        self.context.storage_state.assert_called_once()

    def test_cancel_or_eof_leaves_state_unchanged(self):
        for prompt in (lambda msg: "q", Mock(side_effect=EOFError), Mock(side_effect=KeyboardInterrupt)):
            with self.assertRaisesRegex(LoginPreflightError, "取消"):
                self.check(Page(["https://www.zhihu.com/signin"]), prompt=prompt)
        self.context.storage_state.assert_not_called()

    def test_security_waits_for_manual_completion_then_same_context_continues(self):
        page=Page(["https://www.zhihu.com/account/unhuman",self.target])
        prompt=Mock(return_value="")
        self.check(page,prompt=prompt)
        prompt.assert_called_once()
        self.assertEqual(page.visits,[self.target,self.target])
        self.context.storage_state.assert_called_once()

    def test_repeated_security_stays_open_until_explicit_cancel(self):
        before=self.state.read_bytes()
        page=Page(["https://www.zhihu.com/account/unhuman"]*2)
        prompt=Mock(side_effect=["","q"])
        with self.assertRaisesRegex(LoginPreflightError,"取消"):
            self.check(page,prompt=prompt)
        self.assertEqual(prompt.call_count,2)
        self.context.storage_state.assert_not_called()
        self.assertEqual(self.state.read_bytes(),before)

    def test_security_headless_stops_without_prompt(self):
        prompt=Mock()
        with self.assertRaisesRegex(LoginPreflightError,"安全验证"):
            self.check(Page(["https://www.zhihu.com/account/unhuman"]),headed=False,prompt=prompt)
        prompt.assert_not_called()

    def test_user_closed_browser_stops(self):
        page=Page(["https://www.zhihu.com/signin"])
        page.is_closed=lambda:True
        with self.assertRaisesRegex(LoginPreflightError,"用户关闭"):
            self.check(page,prompt=lambda msg:"")
        self.context.storage_state.assert_not_called()

    def test_non_q_text_does_not_close_window(self):
        page=Page(["https://www.zhihu.com/signin",self.target])
        prompt=Mock(side_effect=["还没登录",""])
        self.check(page,prompt=prompt)
        self.assertEqual(prompt.call_count,2)

    def test_network_and_structure_failures_do_not_prompt(self):
        for page, reason in ((Page([],network=True),"页面加载失败"),(Page([self.target],cards=False),"动态列表")):
            prompt=Mock()
            with self.assertRaisesRegex(LoginPreflightError, reason): self.check(page,prompt=prompt)
            prompt.assert_not_called()

    def test_headless_and_noninteractive_stop(self):
        for options in ({"headed":False},{"interactive":False}):
            with self.assertRaisesRegex(LoginPreflightError,"登录已失效"):
                self.check(Page(["https://www.zhihu.com/signin"]),**options)
        with patch("playwright_adapter.login.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(LoginPreflightError,"不能交互"):
                self.check(Page(["https://www.zhihu.com/signin"]))

    def test_corrupt_and_missing_state_use_empty_interactive_context(self):
        browser=Mock()
        for value in ("invalid", "{}"):
            self.state.write_text(value,encoding="utf-8")
            create_login_context(browser,self.config,headed=True,interactive=True)
            self.assertIsNone(browser.new_context.call_args.kwargs["storage_state"])
            self.assertEqual(self.state.read_text(),value)
        cfg=SimpleNamespace(state_file=str(self.root/"absent.json"))
        create_login_context(browser,cfg,headed=True,interactive=True)
        with self.assertRaises(LoginPreflightError): create_login_context(browser,cfg,headed=False,interactive=True)

    def test_state_save_failure_stops(self):
        self.context.storage_state.side_effect=OSError("denied")
        with self.assertRaisesRegex(LoginPreflightError,"保存失败"):
            self.check(Page(["https://www.zhihu.com/signin",self.target]),prompt=lambda msg: "")
        self.assertEqual(json.loads(self.state.read_text()),{"cookies":[],"origins":[]})

    def test_preflight_failure_does_not_initialize_archive_database(self):
        browser=Mock(); manager=Mock()
        manager.__enter__=Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=Mock(return_value=browser))))
        manager.__exit__=Mock(return_value=False)
        with patch("playwright_adapter.incremental.sync_playwright",return_value=manager), \
             patch("playwright_adapter.incremental.require_markdownify"), \
             patch("playwright_adapter.incremental.create_login_context",return_value=(self.context,False)), \
             patch("playwright_adapter.incremental.ensure_logged_in",side_effect=LoginPreflightError("cancel")), \
             patch("playwright_adapter.incremental.init_db") as initialize, \
             patch("playwright_adapter.incremental.archive_store.begin_run") as begin:
            cfg=SimpleNamespace(**vars(self.config),default_url=self.target)
            with self.assertRaises(LoginPreflightError):run_incremental(cfg,interactive_login=True)
            initialize.assert_not_called();begin.assert_not_called();browser.close.assert_called_once()
