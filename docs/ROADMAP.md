# 后续改进记录

## 拆分 Playwright 采集模块

状态：待处理，当前不影响使用。

`zhihu_scraper.py` 目前同时包含页面选择器、正文与元数据提取、评论 API、日常增量采集和历史回溯，文件较大，后续维护知乎页面变化时容易扩大改动范围。

计划在真实采集流程稳定、并且具备脱离网络的页面测试夹具后，按以下职责逐步拆分：

```text
playwright_adapter/
├── page_detection.py     登录、风控和页面异常识别
├── activity_parser.py    动态卡片、标题、作者、时间和内容 ID
├── comments.py           回答评论 API 与 Markdown 格式化
├── incremental.py        日常增量扫描和边界控制
└── backfill.py           历史日期区间回溯
```

拆分约束：

- 保持根目录 `python zhihu_scraper.py ...` 命令兼容；
- 不改变共享数据库、文件名、YAML、图片目录或边界语义；
- 先为典型回答、文章、想法、同名回答、登录失效和安全验证页面建立 HTML/结构化测试夹具；
- 每次只迁移一个职责并运行完整回归测试，避免一次性重写。
