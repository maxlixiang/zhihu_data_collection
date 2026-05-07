from playwright.sync_api import sync_playwright
import time

print("="*50)
print("🚀 初始化知乎登录凭证")
print("="*50)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False) # 必须显示界面让你扫码
    context = browser.new_context(viewport={'width': 1366, 'height': 768})
    page = context.new_page()

    page.goto("https://www.zhihu.com")
    print("⏳ 请在弹出的浏览器中，利用这 60 秒时间扫码或密码登录知乎！")
    print("⏳ 登录成功后请耐心等待倒计时结束...")
    
    # 给足够的时间让你扫码
    time.sleep(60) 

    # 保存登录状态到 state.json
    context.storage_state(path="state.json")
    print("✅ 登录凭证已成功保存为 state.json！")
    browser.close()