# pots.py

from __future__ import annotations

import json
import time
from typing import Literal, get_args

import aiosqlite

from .config import PluginConfig
from .model import Comment, Post

PostKey = Literal[
    "id",
    "tid",
    "uin",
    "name",
    "avatar_url",
    "gin",
    "status",
    "anon",
    "text",
    "images",
    "videos",
    "create_time",
    "rt_con",
    "comments",
    "extra_text",
]
POST_KEYS = set(get_args(PostKey))


class PostDB:
    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
            id=row[0],
            tid=row[1],
            uin=row[2],
            name=row[3],
            avatar_url=row[4],
            gin=row[5],
            text=row[6],
            images=json.loads(row[7]),
            videos=json.loads(row[8]),
            anon=bool(row[9]),
            status=row[10],
            create_time=row[11],
            rt_con=row[12],
            comments=[Comment.model_validate(c) for c in json.loads(row[13])],
            extra_text=row[14],
        )

    @staticmethod
    def _encode_urls(urls: list[str]) -> str:
        return json.dumps(urls, ensure_ascii=False)

    async def initialize(self):
        """初始化数据库"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT UNIQUE,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    avatar_url TEXT,
                    gin INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    images TEXT NOT NULL CHECK(json_valid(images)),
                    videos TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(videos)),
                    anon INTEGER NOT NULL CHECK(anon IN (0,1)),
                    status TEXT NOT NULL,
                    create_time INTEGER NOT NULL,
                    rt_con TEXT NOT NULL DEFAULT '',
                    comments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments)),
                    extra_text TEXT
                )
            """)
            await self._ensure_avatar_url_column(db)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS qzone_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT NOT NULL,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    snippet TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_qzone_actions_created
                ON qzone_actions(created_at)
            """)
            await db.commit()

    @staticmethod
    async def _ensure_avatar_url_column(db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(posts)") as cursor:
            columns = await cursor.fetchall()
        if any(column[1] == "avatar_url" for column in columns):
            return
        await db.execute("ALTER TABLE posts ADD COLUMN avatar_url TEXT")

    async def add(self, post: Post) -> int:
        """添加稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            cur = await db.execute(
                """
                INSERT INTO posts (tid, uin, name, avatar_url, gin, text, images, videos, anon, status, create_time, rt_con, comments, extra_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.avatar_url,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                ),
            )
            await db.commit()
            last_id = cur.lastrowid  # 获取自增ID
            assert last_id is not None
            return last_id

    async def get(self, value, key: PostKey = "id") -> Post | None:
        """
        根据指定字段查询一条稿件记录，默认按 id 查询。
        当 key=='id' 且 value==-1 时，返回 id 最大的那一条记录。
        """
        if value is None:
            raise ValueError("必须提供查询值")
        if key not in POST_KEYS:
            raise ValueError(f"不允许的查询字段: {key}")
        async with aiosqlite.connect(self.db_path) as db:
            # 关键判断：-1 代表取最大 ID
            if key == "id" and value == -1:
                query = "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
                async with db.execute(query) as cursor:
                    row = await cursor.fetchone()
                    return self._row_to_post(row) if row else None
            # 普通查询保持原逻辑
            query = f"SELECT * FROM posts WHERE {key} = ? LIMIT 1"
            async with db.execute(query, (value,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_post(row) if row else None

    async def list(
        self,
        offset: int = 0,
        limit: int = 1,
        *,
        reverse: bool = False,
    ) -> list[Post]:
        """
        批量获取稿件

        offset: 起始偏移（0 表示最早的）
        limit: 数量
        reverse: 是否反转顺序（True = 最新优先）
        """
        if offset < 0 or limit <= 0:
            return []

        order = "DESC" if reverse else "ASC"

        async with aiosqlite.connect(self.db_path) as db:
            query = f"""
                SELECT * FROM posts
                ORDER BY id {order}
                LIMIT ? OFFSET ?
            """
            async with db.execute(query, (limit, offset)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_post(row) for row in rows]

    async def update(self, post: Post) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            await db.execute(
                """
                UPDATE posts SET
                    tid = ?, uin = ?, name = ?, avatar_url = ?, gin = ?, text = ?,
                    images = ?, videos = ?, anon = ?, status = ?,
                    create_time = ?, rt_con = ?, comments = ?, extra_text = ?
                WHERE id = ?
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.avatar_url,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                    post.id,
                ),
            )
            await db.commit()

    async def save(self, post: Post) -> int | None:
        """
        保存 Post：
        1. 有 tid → 尝试按 tid 更新
        2. 有 id  → 按 id 更新
        3. 否则   → 新增
        """
        # 1. 优先用 tid 去重
        if post.tid:
            old = await self.get(post.tid, key="tid")
            if old:
                post.id = old.id
                await self.update(post)
                return post.id

        # 2. 有 id 就更新
        if post.id is not None:
            await self.update(post)
            return post.id

        # 3. 新记录
        post.id = await self.add(post)
        return post.id

    async def log_action(self, post: Post, action: str) -> None:
        """记一条空间动作（view / like / comment / reply），供日后回忆。"""
        snippet = " ".join((post.text or post.rt_con or "").split())[:80]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO qzone_actions (tid, uin, name, snippet, action, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(post.tid or ""),
                    int(post.uin or 0),
                    post.name or "",
                    snippet,
                    action,
                    int(time.time()),
                ),
            )
            await db.commit()

    async def recent_actions(
        self,
        *,
        days: int = 7,
        uin: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """取最近若干天的空间动作，最新在前。"""
        since = int(time.time()) - max(1, days) * 86400
        sql = (
            "SELECT action, uin, name, snippet, created_at FROM qzone_actions "
            "WHERE created_at >= ?"
        )
        params: list = [since]
        if uin:
            sql += " AND uin = ?"
            params.append(int(uin))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "action": row[0],
                "uin": row[1],
                "name": row[2],
                "snippet": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    async def delete(self, post_id: int) -> int:
        """删除稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            await db.commit()
            return cur.rowcount
