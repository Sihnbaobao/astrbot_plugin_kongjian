# Changelog

本仓库从 `astrbot_plugin_qzone` v3.2.0 的源码接续维护（上游仓库已不可访问），版本号自 v0.1.0 重新起算。

## v0.1.0

初始版本，源码基线为上游 v3.2.0。

Changes:

* 插件更名为 `astrbot_plugin_kongjian`，配置文件名、数据目录、Web API 路由前缀同步改名。
* 定时读说说不再一次性连评多条：新增 `trigger.comment_max_per_run`（单次最多评论几条，默认 1）与 `trigger.comment_unique_author`（同一位好友单次只评论一条，默认开启），多条之间随机间隔 5~10 秒。
* 修正调度器启动日志把偏移秒数写成“分钟”的问题。
* 修正 README 中遗留的 `publish_offset_minutes` / `comment_offset_minutes` 配置名。
