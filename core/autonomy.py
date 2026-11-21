"""自主行为：让 QQ 空间像真人一样，自己想发就发、想点赞就点赞。"""

import asyncio
import json
import random
import re
import time
from datetime import datetime, time as dtime

from astrbot.api import logger

from . import activity, body_sleep
from .config import PluginConfig
from .llm_action import clean_stage_direction
from .model import Post
from .sender import Sender
from .service import PostService


class AutoAct:
    """隔一段随机时间醒来一次，问模型这一刻想不想做点什么。

    醒来间隔在 auto_act_interval_min / auto_act_interval_max 之间随机，
    不做固定排期，避免变成"每天定时发一条"。
    """

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        self.cfg = config
        self.service = service
        self.sender = sender
        self.job_name = "AutoAct"
        self.state_path = config.data_dir / "auto_act_state.json"
        self._task: asyncio.Task | None = None
        self._terminated = False

    # ==================== 调度 ====================

    def start(self) -> None:
        if self._terminated or self._task is not None:
            return
        low, high = self._interval_range()
        delay = random.uniform(low, high)
        logger.info(f"[AutoAct] 下次醒来：{delay / 60:.0f} 分钟后")
        self._task = asyncio.create_task(self._sleep_then_run(delay))

    def _interval_range(self) -> tuple[float, float]:
        low = max(1, int(self.cfg.trigger.auto_act_interval_min or 60))
        high = max(low, int(self.cfg.trigger.auto_act_interval_max or low))
        return low * 60, high * 60

    async def _sleep_then_run(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._terminated:
                return
            await self.do_task()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[AutoAct] 执行失败：{e}")
        finally:
            self._task = None
            if not self._terminated:
                self.start()

    async def terminate(self) -> None:
        self._terminated = True
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("[AutoAct] 已停止")

    # ==================== 状态 ====================

    def _today(self) -> str:
        return self.cfg.now().strftime("%Y-%m-%d")

    def _load_state(self) -> dict:
        state = {
            "date": self._today(),
            "posts": 0,
            "interacts": 0,
            "done": [],
            "next_post_at": 0,
        }
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except Exception as e:
            logger.warning(f"[AutoAct] 读取状态失败：{e}")
            return state
        if not isinstance(data, dict) or data.get("date") != state["date"]:
            # 跨天后次数清零，但两条说说之间的冷却要延续
            state["next_post_at"] = int(data.get("next_post_at") or 0)
            return state
        state["posts"] = int(data.get("posts") or 0)
        state["next_post_at"] = int(data.get("next_post_at") or 0)
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

    def _post_gap_ready(self, state: dict) -> bool:
        """两条说说之间要隔一段随机时长，避免每天准点一条。"""
        return time.time() >= int(state.get("next_post_at") or 0)

    async def _own_recent(self, limit: int = 5) -> list[Post]:
        """读自己最近发过的说说，避免重复自己。"""
        try:
            uin = await self.service.session.get_uin()
            posts = await self.service.query_feeds(target_id=str(uin), num=limit)
        except Exception as e:
            logger.debug(f"[AutoAct] 读取自己的说说失败：{e}")
            return []
        return [post for post in posts if (post.text or post.rt_con)][:limit]

    def _own_digest(self, posts: list[Post]) -> str:
        lines = []
        for post in posts:
            text = " ".join((post.text or post.rt_con or "").split())
            if not text:
                continue
            when = ""
            if post.create_time:
                try:
                    when = datetime.fromtimestamp(
                        post.create_time, self.cfg.timezone
                    ).strftime("%m-%d ")
                except (ValueError, OSError):
                    when = ""
            lines.append(f"- {when}{text[:60]}")
        return "\n".join(lines)

    @staticmethod
    def _too_similar(text: str, recent: list[Post], threshold: float = 0.5) -> bool:
        """和最近发过的说说太像就不发了，省得重复。"""
        def normalize(value: str) -> str:
            return re.sub(r"[\s\W_]+", "", str(value or "").lower())

        target = normalize(text)
        if len(target) < 6:
            return False
        target_grams = {target[i : i + 2] for i in range(len(target) - 1)}
        for post in recent:
            other = normalize(post.text or post.rt_con or "")
            if len(other) < 6:
                continue
            other_grams = {other[i : i + 2] for i in range(len(other) - 1)}
            if not target_grams or not other_grams:
                continue
            overlap = len(target_grams & other_grams) / len(target_grams | other_grams)
            if overlap >= threshold:
                return True
        return False

    async def _candidates(self, state: dict) -> list[Post]:
        try:
            posts = await self.service.query_feeds(pos=0, num=10, no_self=True)
        except Exception as e:
            logger.debug(f"[AutoAct] 拉取好友动态失败：{e}")
            return []
        done = set(state["done"])
        result: list[Post] = []
        for post in posts:
            if not post.tid or post.tid in done:
                continue
            # 兜底任务、手动机器人、页面评论过的说说都不要再评一次
            if await self.service.has_own_comment(post):
                continue
            result.append(post)
            if len(result) >= 5:
                break
        return result

    # ==================== 执行 ====================

    async def do_task(self):
        if self._in_quiet_hours():
            logger.debug("[AutoAct] 静默时段，跳过")
            return

        # 她真的睡着了就什么都不做。身体插件的睡眠优先于固定时段：
        # 没睡的时候半夜也可能想发，睡着的时候白天也不会发。
        sleep_state = body_sleep.read(self.cfg.context)
        if sleep_state["asleep"]:
            logger.info(
                f"[AutoAct] 睡着了，跳过这一轮：{sleep_state['line'] or sleep_state['kind']}"
            )
            return

        plan = body_sleep.read(self.cfg.context)
        now = self.cfg.now()
        now_ts = now.timestamp()
        # Only act inside her awake window: no posting while she naps, right
        # before bed, or when a companion plugin is already doing something.
        bedtime = float(plan.get("bedtime") or 0.0)
        if bedtime and now_ts > bedtime - 3600:
            logger.debug("[AutoAct] 快到她睡觉的时间了，跳过")
            return
        nap_at = float(plan.get("nap_at") or 0.0)
        if nap_at and nap_at - 600 <= now_ts <= nap_at + 3600:
            logger.debug("[AutoAct] 午睡时间，跳过")
            return
        busy_until, busy_kind = activity.busy("astrbot_plugin_kongjian", now_ts)
        if busy_until > now_ts:
            logger.info(f"[AutoAct] 她正在忙（{busy_kind}），这一轮让给她")
            return

        state = self._load_state()
        post_limit = int(self.cfg.trigger.auto_act_daily_post_limit or 0)
        interact_limit = int(self.cfg.trigger.auto_act_daily_interact_limit or 0)
        allow_post = state["posts"] < post_limit and self._post_gap_ready(state)
        allow_interact = state["interacts"] < interact_limit
        if not allow_post and not allow_interact:
            logger.debug("[AutoAct] 今日额度已用完，跳过")
            return

        candidates = await self._candidates(state)
        own_recent = await self._own_recent()
        urge = self._persona_urge()
        # 给她补上“刚聊过什么 / 记得什么”，否则写说说时只剩人格空转
        chat_digest = await self.service.llm.recent_chat_digest(limit=20)
        memory_query = (
            chat_digest[-200:]
            or urge
            or " ".join(
                (post.text or post.rt_con or "")[:80] for post in candidates[:3]
            )
        )
        memory_digest = (
            await self.service.llm.memory_digest(memory_query, k=5)
            if memory_query
            else ""
        )
        logger.info(
            f"[AutoAct] 决策上下文：群聊 {len(chat_digest)} 字，"
            f"记忆 {len(memory_digest.splitlines()) if memory_digest else 0} 条，"
            f"候选动态 {len(candidates)} 条，自己发过 {len(own_recent)} 条"
        )
        decision = await self.service.llm.decide_action(
            candidates,
            allow_post=allow_post,
            allow_interact=allow_interact,
            urge=urge,
            chat_digest=chat_digest,
            memory_digest=memory_digest,
            own_digest=self._own_digest(own_recent),
        )
        if not decision:
            return

        action = str(decision.get("action") or "none").strip().lower()
        reason = str(decision.get("reason") or "").strip()
        if action == "none":
            logger.info(f"[AutoAct] 这一刻什么都不做：{reason}")
            return

        if action == "post":
            activity.write("astrbot_plugin_kongjian", "发说说", "", now_ts + 600)
            if not allow_post:
                logger.info(f"[AutoAct] 今天已经发过说说，忽略：{reason}")
                return
            await self._post(state, str(decision.get("text") or ""), own_recent)
            return

        if not allow_interact:
            logger.info(f"[AutoAct] 今天的互动次数已用完，忽略：{reason}")
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
        activity.write("astrbot_plugin_kongjian", "逛空间", "", now_ts + 480)
        if action == "like":
            await self._like(post, state)
        elif action == "comment":
            await self._comment(post, state)
        else:
            logger.warning(f"[AutoAct] 未知动作：{action}")

    async def _post(
        self, state: dict, text: str = "", recent: list[Post] | None = None
    ) -> None:
        # 决策时模型已经想好一句“想发的话”，优先用它；没有才回头去翻群聊
        text = clean_stage_direction(text) if text else ""
        if not text:
            try:
                text = await self.service.llm.generate_post()
            except Exception as e:
                logger.warning(f"[AutoAct] 生成说说失败：{e}")
                return
        if not text:
            return
        if recent and self._too_similar(text, recent):
            logger.info(f"[AutoAct] 和最近发过的太像，这次不发了：{text[:40]}")
            return
        try:
            post = await self.service.publish_post(text=text)
        except Exception as e:
            logger.warning(f"[AutoAct] 发布说说失败：{e}")
            return
        state["posts"] += 1
        gap_min = max(1, int(self.cfg.trigger.auto_act_post_gap_min or 18))
        gap_max = max(gap_min, int(self.cfg.trigger.auto_act_post_gap_max or gap_min))
        state["next_post_at"] = int(
            time.time() + random.uniform(gap_min, gap_max) * 3600
        )
        self._save_state(state)
        logger.info(f"[AutoAct] 下一条说说至少隔到：{gap_min}~{gap_max} 小时后")
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
