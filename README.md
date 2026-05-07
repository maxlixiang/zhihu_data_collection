# Zhihu Activity Local Archiver

本工具用于本地归档任意知乎个人主页动态、回答、专栏、文章等出现在个人主页选项卡上的文章。
本工具用于本地归档任意知乎个人主页动态、回答、专栏、文章等出现在个人主页选项卡上的文章，比如：
https://www.zhihu.com/people/yuanmu96/answers # 回答
https://www.zhihu.com/people/yuanmu96   # 主页动态
https://www.zhihu.com/people/yuanmu96/asks # 提问
https://www.zhihu.com/people/yuanmu96/posts #文章
https://www.zhihu.com/people/yuanmu96/pins # 想法

至于专栏和收藏，则需要手动打开专栏页面和收藏页面，找到对应的专栏和收藏URL以后，也可以实现抓取。比如：
https://www.zhihu.com/collection/20441812 #某收藏夹

以上链接的特点是都可以在一个网页中滚动查看所有内容，不需要打开新网页，只要满足这个条件的知乎页面都可以被抓取。

程序只在目标URL的动态流中滚动、点击“阅读全文/展开全文/阅读原文”、提取当前卡片正文，并保存为 Markdown；不会打开文章或回答详情页。
## 功能

- 支持输入任意可滚动的知乎列表页 URL。
- 按动态发生时间过滤，例如只抓 `2020-01-01` 到 `2024-02-14`。
- 在主页动态卡片内展开正文并保存 Markdown。
- Markdown 文件名格式为 `[YYYY-MM-DD_HH-MM] 标题.md`。
- 自动下载正文图片到同名目录，并把 Markdown 图片链接改为本地相对路径。
- 对回答动态可抓取第一页精选评论。
- 使用 SQLite 去重，重复运行会跳过已保存文章。

## 文件结构

```text
zhihu_data_collection/
├── zhihu_scraper.py      # 本地归档主脚本
├── init_login.py         # 生成知乎登录态 state.json
├── state.json            # Playwright 登录态
├── zhihu_articles.db     # SQLite 去重数据库
├── requirements.txt
└── data/
    └── articles/         # 默认 Markdown 输出目录
```

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 登录

首次使用需要生成 `state.json`：

```powershell
python init_login.py
```

按浏览器提示完成知乎登录。后续抓取会复用 `state.json`。

## 快速测试

抓取目标主页最新 5 条符合条件的动态：

```powershell
python zhihu_scraper.py --backfill-local --url "https://www.zhihu.com/people/li-xiang-57-76" --start-date 2026-01-01 --end-date 2026-12-31 --limit 5 --max-scrolls 50
```

输出文件默认保存在：

```text
data/articles/
```

也可以指定输出目录：

```powershell
python zhihu_scraper.py --backfill-local --url "https://www.zhihu.com/people/li-xiang-57-76" --start-date 2026-01-01 --end-date 2026-12-31 --limit 5 --output-dir "D:\zhihu_exports\li-xiang"
```

## 历史回溯

示例：抓取某个主页在 `2020-01-01` 到 `2024-02-14` 之间产生的动态。

```powershell
python zhihu_scraper.py --backfill-local --url "https://www.zhihu.com/people/li-xiang-57-76" --start-date 2020-01-01 --end-date 2024-02-14 --limit 0 --max-scrolls 10000
```

## 加速参数

程序分为两个阶段：

- `seek`：快速滚动到目标结束日期附近，只检查页面底部少量动态的时间。
- `collect`：进入目标时间段后，逐条展开动态并保存 Markdown。

常用加速参数：

```powershell
python zhihu_scraper.py --backfill-local `
  --url "https://www.zhihu.com/people/li-xiang-57-76" `
  --start-date 2020-01-01 `
  --end-date 2024-02-14 `
  --limit 0 `
  --max-scrolls 10000 `
  --seek-delay-min 0.3 `
  --seek-delay-max 0.8 `
  --collect-delay-min 0.8 `
  --collect-delay-max 1.5 `
  --seek-tail-count 30 `
  --seek-scroll-burst 3
```

参数说明：

- `--seek-tail-count`：seek 阶段每轮只检查最后 N 个动态卡片，默认 `30`。
- `--seek-scroll-burst`：seek 阶段每轮连续滚动次数，默认 `3`。
- `--seek-delay-min/max`：seek 阶段滚动后的等待范围。
- `--collect-delay-min/max`：collect 阶段滚动后的等待范围。

## 常用参数

```text
--url               知乎可滚动列表页 URL
--start-date        动态发生日期起点
--end-date          动态发生日期终点
--limit             最多保存多少篇；0 表示不限
--max-scrolls       最大滚动轮数
--output-dir        Markdown 输出目录
--debug-comments    只测试第一条动态的正文和评论
```

## 注意事项

- 时间过滤依据是“主页动态发生时间”，不是文章发布日期。
- 程序只在主页动态流操作，不打开新的详情页。
- 抓取大量历史动态会耗时较长，建议先用 `--limit 5` 测试。
- 如果同一个输出目录用于多个知乎页面，建议为每个页面指定不同 `--output-dir`。
- 本工具仅供个人学习和数据备份使用，请控制频率，避免对网站造成压力。
