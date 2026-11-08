
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_kongjian?name=astrbot_plugin_kongjian&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_kongjian

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) QQ空间对接插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Sihnbaobao-blue)](https://github.com/Sihnbaobao)

</div>

## 🤝 介绍

QQ空间对接插件：让 bot 像真人一样自己逛空间、自己发说说、自己点赞评论，而不是等你下命令。

> 本仓库自 v0.1.0 起自主维护，源码基线为 `astrbot_plugin_qzone` v3.2.0（原作者 Zhalslar，上游仓库已不可访问）。
> 原项目采用 MIT 协议，本仓库保留原始版权声明。

## 📦 安装

- 直接在astrbot的插件市场搜索 astrbot_plugin_kongjian，点击安装，等待完成即可

- 也可以克隆源码到插件文件夹：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Sihnbaobao/astrbot_plugin_kongjian

# 控制台重启AstrBot
```

## ⌨️ 配置

请前往插件配置面板进行配置

### 提示词说明

- `llm.main_prompt`：全局主提示词，写说说 / 评论 / 回复 / 自主行动四个场景**共享**。
- `llm.post_prompt` / `llm.comment_prompt` / `llm.reply_prompt` / `llm.act_prompt`：各场景独立提示词，拼接在主提示词之后。
- 最终提示词 =【全局主提示词】+【当前场景独立提示词】+【场景附加要求】（如输出格式要求）。
- 支持占位符：`{current_time}`（当前时间）、`{scene}`（当前场景：写说说 / 评论说说 / 回复评论）。
- 此外，system prompt 末尾会固定附带一段“当前时间”，确保 LLM 知道现在是什么时候。

### 聊天记录来源

- 写说说时只读取最近 `source.post_days` 天内（按日期）的聊天记录。
- 未指定群聊时，会从可用群聊中随机抽取 `source.group_session_pool` 个作为候选会话，依次尝试；若某个会话在指定日期内没有聊天记录，则自动切换到下一个会话，直到拿到内容为止。

说说和系统消息统一使用内置卡片渲染器，不再需要配置渲染器或外部样式目录。卡片渲染未生成图片时，发送原始文本。

### 自主行为说明

插件不再按固定脚本发说说，而是按 `auto_act_cron` 的节奏醒来一次，把「今天已经做过什么 + 好友最近发了什么 + 人格此刻的念头」交给模型，由模型决定：发一条说说、给谁点个赞、评论一句，还是什么都不做。

- `auto_act_cron`：醒来节奏，默认 `0 * * * *`（每小时一次），留空表示关闭。
- `auto_act_offset`：基准时间前后的随机浮动（单位**秒**），默认 1800。
- `auto_act_quiet_hours`：静默时段，默认 `01:00-08:00`。
- `auto_act_daily_post_limit` / `auto_act_daily_interact_limit`：每天发说说 / 互动的次数上限。
- `comment_cron`：兜底任务，到点扫一遍好友动态，给还没评论过的说说补一条；另有 `comment_max_per_run`（单次最多几条）与 `comment_unique_author`（同一位好友只评一条）防止刷屏。

与人格插件联动：如果装了 `astrbot_plugin_persona_presence`，且在其实例上实现了 `qzone_urge()`（返回一小段“此刻想干什么”的文本），会自动拼进决策上下文；没有这个插件也能正常工作。

## 🐔 使用说明

### 一、命令

日常行为由 bot 自己决定，命令只保留管理员兜底：

| 命令 | 别名 | 权限 | 参数 | 功能说明 |
|----|----|----|----|----|
| 看说说 | 查看说说 | ADMIN | `[@用户] [序号/范围]` | 查看好友动态 / 指定用户空间（会拉完整详情） |
| 发说说 | - | ADMIN | `<文本> [图片]` | 手动立即发布一条说说 |
| 删说说 | - | ADMIN | `<序号/范围>` | 删除自己空间里的说说 |

序号从 0 开始（0 = 最新，-1 = 最后），支持 `2~5` 范围语法。

```text
看说说
看说说 2
看说说 1~3
看说说 @某人 0
```

### 二、LLM 工具

bot 在聊天里自己决定要不要调用：

| 工具 | 作用 |
|---|---|
| `llm_view_feed` | 查看某个人的第 `pos` 条说说，可顺手点赞 / 评论 |
| `llm_publish_feed` | 直接发一条说说（可带当前对话里的图片） |

### 三、插件页面

WebUI → 插件 → QQ空间：好友动态 / 我的空间 / 查某人空间，可以刷、点赞、评论、发说说。

## 💡 TODO

- [x] 自主发说说 / 点赞 / 评论
- [x] 插件页面（刷空间、发说说）
- [x] LLM 工具
- [ ] 与 persona_presence 的 curiosity 模块打通
- [ ] 点赞结果自检（回读 `data-islike`）

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 问题与建议请直接提 [Issue](https://github.com/Sihnbaobao/astrbot_plugin_kongjian/issues)。

## 🤝 鸣谢

- 部分代码参考了[CampuxBot项目](https://github.com/idoknow/CampuxBot)，由作者之一的Soulter推荐

- [QQ 空间爬虫之爬取说说](https://kylingit.com/blog/qq-空间爬虫之爬取说说/)
  感谢这篇博客提供的思路。

- [一个QQ空间爬虫项目](https://github.com/wwwpf/QzoneExporter)

- [QQ空间](https://qzone.qq.com/) 网页显示本地数据时使用的样式与布局均来自于QQ空间。
