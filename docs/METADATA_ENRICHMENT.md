# 旧 Markdown 元数据补全

项目根目录的 `enrich_old_metadata.py` 用个人主页动态记录匹配旧 Markdown，并补齐与新采集文件一致的 YAML 表头。实现位于 `maintenance/metadata_enrichment.py`，支持断点进度、匹配报告、歧义清单和写入错误记录。

默认行为是 dry-run，不会修改文章：

```powershell
python enrich_old_metadata.py --start-date 2023-01-01 --end-date 2026-05-31
```

报告默认写入当前项目的 `metadata_reports`。确认匹配结果后，用完全相同的参数增加 `--apply`：

```powershell
python enrich_old_metadata.py --start-date 2023-01-01 --end-date 2026-05-31 --apply
```

常用覆盖参数：

```text
--archive-root   待补全 Markdown 根目录，默认 data/articles
--state-file     Playwright 登录态，默认 state.json
--report-dir     报告目录，默认 metadata_reports
--url            知乎个人主页或 activities URL
--headless       隐藏浏览器；默认显示
```

建议始终先 dry-run。遇到同名、同分钟等歧义记录时，脚本不会盲目覆盖，而会写入报告供人工确认。
