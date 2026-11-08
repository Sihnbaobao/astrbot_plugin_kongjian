"""自主行为：让 QQ 空间像真人一样，自己想发就发、想点赞就点赞。"""

import json
from datetime import time as dtime

from astrbot.api import logger

from .config import PluginConfig
from .model import Post
from .scheduler import AutoRandomCronTask
from .sender import Sender
from .service import PostService


class AutoAct(AutoRandomCronTask):
    """周期性醒来一次，问模型这一刻想不想做点什么。

    动作用模型决定，频率和上限由配置兜底，避免人格一上头就刷屏。
    """

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        super().__init__(
            "AutoAct",
            config.trigger.auto_act_cron,
            config.timezone,
            config.trigger.auto_act_offset,
        )
        self.cfg = config
        self.service = service
        self.sender = sender
        self.state_path = config.data_dir / "auto_act_state.json"

    # ==================== 状态 ====================

    def _today(self) -> str:
        return self.cfg.now().strftime("%Y-%m-%d")

    def _load_state(self) -> dict:
        state = {"date": self._today(), "posts": 0, "interacts": 0, "done": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except Exception as e:
            logger.warning(f"[AutoAct] 读取状态失败：{e}")
            return state
        if not isinstance(data, dict) or data.get("date") != state["date"]:
            return state
        state["posts"] = int(data.get("posts") or 0)
        state["interacts"] = int(data.get("interacts") or 0)
        state["done"] = [str(tid) for tid in (data.get("done") or [])]
        return state

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[AutoAct] 保存状态失败：{e}")

    # ==================== 条件 ====================

    def _in_quiet_hours(self) -> bool:
        raw = str(self.cfg.trigger.auto_act_quiet_hours or "").strip()
        if "-" not in raw:
            return False
        start_raw, end_raw = raw.split("-", 1)
        try:
            start = dtime.fromisoformat(start_raw.strip())
            end = dtime.fromisoformat(end_raw.strip())
        except ValueError:
            logger.warning(f"[AutoAct] 静默时段格式错误：{raw}")
            return False
        now = self.cfg.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def _persona_urge(self) -> str:
        """读取人格插件给出的“此刻念头”，插件不存在时返回空串。"""
        try:
            star = self.cfg.context.get_registered_star(
                "astrbot_plugin_persona_presence"
            )
            plugin = getattr(star, "star_cls", None) if star else None
            getter = getattr(plugin, "qzone_urge", None)
            if callable(getter):
                return str(getter() or "").strip()
        except Exception as e:
            logger.debug(f"[AutoAct] 读取人格念头失败：{e}")
        return ""

    async def _candidates(self, state: dict) -> list[Post]:
        try:
            posts = await self.service.query_feeds(pos=0, num=10, no_self=True)
        except Exception as e:
            logger.debug(f"[AutoAct] 拉取好友动态失败：{e}")
            return []
        done = set(state["done"])
        return [post for post in posts if post.tid and post.tid not in done][:5]

    # ==================== 执行 ====================

    async def do_task(self):
        if self._in_quiet_hours():
            logger.debug("[AutoAct] 静默时段，跳过")
            return

        state = self._load_state()
        post_limit = int(self.cfg.trigger.auto_act_daily_post_limit or 0)
        interact_limit = int(self.cfg.trigger.auto_act_daily_interact_limit or 0)
        if state["posts"] >= post_limit and state["interacts"] >= interact_limit:
            logger.debug("[AutoAct] 今日额度已用完，跳过")
            return

        candidates = await self._candidates(state)
        decision = await self.service.llm.decide_action(
            candidates,
            today_posts=state["posts"],
            today_interacts=state["interacts"],
            post_limit=post_limit,
            interact_limit=interact_limit,
            urge=self._persona_urge(),
        )
        if not decision:
            return

        action = str(decision.get("action") or "none").strip().lower()
        reason = str(decision.get("reason") or "").strip()
        if action == "none":
            logger.info(f"[AutoAct] 这一刻什么都不做：{reason}")
            return

        if action == "post":
            if state["posts"] >= post_limit:
                logger.info(f"[AutoAct] 今日发说说额度已满：{reason}")
                return
            await self._post(state)
            return

        if state["interacts"] >= interact_limit:
            logger.info(f"[AutoAct] 今日互动额度已满：{reason}")
            return

        try:
            index = int(decision.get("target"))
        except (TypeError, ValueError):
            logger.warning(f"[AutoAct] 无效的目标：{decision.get('target')}")
            return
        if not 0 <= index < len(candidates):
            logger.warning(f"[AutoAct] 目标越界：{index}")
            return

        post = candidates[index]
        if action == "like":
            await self._like(post, state)
        elif action == "comment":
            await self._comment(post, state)
        else:
            logger.warning(f"[AutoAct] 未知动作：{action}")

    async def _post(self, state: dict) -> None:
        try:
            text = await self.service.llm.generate_post()
        except Exception as e:
            logger.warning(f"[AutoAct] 生成说说失败：{e}")
            return
        if not text:
            return
        try:
            post = await self.service.publish_post(text=text)
        except Exception as e:
            logger.warning(f"[AutoAct] 发布说说失败：{e}")
            return
        state["posts"] += 1
        self._save_state(state)
        logger.info(f"[AutoAct] 自主发说说：{text[:40]}")
        if self.cfg.trigger.send_admin:
            await self.sender.send_admin_post(post, message="自主发说说")

    async def _like(self, post: Post, state: dict) -> None:
        try:
            await self.service.like_posts(post)
        except Exception as e:
            logger.warning(f"[AutoAct] 点赞失败：{e}")
            return
        self._remember(post, state)
        logger.info(f"[AutoAct] 自主点赞 → {post.name}")

    async def _comment(self, post: Post, state: dict) -> None:
        try:
            await self.service.comment_posts(post)
        except Exception as e:
            logger.warning(f"[AutoAct] 评论失败：{e}")
            return
        if self.cfg.trigger.like_when_comment:
            try:
                await self.service.like_posts(post)
            except Exception as e:
                logger.warning(f"[AutoAct] 评论后点赞失败：{e}")
        self._remember(post, state)
        logger.info(f"[AutoAct] 自主评论 → {post.name}")
        if self.cfg.trigger.send_admin:
            await self.sender.send_admin_post(post, message="自主评论")

    def _remember(self, post: Post, state: dict) -> None:
        state["interacts"] += 1
        if post.tid and post.tid not in state["done"]:
            state["done"].append(post.tid)
        state["done"] = state["done"][-100:]
        self._save_state(state)
