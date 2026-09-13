# 本地使用说明

所有命令都在本项目目录执行，无需再进入旧项目。本页是速查说明，整体架构和两种采集方式见项目根目录的 `README.md`。

```powershell
Set-Location "F:\Git上的程序等等\zhihu_data_collection-playwright技术"
```

## 首次登录或登录态失效

```powershell
python init_login.py
```

在 Chromium 中完成登录后，回到终端按 Enter。登录态保存为项目根目录的 `state.json`。

## 日常增量采集

```powershell
python zhihu_scraper.py --limit 30
```

程序默认访问 `https://www.zhihu.com/people/li-xiang-57-76`，显示浏览器窗口，并把 Markdown、图片和数据库都保存在当前项目。正常结束条件是命中上轮边界；如果新内容超过 `--limit`，可再次执行，已保存内容会由共享数据库跳过。

可选参数：

```powershell
python zhihu_scraper.py --limit 30 --no-comments
python zhihu_scraper.py --limit 30 --headless
python zhihu_scraper.py --limit 30 --db-file ".\zhihu_articles.db" --state-file ".\state.json"
```

## 历史回溯

```powershell
python zhihu_scraper.py --backfill-local --start-date 2024-01-01 --end-date 2024-12-31 --limit 0
```

## 旧 Markdown 补表头

先预演并查看报告：

```powershell
python enrich_old_metadata.py --start-date 2023-01-01 --end-date 2026-05-31
```

确认后才原位写入：

```powershell
python enrich_old_metadata.py --start-date 2023-01-01 --end-date 2026-05-31 --apply
```

## 异常处理

出现安全验证、登录页、找不到动态卡片或连续滚动无增长时，采集会停止，并在项目根目录生成 `zhihu_last_*.png`。处理验证或重新生成登录态后再运行，不要在异常页面上连续重试。
