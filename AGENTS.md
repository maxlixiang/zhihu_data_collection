# AGENTS.md

本文件面向在此仓库工作的编程代理。用户使用说明见 `README.md`。

## 项目目标

这是一个 Windows 本地知乎个人归档工具。系统有两个采集入口，但只有一套保存规则：

- `zhihu_scraper.py`：Playwright 主页动态流采集。
- `clipboard_bridge.py`：Computer Use + 油猴插件的剪贴板入库。
- `zhihu_archive/`：两种方式共享的文件、图片、SQLite、稳定 ID、边界和元数据逻辑。

不要重新引入 Docker、VPS、Telegram 机器人或 GitHub 自动推送。用户只维护本地版本。

## 目录职责

```text
zhihu_scraper.py                       Playwright CLI 与旧 API 兼容外壳
clipboard_bridge.py                    剪贴板桥接 CLI
init_login.py                          Playwright 登录态初始化
enrich_old_metadata.py                 元数据补全 CLI 包装
playwright_adapter/config.py           运行路径与显式配置
playwright_adapter/page_detection.py   登录、风控与页面异常
playwright_adapter/activity_parser.py  动态卡片、元数据与稳定 ID
playwright_adapter/comments.py         评论 API、回复与 Markdown
playwright_adapter/exporter.py         单条动态导出和入库桥接
playwright_adapter/incremental.py      日常增量扫描与边界
playwright_adapter/backfill.py         历史回溯
playwright_adapter/debug.py            只读评论调试
zhihu_archive/content.py               YAML、文件名、图片本地化
zhihu_archive/store.py                 数据库、去重、运行状态、边界
maintenance/metadata_enrichment.py     旧 Markdown 匹配和补全
docs/                                  用户操作细节
tests/                                 不联网的回归测试
legacy_reference/                      历史参考，禁止作为运行时依赖
data/articles/                         用户归档数据
metadata_reports/                      元数据补全报告
zhihu_articles.db                      共享数据库
state.json                             登录态
```

根目录四个 Python 文件是稳定入口。内部重构不得无故改变以下命令：

```powershell
python zhihu_scraper.py --limit 30
python zhihu_scraper.py --limit 30 --continue-to-boundary --max-new 200
python clipboard_bridge.py --help
python init_login.py
python enrich_old_metadata.py --help
```

## 必须保持的数据契约

1. 两种方式默认使用项目根目录同一个 `zhihu_articles.db`。
2. 两种方式默认保存到 `data/articles/YYYY/MM/`。
3. 去重优先使用 `answer:<id>` 或 `article:<id>`，不得退回到仅按标题去重。
4. 同一问题的不同回答可以同名，必须视为不同内容。
5. 普通文件名为 `[YYYY-MM-DD_HH-MM] 标题.md`；仅真实冲突时追加 `-01`、`-02` 等伪秒。
6. 图片目录与 Markdown 同名，正文内使用相对图片路径。
7. YAML 字段保持兼容：`title`、`author`、`activity_at`、`activity_action`、`published_at`、`source_url`、`source_type`、回答对应的 `zhihu_answer_id`。
8. 成功写入文件后才能登记成功记录，失败不得伪装为已归档。
9. 只有命中旧边界或明确到达列表末尾才能推进 `archive_frontier`。数量上限、验证码、登录失效和页面异常必须标记为不完整。
10. Playwright 与桥接通道必须识别对方保存的内容。

## Computer Use 规则

- 不修改用户日常使用的油猴插件；自动化专用逻辑留在桥接程序。
- 默认从 `https://www.zhihu.com/people/li-xiang-57-76` 最新动态开始。
- 评论不超过 30 条时尽量全部保存；否则约保存一半，最多 200 条。
- 默认只展开 3 条热门评论下的全部回复。
- 必须检查到上次采集的重复项才算正常完成。
- 遇到验证码、登录页、访问异常、插件变化或连续两次无法识别同一步骤时停止。
- UI 顺序以 `docs/COMPUTER_USE_WORKFLOW.md` 为准。
- 桥接写入前必须校验完整元数据；缺字段时返回错误，不生成 Markdown、不登记数据库。

## Playwright 与风控

- 默认显示浏览器；命令带 `--headless` 时才隐藏。
- 回答默认最多保存 30 条顶层评论；只获取默认排序前 2 条热门评论的回复，每条最多 10 条，总回复数不得超过 20。
- 顶层评论接口单页不得请求超过 20 条；30 条必须使用串行分页累计，并按评论 ID 去重。
- 回复请求必须串行并优先复用顶层评论返回的内嵌回复；单条回复线程失败时保留正文、顶层评论和已有内嵌回复。
- `--limit` 在普通模式下是硬上限；只有显式使用 `--continue-to-boundary` 时才是软限制。
- 边界优先模式必须同时受 `--max-new` 硬上限保护；到达上限仍未命中旧边界时必须以不完整状态结束，不得推进 `archive_frontier`。
- 没有旧边界的首轮采集不得扩展到 `--max-new`，必须仍以 `--limit` 建立初始边界。
- 继续到边界时复用当前浏览器会话并保持串行，不重新从顶部扫描，不增加并发。
- 保留随机短等待，不增加高并发、并行分页或激进重试。
- 检测到安全验证、登录页、零动态卡片或滚动停滞时立即停止，并保留 `zhihu_last_*.png`。
- 不尝试绕过验证码、平台访问限制或登录验证。
- 调试优先使用 `--limit 1` 或 `--debug-comments`。

## 数据与文件安全

- `data/articles/`、`zhihu_articles.db`、`state.json` 和 `metadata_reports/` 是用户数据或运行状态，未经明确请求不得删除、清空、覆盖或迁移。
- 禁止批量删除文件或目录；禁止 `del /s`、`rd /s`、`rmdir /s`、`rm -rf`、`Remove-Item -Recurse`。
- 必须删除时，一次只处理一个已核实的明确文件路径。
- 不批量重写现有 Markdown。元数据补全必须先 dry-run、检查报告，再经用户授权使用 `--apply`。
- 修改中文文件必须用 UTF-8，结束前以 UTF-8 回读并确认没有 `U+FFFD` 乱码。
- 不修改 `legacy_reference/`，除非用户明确要求清理历史参考。

## 开发要求

- 内容格式或路径规则放在 `zhihu_archive/content.py`。
- 数据库、去重或边界规则放在 `zhihu_archive/store.py`。
- 采集入口只负责获取原始内容并调用共享核心，不复制保存逻辑。
- 新增数据库字段要向后兼容现有数据库，不破坏旧 `articles` 表。
- 文件写入与数据库登记应保持“文件成功后入库”的顺序。
- 修改行为后同步更新 `README.md`、相关 `docs/` 和测试。
- `zhihu_scraper.py` 必须保持为稳定兼容入口；新的 Playwright 逻辑放入对应 `playwright_adapter/` 模块，不得再堆回入口文件。
- 典型动态结构化夹具位于 `tests/fixtures/`；页面选择器变更时应同步增加或更新夹具和离线测试。

## 验证

至少执行：

```powershell
python -m compileall -q zhihu_scraper.py clipboard_bridge.py init_login.py enrich_old_metadata.py zhihu_archive playwright_adapter maintenance
python -m unittest discover -s tests -v
python zhihu_scraper.py --help
python clipboard_bridge.py --help
python enrich_old_metadata.py --help
```

测试默认不得联网、打开真实浏览器或修改用户归档数据。真实知乎验证必须说明访问范围，并使用最小采样量。
