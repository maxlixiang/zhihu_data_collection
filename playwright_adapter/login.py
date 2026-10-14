"""Interactive login preflight shared with the incremental collector."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .console import safe_print as print
from .page_detection import detect_blocked_zhihu_page, safe_page_snapshot


class LoginPreflightError(RuntimeError):
    """No archive database or article writes may follow this startup failure."""


def create_login_context(browser, config, *, headed, interactive):
    state = None
    try:
        with open(config.state_file, encoding="utf-8-sig") as handle:
            state = json.load(handle)
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
            raise ValueError("登录态格式不完整")
    except (OSError, ValueError):
        if not (headed and interactive):
            raise LoginPreflightError("登录凭证缺失或损坏，请运行 python init_login.py，或使用 --interactive-login")
        print("[登录检查] 登录凭证缺失或损坏，将在当前浏览器中重新登录。")
        state = None
    context = browser.new_context(viewport={"width": 1366, "height": 768},
                                  storage_state=state, timezone_id="Asia/Shanghai")
    return context, state is None


def _visit(page, config, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
        time.sleep(4)
    except Exception as exc:
        safe_page_snapshot(page, "login-check-network-error", config.project_dir)
        raise LoginPreflightError("登录检查页面加载失败，请检查网络后重试；尚未下载或分类。") from exc


def _check(page, config):
    reason = detect_blocked_zhihu_page(page)
    if "安全验证" in reason:
        safe_page_snapshot(page, "login-check-security", config.project_dir)
        return False
    if reason:
        return False
    try:
        page.locator(".List-item").first.wait_for(state="attached", timeout=10000)
    except Exception as exc:
        safe_page_snapshot(page, "login-check-no-list", config.project_dir)
        raise LoginPreflightError("目标页面没有加载动态列表，可能是页面异常或结构变化；尚未下载或分类。") from exc
    return True


def _save_state(context, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".login-state-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        context.storage_state(path=str(temporary))
        with temporary.open(encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
            raise ValueError("登录态保存结果不完整")
        os.replace(temporary, target)
    finally:
        # 只删除本次创建的一个明确临时文件。
        temporary.unlink(missing_ok=True)


def ensure_logged_in(page, context, config, target_url, *, headed, interactive, prompt=None, login_needed=False):
    print("[登录检查] 正在验证知乎登录和目标动态列表；下载、备份及分类尚未开始。")
    _visit(page, config, "https://www.zhihu.com/signin" if login_needed else target_url)
    if _check(page, config):
        print("[登录检查] 验证通过，继续下载。")
        return
    safe_page_snapshot(page, "blocked-or-login", config.project_dir)
    if not (headed and interactive):
        reason = detect_blocked_zhihu_page(page)
        label = "知乎要求安全验证" if "安全验证" in reason else "知乎登录已失效"
        raise LoginPreflightError(label + "，请在可见浏览器中人工登录/验证后重试。")
    if prompt is None:
        if not sys.stdin.isatty():
            raise LoginPreflightError("当前终端不能交互，请单独运行 python init_login.py 完成登录/验证后重试。")
        prompt = input
    while True:
        reason = detect_blocked_zhihu_page(page)
        if "安全验证" in reason:
            print("\n[等待人工验证] 知乎要求安全验证。浏览器将保持打开，请在网页中手动完成。")
        else:
            print("\n[等待人工登录] 知乎尚未登录。浏览器将保持打开，请在网页中完成登录。")
        print("下载、追加备份和分类均已暂停；没有登录等待倒计时，不会自动关闭窗口。")
        print("操作完成后回到PowerShell按 Enter 重新检查；输入 q 可取消。")
        try:
            answer = prompt("完成登录/验证后按 Enter 继续：").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise LoginPreflightError("登录/验证已取消，未进入下载、备份或分类。") from exc
        if answer.lower() == "q":
            raise LoginPreflightError("登录/验证已取消，未进入下载、备份或分类。")
        if answer:
            print("请输入 q 取消，或直接按 Enter 重新检查；窗口继续保留。")
            continue
        if page.is_closed():
            raise LoginPreflightError("浏览器页面已由用户关闭，本次停止。请重新运行。")
        print("[登录检查] 正在重新验证，请稍候……")
        _visit(page, config, target_url)
        if _check(page, config):
            break
        print("[登录检查] 仍需登录或人工验证，继续等待；尚未开始下载、备份或分类。")
    try:
        _save_state(context, config.state_file)
    except Exception as exc:
        raise LoginPreflightError("登录验证通过，但凭证保存失败，本次停止；请检查目录权限。") from exc
    print("[登录检查] 登录成功，新凭证已保存；继续使用当前浏览器下载。")
