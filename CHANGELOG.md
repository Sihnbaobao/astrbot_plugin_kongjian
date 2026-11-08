# Changelog

本仓库从 `astrbot_plugin_qzone` v3.2.0 的源码接续维护（上游仓库已不可访问），版本号自 v0.1.0 重新起算。

## v0.2.0

目标从「人下命令、bot 执行」改成「bot 自己决定」。

- 新增自主行为引擎 `AutoAct`：按 `auto_act_cron` 周期性醒来，由模型结合人格、今日活动与好友动态决定发说说 / 点赞 / 评论 / 什么都不做，并受每日额度与静默时段约束。
- 移除定时发说说 `AutoPublish`，发说说改由自主行为触发。
- 移除表白墙（投稿 / 匿名投稿 / 撤稿 / 看稿 / 过稿 / 拒稿）及稿件流程。
- 命令只保留管理员兜底：看说说、发说说、删说说；其余交互交给 LLM 工具与自主行为。
- 移除聊天时按概率读说说的 `prob_read_feed` 与 `read_prob` 配置。
- 定时任务补充 `misfire_grace_time`，修复事件循环繁忙时任务被跳过、整条任务链永久断掉的问题。
- 定时任务没有消息事件时，退回使用全局默认人格，而不是丢失人格上下文。

## v0.1.0

初始版本，源码基线为上游 v3.2.0。

Changes:

- 插件更名为 `astrbot_plugin_kongjian`，配置文件名、数据目录、Web API 路由前缀同步改名。
- 定时读说说不再一次性连评多条：新增 `trigger.comment_max_per_run`（默认 1）与 `trigger.comment_unique_author`（默认开启）。
- 修正调度器启动日志把偏移秒数写成“分钟”的问题。
- 修正 README 中遗留的 `publish_offset_minutes` / `comment_offset_minutes` 配置名。
