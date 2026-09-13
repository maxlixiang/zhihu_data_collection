import argparse
import os

from playwright.sync_api import sync_playwright


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(description="初始化知乎 Playwright 登录凭证")
    parser.add_argument(
        "--state-file",
        default=os.path.join(PROJECT_DIR, "state.json"),
        help="登录态保存路径，默认当前项目的 state.json",
    )
    args = parser.parse_args()
    state_file = os.path.abspath(args.state_file)

    print("=" * 50)
    print("🚀 初始化知乎登录凭证")
    print("=" * 50)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded", timeout=30000)

        print("⏳ 请在浏览器中完成扫码或密码登录。")
        input("✅ 确认已登录并能正常浏览知乎后，回到此终端按 Enter 保存凭证...")

        context.storage_state(path=state_file)
        print(f"✅ 登录凭证已保存: {state_file}")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
