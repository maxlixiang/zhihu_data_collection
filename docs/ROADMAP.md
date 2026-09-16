# 后续改进记录

## 拆分 Playwright 采集模块

状态：已完成。

`zhihu_scraper.py` 已缩减为命令行入口和旧 API 兼容外壳。页面检测、动态解析、评论、单条导出、日常增量和历史回溯已拆分到：

```text
playwright_adapter/
├── config.py             路径和运行配置
├── page_detection.py     登录、风控和页面异常识别
├── activity_parser.py    动态卡片、标题、作者、时间和内容 ID
├── comments.py           回答评论 API 与 Markdown 格式化
├── exporter.py           单条动态导出与入库桥接
├── incremental.py        日常增量扫描和边界控制
├── backfill.py           历史日期区间回溯
└── debug.py              只读评论调试
```

后续维护约束：

- 保持根目录 `python zhihu_scraper.py ...` 命令兼容；
- 不改变共享数据库、文件名、YAML、图片目录或边界语义；
- 结构化测试夹具放在 `tests/fixtures/`，页面变化时先更新夹具和测试；
- 根入口只做兼容转发，不再新增页面选择器或采集逻辑。
