# 知乎 Computer Use 日常归档流程

本流程不修改“知乎备份剪藏”油猴脚本。浏览器负责加载正文和评论，`clipboard_bridge.py` 负责统一输出、图片本地化、数据库去重和扫描边界。默认文章保存在本项目的 `data/articles/YYYY/MM/`，数据库为本项目的 `zhihu_articles.db`；路径根据脚本位置计算，不受启动时工作目录影响。

## 固定配置

- 模型：GPT-5.6 Luna
- 推理强度：Low
- Fast：关闭
- 同一步最多重试一次；再次失败即停止并报告
- 评论排序：默认
- 评论目标：不超过 30 条时尽可能全部保存；否则保存约一半，最多 200 条
- 默认展开前三条热门评论的“查看全部回复/展开其他回复”
- 正常完成条件：命中上一轮边界；没有命中边界时不得更新边界

## 开始一次运行

```powershell
python clipboard_bridge.py begin-run
```

保存返回的 `run_id` 和 `boundary_keys`。

如果需要按评论总数计算本篇目标，可运行：

```powershell
python clipboard_bridge.py comment-target --total 204
```

返回的目标为 102；总数超过 400 时目标保持为 200。

固定扫描主页为 `https://www.zhihu.com/people/li-xiang-57-76`。每次运行都在用户的 Chrome 中新开该地址，不复用旧标签页；桥接程序的默认 scope 也是该地址。

## 每条动态

1. 从最新动态向下处理，读取赞同活动时间、内容 URL 和内容 ID。
2. 调用 `inspect` 登记扫描顺序并查询是否已保存、是否命中旧边界：

```powershell
python clipboard_bridge.py inspect --run-id RUN_ID --content-key "answer:123456"
```

3. 如果 `boundary=true`，结束扫描并成功提交边界。
4. 如果 `known=true` 但不是边界，跳过下载并继续向下。
5. 对新内容打开评论区，先展开前三条热门评论的回复。
6. 滚动到初始评论底部，点击“点击查看全部评论”。
7. 在弹窗内分批滚动。第一次滚动一段距离后点击“暂存此页评论”，再点击“查看暂存数”。
8. 数量不足时继续滚动、再次暂存并查看；插件按评论 ID 去重。
9. 达到目标或 200 条上限后关闭评论弹窗，不刷新页面。
10. 确认“保存评论”已勾选，点击“复制为 Markdown”，等待“复制成功”。
11. 调用桥接程序；活动时间必须使用赞同动态时间而非回答发布时间：

```powershell
python clipboard_bridge.py ingest --run-id RUN_ID --activity-time "2026-09-13 21:08" --activity-action "赞同了回答" --author "作者名" --published-at "2026-09-12 20:01" --source-type "answer" --content-url "https://www.zhihu.com/question/1/answer/2" --expected-title "页面上的完整标题"
```

12. 只有返回 `status=saved` 后才进入下一条。

## 结束运行

命中旧边界：

```powershell
python clipboard_bridge.py finish-run --run-id RUN_ID --boundary-hit
```

确认到达列表末尾：

```powershell
python clipboard_bridge.py finish-run --run-id RUN_ID --reached-end
```

验证码、登录失效、页面结构异常、同一步连续失败两次或未命中边界时，不传完成参数。该次运行会标记为 `incomplete`，不会更新边界。
