import base64
import json
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.autonomy import AutoAct
from .core.config import PluginConfig
from .core.db import PostDB
from .core.llm_action import LLMAction
from .core.model import Comment, Post
from .core.qzone import QzoneAPI, QzoneParser, QzoneSession
from .core.scheduler import AutoComment
from .core.sender import Sender
from .core.service import PostService
from .core.utils import get_ats, get_image_urls, parse_range

try:
    from quart import jsonify as _quart_jsonify
    from quart import request as _quart_request
except Exception:
    _quart_jsonify = None
    _quart_request = None


class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 自主行为模块
        self.auto_act: AutoAct | None = None
        self._page_post_refs: dict[str, Post] = {}
        self._register_page_web_apis()

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        # 定时任务与后台通知都依赖 CQHttp 客户端，尽早绑定一次
        self._capture_page_client()

        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

        if not self.auto_act and self.cfg.trigger.auto_act_enabled:
            self.auto_act = AutoAct(self.cfg, self.service, self.sender)
            self.auto_act.start()

    def _register_page_web_apis(self) -> None:
        routes = (
            ("page/status", self.page_status, ["GET"], "Qzone dashboard status"),
            ("page/feed", self.page_feed, ["GET"], "Qzone dashboard feed"),
            ("page/detail", self.page_detail, ["GET"], "Qzone dashboard detail"),
            ("page/publish", self.page_publish, ["POST"], "Qzone dashboard publish"),
            ("page/like", self.page_like, ["POST"], "Qzone dashboard like"),
            ("page/comment", self.page_comment, ["POST"], "Qzone dashboard comment"),
            ("page/reply", self.page_reply, ["POST"], "Qzone dashboard reply"),
            ("page/delete", self.page_delete, ["POST"], "Qzone dashboard delete"),
            ("page/config", self.page_config, ["GET"], "Qzone plugin config"),
            (
                "page/config/update",
                self.page_config_update,
                ["POST"],
                "Qzone plugin config update",
            ),
            (
                "page/upload-media",
                self.page_upload_media,
                ["POST"],
                "Qzone dashboard upload media",
            ),
        )
        for endpoint, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/astrbot_plugin_kongjian/{endpoint}",
                handler,
                methods,
                desc,
            )

    async def _page_response(self, payload: dict, status: int = 200):
        if _quart_jsonify is None:
            return payload
        response = _quart_jsonify(payload)
        response.status_code = status
        return response

    async def _page_json(self, callback):
        try:
            payload = await callback()
            status = 200
        except Exception as exc:
            logger.exception("qzone page api failed: %s", exc)
            message = str(exc) or "请求失败"
            payload = {
                "ok": False,
                # AstrBot 的插件页桥接只识别字符串类型的 message/error，
                # 保留 error 对象的同时补一个顶层 message，避免真实异常被
                # 前端统一显示成 “Request failed with status code 400”。
                "message": message,
                "error": {"message": message},
            }
            status = 400
        return await self._page_response(payload, status)

    async def _page_query_params(self) -> dict:
        if _quart_request is None:
            return {}
        args = getattr(_quart_request, "args", {}) or {}
        try:
            return {str(key): value for key, value in args.items()}
        except Exception:
            return dict(args)

    async def _page_json_body(self) -> dict:
        if _quart_request is None:
            return {}
        try:
            data = await _quart_request.get_json(silent=True)
        except TypeError:
            data = await _quart_request.get_json()
        return data if isinstance(data, dict) else {}

    async def _build_page_status(self) -> dict:
        self._capture_page_client()
        try:
            uin = await self.session.get_uin()
            nickname = await self.session.get_nickname()
            bound = True
        except Exception:
            uin = 0
            nickname = ""
            bound = False
        return {
            "ok": True,
            "data": {
                "daemon": {"state": "ready" if bound else "offline"},
                "login": {
                    "bound": bound,
                    "uin": uin,
                    "nickname": nickname,
                    "avatar": "",
                },
                "limits": {
                    "feed": 10,
                    "images": 9,
                },
            },
        }

    def _remember_page_post(self, post: Post) -> str:
        post_id = f"{post.uin}:{post.tid}"
        self._page_post_refs[post_id] = post
        return post_id

    def _capture_page_client(self):
        if self.cfg.client is not None:
            return self.cfg.client
        try:
            platform = self.context.get_platform("aiocqhttp")
        except Exception:
            platform = None
        if platform is None:
            try:
                for candidate in self.context.platform_manager.platform_insts:
                    meta = candidate.meta()
                    if getattr(meta, "name", "") == "aiocqhttp":
                        platform = candidate
                        break
            except Exception:
                platform = None
        bot = getattr(platform, "bot", None) if platform is not None else None
        if bot is not None:
            self.cfg.client = bot
        return self.cfg.client

    def _page_post_payload(
        self,
        post: Post,
        *,
        include_comments: bool = False,
        self_uin: int = 0,
    ) -> dict:
        images = list(dict.fromkeys([*(post.images or []), *(post.videos or [])]))
        payload = {
            "id": self._remember_page_post(post),
            "author": {
                "uin": post.uin,
                "nickname": post.name or str(post.uin),
                "avatar": post.avatar_url or "",
            },
            "content": post.text or post.rt_con or "",
            "created_at": int(post.create_time or 0),
            "stats": {
                "likes": 0,
                "comments": len(post.comments or []),
            },
            "liked": False,
            "images": images,
            "can_delete": bool(self_uin and int(post.uin) == int(self_uin)),
        }
        if include_comments:
            payload["comments"] = [
                {
                    "id": str(comment.tid or index),
                    "author": {
                        "uin": comment.uin,
                        "nickname": comment.nickname or str(comment.uin),
                    },
                    "content": comment.content,
                    "created_at": int(comment.create_time or 0),
                    "can_reply": bool(comment.tid and comment.uin),
                    "parent_id": str(comment.parent_tid)
                    if comment.parent_tid
                    else None,
                }
                for index, comment in enumerate(post.comments or [], start=1)
            ]
        return payload

    def _require_page_post(self, post_id: str) -> Post:
        post = self._page_post_refs.get(str(post_id or "").strip())
        if not post:
            raise RuntimeError("说说引用已失效，请刷新页面后重试")
        return post

    async def page_status(self):
        return await self._page_json(self._build_page_status)

    async def page_feed(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            scope = str(params.get("scope") or "friends")
            self_uin = await self.session.get_uin()
            target_id = None
            no_self = False
            if scope == "self":
                target_id = str(self_uin)
            elif scope == "profile":
                target_id = str(params.get("hostuin") or "").strip() or None
            else:
                no_self = False
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=0,
                num=min(max(int(params.get("limit") or 10), 1), 10),
                with_detail=False,
                no_self=no_self,
            )
            return {
                "ok": True,
                "data": {
                    "items": [
                        self._page_post_payload(post, self_uin=self_uin)
                        for post in posts
                    ],
                    "cursor": "",
                    "has_more": False,
                },
            }

        return await self._page_json(handler)

    async def page_detail(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            post = self._require_page_post(params.get("id", ""))
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                raise RuntimeError(resp.message or "获取详情失败")
            parsed = QzoneParser.parse_feeds([resp.data])
            matched = parsed[0] if parsed else post
            self._remember_page_post(matched)
            await self.db.log_action(matched, "view")
            return {
                "ok": True,
                "data": {
                    "post": self._page_post_payload(
                        matched,
                        include_comments=True,
                        self_uin=await self.session.get_uin(),
                    )
                },
            }

        return await self._page_json(handler)

    async def page_publish(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            media = body.get("media") or []
            images = []
            for item in media:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source") or "")
                if source.startswith("base64://"):
                    images.append(base64.b64decode(source.removeprefix("base64://")))
            post = await self.service.publish_post(
                text=str(body.get("content") or ""),
                images=images,
            )
            return {
                "ok": True,
                "data": {"post": self._page_post_payload(post)},
                "message": "说说已发布。",
            }

        return await self._page_json(handler)

    async def page_like(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            resp = await self.qzone.like(post)
            if not resp.ok:
                raise RuntimeError(resp.message or "点赞失败")
            await self.db.log_action(post, "like")
            return {"ok": True, "data": {"liked": True, "verified": True}}

        return await self._page_json(handler)

    async def page_comment(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            if not content:
                raise RuntimeError("评论内容不能为空")
            resp = await self.qzone.comment(post, content)
            if not resp.ok:
                raise RuntimeError(resp.message or "评论失败")
            await self.db.log_action(post, "comment")
            comment = {
                "id": f"local-{len(post.comments) + 1}",
                "content": content,
                "author": {
                    "uin": await self.session.get_uin(),
                    "nickname": await self.session.get_nickname(),
                    "avatar": "",
                },
            }
            post.comments.append(
                Comment(
                    uin=comment["author"]["uin"],
                    nickname=comment["author"]["nickname"],
                    content=content,
                    create_time=0,
                    tid=0,
                    parent_tid=None,
                )
            )
            return {"ok": True, "data": {"comment": comment}, "message": "评论已发送。"}

        return await self._page_json(handler)

    async def page_reply(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            comment_id = str(body.get("commentid") or "")
            comment_uin = str(body.get("comment_uin") or body.get("commentuin") or "")
            if not content:
                raise RuntimeError("回复内容不能为空")

            # 楼中楼的 tid 是主评论内的楼层号（都从 1 开始），单靠 tid 会匹配到
            # 第一条同号评论；前端会带 comment_uin，用它一起定位。
            def _matches(item: Comment) -> bool:
                if str(item.tid) != comment_id:
                    return False
                return not comment_uin or str(item.uin) == comment_uin

            target = next((item for item in post.comments if _matches(item)), None)
            if target is None:
                raise RuntimeError("未找到要回复的评论")

            # 楼中楼回复：QQ 空间只有两级评论，回复楼中楼实际是在其主评论下
            # 再发一条 @对方的楼中楼，commentId 必须用主评论的 tid。
            reply_target = target
            reply_uin = None
            reply_nick = None
            target_comment_uin = None
            if target.parent_tid:
                main = next(
                    (
                        item
                        for item in post.comments
                        if not item.parent_tid
                        and str(item.tid) == str(target.parent_tid)
                    ),
                    None,
                )
                if main is None:
                    raise RuntimeError("未找到该楼中楼所属的主评论")
                reply_target = Comment(
                    uin=target.uin,
                    nickname=target.nickname,
                    content=target.content,
                    create_time=target.create_time,
                    tid=main.tid,
                    parent_tid=None,
                )
                reply_uin = target.uin
                reply_nick = target.nickname
                target_comment_uin = main.uin

            resp = await self.qzone.reply(
                post,
                reply_target,
                content,
                reply_uin=reply_uin,
                reply_nick=reply_nick,
                comment_uin=target_comment_uin,
            )
            if not resp.ok:
                raise RuntimeError(resp.message or "回复失败")
            # 不同代理把新评论 ID 放在不同层级（顶层 tid 或 data.tid），
            # 取不到时留空，后续详情刷新会补上。
            inner = resp.data.get("data")
            inner = inner if isinstance(inner, dict) else {}
            reply = {
                "id": str(inner.get("tid") or resp.data.get("tid") or ""),
                "content": content,
                "author": {
                    "uin": await self.session.get_uin(),
                    "nickname": await self.session.get_nickname(),
                    "avatar": "",
                },
            }
            # 服务端是两级结构：回复楼中楼时新评论挂在主评论下（reply_target
            # 已被换成主评论），缓存里也要保持一致，否则下次按 parent_tid
            # 找不到所属主评论。
            cached_parent_tid = reply_target.tid if target.parent_tid else None
            post.comments.append(
                Comment(
                    uin=reply["author"]["uin"],
                    nickname=reply["author"]["nickname"],
                    content=content,
                    create_time=0,
                    tid=int(reply["id"]) if reply["id"].isdigit() else 0,
                    parent_tid=cached_parent_tid,
                )
            )
            return {"ok": True, "data": {"reply": reply}, "message": "回复已发送。"}

        return await self._page_json(handler)

    async def page_delete(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            self_uin = await self.session.get_uin()
            if int(post.uin) != int(self_uin):
                raise RuntimeError("只能删除自己发布的说说")
            resp = await self.qzone.delete(str(post.tid or ""))
            if not resp.ok:
                raise RuntimeError(resp.message or "删除失败")
            return {"ok": True, "data": {}, "message": "说说已删除。"}

        return await self._page_json(handler)

    async def page_upload_media(self):
        async def handler():
            if _quart_request is None:
                raise RuntimeError("当前环境不支持上传")
            files = await _quart_request.files
            upload = files.get("file") or files.get("image") or files.get("media")
            if upload is None:
                raise RuntimeError("没有收到图片文件")
            data = await upload.read()
            return {
                "ok": True,
                "data": {
                    "media": {
                        "kind": "image",
                        "name": upload.filename or "image.jpg",
                        "source": f"base64://{base64.b64encode(data).decode('ascii')}",
                        "size": len(data),
                        "mime_type": getattr(upload, "content_type", "")
                        or "image/jpeg",
                    }
                },
            }

        return await self._page_json(handler)

    # ==================== 配置读写 ====================

    def _config_schema(self) -> dict:
        path = self.cfg.plugin_dir / "_conf_schema.json"
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.error(f"读取插件配置 schema 失败：{e}")
            return {}

    @staticmethod
    def _coerce_config_value(spec: dict, value):
        """按 schema 把前端传来的值转成正确类型，顺手做范围裁剪。"""
        kind = spec.get("type")
        slider = spec.get("slider") or {}
        if kind == "bool":
            return bool(value)
        if kind in ("int", "float"):
            try:
                number = int(value) if kind == "int" else float(value)
            except (TypeError, ValueError):
                return None
            if "min" in slider:
                number = max(slider["min"], number)
            if "max" in slider:
                number = min(slider["max"], number)
            return number
        if kind == "list":
            if isinstance(value, str):
                return [line.strip() for line in value.splitlines() if line.strip()]
            if isinstance(value, (list, tuple)):
                return [str(item).strip() for item in value if str(item).strip()]
            return []
        return "" if value is None else str(value)

    def _apply_config(self, values: dict) -> list[str]:
        """把前端提交的值写回插件配置，返回实际改动的键。"""
        schema = self._config_schema()
        changed: list[str] = []
        for key, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("type") == "object":
                section = values.get(key)
                if not isinstance(section, dict):
                    continue
                node = getattr(self.cfg, key, None)
                if node is None:
                    continue
                for sub, sub_spec in (spec.get("items") or {}).items():
                    if sub not in section:
                        continue
                    converted = self._coerce_config_value(sub_spec, section[sub])
                    if converted is None:
                        continue
                    setattr(node, sub, converted)
                    changed.append(f"{key}.{sub}")
            elif key in values:
                converted = self._coerce_config_value(spec, values[key])
                if converted is None:
                    continue
                setattr(self.cfg, key, converted)
                changed.append(key)
        if changed:
            self.cfg.save_config()
        return changed

    async def _reload_schedulers(self) -> None:
        """配置改动后按新参数重建定时任务。"""
        if self.auto_comment:
            await self.auto_comment.terminate()
            self.auto_comment = None
        if self.auto_act:
            await self.auto_act.terminate()
            self.auto_act = None
        if self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)
        if self.cfg.trigger.auto_act_enabled:
            self.auto_act = AutoAct(self.cfg, self.service, self.sender)
            self.auto_act.start()

    async def page_config(self):
        async def handler():
            values = json.loads(
                json.dumps(dict(self.cfg.raw_data()), ensure_ascii=False, default=str)
            )
            return {
                "ok": True,
                "data": {"schema": self._config_schema(), "values": values},
            }

        return await self._page_json(handler)

    async def page_config_update(self):
        async def handler():
            body = await self._page_json_body()
            values = body.get("values")
            if not isinstance(values, dict):
                raise RuntimeError("配置格式不正确")
            changed = self._apply_config(values)
            if changed:
                await self._reload_schedulers()
            return {
                "ok": True,
                "data": {"changed": changed},
                "message": "配置已保存" if changed else "没有需要保存的改动",
            }

        return await self._page_json(handler)

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.auto_act:
            await self.auto_act.terminate()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def capture_client(self, event: AiocqhttpMessageEvent):
        """记录 CQHttp 客户端，定时任务和后台通知都依赖它。"""
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")

    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
            return posts
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent):
        """
        看说说 <@群友> <序号>
        """
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        """发说说 <内容> <图片>, 由用户指定内容"""
        text = event.message_str.partition(" ")[2]
        images = await get_image_urls(event)
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AiocqhttpMessageEvent):
        """删说说 <稿件ID>"""
        posts = await self._get_posts(event, target_id=event.get_self_id())
        for post in posts:
            try:
                await self.sender.send_post(event, post, message="已删除说说")
                await self.service.delete_post(post)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    async def _target_post(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None,
        pos: int,
        *,
        with_detail: bool = False,
    ) -> Post | None:
        """取某人空间里的第 pos 条说说，user_id 为空时取发消息的人。"""
        target_id = str(user_id or event.get_sender_id())
        logger.debug(f"正在查询用户（{target_id}）的第 {pos} 条说说")
        posts = await self.service.query_feeds(
            target_id=target_id,
            pos=pos,
            num=1,
            with_detail=with_detail,
        )
        return posts[0] if posts else None

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
    ):
        """
        看某个人的QQ空间。你想知道他最近发了什么的时候使用。
        Args:
            user_id(string): 目标QQ号，一串数字；不填就看作当前说话的人
            pos(number): 看第几条，0 表示最新的一条，默认 0
        """
        try:
            post = await self._target_post(event, user_id, pos, with_detail=True)
            if not post:
                return "查询结果为空"
            await self.db.log_action(post, "view")
            await self.sender.send_post(event, post, message="看说说")
            return f"{post.name}：{post.text}\n" + "\n".join(post.images)
        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_like_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
    ):
        """
        给某个人的一条说说点赞。看到让你觉得好笑、认同或者想表示在看的动态时用，
        不用每条都点。
        Args:
            user_id(string): 目标QQ号，一串数字；不填就看作当前说话的人
            pos(number): 第几条，0 表示最新的一条，默认 0
        """
        try:
            post = await self._target_post(event, user_id, pos)
            if not post:
                return "没有找到这条说说"
            await self.service.like_posts(post)
            await self.sender.send_post(event, post, message="已点赞")
            return f"已给{post.name}的说说点赞"
        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_comment_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
    ):
        """
        在某人的一条说说下面留一句评论。只有真的想接话时才用；
        评论内容按你的人格风格生成，不需要你另外写。
        Args:
            user_id(string): 目标QQ号，一串数字；不填就看作当前说话的人
            pos(number): 第几条，0 表示最新的一条，默认 0
        """
        try:
            post = await self._target_post(event, user_id, pos)
            if not post:
                return "没有找到这条说说"
            await self.service.comment_posts(post, event=event)
            if self.cfg.trigger.like_when_comment:
                await self.service.like_posts(post)
            await self.sender.send_post(event, post, message="已评论")
            return f"已在{post.name}的说说下留言"
        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """
        在自己的QQ空间发一条说说。当你有话想说、有值得记一下的小事，
        或者想让别人知道你现在的状态时用；不用为了发而发。
        Args:
            text(string): 说说正文
            get_image(boolean): 是否把当前对话里的图片一起发出去，默认 True
        """
        images = await get_image_urls(event) if get_image else []
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间: \n" + post.text + "\n" + "\n".join(post.images)
        except Exception as e:
            return str(e)

    @filter.llm_tool()
    async def llm_recall_qzone(
        self,
        event: AiocqhttpMessageEvent,
        days: int = 7,
        user_id: str | None = None,
    ):
        """
        回想你最近在 QQ 空间里做过什么：看过、赞过、评论过谁的哪条说说。
        当有人问「你是不是看过我的说说」「你昨天是不是给我点赞了」这类事情时用。
        Args:
            days(integer): 往回查几天，默认 7，最多 30
            user_id(string): 只看某个 QQ 号，不填就查全部
        """
        labels = {
            "view": "看过",
            "like": "点了赞",
            "comment": "评论了",
            "reply": "回复了",
        }
        try:
            days = max(1, min(int(days or 7), 30))
            uin = int(user_id) if user_id and str(user_id).isdigit() else None
            rows = await self.db.recent_actions(days=days, uin=uin, limit=20)
            if not rows:
                return f"最近 {days} 天你在空间里没有任何动作记录"
            lines = [f"最近 {days} 天你在空间里的动作（最新在前）："]
            for row in rows:
                when = datetime.fromtimestamp(
                    row["created_at"], self.cfg.timezone
                ).strftime("%m-%d %H:%M")
                label = labels.get(row["action"], row["action"])
                lines.append(
                    f"- {when} {label} {row['name']}({row['uin']})「{row['snippet']}」"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.error(e)
            return str(e)
